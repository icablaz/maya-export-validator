# ===========================================================================
#  asset_tools.py  —  Northwest Passage Asset Tools   (Maya 2026 / PySide6)
#
#  One self-contained tool: validate Maya assets against the studio
#  convention, then batch-export clean FBX to a UE-mirrored folder tree.
#
#  Launch:   import asset_tools; asset_tools.show()
#
#  Layout of this file:
#     1. Validation checks   (the rules — single source of truth)
#     2. run_all             (merge checks into one report)
#     3. Export logic        (validate-gated FBX export)
#     4. UI                  (tabbed window: Validate | Export)
# ===========================================================================

import os
import re
import maya.cmds as cmds
import maya.mel as mel
import maya.OpenMayaUI as omui

from PySide6 import QtWidgets, QtCore, QtGui
from shiboken6 import wrapInstance


# ===========================================================================
#  1. VALIDATION CHECKS
# ===========================================================================

PREFIX_BY_TYPE = {
    "mesh": "SM_",          # static mesh
    "skinnedMesh": "SK_",   # skeletal mesh
}
SUFFIX_PATTERN = re.compile(r"^[A-Z][A-Za-z0-9]*(_[A-Z0-9][A-Za-z0-9]*)*$")


def _all_mesh_transforms():
    """Every mesh transform in the scene. Shared by all checks + export."""
    mesh_shapes = cmds.ls(type="mesh", long=True) or []
    transforms = []
    for shape in mesh_shapes:
        parent = cmds.listRelatives(shape, parent=True, fullPath=True)
        if parent:
            transforms.append(parent[0])
    return list(set(transforms))


def _selected_mesh_transforms():
    """Selected transforms that actually have a mesh shape."""
    sel = cmds.ls(selection=True, long=True, type="transform") or []
    return [t for t in sel
            if cmds.listRelatives(t, shapes=True, type="mesh", fullPath=True)]


def _expected_prefix(transform):
    shapes = cmds.listRelatives(transform, shapes=True, fullPath=True) or []
    if not shapes:
        return None
    shape = shapes[0]
    history = cmds.listHistory(shape) or []
    is_skinned = any(cmds.nodeType(h) == "skinCluster" for h in history)
    if cmds.nodeType(shape) == "mesh":
        return PREFIX_BY_TYPE["skinnedMesh"] if is_skinned else PREFIX_BY_TYPE["mesh"]
    return None


def check_naming(transforms=None):
    """Prefix + PascalCase convention. Returns list of failure dicts."""
    if transforms is None:
        transforms = _all_mesh_transforms()
    failures = []
    for t in transforms:
        name = t.split("|")[-1]
        expected = _expected_prefix(t)
        if expected is None:
            continue
        if not name.startswith(expected):
            failures.append({"object": t, "name": name,
                             "reason": "missing prefix '{}' (skeletal vs static?)".format(expected)})
            continue
        suffix = name[len(expected):]
        if not SUFFIX_PATTERN.match(suffix):
            failures.append({"object": t, "name": name,
                             "reason": "bad format after prefix (use PascalCase, e.g. IcebergLarge_01)"})
    return failures


def check_transforms(transforms=None):
    """Frozen rotation (0) and scale (1). Translation left alone."""
    if transforms is None:
        transforms = _all_mesh_transforms()
    failures = []
    tol = 0.0001
    for t in transforms:
        name = t.split("|")[-1]
        rot = cmds.xform(t, query=True, rotation=True)
        scl = cmds.xform(t, query=True, scale=True, relative=True)
        if any(abs(r) > tol for r in rot):
            failures.append({"object": t, "name": name,
                             "reason": "unfrozen rotation {} — freeze transforms".format(
                                 tuple(round(r, 2) for r in rot))})
        if any(abs(s - 1.0) > tol for s in scl):
            failures.append({"object": t, "name": name,
                             "reason": "non-1.0 scale {} — freeze transforms".format(
                                 tuple(round(s, 2) for s in scl))})
    return failures


