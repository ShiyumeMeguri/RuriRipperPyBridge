"""The cabmap browser's whole model, with no widgets in it: the columnar row
table, the pythonnet bridge session, the virtual-folder-tree navigation state,
and search/sort/selection bookkeeping.

Deliberately NOT the host's own model type -- not a bpy CollectionProperty, not
a Qt model. At real-world cabmap scale (~260k rows for Endfield 1.3.3, confirmed
against the real game) materializing every row into host objects is itself the
bottleneck: RNA allocation per element, .blend/undo bloat and O(n) mutation cost
on the Blender side, widget-item churn on the Qt side. Exactly why the WinForms
browser this one mirrors used VirtualMode plus a plain backing list. Each host's
panel materializes only ``display_window()`` -- a capped, already-filtered
window -- and nothing else.

Per game, one session. All of that per-cabmap state (rows, folder tree,
selection, sort, filter, the animation-build handover) lives on a GameSession,
one per game name, so several games' cabmaps can be open at once and switched
between without either one throwing the other away -- a CabMapHandle is a pure
value object and the bridge holds many (see pythonnet_bridge.use_game). ``activate``
picks the live one; the module-level session-field names (ROWS, VISIBLE,
CURRENT_DIR, ...) are a read/write VIEW onto whichever session is active, proxied
through __getattr__/set_select_anchor so every existing ``cabmap_state.ROWS`` reader
sees the active game with no change. The ONE thing that is genuinely process-wide
and shared across games is BRIDGE, the CLR session itself.

The one thing that stays with the host is the search DEBOUNCE TIMER, because
that is a UI event loop's job (``bpy.app.timers`` / ``QTimer``); the policy
constant it needs (SEARCH_DEBOUNCE_SECONDS) and the work it triggers
(``reapply_filter``) are both here.
"""

from __future__ import annotations

from ..runtime import pythonnet_bridge

# Holds the loaded cabmaps (each a multi-second load) and the live bridge session,
# so a host's script reload skips this module instead of throwing them away. The
# per-game sessions (SESSIONS/ACTIVE) and BRIDGE all ride through a reload intact.
HOLDS_PROCESS_STATE = True

DISPLAY_CAP = 500  # max rows ever materialized into the UI at once
SEARCH_DEBOUNCE_SECONDS = 0.25  # the host's own timer applies this

# The CLR session, process-wide and shared by every game's cabmap (its _map is
# switched per game by pythonnet_bridge.use_game). Not session state: a true
# single bridge for the whole process.
BRIDGE = None   # pythonnet_bridge.RipperBridge | None


# --- Virtual folder tree node -------------------------------------------------
# Defined before GameSession because a session starts with an empty _ROOT node.

_NO_PATH_BUCKET = "(no virtual path)"  # synthetic root folder for the rare row with zero container paths


class _Node:
    """One folder-tree node, keyed into by its parent's `children` dict under
    its own path segment -- that segment IS the node's leaf name, so nothing
    here stores its own name. A node with `children` is browsable as a
    folder; a node with `files` means at least one row's container path ends
    exactly here (both can be true at once: some other row's path continues
    past this one -- rare, but shown as both a folder and a file rather than
    picking one)."""

    __slots__ = ("children", "files", "file_count")

    def __init__(self):
        self.children = {}   # str segment -> _Node
        self.files = []      # list[int] -- ROWS indices whose container path ends exactly here
        self.file_count = 0  # recursive count of files at or below this node


class GameSession:
    """One game's cabmap browser state. Everything here is about a SINGLE loaded
    cabmap; a second game gets its own GameSession, and switching between them is
    ``activate``. The field names match the module-level view names exactly, which
    is what lets __getattr__ proxy ``cabmap_state.ROWS`` straight through to the
    active session."""

    __slots__ = ("game", "ROWS", "VISIBLE", "CURRENT_DIR", "CURRENT_SUBFOLDERS",
                 "SELECTED_CABS", "SELECT_ANCHOR", "ANIMATION_BUILD_STATE",
                 "_ROOT", "_ROWS_BY_CAB", "_sort_column", "_sort_dir", "_active_rules")

    def __init__(self, game):
        self.game = game
        self.ROWS = []             # row_table.RowTable -- the full cabmap, set by load_rows()
        self.VISIBLE = []          # list[int] -- indices into ROWS after the current filter+sort
        self.CURRENT_DIR = ()      # tuple[str, ...] -- () is the virtual root; segments of the browsed folder
        self.CURRENT_SUBFOLDERS = []  # list[(name, recursive_file_count)] -- CURRENT_DIR's child folders, alpha-sorted
        self.SELECTED_CABS = set()    # cab keys of every selected row
        self.SELECT_ANCHOR = None     # ROWS index of the last plainly-clicked row (Shift range anchor)
        self.ANIMATION_BUILD_STATE = None  # animation-build handover dict | None (see below)
        self._ROOT = _Node()          # virtual folder tree root
        self._ROWS_BY_CAB = None      # lazily built cab -> row view (see rows_by_cab)
        self._sort_column = "name"
        self._sort_dir = 0            # 0 = unsorted (load order), 1 = ascending, 2 = descending
        self._active_rules = ()       # whatever was last passed to apply_filter()'s `rules` arg


