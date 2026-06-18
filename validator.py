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
    Check UV health for export + Unreal lightmaps:
      1. Mesh must have at least one UV set (or it can't be textured).
      2. Static meshes should have a 2nd UV channel for lightmaps in UE.

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

        # list all UV sets on this mesh
        uv_sets = cmds.polyUVSet(shape, query=True, allUVSets=True) or []

        # Rule 1: no UV sets at all -> can't be textured
        if not uv_sets:
            failures.append({
                "object": t,
                "name": name,
                "reason": "no UV sets — mesh can't be textured",
            })
            continue  # nothing more to check if there are zero UVs

        # Rule 2: does the FIRST UV set actually contain UVs? (empty set = nothing)
        uv_count = cmds.polyEvaluate(shape, uvComponent=True)
        if not uv_count:
            failures.append({
                "object": t,
                "name": name,
                "reason": "UV set exists but contains no UVs — needs unwrapping",
            })
            continue

        # Rule 3: only one UV set -> missing the lightmap channel UE wants
        if len(uv_sets) < 2:
            failures.append({
                "object": t,
                "name": name,
                "reason": "only 1 UV channel — add UV1 for Unreal lightmaps",
            })

    return failures

def run_all(): ...