def check_history(transforms=None):
    """Leftover construction history, ignoring expected node types."""
    if transforms is None:
        transforms = _all_mesh_transforms()
    IGNORE = {"mesh", "shadingEngine", "groupId", "groupParts", "tweak", "skinCluster"}
    failures = []
    for t in transforms:
        name = t.split("|")[-1]
        shapes = cmds.listRelatives(t, shapes=True, fullPath=True) or []
        if not shapes:
            continue
        history = cmds.listHistory(shapes[0]) or []
        dirty = [h for h in history if cmds.nodeType(h) not in IGNORE]
        if dirty:
            kinds = sorted({cmds.nodeType(h) for h in dirty})
            failures.append({"object": t, "name": name,
                             "reason": "construction history present ({}) — delete history".format(
                                 ", ".join(kinds))})
    return failures


def check_pivot(transforms=None):
    """Pivot stranded at world origin while geometry sits elsewhere."""
    if transforms is None:
        transforms = _all_mesh_transforms()
    failures = []
    tol, far = 0.001, 1.0
    for t in transforms:
        name = t.split("|")[-1]
        pivot = cmds.xform(t, query=True, worldSpace=True, rotatePivot=True)
        pivot_at_origin = all(abs(p) < tol for p in pivot)
        bbox = cmds.exactWorldBoundingBox(t)
        center = [(bbox[0]+bbox[3])/2.0, (bbox[1]+bbox[4])/2.0, (bbox[2]+bbox[5])/2.0]
        geometry_is_far = any(abs(c) > far for c in center)
        if pivot_at_origin and geometry_is_far:
            failures.append({"object": t, "name": name,
                             "reason": "pivot at world origin but mesh centered near {} — set the pivot".format(
                                 tuple(round(c, 1) for c in center))})
    return failures


def check_uvs(transforms=None):
    """UV health for texturing (Lumen project — no lightmap UV needed)."""
    if transforms is None:
        transforms = _all_mesh_transforms()
    failures = []
    for t in transforms:
        name = t.split("|")[-1]
        shapes = cmds.listRelatives(t, shapes=True, fullPath=True) or []
        if not shapes:
            continue
        shape = shapes[0]
        uv_sets = cmds.polyUVSet(shape, query=True, allUVSets=True) or []
        if not uv_sets:
            failures.append({"object": t, "name": name,
                             "reason": "no UV sets — mesh can't be textured"})
            continue
        coords = cmds.polyEditUV("{}.map[*]".format(shape), query=True) or []
        if not coords:
            failures.append({"object": t, "name": name,
                             "reason": "UV set exists but contains no UVs — needs unwrapping"})
            continue
        us, vs = coords[0::2], coords[1::2]
        m = 0.001
        if min(us) < -m or max(us) > 1.0+m or min(vs) < -m or max(vs) > 1.0+m:
            failures.append({"object": t, "name": name,
                             "reason": "UVs extend outside 0-1 space — check the unwrap layout"})
    return failures


# ===========================================================================
#  2. RUN ALL
# ===========================================================================

def run_all(transforms=None):
    """Run every check, tag each failure with its check name, merge to one list."""
    if transforms is None:
        transforms = _all_mesh_transforms()
    checks = {
        "naming": check_naming, "transforms": check_transforms,
        "history": check_history, "pivot": check_pivot, "uvs": check_uvs,
    }
    out = []
    for check_name, fn in checks.items():
        for f in fn(transforms):
            f["check"] = check_name
            out.append(f)
    return out


# ===========================================================================
#  3. EXPORT LOGIC
# ===========================================================================

SUBFOLDER_BY_PREFIX = {"SM_": "StaticMeshes", "SK_": "SkeletalMeshes"}
DEFAULT_SUBFOLDER = "Misc"


def _ensure_fbx_plugin():
    if not cmds.pluginInfo("fbxmaya", query=True, loaded=True):
        cmds.loadPlugin("fbxmaya")


def _configure_fbx_for_unreal():
    mel.eval('FBXResetExport')
    mel.eval('FBXExportSmoothingGroups -v true')
    mel.eval('FBXExportSmoothMesh -v false')
    mel.eval('FBXExportTangents -v true')
    mel.eval('FBXExportTriangulate -v false')
    mel.eval('FBXExportInstances -v false')
    mel.eval('FBXExportReferencedAssetsContent -v true')
    mel.eval('FBXExportUpAxis z')
    mel.eval('FBXExportFileVersion -v FBX202000')
    mel.eval('FBXExportInAscii -v false')


