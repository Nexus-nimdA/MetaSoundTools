"""Rendered-audio capture: record the editor's audio output to WAV + analyze.

This is the read-back half of the audition loop: the agent can not only
*play* what it built (audition tools) but capture the result to a WAV on
disk and get objective measurements back (duration, peak dB, RMS dB,
silence check). The WAV itself is the artifact a human can play, and the
numbers are the agent's screenshot-verify.

Mechanics (live-probed 2026-10-23): ``unreal.AudioMixerLibrary``
``start_recording_output`` / ``stop_recording_output`` tap a submix (the
master output when no submix is given) and export a WAV file. The export
finalizes asynchronously off the game thread, so analysis tolerates a
short wait for the file.

House laws hold: everything validates before touching the engine, raises
standard exceptions, and never opens a dialog. Recording session state is
module-level, so it is scoped to the editor's Python session. Unlike the
builder records, it survives a toolset reload (toolset_registry's
reload_module re-runs this module in place): an engine recording keeps
growing whether Python remembers it or not, so the reloaded module adopts
the sessions, and the tick callback registered before the reload, which
calls into this same module, keeps enforcing the cap on them.
"""

import math
import os
import struct
import time
from dataclasses import dataclass

import unreal

from . import _core

#: Default export directory, under the project's Saved folder (never under
#: Content: renders are scratch, not assets).
RENDER_SUBDIR = 'MetaSoundToolset/Renders'

#: Hard cap on a recording's expected-duration hint, in seconds.
MAX_RECORDING_SECONDS = 600.0

#: Wall-clock cap on a recording, in seconds. Nothing in the engine stops a
#: recording: the submix keeps appending capture on the render thread, about
#: 1.4 GB an hour at 48 kHz stereo float, until memory runs out (security
#: pass S12). A Slate post-tick callback, registered while any recording
#: runs, stops and exports one that reaches this age; finish_output_recording
#: then reports the cap and the WAV path instead of a result. Twice the
#: duration hint's ceiling, so a recording that reaches it was forgotten.
RECORDING_CAP_SECONDS = 2 * MAX_RECORDING_SECONDS

#: An export whose file has not appeared this long after its stop is taken
#: to have no writer at all. The basis is the engine's: a capture with no
#: samples returns before it touches the writer
#: (AudioMixerBlueprintLibrary.cpp:271-276, 5.8.4), so it makes no file and
#: no completion. A capture with samples has no file either while its worker
#: is queued or serializing (SampleBufferIO.cpp:298-299, 600-615), and
#: nothing the toolset can reach tells the two apart, so this is the one
#: release that rests on timing (delta D1). Seconds, plus
#: EXPORT_NO_FILE_PER_SECOND per second the recording ran (its whole age,
#: PIE and waits included, which only lengthens it), and at least
#: EXPORT_SETTLE_TICKS ticks. Live on 5.8.4, a 2 s capture's file was on
#: disk by the next tick.
EXPORT_NO_FILE_SECONDS = 2.0
EXPORT_NO_FILE_PER_SECOND = 0.02

#: An export that has held the engine's writer this long is logged, once.
#: It keeps holding: letting the next stop through could cancel it (delta
#: D1), so finishes stay refused and overdue recordings wait.
EXPORT_TIMEOUT_SECONDS = 300.0

#: Only when the export's completion could not be subscribed to (see
#: _export_in_flight): ticks, and seconds, the export must stay complete on
#: disk before the next stop. The writer posts its completion to the game
#: thread after closing the file, and that completion resets whichever
#: writer is current by then, so this is a guess that it has run.
EXPORT_SETTLE_TICKS = 2
EXPORT_SETTLE_SECONDS = 0.1

#: Post-ticks from an export's first counted completion to its writer being
#: released; later counted ones never move it (micro-delta N4). The
#: completion arrives inside the engine's broadcast, before the engine
#: resets the writer; a post-tick does not run inside that broadcast, so by
#: the first the reset has run (micro-delta M3). Unless a subscriber runs
#: the editor's loop inside the broadcast (a modal dialog or another nested
#: loop): post-ticks can then run, and count, before the reset, and nothing
#: here can rule that out. The second is for M2's
#: remaining shape (_record_completion): when the first counted completion
#: was another writer's, this export's own still lands before the release
#: if it arrives before the second post-tick.
EXPORT_ACK_TICKS = 2

#: Hard cap on the audio bytes one analysis reads. max_seconds is measured
#: in the file's own claimed sample rate, so seconds alone cannot bound the
#: work: a header can claim any rate. 64 MiB is about 170 s of 48 kHz stereo
#: float, and stays a few seconds of game-thread time.
MAX_ANALYSIS_BYTES = 64 * 1024 * 1024

#: Hard cap on RIFF chunks walked. Real WAVs hold a handful; a crafted file
#: of zero-size chunks would otherwise cost one loop per 8 bytes of file.
MAX_RIFF_CHUNKS = 4096


@dataclass
class RecordingSession:
    """One output recording in flight."""
    name: str
    directory: str  # absolute filesystem directory the WAV lands in
    expected_duration: float
    submix_path: str  # '' = master output
    started_at: float = 0.0  # time.monotonic() at the engine start call
    # Set once the cap has logged that it cannot stop this recording yet
    # (PIE, or another export holding the engine writer), so the wait is
    # logged once, not on every tick.
    cap_wait_logged: bool = False
    # A master-output session only, and only a marker: '' when its start was
    # given None; None on a session adopted from a version without the field;
    # otherwise the master's path at start, for reading. Never looked up: a
    # path names whatever occupies it by stop time (micro-delta N1).
    master_path: str | None = None
    # The submix object the engine start was given, held for the session's
    # life so the stop is given that same object (_session_submix). The
    # Python wrapper is a strong reference, carried across an in-place reload
    # with the session; the device roots its master besides. None when the
    # start was given None, and on a session adopted from a version that
    # held no object.
    start_submix: 'unreal.SoundSubmix | None' = None


