"""
validator.py — Maya -> Unreal export validator
"""

import re
import maya.cmds as cmds

# ---------------------------------------------------------------------------
# The naming standard, in one place so the UE auditor can share the same rules.
# Key = Maya node type, Value = required prefix.
# ---------------------------------------------------------------------------
PREFIX_BY_TYPE = {
    "mesh": "SM_",  # static mesh (most of your scene)
    "skinnedMesh": "SK_",  # skeletal mesh (the ship, if rigged)
}

# Pattern for the part AFTER the prefix:
#   - PascalCase words (Iceberg, Large)
#   - optional underscores between word-groups (Iceberg_Large)
#   - optional trailing _01 / _02 numbering
# Examples that PASS: IcebergLarge_01, Ship, Ice_Normal_03
# Examples that FAIL: iceberg (lowercase start), iceberg-large (hyphen),
#                     Iceberg large (space)
SUFFIX_PATTERN = re.compile(r"^[A-Z][A-Za-z0-9]*(_[A-Z0-9][A-Za-z0-9]*)*$")


def _expected_prefix(transform):
    """
    Given a transform node, look at its shape to decide what KIND of asset it
    is, and return the prefix it should have. Returns None if we don't have a
    rule for this type (so we skip it rather than false-flag it).
    """
    shapes = cmds.listRelatives(transform, shapes=True, fullPath=True) or []
    if not shapes:
        return None
    shape = shapes[0]

    # Is this mesh skinned (has a skinCluster in its history)? -> skeletal.
    history = cmds.listHistory(shape) or []
    is_skinned = any(cmds.nodeType(h) == "skinCluster" for h in history)

    if cmds.nodeType(shape) == "mesh":
        return PREFIX_BY_TYPE["skinnedMesh"] if is_skinned else PREFIX_BY_TYPE["mesh"]
    return None


def check_naming(transforms=None):
    """
    Check that mesh transforms follow the studio naming convention.

    transforms: optional list of transform names. If None, checks every
                mesh transform in the scene.

    Returns a list of failure dicts: {"object", "name", "reason"}.
    An empty list means everything passed.
    """
    if transforms is None:
        # Grab all mesh shapes, then their parent transforms (that's what we name).
        mesh_shapes = cmds.ls(type="mesh", long=True) or []
        transforms = []
        for shape in mesh_shapes:
            parent = cmds.listRelatives(shape, parent=True, fullPath=True)
            if parent:
                transforms.append(parent[0])
        transforms = list(set(transforms))  # de-duplicate

    failures = []

    for t in transforms:
        # short name = the part after the last "|" in Maya's long path
        name = t.split("|")[-1]

        expected = _expected_prefix(t)
        if expected is None:
            continue  # no rule for this type; not our job to judge it

        # Rule 1: correct prefix?
        if not name.startswith(expected):
            failures.append({
                "object": t,
                "name": name,
                "reason": f"missing prefix '{expected}' (skeletal vs static?)",
            })
            continue  # if the prefix is wrong, no point checking the suffix yet

        # Rule 2: is the part after the prefix valid PascalCase + numbering?
        suffix = name[len(expected):]
        if not SUFFIX_PATTERN.match(suffix):
            failures.append({
                "object": t,
                "name": name,
                "reason": "bad format after prefix (use PascalCase, e.g. IcebergLarge_01)",
            })

    return failures


# Quick manual test you can run in Maya's script editor:
#   import validator
#   for f in validator.check_naming():
#       print(f["name"], "->", f["reason"])