def _prefix_of(name):
    for p in SUBFOLDER_BY_PREFIX:
        if name.startswith(p):
            return p
    return None


def _target_path(output_root, name):
    sub = SUBFOLDER_BY_PREFIX.get(_prefix_of(name), DEFAULT_SUBFOLDER)
    folder = os.path.join(output_root, sub)
    if not os.path.isdir(folder):
        os.makedirs(folder)
    return os.path.join(folder, name + ".fbx")


def _export_selection_to(path):
    mel.eval('FBXExport -f "{}" -s'.format(path.replace("\\", "/")))


def export_assets(output_root, transforms=None, combine=False, validate=True):
    """Validate, then export. Returns {ok, exported, failures, message}."""
    _ensure_fbx_plugin()
    if transforms is None:
        transforms = _all_mesh_transforms()
    if not transforms:
        return {"ok": False, "exported": [], "failures": [],
                "message": "Nothing to export — no meshes found."}

    if validate:
        failures = run_all(transforms)
        if failures:
            return {"ok": False, "exported": [], "failures": failures,
                    "message": "Export blocked: {} validation issue(s). Fix them, then export.".format(len(failures))}

    _configure_fbx_for_unreal()
    prior = cmds.ls(selection=True, long=True)
    exported = []
    try:
        if combine:
            cmds.select(transforms, replace=True)
            path = _target_path(output_root, transforms[0].split("|")[-1])
            _export_selection_to(path)
            exported.append(path)
        else:
            for t in transforms:
                cmds.select(t, replace=True)
                path = _target_path(output_root, t.split("|")[-1])
                _export_selection_to(path)
                exported.append(path)
    finally:
        if prior:
            cmds.select(prior, replace=True)
        else:
            cmds.select(clear=True)

    return {"ok": True, "exported": exported, "failures": [],
            "message": "Exported {} file(s).".format(len(exported))}


# ===========================================================================
#  4. UI
# ===========================================================================

def _maya_main_window():
    ptr = omui.MQtUtil.mainWindow()
    return wrapInstance(int(ptr), QtWidgets.QWidget)


_CHECK_COLOR = {
    "naming": "#e0a132", "transforms": "#6aa9e0", "history": "#c77dff",
    "pivot": "#82c77d", "uvs": "#e0635c",
}


def _scope_transforms(selected_only):
    """Resolve which transforms a tab should act on, based on its scope toggle.
    Returns (transforms, error_message). transforms=None means whole scene."""
    if not selected_only:
        return None, None
    sel = _selected_mesh_transforms()
    if not sel:
        return None, "Nothing valid selected — select meshes or switch to Whole scene."
    return sel, None


