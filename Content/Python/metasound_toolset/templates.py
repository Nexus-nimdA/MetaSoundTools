"""One-call MetaSound graph templates.

Jam speed is one composite call, not forty serial ones (the MCP executes
tools serially on the game thread with a 60 s default timeout). Every
template validates all of the caller's inputs BEFORE touching the engine,
so a bad call fails fast with a clear exception. Checks that need the
engine's own state (the preset reference asset's input schema, mid-build
node results) run once the builder exists and unwind it - unregistered and
untracked - before raising, so no call leaves half-built state.

Two templates ship in v1:

* ``create_random_oneshot_preset`` - a preset of the engine's own
  ``MSS_PlayRandom_Oneshot`` reference source (random wave pick + pitch /
  volume spread). Presets are the designer-idiomatic way to get this graph:
  robust against node-level churn and instantly editable in the MetaSound
  editor.
* ``create_wave_player_source`` - a from-scratch source built node by node
  through the Builder substrate: Wave Player with an exposed pitch-shift
  input and a volume input authored in dB (Decibels to Linear Gain ->
  Multiply (Audio by Float), the 5.8 gain idiom - there is no Gain (dB)
  node).

Where possible, pins are located by DATA TYPE rather than display name so
the templates survive pin-name drift between engine versions.
"""

import unreal

from . import _core

# The engine's reference random-player sources (see also
# MSS_PlayRandom_Loop for looping beds).
ENGINE_RANDOM_ONESHOT_ASSET = (
    '/Metasound/DefaultAssets/Sources/MSS_PlayRandom_Oneshot')
ENGINE_RANDOM_LOOP_ASSET = (
    '/Metasound/DefaultAssets/Sources/MSS_PlayRandom_Loop')

_OUTPUT_FORMATS = {
    'Mono': unreal.MetaSoundOutputAudioFormat.MONO,
    'Stereo': unreal.MetaSoundOutputAudioFormat.STEREO,
}


def _validate_new_builder_name(builder_name: str) -> None:
    _core.builder_key(builder_name)  # refuses empty and NAME_None names
    if _core.builder_subsystem().find_builder(builder_name) is not None:
        raise ValueError(
            f"A builder named '{builder_name}' already exists; pick another "
            'name or release_builder it first.')


def _validate_wave_assets(wave_asset_paths: list[str]) -> list[unreal.Object]:
    if not wave_asset_paths:
        raise ValueError('wave_asset_paths must contain at least one path.')
    _core.ensure_list_bounded('wave_asset_paths', wave_asset_paths)
    waves = []
    for path in wave_asset_paths:
        wave = _core.load_asset_checked(path)
        if not isinstance(wave, unreal.SoundWave):
            raise TypeError(
                f"Asset '{path}' is a {type(wave).__name__}, expected a "
                'SoundWave (imported audio file).')
        waves.append(wave)
    return waves


def _resolve_output_format(stereo: bool) -> unreal.MetaSoundOutputAudioFormat:
    return _OUTPUT_FORMATS['Stereo' if stereo else 'Mono']


# ---------------------------------------------------------------------------
# Template 1: random one-shot as a preset of the engine reference source
# ---------------------------------------------------------------------------