def check_transforms(transforms=None):
    """
    Check that mesh transforms are 'frozen': rotation at 0, scale at 1.
    (Translation is left alone — assets are often intentionally positioned.)

    Returns a list of failure dicts: {"object", "name", "reason"}.
    Empty list = everything passed.
    """
    if transforms is None:
        transforms = _all_mesh_transforms()

    failures = []
    tol = 0.0001  # floating-point wiggle room; never compare floats with ==

    for t in transforms:
        name = t.split("|")[-1]

        rot = cmds.xform(t, query=True, rotation=True)          # [rx, ry, rz]
        scl = cmds.xform(t, query=True, scale=True, relative=True)  # [sx, sy, sz]

        # rotation should be ~0 on every axis
        if any(abs(r) > tol for r in rot):
            failures.append({
                "object": t,
                "name": name,
                "reason": f"unfrozen rotation {tuple(round(r, 2) for r in rot)} — freeze transforms",
            })

        # scale should be ~1 on every axis
        if any(abs(s - 1.0) > tol for s in scl):
            failures.append({
                "object": t,
                "name": name,
                "reason": f"non-1.0 scale {tuple(round(s, 2) for s in scl)} — freeze transforms",
            })

    return failures


def check_history(transforms=None):
    """
    Check that meshes have no leftover construction history.

    We ignore a few node types that are normal/expected and not 'dirty'
    history (shading connections, the shape's own tweak node, skinClusters
    on rigged meshes).

    Returns a list of failure dicts: {"object", "name", "reason"}.
    """
    if transforms is None:
        transforms = _all_mesh_transforms()

    # node types that are fine to see in history and should NOT trigger a flag
    IGNORE = {"mesh", "shadingEngine", "groupId", "groupParts",
              "tweak", "skinCluster"}

    failures = []

    for t in transforms:
        name = t.split("|")[-1]

        shapes = cmds.listRelatives(t, shapes=True, fullPath=True) or []
        if not shapes:
            continue
        shape = shapes[0]

        history = cmds.listHistory(shape) or []
        # find history nodes that AREN'T in our ignore list
        dirty = [h for h in history if cmds.nodeType(h) not in IGNORE]

        if dirty:
            kinds = sorted({cmds.nodeType(h) for h in dirty})
            failures.append({
                "object": t,
                "name": name,
                "reason": f"construction history present ({', '.join(kinds)}) — delete history",
            })

    return failures


# ---------------------------------------------------------------------------
# Shared helper — pull every mesh transform in the scene. Used by all checks
# so the 'find the meshes' logic lives in ONE place, not copied per function.
# ---------------------------------------------------------------------------
def _all_mesh_transforms():
    mesh_shapes = cmds.ls(type="mesh", long=True) or []
    transforms = []
    for shape in mesh_shapes:
        parent = cmds.listRelatives(shape, parent=True, fullPath=True)
        if parent:
            transforms.append(parent[0])
    return list(set(transforms))

def check_pivot(transforms=None):
    """
    Check that a mesh's pivot isn't stranded at the world origin while the
    geometry sits somewhere else. That's the classic 'forgot to set the pivot'
    mistake — it makes the asset rotate/snap around empty space in Unreal.

    We do NOT enforce a specific pivot location (base vs center) because that
    legitimately varies by asset type. We only flag the clear error: pivot at
    (0,0,0) but the mesh's bounding-box center is far away.

    Returns a list of failure dicts: {"object", "name", "reason"}.
    """
    if transforms is None:
        transforms = _all_mesh_transforms()

    failures = []
    tol = 0.001       # how close to origin counts as 'at origin'
    far = 1.0         # how far the geometry must be before we care (scene units)

    for t in transforms:
        name = t.split("|")[-1]

        # pivot position in world space
        pivot = cmds.xform(t, query=True, worldSpace=True, rotatePivot=True)  # [x,y,z]
        pivot_at_origin = all(abs(p) < tol for p in pivot)

        # bounding box center in world space: bbox = [xmin,ymin,zmin,xmax,ymax,zmax]
        bbox = cmds.exactWorldBoundingBox(t)
        center = [
            (bbox[0] + bbox[3]) / 2.0,
            (bbox[1] + bbox[4]) / 2.0,
            (bbox[2] + bbox[5]) / 2.0,
        ]
        geometry_is_far = any(abs(c) > far for c in center)

        if pivot_at_origin and geometry_is_far:
            failures.append({
                "object": t,
                "name": name,
                "reason": f"pivot at world origin but mesh centered near "
                          f"{tuple(round(c, 1) for c in center)} — set the pivot",
            })

    return failures