# Module state below is carried across a toolset reload: importlib.reload
# re-runs this file in the same module namespace, so each assignment reuses
# the value already there. See the module docstring for why.
#
# Every name-keyed dict below is keyed by the CASEFOLDED recording name
# (_name_key): the name becomes the WAV file name, and Windows paths are
# case-insensitive, so 'Hit' and 'hit' are the same output file and must be
# the same session identity (a case-sensitive key let 'hit' slip past the
# pending-export fence 'Hit' set). Sessions keep the caller's spelling for
# display and for the file name.
_recordings: dict[str, RecordingSession] = globals().get('_recordings', {})

# Renders finished but whose WAV had not yet been seen complete: name key ->
# expected absolute path. The export needs game-thread ticks after the
# finishing tool call returns, so a same-name restart in that window could
# otherwise adopt the PREVIOUS recording's export as its own output.
# Cleared by a later start that finds the landed file, or by a successful
# analysis of it (acknowledge_export).
_pending_wavs: dict[str, str] = globals().get('_pending_wavs', {})

# Source-render pairings: recording/wav name key -> (the caller's spelling,
# the audition session name started alongside it); see start_source_render /
# finish_source_render. Keyed like _recordings so a render that finishes
# under a case-drifted name pops its audition with its recording.
_source_renders: dict[str, tuple[str, str]] = globals().get(
    '_source_renders', {})

# Recordings the cap stopped, not yet reported: name key -> (the caller's
# spelling, the exported WAV path, the session it was made from).
# finish_output_recording on that name raises with the first two and
# forgets the entry; a new start under the name is allowed, because nothing
# is recording under it any more. The session names the report's origin, so
# a report can be told from another made under the same name (micro-delta
# N3: test cleanup consumes only reports made from its own sessions). A
# report carried across a reload from an earlier version has only two.
_capped: dict[str, tuple] = globals().get('_capped', {})

# Handle of the Slate post-tick callback that enforces the cap; registered
# by the first start, released by the tick that finds nothing recording.
# Kept across a reload, or the callback registered before it could never
# be released, and the next start would register a second one.
_cap_handle = globals().get('_cap_handle')

# The engine exports every recording through ONE file-static writer
# (AudioMixerBlueprintLibrary.cpp, 5.8.4): a stop replaces it, abandoning a
# write that has not started, and a finished write's completion, posted to
# the game thread, resets whichever writer is current by then. Two exports
# close together could cancel each other's WAV while both were reported
# exported (verify-cold R4). So one export at a time, by refusal: a stop
# while the writer is held is refused, and the recording keeps recording,
# listed and under the cap, until a later stop goes through (delta D2; the
# pause-and-queue this replaced could leave an empty capture recording
# after its finish was accepted, outside the cap's reach).
#
# The writer is released after the engine says its export is over. The
# completion of a WAV export broadcasts the submix the stop was given
# (USoundSubmix.OnSubmixRecordedFileDone) and then resets the writer, both in
# one game-thread task (AudioMixerBlueprintLibrary.cpp:293-302). The reset
# runs only once every subscriber has returned, and subscribers run one
# after another (ScriptDelegates.h:1436-1446), so a finish that a later
# subscriber calls still finds the writer about to be reset (micro-delta
# M3). The completion handler therefore only records the tick it arrived
# on, and the writer is released by a check on a later post-tick
# (EXPORT_ACK_TICKS), which holds against ordinary subscribers, not one that
# runs the editor's loop inside the broadcast (see there). The two releases
# that rest on a guess are an empty
# capture (no completion ever comes; see EXPORT_NO_FILE_SECONDS) and the
# settle fallback for an export whose completion could not be subscribed
# to.
#
# A completion counts only when it fits the export (_record_completion):
# the broadcast names no export, and the submix's own Start/Finish Recording
# Submix Output writer broadcasts it too (micro-delta M2).
#
# _export_in_flight describes the export holding the writer, None when it
# is free: {'path', 'stopped_at', 'stop_tick', 'ran_seconds', 'subscribed',
# 'ack_tick', 'stray_acks', 'ack_target', 'ack_fn', 'seen_tick', 'seen_at',
# 'hold_logged', 'fit_gated'}. subscribed is whether its completion handler
# was bound; ack_tick is the post-tick count when its first counted
# completion arrived, None before (later ones only count, in late_fits);
# stray_acks counts completions that did not fit it. seen_* are set when
# the WAV is first seen complete (settle
# fallback only). fit_gated stamps a flight this version issued, whose
# handler runs the fit test at arrival; a flight without it was issued
# before a reload and is adopted onto the settle guess ('legacy',
# _adopt_legacy_flight).
_export_in_flight: dict | None = globals().get('_export_in_flight')

# Fix round 3 gave a master-output stop a transient submix, so its
# completion had something to broadcast on, and relied on no device having
# registered it. Creating any Sound Submix asset registers every loaded
# non-dynamic submix on every device, the transient one too
# (AudioMixerDevice.cpp:2007-2015), and from then on a master stop stopped
# that empty recorder while the master kept recording (micro-delta M1, fired
# live 2026-11-03). Master sessions now start through the master submix
# object itself (_master_submix) and stop through that same held object
# (_session_submix). The old one is only kept alive for an export issued
# before a reload: its completion holds a raw pointer to it.
_ack_token = globals().get('_ack_token')

#: The AudioSettings default object. Its class is not exported to Python, so
#: the wrapper is DeveloperSettings and properties are found by their C++
#: names only (get_editor_property('MasterSubmix'), live 2026-11-03).
AUDIO_SETTINGS_DEFAULT = '/Script/Engine.Default__AudioSettings'

# Slate post-ticks seen by the callback; settles an export by ticks, since
# a tool call blocks them.
_tick_count: int = globals().get('_tick_count', 0)


def _name_key(name: str) -> str:
    """Canonical identity of a recording name (see the dicts above)."""
    return name.casefold()


