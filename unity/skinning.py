"""Bind-pose baking for skinned meshes.

A ``SkinnedMeshRenderer``'s vertices are stored in the mesh's own local space
and only reach their visible positions once the skin transform is applied. This
module reconstructs the pose the renderer actually displays at rest, in world
space, which is both what a Blender armature's bones are placed at and the only
correct pose to texture on in Painter -- so both hosts want exactly this, and
neither should own it.

Everything here stays in UNITY space; converting to the host's coordinate system
is a later, separate step (``math3d.coordinate``).
"""

from __future__ import annotations

import numpy as np


def bake_bind_pose(decoded, smr_bones, file_id_to_world):
    """Transform mesh-local vertices to their bind-pose world positions.

        bind_world(v) = sum_j w_j * (boneWorld_j @ bindpose_j) @ v_local

    ``decoded`` is a mesh_decoder.Mesh, mutated in place; ``smr_bones`` is the
    renderer's ``m_Bones`` list of ``{fileID}`` refs (bone slot order); and
    ``file_id_to_world`` maps a transform fileID to its Unity-space world 4x4
    (hierarchy.world_matrices). Returns True when the bake actually ran -- False
    means the mesh carries no skin data, and the caller must place it by its
    node transform instead.
    """
    if (decoded.bind_poses is None or decoded.bone_weights is None
            or decoded.bone_indices is None or not smr_bones):
        return False
    n = len(decoded.positions)
    n_bones = len(smr_bones)
    world = np.tile(np.eye(4, dtype=np.float64), (n_bones, 1, 1))
    for slot, ref in enumerate(smr_bones):
        fid = ref.get("fileID") if isinstance(ref, dict) else None
        wmat = file_id_to_world.get(fid)
        if wmat is not None:
            world[slot] = wmat
    bind = decoded.bind_poses.astype(np.float64)
    count = min(n_bones, bind.shape[0])
    skin = np.tile(np.eye(4, dtype=np.float64), (n_bones, 1, 1))
    skin[:count] = world[:count] @ bind[:count]

    idx = np.clip(decoded.bone_indices, 0, n_bones - 1)
    weights = decoded.bone_weights
    vh = np.concatenate([decoded.positions.astype(np.float64),
                         np.ones((n, 1))], axis=1)
    baked = np.zeros((n, 3), dtype=np.float64)
    for j in range(idx.shape[1]):
        mats = skin[idx[:, j]]
        transformed = np.einsum("nij,nj->ni", mats, vh)[:, :3]
        baked += weights[:, j, None] * transformed
    decoded.positions = baked.astype(np.float32)

    # Normals (and tangents) transform by the INVERSE TRANSPOSE of the linear
    # part, not by the linear part itself. They coincide for a rigid bone, but
    # a bone carrying non-uniform scale (Unity rigs do use them -- squash bones,
    # mirrored limbs with a negative axis) would otherwise shear the normals
    # away from the surface and light the mesh wrongly.
    linear = skin[:, :3, :3]
    normal_mats = linear.copy()
    invertible = np.abs(np.linalg.det(linear)) > 1e-12
    if invertible.any():
        normal_mats[invertible] = np.linalg.inv(linear[invertible]).transpose(0, 2, 1)

    def _blend(vectors, mats_by_bone):
        out = np.zeros((n, 3), dtype=np.float64)
        for j in range(idx.shape[1]):
            out += weights[:, j, None] * np.einsum(
                "nij,nj->ni", mats_by_bone[idx[:, j]], vectors)
        lengths = np.linalg.norm(out, axis=1, keepdims=True)
        lengths[lengths < 1e-6] = 1.0
        return (out / lengths).astype(np.float32)

    if decoded.normals is not None:
        decoded.normals = _blend(decoded.normals.astype(np.float64), normal_mats)
    if decoded.tangents is not None:
        # The tangent is a real surface direction (dP/du), so unlike the normal
        # it follows the linear part; its w handedness is unaffected by a
        # skin transform and is carried through untouched.
        baked_t = _blend(decoded.tangents[:, :3].astype(np.float64), linear)
        decoded.tangents = np.concatenate(
            [baked_t, decoded.tangents[:, 3:4].astype(np.float32)], axis=1)
    return True
