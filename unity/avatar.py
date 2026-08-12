"""Avatar asset parsing: the embedded skeleton, for building an armature from a standalone
Avatar asset.

This is DATA extraction, deliberately not solving: Unity's muscle solve lives on the C# side
(Ruri.RipperHook's HumanoidClipGenericizer), asked for one clip at a time through
RipperBlenderBridge.SolveHumanoidClip, which answers with ordinary per-bone transform curves
and leaves the clip asset untouched. Nothing rewrites clips globally -- that is what keeps a
Unity-bound export's humanoid clips humanoid. The only thing a host still reads off an Avatar
is its own raw skeleton -- node hierarchy, rest pose, and TOS-resolved names -- which is
populated for Generic avatars too.
"""

from __future__ import annotations

_HEXDIGITS = set("0123456789abcdefABCDEF")


def _unwrap(value):
    """Peel Unity's OffsetPtr ``{data: ...}`` indirection."""
    while isinstance(value, dict) and len(value) == 1 and "data" in value:
        value = value["data"]
    return value


def _int_array(blob):
    """Parse a little-endian hex int32 array; tolerant of AssetRipper's trailing -1 padding."""
    if isinstance(blob, (list, tuple)):
        return [int(x) for x in blob]
    text = str(blob)
    out = []
    i = 0
    while i + 8 <= len(text):
        chunk = text[i:i + 8]
        if all(c in _HEXDIGITS for c in chunk):
            out.append(int.from_bytes(bytes.fromhex(chunk), "little", signed=True))
            i += 8
        else:
            break
    return out


def _parse_tos(data):
    """The avatar's ``m_TOS`` (CRC32 path hash -> transform path) as ``{int: str}``."""
    tos = data.get("m_TOS")
    result = {}
    if isinstance(tos, list):
        for pair in tos:
            if not isinstance(pair, dict):
                continue
            key = pair.get("first")
            value = pair.get("second")
            if value is None and isinstance(key, dict):  # {key: path} flow-map variant
                for k, v in key.items():
                    key, value = k, v
            try:
                result[int(key) & 0xFFFFFFFF] = str(value)
            except (TypeError, ValueError):
                continue
    elif isinstance(tos, dict):
        for key, value in tos.items():
            try:
                result[int(key) & 0xFFFFFFFF] = str(value)
            except (TypeError, ValueError):
                continue
    return result


def transform_paths(data):
    """Every transform path the avatar's ``m_TOS`` names -- the CRC32(path)->path
    table for the whole skeleton, which a shared-skeleton part mesh hashes its
    bones from. Returned as a plain list of full paths (Root/Bip001/...)."""
    return list(_parse_tos(data).values())


def _trs_matrix(np, x):
    """Local TRS entry {t, q, s} -> 4x4 (Unity space), scale before rotation."""
    if not isinstance(x, dict):
        return np.eye(4, dtype=np.float64)
    t = x.get("t") or {}
    q = x.get("q") or {}
    s = x.get("s") or {}
    w = float(q.get("w", 1.0)); qx = float(q.get("x", 0.0))
    qy = float(q.get("y", 0.0)); qz = float(q.get("z", 0.0))
    rot = np.array([
        [1 - 2 * (qy * qy + qz * qz), 2 * (qx * qy - qz * w), 2 * (qx * qz + qy * w)],
        [2 * (qx * qy + qz * w), 1 - 2 * (qx * qx + qz * qz), 2 * (qy * qz - qx * w)],
        [2 * (qx * qz - qy * w), 2 * (qy * qz + qx * w), 1 - 2 * (qx * qx + qy * qy)]],
        dtype=np.float64)
    scale = np.array([float(s.get("x", 1.0)), float(s.get("y", 1.0)), float(s.get("z", 1.0))])
    matrix = np.eye(4, dtype=np.float64)
    matrix[:3, :3] = rot * scale[None, :]
    matrix[0, 3] = float(t.get("x", 0.0))
    matrix[1, 3] = float(t.get("y", 0.0))
    matrix[2, 3] = float(t.get("z", 0.0))
    return matrix


def skeleton_world_rests(data):
    """``{crc32(path): Unity world rest 4x4}`` for the avatar's FULL skeleton.

    The real STANDING rest a shared-skeleton part mesh is bind-baked against
    lives in ``m_AvatarSkeleton`` (the whole transform tree, parent links +
    CRC32-of-path ids) posed by ``m_AvatarSkeletonPose`` (per-node local TRS) --
    NOT ``m_Human.m_Skeleton`` (the 24-bone humanoid subset, normalised to a
    different, near-origin frame). World matrices are numpy 4x4 in Unity space, so
    ``skinning.bake_bind_pose`` and ``coordinate.convert_matrix`` both consume them
    directly. Empty when the avatar carries no full skeleton pose."""
    import numpy as np

    constant = _unwrap(data.get("m_Avatar") or {})
    skeleton = _unwrap(constant.get("m_AvatarSkeleton") or {})
    nodes = skeleton.get("m_Node") or []
    ids = [v & 0xFFFFFFFF for v in _int_array(skeleton.get("m_ID") or [])]
    pose = _unwrap(constant.get("m_AvatarSkeletonPose") or {})
    locals_ = pose.get("m_X") or []
    if not nodes or not locals_:
        return {}

    world = [None] * len(nodes)

    def resolve(index):
        if world[index] is not None:
            return world[index]
        local = _trs_matrix(np, locals_[index] if index < len(locals_) else None)
        parent = int(nodes[index].get("m_ParentId", -1))
        world[index] = resolve(parent) @ local if 0 <= parent < len(nodes) else local
        return world[index]

    for index in range(len(nodes)):
        resolve(index)
    return {ids[index]: world[index] for index in range(len(nodes)) if index < len(ids)}


def skeleton_nodes(data):
    """The avatar's own embedded skeleton (Generic avatars included): one entry per raw node, in
    m_ParentId's index space -- ``(name, parent_index, (tx,ty,tz), (qw,qx,qy,qz), path)``. Names
    resolve through TOS path leaves, falling back to ``bone_{i}`` (path None when TOS misses the
    node); rest TRS from m_SkeletonPose."""
    constant = _unwrap(data["m_Avatar"])
    human = _unwrap(constant["m_Human"])
    skeleton = _unwrap(human["m_Skeleton"])
    nodes = skeleton["m_Node"]
    skel_ids = [v & 0xFFFFFFFF for v in _int_array(skeleton.get("m_ID") or [])]
    tos = _parse_tos(data)
    skeleton_pose = _unwrap(human.get("m_SkeletonPose") or {})
    rest = skeleton_pose.get("m_X") or []

    result = []
    for index in range(len(nodes)):
        parent = int(nodes[index].get("m_ParentId", -1))
        path = tos.get(skel_ids[index]) if index < len(skel_ids) else None
        name = path.rsplit("/", 1)[-1] if path else f"bone_{index}"
        if index < len(rest):
            x = rest[index]
            t = (float(x["t"]["x"]), float(x["t"]["y"]), float(x["t"]["z"]))
            q = (float(x["q"]["w"]), float(x["q"]["x"]), float(x["q"]["y"]), float(x["q"]["z"]))
        else:
            t = (0.0, 0.0, 0.0)
            q = (1.0, 0.0, 0.0, 0.0)
        result.append((name, parent, t, q, path))
    return result