# A reload from the pause-and-queue code can carry its export queue. Those
# recorders were only paused, or, for an empty capture, never paused at all,
# and nothing in this module would ever stop them, so they go back on the
# recording list, where finish and the cap reach them. That code also
# recorded a cap-expired queued session as capped; the report is stale once
# the session is listed again, so it goes (micro-delta M6).
for _queued in globals().pop('_export_queue', []):
    if _recordings.setdefault(_name_key(_queued.name), _queued) is _queued:
        _capped.pop(_name_key(_queued.name), None)


def track_source_render(name: str, audition_name: str) -> None:
    _source_renders[_name_key(name)] = (name, audition_name)


def pop_source_render(name: str) -> str:
    entry = _source_renders.pop(_name_key(name), None)
    if entry is None:
        known = ', '.join(
            sorted(n for n, _ in _source_renders.values())) or '(none)'
        raise LookupError(
            f"No source render named '{name}'. Renders in flight: {known}.")
    return entry[1]


def renders_dir() -> str:
    """Absolute default directory for rendered WAVs (created on demand)."""
    saved = unreal.Paths.convert_relative_path_to_full(
        unreal.Paths.project_saved_dir())
    path = os.path.join(saved, *RENDER_SUBDIR.split('/'))
    os.makedirs(path, exist_ok=True)
    return path


def validate_recording_name(name: str) -> None:
    if (not name
            or any(c in name for c in ' /.:\\\'"?*<>|')
            or any(ord(c) < 32 for c in name)
            or name.upper() in _core.RESERVED_FILE_NAMES):
        raise ValueError(
            f"Recording name '{name}' is invalid: it becomes the WAV file "
            'name, so no spaces, slashes, control characters, reserved file '
            'names such as NUL, or other filesystem-special characters.')


def _load_submix(submix_path: str | None):
    if not submix_path:
        return None
    return _core.load_asset_checked(submix_path, unreal.SoundSubmix)


def _master_submix():
    """The audio device's master submix object, or None if it can't be named.

    A device resolves a required submix (master, reverb, EQ) before its
    table of registered ones (GetSubmixInstance, AudioMixerDevice.cpp:2912),
    so this object addresses the master's recorder whatever else is
    registered, and an export's completion is broadcast on it. The device
    loads it from AudioSettings.MasterSubmix and writes the engine fallback
    back into that setting when the configured one fails to load
    (LoadRequiredSubmix), so the setting names the loaded master. Only an
    object already loaded is used: loading one here would register it as an
    ordinary submix. None, when the setting or object can't be reached:
    the caller then passes None, which the device also resolves to the
    master, and the export goes without a completion to wait for.
    """
    try:
        settings = unreal.find_object(None, AUDIO_SETTINGS_DEFAULT)
        path = settings.get_editor_property('MasterSubmix').export_text()
        master = unreal.find_object(None, path) if path else None
    except Exception:  # noqa: BLE001 - None addresses the master too
        return None
    return master if isinstance(master, unreal.SoundSubmix) else None


def _session_submix(session: RecordingSession):
    """The submix object a session's stop is given: the one its start was.

    The held object, never a lookup (micro-delta N1). A path, the session's
    own or the Master Submix setting's, names whatever occupies it at stop
    time: after a rename and a redirector fix-up, another submix renamed
    into the old path has a recorder of its own, and the stop would end
    that one while the started recorder ran on outside the cap. A rename
    keeps the same UObject, so the held object still addresses the recorder
    the start did. This holds for explicit submixes as much as the master:
    their stop loaded the stored path again the same way.

    Without a held object:
    - master (a start given None, or a session adopted from an earlier
      version across an in-place reload): None. The device resolves None to
      its own master submix, by identity: the lookup fails, required
      submixes are never null, and the stop falls back to GetMasterSubmix
      (AudioMixerDevice.cpp:2637-2647, 2912-2927, 3204-3217, 5.8.4). No
      completion is broadcast on None, so that export falls to the settle
      guess.
    - explicit submix (adopted sessions only): the stored path, loaded
      again. None would address the master instead, so this is the one
      lookup left, for sessions issued before this version.
    """
    held = getattr(session, 'start_submix', None)
    if held is not None:
        return held
    if session.submix_path:
        return _load_submix(session.submix_path)
    return None


def start_recording(name: str, expected_duration: float,
                    submix_path: str | None) -> RecordingSession:
    """Start recording the editor's audio output. Raises on bad input."""
    validate_recording_name(name)
    if not 0.0 < expected_duration <= MAX_RECORDING_SECONDS:
        raise ValueError(
            f'expected_duration_seconds {expected_duration} outside '
            f'(0, {MAX_RECORDING_SECONDS}].')
    key = _name_key(name)
    if key in _recordings:
        raise RuntimeError(
            f"A recording named '{_recordings[key].name}' is already "
            'running (recording names are case-insensitive: they are file '
            'names); finish_output_recording it first or pick another '
            'name.')
    submix = _load_submix(submix_path)
    # One recording per submix (master = ''): the engine recorder is
    # addressed BY SUBMIX with no per-session handle, so two sessions on
    # the same submix cannot be independent captures - finishing one
    # stops the recorder the other claims. The submix is compared by its
    # canonical package path so path spellings cannot alias.
    submix_key = (
        submix.get_path_name().split('.')[0] if submix is not None else '')
    holder = next((s.name for s in _recordings.values()
                   if s.submix_path == submix_key), None)
    target = (f"submix '{submix_key}'" if submix_key
              else 'the master output')
    if holder is not None:
        raise RuntimeError(
            f"Recording '{holder}' is already capturing {target}, and the "
            'engine runs one recorder per submix - a second session on it '
            "would hijack that capture (finishing either stops the "
            f"other's recorder). finish_output_recording '{holder}' "
            'first, or record a different submix.')
    # Every refusal runs before the clear below: a start refused for want
    # of an editor world (PIE running, none loaded) must not have already
    # deleted the previous good render.
    world = _core.editor_world()
    # Clear any previous render of the same name BEFORE the engine recorder
    # starts: wait_for_file accepts any non-empty WAV at the output path,
    # so a leftover <name>.wav would be analyzed as this recording's result
    # (stale-generation poisoning of the verify loop).
    directory = renders_dir()
    wav_path = os.path.join(directory, f'{name}.wav')
    if os.path.exists(wav_path):
        try:
            os.remove(wav_path)
        except OSError as exc:
            raise RuntimeError(
                f"A previous render already sits at '{wav_path}' and could "
                f'not be removed ({exc}). Close whatever holds that file '
                'open or pick another recording name; analyzing this '
                'recording could otherwise read the stale file as its '
                'result.') from exc
        _pending_wavs.pop(key, None)
    elif key in _pending_wavs:
        # Finished earlier, but its WAV has not landed yet (the export
        # needs game-thread ticks after the finishing call returns). A
        # same-name recording started now could have that OLD export
        # analyzed as its own output, so refuse until the old file lands
        # (the next start finds and clears it).
        raise RuntimeError(
            f"The previous render named '{name}' is still exporting its "
            f"WAV to '{_pending_wavs[key]}'. Wait a moment and retry - "
            'the landed file is then cleared automatically - or pick '
            'another recording name.')
    master = _master_submix() if submix is None else None
    start_submix = submix if submix is not None else master
    unreal.AudioMixerLibrary.start_recording_output(
        world, expected_duration, start_submix)
    session = RecordingSession(
        name=name, directory=directory,
        expected_duration=expected_duration, submix_path=submix_key,
        started_at=time.monotonic(),
        master_path=(None if submix is not None
                     else master.get_path_name() if master is not None
                     else ''),
        start_submix=start_submix)
    _recordings[key] = session
    _capped.pop(key, None)
    _watch_recording_cap()
    return session


