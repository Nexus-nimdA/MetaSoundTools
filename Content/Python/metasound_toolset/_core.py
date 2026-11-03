"""Shared plumbing for the MetaSound toolset.

Everything here enforces the house design laws:

* raise standard Python exceptions, never return status;
* no code path should open a modal dialog (paths are validated before any
  engine write, saves go through silent APIs); the exceptions are two
  engine steps on a source-controlled project, named in the tool docs of
  build_to_asset and create_attenuation_asset. The open-editor write guard
  is best-effort - stock 5.8 Python exposes no open-editor accessor, so it
  cannot actually refuse (see ensure_asset_not_open_in_editor);
* read-back parity helpers so every setter has a matching getter.

Session state (named builders, audition sessions, node-handle tracking) is
module-level and therefore scoped to the editor's Python session. Handles
are never serialized; agents refer to nodes by the opaque id strings these
helpers hand out.
"""

import itertools
import os
import uuid
from dataclasses import dataclass, field

import unreal

# ---------------------------------------------------------------------------
# Session state
# ---------------------------------------------------------------------------

# Builder names this toolset registered with UMetaSoundBuilderSubsystem.
# The subsystem has FindBuilder but no enumeration, so we keep the roster.
# Like every builder-keyed dict below, it is keyed by builder_key(): the
# engine's find_builder is an FName lookup and FName comparison is
# case-insensitive, so 'Edit' and 'edit' address ONE live builder and must
# share one node-id namespace, class map and edge ledger (case-sensitive
# keys minted a second 'n1' for the alias, and remove_node could then
# remove the wrong live node). The fold is FName's, ASCII case only, not
# str.casefold: 'Äther' and 'äther' are TWO live builders, and one key for
# both let the second attachment overwrite the first's record, so the cycle
# guard asked about the wrong asset and the editor died (live 2026-11-02,
# cold read C1). The key is taken from the engine's own FName of the name,
# so number-suffix spellings the engine merges merge here too (R1). Values
# keep the registered spelling.
_builder_names: dict[str, str] = {}

# builder name -> {node id string -> unreal.MetaSoundNodeHandle}
# Handle structs must never be serialized (engine rule); agents get readable
# session-counter id strings ('n1', 'n2', ...) and we map them back to the
# live structs here.
_node_handles: dict[str, dict[str, unreal.MetaSoundNodeHandle]] = {}

# builder name -> [node id string] in creation order (used for auto-layout).
_node_order: dict[str, list[str]] = {}

# builder name -> next session-counter id number (ids never reused within a
# builder, so a removed node's id cannot silently alias a later node).
_node_counters: dict[str, int] = {}

# builder name -> {handle key -> node id} so re-registering the same live
# handle hands back the same id instead of minting a duplicate.
_handle_ids: dict[str, dict[str, str]] = {}

# builder name -> {node id -> class display string} for graph read-back
# ('UE.Wave Player.Stereo', or 'asset:/Game/...' for asset-referencing
# nodes); session-tracked nodes only.
_node_classes: dict[str, dict[str, str]] = {}

# builder name -> [edge dict] in creation order: the wiring made through
# this toolset this session. Each edge is {'from_node_id', 'from_output',
# 'to_node_id', 'to_input'}; a graph input edge has from_node_id '' and the
# graph input's name as from_output, a graph output edge has to_node_id ''
# and the graph output's name as to_input. The engine's Builder API cannot
# enumerate existing connections in 5.8, so this session ledger is the
# wiring read-back. Pin and member names are recorded as the engine reads
# them back, and matched case-insensitively on cleanup (_same_name): they
# are FNames to the engine, so a disconnect or member removal spelled with
# different case succeeds there and must forget the recorded edge too.
_edges: dict[str, list[dict]] = {}

# builder key -> package path of the saved asset the builder is attached
# to (attach_builder_to_asset); absent for builders created fresh, whose
# MetaSounds stay transient until build_to_asset. Keyed by builder_key()
# because the engine's find_builder is an FName lookup and FName
# comparison is case-insensitive: a case-drifted builder name resolves the
# live builder, so the attachment record must match it too (a
# case-sensitive dict here let 'edithit' bypass the self-overwrite guard
# on 'EditHit').
_attached_assets: dict[str, str] = {}

# folded host package -> {edge serial -> package added to the host as a
# node this session}. Kept apart from the builder records on purpose:
# release_builder forgets those, but an unsaved node stays in the asset's
# in-memory document, and the cycle check has to keep seeing that edge.
# Entries leave three ways: remove_node drops its own, a successful
# save_builder_asset drops the host's (the registry then knows the saved
# edges), and build_to_asset drops the host's (the document is replaced).
# Without those exits a legitimate graph stayed refused for the rest of
# the session (cold read C3).
#
# Edges are keyed by a serial that is never reused, not by (builder, node
# id): node ids restart at 'n1' when a released name is reused, so a new
# incarnation's remove_node erased an older one's retained edge on ANOTHER
# host, and a reattach assigned straight over it (verify-cold R2).
_session_asset_edges: dict[str, dict[int, str]] = {}

# builder key -> {node id -> edge serial} for the asset nodes this builder
# added since it was registered: the only edges its remove_node may forget.
# Dropped with the builder's other records, so a later builder under the
# same name owns none of them.
_node_asset_edges: dict[str, dict[str, int]] = {}

# Source of edge serials; never reset in a session.
_edge_serials = itertools.count(1)

# Same key -> the host package as it was spelled, for messages.
_session_host_names: dict[str, str] = {}

# Same key -> the attached asset object. The path above goes stale on a
# rename and can name a replacement after a delete; the object says whether
# the document this builder edits still exists at all.
_attached_objects: dict[str, unreal.Object] = {}


@dataclass
class AuditionSession:
    """One live audition: an audio component playing a (built) MetaSound."""
    name: str
    component: unreal.AudioComponent
    source_description: str  # builder name or asset path it was started from
    live_updates: bool
    # Read-back for parameter setters: param name -> last value set by us.
    # AudioComponent instance parameters are write-oriented, so the toolset
    # reports what IT set; that is stated in the tool docs.
    params_set: dict[str, object] = field(default_factory=dict)


_auditions: dict[str, AuditionSession] = {}


# ---------------------------------------------------------------------------
# Result / lookup guards
# ---------------------------------------------------------------------------