# game name (or None for a nameless / not-yet-recognised session) -> GameSession.
SESSIONS = {}
# The session every module-level session-field name currently views.
ACTIVE = None


def session_for(game):
    """The GameSession for ``game`` (a GameType member name, or None for the
    nameless session a plain un-hooked Unity build browses under), created empty
    on first use."""
    session = SESSIONS.get(game)
    if session is None:
        session = GameSession(game)
        SESSIONS[game] = session
    return session


def activate(game):
    """Make ``game``'s session the live one every ``cabmap_state.<field>`` view
    reads. When BRIDGE already has that game's cabmap loaded, the bridge's own
    _map/decoder is switched in lockstep (use_game), so a search or import against
    the active session hits the right cabmap and there is no way to leave the model
    and the bridge disagreeing about which game is current. A game with no loaded
    map yet (session created, cabmap not built/loaded) just switches the Python
    view."""
    global ACTIVE
    ACTIVE = session_for(game)
    if BRIDGE is not None and game in BRIDGE.maps_by_game:
        BRIDGE.use_game(game)
    return ACTIVE


def drop(game):
    """Discard ``game``'s browser session entirely -- its rows, folder tree,
    selection and animation handover -- when its tab is closed. Only this one
    game's in-memory view is released; the process-wide BRIDGE and every other
    game's session are untouched (HOLDS_PROCESS_STATE). If it was the live one,
    the nameless session becomes active so every module-level view still
    resolves."""
    global ACTIVE
    session = SESSIONS.pop(game, None)
    if session is None:
        return
    if ACTIVE is session:
        ACTIVE = session_for(None)


def active_game():
    """The game name the live session is for, or None for the nameless session."""
    return ACTIVE.game if ACTIVE is not None else None


# The names that are a VIEW onto ACTIVE rather than real module attributes.
# __getattr__ (PEP 562, only called on a normal-lookup miss) resolves each read
# to the live session; writes to SELECT_ANCHOR go through set_select_anchor. None
# of these is ever assigned at module scope -- doing so would shadow the proxy.
_SESSION_FIELDS = frozenset({
    "ROWS", "VISIBLE", "CURRENT_DIR", "CURRENT_SUBFOLDERS",
    "SELECTED_CABS", "SELECT_ANCHOR", "ANIMATION_BUILD_STATE",
    "_ROOT", "_ROWS_BY_CAB", "_sort_column", "_sort_dir", "_active_rules",
})