def _watch_recording_cap() -> None:
    """Make sure the cap is enforced on every tick while anything records."""
    global _cap_handle
    if _cap_handle is None:
        _cap_handle = unreal.register_slate_post_tick_callback(
            _on_slate_post_tick)


def _on_slate_post_tick(_delta_seconds: float) -> None:
    global _tick_count
    _tick_count += 1
    try:
        enforce_recording_cap()
    except Exception as exc:  # noqa: BLE001 - a tick callback must not raise
        unreal.log_warning(
            f'MetaSoundToolset: recording cap check failed: {exc}')


def _recording_world() -> unreal.World:
    """The world context the engine stop and pause resolve the device from.

    Only the editor world reaches the editor's audio device, and only
    outside PIE. Entering PIE sets the editor world's bAllowAudioPlayback
    to false (PlayLevel.cpp:2744, 5.8.4), so the engine resolves no device
    from it at all; the PIE world has a device of its own. A stop through
    the editor world found by path during PIE was a no-op that the cap then
    reported as an export (fired live 2026-11-03: 'Output recording is an
    audio mixer only feature', no WAV, the session forgotten).

    Raises:
        RuntimeError: PIE is running, or there is no editor world. The cap
            keeps the session and retries on a later tick.
    """
    return _core.editor_world()


def _wav_path(session: RecordingSession) -> str:
    return os.path.join(session.directory, f'{session.name}.wav')


def _wav_complete(path: str) -> bool:
    """True once the WAV holds everything its RIFF header claims.

    The engine writer puts the full-size header first and the samples after
    it. The size is checked before the file is opened, so only a file the
    writer has already created is ever opened here.
    """
    try:
        size = os.path.getsize(path)
        if size <= 44:
            return False
        with open(path, 'rb') as f:
            head = f.read(8)
    except OSError:
        return False
    if len(head) < 8 or head[:4] != b'RIFF':
        return False
    return size >= struct.unpack('<I', head[4:8])[0] + 8


def _release_writer(flight: dict) -> bool:
    """Free the writer from this export and drop its completion handler."""
    global _export_in_flight
    _export_in_flight = None
    if flight.get('ack_fn') is not None:
        try:
            flight['ack_target'].on_submix_recorded_file_done \
                .remove_callable(flight['ack_fn'])
        except Exception:  # noqa: BLE001 - the handler only sets a flag
            pass
    return True


def _record_completion(flight: dict, sound_wave) -> None:
    """Count a completion broadcast on the export's submix, if it is its own.

    The broadcast carries no export identity, and it is not this writer's
    alone: USoundSubmix's own Start/Finish Recording Submix Output exports
    through a writer of its own and broadcasts the same delegate
    (SoundSubmix.cpp:129-183), without ever touching the writer held here
    (micro-delta M2). So a completion counts only when it fits this export.
    A WAV export's completion passes no sound wave (that writer's SoundWave
    export does), and this export's completion is posted after its file is
    written and closed (SampleBufferIO.cpp:640-643, 770-776), so its WAV is
    complete when it arrives. Any other completion is counted as stray and
    released nothing: one arriving before this file is complete, or with no
    file at all (that export then falls to the no-file guess).

    The first completion that fits fixes the release tick; later ones are
    counted (late_fits) and never move it (micro-delta N4). Every fitting
    completion used to move it, so another producer on the submix that
    completed a WAV every tick held the writer, idle since this export's own
    completion, without end, and every other recording's finish and cap
    with it. Moving it made nothing safer: a completion that arrives before
    the release is already before it, and the further cover a move gave
    rested on how often another producer happened to complete.

    Left open (M2): another writer's WAV completion on this submix arriving
    after this file is complete but before this export's own completion.
    The writer is released EXPORT_ACK_TICKS post-ticks after that first
    fit, so this export's own completion arriving later than that can still
    cancel the next export.
    """
    if sound_wave is None and _wav_complete(flight['path']):
        if flight.get('ack_tick') is None:
            flight['ack_tick'] = _tick_count
        else:
            flight['late_fits'] = flight.get('late_fits', 0) + 1
    else:
        flight['stray_acks'] = flight.get('stray_acks', 0) + 1


def _completion_handler(flight: dict):
    """The callable an export binds to its submix's completion broadcast.

    It only records (_record_completion). It runs inside the engine's
    broadcast, before the engine resets the writer, so releasing the writer
    here would let a finish called by a later subscriber of the same
    broadcast install a writer that reset then destroys (micro-delta M3).
    _export_writer_free releases it on a later post-tick.
    """
    def on_done(sound_wave):
        _record_completion(flight, sound_wave)
    return on_done