def check_result(result: unreal.MetaSoundBuilderResult, what: str) -> None:
    """Raise RuntimeError if a builder operation reported failure.

    Args:
        result: The EMetaSoundBuilderResult out-value of a builder call.
        what: Human-readable description of the attempted operation.
    """
    if result != unreal.MetaSoundBuilderResult.SUCCEEDED:
        raise RuntimeError(f'MetaSound builder operation failed: {what}')


def builder_subsystem() -> unreal.MetaSoundBuilderSubsystem:
    subsystem = unreal.get_engine_subsystem(unreal.MetaSoundBuilderSubsystem)
    if subsystem is None:
        raise RuntimeError('MetaSoundBuilderSubsystem is unavailable.')
    return subsystem


def editor_subsystem() -> unreal.MetaSoundEditorSubsystem:
    subsystem = unreal.get_editor_subsystem(unreal.MetaSoundEditorSubsystem)
    if subsystem is None:
        raise RuntimeError('MetaSoundEditorSubsystem is unavailable.')
    return subsystem


def object_alive(obj) -> bool:
    """True if a Python wrapper still points at a live engine object.

    A destroyed object's stale wrapper refuses to nativize into ANY
    reflected call, SystemLibrary.is_valid included (live-verified
    2026-10-23), so the check has to tolerate that failure.
    """
    if obj is None:
        return False
    try:
        return bool(unreal.SystemLibrary.is_valid(obj))
    except Exception:
        return False


def named_builder(builder_name: str) -> unreal.MetaSoundBuilderBase:
    """Return the named builder or raise LookupError, live or not.

    Only for callers that never read the builder's document. Everything
    else goes through get_builder.
    """
    builder_key(builder_name)  # refuses empty and NAME_None names
    builder = builder_subsystem().find_builder(builder_name)
    if builder is None:
        known = ', '.join(list_tracked_builders()) or '(none)'
        raise LookupError(
            f"No MetaSound builder named '{builder_name}'. "
            f'Builders created this session: {known}.')
    return builder


def get_builder(builder_name: str) -> unreal.MetaSoundBuilderBase:
    """Return the named builder if its document can be read, else raise.

    Deleting an attached asset ends its builder's document but leaves the
    builder findable, and the next read of it is a fatal engine assert
    (live-verified 2026-11-02: 'Assertion failed: Interface'). The subsystem
    has no validity query, so the attached object is what tells.

    Args:
        builder_name: Name the builder was registered under.

    Raises:
        LookupError: if no builder has that name.
        RuntimeError: if the asset the builder was attached to is gone.
    """
    builder = named_builder(builder_name)
    key = builder_key(builder_name)
    if key in _attached_objects and not object_alive(_attached_objects[key]):
        raise RuntimeError(
            f"Builder '{builder_name}' was attached to "
            f"'{_attached_assets.get(key)}', and that asset has been deleted "
            'or replaced, so the builder has no document left. Call '
            'release_builder on it, then attach again.')
    return builder


def get_source_builder(builder_name: str) -> unreal.MetaSoundSourceBuilder:
    """Return the named builder if it is a source builder, else raise."""
    builder = get_builder(builder_name)
    if not isinstance(builder, unreal.MetaSoundSourceBuilder):
        raise TypeError(
            f"Builder '{builder_name}' is a patch builder; this operation "
            'needs a source builder (sources are playable, patches are not).')
    return builder


_ASCII_LOWER = str.maketrans(
    'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz')


def fold_name(text: str) -> str:
    """Fold a string the way FName compares it: ASCII letters only.

    FName hashes names through TChar::ToLower, which converts the 26 ASCII
    letters and nothing else (Char.h, 5.8). str.casefold folds far more,
    and every pair it merges that the engine keeps apart is a live
    aliasing bug: 'äx'/'Äx', 'év'/'Év', 'ßy'/'ssy', 'ſz'/'sz' and the
    Kelvin sign against 'k' were all two engine builders under one
    session key (cold read C1, fired). Use this for anything that mirrors
    an FName: builder names, package paths, pin and member names.
    """
    return text.translate(_ASCII_LOWER)


def builder_key(builder_name: str) -> str:
    """Canonical session-state key for a builder name: its engine FName.

    The name goes through unreal.Name and back before it is folded, so the
    key is what find_builder actually looks up, not the caller's spelling.
    The fold alone missed the engine's number parsing: FName strips a
    trailing '_<digits>' and, when nothing is left, makes NAME_None, which
    is also what 'None' in any casing names (NameTypes.h, UnrealNames.cpp,
    5.8.4). So '_0', '_1' and 'none' were one engine builder under three
    session keys, and every guard that reads the attachment records skipped
    the untracked alias (verify-cold R1).

    Raises:
        ValueError: if the name is empty, or the engine reads it as
            NAME_None. Every lookup and creation goes through here, so no
            tool can reach the nameless builder slot.
    """
    if not builder_name:
        raise ValueError('builder_name must not be empty.')
    engine_name = unreal.Name(builder_name)
    # Headless-probed 2026-11-03 (5.8.4): '_0', '_1', '_12', 'None', 'none'
    # and '' all find one builder registered under '_0'; '_00', 'None_0'
    # and 'Foo_01' keep their own names.
    if engine_name.is_none():
        raise ValueError(
            f"Builder name '{builder_name}' is no name to the engine: it "
            "drops a trailing '_<number>' and reads what is left, and "
            "'None' in any casing, as the empty name, so '_0', '_1' and "
            "'None' all address one nameless builder. Use a name with "
            "letters, such as 'Edit_1'.")
    return fold_name(str(engine_name))


def track_builder(builder_name: str) -> None:
    key = builder_key(builder_name)
    _builder_names.setdefault(key, builder_name)
    _node_handles.setdefault(key, {})
    _node_order.setdefault(key, [])
    _node_counters.setdefault(key, 0)
    _handle_ids.setdefault(key, {})
    _node_classes.setdefault(key, {})
    _edges.setdefault(key, [])


def untrack_builder(builder_name: str) -> None:
    key = builder_key(builder_name)
    _builder_names.pop(key, None)
    _node_handles.pop(key, None)
    _node_order.pop(key, None)
    _node_counters.pop(key, None)
    _handle_ids.pop(key, None)
    _node_classes.pop(key, None)
    _edges.pop(key, None)
    _attached_assets.pop(key, None)
    _attached_objects.pop(key, None)
    # Its retained host edges stay in _session_asset_edges; only the right
    # to forget them through remove_node goes with the builder.
    _node_asset_edges.pop(key, None)