class ValidateTab(QtWidgets.QWidget):

    def __init__(self):
        super().__init__()
        self._rows = []
        layout = QtWidgets.QVBoxLayout(self)
        layout.setSpacing(8)

        # scope toggle
        self.sel_radio = QtWidgets.QRadioButton("Selected only")
        self.scene_radio = QtWidgets.QRadioButton("Whole scene")
        self.sel_radio.setChecked(True)
        scope = QtWidgets.QHBoxLayout()
        scope.addWidget(QtWidgets.QLabel("Scope:"))
        scope.addWidget(self.sel_radio)
        scope.addWidget(self.scene_radio)
        scope.addStretch(1)
        layout.addLayout(scope)

        top = QtWidgets.QHBoxLayout()
        self.run_btn = QtWidgets.QPushButton("Validate")
        self.run_btn.setMinimumHeight(34)
        self.run_btn.clicked.connect(self.run)
        top.addWidget(self.run_btn)
        self.summary = QtWidgets.QLabel("Ready.")
        self.summary.setStyleSheet("font-weight: bold;")
        top.addWidget(self.summary, stretch=1)
        layout.addLayout(top)

        self.table = QtWidgets.QTableWidget(0, 3)
        self.table.setHorizontalHeaderLabels(["Check", "Object", "Problem"])
        self.table.horizontalHeader().setSectionResizeMode(2, QtWidgets.QHeaderView.Stretch)
        self.table.horizontalHeader().setSectionResizeMode(0, QtWidgets.QHeaderView.ResizeToContents)
        self.table.setEditTriggers(QtWidgets.QAbstractItemView.NoEditTriggers)
        self.table.setSelectionBehavior(QtWidgets.QAbstractItemView.SelectRows)
        self.table.itemSelectionChanged.connect(self._on_row_selected)
        layout.addWidget(self.table)

    def run(self):
        transforms, err = _scope_transforms(self.sel_radio.isChecked())
        if err:
            self.summary.setText("✗  " + err)
            self.summary.setStyleSheet("font-weight: bold; color: #e0635c;")
            return None

        failures = run_all(transforms)
        self.table.setRowCount(0)
        self._rows = []
        scope_word = "selection" if self.sel_radio.isChecked() else "scene"

        if not failures:
            self.summary.setText("✓  {} clean — export-ready.".format(scope_word.capitalize()))
            self.summary.setStyleSheet("font-weight: bold; color: #82c77d;")
        else:
            self.summary.setText("✗  {} issue(s) in {}.".format(len(failures), scope_word))
            self.summary.setStyleSheet("font-weight: bold; color: #e0635c;")
            for f in failures:
                row = self.table.rowCount()
                self.table.insertRow(row)
                item = QtWidgets.QTableWidgetItem(f["check"])
                item.setForeground(QtGui.QColor(_CHECK_COLOR.get(f["check"], "#cccccc")))
                self.table.setItem(row, 0, item)
                self.table.setItem(row, 1, QtWidgets.QTableWidgetItem(f["name"]))
                self.table.setItem(row, 2, QtWidgets.QTableWidgetItem(f["reason"]))
                self._rows.append(f["object"])

        self.window().on_validation_changed(failures)
        return failures

    def _on_row_selected(self):
        rows = self.table.selectionModel().selectedRows()
        if not rows:
            return
        obj = self._rows[rows[0].row()]
        if cmds.objExists(obj):
            cmds.select(obj, replace=True)