def _adopt_legacy_flight(flight: dict) -> None:
    """Put an export issued before an in-place reload onto the settle guess.

    Only a flight this version issued is stamped 'fit_gated': its handler
    runs _record_completion when a completion arrives. An earlier version's
    handler stays bound across the reload and does not. Fix round 3's sets
    'acked' True and 94e4e63's writes 'ack_tick' itself, both ignoring the
    event's argument and the file's state when it arrived. What they latch
    has lost the two facts the fit test needs, so it is never counted as a
    completion, now or later (micro-delta N2: reading the Boolean at a later
    check and testing the file then upgraded a foreign or early completion
    into a counted one). Nor is the dict's shape taken as evidence of which
    handler is bound: a flight without the stamp is legacy, whatever fields
    it carries, round 4's own included.

    A legacy flight is released as one whose completion could not be
    subscribed to: the settle guess once its WAV is complete, or the no-file
    guess. That is weaker than a counted completion, and it applies only to
    an export in flight across a reload from an earlier version.

    Only the stamp ends the normalization, never an earlier adapter's
    'legacy' flag: round 4's set it on a round-3 flight it adopted, beside
    'subscribed' True, so a check that trusted the flag left that flight
    with no release rule at all once its WAV was complete (micro-delta
    MD3-1). Run on every check, the body changes nothing its own output
    already says, and an in-progress settle's seen_tick survives it.
    """
    if flight.get('fit_gated'):
        return
    flight['legacy'] = True
    flight['subscribed'] = False
    flight['ack_tick'] = None
    flight.setdefault('seen_tick', None)
    flight.setdefault('seen_at', None)
    flight.setdefault('stray_acks', 0)


def _export_writer_free() -> bool:
    """Whether the engine's export writer can take another stop now.

    Free when no export holds it; on a post-tick at least EXPORT_ACK_TICKS
    after the first completion that fits the export holding it reached this
    module (the engine's own end of the write, the writer reset by then;
    later ones never postpone it, micro-delta N4); when its
    file never appeared within EXPORT_NO_FILE_SECONDS (nothing was
    captured, so the engine made no writer: a guess, see there); or, only
    for an export whose completion could not be subscribed to, or one issued
    before an in-place reload (_adopt_legacy_flight), once its WAV has been
    complete on disk for EXPORT_SETTLE_TICKS ticks. Nothing else
    frees it: past EXPORT_TIMEOUT_SECONDS it is logged once and keeps
    holding, because letting a stop through could cancel the export (delta
    D1). Never waits: a stop it calls held is refused at once and retried
    (delta D3). Measured on the real clock, whatever a caller passes as now.
    """
    flight = _export_in_flight
    if flight is None:
        return True
    _adopt_legacy_flight(flight)
    if _release_due(flight):
        return _release_writer(flight)
    _log_long_hold(flight)
    return False


def _release_due(flight: dict) -> bool:
    """Whether one of _export_writer_free's release rules frees it now."""
    # A legacy flight's old handler may still write ack_tick; it is not
    # evidence (micro-delta N2), so only a stamped flight's counts.
    if flight['ack_tick'] is not None and not flight.get('legacy'):
        # Called inside the completion broadcast itself (a later subscriber
        # finishing a recording), the count has not moved: still held. A
        # subscriber that runs the editor's loop first is the exception
        # (EXPORT_ACK_TICKS).
        return _tick_count - flight['ack_tick'] >= EXPORT_ACK_TICKS
    now = time.monotonic()
    path = flight['path']
    if not flight['subscribed']:
        if flight['seen_tick'] is not None:
            return (_tick_count - flight['seen_tick'] >= EXPORT_SETTLE_TICKS
                    and now - flight['seen_at'] >= EXPORT_SETTLE_SECONDS)
        if _wav_complete(path):
            flight['seen_tick'], flight['seen_at'] = _tick_count, now
            return False
    no_file_after = (EXPORT_NO_FILE_SECONDS
                     + EXPORT_NO_FILE_PER_SECOND * flight['ran_seconds'])
    return (not os.path.exists(path)
            and now - flight['stopped_at'] >= no_file_after
            and _tick_count - flight.get('stop_tick', 0)
            >= EXPORT_SETTLE_TICKS)


def _log_long_hold(flight: dict) -> None:
    """Log, once, an export holding the writer past EXPORT_TIMEOUT_SECONDS.

    Checked on every held path, an acknowledged export's included: that
    branch used to return before this, so the diagnostic went silent once
    any completion had counted (micro-delta N4).
    """
    held = time.monotonic() - flight['stopped_at']
    if held < EXPORT_TIMEOUT_SECONDS or flight.get('hold_logged'):
        return
    flight['hold_logged'] = True
    unreal.log_warning(
        f"MetaSoundToolset: the export to '{flight['path']}' has held the "
        f'engine WAV writer for {held:.0f} s and still holds it: letting '
        'another stop through could cancel it, so finishes stay refused and '
        f'overdue recordings wait. {_hold_advice(flight)}')


def _hold_advice(flight: dict) -> str:
    """What frees a long-held writer, for the hold warning and the refusal."""
    if flight['ack_tick'] is not None and not flight.get('legacy'):
        return ('Its completion has arrived, and the writer is released '
                f'{EXPORT_ACK_TICKS} editor ticks after it; fewer have run '
                'since.')
    return ('If that export failed, deleting or moving its file releases '
            f'the writer {_recovery_wait(flight)}.')


def _recovery_wait(flight: dict) -> str:
    """When deleting a stuck export's file frees the writer.

    The delete only hands the export to the no-file release, whose wait it
    does not shorten: 434 s after a six-hour recording's stop.
    """
    no_file_after = (EXPORT_NO_FILE_SECONDS
                     + EXPORT_NO_FILE_PER_SECOND * flight['ran_seconds'])
    return (f'{no_file_after:.0f} s after its stop (and two editor ticks), '
            'not at once')