def track_attachment(builder_name: str, package_path: str,
                     asset: unreal.Object | None = None) -> None:
    """Record which saved asset a builder is attached to."""
    key = builder_key(builder_name)
    _attached_assets[key] = package_path
    if asset is not None:
        _attached_objects[key] = asset


def attached_package_now(builder_name: str) -> str | None:
    """The attached asset's current package, following any rename."""
    asset = _attached_objects.get(builder_key(builder_name))
    if object_alive(asset):
        return asset.get_path_name().split('.')[0]
    return attached_asset_path(builder_name)


def attached_asset_path(builder_name: str) -> str | None:
    """Package path the builder is attached to, or None for transient.

    Case-insensitive on the builder name, matching the engine's FName
    lookup semantics (find_builder resolves case-drifted names).
    """
    return _attached_assets.get(builder_key(builder_name))


def list_tracked_builders() -> list[str]:
    return sorted(_builder_names.values())


# ---------------------------------------------------------------------------
# Node handle tracking (opaque ids over the wire, live structs in session)
# ---------------------------------------------------------------------------

def _handle_key(handle: unreal.MetaSoundNodeHandle) -> str:
    """Derive a session-stable dedup key for a node handle (NodeID guid)."""
    try:
        return str(handle.get_editor_property('node_id'))
    except Exception:
        # Bare UPROPERTY visibility can vary across reflection paths; the
        # struct's string form is an opaque memory-address id (live-verified
        # 2026-10-23), unique for this session only because the registry
        # keeps the handle struct alive. It is NEVER content-stable and is
        # never shown to agents - they get the 'n<counter>' ids below.
        return str(handle)


def register_node_handle(
        builder_name: str, handle: unreal.MetaSoundNodeHandle,
        class_display: str = '') -> str:
    """Store a node handle for the session; returns its node id string.

    Ids are per-builder session counters ('n1', 'n2', ...): readable,
    persistable within the editor session, and never reused within a
    builder (a removed node's id cannot alias a later node). Registering
    the same live handle again returns its existing id.
    """
    bkey = builder_key(builder_name)
    key = _handle_key(handle)
    ids = _handle_ids.setdefault(bkey, {})
    node_id = ids.get(key)
    if node_id is None:
        count = _node_counters.get(bkey, 0) + 1
        _node_counters[bkey] = count
        node_id = f'n{count}'
        ids[key] = node_id
    _node_handles.setdefault(bkey, {})[node_id] = handle
    order = _node_order.setdefault(bkey, [])
    if node_id not in order:
        order.append(node_id)
    if class_display:
        _node_classes.setdefault(bkey, {})[node_id] = class_display
    return node_id


def resolve_node_handle(
        builder_name: str, node_id: str) -> unreal.MetaSoundNodeHandle:
    """Map an opaque node id string back to its live handle or raise."""
    get_builder(builder_name)  # raises LookupError on unknown builder
    handle = _node_handles.get(builder_key(builder_name), {}).get(node_id)
    if handle is None:
        raise LookupError(
            f"Unknown node id '{node_id}' on builder '{builder_name}'. "
            'Node ids are session-scoped: use the id returned by add_node / '
            'add_node_from_asset in this editor session.')
    return handle


def forget_node_handle(builder_name: str, node_id: str) -> None:
    bkey = builder_key(builder_name)
    handles = _node_handles.get(bkey, {})
    handle = handles.pop(node_id, None)
    if handle is not None:
        _handle_ids.get(bkey, {}).pop(_handle_key(handle), None)
    _node_classes.get(bkey, {}).pop(node_id, None)
    order = _node_order.get(bkey)
    if order and node_id in order:
        order.remove(node_id)
    forget_node_edges(builder_name, node_id)
    forget_node_asset_reference(builder_name, node_id)


def tracked_node_ids(builder_name: str) -> list[str]:
    return list(_node_order.get(builder_key(builder_name), []))


def node_id_for_handle(builder_name: str,
                       handle: unreal.MetaSoundNodeHandle) -> str:
    """The session node id a handle was registered under, '' if untracked.

    Untracked handles are the graph-member nodes (graph inputs/outputs)
    whose pins come back from builder creation - edges touching those are
    recorded with the empty node id and the member's name as the pin.
    """
    ids = _handle_ids.get(builder_key(builder_name), {})
    return ids.get(_handle_key(handle), '')


def tracked_node_classes(builder_name: str) -> dict[str, str]:
    """node id -> class display string for session-tracked nodes."""
    return dict(_node_classes.get(builder_key(builder_name), {}))


# ---------------------------------------------------------------------------
# Edge ledger (wiring read-back; 5.8's Builder API cannot enumerate edges)
# ---------------------------------------------------------------------------

def _same_name(a: str, b: str) -> bool:
    """FName equality for pin/member names (ASCII case-insensitive)."""
    return fold_name(a) == fold_name(b)


def record_edge(builder_name: str, from_node_id: str, from_output: str,
                to_node_id: str, to_input: str) -> None:
    """Record a connection made through this toolset.

    A MetaSound input pin holds at most one connection, so a new edge into
    the same destination replaces the previously recorded one.
    """
    edges = _edges.setdefault(builder_key(builder_name), [])
    edges[:] = [e for e in edges if not (
        e['to_node_id'] == to_node_id
        and _same_name(e['to_input'], to_input))]
    edges.append({
        'from_node_id': from_node_id, 'from_output': from_output,
        'to_node_id': to_node_id, 'to_input': to_input})


def forget_edge(builder_name: str, from_node_id: str, from_output: str,
                to_node_id: str, to_input: str) -> None:
    edges = _edges.get(builder_key(builder_name))
    if edges:
        edges[:] = [e for e in edges if not (
            e['from_node_id'] == from_node_id
            and _same_name(e['from_output'], from_output)
            and e['to_node_id'] == to_node_id
            and _same_name(e['to_input'], to_input))]


