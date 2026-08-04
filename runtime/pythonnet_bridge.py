"""In-process pythonnet bridge into Ruri.RipperHook.dll: boots a CoreCLR runtime
inside the host application's own process (``bootstrap`` having already
installed pythonnet) and exposes thin Python wrappers over
Ruri.RipperHook.Bridge.RipperBlenderBridge. Everything crossing the CLR/Python
boundary out of this module is plain data (str/bytes/dict/list) -- nothing above
it ever touches `clr`/.NET objects directly, which is what keeps the bridge one
implementation rather than one per host.

The bin dir -- the folder that has to contain BOTH Ruri.RipperHook.dll and
Ruri.RipperHook.CLI.runtimeconfig.json, typically
``<checkout>/AssetRipper/Source/0Bins/AssetRipper/Release`` -- is the one genuinely
machine-specific value here. No repo-root-relative derivation, no
configuration guessing, nothing hardcoded: it is resolved, in order, from

1. ``set_bin_dir(path)`` -- what the host pushed in (Blender: its
   AddonPreferences; Painter: its settings file), which is the normal case;
2. ``set_bin_dir_provider(fn)`` -- a callable, for a host whose preference can
   change under it and would rather be asked than have to remember to push;
3. ``$RURI_RIPPERHOOK_BIN`` -- the headless/CLI escape hatch, no UI needed.
"""

from __future__ import annotations

import os
import sys

# A claimed CoreCLR runtime can never be re-claimed in this process, so reloading
# this module would only desync the guards below from what is really loaded.
HOLDS_PROCESS_STATE = True

_runtime_set = False
_bridge_type = None
_bin_dir_override = None
_bin_dir_provider = None
_bin_dir_hint = ("Set it in the RuriRipper panel, or set the RURI_RIPPERHOOK_BIN "
                 "environment variable.")


def set_bin_dir(path):
    """Push the user-configured bin dir in. Takes priority over the provider and
    over RURI_RIPPERHOOK_BIN; an empty value clears it again."""
    global _bin_dir_override
    _bin_dir_override = (path or "").strip() or None


def set_bin_dir_provider(provider):
    """Register a callable returning the configured bin dir (or None), for a
    host that would rather be asked than push on every change."""
    global _bin_dir_provider
    _bin_dir_provider = provider


def set_bin_dir_hint(hint):
    """One sentence telling THIS host's user where that path is configured --
    it goes into the error raised when none is set."""
    global _bin_dir_hint
    _bin_dir_hint = hint


def _configured_bin_dir():
    if _bin_dir_override:
        return _bin_dir_override
    if _bin_dir_provider is not None:
        try:
            provided = (_bin_dir_provider() or "").strip()
        except Exception:
            provided = ""
        if provided:
            return provided
    return (os.environ.get("RURI_RIPPERHOOK_BIN") or "").strip() or None


def _dll_dir():
    d = _configured_bin_dir()
    if not d:
        raise RuntimeError(
            "No Ruri-RipperHook bin dir configured (the folder containing "
            "Ruri.RipperHook.dll, e.g. AssetRipper/Source/0Bins/AssetRipper/Release). "
            + _bin_dir_hint)
    if not os.path.isfile(os.path.join(d, "Ruri.RipperHook.dll")):
        raise RuntimeError(f"Ruri.RipperHook.dll not found in configured bin dir: {d}")
    if not os.path.isfile(os.path.join(d, "Ruri.RipperHook.CLI.runtimeconfig.json")):
        raise RuntimeError(
            f"Ruri.RipperHook.CLI.runtimeconfig.json not found next to the DLL in: {d} -- "
            "build Source/Ruri.RipperHook.CLI/Ruri.RipperHook.CLI.csproj (a Release build that "
            "only ran the GUI/core projects has the DLL but not this file).")
    return d


def _runtime_config_path():
    dll_dir = _dll_dir()
    # Reuse the CLI's own runtimeconfig.json (Microsoft.NETCore.App +
    # Microsoft.AspNetCore.App only -- confirmed the core DLL needs no
    # Microsoft.WindowsDesktop.App, that's GUI-only) rather than authoring a
    # new one; it's built right next to the DLL already.
    return dll_dir, os.path.join(dll_dir, "Ruri.RipperHook.CLI.runtimeconfig.json")


def _bound_runtime_kind():
    """Best-effort introspection of whichever runtime pythonnet already has
    set (pythonnet._RUNTIME is not public API, so this degrades to None --
    "unknown" -- rather than raising if a future pythonnet version removes
    or renames it)."""
    try:
        import pythonnet
        bound = getattr(pythonnet, "_RUNTIME", None)
    except ImportError:
        return None, None
    if bound is None:
        return None, None
    return bound, f"{type(bound).__module__}.{type(bound).__qualname__}"


