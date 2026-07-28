"""Bridge-mode AssetDatabase: resolves guids against an in-memory pythonnet
closure (RipperBlenderBridge.ImportCabs' single guid -> bytes Assets dict)
instead of scanning a directory of .meta files. Same duck-typed surface as
asset_db.AssetDatabase (load_guid, load_file, resolve_guid, resolve_ref), plus
texture_bytes/all_guids/clip_curves for the bridge-only callers -- every consumer
that sticks to the shared surface (hierarchy, mesh_decoder, prefab, material,
and each host's own mesh/material builders) works unchanged against either
database, which is what lets one importer serve both a folder of extracted
YAML and a live cabmap closure.

The closure hands over one dict of raw bytes and nothing else: the bridge does
not sort payloads by kind, because the caller asking for a guid already knows
whether it wants YAML or an image. Decoding therefore happens here, on demand,
and an exporter that starts writing TGA or EXR where it used to write PNG needs
no change on either side of the bridge.
"""

from __future__ import annotations

from . import clip_curves as clip_curves_module
from . import unity_yaml


class BridgeAssetDatabase:
    """Backed by GUID-keyed dicts already pulled into memory by the pythonnet
    bridge. Each guid is parsed at most once and memoized, same caching
    behaviour as the disk AssetDatabase's file cache."""

    def __init__(self, assets, bridge=None, clip_curve_blobs=None):
        # assets: dict[guid_lower] -> the asset's exported bytes, any format
        # clip_curve_blobs: dict[guid_lower] -> (meta_json, payload_bytes) --
        #   the bridge's zero-parse AnimationClip curve payloads (see
        #   RipperBridge.clip_curves_by_guid / ClipCurveBlob.cs)
        self._assets = assets
        self._bridge = bridge  # optional: fetch_guid() fallback on a closure miss
        self._file_cache = {}  # guid -> UnityFile
        self._text_cache = {}  # guid -> decoded text, or None when the bytes aren't text
        self._clip_curve_blobs = clip_curve_blobs or {}
        self._clip_curve_cache = {}  # guid -> ClipCurves

    def clip_curves(self, guid):
        """The zero-parse ClipCurves for an AnimationClip guid, or None when
        this closure carries no blob for it (older bridge, blob build failure,
        or simply not a clip) -- callers fall back to load_guid + YAML."""
        if not guid:
            return None
        guid = guid.lower()
        cached = self._clip_curve_cache.get(guid)
        if cached is not None:
            return cached
        blob = self._clip_curve_blobs.get(guid)
        if blob is None:
            return None
        clip = clip_curves_module.ClipCurves.from_blob(blob[0], blob[1])
        self._clip_curve_cache[guid] = clip
        return clip

    def _text(self, guid):
        """The asset's bytes as text, or None when they are not text at all (an
        image, a mesh blob, ...). Decoding here rather than in the bridge is what
        keeps the closure format-agnostic; the result is memoized either way, so a
        closure-wide scan pays one failed decode per binary asset, not one per look."""
        if guid in self._text_cache:
            return self._text_cache[guid]
        data = self._assets.get(guid)
        text = None
        if data is not None:
            try:
                text = data.decode("utf-8")
            except UnicodeDecodeError:
                text = None
        self._text_cache[guid] = text
        return text

    def load_guid(self, guid):
        if not guid:
            return None
        guid = guid.lower()
        cached = self._file_cache.get(guid)
        if cached is not None:
            return cached
        text = self._text(guid)
        if text is None and self._bridge is not None:
            text = self._bridge.fetch_guid(guid)
        if text is None:
            return None
        unity_file = unity_yaml.UnityFile(guid, unity_yaml.parse_text(text, guid))
        self._file_cache[guid] = unity_file
        return unity_file

    def load_file(self, key):
        """Signature parity with AssetDatabase.load_file(path) -- key is a guid here."""
        return self.load_guid(key)

    def resolve_guid(self, guid):
        """No filesystem path concept in bridge mode: the guid itself is the
        opaque lookup key a host's texture stage uses to fetch bytes (see
        texture_bytes). Returns None when the guid isn't in the closure at all."""
        if not guid:
            return None
        guid = guid.lower()
        return guid if guid in self._assets else None

    def resolve_ref(self, ref):
        """Resolve a {fileID, guid} reference to (UnityDocument, guid) or (None, None).
        Mirrors asset_db.AssetDatabase.resolve_ref exactly, minus disk."""
        if not isinstance(ref, dict):
            return None, None
        guid = ref.get("guid")
        file_id = ref.get("fileID")
        if not guid:
            return None, None
        unity_file = self.load_guid(guid)
        if not unity_file:
            return None, None
        doc = unity_file.get(file_id) if file_id is not None else None
        if doc is None and unity_file.documents:
            doc = unity_file.documents[0]
        return doc, unity_file.path

    def texture_bytes(self, key):
        """The bridge-mode image source: the asset's exported bytes for a guid,
        in whatever container AssetRipper chose for it (png/tga/exr/...). Presence
        of this method (vs. the disk AssetDatabase, which lacks it) is what a
        host's texture stage branches on to pick the in-memory decode path."""
        if not key:
            return None
        return self._assets.get(key.lower())

    def all_guids(self):
        """Every guid present in the closure -- for closure-wide scans (e.g.
        loose-AnimationClip gathering, the bridge equivalent of the disk
        importer's folder walk). Non-text assets are in here too; a scan that
        sniffs raw_text simply gets None for them."""
        return self._assets.keys()

    def raw_text(self, guid):
        """Unparsed YAML text for a guid, without going through load_guid's
        unity_yaml.parse_text (and its memoizing cache). Lets a caller peek
        at a document (e.g. the cheap class/name sniff animation-clip discovery
        does) without paying a full parse for every candidate -- some
        AnimationClip documents in a character's closure run to 100+MB, and
        most closure guids aren't clips at all. None for a non-text asset."""
        if not guid:
            return None
        return self._text(guid.lower())