def forget_node_edges(builder_name: str, node_id: str) -> None:
    """Drop every recorded edge touching a node (node removal)."""
    edges = _edges.get(builder_key(builder_name))
    if edges and node_id:
        edges[:] = [e for e in edges if node_id not in (
            e['from_node_id'], e['to_node_id'])]


def forget_graph_member_edges(builder_name: str, member_name: str,
                              is_input: bool) -> None:
    """Drop edges touching a removed graph input/output member."""
    edges = _edges.get(builder_key(builder_name))
    if not edges:
        return
    if is_input:
        edges[:] = [e for e in edges if not (
            e['from_node_id'] == ''
            and _same_name(e['from_output'], member_name))]
    else:
        edges[:] = [e for e in edges if not (
            e['to_node_id'] == ''
            and _same_name(e['to_input'], member_name))]


def prune_missing_member_edges(builder_name: str, input_names: list[str],
                               output_names: list[str]) -> None:
    """Drop edges whose graph input/output member no longer exists.

    For engine operations that remove members wholesale without naming
    them (interface removal): the ledger reconciles against the
    builder's current member names, read back by the caller.
    """
    edges = _edges.get(builder_key(builder_name))
    if not edges:
        return
    inputs = {fold_name(name) for name in input_names}
    outputs = {fold_name(name) for name in output_names}
    edges[:] = [e for e in edges if not (
        (e['from_node_id'] == ''
         and fold_name(e['from_output']) not in inputs)
        or (e['to_node_id'] == ''
            and fold_name(e['to_input']) not in outputs))]


def tracked_edges(builder_name: str) -> list[dict]:
    return [dict(e) for e in _edges.get(builder_key(builder_name), [])]


# ---------------------------------------------------------------------------
# Audition session registry
# ---------------------------------------------------------------------------

# Components whose audition session was forgotten while an asynchronous
# fade-out was still playing (stop_audition / finish_source_render with a
# fade). They are unreachable by name, so stop_all_auditions hard-stops
# them from here; entries clear on every sweep.
_fading_components: list = []


def note_fading_component(component) -> None:
    _fading_components.append(component)


def sweep_fading_components() -> int:
    """Hard-stop every component still fading out; returns the live count.

    A destroyed component's stale wrapper refuses to nativize into any
    reflected call (the _component_alive law), so liveness tolerates the
    TypeError and dead wrappers just drop off the list.
    """
    stopped = 0
    for component in _fading_components:
        try:
            if component is not None and unreal.SystemLibrary.is_valid(
                    component):
                component.stop()
                stopped += 1
        except Exception:
            pass
    _fading_components.clear()
    return stopped


def register_audition(session: AuditionSession) -> None:
    _auditions[session.name] = session


def get_audition(audition_name: str) -> AuditionSession:
    session = _auditions.get(audition_name)
    if session is None:
        known = ', '.join(sorted(_auditions)) or '(none)'
        raise LookupError(
            f"No audition named '{audition_name}'. Active or recently "
            f'started auditions: {known}.')
    return session


def forget_audition(audition_name: str) -> None:
    _auditions.pop(audition_name, None)


def list_audition_names() -> list[str]:
    return sorted(_auditions)


# ---------------------------------------------------------------------------
# Asset path validation (pre-create, so no engine write can hit a dialog)
# ---------------------------------------------------------------------------

# Epic's own invalid sets (PackageName.cpp, pinned to 5.8): characters a
# long package path / object name must not contain. Control characters
# are rejected separately (anything below 0x20). The object-name set
# additionally keeps the Windows-filename reserved characters the
# original validator already refused (\\ ? * < >) - an asset name
# becomes part of a filename, and weakening the check buys nothing.
_INVALID_LONGPACKAGE_CHARS = '\\:*?"<>|\' ,.&!~\n\r\t@#'
_INVALID_OBJECTNAME_CHARS = '"\' ,/.:|&!~\n\r\t@#(){}[]=;^%$`\\?*<>'

#: Names the engine refuses as a file or folder (FileHelper.cpp, pinned to
#: 5.8: the Windows device names plus NONE, which FName reads as NAME_None).
#: Compared case-insensitively. Passing validation with one of these only
#: fails later, inside a save, with the asset half made.
RESERVED_FILE_NAMES = frozenset({
    'CON', 'PRN', 'AUX', 'CLOCK$', 'NUL', 'NONE',
    *(f'COM{n}' for n in range(1, 10)),
    *(f'LPT{n}' for n in range(1, 10)),
})


def _fold_root(root: str) -> str:
    """Fold a mount root the way FName compares: ASCII case only.

    str.casefold also maps characters like the long s onto plain letters,
    which the engine does not, so '/Meta\u017found' would pass as a mount and
    then fail inside an engine write, on a dialog.
    """
    return fold_name(root)


def _mounted_content_roots() -> set[str]:
    """Folded top-level content mounts known to the asset registry.

    get_sub_paths('/') enumerates the mounted roots quietly ('/Game',
    '/Engine', every enabled plugin; headless-probed 2026-10-24) - a path
    under no mounted root cannot be created silently, so it must fail
    here, in validation, not inside an engine write.
    """
    registry = unreal.AssetRegistryHelpers.get_asset_registry()
    return {_fold_root(str(p))
            for p in (registry.get_sub_paths('/', False) or [])}


def validate_content_path(package_path: str) -> None:
    """Validate a long package path like '/Game/Audio' before any write.

    Raises:
        ValueError: if the path is not a rooted content path, contains a
            character from Epic's invalid set, or sits under no mounted
            content root.
    """
    # '//' is refused outright: the segment split below strips it, the
    # filename mapping fails on it, and the engine then collapses it, so
    # '//Engine/X' would pass every check here and land in /Engine/X.
    if (not package_path or not package_path.startswith('/')
            or '//' in package_path):
        raise ValueError(
            f"Package path '{package_path}' must be a rooted content path "
            "such as '/Game/Audio'.")
    for part in package_path.strip('/').split('/'):
        if (not part
                or any(c in part for c in _INVALID_LONGPACKAGE_CHARS)
                or any(ord(c) < 32 for c in part)
                or part.upper() in RESERVED_FILE_NAMES):
            raise ValueError(
                f"Package path '{package_path}' contains an invalid "
                f"segment '{part}' (characters "
                f'{_INVALID_LONGPACKAGE_CHARS!r}, control characters and '
                'reserved file names such as CON or NONE are not allowed '
                'in package paths).')
    root = '/' + package_path.strip('/').split('/')[0]
    if _fold_root(root) not in _mounted_content_roots():
        raise ValueError(
            f"Package path '{package_path}' is under no mounted content "
            f"root: '{root}' is not a registered mount. Project content "
            "goes under '/Game'; plugin content under the plugin's own "
            'mounted root.')