def check_uvs(transforms=None):
    """
    Check UV health for texturing. (Lumen project — no lightmap UV needed,
    since Lumen is fully dynamic and bakes nothing.)

    Tiers:
      1. Mesh has at least one UV set (or it can't be textured at all).
      2. That UV set actually contains UVs (an empty set is a silent trap).
      3. UVs fall within the standard 0-1 space (UVs sprawling far outside
         0-1 usually mean an unwrap was never laid out properly).

    Returns a list of failure dicts: {"object", "name", "reason"}.
    """
    if transforms is None:
        transforms = _all_mesh_transforms()

    failures = []

    for t in transforms:
        name = t.split("|")[-1]

        shapes = cmds.listRelatives(t, shapes=True, fullPath=True) or []
        if not shapes:
            continue
        shape = shapes[0]

        # all UV sets on this mesh
        uv_sets = cmds.polyUVSet(shape, query=True, allUVSets=True) or []

        # Tier 1: no UV sets at all -> can't be textured
        if not uv_sets:
            failures.append({
                "object": t,
                "name": name,
                "reason": "no UV sets — mesh can't be textured",
            })
            continue

        # Query the actual UV coordinates ONCE. polyEditUV (query) returns a
        # flat list [u0, v0, u1, v1, ...]. This is the reliable way to know
        # whether UVs exist AND where they are — polyEvaluate(uvComponent=True)
        # is inconsistent across Maya versions and falsely reports empty.
        uv_indices = "{}.map[*]".format(shape)
        coords = cmds.polyEditUV(uv_indices, query=True) or []

        # Tier 2: set exists but holds no actual UVs -> never unwrapped
        if not coords:
            failures.append({
                "object": t,
                "name": name,
                "reason": "UV set exists but contains no UVs — needs unwrapping",
            })
            continue

        # Tier 3: are the UVs roughly inside the 0-1 space?
        us = coords[0::2]   # even indices = U
        vs = coords[1::2]   # odd indices  = V
        margin = 0.001       # tiny tolerance so values right at 0 or 1 pass
        out_of_bounds = (
            min(us) < -margin or max(us) > 1.0 + margin or
            min(vs) < -margin or max(vs) > 1.0 + margin
        )
        if out_of_bounds:
            failures.append({
                "object": t,
                "name": name,
                "reason": "UVs extend outside 0-1 space — check the unwrap layout",
            })

    return failures

# ===========================================================================
#  run_all  +  PySide6 UI   (Maya 2026 / Qt6)
#  Append this to the bottom of validator.py
# ===========================================================================

def run_all(transforms=None):
    """
    Run every check and return one merged list of failures.
    Each failure gets a 'check' field so the UI knows which check raised it.

    We resolve the mesh list ONCE and hand it to every check, so a big scene
    isn't scanned five separate times.

    Returns: list of {"check", "object", "name", "reason"}.
    """
    if transforms is None:
        transforms = _all_mesh_transforms()

    checks = {
        "naming":     check_naming,
        "transforms": check_transforms,
        "history":    check_history,
        "pivot":      check_pivot,
        "uvs":        check_uvs,
    }

    all_failures = []
    for check_name, fn in checks.items():
        for f in fn(transforms):
            f["check"] = check_name      # tag it
            all_failures.append(f)

    return all_failures


# ---------------------------------------------------------------------------
#  UI
# ---------------------------------------------------------------------------
from PySide6 import QtWidgets, QtCore, QtGui   # Maya 2026 = PySide6 (Qt6)
from shiboken6 import wrapInstance             # PySide6's companion (was shiboken2)
import maya.OpenMayaUI as omui