def __getattr__(name):
    if name in _SESSION_FIELDS:
        return getattr(ACTIVE, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


ACTIVE = session_for(None)  # a nameless default session so the module works before any game is picked


def clear_selection():
    ACTIVE.SELECTED_CABS.clear()
    ACTIVE.SELECT_ANCHOR = None


def set_select_anchor(row_index):
    """Set the Shift-range anchor on the active session -- the one session field a
    host writes back (a click's anchor), so it gets an explicit setter rather than
    a bare module-attribute assignment the read-only view proxy could not catch."""
    ACTIVE.SELECT_ANCHOR = row_index


def selected_row_indices():
    """Selected rows as ROWS indices, in master ROWS order -- the deterministic
    order a batch import runs in (not click order, which nobody can
    reproduce)."""
    if not ACTIVE.SELECTED_CABS or not len(ACTIVE.ROWS):
        return []
    index_of = ACTIVE.ROWS.cab_to_index()
    return sorted(index_of[cab] for cab in ACTIVE.SELECTED_CABS if cab in index_of)


def selected_row_dicts():
    """The same selection as row views (dict-compatible)."""
    return [ACTIVE.ROWS[i] for i in selected_row_indices()]


def selected_cabs():
    """The same selection as bare cab names -- what import_cabs() seeds."""
    return [ACTIVE.ROWS.cab(i) for i in selected_row_indices()]


# --- Process-Monitor-style Include/Exclude rules. The DATA shape only: matching
# lives in the ONE C# engine (CabTableSearch, the same implementation behind the
# WinForms browser), reached through RipperBridge.search_table -- a host never
# evaluates a rule itself. A "rule" is anything exposing .field / .relation /
# .value / .action / .enabled attributes -- a DUCK TYPE on purpose, so a host can
# pass its own UI-backed storage straight in (Blender's rules really are bpy
# PropertyGroup instances) and Rule below covers headless and Qt-side use.
#
# Every enabled rule is a required constraint: Include(X) means the row MUST
# match X, Exclude(X) means the row must NOT match X. A row passes only if
# ALL enabled rules hold simultaneously (empty/all-disabled rule set => show
# everything).

FILTER_FIELDS = ("name", "container", "type_names", "source", "deps")
FIELD_LABELS = {"name": "Name", "container": "Container", "type_names": "Type",
                 "source": "Source", "deps": "Deps"}

RELATIONS = ("is", "is_not", "contains", "excludes", "begins_with", "ends_with",
             "less_than", "more_than", "matches_regex", "not_matches_regex")
RELATION_LABELS = {
    "is": "is", "is_not": "is not", "contains": "contains", "excludes": "excludes",
    "begins_with": "begins with", "ends_with": "ends with",
    "less_than": "less than", "more_than": "more than",
    "matches_regex": "matches regex", "not_matches_regex": "not matches regex",
}

ACTIONS = ("include", "exclude")


class Rule:
    """Plain-Python rule -- the duck type above, for every caller that doesn't
    already have host-side rule storage of its own."""
    __slots__ = ("field", "relation", "value", "action", "enabled")

    def __init__(self, field, relation, value, action, enabled=True):
        self.field = field
        self.relation = relation
        self.value = value
        self.action = action
        self.enabled = enabled


def reset():
    """Reset the ACTIVE session back to empty (its rows, folder tree, selection,
    sort and animation handover). Only the current session -- the other games'
    sessions and the process-wide BRIDGE are left alone, matching HOLDS_PROCESS_STATE:
    a reset is "clear what I'm looking at", not "throw away the CLR runtime and every
    loaded cabmap"."""
    ACTIVE.ROWS = []
    ACTIVE.VISIBLE = []
    ACTIVE._sort_dir = 0
    ACTIVE._ROWS_BY_CAB = None
    ACTIVE.CURRENT_DIR = ()
    ACTIVE.CURRENT_SUBFOLDERS = []
    ACTIVE._ROOT = _Node()
    clear_selection()
    clear_animation_build_state()


def ensure_bridge(hook_ids, game_root):
    """Get (or lazily create) the process-wide bridge, with its hook selection kept in
    sync with hook_ids on every call -- NOT just on first construction.

    Root-cause fix (2026-07-18): this used to construct RipperBridge(hook_ids) once and return
    the SAME instance forever after, ignoring hook_ids on every later call. A cabmap "Build"/
    "Load" done with zero (or a since-corrected) hook selection -- e.g. before a hook the user
    wants was even listable, or before they'd ticked its checkbox -- permanently poisoned BRIDGE
    with no VFS game hook wired up. Re-ticking the checkbox and rebuilding/reloading the cabmap
    never fixed it: this function kept handing back the same broken session, and downstream
    scene-tab operators read cabmap_state.BRIDGE directly rather than calling ensure_bridge
    themselves, so they inherited the poisoning with no path to recover short of restarting the
    application. Symptom: "Discover maps failed: InvalidOperationException:
    No VFS game hook active" even with the right hook checked.
    Fix: re-apply the current hook_ids via RipperBridge.reinitialize() whenever they differ from
    what the existing session was last (re)Initialize()d with -- see reinitialize()'s doc comment
    for why this is safe (RuriHook.ApplyHooks is a diff against the currently active hook set, so
    re-Initialize only enables/disables the delta) and preserves the already-loaded cabmap/db
    instead of dropping it the way constructing a fresh RipperBridge would."""
    global BRIDGE
    hook_ids = tuple(hook_ids)
    game_root = str(game_root or "")
    if BRIDGE is None:
        BRIDGE = pythonnet_bridge.RipperBridge(hook_ids, game_root)
    elif BRIDGE.hook_ids != hook_ids or BRIDGE.game_root != game_root:
        BRIDGE.reinitialize(hook_ids, game_root)
    return BRIDGE


# --- Animation browser build context ----------------------------------------
# An animation browser only DISCOVERS which CABs in the selected row's
# dependency closure are AnimationClip-classed -- pure in-memory cabmap metadata
# (ResolveClosureCabNames + each CAB's already-loaded TypeNames), no VFS
# decrypt, no AssetRipper export, no db. Actually building an action is deferred
# until the user checks specific clips and asks for them. That later build needs
# a real skeleton to attach onto AND a real db (guid-keyed resolved closure) to
# translate a checked clip's CAB name into its actual guid; two paths lead
# there:
#   - the character was already fully imported -- set_animation_build_state
#     records the REAL post-build fields immediately (db/arm_name/maps/
#     path_to_meshobjects all present).
#   - only clip DISCOVERY (cheap, no import_cabs call at all) has run so far --
#     the state has no db/skeleton yet, just enough to resolve them lazily
#     (seed_cabs + options) the first time the user actually asks to attach a
#     clip (mark_animation_build_done fills in the real fields once that
#     happens). Keyed to a single character at a time: discovering/importing a
#     different one replaces this outright.
#
# The values are opaque here on purpose -- ``arm_name`` and
# ``path_to_meshobjects`` mean whatever the host's own builder put in them.
# This module only guarantees the handover survives between the two clicks. It
# rides on the active session, so each game's in-progress animation build is its
# own.


def set_animation_build_state(db, arm_name, maps, path_to_meshobjects):
    """A character was just FULLY imported (mesh/skeleton/materials already
    built) -- record its real post-build fields immediately."""
    ACTIVE.ANIMATION_BUILD_STATE = {
        "db": db,
        "arm_name": arm_name,
        "maps": maps,
        "path_to_meshobjects": path_to_meshobjects,
        "seed_cabs": [],
        "options": None,
    }


def set_animation_discovery_state(seed_cabs, options):
    """Only a cheap CAB-level clip discovery has happened -- no import_cabs
    call yet, no db, nothing built into the scene. arm_name/maps/
    path_to_meshobjects/db stay unset until mark_animation_build_done runs
    the lazy full import (which resolves the seed closure for the first
    time). seed_cabs is a LIST -- discovery runs over the whole multi-
    selection, and the lazy build must co-seed every one of them or clips
    discovered from the extra rows would never resolve in clips_by_cab."""
    ACTIVE.ANIMATION_BUILD_STATE = {
        "db": None,
        "arm_name": None,
        "maps": None,
        "path_to_meshobjects": None,
        "seed_cabs": list(seed_cabs),
        "options": options,
    }


def mark_animation_build_done(db, arm_name, maps, path_to_meshobjects):
    """Fill in the real post-build fields once the lazy full import has actually
    happened, so a second click attaches more clips to the SAME armature instead
    of re-importing."""
    if ACTIVE.ANIMATION_BUILD_STATE is not None:
        ACTIVE.ANIMATION_BUILD_STATE["db"] = db
        ACTIVE.ANIMATION_BUILD_STATE["arm_name"] = arm_name
        ACTIVE.ANIMATION_BUILD_STATE["maps"] = maps
        ACTIVE.ANIMATION_BUILD_STATE["path_to_meshobjects"] = path_to_meshobjects


def clear_animation_build_state():
    ACTIVE.ANIMATION_BUILD_STATE = None


def load_rows(preferred_dir=()):
    """Pull every row from the currently-loaded cabmap into the active session's
    ROWS and point the browser at ``preferred_dir`` (the virtual root when
    omitted). ROWS is a columnar row_table.RowTable -- indexing/iteration yield
    dict-compatible row views, so per-row consumers are unchanged while the hot
    paths (search/sort/window) run columnar. Reads the bridge's CURRENT _map, so
    the caller selects the game first (activate / use_game).

    ``preferred_dir`` lets a host restore the folder the user was last browsing
    instead of dumping them back at the root on every Load; a path that no
    longer exists in THIS map (a different game, a renamed folder) simply falls
    back to the root -- see browse_dir."""
    if BRIDGE is None:
        raise RuntimeError("No bridge session -- call ensure_bridge() first.")
    ACTIVE.ROWS = BRIDGE.enumerate_table()
    ACTIVE._ROWS_BY_CAB = None  # rebuilt lazily on first rows_by_cab() call
    clear_selection()           # cab keys from a previous map mean nothing in this one
    _build_tree(preferred_dir)  # also sets CURRENT_DIR/VISIBLE/CURRENT_SUBFOLDERS


class _RowsByCab:
    """cab -> row-view mapping over a columnar RowTable: the cab->index dict
    is the table's own lazy index; row views materialize per lookup only."""

    __slots__ = ("_table",)

    def __init__(self, table):
        self._table = table

    def get(self, cab, default=None):
        index = self._table.cab_to_index().get(cab)
        return self._table[index] if index is not None else default

    def __getitem__(self, cab):
        return self._table[self._table.cab_to_index()[cab]]

    def __contains__(self, cab):
        return cab in self._table.cab_to_index()


def rows_by_cab():
    """cab -> row-view mapping, built once per load_rows() and cached -- used
    to look up TypeNames/Name for a batch of dependency-closure CAB names
    (see resolve_closure_cab_names) without an O(closure_size * len(ROWS))
    linear scan."""
    if ACTIVE._ROWS_BY_CAB is None:
        ACTIVE._ROWS_BY_CAB = _RowsByCab(ACTIVE.ROWS)
    return ACTIVE._ROWS_BY_CAB


# --- Virtual folder tree ------------------------------------------------------
# The browser's default view: a real file-browser-style drill-down over each
# row's container path(s) (Unity's own AssetBundle.Container addressable keys,
# see CabMap.Entry.ContainerPaths -- already lowercase/"/"-separated, exactly a
# virtual filesystem path) instead of dumping all ~260k rows flat. Built once
# per load_rows() in O(total path segments); browse_dir() then reads it in
# O(children of that folder), so opening a folder never rescans ROWS the way
# an ad-hoc per-click scan would.


def _add_leaf(segments, row_index):
    node = ACTIVE._ROOT
    for seg in segments:
        child = node.children.get(seg)
        if child is None:
            child = _Node()
            node.children[seg] = child
        node = child
        node.file_count += 1
    node.files.append(row_index)


def _build_tree(preferred_dir=()):
    """Rebuild the folder tree from ROWS and reset browsing to the root. A row
    exported under more than one container path (rare) appears as its own
    leaf under EVERY one of its paths -- the same asset reachable from more
    than one virtual name, same as the real game would resolve it. A row that
    lands nowhere (zero container paths, or every one of them is blank/all-
    separators) falls back to a child of _NO_PATH_BUCKET keyed by its own cab
    id, so it stays reachable (as a FOLDER you can open, not a same-named
    leaf sitting directly on the bucket node -- a childless node reads as a
    file, not a folder, see _Node) instead of silently vanishing.

    Local-bound methods + a plain list instead of a genexpr/tuple() (segments
    is only ever iterated here, never used as a dict key) -- ~45% faster at
    real cabmap scale (260k rows, confirmed by measurement), worth it since
    this runs synchronously inside the Build/Load operator."""
    ACTIVE._ROOT = _Node()
    rows = ACTIVE.ROWS
    path_count_of = rows.container_path_count
    path_of = rows.container_path
    cab_of = rows.cab
    for index in range(len(rows)):
        placed = False
        for p in range(path_count_of(index)):
            segments = [s for s in path_of(index, p).split("/") if s]
            if segments:
                _add_leaf(segments, index)
                placed = True
        if not placed:
            _add_leaf((_NO_PATH_BUCKET, cab_of(index)), index)
    browse_dir(tuple(preferred_dir))


def folder_of(row_index, path_index=0):
    """The folder-tree path (tuple of segments, NOT including the row's own
    leaf name) that ROWS[row_index]'s path_index'th container path lives
    under -- mirrors _build_tree's own placement logic exactly (including its
    zero-container-paths fallback into a _NO_PATH_BUCKET folder keyed by cab),
    so "jump to this row's folder" always lands exactly where browse_dir
    would already show it. Hosts persist the browsed folder as the "/"-joined
    form of CURRENT_DIR and hand it back to load_rows(); see dir_to_key/
    key_to_dir for that round trip. path_index is clamped, not validated -- callers
    that don't care which of a multi-path row's folders they land in (the
    common case) can just pass the default 0."""
    rows = ACTIVE.ROWS
    path_count = rows.container_path_count(row_index)
    if path_count == 0:
        return (_NO_PATH_BUCKET,)
    path = rows.container_path(row_index, min(max(path_index, 0), path_count - 1))
    segments = [s for s in path.split("/") if s]
    return tuple(segments[:-1])


def best_path_index_for_jump(row_index, query):
    """Which of ROWS[row_index]'s container paths folder_of() should target
    when a row has more than one (rare) -- picks whichever path is actually
    relevant to how the row is CURRENTLY being shown, instead of blindly
    defaulting to path 0. RowTable.name()'s own "always path[0]'s leaf"
    default is exactly what a multi-path row's OTHER paths can silently
    disagree with -- the same trap leaf_name_in_current_dir already
    documents and works around for the folder-browse view; a flat
    search-result row needs the equivalent fix, or jumping from a match that
    only exists on path 1+ lands in path 0's unrelated folder instead (e.g.
    an animation-CAB path when the row actually matched on a dynamicassets
    path -- confirmed report).

    - Folder-browse view (query blank -- no active search text): the path
      whose folder segments equal CURRENT_DIR, i.e. the SAME identity the
      row is already being displayed under (browse_dir already put you
      there, so this normally resolves back to a no-op).
    - Flat search-result view (query non-blank): the first path containing
      the search text -- the one the row actually matched on.
    - Falls back to path 0 when neither applies (e.g. the row matched via a
      different field -- Type/Source -- or purely through an Include/Exclude
      rule with no plain search text typed)."""
    rows = ACTIVE.ROWS
    path_count = rows.container_path_count(row_index)
    if path_count <= 1:
        return 0

    needle = (query or "").strip().lower()
    if not needle:
        depth = len(ACTIVE.CURRENT_DIR)
        for p in range(path_count):
            segments = tuple(s for s in rows.container_path(row_index, p).split("/") if s)
            if len(segments) == depth + 1 and segments[:depth] == ACTIVE.CURRENT_DIR:
                return p
        return 0

    for p in range(path_count):
        if needle in rows.container_path(row_index, p).lower():
            return p
    return 0


def _node_at(path):
    node = ACTIVE._ROOT
    for seg in path:
        node = node.children.get(seg)
        if node is None:
            return None
    return node


def browse_dir(path):
    """Point the browser at a virtual folder (a tuple of path segments, ()
    for root) and recompute VISIBLE/CURRENT_SUBFOLDERS for exactly that
    folder's own children -- O(children), never O(len(ROWS)). An unreachable
    path (e.g. CURRENT_DIR from a since-replaced cabmap) falls back to root
    rather than showing a dead end."""
    node = _node_at(path)
    if node is None:
        path, node = (), ACTIVE._ROOT
    ACTIVE.CURRENT_DIR = tuple(path)
    subfolders = []
    files = []
    for name, child in node.children.items():
        if child.children:  # has descendants beyond itself -> browsable folder
            subfolders.append((name, child.file_count))
        if child.files:     # a row's container path ends exactly here -> also a file entry
            files.extend(child.files)
    subfolders.sort(key=lambda pair: pair[0].lower())
    ACTIVE.CURRENT_SUBFOLDERS = subfolders
    ACTIVE.VISIBLE = files
    _apply_sort()


def dir_to_key(path=None):
    """The browsed folder as the flat "a/b/c" string a host persists (empty at
    the root). Defaults to CURRENT_DIR, i.e. "remember where I am now"."""
    return "/".join(ACTIVE.CURRENT_DIR if path is None else path)


def key_to_dir(key):
    """A persisted dir_to_key string back to a segment tuple. Blank/garbage
    resolves to the root, and browse_dir independently falls back to the root
    for a path that does not exist in the CURRENT map -- so a key saved against
    a different game can never strand the browser."""
    return tuple(segment for segment in (key or "").split("/") if segment)


def has_active_query(query, rules):
    """True when the flat global-search view should replace the folder
    browser -- non-blank quick-search text, or any ENABLED Include/Exclude
    rule (a disabled rule is inert, same as apply_filter treats it)."""
    if (query or "").strip():
        return True
    return any(r.enabled for r in rules)


def refresh_visible(query, rules=()):
    """The single dispatch point between the two views: flat global search/
    rule results (apply_filter) or the folder listing for CURRENT_DIR
    (browse_dir). Always refreshes _active_rules -- even when the folder-tree
    branch runs and skips apply_filter entirely -- so a later debounced
    search (reapply_filter, which only has the cached rules to go on) never
    fires against a stale or since-removed rule set."""
    ACTIVE._active_rules = tuple(rules)
    if has_active_query(query, rules):
        apply_filter(query, rules)
    else:
        browse_dir(ACTIVE.CURRENT_DIR)


def leaf_name_in_current_dir(index):
    """The display name for ROWS[index] AS BROWSED under CURRENT_DIR
    specifically. Matters only for the rare row with more than one container
    path: its default name (RowTable.name(), always path[0]'s leaf) can
    belong to a completely different folder than the one it's actually being
    shown in here. Falls back to that default if, somehow, none of the row's
    paths match (shouldn't happen for anything browse_dir actually placed in
    VISIBLE)."""
    rows = ACTIVE.ROWS
    depth = len(ACTIVE.CURRENT_DIR)
    for p in range(rows.container_path_count(index)):
        segments = tuple(s for s in rows.container_path(index, p).split("/") if s)
        if len(segments) == depth + 1 and segments[:depth] == ACTIVE.CURRENT_DIR:
            return segments[-1]
    return rows.name(index)


def apply_filter(query, rules=()):
    """Row shows if it matches the quick search across Name/Container/Source/
    Type/Cab AND passes the Include/Exclude rule set -- one call into the C#
    CabTableSearch engine, which quick-searches the raw column blobs
    (vectorized, all cores), evaluates the rules over the narrowed candidates
    and hands back the ids ALREADY sorted by the current column. Always a FLAT
    result set over the whole cabmap, never scoped to CURRENT_DIR -- see
    refresh_visible for how this and the folder browser (browse_dir) dispatch
    between each other."""
    rules = tuple(rules)
    ACTIVE._active_rules = rules
    ACTIVE.CURRENT_SUBFOLDERS = []
    if BRIDGE is None:
        ACTIVE.VISIBLE = []
        return
    ACTIVE.VISIBLE = BRIDGE.search_table((query or "").strip(), rules,
                                         ACTIVE._sort_column, ACTIVE._sort_dir).tolist()


def reapply_filter(query):
    """Re-run refresh_visible with whatever rules were last active -- what a
    host's debounce timer calls when only the search text changed, not the rule
    set. Routes through refresh_visible, not apply_filter directly, so an empty
    query clearing back to no-rules-either correctly lands back in the folder
    browser instead of a stale flat view."""
    refresh_visible(query, ACTIVE._active_rules)


def active_rules():
    """The rule set the current VISIBLE was computed with."""
    return ACTIVE._active_rules


def _apply_sort():
    if ACTIVE._sort_dir == 0:
        ACTIVE.VISIBLE.sort()  # back to load order
        return
    if BRIDGE is None or not ACTIVE.VISIBLE:
        return
    # Same C# engine as apply_filter, over the current id set (the folder
    # view's listing, or a re-click on an already-filtered result).
    ACTIVE.VISIBLE = BRIDGE.sort_rows(ACTIVE.VISIBLE, ACTIVE._sort_column, ACTIVE._sort_dir).tolist()


def cycle_sort(column):
    """Tri-state per column: ascending -> descending -> unsorted, mirroring the
    WinForms browser's column-header click behaviour."""
    if ACTIVE._sort_column != column:
        ACTIVE._sort_column, ACTIVE._sort_dir = column, 1
    else:
        ACTIVE._sort_dir = (ACTIVE._sort_dir + 1) % 3
    _apply_sort()


def sort_state():
    return ACTIVE._sort_column, ACTIVE._sort_dir


def display_window():
    """Up to DISPLAY_CAP filtered/sorted rows, ready for the host to materialize
    into whatever its UI shows. Returns
    (total_visible_count, [(rows_index, row_view), ...]).

    The cap is the entire point: a search that matches 200k rows still hands
    back 500. The count comes back separately so the UI can say so honestly."""
    capped = ACTIVE.VISIBLE[:DISPLAY_CAP]
    return len(ACTIVE.VISIBLE), [(i, ACTIVE.ROWS[i]) for i in capped]