def create_random_oneshot_preset(
        builder_name: str,
        wave_asset_paths: list[str],
        pitch_base_semitones: float = 0.0,
        pitch_random_spread_semitones: float = 0.0,
        volume_base: float = 1.0,
        volume_random_spread: float = 0.0,
        seed: int = -1) -> list[str]:
    """Create a random one-shot player as a preset of MSS_PlayRandom_Oneshot.

    Validates the caller's inputs, creates a source preset builder
    referencing the engine's reference asset, checks that asset still
    exposes the inputs it needs (unregistering the builder and raising if
    not), then overrides its graph-input defaults.

    Returns:
        The names of the graph inputs that were set (read back for parity
        via get_graph_input_default).
    """
    # -- validate before any engine write ---------------------------------
    _validate_new_builder_name(builder_name)
    waves = _validate_wave_assets(wave_asset_paths)
    if pitch_random_spread_semitones < 0.0:
        raise ValueError('pitch_random_spread_semitones must be >= 0.')
    if volume_random_spread < 0.0:
        raise ValueError('volume_random_spread must be >= 0.')
    if volume_base < 0.0:
        raise ValueError(
            'volume_base is a linear multiplier and must be >= 0.')
    reference = _core.load_asset_checked(
        ENGINE_RANDOM_ONESHOT_ASSET, unreal.MetaSoundSource)

    # -- build -------------------------------------------------------------
    subsystem = _core.builder_subsystem()
    builder, result = subsystem.create_source_preset_builder(
        builder_name, reference)
    _core.check_result(
        result, f"create source preset builder '{builder_name}' referencing "
        f'{ENGINE_RANDOM_ONESHOT_ASSET}')
    # Create does NOT register (live-verified 2026-10-23): find_builder
    # only sees builders explicitly registered with the subsystem.
    subsystem.register_builder(builder_name, builder)
    _core.track_builder(builder_name)

    input_names, result = builder.get_graph_input_names()
    _core.check_result(result, 'read preset graph input names')
    available = {str(name) for name in input_names}

    # Engine-asset input names, with the values we want on each. The asset's
    # name table verifies Sounds/PitchRandomSpread/VolumeRandomSpread; the
    # rest are matched defensively and skipped-with-error if absent.
    wanted: dict[str, unreal.MetasoundFrontendLiteral] = {
        'Sounds': _core.make_literal(asset_paths=wave_asset_paths),
    }
    if pitch_base_semitones != 0.0:
        wanted['PitchBase'] = _core.make_literal(
            float_value=pitch_base_semitones)
    if pitch_random_spread_semitones != 0.0:
        wanted['PitchRandomSpread'] = _core.make_literal(
            float_value=pitch_random_spread_semitones)
    if volume_base != 1.0:
        wanted['VolumeBase'] = _core.make_literal(float_value=volume_base)
    if volume_random_spread != 0.0:
        wanted['VolumeRandomSpread'] = _core.make_literal(
            float_value=volume_random_spread)
    if seed >= 0:
        wanted['Seed'] = _core.make_literal(int_value=seed)

    missing = sorted(set(wanted) - available)
    if missing:
        subsystem.unregister_builder(builder_name)
        _core.untrack_builder(builder_name)
        raise LookupError(
            f'Engine reference asset has no graph input(s) {missing}; its '
            f'inputs are {sorted(available)}. The engine asset layout '
            'changed - update the template mapping.')

    set_names: list[str] = []
    try:
        for name, literal in wanted.items():
            result = builder.set_graph_input_default(name, literal)
            _core.check_result(result, f"set preset input default '{name}'")
            set_names.append(name)
    except Exception:
        # Mirror the missing-inputs branch: a mid-loop failure must not
        # leave a registered half-configured builder behind (the module
        # promises a bad call leaves no half-built state).
        subsystem.unregister_builder(builder_name)
        _core.untrack_builder(builder_name)
        raise

    del waves  # loaded only to validate; literals hold their own refs
    return set_names


# ---------------------------------------------------------------------------
# Template 2: from-scratch wave player source (Builder substrate exercise)
# ---------------------------------------------------------------------------