def validate_asset_name(asset_name: str) -> None:
    if (not asset_name
            or any(c in asset_name for c in _INVALID_OBJECTNAME_CHARS)
            or any(ord(c) < 32 for c in asset_name)
            or asset_name.upper() in RESERVED_FILE_NAMES):
        raise ValueError(
            f"Asset name '{asset_name}' is invalid: characters "
            f'{_INVALID_OBJECTNAME_CHARS!r}, control characters and reserved '
            'file names such as CON or NONE are not allowed in object names '
            "(Epic's invalid set).")


def package_under_path(package: str, path_filter: str) -> bool:
    """True when a package equals the filter path or sits under it.

    Boundary-aware prefix matching: '/Game/Audio' matches '/Game/Audio'
    and '/Game/Audio/Sub' but not '/Game/AudioOld' (a bare startswith
    would sweep in sibling folders that merely share the spelling).
    """
    root = path_filter.rstrip('/')
    return package == root or package.startswith(root + '/')


def ensure_not_engine_content(asset_path: str) -> None:
    """Refuse a write that would land in the engine install.

    /Engine and engine-plugin mounts such as /Metasound are writable to the
    editor, and the templates build on assets that live there, so a tool
    write could rewrite content every project on this engine shares. Reads
    stay allowed. Call this after the path is known to be mounted.

    Raises:
        PermissionError: if the package maps under the engine directory.
    """
    package_name = asset_path.split('.')[0]
    filename = str(unreal.PackageTools.package_name_to_filename(package_name))
    if not filename:
        raise PermissionError(
            f"'{asset_path}' maps to no content folder, so this toolset "
            'cannot confirm the write stays out of engine content.')
    target = os.path.normcase(os.path.realpath(
        unreal.Paths.convert_relative_path_to_full(filename)))
    engine = os.path.normcase(os.path.realpath(
        unreal.Paths.convert_relative_path_to_full(unreal.Paths.engine_dir())))
    if target == engine or target.startswith(engine + os.sep):
        raise PermissionError(
            f"'{asset_path}' is engine content; this toolset writes project "
            'and project-plugin content only. Build to a path under /Game '
            'or a project plugin instead.')


def package_asset_classes(asset_path: str) -> list[str]:
    """Class names of the assets the registry knows in a package."""
    registry = unreal.AssetRegistryHelpers.get_asset_registry()
    assets = registry.get_assets_by_package_name(asset_path.split('.')[0])
    return sorted(str(a.asset_class_path.asset_name) for a in assets or [])


def _asset_data_quiet(asset_path: str) -> unreal.AssetData:
    """Asset registry lookup that logs nothing on a miss.

    EditorAssetLibrary logs an engine Error for missing assets, and error
    logs fail automation tests; raise paths must stay log-silent
    (live-verified 2026-10-23).
    """
    object_path = asset_path if '.' in asset_path else (
        asset_path + '.' + asset_path.rstrip('/').rsplit('/', 1)[-1])
    registry = unreal.AssetRegistryHelpers.get_asset_registry()
    # 5.8's Python binding of GetAssetByObjectPath takes the (deprecated
    # FName) path overload; a SoftObjectPath fails to nativize
    # (live-verified 2026-10-23).
    return registry.get_asset_by_object_path(object_path)


def asset_exists_quiet(asset_path: str) -> bool:
    """True if an asset exists at the path, without error logs on a miss."""
    return _asset_data_quiet(asset_path).is_valid()


def package_file_on_disk(package_path: str) -> str | None:
    """Absolute package filename if the package exists ON DISK, else None.

    The registry can lag an external file write (a source-control sync
    racing the call), so a registry miss alone never proves a path free.
    PackageTools maps the package name through the live mount table and
    returns '' for an unmounted path, which reads as not-on-disk.
    """
    package_name = package_path.split('.')[0]
    base = str(unreal.PackageTools.package_name_to_filename(package_name))
    if not base:
        return None
    for extension in ('.uasset', '.umap'):
        candidate = str(unreal.Paths.convert_relative_path_to_full(
            base + extension))
        if os.path.isfile(candidate):
            return candidate
    return None


def asset_exists_checked(asset_path: str) -> bool:
    """Existence check that registry lag cannot fool (the modal shield).

    A `.uasset` on disk that the registry has not indexed yet (external
    write racing the call) would otherwise slip past every overwrite
    protection and walk BuildToAsset/create_asset into their
    existing-object confirmation path - the live-verified modal trap. On
    a registry miss with the file on disk, the folder is rescanned
    (bounded, the same primitive the referencer guard uses) and the
    registry asked again; if it still refuses to see the file, this
    raises rather than letting an engine write find out.
    """
    if asset_exists_quiet(asset_path):
        return True
    on_disk = package_file_on_disk(asset_path)
    if on_disk is None:
        return False
    package_name = asset_path.split('.')[0]
    folder = package_name.rsplit('/', 1)[0]
    registry = unreal.AssetRegistryHelpers.get_asset_registry()
    registry.scan_paths_synchronous([folder], force_rescan=True)
    # Package-level re-query, not object-path: a copied/synced package
    # keeps its ORIGINAL inner object name (headless-verified 2026-10-24:
    # a file-copied MetaSound indexed as <NewPackage>.<OldAssetName>), so
    # the object-path guess can miss an occupied package.
    if registry.get_assets_by_package_name(package_name):
        return True
    raise RuntimeError(
        f"The asset registry does not know '{asset_path}' but its package "
        f"file exists on disk ('{on_disk}'), even after a rescan. Writing "
        'there could hit the engine\'s existing-asset confirmation dialog. '
        'Resolve the file (delete it, or let the editor finish importing/'
        'syncing) and retry, or pick another name.')


#: Registry class names of the two MetaSound asset types.
METASOUND_CLASSES = frozenset({'MetaSoundSource', 'MetaSoundPatch'})

