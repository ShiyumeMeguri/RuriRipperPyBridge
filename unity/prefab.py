"""Renderer discovery over a parsed Unity prefab.

"Which renderers does this prefab actually draw, with what mesh and what
materials" is the same question in every host -- only what gets built from the
answer differs (Blender objects, glTF primitives). Both importers had grown
their own copy of the rules, which is how they ended up disagreeing about
things like static batching and disabled GameObjects; this module is the single
statement of them.

The rules, and why each exists:

* **LODGroup** -- a renderer listed under LOD1+ (and never under LOD0) is a
  lower-detail duplicate of geometry already covered by LOD0. Discarding it is
  keyed on the LODGroup's own lists, not on a name suffix.
* **ShadowsOnly renderers** -- ``m_CastShadows == 3`` means the renderer is
  invisible to the camera and exists purely to cast a shadow (a shadow proxy);
  importing it drops an invisible duplicate on top of the real mesh.
* **Disabled renderers / inactive GameObjects** -- these draw nothing in Unity
  right now, but are routinely runtime-toggled variants, so the default is to
  import them and SAY so (counted separately) rather than silently lose
  geometry. ``import_inactive=False`` matches exactly what the game draws.
* **Static batching** -- Unity merges several objects into one shared mesh and
  gives each renderer a window (``m_StaticBatchInfo``) into it. Drawing the
  whole mesh for such a renderer pulls in the other objects' geometry AND
  shifts every material slot, so the window travels with the renderer.

Iteration order is SkinnedMeshRenderers first, then MeshRenderers, matching
both hosts' existing output ordering.
"""

from __future__ import annotations

from . import mesh_decoder

# Unity ShadowCastingMode.ShadowsOnly.
SHADOWS_ONLY = 3

DEFAULT_FILTERS = {
    "lod0_only": True,
    "import_shadow_proxies": False,
    "import_inactive": True,
}


class SkipStats:
    """Why renderers did not make it through, for the host's own report."""

    __slots__ = ("lod", "shadow", "inactive", "inactive_included")

    def __init__(self):
        self.lod = 0                # discarded as LOD1+
        self.shadow = 0             # discarded as a ShadowsOnly proxy
        self.inactive = 0           # discarded as disabled/inactive
        self.inactive_included = 0  # disabled/inactive but imported anyway

    def __repr__(self):
        return ("<SkipStats lod={0} shadow={1} inactive={2} "
                "inactive_included={3}>".format(self.lod, self.shadow, self.inactive,
                                                self.inactive_included))


class RendererInfo:
    """One renderer that passed the filters, with everything a builder needs."""

    __slots__ = ("document", "kind", "file_id", "go_id", "node", "name",
                 "mesh_ref", "material_refs", "bones", "submesh_range", "disabled")

    def __init__(self, document, kind, go_id, node, name, mesh_ref,
                 material_refs, bones, submesh_range, disabled):
        self.document = document          # the UnityDocument of the renderer
        self.kind = kind                  # "skinned" | "static"
        self.file_id = document.file_id
        self.go_id = go_id
        self.node = node                  # hierarchy.Node | None
        self.name = name                  # owning GameObject's m_Name
        self.mesh_ref = mesh_ref          # {fileID, guid} of the Mesh
        self.material_refs = material_refs
        self.bones = bones                # m_Bones (skinned only), else []
        self.submesh_range = submesh_range  # (first, count) | None -- static batching
        self.disabled = disabled          # drew nothing in Unity, imported anyway

    @property
    def is_skinned(self):
        return self.kind == "skinned"

    def __repr__(self):
        return "<RendererInfo {0} {1} &{2}>".format(self.kind, self.name, self.file_id)


def lod_discard_set(prefab):
    """Renderer fileIDs that belong to LOD1+ only, i.e. the ones to discard.

    A renderer listed under BOTH LOD0 and a lower level stays (the subtraction
    at the end) -- LODGroups do share renderers across levels."""
    keep = set()
    discard = set()
    for group in prefab.all("LODGroup"):
        lods = group.data.get("m_LODs") or []
        for level, lod in enumerate(lods):
            for ref in (lod.get("renderers") or []):
                renderer = ref.get("renderer") if isinstance(ref, dict) else None
                fid = renderer.get("fileID") if isinstance(renderer, dict) else None
                if fid is None:
                    continue
                if level == 0:
                    keep.add(fid)
                else:
                    discard.add(fid)
    return discard - keep


def go_name(prefab, go_id):
    """The owning GameObject's m_Name, or "Object" when it isn't in the file."""
    go = prefab.get(go_id)
    return str(go.data.get("m_Name", "Object")) if go else "Object"


def mesh_filter_ref(prefab, node):
    """The ``m_Mesh`` reference of the MeshFilter sitting on a node's own
    GameObject -- where a MeshRenderer's geometry actually lives."""
    if node is None:
        return None
    for comp_id in node.components:
        comp = prefab.get(comp_id)
        if comp is not None and comp.class_name == "MeshFilter":
            return comp.data.get("m_Mesh")
    return None


def static_batch_range(renderer):
    """(first_submesh, submesh_count) when this renderer is a window into a
    static-batched shared mesh, else None."""
    info = renderer.data.get("m_StaticBatchInfo") or {}
    count = int(info.get("subMeshCount") or 0)
    if count <= 0:
        return None
    return int(info.get("firstSubMesh") or 0), count


def resolve_filters(options):
    merged = dict(DEFAULT_FILTERS)
    if options:
        for key in DEFAULT_FILTERS:
            if key in options:
                merged[key] = options[key]
    return merged