def create_wave_player_source(
        builder_name: str,
        wave_asset_path: str,
        stereo: bool = True,
        loop: bool = False,
        gain_db: float = 0.0,
        pitch_shift_semitones: float = 0.0) -> dict:
    """Create a source that plays one wave with dB gain + pitch inputs.

    Graph: Wave Player -> Multiply (Audio by Float) per channel -> audio out,
    with 'GainDb' (through Decibels to Linear Gain) and 'PitchShift' exposed
    as graph inputs so the sound stays tweakable live and in Blueprint.

    Returns:
        Dict with the builder's tracked node ids by role, for follow-up
        edits ('wave_player', 'db_to_gain', 'multiplies': [...]).
    """
    # -- validate before any engine write ---------------------------------
    _validate_new_builder_name(builder_name)
    _validate_wave_assets([wave_asset_path])
    if not -96.0 <= gain_db <= 12.0:
        raise ValueError(
            f'gain_db {gain_db} outside the sane range [-96, +12].')
    if not -48.0 <= pitch_shift_semitones <= 48.0:
        raise ValueError(
            f'pitch_shift_semitones {pitch_shift_semitones} outside '
            '[-48, +48].')

    subsystem = _core.builder_subsystem()
    one_shot = not loop
    builder, on_play_out, on_finished_in, audio_outs, result = (
        subsystem.create_source_builder(
            builder_name,
            output_format=_resolve_output_format(stereo),
            is_one_shot=one_shot))
    _core.check_result(result, f"create source builder '{builder_name}'")
    # Create does NOT register; see create_random_oneshot_preset.
    subsystem.register_builder(builder_name, builder)
    _core.track_builder(builder_name)

    def _add_node(namespace: str, name: str, variant: str) -> tuple:
        class_name = unreal.MetasoundFrontendClassName()
        class_name.set_editor_property('namespace', namespace)
        class_name.set_editor_property('name', name)
        class_name.set_editor_property('variant', variant)
        handle, add_result = builder.add_node_by_class_name(class_name)
        _core.check_result(
            add_result, f"add node '{namespace}.{name}.{variant}'")
        node_id = _core.register_node_handle(
            builder_name, handle,
            '.'.join(p for p in (namespace, name, variant) if p))
        return handle, node_id

    def _input_by_name(handle, pin_name: str):
        pin, find_result = builder.find_node_input_by_name(handle, pin_name)
        _core.check_result(find_result, f"find node input '{pin_name}'")
        return pin

    def _inputs_by_type(handle, data_type: str) -> list:
        pins, find_result = builder.find_node_inputs_by_data_type(
            handle, data_type)
        _core.check_result(find_result, f'find {data_type} inputs')
        return list(pins)

    def _outputs_by_type(handle, data_type: str) -> list:
        pins, find_result = builder.find_node_outputs_by_data_type(
            handle, data_type)
        _core.check_result(find_result, f'find {data_type} outputs')
        return list(pins)

    def _connect(output_handle, input_handle, what: str,
                 from_node_id: str = '', to_node_id: str = '') -> None:
        """Connect two pins and record the edge in the session ledger.

        Pin names for the ledger are read back from the engine (ground
        truth); an empty node id marks a graph input/output member pin,
        per the ledger convention.
        """
        from_output, _, name_result = builder.get_node_output_data(
            output_handle)
        _core.check_result(name_result, f'read output pin name ({what})')
        to_input, _, name_result = builder.get_node_input_data(input_handle)
        _core.check_result(name_result, f'read input pin name ({what})')
        connect_result = builder.connect_nodes(output_handle, input_handle)
        _core.check_result(connect_result, f'connect {what}')
        _core.record_edge(builder_name, from_node_id, str(from_output),
                          to_node_id, str(to_input))

    def _build() -> dict:
        # -- wave player -------------------------------------------------------
        variant = 'Stereo' if stereo else 'Mono'
        wave_player, wave_player_id = _add_node('UE', 'Wave Player', variant)

        # Wave Asset default (located by data type: the only WaveAsset input).
        wave_inputs = _inputs_by_type(wave_player, 'WaveAsset')
        if not wave_inputs:
            raise RuntimeError('Wave Player exposes no WaveAsset input pin.')
        result = builder.set_node_input_default(
            wave_inputs[0], _core.make_literal(asset_path=wave_asset_path))
        _core.check_result(result, 'set Wave Player wave asset')

        if loop:
            loop_pin = _input_by_name(wave_player, 'Loop')
            result = builder.set_node_input_default(
                loop_pin, _core.make_literal(bool_value=True))
            _core.check_result(result, 'set Wave Player Loop = true')

        # OnPlay (graph input node output handle from builder creation) -> Play.
        _connect(on_play_out, _input_by_name(wave_player, 'Play'),
                 'OnPlay -> Wave Player Play', to_node_id=wave_player_id)

        # One-shots must fire OnFinished (the OneShot interface leak rule).
        if one_shot:
            finished_pin, result = builder.find_node_output_by_name(
                wave_player, 'On Finished')
            _core.check_result(result, "find Wave Player 'On Finished'")
            _connect(finished_pin, on_finished_in,
                     'Wave Player On Finished -> graph OnFinished',
                     from_node_id=wave_player_id)

        # -- pitch input -------------------------------------------------------
        pitch_input_out, result = builder.add_graph_input_node(
            'PitchShift', 'Float',
            _core.make_literal(float_value=pitch_shift_semitones))
        _core.check_result(result, "add graph input 'PitchShift'")
        _connect(pitch_input_out, _input_by_name(wave_player, 'Pitch Shift'),
                 'PitchShift -> Wave Player Pitch Shift',
                 to_node_id=wave_player_id)

        # -- gain path: GainDb -> Decibels to Linear Gain -> Multiply ----------
        gain_input_out, result = builder.add_graph_input_node(
            'GainDb', 'Float', _core.make_literal(float_value=gain_db))
        _core.check_result(result, "add graph input 'GainDb'")

        # Variant 'Float' is mandatory: the registered class is
        # UE.Decibels to Linear Gain.Float (live-verified 2026-10-23).
        db_to_gain, db_to_gain_id = _add_node(
            'UE', 'Decibels to Linear Gain', 'Float')
        db_in_pins = _inputs_by_type(db_to_gain, 'Float')
        db_out_pins = _outputs_by_type(db_to_gain, 'Float')
        if not db_in_pins or not db_out_pins:
            raise RuntimeError(
                'Decibels to Linear Gain exposes no Float input/output pin.')
        _connect(gain_input_out, db_in_pins[0], 'GainDb -> Decibels',
                 to_node_id=db_to_gain_id)

        wave_audio_outs = _outputs_by_type(wave_player, 'Audio')
        expected_channels = 2 if stereo else 1
        if len(wave_audio_outs) < expected_channels:
            raise RuntimeError(
                f'Wave Player ({variant}) exposes {len(wave_audio_outs)} audio '
                f'outputs, expected {expected_channels}.')
        if len(audio_outs) < expected_channels:
            raise RuntimeError(
                f'Source graph exposes {len(audio_outs)} audio outputs, '
                f'expected {expected_channels}.')

        multiply_ids: list[str] = []
        for channel in range(expected_channels):
            multiply, multiply_id = _add_node('UE', 'Multiply', 'Audio by Float')
            multiply_ids.append(multiply_id)
            audio_in = _inputs_by_type(multiply, 'Audio')
            float_in = _inputs_by_type(multiply, 'Float')
            audio_out = _outputs_by_type(multiply, 'Audio')
            if not audio_in or not float_in or not audio_out:
                raise RuntimeError(
                    'Multiply (Audio by Float) pin layout unexpected.')
            _connect(wave_audio_outs[channel], audio_in[0],
                     f'Wave Player ch{channel} -> Multiply',
                     from_node_id=wave_player_id, to_node_id=multiply_id)
            _connect(db_out_pins[0], float_in[0], 'Linear Gain -> Multiply',
                     from_node_id=db_to_gain_id, to_node_id=multiply_id)
            _connect(audio_out[0], audio_outs[channel],
                     f'Multiply ch{channel} -> graph audio out',
                     from_node_id=multiply_id)

        return {
            'wave_player': wave_player_id,
            'db_to_gain': db_to_gain_id,
            'multiplies': multiply_ids,
        }

    try:
        return _build()
    except Exception:
        # Mirror the preset template (house F11 fix, bfa1c2b): a
        # mid-build failure must not leave a registered half-built
        # builder behind - the module promises a bad call leaves
        # no half-built state.
        subsystem.unregister_builder(builder_name)
        _core.untrack_builder(builder_name)
        raise