def _claim_coreclr(runtime_config):
    """The one and only set_runtime() call site. pythonnet allows exactly one
    CLR runtime per process, ever -- if ANYTHING else in this host session
    (a Blender profile can have dozens of add-ons, a Painter install several
    plugins; a lazily-triggered `import clr` in any of them defaults to .NET
    Framework on Windows) claims a runtime before we do, our net10.0 DLL can
    never load under it. "Already loaded" is only safe to swallow when what's
    already bound is a CoreCLR-family runtime (our own earlier claim -- e.g. a
    second register() in this process after Blender's Reload Scripts, which
    resets this module's own globals via importlib.reload but can't un-claim
    the real process-wide runtime -- or anything else CoreCLR-compatible); if
    it's .NET Framework, swallowing the error here would just defer the real
    failure to a much more confusing spot later (clr.AddReference silently not
    registering the assembly's namespaces, surfacing as "No module named
    'Ruri'" at the unrelated from-import line) -- fail loudly and specifically
    right here instead."""
    global _runtime_set
    if _runtime_set:
        return
    from clr_loader import get_coreclr
    from pythonnet import set_runtime
    try:
        set_runtime(get_coreclr(runtime_config=runtime_config))
    except RuntimeError as exc:
        if "already been loaded" not in str(exc):
            raise
        bound, bound_kind = _bound_runtime_kind()
        if bound_kind and "netfx" in bound_kind.lower():
            raise RuntimeError(
                "A .NET Framework runtime is already loaded in this host process "
                f"({bound_kind}, {bound!r}) -- pythonnet allows only one CLR runtime per "
                "process, and .NET Framework cannot load Ruri.RipperHook.dll (targets "
                "net10.0). Something imported `clr` (or called pythonnet.load()) before "
                "claim_runtime_early() got the chance to claim CoreCLR. Restart the "
                "application with this plugin loading first; if it keeps happening, "
                "another plugin/add-on in this profile is the culprit and needs to be "
                "identified."
            ) from exc
        # Bound to something else CoreCLR-compatible (most likely: our own
        # earlier claim_runtime_early() in this same process) -- fine.
    _runtime_set = True


def claim_runtime_early():
    """Call at plugin/add-on start (not lazily on first bridge use) to win the
    single-runtime-per-process race as early as structurally possible --
    before the user has clicked anything that might trigger some other
    plugin's own lazy pythonnet/CLR usage. Best-effort/silent: if pythonnet
    isn't installed yet or the DLL isn't built yet, this is a no-op and
    _ensure_runtime() will do the real work (and raise a real error if
    appropriate) on first actual bridge use instead. bootstrap's on_ready hook
    covers the remaining window: the case where pythonnet only becomes
    importable partway through the session, after this call already no-opped."""
    try:
        _, runtime_config = _runtime_config_path()
    except RuntimeError:
        return
    if not os.path.isfile(runtime_config):
        return
    try:
        _claim_coreclr(runtime_config)
    except ImportError:
        pass  # pythonnet/clr_loader not installed yet