def _issue_export(session: RecordingSession, world, submix) -> None:
    """Stop the session's engine recorder and hand its capture to the writer.

    submix is the object the session's start was given (the master submix
    for a master session), so the stop addresses the recorder the start
    did, and the completion is broadcast on it. None addresses the master
    too, but broadcasts nothing: that export falls back to the settle guess.
    """
    global _export_in_flight
    target = submix
    flight = {
        'path': _wav_path(session), 'stopped_at': 0.0, 'stop_tick': 0,
        'ran_seconds': 0.0, 'subscribed': False, 'ack_tick': None,
        'stray_acks': 0, 'ack_target': target, 'ack_fn': None,
        'seen_tick': None, 'seen_at': None, 'hold_logged': False,
        'fit_gated': True}
    if target is not None:
        on_done = _completion_handler(flight)
        try:
            target.on_submix_recorded_file_done.add_callable(on_done)
            flight['ack_fn'], flight['subscribed'] = on_done, True
        except Exception:  # noqa: BLE001 - fall back to the settle guess
            pass
    try:
        unreal.AudioMixerLibrary.stop_recording_output(
            world, unreal.AudioRecordingExportType.WAV_FILE, session.name,
            session.directory, target)
    except Exception:
        _release_writer(flight)
        raise
    now = time.monotonic()
    flight['stopped_at'], flight['stop_tick'] = now, _tick_count
    flight['ran_seconds'] = max(0.0, now - session.started_at)
    _export_in_flight = flight


def _refuse_while_writer_held(session: RecordingSession) -> None:
    """Raise if another export holds the engine writer; the stop must wait.

    Nothing is paused or stopped here: the recording keeps recording, keeps
    its place in _recordings and stays under the cap, so a refused finish
    leaves it exactly as it was (delta D2).
    """
    if _export_writer_free():
        return
    flight = _export_in_flight
    held = time.monotonic() - flight['stopped_at']
    stuck = ''
    if held >= EXPORT_TIMEOUT_SECONDS:
        stuck = (f' It has held the writer for {held:.0f} s. '
                 f'{_hold_advice(flight)}')
    raise RuntimeError(
        f"The engine's WAV writer is still exporting '{flight['path']}', "
        'and it exports one recording at a time: stopping '
        f"'{session.name}' now could cancel that file. '{session.name}' is "
        'still recording and listed. Retry the finish when that export '
        f'lands, normally within a second or two.{stuck}')


def _stop_and_export(session: RecordingSession) -> str:
    """Stop the engine recorder behind a session and export its WAV.

    Refused, with the session untouched, while another export holds the
    engine writer or while PIE keeps the editor's audio device out of reach.
    Returns the WAV path, and fences the name as exporting.
    """
    _refuse_while_writer_held(session)
    submix = _session_submix(session)
    world = _recording_world()
    _issue_export(session, world, submix)
    wav_path = _wav_path(session)
    # The export lands asynchronously; remember it so a same-name restart
    # cannot adopt this recording's output as its own (cleared when a later
    # start finds the landed file and removes it, or when it is analyzed).
    _pending_wavs[_name_key(session.name)] = wav_path
    return wav_path


def refuse_if_cannot_stop(name: str) -> None:
    """Raise what a finish of this recording would be refused for now.

    For callers that consume other state before finishing (the source
    render's audition and pairing), so a refusal leaves that state intact.
    Silent for a name not listed (the cap stopped it, or nothing records
    under it): the finish itself reports those. A listed session is
    checked even if a cap report under its name survives (micro-delta M6).
    """
    session = _recordings.get(_name_key(name))
    if session is None:
        return
    _refuse_while_writer_held(session)
    _recording_world()


def enforce_recording_cap(now: float | None = None) -> list[str]:
    """Stop and export every recording older than the cap; names stopped.

    Runs from the post-tick callback, so it is ordinary game-thread work
    between tool calls. A recording that cannot be stopped yet keeps its
    session and is retried on every tick, the wait logged once per
    recording: while PIE runs no world context reaches the editor's audio
    device (_recording_world), and while another export holds the engine
    writer a stop could cancel that file. So several overdue recordings in
    one tick export in turn, one per writer release. The callback
    unregisters itself once nothing is recording or exporting.
    """
    global _cap_handle
    now = time.monotonic() if now is None else now
    stopped = []
    for key, session in list(_recordings.items()):
        if now - session.started_at < RECORDING_CAP_SECONDS:
            continue
        try:
            wav_path = _stop_and_export(session)
        except Exception as exc:  # noqa: BLE001 - retried next tick
            if not getattr(session, 'cap_wait_logged', False):
                session.cap_wait_logged = True
                unreal.log_warning(
                    f"MetaSoundToolset: recording '{session.name}' is past "
                    f'the {RECORDING_CAP_SECONDS:.0f} s cap and cannot be '
                    f'stopped yet; retrying every tick: {exc}')
            continue
        del _recordings[key]
        _capped[key] = (session.name, wav_path, session)
        stopped.append(session.name)
        unreal.log_warning(
            f"MetaSoundToolset: recording '{session.name}' ran past the "
            f'{RECORDING_CAP_SECONDS:.0f} s cap; it was stopped and exported '
            f"to '{wav_path}'. finish_output_recording reports this.")
    # Checked on every tick, recordings or not, so an acknowledged export
    # frees the writer, and a long hold is logged, without waiting for a stop.
    writer_free = _export_writer_free()
    if not _recordings and writer_free and _cap_handle is not None:
        unreal.unregister_slate_post_tick_callback(_cap_handle)
        _cap_handle = None
    return stopped


def get_recording(name: str) -> RecordingSession:
    session = _recordings.get(_name_key(name))
    if session is None:
        known = ', '.join(list_recordings()) or '(none)'
        raise LookupError(
            f"No recording named '{name}'. Recordings in flight: {known}.")
    return session


