# ===========================================================================
#  exporter.py  —  batch FBX export with validation gate
#  Maya 2026 / PySide6.  Sits alongside validator.py in the same tool.
#
#  Workflow:  validate (run_all) -> if clean, export -> route by prefix.
#  Refuses to export anything that fails validation.
# ===========================================================================

import os
import maya.cmds as cmds
import maya.mel as mel

# import the validator we already built so the exporter can gate on it
import validator


# ---------------------------------------------------------------------------
# Where each prefix lands inside your UE-mirrored export tree.
# ---------------------------------------------------------------------------
SUBFOLDER_BY_PREFIX = {
    "SM_": "StaticMeshes",
    "SK_": "SkeletalMeshes",
}
DEFAULT_SUBFOLDER = "Misc"   # anything we don't have a rule for


def _ensure_fbx_plugin():
    """FBX export is a plugin; make sure it's loaded before we call it."""
    if not cmds.pluginInfo("fbxmaya", query=True, loaded=True):
        cmds.loadPlugin("fbxmaya")


def _configure_fbx_for_unreal():
    """
    Set FBX export options for clean Maya -> Unreal static meshes.
    Done via MEL because the FBX command set is MEL-only, even in 2026.
    """
    mel.eval('FBXResetExport')                       # start from a known state
    mel.eval('FBXExportSmoothingGroups -v true')     # UE needs smoothing groups
    mel.eval('FBXExportSmoothMesh -v false')         # don't export subdiv preview
    mel.eval('FBXExportTangents -v true')            # tangents/binormals for normal maps
    mel.eval('FBXExportTriangulate -v false')        # let UE triangulate
    mel.eval('FBXExportInstances -v false')
    mel.eval('FBXExportReferencedAssetsContent -v true')
    mel.eval('FBXExportUpAxis z')                    # UE is Z-up
    mel.eval('FBXExportFileVersion -v FBX202000')    # modern, UE-friendly version
    mel.eval('FBXExportInAscii -v false')            # binary = smaller/faster


def _prefix_of(name):
    """Return the asset prefix (e.g. 'SM_') from a short name, or None."""
    for p in SUBFOLDER_BY_PREFIX:
        if name.startswith(p):
            return p
    return None


def _target_path(output_root, name):
    """Build the full FBX path for an asset, routed by its prefix."""
    prefix = _prefix_of(name)
    sub = SUBFOLDER_BY_PREFIX.get(prefix, DEFAULT_SUBFOLDER)
    folder = os.path.join(output_root, sub)
    if not os.path.isdir(folder):
        os.makedirs(folder)            # create the subfolder if missing
    return os.path.join(folder, name + ".fbx")


def _export_selection_to(path):
    """Export whatever is currently selected to a single FBX at `path`."""
    # FBXExport's -f flag wants forward slashes even on Windows
    safe = path.replace("\\", "/")
    mel.eval('FBXExport -f "{}" -s'.format(safe))    # -s = export selected only


def export_assets(output_root, transforms=None, combine=False, validate=True):
    """
    Validate, then export.

    output_root : folder to export into (the UE-mirrored tree root).
    transforms  : list of transforms to export. None = every exportable mesh.
    combine     : True  -> all transforms into ONE fbx (e.g. the ship).
                  False -> one fbx per asset (the kit workflow).
    validate    : True  -> run the validator first and ABORT on any failure.

    Returns a result dict:
      {"ok": bool, "exported": [paths], "failures": [validator failures], "message": str}
    """
    _ensure_fbx_plugin()

    if transforms is None:
        transforms = validator._all_mesh_transforms()

    if not transforms:
        return {"ok": False, "exported": [], "failures": [],
                "message": "Nothing to export — no meshes found."}

    # ---- the gate ----
    if validate:
        failures = validator.run_all(transforms)
        if failures:
            return {"ok": False, "exported": [], "failures": failures,
                    "message": "Export blocked: {} validation issue(s). "
                               "Fix them, then export.".format(len(failures))}

    _configure_fbx_for_unreal()

    # remember the user's current selection so we can restore it afterward
    prior_selection = cmds.ls(selection=True, long=True)
    exported = []

    try:
        if combine:
            # one FBX containing everything; name it from the first asset
            cmds.select(transforms, replace=True)
            first_name = transforms[0].split("|")[-1]
            path = _target_path(output_root, first_name)
            _export_selection_to(path)
            exported.append(path)
        else:
            # one FBX per asset, routed by prefix
            for t in transforms:
                name = t.split("|")[-1]
                cmds.select(t, replace=True)
                path = _target_path(output_root, name)
                _export_selection_to(path)
                exported.append(path)
    finally:
        # always restore the selection, even if an export raised
        if prior_selection:
            cmds.select(prior_selection, replace=True)
        else:
            cmds.select(clear=True)

    return {"ok": True, "exported": exported, "failures": [],
            "message": "Exported {} file(s).".format(len(exported))}

# ===========================================================================
#  Exporter UI  (Maya 2026 / PySide6)
#  Append to the bottom of exporter.py
# ===========================================================================