#: Longest list a tool accepts (wave lists, array literals). Each item
#: loads an asset or becomes a literal element on the game thread, and
#: nothing bounded the count (security pass S21).
MAX_LIST_ITEMS = 256


def ensure_list_bounded(name: str, items: list) -> None:
    if len(items) > MAX_LIST_ITEMS:
        raise ValueError(
            f'{name} holds {len(items)} items; at most {MAX_LIST_ITEMS} are '
            'accepted in one call.')


def ensure_asset_class(asset_path: str, allowed: frozenset[str],
                       what: str) -> unreal.AssetData:
    """Refuse an asset of the wrong class from registry data, before loading.

    The class check after a load runs after the load: describe_metasound on
    a level loaded the level before refusing it (security pass S21). The
    registry knows the class without loading anything.

    Raises:
        FileNotFoundError: no asset at the path.
        TypeError: the asset's class is not one of allowed.
    """
    asset_data = ensure_asset_exists(asset_path)
    class_name = str(asset_data.asset_class_path.asset_name)
    if class_name not in allowed:
        raise TypeError(
            f"Asset '{asset_path}' is a {class_name}, not a {what}.")
    return asset_data


def ensure_asset_exists(asset_path: str) -> unreal.AssetData:
    """Return AssetData for an existing asset or raise FileNotFoundError."""
    if not asset_path or not asset_path.startswith('/'):
        raise ValueError(
            f"Asset path '{asset_path}' must be a rooted object path such "
            "as '/Game/Audio/MySound'.")
    asset_data = _asset_data_quiet(asset_path)
    if not asset_data.is_valid():
        raise FileNotFoundError(f"No asset found at '{asset_path}'.")
    return asset_data


# Content folders this session saved assets into. The registry's
# dependency graph only learns a saved package's dependencies from a DISK
# SCAN - startup, or the directory watcher, which needs game-thread ticks
# a tool call never yields - so a same-session save is invisible to
# get_referencers until these folders are rescanned (headless-verified
# 2026-10-24: get_dependencies/get_referencers return None for a freshly
# saved package; scan_paths_synchronous(force_rescan=True) populates them
# in-call).
_saved_content_folders: set[str] = set()


def note_saved_package(package_path: str) -> None:
    """Record a toolset-saved package so referencer checks can rescan it."""
    folder = package_path.split('.')[0].rsplit('/', 1)[0]
    if folder:
        _saved_content_folders.add(folder)


def _hard_reference_options() -> unreal.AssetRegistryDependencyOptions:
    return unreal.AssetRegistryDependencyOptions(
        include_soft_package_references=False,
        include_hard_package_references=True,
        include_searchable_names=False,
        include_soft_management_references=False,
        include_hard_management_references=False)


def _rescan_session_folders(extra_folder: str) -> None:
    """Rescan this session's output folders so its saves count as saved.

    has_assets filters out folders whose mount is gone, quietly.
    """
    registry = unreal.AssetRegistryHelpers.get_asset_registry()
    folders = [
        folder for folder in sorted(_saved_content_folders | {extra_folder})
        if registry.has_assets(folder, True)]
    if folders:
        registry.scan_paths_synchronous(folders, force_rescan=True)


def saved_referencer_packages(package_path: str) -> list[str]:
    """Saved packages that hard-reference the asset, loaded or not.

    The overwrite path can only clear an existing asset with a force
    delete (the one path-clearing primitive inside a tick-less tool call,
    headless-verified 2026-10-24), and that hurts referencers two ways. A
    force delete nulls every loaded hard reference to the old object. And
    a rebuilt MetaSound gets a NEW class identity (live-verified 2026-11-02:
    the root class name changes across an overwrite), while a MetaSound
    binds its nodes by class, not path, so a parent on disk no longer finds
    its node on its next load. Rebuilding at the same path repairs neither.

    The asset registry only knows saved, scanned referencers: a referencer
    that has never been saved to disk is invisible here - callers' tool docs
    must say so.
    """
    package_name = package_path.split('.')[0]
    registry = unreal.AssetRegistryHelpers.get_asset_registry()
    _rescan_session_folders(package_name.rsplit('/', 1)[0])
    referencers = registry.get_referencers(
        package_name, _hard_reference_options()) or []
    return sorted(str(package) for package in referencers
                  if str(package) != package_name)


def builders_using_asset(package_path: str) -> list[str]:
    """Session builders holding a node that references the asset."""
    wanted = fold_name(f"asset:{package_path.split('.')[0]}")
    return sorted(
        _builder_names.get(key, key) for key, classes in _node_classes.items()
        if any(fold_name(c) == wanted for c in classes.values()))


def note_asset_reference(host_package: str, reference: str,
                         builder_name: str, node_id: str) -> None:
    """Record an edge the registry cannot see until the host is saved."""
    key = fold_name(host_package)
    serial = next(_edge_serials)
    _session_asset_edges.setdefault(key, {})[serial] = reference
    _session_host_names.setdefault(key, host_package)
    _node_asset_edges.setdefault(builder_key(builder_name), {})[
        node_id] = serial


def forget_node_asset_reference(builder_name: str, node_id: str) -> None:
    """Drop the session edge a removed node carried, if it carried one.

    Only an edge this builder recorded for this node since it was
    registered: a serial is never shared, so no other host's edge can match.
    """
    serial = _node_asset_edges.get(builder_key(builder_name), {}).pop(
        node_id, None)
    if serial is None:
        return
    for host, edges in list(_session_asset_edges.items()):
        if edges.pop(serial, None) is not None:
            if not edges:
                del _session_asset_edges[host]
                _session_host_names.pop(host, None)
            return


def forget_asset_references(host_package: str) -> None:
    """Drop every session edge out of a host whose document is settled.

    Called when the host is saved, so the registry can see its edges, and
    when build_to_asset replaces it, so the old document's edges are gone.
    """
    key = fold_name(host_package)
    _session_asset_edges.pop(key, None)
    _session_host_names.pop(key, None)


def session_hosts_referencing(package_path: str) -> list[str]:
    """Hosts holding an unsaved node of the asset, by session edge.

    builders_using_asset sees live builders only; release_builder forgets
    those, and the node stays in the host's in-memory document (cold read
    C9).
    """
    wanted = fold_name(package_path.split('.')[0])
    return sorted(
        _session_host_names.get(host, host)
        for host, edges in _session_asset_edges.items()
        if any(fold_name(ref) == wanted for ref in edges.values()))