class _StaticTypeProxy:
    """Wraps a `System.Type` obtained via reflection (Assembly.GetType) so `.SomeMethod(*args)`
    still works as if it were a normal pythonnet-imported class.

    Root cause (confirmed against pythonnet's actual source, not guessed): pythonnet's
    `from Namespace import Class` only works for a type if AssemblyManager.ScanAssembly's
    `Assembly.GetExportedTypes()` call succeeded for the WHOLE containing assembly first
    (AssemblyManager.cs GetTypes()) -- and that call throws FileNotFoundException (silently
    swallowed, returning zero types for the ENTIRE assembly) if ANY exported type anywhere in
    Ruri.RipperHook.dll can't resolve one of its own dependencies, even ones having nothing to
    do with RipperBlenderBridge. clr.AddReference() itself still succeeds (the assembly file
    loads fine), so _ensure_runtime() falls back to Assembly.GetType(fullName) -- a single-type,
    much narrower reflection lookup that isn't affected by that whole-assembly scan failure.

    But a raw reflected System.Type crosses into Python as a plain object exposing Type's OWN
    instance API (.Name, .GetMethod(), ...) -- NOT as the callable class it describes (that
    special wrapping, ReflectedClrType, is pythonnet's import-hook machinery specifically,
    confirmed in Converter.ToPython: a Type value takes the generic CLRObject.GetReference
    path, not ReflectedClrType.GetOrCreate). So `RipperBlenderBridge.ListAvailableHooks()`
    fails with AttributeError. This proxy makes `.SomeMethod(*args)` dispatch through
    `GetMethod(name).Invoke(None, args)` (static: no target instance) instead, sidestepping
    pythonnet's class-wrapping entirely -- pure .NET reflection, unaffected by any of the above.
    """

    def __init__(self, clr_type):
        self._clr_type = clr_type

    def __getattr__(self, name):
        method = self._clr_type.GetMethod(name)
        if method is None:
            raise AttributeError(f"{self._clr_type.FullName} has no method '{name}'")

        def call(*args):
            import System

            def coerce(value):
                # Boxing straight into Object[] keeps Python scalars as PyInt/
                # PyFloat wrappers, which MethodInfo.Invoke then can't bind to an
                # Int32/Double parameter ("Object of type 'Python.Runtime.PyInt'
                # cannot be converted...") -- pythonnet only runs its numeric
                # conversion when the TARGET type is known, and Object gives it
                # nothing to aim at. Convert scalars explicitly. bool first:
                # it's an int subclass in Python.
                if isinstance(value, bool):
                    return System.Boolean(value)
                if isinstance(value, int):
                    return System.Int32(value)
                if isinstance(value, float):
                    return System.Double(value)
                return value

            arg_array = System.Array[System.Object]([coerce(a) for a in args]) if args else None
            try:
                return method.Invoke(None, arg_array)
            except Exception as exc:
                # MethodInfo.Invoke wraps any exception the target method itself throws in a
                # System.Reflection.TargetInvocationException -- unwrap it so callers (and
                # _report_exception's `type(exc).__name__`) see the real underlying exception
                # (DirectoryNotFoundException, etc.), not just "TargetInvocationException" for
                # every possible C#-side error.
                inner = getattr(exc, "InnerException", None)
                if inner is not None:
                    raise inner from exc
                raise

        return call


def _ensure_runtime():
    """Boot CoreCLR (once per host process -- it cannot be re-pointed or
    unloaded once set, whether that "once" was this call or an earlier
    claim_runtime_early()) and load Ruri.RipperHook.dll."""
    global _bridge_type
    if _bridge_type is not None:
        return
    dll_dir, runtime_config = _runtime_config_path()
    if not os.path.isfile(runtime_config):
        raise RuntimeError(f"Missing runtimeconfig.json next to the DLL: {runtime_config}")
    _claim_coreclr(runtime_config)

    if dll_dir not in sys.path:
        sys.path.append(dll_dir)
    import clr
    import System

    dll_path = os.path.join(dll_dir, "Ruri.RipperHook.dll")
    clr.AddReference(dll_path)
    assembly = next((a for a in System.AppDomain.CurrentDomain.GetAssemblies()
                     if str(a.GetName().Name) == "Ruri.RipperHook"), None)
    if assembly is None:
        raise RuntimeError(
            f"Ruri.RipperHook.dll (loaded from {dll_path}) is not among "
            "AppDomain.CurrentDomain.GetAssemblies() after AddReference() -- the load itself failed.")

    # Diagnostic only, never fatal: if Ruri.RipperHook.dll has a type somewhere that can't
    # resolve one of its own dependencies, THIS is what silently empties AssemblyManager's
    # namespace scan for the whole assembly (see _StaticTypeProxy's doc comment) -- surface
    # exactly which dependency so the real fix (getting it into the bin dir) is findable,
    # without blocking on it, since Assembly.GetType() below doesn't need this to succeed.
    try:
        assembly.GetExportedTypes()
    except Exception as exc:
        missing = getattr(exc, "FileName", None) or getattr(exc, "Message", None) or str(exc)
        print(f"[RuriRipper] Ruri.RipperHook.dll: not every exported type resolves cleanly "
              f"({type(exc).__name__}: {missing}) -- this is why `from Ruri.RipperHook...import` "
              "doesn't work and the reflection fallback is needed; harmless if the fallback "
              "below still finds RipperBlenderBridge.")

    bridge_type = assembly.GetType("Ruri.RipperHook.Bridge.RipperBlenderBridge")
    if bridge_type is None:
        raise RuntimeError(
            "Ruri.RipperHook.dll loaded, but has no Ruri.RipperHook.Bridge.RipperBlenderBridge type -- "
            "rebuild Source/Ruri.RipperHook/Ruri.RipperHook.csproj against the latest source.")
    _bridge_type = _StaticTypeProxy(bridge_type)


