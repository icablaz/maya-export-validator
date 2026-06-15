def check_naming(obj):      # SM_ / T_ / M_ convention


"""
validator.py — Maya -> Unreal export validator
Day 2: check_naming
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




def check_transforms(obj): ...  # frozen?
def check_history(obj): ...     # construction history deleted?
def check_pivot(obj): ...       # pivot at expected location?
def check_uvs(obj): ...         # UV set present?
def run_all(): ...