def iter_renderers(prefab, go_to_node, options=None, stats=None):
    """Yield a RendererInfo per renderer that should contribute geometry.

    ``go_to_node`` maps GameObject fileID -> hierarchy.Node (``{n.go_id: n for
    n in nodes.values()}``). ``stats`` counts what was filtered out; pass one in
    to read it afterwards, or let it be created and dropped."""
    filters = resolve_filters(options)
    stats = stats if stats is not None else SkipStats()
    discard = lod_discard_set(prefab) if filters["lod0_only"] else set()

    def accept(renderer, node):
        if renderer.file_id in discard:
            stats.lod += 1
            return None
        if (not filters["import_shadow_proxies"]
                and renderer.data.get("m_CastShadows") == SHADOWS_ONLY):
            stats.shadow += 1
            return None
        disabled = (renderer.data.get("m_Enabled") == 0
                    or (node is not None and not node.active))
        if disabled:
            if filters["import_inactive"]:
                stats.inactive_included += 1
            else:
                stats.inactive += 1
                return None
        return disabled

    for smr in prefab.all("SkinnedMeshRenderer"):
        go_id = (smr.data.get("m_GameObject") or {}).get("fileID")
        node = go_to_node.get(go_id)
        disabled = accept(smr, node)
        if disabled is None:
            continue
        yield RendererInfo(
            document=smr, kind="skinned", go_id=go_id, node=node,
            name=go_name(prefab, go_id), mesh_ref=smr.data.get("m_Mesh"),
            material_refs=smr.data.get("m_Materials") or [],
            bones=smr.data.get("m_Bones") or [],
            submesh_range=static_batch_range(smr), disabled=disabled)

    for mr in prefab.all("MeshRenderer"):
        go_id = (mr.data.get("m_GameObject") or {}).get("fileID")
        node = go_to_node.get(go_id)
        disabled = accept(mr, node)
        if disabled is None:
            continue
        if node is None:
            # No Transform means nowhere to place it; a MeshRenderer's geometry
            # is only reachable through its GameObject's MeshFilter anyway.
            continue
        yield RendererInfo(
            document=mr, kind="static", go_id=go_id, node=node,
            name=go_name(prefab, go_id), mesh_ref=mesh_filter_ref(prefab, node),
            material_refs=mr.data.get("m_Materials") or [], bones=[],
            submesh_range=static_batch_range(mr), disabled=disabled)


# ---------------------------------------------------------------------------
# Mesh resolution
# ---------------------------------------------------------------------------
class MeshLoad:
    """The outcome of following a renderer's mesh reference.

    Carries FACTS, not sentences: a host words its own warnings, but neither
    has to re-derive why a mesh came back empty (see mesh_decoder.diagnose_empty
    for that one, which is genuinely about Unity's storage formats)."""

    __slots__ = ("decoded", "document", "problem", "detail", "name",
                 "dropped_topologies")

    def __init__(self, decoded=None, document=None, problem=None, detail="",
                 name="", dropped_topologies=()):
        self.decoded = decoded
        self.document = document
        self.problem = problem      # None | no_ref | not_found | no_mesh_document | empty
        self.detail = detail        # guid, or the empty-mesh diagnosis
        self.name = name            # the Mesh asset's own m_Name
        self.dropped_topologies = tuple(dropped_topologies)

    @property
    def ok(self):
        return self.decoded is not None


def load_mesh(db, mesh_ref, fallback_name=""):
    """Resolve + decode a ``{fileID, guid}`` mesh reference through any of the
    asset databases (disk or bridge closure)."""
    if not isinstance(mesh_ref, dict) or not mesh_ref.get("guid"):
        return MeshLoad(problem="no_ref", name=fallback_name)
    guid = mesh_ref["guid"]

    # Bridge fast path first: the exporter already handed this mesh's serialized
    # buffers across raw (see mesh_decoder.decode_mesh_blob) -- no YAML text at
    # all. A blob only exists when the geometry is plainly in hand, so the empty
    # case here is genuinely "zero vertices"; compressed/stream-missing meshes
    # never get a blob and fall through to the YAML document + its diagnosis.
    blob = db.mesh_blob(guid) if hasattr(db, "mesh_blob") else None
    if blob is not None:
        decoded = mesh_decoder.decode_mesh_blob(blob[0], blob[1])
        name = decoded.name or fallback_name
        if decoded.positions is None or len(decoded.positions) == 0:
            return MeshLoad(problem="empty", name=name,
                            detail="'{0}' decoded to zero vertices.".format(name))
        dropped = sorted({sm.topology for sm in decoded.submeshes} - {0})
        return MeshLoad(decoded=decoded, name=name, dropped_topologies=dropped)

    mesh_file = db.load_guid(guid)
    if mesh_file is None:
        return MeshLoad(problem="not_found", detail=str(guid), name=fallback_name)
    mesh_doc = mesh_file.first("Mesh")
    if mesh_doc is None:
        return MeshLoad(problem="no_mesh_document", detail=str(guid), name=fallback_name)

    decoded = mesh_decoder.decode_mesh(mesh_doc)
    name = str(mesh_doc.data.get("m_Name", fallback_name))
    if decoded.positions is None or len(decoded.positions) == 0:
        return MeshLoad(document=mesh_doc, problem="empty", name=name,
                        detail=mesh_decoder.diagnose_empty(mesh_doc, name))
    # Partial losses, where SOMETHING still comes through: surface them rather
    # than letting the difference show up later as a shading or seam mystery.
    dropped = sorted({int(sm.get("topology") or 0)
                      for sm in (mesh_doc.data.get("m_SubMeshes") or [])} - {0})
    return MeshLoad(decoded=decoded, document=mesh_doc, name=name,
                    dropped_topologies=dropped)