class ExportTab(QtWidgets.QWidget):

    def __init__(self):
        super().__init__()
        layout = QtWidgets.QVBoxLayout(self)
        layout.setSpacing(8)

        self.val_banner = QtWidgets.QLabel("Validation: not run yet.")
        self.val_banner.setStyleSheet(
            "padding: 6px; border-radius: 4px; background: #333; font-weight: bold;")
        self.val_banner.setWordWrap(True)
        layout.addWidget(self.val_banner)

        recheck = QtWidgets.QPushButton("Re-check now")
        recheck.clicked.connect(self._recheck)
        layout.addWidget(recheck)

        path_row = QtWidgets.QHBoxLayout()
        path_row.addWidget(QtWidgets.QLabel("Export to:"))
        self.path_field = QtWidgets.QLineEdit()
        self.path_field.setPlaceholderText("…/NorthwestPassage/exports")
        if cmds.optionVar(exists="nwp_export_path"):
            self.path_field.setText(cmds.optionVar(query="nwp_export_path"))
        path_row.addWidget(self.path_field, stretch=1)
        browse = QtWidgets.QPushButton("Browse…")
        browse.clicked.connect(self._browse)
        path_row.addWidget(browse)
        layout.addLayout(path_row)

        self.sel_radio = QtWidgets.QRadioButton("Selected only")
        self.sel_radio.setChecked(True)
        self.scene_radio = QtWidgets.QRadioButton("Whole scene")
        scope = QtWidgets.QHBoxLayout()
        scope.addWidget(QtWidgets.QLabel("Scope:"))
        scope.addWidget(self.sel_radio)
        scope.addWidget(self.scene_radio)
        scope.addStretch(1)
        layout.addLayout(scope)

        self.combine_check = QtWidgets.QCheckBox(
            "Combine into one FBX (multi-part assets)")
        layout.addWidget(self.combine_check)

        self.validate_check = QtWidgets.QCheckBox("Validate before export (recommended)")
        self.validate_check.setChecked(True)
        layout.addWidget(self.validate_check)

        self.export_btn = QtWidgets.QPushButton("Validate && Export")
        self.export_btn.setMinimumHeight(36)
        self.export_btn.clicked.connect(self._on_export)
        layout.addWidget(self.export_btn)

        self.status = QtWidgets.QLabel("Ready.")
        self.status.setWordWrap(True)
        self.status.setStyleSheet("font-weight: bold;")
        layout.addWidget(self.status)

        self.results = QtWidgets.QListWidget()
        self.results.setMaximumHeight(150)
        layout.addWidget(self.results)

    def update_validation_banner(self, failures):
        if failures is None:
            self.val_banner.setText("Validation: not run yet.")
            self.val_banner.setStyleSheet(
                "padding: 6px; border-radius: 4px; background: #333; font-weight: bold;")
        elif not failures:
            self.val_banner.setText("✓  Scene is clean — safe to export.")
            self.val_banner.setStyleSheet(
                "padding: 6px; border-radius: 4px; background: #2c4a2c; color: #b6e0b0; font-weight: bold;")
        else:
            self.val_banner.setText("✗  {} issue(s) — fix on the Validate tab.".format(len(failures)))
            self.val_banner.setStyleSheet(
                "padding: 6px; border-radius: 4px; background: #4a2c2c; color: #e8b0b0; font-weight: bold;")

    def _recheck(self):
        transforms, err = _scope_transforms(self.sel_radio.isChecked())
        if err:
            self.status.setText("✗  " + err)
            self.status.setStyleSheet("font-weight: bold; color: #e0635c;")
            return
        self.window().on_validation_changed(run_all(transforms))

    def _browse(self):
        folder = QtWidgets.QFileDialog.getExistingDirectory(
            self, "Choose export folder", self.path_field.text() or "")
        if folder:
            self.path_field.setText(folder)

    def _on_export(self):
        out = self.path_field.text().strip()
        if not out:
            return self._fail("Pick an export folder first.")
        if not os.path.isdir(out):
            return self._fail("That folder doesn't exist: {}".format(out))
        cmds.optionVar(stringValue=("nwp_export_path", out))

        transforms, err = _scope_transforms(self.sel_radio.isChecked())
        if err:
            return self._fail(err)

        result = export_assets(out, transforms=transforms,
                               combine=self.combine_check.isChecked(),
                               validate=self.validate_check.isChecked())
        self.results.clear()
        if result["ok"]:
            self.status.setText("✓  " + result["message"])
            self.status.setStyleSheet("font-weight: bold; color: #82c77d;")
            for p in result["exported"]:
                self.results.addItem(p)
            self.window().on_validation_changed([])
        else:
            self.status.setText("✗  " + result["message"])
            self.status.setStyleSheet("font-weight: bold; color: #e0635c;")
            for f in result["failures"]:
                self.results.addItem("[{}] {} — {}".format(f["check"], f["name"], f["reason"]))
            if result["failures"]:
                self.window().on_validation_changed(result["failures"])

    def _fail(self, msg):
        self.status.setText("✗  " + msg)
        self.status.setStyleSheet("font-weight: bold; color: #e0635c;")


class AssetToolsUI(QtWidgets.QDialog):

    def __init__(self, parent=None):
        super().__init__(parent or _maya_main_window())
        self.setWindowTitle("Asset Tools — Northwest Passage")
        self.setMinimumWidth(560)
        self.setMinimumHeight(480)
        layout = QtWidgets.QVBoxLayout(self)
        self.tabs = QtWidgets.QTabWidget()
        self.validate_tab = ValidateTab()
        self.export_tab = ExportTab()
        self.tabs.addTab(self.validate_tab, "Validate")
        self.tabs.addTab(self.export_tab, "Export")
        layout.addWidget(self.tabs)

    def on_validation_changed(self, failures):
        self.export_tab.update_validation_banner(failures)


_asset_tools_window = None

def show():
    """Launch:  import asset_tools; asset_tools.show()"""
    global _asset_tools_window
    try:
        _asset_tools_window.close()
    except Exception:
        pass
    _asset_tools_window = AssetToolsUI()
    _asset_tools_window.show()