def _maya_main_window():
    """Get Maya's main window as a Qt object so our tool docks to it correctly."""
    ptr = omui.MQtUtil.mainWindow()
    return wrapInstance(int(ptr), QtWidgets.QWidget)


# colour per check type, so the report is scannable at a glance
_CHECK_COLOR = {
    "naming":     "#e0a132",
    "transforms": "#6aa9e0",
    "history":    "#c77dff",
    "pivot":      "#82c77d",
    "uvs":        "#e0635c",
}


class ValidatorUI(QtWidgets.QDialog):

    def __init__(self, parent=None):
        super().__init__(parent or _maya_main_window())
        self.setWindowTitle("Export Validator")
        self.setMinimumWidth(520)
        self.setMinimumHeight(420)
        self._build()

    def _build(self):
        layout = QtWidgets.QVBoxLayout(self)
        layout.setSpacing(8)

        # --- top bar: run button + summary label ---
        top = QtWidgets.QHBoxLayout()
        self.run_btn = QtWidgets.QPushButton("Validate Scene")
        self.run_btn.setMinimumHeight(34)
        self.run_btn.clicked.connect(self._on_run)
        top.addWidget(self.run_btn)

        self.summary = QtWidgets.QLabel("Ready.")
        self.summary.setStyleSheet("font-weight: bold;")
        top.addWidget(self.summary, stretch=1)
        layout.addLayout(top)

        # --- results table ---
        self.table = QtWidgets.QTableWidget(0, 3)
        self.table.setHorizontalHeaderLabels(["Check", "Object", "Problem"])
        self.table.horizontalHeader().setSectionResizeMode(
            2, QtWidgets.QHeaderView.Stretch)
        self.table.horizontalHeader().setSectionResizeMode(
            0, QtWidgets.QHeaderView.ResizeToContents)
        self.table.setEditTriggers(QtWidgets.QAbstractItemView.NoEditTriggers)
        self.table.setSelectionBehavior(QtWidgets.QAbstractItemView.SelectRows)
        # clicking a row selects that object in Maya
        self.table.itemSelectionChanged.connect(self._on_row_selected)
        layout.addWidget(self.table)

        self._rows = []   # parallel list of object long-names, row-aligned

    def _on_run(self):
        failures = run_all()
        self.table.setRowCount(0)
        self._rows = []

        if not failures:
            self.summary.setText("✓  All checks passed — scene is export-ready.")
            self.summary.setStyleSheet("font-weight: bold; color: #82c77d;")
            return

        self.summary.setText(f"✗  {len(failures)} issue(s) found.")
        self.summary.setStyleSheet("font-weight: bold; color: #e0635c;")

        for f in failures:
            row = self.table.rowCount()
            self.table.insertRow(row)

            check_item = QtWidgets.QTableWidgetItem(f["check"])
            color = _CHECK_COLOR.get(f["check"], "#cccccc")
            check_item.setForeground(QtGui.QColor(color))

            self.table.setItem(row, 0, check_item)
            self.table.setItem(row, 1, QtWidgets.QTableWidgetItem(f["name"]))
            self.table.setItem(row, 2, QtWidgets.QTableWidgetItem(f["reason"]))
            self._rows.append(f["object"])

    def _on_row_selected(self):
        rows = self.table.selectionModel().selectedRows()
        if not rows:
            return
        idx = rows[0].row()
        obj = self._rows[idx]
        if cmds.objExists(obj):
            cmds.select(obj, replace=True)   # jump-to-object in Maya


# keep a module-level reference so Python's garbage collector doesn't
# close the window the instant the function returns
_validator_window = None

def show():
    """Call this from the script editor:  import validator; validator.show()"""
    global _validator_window
    try:
        _validator_window.close()
    except Exception:
        pass
    _validator_window = ValidatorUI()
    _validator_window.show()