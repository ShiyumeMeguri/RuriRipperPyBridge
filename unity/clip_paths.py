"""Re-anchoring an AnimationClip's curve bindings onto a real skeleton.

Two independent problems, one join key:

* **Hashed paths.** When the rig wasn't in the export scope, AssetRipper cannot
  restore a curve's binding to a transform-path string and writes the raw Unity
  binding hash instead: ``path_0x<CRC32>_<random>``. The hash is CRC32 of the
  UTF-8 path -- verified empirically against the real game
  (``crc32(b"Root") == 0xB6C65665``, matching its ``path_0xB6C65665_WvpMuNH``
  placeholder exactly, across every probe).
* **Different nesting depth.** Unity hashes the path RELATIVE to the Animator's
  own node, while an imported armature records paths from the prefab ROOT, and
  model variants nest the rig at different depths (confirmed against the real
  game: a uimodel's clip paths start ``Root/Bip001/...`` while a postmodel
  armature stamps ``chr_0013_aglina_postmodel/.../Root/Bip001/...``). Whole-path
  equality -- and whole-path CRC -- therefore never matches across variants.

Both are solved by hashing every SUFFIX of every armature path, so the join is
prefix-agnostic while remaining exact path-structure identity: the same segments
Unity itself hashed, just re-anchored. This is not display-name guessing.

``clip_data`` throughout is either a parsed AnimationClip document's ``.data``
dict or a ``clip_curves.ClipCurves`` (the zero-parse bridge payload); both are
accepted everywhere.
"""

from __future__ import annotations

import re
import zlib

from . import clip_curves, humanoid_retarget

# AssetRipper's placeholder for a binding it could not restore to a string.
_HASHED_PATH_RE = re.compile(r"^path_0x([0-9A-Fa-f]{1,8})_")

_CURVE_LIST_FIELDS = ("m_RotationCurves", "m_PositionCurves", "m_ScaleCurves",
                      "m_EulerCurves", "m_FloatCurves")
_TRANSFORM_CURVE_FIELDS = ("m_RotationCurves", "m_PositionCurves",
                           "m_ScaleCurves", "m_EulerCurves")


def build_suffix_crc_table(path_to_bone):
    """{CRC32 -> full stamped path} over EVERY level-suffix of every bone path.

    Enumerating each path's suffixes ("a/b/c", "b/c", "c") makes the join
    prefix-agnostic: the clip's Animator-relative path IS one of the suffixes
    whenever the bone genuinely exists under the selected skeleton. On a
    suffix-CRC collision the LONGEST suffix wins -- a leaf-only match can be
    ambiguous, a long chain can't."""
    table = {}
    for path in path_to_bone:
        parts = path.split("/")
        for i in range(len(parts)):
            suffix = "/".join(parts[i:])
            crc = zlib.crc32(suffix.encode("utf-8")) & 0xFFFFFFFF
            prev = table.get(crc)
            if prev is None or len(suffix) > prev[0]:
                table[crc] = (len(suffix), path)
    return {crc: path for crc, (_length, path) in table.items()}


def entry_crc(path):
    """The binding CRC32 a curve entry's path stands for: a hashed placeholder
    carries it literally, a restored string path hashes to it -- one join key
    for both forms."""
    hash_match = _HASHED_PATH_RE.match(path)
    if hash_match:
        return int(hash_match.group(1), 16)
    return zlib.crc32(path.encode("utf-8")) & 0xFFFFFFFF


def repair_hashed_clip_paths(clip_data, path_to_bone):
    """Rewrite a clip's curve paths (in place) to the target skeleton's own full
    paths, joining through the suffix-CRC table.

    Hashed placeholders resolve by their literal CRC32; already-restored string
    paths that don't literally appear in ``path_to_bone`` (rig nested at a
    different depth in this variant) resolve by hashing. Paths that already
    match verbatim are left untouched. Returns (repaired, unmatched)."""
    table = build_suffix_crc_table(path_to_bone)
    repaired = 0
    unmatched = 0
    if isinstance(clip_data, clip_curves.ClipCurves):
        for channels in clip_data.all_channel_lists():
            for channel in channels:
                path = channel.path or ""
                if not path or path in path_to_bone:
                    continue
                real = table.get(entry_crc(path))
                if real is None:
                    unmatched += 1
                    continue
                channel.path = real
                repaired += 1
        return repaired, unmatched
    for field in _CURVE_LIST_FIELDS:
        for entry in clip_data.get(field) or []:
            # "path:" with no value parses to an EXISTING key holding None
            # (root-level curves) -- `or ""` covers both missing and null, a
            # .get default only the former.
            path = entry.get("path") or ""
            if not path or path in path_to_bone:
                continue
            real = table.get(entry_crc(path))
            if real is None:
                unmatched += 1
                continue
            entry["path"] = real
            repaired += 1
    return repaired, unmatched


def clip_path_match_ratio(clip_data, path_to_bone):
    """Fraction of a clip's transform-curve paths that resolve to a bone of the
    target skeleton -- the compatibility check for importing a clip onto a
    skeleton the user picked. Same suffix-CRC join as the repair, so a clip
    anchored anywhere inside the hierarchy counts as matching. Returns
    (ratio, total); (0.0, 0) for a clip with no transform curves at all (e.g. a
    pure blendshape clip)."""
    table = build_suffix_crc_table(path_to_bone)
    total = 0
    matched = 0
    if isinstance(clip_data, clip_curves.ClipCurves):
        for channels in clip_data.transform_channel_lists():
            for channel in channels:
                path = channel.path or ""
                if not path:
                    continue
                total += 1
                if path in path_to_bone or entry_crc(path) in table:
                    matched += 1
        return (matched / total if total else 0.0), total
    for field in _TRANSFORM_CURVE_FIELDS:
        for entry in clip_data.get(field) or []:
            path = entry.get("path") or ""  # null-valued for root-level curves
            if not path:
                continue
            total += 1
            if path in path_to_bone or entry_crc(path) in table:
                matched += 1
    return (matched / total if total else 0.0), total


def clip_is_humanoid(clip_data):
    """Whether a clip drives a humanoid rig.

    Humanoid body motion ships as muscle/root FLOAT curves (attribute names like
    "Spine Front-Back" / "RootT.x"), not as transform curves -- the exact
    predicate a muscle bake gates on, exposed so a caller can KNOW (e.g. to warn
    that a humanoid clip was imported with no Avatar in scope, which silently
    drops the entire body's motion)."""
    if isinstance(clip_data, clip_curves.ClipCurves):
        return any(humanoid_retarget.is_muscle(ch.attribute)
                   or humanoid_retarget.is_root(ch.attribute)
                   for ch in clip_data.floats)
    for entry in clip_data.get("m_FloatCurves") or []:
        attribute = entry.get("attribute") or ""
        if humanoid_retarget.is_muscle(attribute) or humanoid_retarget.is_root(attribute):
            return True
    return False