def finish_recording(name: str) -> str:
    """Stop the recording and export the WAV; returns the absolute path.

    The engine finalizes the file asynchronously: the path is where the WAV
    lands, typically within well under a second for short recordings.

    Raises:
        RuntimeError: the cap already stopped this recording (the message
            carries the WAV path; nothing records under the name any more);
            or another recording's export still holds the engine writer, or
            PIE is running, and this recording keeps recording, listed,
            until a retry goes through.
        LookupError: no recording of that name is in flight.
    """
    # A listed session is recording, whatever cap report its name still
    # carries: only one adopted across a reload could carry one, and that
    # report is stale, so the finish goes ahead (micro-delta M6).
    capped = _capped.pop(_name_key(name), None)
    if capped is not None and _name_key(name) not in _recordings:
        spelled, wav_path = capped[:2]
        raise RuntimeError(
            f"Recording '{spelled}' ran past the cap of "
            f'{RECORDING_CAP_SECONDS:.0f} s, so the toolset stopped it and '
            f"exported what it had to '{wav_path}' (analyze_rendered_wav "
            'measures it). Nothing is recording under that name now.')
    session = get_recording(name)
    wav_path = _stop_and_export(session)
    del _recordings[_name_key(name)]
    # Releases the tick callback only when idle: while this export still
    # holds the writer, the ticks are what settle it.
    enforce_recording_cap()
    return wav_path


def acknowledge_export(path: str) -> None:
    """Drop the pending-export entry whose WAV is this (complete) file."""
    target = os.path.normcase(os.path.abspath(path))
    for key, pending in list(_pending_wavs.items()):
        if os.path.normcase(os.path.abspath(pending)) == target:
            del _pending_wavs[key]


def list_recordings() -> list[str]:
    return sorted(s.name for s in _recordings.values())


def wait_for_file(path: str, wait_seconds: float) -> bool:
    """Poll briefly for an async-written file; True once it exists non-empty.

    The export writer runs off the game thread, so a short blocking wait
    here does not deadlock it.
    """
    deadline = time.monotonic() + max(0.0, wait_seconds)
    while True:
        try:
            if os.path.getsize(path) > 44:  # more than a bare WAV header
                return True
        except OSError:
            pass
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.05)


def _under(target: str, root: str) -> bool:
    return target == root or target.startswith(root + os.sep)


def ensure_inside_project(path: str) -> None:
    """Refuse a file path outside the project, lexically first, then resolved.

    These are the roots Epic's own execute_tool_script file access is held
    to (programmatic.py, 5.8): an agent-supplied path must not reach further
    through this toolset than it can through the sandbox.

    The first check is an allowlist on the spelling: os.path.abspath is
    GetFullPathName, which touches nothing, and the result must sit under
    the project or Saved root as spelled. A denylist of network prefixes
    cannot hold on Windows: '\\??\\UNC\\host\\share\\x.wav' and
    '\\??\\GLOBALROOT\\Device\\Mup\\...' carry no '\\\\', and realpath
    normalizes '/??/' into them before it opens anything, so each reached
    the SMB redirector past the old prefix check (cold read C2, fired). Only
    a path already spelled inside the project reaches realpath, which then
    refuses a junction or symlink that leads back out. A project that lives
    on a network share keeps working, because its own roots are spelled the
    same way (cold read C5).

    Raises:
        PermissionError: if the path is not spelled under the project, or
            resolves outside it through a link.
    """
    if not path or any(ord(c) < 32 for c in path):
        raise PermissionError(
            f"'{path}' is not a usable file path; analyze_rendered_wav "
            'reads WAVs under the project folder and its Saved folder only.')
    roots = [unreal.Paths.convert_relative_path_to_full(root)
             for root in (unreal.Paths.project_dir(),
                          unreal.Paths.project_saved_dir())]
    spelled = os.path.normcase(os.path.abspath(path))
    if not any(_under(spelled, os.path.normcase(os.path.abspath(root)))
               for root in roots):
        raise PermissionError(
            f"'{path}' is outside this project (checked on the spelling, "
            'before anything is opened: network, device and \\\\?\\ paths '
            'are refused here); analyze_rendered_wav reads WAVs under the '
            'project folder and its Saved folder only. Copy the file into '
            'the project to measure it.')
    resolved = os.path.normcase(os.path.realpath(path))
    if not any(_under(resolved, os.path.normcase(os.path.realpath(root)))
               for root in roots):
        raise PermissionError(
            f"'{path}' leads outside this project through a link or "
            'junction; analyze_rendered_wav reads WAVs under the project '
            'folder and its Saved folder only. Copy the file into the '
            'project to measure it.')