from PySide6 import QtWidgets, QtCore
from shiboken6 import wrapInstance
import maya.OpenMayaUI as omui


def _maya_main_window():
    ptr = omui.MQtUtil.mainWindow()
    return wrapInstance(int(ptr), QtWidgets.QWidget)


class ExporterUI(QtWidgets.QDialog):

    def __init__(self, parent=None):
        super().__init__(parent or _maya_main_window())
        self.setWindowTitle("FBX Exporter")
        self.setMinimumWidth(540)
        self._build()
        self._load_last_path()

    def _build(self):
        layout = QtWidgets.QVBoxLayout(self)
        layout.setSpacing(8)

        # --- output folder row ---
        path_row = QtWidgets.QHBoxLayout()
        path_row.addWidget(QtWidgets.QLabel("Export to:"))
        self.path_field = QtWidgets.QLineEdit()
        self.path_field.setPlaceholderText("…/NorthwestPassage/exports")
        path_row.addWidget(self.path_field, stretch=1)
        browse = QtWidgets.QPushButton("Browse…")
        browse.clicked.connect(self._browse)
        path_row.addWidget(browse)
        layout.addLayout(path_row)

        # --- options ---
        self.scene_radio = QtWidgets.QRadioButton("Whole scene")
        self.sel_radio = QtWidgets.QRadioButton("Selected only")
        self.sel_radio.setChecked(True)
        scope_box = QtWidgets.QHBoxLayout()
        scope_box.addWidget(QtWidgets.QLabel("Scope:"))
        scope_box.addWidget(self.sel_radio)
        scope_box.addWidget(self.scene_radio)
        scope_box.addStretch(1)
        layout.addLayout(scope_box)

        self.combine_check = QtWidgets.QCheckBox(
            "Combine into one FBX (use for multi-part assets)")
        layout.addWidget(self.combine_check)

        self.validate_check = QtWidgets.QCheckBox(
            "Validate before export (recommended)")
        self.validate_check.setChecked(True)
        layout.addWidget(self.validate_check)

        # --- export button ---
        self.export_btn = QtWidgets.QPushButton("Validate && Export")
        self.export_btn.setMinimumHeight(36)
        self.export_btn.clicked.connect(self._on_export)
        layout.addWidget(self.export_btn)

        # --- status / results ---
        self.status = QtWidgets.QLabel("Ready.")
        self.status.setWordWrap(True)
        self.status.setStyleSheet("font-weight: bold;")
        layout.addWidget(self.status)

        self.results = QtWidgets.QListWidget()
        self.results.setMaximumHeight(180)
        layout.addWidget(self.results)

    def _browse(self):
        folder = QtWidgets.QFileDialog.getExistingDirectory(
            self, "Choose export folder", self.path_field.text() or "")
        if folder:
            self.path_field.setText(folder)

    def _load_last_path(self):
        # remember last export folder between sessions via an optionVar
        if cmds.optionVar(exists="nwp_export_path"):
            self.path_field.setText(cmds.optionVar(query="nwp_export_path"))

    def _save_last_path(self, path):
        cmds.optionVar(stringValue=("nwp_export_path", path))

    def _on_export(self):
        out = self.path_field.text().strip()
        if not out:
            self._fail("Pick an export folder first.")
            return
        if not os.path.isdir(out):
            self._fail("That folder doesn't exist: {}".format(out))
            return

        self._save_last_path(out)

        # gather transforms based on scope
        if self.sel_radio.isChecked():
            sel = cmds.ls(selection=True, long=True, type="transform") or []
            # keep only transforms that actually have a mesh shape
            transforms = [t for t in sel
                          if cmds.listRelatives(t, shapes=True, type="mesh", fullPath=True)]
            if not transforms:
                self._fail("Nothing valid selected — select mesh objects, "
                           "or switch to Whole scene.")
                return
        else:
            transforms = None   # exporter defaults to whole scene

        result = export_assets(
            out,
            transforms=transforms,
            combine=self.combine_check.isChecked(),
            validate=self.validate_check.isChecked(),
        )

        self.results.clear()

        if result["ok"]:
            self.status.setText("✓  " + result["message"])
            self.status.setStyleSheet("font-weight: bold; color: #82c77d;")
            for p in result["exported"]:
                self.results.addItem(p)
        else:
            self.status.setText("✗  " + result["message"])
            self.status.setStyleSheet("font-weight: bold; color: #e0635c;")
            # if blocked by validation, list the issues so they're actionable
            for f in result["failures"]:
                self.results.addItem("[{}] {} — {}".format(
                    f["check"], f["name"], f["reason"]))

    def _fail(self, msg):
        self.status.setText("✗  " + msg)
        self.status.setStyleSheet("font-weight: bold; color: #e0635c;")


_exporter_window = None

def show_exporter():
    """Call from script editor:  import exporter; exporter.show_exporter()"""
    global _exporter_window
    try:
        _exporter_window.close()
    except Exception:
        pass
    _exporter_window = ExporterUI()
    _exporter_window.show()