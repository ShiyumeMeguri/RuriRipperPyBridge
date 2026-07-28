"""Avatar asset parsing: the embedded skeleton, for building an armature from a standalone
Avatar asset.

This is DATA extraction, deliberately not solving: Unity's muscle solve lives on the C# side
(Ruri.RipperHook's HumanoidToGeneric pass), which rewrites every humanoid clip into ordinary
per-bone transform curves before anything reaches Python. The only thing a host still reads off
an Avatar is its own raw skeleton -- node hierarchy, rest pose, and TOS-resolved names -- which
is populated for Generic avatars too.
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


def skeleton_nodes(data):
    """The avatar's own embedded skeleton (Generic avatars included): one entry per raw node, in
    m_ParentId's index space -- ``(name, parent_index, (tx,ty,tz), (qw,qx,qy,qz))``. Names resolve
    through TOS path leaves, falling back to ``bone_{i}``; rest TRS from m_SkeletonPose."""
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
        name = None
        if index < len(skel_ids):
            path = tos.get(skel_ids[index])
            if path:
                name = path.rsplit("/", 1)[-1]
        if not name:
            name = f"bone_{index}"
        if index < len(rest):
            x = rest[index]
            t = (float(x["t"]["x"]), float(x["t"]["y"]), float(x["t"]["z"]))
            q = (float(x["q"]["w"]), float(x["q"]["x"]), float(x["q"]["y"]), float(x["q"]["z"]))
        else:
            t = (0.0, 0.0, 0.0)
            q = (1.0, 0.0, 0.0, 0.0)
        result.append((name, parent, t, q))
    return result