def find_reference_loop(host_package: str, new_reference: str) -> str | None:
    """How adding new_reference to host_package would close a cycle.

    The Builder API adds a self-reference without complaint, and the next
    registration recurses until the stack overflows and the editor dies
    (live-verified 2026-11-02: EXCEPTION_STACK_OVERFLOW). The editor's own
    loop check is not exposed, so this walks hard dependencies from the new
    reference, plus edges made this session that are not saved yet.

    Returns:
        None when no cycle would form; 'saved' when the walk closed it
        through the asset registry alone; 'session' when at least one hop
        was an unsaved edge made through this toolset, which the caller
        should say, because only the session remembers it.
    """
    registry = unreal.AssetRegistryHelpers.get_asset_registry()
    _rescan_session_folders(host_package.rsplit('/', 1)[0])
    options = _hard_reference_options()
    goal = fold_name(host_package)
    seen: set[str] = set()
    pending: list[tuple[str, bool]] = [(new_reference, False)]
    while pending:
        package, via_session = pending.pop()
        folded = fold_name(package)
        if folded == goal:
            return 'session' if via_session else 'saved'
        if folded in seen or folded.startswith('/script/'):
            continue
        seen.add(folded)
        pending.extend(
            (str(d), via_session)
            for d in registry.get_dependencies(package, options) or [])
        pending.extend(
            (ref, True) for ref in _session_asset_edges.get(folded, {}).values())
    return None


def ensure_asset_not_open_in_editor(asset_path: str) -> None:
    """Best-effort refusal of writes while an asset editor holds the asset.

    Editing a document an open MetaSound editor is also mutating invites
    divergent state; Epic's own community toolsets treat this as the top
    hang/corruption trap. On stock 5.8 Python, AssetEditorSubsystem exposes
    no read accessor (live-verified 2026-10-23), so this guard degrades to
    a no-op there: callers' tool docs must tell the agent to close the
    asset's editor before writing. The check stays armed for engine
    versions whose Python binding exposes the accessor.
    """
    asset_editor = unreal.get_editor_subsystem(unreal.AssetEditorSubsystem)
    if asset_editor is None:
        return
    getter = getattr(asset_editor, 'get_all_edited_assets', None)
    if getter is None:
        # 5.8 Python does not expose AssetEditorSubsystem.GetAllEditedAssets
        # (live-verified 2026-10-23): the open-editor guard degrades to a
        # no-op rather than breaking every write path.
        return
    for opened in getter():
        if opened is not None and opened.get_path_name().split('.')[0] == \
                asset_path.split('.')[0]:
            raise RuntimeError(
                f"Asset '{asset_path}' is open in an asset editor. Close it "
                'before modifying the asset through the toolset (open '
                'editors and external writes diverge).')


def load_asset_checked(asset_path: str,
                       expected_class: type | None = None) -> unreal.Object:
    """Load an asset, optionally checking its class; raise on failure."""
    ensure_asset_exists(asset_path)
    asset = unreal.EditorAssetLibrary.load_asset(asset_path)
    if asset is None:
        raise RuntimeError(f"Failed to load asset '{asset_path}'.")
    # load_asset hands back the redirector object itself in 5.8
    # (FAssetData::GetAsset does not follow redirects, AssetData.h), so a
    # renamed asset's old path is a TypeError here, never a silent hop to
    # wherever it points (cold read C4). Not fired: no headless rename has
    # produced a redirector.
    redirector = getattr(unreal, 'ObjectRedirector', None)
    if redirector is not None and isinstance(asset, redirector):
        raise TypeError(
            f"'{asset_path}' is a redirector left by a rename or move, not "
            "the asset. Use the asset's current path, or fix up redirectors "
            'in the Content Browser.')
    if expected_class is not None and not isinstance(asset, expected_class):
        raise TypeError(
            f"Asset '{asset_path}' is a {type(asset).__name__}, expected "
            f'{expected_class.__name__}.')
    return asset


# ---------------------------------------------------------------------------
# Literal coercion (python values <-> FMetasoundFrontendLiteral)
# ---------------------------------------------------------------------------

#: Graph/node data types that take no settable default literal.
UNSETTABLE_DATA_TYPES = ('Audio',)


def make_literal(float_value: float | None = None,
                 int_value: int | None = None,
                 bool_value: bool | None = None,
                 string_value: str | None = None,
                 asset_path: str | None = None,
                 float_array: list[float] | None = None,
                 asset_paths: list[str] | None = None,
                 ) -> unreal.MetasoundFrontendLiteral:
    """Build a FMetasoundFrontendLiteral from exactly one typed value.

    Args:
        float_value: Float literal (data type 'Float').
        int_value: Integer literal ('Int32').
        bool_value: Boolean literal ('Bool').
        string_value: String literal ('String').
        asset_path: Object literal from an asset path (e.g. 'WaveAsset').
        float_array: Float array literal ('Float:Array').
        asset_paths: Object array literal (e.g. 'WaveAsset:Array').

    Raises:
        ValueError: if zero or more than one value is provided.
    """
    provided = [name for name, value in (
        ('float_value', float_value),
        ('int_value', int_value),
        ('bool_value', bool_value),
        ('string_value', string_value),
        ('asset_path', asset_path),
        ('float_array', float_array),
        ('asset_paths', asset_paths),
    ) if value is not None]
    if len(provided) != 1:
        raise ValueError(
            'Provide exactly one typed value for the literal; got '
            f'{provided or "none"}.')
    if float_array is not None:
        ensure_list_bounded('float_array', float_array)
    if asset_paths is not None:
        ensure_list_bounded('asset_paths', asset_paths)

    access = unreal.MetasoundFrontendLiteralBlueprintAccess
    if float_value is not None:
        return access.create_float_meta_sound_literal(float_value)
    if int_value is not None:
        return access.create_int_meta_sound_literal(int_value)
    if bool_value is not None:
        return access.create_bool_meta_sound_literal(bool_value)
    if string_value is not None:
        return access.create_string_meta_sound_literal(string_value)
    if asset_path is not None:
        return access.create_object_meta_sound_literal(
            load_asset_checked(asset_path))
    if float_array is not None:
        return access.create_float_array_meta_sound_literal(float_array)
    # asset_paths
    objects = [load_asset_checked(path) for path in asset_paths]
    return access.create_object_array_meta_sound_literal(objects)