def list_available_hooks():
    """Every hook id (e.g. "EndField_1.3.3") compiled into the loaded Ruri.RipperHook.dll, straight
    from RipperBlenderBridge.ListAvailableHooks() -- no RipperBridge session (Initialize with chosen
    hook ids) required first, since this only boots the CLR runtime and loads the DLL, then reflects
    over its already-loaded hook types. This is what a host's Hook picker populates its
    list from instead of a hardcoded/free-text id."""
    _ensure_runtime()
    return [str(h) for h in _bridge_type.ListAvailableHooks()]


def _string_array(strings):
    """pythonnet does not auto-marshal a plain Python list to
    IEnumerable<string>/string[] -- build a real System.String[] explicitly."""
    import System
    return System.Array[System.String](list(strings))


def _int_array(values):
    """Same story for int[] -- see _string_array."""
    import System
    return System.Array[System.Int32]([int(v) for v in values])


def _as_root_list(vfs_roots):
    """VFS-root parameters accept either one path (str) or a priority-ordered
    list of paths -- normalize to a list so callers don't have to remember
    to wrap a single root themselves."""
    return [vfs_roots] if isinstance(vfs_roots, str) else list(vfs_roots)


class RipperBridge:
    """One bridge session: Initialize once with the target game's hook id(s),
    then Build/Load a cabmap, browse rows, and pull a selection into memory.
    Call from one thread at a time -- the underlying C# side is written for a
    single active session per the CLI's own model (see RipperBlenderBridge's
    doc comments on GameFileLoader/GameBundleHook static state)."""

    def __init__(self, hook_ids):
        _ensure_runtime()
        self._bridge = _bridge_type
        self._bridge.Initialize(_string_array(hook_ids))
        self._hook_ids = tuple(hook_ids)
        self._map = None
        # {clip guid -> (meta_json, payload_bytes)} from the LAST import_cabs
        # call -- the zero-parse curve fast path (see ClipCurveBlob.cs).
        self.clip_curves_by_guid = {}
        # {mesh guid -> (meta_json, payload_bytes)} -- the geometry counterpart
        # (see MeshRawBlob.cs), same replacement policy per import_cabs call.
        self.mesh_blobs_by_guid = {}
        # {root guid -> hosting cab name} for the LAST import_cabs call -- the
        # per-root CAB attribution (RipperBlenderBridge.BuildRootCabs) that lets
        # a UNION closure's roots be split back into their sub-closures.
        self.root_cabs_by_guid = {}

    @property
    def hook_ids(self):
        """The hook id set this session was last (re)Initialize()d with -- see reinitialize()."""
        return self._hook_ids

    def reinitialize(self, hook_ids):
        """Re-apply a (possibly different) hook selection onto this SAME session, preserving
        self._map/clip_curves_by_guid -- unlike constructing a fresh RipperBridge, this does not
        drop an already-loaded cabmap. Safe/idempotent on the C# side (RipperBlenderBridge.
        Initialize -> RuriHook.ApplyHooks diffs the desired hook id set against the currently
        active one and only enables/disables the delta -- see its doc comment "safe to call more
        than once per process"), so this is cheap even when hook_ids is unchanged. Callers should
        still skip the call when hook_ids == self.hook_ids to avoid the log spam ApplyHooks prints
        per hook transition."""
        self._bridge.Initialize(_string_array(hook_ids))
        self._hook_ids = tuple(hook_ids)

    @property
    def has_map(self):
        return self._map is not None

    def build_cab_map(self, game_root, out_path):
        """Scan game_root and write a fresh cabmap to out_path. Returns 0 on success."""
        return int(self._bridge.BuildCabMap(game_root, out_path))

    def load_cab_map(self, cab_map_path):
        """Load an existing cabmap file; must be called (or build_cab_map) before
        enumerate_rows()/import_cabs()."""
        self._map = self._bridge.LoadCabMap(cab_map_path)

    def enumerate_table(self):
        """The row set as a columnar row_table.RowTable -- raw blob/offset
        buffers in ONE interop crossing, nothing materialized per row (the
        load-path optimum; see row_table.py)."""
        if self._map is None:
            raise RuntimeError("No cabmap loaded -- call load_cab_map()/build_cab_map() first.")
        # Imported here, not at module scope: row_table needs numpy, and this
        # module has to stay importable (for claim_runtime_early) before the
        # bootstrap has installed it.
        from . import row_table
        return row_table.RowTable.from_packed(self._bridge.EnumerateTablePacked(self._map))

    def search_table(self, query, rules=None, sort_column="name", sort_direction=0):
        """Quick search + Include/Exclude rules + sort over the loaded cabmap, on the C#
        CabTableSearch engine (the SAME implementation the WinForms browser runs -- the
        hosts carry no row-matching logic of their own). ``rules`` is any iterable of
        objects exposing .field/.relation/.value/.action/.enabled (the UI rule duck type);
        ``sort_direction`` is 0 = load order, 1 = ascending, 2 = descending. Returns the
        visible row ids as a numpy int32 array, already sorted."""
        if self._map is None:
            raise RuntimeError("No cabmap loaded -- call load_cab_map()/build_cab_map() first.")
        import numpy as np
        flat = []
        for rule in (rules or ()):
            flat.extend((str(rule.field), str(rule.relation), str(rule.value),
                         str(rule.action), "1" if rule.enabled else "0"))
        payload = self._bridge.SearchTable(self._map, query or "",
                                           _string_array(flat) if flat else None,
                                           str(sort_column), int(sort_direction))
        return np.frombuffer(bytes(payload), dtype="<i4")

    def sort_rows(self, row_ids, sort_column, sort_direction):
        """Sort an explicit row-id subset (the folder view's listing) by a display column --
        same engine, encoding and direction semantics as search_table."""
        if self._map is None:
            raise RuntimeError("No cabmap loaded -- call load_cab_map()/build_cab_map() first.")
        import numpy as np
        payload = self._bridge.SortRows(self._map, _int_array(row_ids),
                                        str(sort_column), int(sort_direction))
        return np.frombuffer(bytes(payload), dtype="<i4")

    def resolve_cabs_for_paths(self, container_paths):
        """Resolve addressable container paths (e.g. discover_scene_placements'
        asset_path values) to the CAB names that host them. Paths with no
        match are silently skipped -- compare len(input) to len(result) to
        check coverage. Requires a loaded cabmap."""
        if self._map is None:
            raise RuntimeError("No cabmap loaded -- call load_cab_map()/build_cab_map() first.")
        return [str(c) for c in self._bridge.ResolveCabsForPaths(self._map, _string_array(container_paths))]

    def resolve_closure_cab_names(self, cab_names):
        """Pure in-memory dependency-closure CAB-name enumeration for the
        given seed CABs -- no VFS decrypt, no AssetRipper export, just the
        already-loaded cabmap's own dependency graph (CabMap.
        ResolveClosureCabNames). Pair with enumerate_rows()' own type_names
        (already loaded per CAB) to answer "does this prefab's closure
        include an AnimationClip" without resolving/exporting anything.
        Requires a loaded cabmap."""
        if self._map is None:
            raise RuntimeError("No cabmap loaded -- call load_cab_map()/build_cab_map() first.")
        return [str(c) for c in self._bridge.ResolveClosureCabNames(self._map, _string_array(cab_names))]

    def find_direct_dependents(self, cab_names):
        """Reverse dependency lookup: every CAB that DIRECTLY depends on
        (references) any of the given seed CABs -- the mirror of
        resolve_closure_cab_names' forward walk (CabMap.FindDirectDependents,
        pure in-memory graph lookup via the cabmap's reverse-adjacency index,
        no VFS decrypt/export). Useful when an asset's real usage context
        isn't reachable from its own forward dependencies -- e.g. a Mesh-only
        FBX sub-asset carries no Material of its own; the Prefab whose
        Renderer pairs that mesh with a material is a direct dependent.
        Direct (one-hop) only; feed the results into import_cabs next to
        pull in each dependent's own forward closure. Requires a loaded
        cabmap."""
        if self._map is None:
            raise RuntimeError("No cabmap loaded -- call load_cab_map()/build_cab_map() first.")
        return [str(c) for c in self._bridge.FindDirectDependents(self._map, _string_array(cab_names))]

    def enumerate_vfs_files(self, vfs_roots, block_type_filter=None):
        """Every file recorded in every .blc manifest across vfs_roots (a
        path, or a priority-ordered list of paths -- e.g. [Persistent/VFS,
        StreamingAssets/VFS], see the C# doc comments on EnumerateVfsFiles/
        BuildMergedFileIndex for why a hot-update overlay root and the base
        client root normally both need to be passed together), of ANY block
        type (not just Unity-CAB-shaped entries). Returns plain dicts
        (file_name/file_name_hash/block_type/length/chk_path). Independent of
        load_cab_map() -- only needs Initialize() (an active session) to have
        run."""
        filter_arg = _string_array(block_type_filter) if block_type_filter else None
        return [
            {
                "file_name": f.FileName,
                "file_name_hash": int(f.FileNameHash),
                "block_type": f.BlockType,
                "length": int(f.Length),
                "chk_path": f.ChkPath,
            }
            for f in self._bridge.EnumerateVfsFiles(_string_array(_as_root_list(vfs_roots)), filter_arg)
        ]

    def extract_vfs_file(self, vfs_roots, file_name):
        """Raw decrypted bytes of one VFS-packed file, by its exact original
        name (as returned by enumerate_vfs_files' file_name). Tries vfs_roots
        in priority order with fallback -- a hot-update overlay can list a
        file it never duplicated because that patch didn't change it (see
        ExtractFirstAvailable's C# doc comment)."""
        return bytes(self._bridge.ExtractVfsFile(_string_array(_as_root_list(vfs_roots)), file_name))

    def read_character_models(self, cab_names):
        """Each character's authoritative model prefab name and expression-table
        tag, from the character data assets in cab_names. Returns
        {character_id: {"model", "tag", "asset"}}. A character's model is NOT
        derivable from its id -- no config table carries one -- so this asset is
        the only source. Read and field-extracted on the C# side."""
        if self._map is None:
            raise RuntimeError("No cabmap loaded -- call load_cab_map()/build_cab_map() first.")
        flat = [str(v) for v in self._bridge.ReadCharacterModels(self._map, _string_array(cab_names))]
        return {flat[i]: {"model": flat[i + 1], "tag": flat[i + 2], "asset": flat[i + 3]}
                for i in range(0, len(flat), 4)}

    def npc_prefab_manifest(self, vfs_roots):
        """Every npc template the game ships an assembled model for."""
        return [str(v) for v in self._bridge.NpcPrefabManifest(
            _string_array(_as_root_list(vfs_roots)))]

    def npc_prefab_parts(self, vfs_roots, template_id):
        """What one npc template is assembled from, as
        {character_id, lod_count, facial_morph, parts}. A generic npc ships no
        model prefab of its own -- the game builds it out of body/face/hair/ear/
        tail part prefabs, and only its own per-template manifest lists them.
        The json is read and parsed on the C# side; nothing is parsed here."""
        flat = [str(v) for v in self._bridge.NpcPrefabParts(
            _string_array(_as_root_list(vfs_roots)), template_id)]
        return {
            "character_id": flat[0],
            "lod_count": int(flat[1] or 0),
            "facial_morph": flat[2],
            "avatar_templet": flat[3],
            "parts": flat[4:],
        }

    def search_data_table(self, table, query):
        """Row ids of ``table`` (a column_table.ColumnTable from
        query_data_table) whose text matches ``query`` -- run by the SAME
        vectorized C# engine the cabmap browser searches with, over the very
        buffers that table was built from. Nothing is matched on this side.
        Returns a numpy int32 array."""
        import numpy as np
        return np.frombuffer(bytes(self._bridge.SearchDataTable(table.handle, query or "")),
                             dtype=np.int32)

    def query_data_table(self, vfs_roots, container_file, column_specs,
                         distinct_by="", prefer_non_empty="", cancellation=None):
        """Project one of the game's own self-describing data containers into a
        column_table.ColumnTable. ``container_file`` is a VFS file name; each
        entry of ``column_specs`` is (name, path) or (name, path, through_file,
        through_path), where ``through_file`` resolves the value at ``path`` as
        a key into that container's own keyed rows -- which is how a roster
        picks up its localized names. Column 0 of the result is the row key.

        The container declares its own schema, so no binding is generated,
        checked in, or able to drift; nothing is parsed on this side.

        ``cancellation`` takes a System.Threading.CancellationToken to abort a
        long read; the whole read/index/project chain honours it."""
        import System.Threading
        from . import column_table
        flat = []
        for spec in column_specs:
            name, path = spec[0], spec[1]
            through = spec[2] if len(spec) > 2 else ""
            through_path = spec[3] if len(spec) > 3 else ""
            # A join chain may be given as a list of hops; the wire form is ';'-separated.
            if not isinstance(through, str):
                through = ";".join(through)
            if not isinstance(through_path, str):
                through_path = ";".join(through_path)
            flat.extend((name, path, through, through_path))
        # CancellationToken.None cannot be written as an attribute here: None is a
        # python keyword, so the member has to be fetched by name.
        token = cancellation if cancellation is not None \
            else getattr(System.Threading.CancellationToken, "None")
        return column_table.ColumnTable.from_packed(self._bridge.QueryDataTable(
            _string_array(_as_root_list(vfs_roots)), container_file, _string_array(flat),
            distinct_by, prefer_non_empty, token))

    def enumerate_scene_maps(self, vfs_roots):
        """Every distinct map name with streaming-chunk data across vfs_roots."""
        return [str(m) for m in self._bridge.EnumerateSceneMaps(_string_array(_as_root_list(vfs_roots)))]

    def diagnose_schema_drift(self, vfs_roots, map_name):
        """Binary/vtable-level schema-drift report (list of str lines) for
        map_name's streaming chunks -- flags any FlatBuffers table type
        where the live game data declares more fields than the currently-
        compiled (1.2.4-era) bindings know how to read. See
        EndfieldSceneBridge.DiagnoseSchemaDrift's C# doc comment."""
        return [str(line) for line in
                self._bridge.DiagnoseSchemaDrift(_string_array(_as_root_list(vfs_roots)), map_name)]

    def discover_scene_placements(self, vfs_roots, map_name):
        """Every mesh-bearing entity placement for map_name's streaming chunks
        -- plain dicts (asset_path/asset_hash/entity_name/source_chunk/
        has_transform/px..sz/material_asset_paths). material_asset_paths is
        the SAME hash-LUT source as asset_path (FBPropertyAssetData,
        AssetType==1 instead of ==2) -- the entity's own real material(s),
        not a naming-convention guess. Cheap: no dependency closure resolved,
        no CAB loaded -- see DiscoverScenePlacements' C# doc comment."""
        return [
            {
                "asset_path": p.AssetPath,
                "asset_hash": int(p.AssetHash),
                "entity_name": p.EntityName,
                "source_chunk": p.SourceChunk,
                "has_transform": bool(p.HasTransform),
                "px": float(p.Px), "py": float(p.Py), "pz": float(p.Pz),
                "qx": float(p.Qx), "qy": float(p.Qy), "qz": float(p.Qz), "qw": float(p.Qw),
                "sx": float(p.Sx), "sy": float(p.Sy), "sz": float(p.Sz),
                "material_asset_paths": [str(m) for m in p.MaterialAssetPaths],
            }
            for p in self._bridge.DiscoverScenePlacements(_string_array(_as_root_list(vfs_roots)), map_name)
        ]

    def import_cabs(self, cab_names, export_class_ids=None):
        """Resolve cab_names' dependency closure, load it, export it in-memory, and return
        (assets, roots, seed_roots, clips_by_cab, scene_roots): assets is a plain Python dict
        keyed by lowercase guid holding each exported asset's own bytes, whatever AssetRipper
        wrote for it -- YAML, PNG, TGA, EXR, anything. The bridge never inspects a payload's
        format, so a caller decodes what it asked for and a new export format needs no change
        anywhere (see BridgeAssetDatabase, which does exactly that lazily);
        roots is the list of guids that are the actual importable (.prefab) top-level assets;
        seed_roots is {cab_name: guid} for each requested cab_names entry that resolved to its
        own asset -- resolved bridge-side directly through the cabmap's own CAB/addressable-path
        identity (RipperBlenderBridge.Partition/NormalizeExportPath), NOT by matching display
        names, so a caller never needs its own name-matching heuristic to figure out which of
        `roots` corresponds to which requested CAB (a single seed's closure routinely resolves
        to more than one root .prefab, e.g. a co-resolved portrait/uimodel variant).

        clips_by_cab is {lowercased cab_name: [clip guid, ...]} for EVERY AnimationClip the
        export wrote, captured asset-side during the export itself (see RipperBlenderBridge.
        ClipCaptureExporter) -- the clip counterpart of seed_roots: a clip CAB's addressable
        path is its host FBX ("...a_x_01.fbx") while the exported .anim is named after the
        clip's own m_Name ("...A_x_ACL.anim"), one CAB can host several clips, and the two
        stems genuinely differ -- so this map is the ONLY correct way to translate a clip-CAB
        browser row into its real clip documents; never join display names to m_Names.

        export_class_ids: optional ClassID allowlist applied to the EXPORT side only
        (RipperBlenderBridge.ImportCabsFiltered). The closure is still resolved, loaded and
        processed in full -- humanoid muscle solve and hashed-curve-path restore need the whole
        rig in scope -- but only assets of the listed classes are serialized and returned. The
        standalone-clip flow passes [AnimationClip's id]: its closure co-seeds the entire
        character for scope, and re-serializing that character's textures/meshes was most of the
        call's wall time for data the flow never reads."""
        if self._map is None:
            raise RuntimeError("No cabmap loaded -- call load_cab_map()/build_cab_map() first.")
        cab_names = list(cab_names)
        if export_class_ids:
            result = self._bridge.ImportCabsFiltered(self._map, _string_array(cab_names),
                                                     _int_array(export_class_ids))
        else:
            result = self._bridge.ImportCabs(self._map, _string_array(cab_names))
        # .NET IReadOnlyDictionary crosses into Python as an iterable of
        # KeyValuePair (no dict-like .items()) -- iterate and pull .Key/.Value.
        assets = {str(kvp.Key).lower(): bytes(kvp.Value) for kvp in result.Assets}
        roots = [str(g).lower() for g in result.Roots]
        seed_roots = {str(kvp.Key): str(kvp.Value).lower() for kvp in result.SeedRoots}
        clips_by_cab = {str(kvp.Key).lower(): [str(g).lower() for g in kvp.Value]
                        for kvp in result.ClipGuidsByCab}
        # Scene (.unity) roots -- a non-bundled build's level files export their whole
        # GameObject hierarchy as a scene, not a prefab; these guids are ALSO in roots.
        scene_roots = {str(g).lower() for g in result.SceneRoots}
        # Per-clip curve blobs (JSON index + float32 payload, see ClipCurveBlob.cs):
        # the same curves the YAML documents carry, handed over as raw numbers so
        # clip building never re-parses them out of 80+MB of text. Exposed as an
        # attribute (not another tuple slot) so every existing 6-tuple unpacker
        # keeps working; replaced wholesale on each import_cabs call. bytes() on a
        # .NET byte[] is a straight memcpy.
        self.clip_curves_by_guid = {}
        meta_by_guid = {str(kvp.Key).lower(): str(kvp.Value) for kvp in result.ClipCurveMeta}
        for kvp in result.ClipCurveData:
            guid = str(kvp.Key).lower()
            meta = meta_by_guid.get(guid)
            if meta:
                self.clip_curves_by_guid[guid] = (meta, bytes(kvp.Value))
        # Mesh raw blobs (JSON index + raw buffer payload, see MeshRawBlob.cs):
        # the geometry the YAML documents carry, as the bytes they already were --
        # mesh building never parses multi-MB hex text again.
        self.mesh_blobs_by_guid = {}
        mesh_meta_by_guid = {str(kvp.Key).lower(): str(kvp.Value) for kvp in result.MeshBlobMeta}
        for kvp in result.MeshBlobData:
            guid = str(kvp.Key).lower()
            meta = mesh_meta_by_guid.get(guid)
            if meta:
                self.mesh_blobs_by_guid[guid] = (meta, bytes(kvp.Value))
        # Per-root CAB attribution (see BuildRootCabs) -- how a union closure's
        # root set is split back into "the hierarchy rows' own roots" vs the
        # co-seeded clip/avatar CABs' rig prefabs.
        self.root_cabs_by_guid = {str(kvp.Key).lower(): str(kvp.Value).lower()
                                  for kvp in result.RootCabs}
        return assets, roots, seed_roots, clips_by_cab, scene_roots

    def find_associated_avatar_cabs(self, clip_cab_name):
        """Every Avatar-bearing CAB in a clip-hosting CAB's dependency neighborhood, nearest
        first, via the cabmap's own dependency graph: reverse BFS to the clip's dependents (the
        AnimatorController, then the character prefabs), then each dependent's forward closure
        -- see RipperBlenderBridge.FindAssociatedAvatarCabs. Returns a (possibly empty) list.
        Co-seed ALL of them into import_cabs alongside the clip CAB: (a) AssetRipper itself then
        restores the clips' hashed curve paths to real "Root/Bip001/..." strings (verified
        against the real game: a clip CAB alone has no dependencies and its curve paths export
        as "path_0x<CRC32>_<suffix>" placeholders), and (b) the importer's Avatar search
        picks the first Avatar that actually builds a working muscle retargeter -- the
        neighborhood routinely contains stub Avatars (7KB, empty m_TOS, zeroed skeleton ids)
        alongside the real one (verified: pelica's battle rig surfaces the stub BEFORE the real
        334KB avatar), and which is which is only knowable from the exported content."""
        if self._map is None:
            raise RuntimeError("No cabmap loaded -- call load_cab_map()/build_cab_map() first.")
        cabs = self._bridge.FindAssociatedAvatarCabs(self._map, clip_cab_name, 4)
        return [str(c) for c in cabs]