def analyze_wav(path: str, max_seconds: float = 120.0,
                silence_threshold_db: float = -60.0) -> dict:
    """Measure a PCM/float WAV: duration, peak dB, RMS dB, silence check.

    Pure-Python RIFF parse (16/24/32-bit PCM and 32-bit float), analysis
    capped at max_seconds of audio, MAX_ANALYSIS_BYTES and MAX_RIFF_CHUNKS
    so a long bounce or a hostile file cannot stall the game thread.

    Raises:
        PermissionError: the path is outside the project.
        FileNotFoundError: no file at the path (exports are asynchronous -
            retry shortly after finish_output_recording).
        ValueError: the file is not a .wav this analyzer understands, it
            holds less data than its header claims (an export still
            finalizing - retry in a moment), or a float file holds NaN or
            infinite samples.
    """
    if max_seconds <= 0.0:
        raise ValueError('max_seconds must be > 0.')
    ensure_inside_project(path)
    if not path.lower().endswith('.wav'):
        raise ValueError(f"'{path}' is not a .wav file.")
    if not os.path.isfile(path):
        raise FileNotFoundError(
            f"No file at '{path}'. WAV exports finalize asynchronously; "
            'if you just finished the recording, try again in a moment.')

    with open(path, 'rb') as f:
        head = f.read(12)
        if len(head) < 12:
            # Shorter than the RIFF header itself: an export caught before
            # its header landed, not a malformed file.
            raise ValueError(
                f"'{path}' holds only {len(head)} bytes, less than a WAV "
                'header. WAV exports finalize asynchronously - try again '
                'in a moment.')
        riff, _size, wave_tag = struct.unpack('<4sI4s', head)
        if riff != b'RIFF' or wave_tag != b'WAVE':
            raise ValueError(f"'{path}' is not a RIFF/WAVE file.")
        audio_format = channels = sample_rate = bits = None
        data_offset = data_size = None
        for _ in range(MAX_RIFF_CHUNKS):
            header = f.read(8)
            if len(header) < 8:
                break
            chunk_id, chunk_size = struct.unpack('<4sI', header)
            if chunk_id == b'fmt ':
                fmt = f.read(min(chunk_size, 40))
                if len(fmt) < 16:
                    raise ValueError(f"'{path}' has a truncated fmt chunk.")
                f.seek(chunk_size - len(fmt) + (chunk_size & 1), 1)
                (audio_format, channels, sample_rate, _byte_rate,
                 _block_align, bits) = struct.unpack('<HHIIHH', fmt[:16])
                if audio_format == 0xFFFE and len(fmt) >= 26:
                    # WAVE_FORMAT_EXTENSIBLE: real format in the GUID head.
                    audio_format = struct.unpack('<H', fmt[24:26])[0]
            elif chunk_id == b'data':
                data_offset, data_size = f.tell(), chunk_size
                f.seek(chunk_size + (chunk_size & 1), 1)
            else:
                f.seek(chunk_size + (chunk_size & 1), 1)
        else:
            raise ValueError(
                f"'{path}' holds more than {MAX_RIFF_CHUNKS} RIFF chunks.")
        if audio_format is None or data_offset is None:
            raise ValueError(f"'{path}' has no fmt/data chunks.")
        if audio_format not in (1, 3):
            raise ValueError(
                f"'{path}' audio format {audio_format} unsupported (PCM and "
                'IEEE float are).')
        if bits not in (16, 24, 32):
            raise ValueError(f"'{path}' bit depth {bits} unsupported.")
        if audio_format == 3 and bits != 32:
            raise ValueError(f"'{path}' float WAV must be 32-bit.")
        if channels == 0 or sample_rate == 0:
            raise ValueError(
                f"'{path}' declares {channels} channels at {sample_rate} Hz.")

        # The async exporter writes the header's data size up front and
        # flushes samples after it: a mid-flush file holds less data than
        # the header claims, and trusting the header would misreport the
        # duration (or feed struct.unpack a partial frame).
        f.seek(0, 2)
        data_available = max(0, f.tell() - data_offset)
        if data_available < data_size:
            raise ValueError(
                f"'{path}' holds less audio data than its header claims "
                f'({data_available} of {data_size} bytes). WAV exports '
                'finalize asynchronously - try again in a moment.')

        bytes_per_sample = bits // 8
        frame_size = bytes_per_sample * channels
        total_frames = data_size // frame_size
        duration = total_frames / float(sample_rate)
        analyze_frames = min(total_frames, int(max_seconds * sample_rate),
                             MAX_ANALYSIS_BYTES // frame_size)

        f.seek(data_offset)
        raw = f.read(analyze_frames * frame_size)
        # Belt and braces for the unpack calls below: never hand them a
        # partial frame even if the file changed under us mid-read.
        raw = raw[:len(raw) // frame_size * frame_size]

    peak = 0.0
    sum_squares = 0.0
    count = 0
    nonfinite = 0
    if audio_format == 3:
        values = struct.unpack(f'<{len(raw) // 4}f', raw)
        for v in values:
            # NaN fails every comparison and poisons the sum, and inf is a
            # peak no audio has: all-NaN data read as clean silence and one
            # bad sample turned the RMS into the silence floor (verify-cold
            # R7). Counted here, refused below.
            if not math.isfinite(v):
                nonfinite += 1
                continue
            a = abs(v)
            if a > peak:
                peak = a
            sum_squares += v * v
        count = len(values)
    elif bits == 16:
        values = struct.unpack(f'<{len(raw) // 2}h', raw)
        scale = 32768.0
        for v in values:
            a = abs(v)
            if a > peak:
                peak = a
            sum_squares += v * v
        peak /= scale
        sum_squares /= scale * scale
        count = len(values)
    elif bits == 32:
        values = struct.unpack(f'<{len(raw) // 4}i', raw)
        scale = 2147483648.0
        for v in values:
            a = abs(v)
            if a > peak:
                peak = a
            sum_squares += float(v) * v
        peak /= scale
        sum_squares /= scale * scale
        count = len(values)
    else:  # 24-bit PCM
        scale = 8388608.0
        for i in range(0, len(raw) - 2, 3):
            v = int.from_bytes(raw[i:i + 3], 'little', signed=True)
            a = abs(v)
            if a > peak:
                peak = a
            sum_squares += float(v) * v
            count += 1
        peak /= scale
        sum_squares /= scale * scale

    def to_db(linear: float) -> float:
        return 20.0 * math.log10(linear) if linear > 0.0 else -144.0

    rms = math.sqrt(sum_squares / count) if count else 0.0
    peak_db = to_db(peak)
    # A complete read proves the export finished (the payload-length guard
    # above refuses mid-flush files), so a later move/delete of this WAV
    # must not leave its name "still exporting" for the rest of the session.
    acknowledge_export(path)
    if nonfinite:
        raise ValueError(
            f"'{path}' holds {nonfinite} NaN or infinite samples among the "
            f'{count} analyzed. That is not measurable audio: no level or '
            'silence result would be true. The graph or the tool that '
            'wrote the file produced invalid values.')
    return {
        'wav_path': path,
        'duration_seconds': duration,
        'sample_rate': sample_rate,
        'channels': channels,
        'bits_per_sample': bits,
        'peak_db': peak_db,
        'rms_db': to_db(rms),
        'is_silent': peak_db < silence_threshold_db,
        'analyzed_seconds': analyze_frames / float(sample_rate),
    }