def ensure_literal_fits(data_type: str,
                        literal: unreal.MetasoundFrontendLiteral) -> None:
    """Refuse a literal the data type cannot hold, using the engine's rule.

    A mismatched default set on a node pin or a new graph input is accepted
    and read back fine, then kills the editor with a fatal assert when the
    graph builds (live-verified 2026-11-02: a string on a Float or WaveAsset
    pin, 'Assertion failed: bExpectsNone'). The Builder API runs the
    data-type check only in set_graph_input_default, and the registry is
    not reachable from Python, so a throwaway patch with one input of the
    same type stands in for it.

    Raises:
        TypeError: if the data type does not accept the literal.
        RuntimeError: if the data type itself is not registered.
    """
    scratch_name = f'__msts_literal_{uuid.uuid4().hex[:12]}'
    subsystem = builder_subsystem()
    builder, result = subsystem.create_patch_builder(scratch_name)
    check_result(result, 'create scratch literal builder')
    subsystem.register_builder(scratch_name, builder)
    try:
        _, result = builder.add_graph_input_node(
            'Value', data_type, unreal.MetasoundFrontendLiteral(), False)
        check_result(result, f"check data type '{data_type}'")
        result = builder.set_graph_input_default('Value', literal)
        if result != unreal.MetaSoundBuilderResult.SUCCEEDED:
            kind = literal_to_dict(literal)['literal_type']
            raise TypeError(
                f"A {kind} value does not fit data type '{data_type}'. "
                'Provide the typed value that matches it (describe_node '
                'shows pin data types).')
    finally:
        subsystem.unregister_builder(scratch_name)


def literal_to_dict(literal: unreal.MetasoundFrontendLiteral) -> dict:
    """Read a FMetasoundFrontendLiteral back into plain python values.

    Returns:
        A dict with 'literal_type' plus the typed value field(s) that apply.
    """
    access = unreal.MetasoundFrontendLiteralBlueprintAccess
    literal_type = access.get_type(literal)
    # str() of a reflected enum value is '<Type.NAME: n>' (live-verified
    # 2026-10-23); prefer .name and fall back to parsing that form.
    type_name = getattr(literal_type, 'name', None) or (
        str(literal_type).split('.')[-1].split(':')[0].strip('< >'))
    out: dict = {'literal_type': str(type_name)}

    def _try(getter):
        value_result = getter(literal)
        if not isinstance(value_result, tuple) or len(value_result) != 2:
            # Binding-shape churn guard: an accessor that hands back no
            # usable (value, result) pair reads as a failed read instead
            # of crashing the tool with a raw unpack error (the Bool
            # accessor does exactly that in 5.8, see the BOOLEAN branch).
            return None
        value, result = value_result
        if result == unreal.MetaSoundBuilderResult.SUCCEEDED:
            return value
        return None

    def _member(*names):
        # 5.8 Python spellings (dir() probe, 2026-10-23): FLOAT, INTEGER,
        # BOOLEAN, STRING, NONE, U_OBJECT and the same + _ARRAY. Candidates
        # stay defensive against churn.
        for candidate in names:
            member = getattr(
                unreal.MetasoundFrontendLiteralType, candidate, None)
            if member is not None:
                return member
        return None

    if literal_type == _member('FLOAT'):
        out['float_value'] = _try(access.get_float_value_from_literal)
    elif literal_type == _member('INTEGER'):
        out['int_value'] = _try(access.get_int_value_from_literal)
    elif literal_type == _member('BOOLEAN'):
        # 5.8 binding gap (headless-verified 2026-10-24): the Python
        # binding of GetBoolValueFromLiteral exposes NO value channel -
        # it returns the bare result enum on success and None on failure,
        # while every other typed accessor returns (value, result) - so
        # the engine's own string form ('true'/'false') is the only Bool
        # read-back path.
        text = str(access.conv_meta_sound_literal_to_string(literal))
        lowered = text.strip().lower()
        if lowered in ('true', 'false'):
            out['bool_value'] = lowered == 'true'
        else:
            # String-form churn: expose what the engine said rather than
            # guessing a boolean out of it.
            out['string_value'] = text
    elif literal_type == _member('STRING'):
        out['string_value'] = _try(access.get_string_value_from_literal)
    elif literal_type == _member('FLOAT_ARRAY'):
        value = _try(access.get_float_array_value_from_literal)
        out['float_array'] = list(value) if value is not None else None
    elif literal_type == _member('U_OBJECT', 'UOBJECT', 'OBJECT'):
        obj = _try(access.get_object_value_from_literal)
        out['asset_path'] = obj.get_path_name() if obj else None
    elif literal_type == _member(
            'U_OBJECT_ARRAY', 'UOBJECT_ARRAY', 'OBJECT_ARRAY'):
        objs = _try(access.get_object_array_value_from_literal)
        out['asset_paths'] = (
            [o.get_path_name() for o in objs if o] if objs else None)
    else:
        # None/invalid or an array type without a dedicated accessor:
        # fall back to the engine's string form so nothing is hidden.
        out['string_value'] = access.conv_meta_sound_literal_to_string(
            literal)
    return out


# ---------------------------------------------------------------------------
# Editor world (for audition components)
# ---------------------------------------------------------------------------

def editor_world() -> unreal.World:
    subsystem = unreal.get_editor_subsystem(unreal.UnrealEditorSubsystem)
    if subsystem is None:
        raise RuntimeError('UnrealEditorSubsystem is unavailable.')
    world = subsystem.get_editor_world()
    if world is None:
        # During PIE the editor world is unavailable but a game world
        # exists (get_game_world returns None outside play, headless-
        # probed 2026-10-24) - say which situation this is instead of
        # the misleading bare 'no editor world' the drill hit.
        game_world = None
        try:
            game_world = subsystem.get_game_world()
        except Exception:
            pass
        if game_world is not None:
            raise RuntimeError(
                'PIE is running: auditions and output recordings drive '
                'the EDITOR world only on 5.8, and this toolset does not '
                'capture play mode. Stop PIE and retry (gameplay audio '
                'capture is a feature request, not a wiring bug).')
        raise RuntimeError('No editor world is available to audition in.')
    return world
