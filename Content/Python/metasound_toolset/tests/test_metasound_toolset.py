"""Tests for the MetaSounds toolset, per Epic's bar: a test per tool,
success path + every raise path that is reachable without external state.

These run inside the editor (ToolCallTestCase comes from the engine's
toolset_registry package). Tool calls raise through RuntimeError inside
assertToolRaisesRuntimeError, with the original exception type named in
the message - assertions check for those names.

Audition tests make brief, quiet noise if an audio device is present; they
assert session mechanics (status/stop/read-back), not audibility, so they
also pass under -NullRHI.
"""

import asyncio
import json
import os
import time
import unittest
import uuid

import unreal

from toolset_registry.tests.toolset_testcase import ToolCallTestCase

from metasound_toolset import _core, node_catalog, rendering, surface
from metasound_toolset.toolset import MetaSoundsToolset as TS

# A SoundWave that ships with every engine install.
ENGINE_WAVE = '/Engine/EngineSounds/WhiteNoise'


def _unique(prefix: str) -> str:
    return f'{prefix}_{uuid.uuid4().hex[:8]}'


def _scratch_root() -> str:
    """A folder inside the project, where analyze_rendered_wav may read."""
    saved = unreal.Paths.convert_relative_path_to_full(
        unreal.Paths.project_saved_dir())
    root = os.path.join(saved, 'MetaSoundToolset', 'TestScratch')
    os.makedirs(root, exist_ok=True)
    return root


def _engine_wave_available() -> bool:
    try:
        asset = unreal.EditorAssetLibrary.find_asset_data(ENGINE_WAVE)
        return bool(asset.is_valid())
    except Exception:
        return False


#: Why a few tests skip on a project with the AssetReferenceRestrictions
#: plugin on (it is off by default and comes in through GameFeatures):
#: saving a MetaSound that references another under the suite's temporary
#: /MSTAutomation mount trips the plugin's validator ('Could not find a
#: matching domain'), and the error it logs fails the test. That is the
#: validator refusing a mount it has no domain for, not the toolset, and
#: product paths under /Game are unaffected. Observed on one test project, 195/3
#: at the same commit that reads 198/0 elsewhere (cold read C10).
_REFERENCE_RESTRICTIONS_REASON = (
    'AssetReferenceRestrictions is on in this project and rejects a saved '
    'reference between assets under the temporary test mount; the guard '
    'under test is unaffected.')


def _reference_restrictions_block_the_test_mount() -> bool:
    # The plugin's settings and subsystem classes are not reflected into
    # Python (probed 2026-11-02 on 5.8.4); its validator class is. Its
    # enabled flag is read when the binding exposes it, else the loaded
    # class is taken to mean the validator runs.
    validator = getattr(unreal, 'AssetValidator_AssetReferenceRestrictions',
                        None)
    if validator is None:
        return False
    try:
        return bool(unreal.get_default_object(validator)
                    .get_editor_property('is_enabled'))
    except Exception:
        return True


async def _settle_exports(seconds: float = 15.0) -> bool:
    """Yield real editor ticks until no export holds the engine writer.

    Exports run one at a time (verify-cold R4) and are released on editor
    ticks, which a test body blocks. Awaiting here lets the editor tick:
    the recording cap's post-tick callback then releases the writer on the
    engine's own completion, or on the no-file rule, exactly as it does
    outside a test. Nothing here writes an export's state (micro-delta M4:
    the helper this replaced marked a real export acknowledged once its
    file was complete, the very assumption the engine's completion
    replaced). Once nothing records, the callback is given the ticks to
    release itself. True once the writer is free.
    """
    deadline = time.monotonic() + seconds
    while (rendering._export_in_flight is not None
           and time.monotonic() < deadline):
        await asyncio.sleep(0.05)
    while (rendering._cap_handle is not None and not rendering._recordings
           and rendering._export_in_flight is None
           and time.monotonic() < deadline + 1.0):
        await asyncio.sleep(0.02)
    return rendering._export_in_flight is None


class MetaSoundToolsetTestCase(ToolCallTestCase,
                               unittest.IsolatedAsyncioTestCase):
    """Base: releases builders/auditions/recordings created during each test.

    Asyncio-based (Epic's runner drives it on the editor thread), so
    teardown can yield real editor ticks to the engine work a test started:
    an export's completion only ever arrives on one.
    """

    def setUp(self):
        self._builders: list[str] = []
        # These tests also run inside a working editor (the
        # AutomationTestToolset route), beside recordings that are not
        # theirs. The sessions this test starts (_start_recording,
        # _start_render), held as objects the way builders are held by name,
        # are the only ones teardown touches.
        self._owned_recordings: list = []

    async def asyncTearDown(self):
        TS.stop_all_auditions()
        await _settle_exports()
        # A finish refused while the writer was held leaves its recording
        # running and claiming its submix; finish what this test started.
        # Ownership, checked by identity (micro-delta N3): set differences of
        # names finished a recording someone else started during the test's
        # awaits, and took the cap report of one that was running at setUp.
        for session in self._owned_recordings:
            if not self._still_listed(session):
                continue
            try:
                rendering.finish_recording(session.name)
            except Exception:
                pass
            await _settle_exports()
        for session in self._owned_recordings:
            self._drop_cap_report(session)

    def _owning(self, name: str, start):
        """Run a start, owning the session it created under this name.

        Owned in a finally, so a start that creates its recording and then
        raises is owned too: start_source_render leaves its recording listed
        when the audition fails while another export holds the writer
        (micro-delta MD3-2). Only a session that was not listed under the
        name before the start is taken: a refusal before creation leaves
        the earlier one, or none, and the test owns nothing. The start runs
        without an await, so nothing else can list a session in between.
        """
        key = rendering._name_key(name)
        before = rendering._recordings.get(key)
        try:
            return start()
        finally:
            session = rendering._recordings.get(key)
            if session is not None and session is not before and not any(
                    s is session for s in self._owned_recordings):
                self._owned_recordings.append(session)

    def _start_recording(self, name: str, *args, **kwargs):
        """TS.start_output_recording, owned by this test."""
        return self._owning(
            name, lambda: TS.start_output_recording(name, *args, **kwargs))

    def _start_render(self, builder_name: str, *args, **kwargs):
        """TS.start_source_render, its recording owned by this test."""
        wav_name = kwargs.get('wav_name', args[0] if args else '')
        return self._owning(
            wav_name or builder_name,
            lambda: TS.start_source_render(builder_name, *args, **kwargs))

    @staticmethod
    def _still_listed(session) -> bool:
        """The session itself is still recording, not another of its name."""
        return rendering._recordings.get(
            rendering._name_key(session.name)) is session

    @staticmethod
    def _drop_cap_report(session) -> None:
        """Forget the cap report made from this session, and no other."""
        key = rendering._name_key(session.name)
        report = rendering._capped.get(key)
        if report is not None and len(report) > 2 and report[2] is session:
            del rendering._capped[key]

    def tearDown(self):
        for name in self._builders:
            try:
                TS.release_builder(name)
            except Exception:
                pass

    def _source(self, **kwargs) -> str:
        name = _unique('t_src')
        TS.create_metasound_source(name, **kwargs)
        self._builders.append(name)
        return name

    def _patch(self) -> str:
        name = _unique('t_patch')
        TS.create_metasound_patch(name)
        self._builders.append(name)
        return name


class TestDiscovery(MetaSoundToolsetTestCase):

    def test_search_node_classes_finds_gain_idiom(self):
        results = TS.search_node_classes('gain db', 5)
        displays = [r.display for r in results]
        self.assertTrue(
            any('Decibels to Linear Gain' in d for d in displays), displays)

    def test_search_node_classes_reports_verification(self):
        results = TS.search_node_classes('wave player', 3)
        self.assertTrue(results)
        self.assertIn(results[0].verification_status,
                      ('verified', 'doc', 'unverified'))

    def test_search_node_classes_raises_on_empty_query(self):
        with self.assertToolRaisesRuntimeError() as cm:
            TS.search_node_classes('')
        self.assertIn('ValueError', str(cm.exception))

    def test_search_node_classes_raises_on_bad_max_results(self):
        with self.assertToolRaisesRuntimeError() as cm:
            TS.search_node_classes('gain', 0)
        self.assertIn('ValueError', str(cm.exception))

    def test_list_metasounds_runs(self):
        listed = TS.list_metasounds('/Game')
        self.assertIsInstance(list(listed), list)

    def test_list_metasounds_raises_on_relative_path(self):
        with self.assertToolRaisesRuntimeError() as cm:
            TS.list_metasounds('Game/Audio')
        self.assertIn('ValueError', str(cm.exception))

    def test_search_node_classes_carries_major_version(self):
        results = TS.search_node_classes('biquad filter', 3)
        self.assertTrue(results)
        biquad = next(r for r in results if r.name == 'Biquad Filter')
        self.assertEqual(biquad.major_version, 2)

    def test_search_finds_crossfade_under_its_real_class_name(self):
        results = TS.search_node_classes('crossfade', 2)
        self.assertEqual({r.namespace for r in results}, {'Crossfade'})
        self.assertEqual({r.variant for r in results}, {'Audio', 'Float'})
        for result in results:
            self.assertEqual(result.verification_status, 'verified')

    def test_search_trigger_route_prefers_the_real_router(self):
        # Crossfade's class name reads 'Trigger Route (...)' (engine quirk);
        # a router query must not surface it first.
        results = TS.search_node_classes('trigger route', 3)
        self.assertEqual(results[0].namespace, 'TriggerRoute')

    def test_search_keeps_get_wave_duration_lone_space_variant(self):
        results = TS.search_node_classes('wave info', 3)
        wave_info = next(r for r in results if r.name == 'Get Wave Duration')
        self.assertEqual(wave_info.variant, ' ')

    def test_catalog_statuses_stay_in_the_documented_vocabulary(self):
        statuses = {e['verification']['status']
                    for e in node_catalog.load_catalog()}
        self.assertLessEqual(statuses, {'verified', 'doc', 'unverified'})

    def test_resolve_envelope_families_use_their_name_as_namespace(self):
        for family, trigger_pin in (('AD Envelope', 'Trigger'),
                                    ('ADSR Envelope', 'Trigger Attack')):
            for variant in ('Audio', 'Float'):
                info = TS.resolve_node_class(family, family, variant)
                self.assertIn(trigger_pin, [p.name for p in info.inputs])
                envelope = next(p for p in info.outputs
                                if p.name == 'Out Envelope')
                self.assertEqual(envelope.data_type, variant)

    def test_resolve_crossfade_covers_two_to_eight_inputs(self):
        for variant in ('Audio', 'Float'):
            for count in (2, 8):
                info = TS.resolve_node_class(
                    'Crossfade', f'Trigger Route ({variant}, {count})',
                    variant)
                in_names = [p.name for p in info.inputs]
                self.assertIn('Crossfade Value', in_names)
                self.assertIn(f'In {count - 1}', in_names)

    def test_resolve_crossfade_raises_past_eight_inputs(self):
        unreal.AutomationLibrary.add_expected_plain_log_error(
            "Failed to add new node by class name "
            "'Crossfade.Trigger Route (Float, 9).Float'", 1)
        with self.assertToolRaisesRuntimeError() as cm:
            TS.resolve_node_class('Crossfade', 'Trigger Route (Float, 9)',
                                  'Float')
        self.assertIn('LookupError', str(cm.exception))

    def test_resolve_get_wave_duration_needs_the_lone_space_variant(self):
        info = TS.resolve_node_class('UE', 'Get Wave Duration', ' ')
        self.assertEqual({p.name: p.data_type for p in info.outputs},
                         {'Duration': 'Time', 'Name': 'String',
                          'Path': 'String'})

    def test_resolve_get_wave_duration_raises_on_empty_variant(self):
        unreal.AutomationLibrary.add_expected_plain_log_error(
            "Failed to add new node by class name "
            "'UE.Get Wave Duration'", 1)
        with self.assertToolRaisesRuntimeError() as cm:
            TS.resolve_node_class('UE', 'Get Wave Duration', '')
        self.assertIn('LookupError', str(cm.exception))

    def test_resolve_biquad_filter_at_major_two(self):
        # Registered at major version 2 in 5.8 (headless probe 2026-10-23).
        info = TS.resolve_node_class('UE', 'Biquad Filter', 'Audio',
                                     major_version=2)
        self.assertTrue(any(p.data_type == 'Audio' for p in info.inputs))

    def test_resolve_clamp_family_spelling(self):
        # Family-token convention: namespace == name == 'Clamp'.
        info = TS.resolve_node_class('Clamp', 'Clamp', 'Float')
        in_names = [p.name for p in info.inputs]
        self.assertTrue(any('Min' in n for n in in_names), in_names)

    def test_resolve_node_class_verifies_real_node(self):
        info = TS.resolve_node_class('UE', 'Decibels to Linear Gain', 'Float')
        in_types = [p.data_type for p in info.inputs]
        out_types = [p.data_type for p in info.outputs]
        self.assertIn('Float', in_types)
        self.assertIn('Float', out_types)

    def test_resolve_node_class_raises_on_fiction(self):
        # The engine logs an Error for the failed registry lookup; expected
        # errors keep the automation controller from failing the test.
        unreal.AutomationLibrary.add_expected_plain_log_error(
            "Failed to add new node by class name "
            "'UE.Definitely Not A Node'", 1)
        with self.assertToolRaisesRuntimeError() as cm:
            TS.resolve_node_class('UE', 'Definitely Not A Node')
        self.assertIn('LookupError', str(cm.exception))

    def test_resolve_node_class_raises_on_empty_name(self):
        with self.assertToolRaisesRuntimeError() as cm:
            TS.resolve_node_class('UE', '')
        self.assertIn('ValueError', str(cm.exception))

    def test_describe_metasound_raises_on_missing_asset(self):
        with self.assertToolRaisesRuntimeError() as cm:
            TS.describe_metasound('/Game/__nope__/NoSuchMetaSound')
        self.assertIn('FileNotFoundError', str(cm.exception))

    def test_describe_metasound_raises_on_non_metasound(self):
        if not _engine_wave_available():
            self.skipTest(f'{ENGINE_WAVE} not available')
        with self.assertToolRaisesRuntimeError() as cm:
            TS.describe_metasound(ENGINE_WAVE)
        self.assertIn('TypeError', str(cm.exception))

    def test_describe_metasound_reads_engine_reference_source(self):
        info = TS.describe_metasound(
            '/Metasound/DefaultAssets/Sources/MSS_PlayRandom_Oneshot')
        self.assertEqual(info.metasound_type, 'Source')
        input_names = [m.name for m in info.inputs]
        self.assertIn('Sounds', input_names)


class TestBuilderLifecycle(MetaSoundToolsetTestCase):

    def test_create_source_has_interface_members(self):
        name = self._source(output_format='Stereo', one_shot=True)
        info = TS.get_graph_info(name)
        self.assertEqual(info.metasound_type, 'Source')
        input_names = [m.name for m in info.inputs]
        self.assertTrue(
            any('OnPlay' in n for n in input_names), input_names)
        self.assertTrue(TS.has_interface(name, 'UE.Source.OneShot'))

    def test_create_source_loop_has_no_oneshot_interface(self):
        name = self._source(output_format='Mono', one_shot=False)
        self.assertFalse(TS.has_interface(name, 'UE.Source.OneShot'))

    def test_create_source_raises_on_bad_format(self):
        with self.assertToolRaisesRuntimeError() as cm:
            TS.create_metasound_source(_unique('t'), output_format='Surround')
        self.assertIn('ValueError', str(cm.exception))

    def test_create_raises_on_duplicate_name(self):
        name = self._source()
        with self.assertToolRaisesRuntimeError() as cm:
            TS.create_metasound_patch(name)
        self.assertIn('ValueError', str(cm.exception))

    def test_create_patch_and_list_builders(self):
        name = self._patch()
        self.assertIn(name, list(TS.list_builders()))
        info = TS.get_graph_info(name)
        self.assertEqual(info.metasound_type, 'Patch')

    def test_release_builder_forgets(self):
        name = _unique('t_rel')
        TS.create_metasound_patch(name)
        TS.release_builder(name)
        self.assertNotIn(name, list(TS.list_builders()))

    def test_release_unknown_builder_raises(self):
        with self.assertToolRaisesRuntimeError() as cm:
            TS.release_builder('never_created')
        self.assertIn('LookupError', str(cm.exception))

    def test_get_graph_info_unknown_builder_raises(self):
        with self.assertToolRaisesRuntimeError() as cm:
            TS.get_graph_info('never_created')
        self.assertIn('LookupError', str(cm.exception))

    def test_attach_builder_raises_on_missing_asset(self):
        with self.assertToolRaisesRuntimeError() as cm:
            TS.attach_builder_to_asset(
                _unique('t'), '/Game/__nope__/NoSuchAsset')
        self.assertIn('FileNotFoundError', str(cm.exception))

    def test_builder_keys_fold_ascii_case_only(self):
        # Cold read C1 (2026-11-02): FName folds the 26 ASCII letters and
        # nothing else, so each of these pairs is TWO live builders. One
        # session key for both (str.casefold) let the second overwrite the
        # first's records, and the cycle guard then asked about the wrong
        # asset: a stack overflow, editor dead.
        suffix = uuid.uuid4().hex[:8]
        for one, two in (('äx', 'Äx'), ('év', 'Év'),
                         ('ßy', 'ssy'), ('ſz', 'sz'),
                         ('Kw', 'kw')):
            first, second = f'{one}_{suffix}', f'{two}_{suffix}'
            self.assertEqual(first.casefold(), second.casefold())  # the trap
            self.assertNotEqual(
                _core.builder_key(first), _core.builder_key(second))
            TS.create_metasound_patch(first)
            self._builders.append(first)
            TS.create_metasound_patch(second)
            self._builders.append(second)
            listed = list(TS.list_builders())
            self.assertIn(first, listed)
            self.assertIn(second, listed)
        # ASCII case still folds, around the non-ASCII letter.
        self.assertEqual(_core.builder_key('ÄX'), _core.builder_key('Äx'))

    def test_engine_empty_name_aliases_are_refused(self):
        # Verify-cold R1 (2026-11-02): FName drops a trailing _<digits> and
        # an empty base is NAME_None, as is 'None' in any casing, so these
        # are ONE engine builder slot whose ASCII folds all differ.
        for alias in ('_0', '_1', '_12', 'None', 'none', 'NONE'):
            with self.assertToolRaisesRuntimeError() as cm:
                TS.create_metasound_patch(alias)
            self.assertIn('ValueError', str(cm.exception))
            self.assertIn('no name to the engine', str(cm.exception))
            with self.assertToolRaisesRuntimeError() as cm:
                TS.create_metasound_source(alias)
            self.assertIn('ValueError', str(cm.exception))
            for lookup in (TS.get_graph_info, TS.release_builder,
                           TS.save_builder_asset):
                with self.assertToolRaisesRuntimeError() as cm:
                    lookup(alias)
                self.assertIn('ValueError', str(cm.exception))
            self.assertNotIn(alias, list(TS.list_builders()))

    def test_number_suffix_keys_follow_the_engine(self):
        # The key is the engine's FName: case still folds across the
        # number suffix, and a different number is a different builder.
        base = f'k{uuid.uuid4().hex[:8]}'
        one, one_upper, two = f'{base}_1', f'{base.upper()}_1', f'{base}_2'
        self.assertEqual(_core.builder_key(one), _core.builder_key(one_upper))
        self.assertNotEqual(_core.builder_key(one), _core.builder_key(two))
        TS.create_metasound_patch(one)
        self._builders.append(one)
        with self.assertToolRaisesRuntimeError() as cm:
            TS.create_metasound_patch(one_upper)
        self.assertIn('already exists', str(cm.exception))
        TS.create_metasound_patch(two)
        self._builders.append(two)
        node = TS.add_node(
            one_upper, 'UE', 'Decibels to Linear Gain', 'Float')
        self.assertIn(node.node_id, _core.tracked_node_ids(one))
        self.assertNotIn(node.node_id, _core.tracked_node_ids(two))


class TestNodesAndWiring(MetaSoundToolsetTestCase):

    def test_add_node_returns_pins(self):
        name = self._patch()
        node = TS.add_node(name, 'UE', 'Decibels to Linear Gain', 'Float')
        self.assertTrue(node.node_id)
        self.assertTrue(len(node.inputs) >= 1)
        self.assertTrue(len(node.outputs) >= 1)

    def test_add_node_raises_on_fiction(self):
        name = self._patch()
        unreal.AutomationLibrary.add_expected_plain_log_error(
            "Failed to add new node by class name "
            "'UE.Definitely Not A Node'", 1)
        with self.assertToolRaisesRuntimeError() as cm:
            TS.add_node(name, 'UE', 'Definitely Not A Node')
        self.assertIn('LookupError', str(cm.exception))

    def test_describe_node_roundtrip(self):
        name = self._patch()
        node = TS.add_node(name, 'UE', 'Decibels to Linear Gain', 'Float')
        described = TS.describe_node(name, node.node_id)
        self.assertEqual(
            [p.name for p in node.inputs],
            [p.name for p in described.inputs])

    def test_describe_node_raises_on_unknown_id(self):
        name = self._patch()
        with self.assertToolRaisesRuntimeError() as cm:
            TS.describe_node(name, 'not-a-node-id')
        self.assertIn('LookupError', str(cm.exception))

    def test_remove_node_then_describe_raises(self):
        name = self._patch()
        node = TS.add_node(name, 'UE', 'Decibels to Linear Gain', 'Float')
        TS.remove_node(name, node.node_id)
        with self.assertToolRaisesRuntimeError() as cm:
            TS.describe_node(name, node.node_id)
        self.assertIn('LookupError', str(cm.exception))

    def test_connect_and_disconnect_float_pins(self):
        name = self._patch()
        db_a = TS.add_node(name, 'UE', 'Decibels to Linear Gain', 'Float')
        db_b = TS.add_node(name, 'UE', 'Decibels to Linear Gain', 'Float')
        out_pin = db_a.outputs[0].name
        in_pin = db_b.inputs[0].name
        TS.connect_nodes(name, db_a.node_id, out_pin, db_b.node_id, in_pin)
        described = TS.describe_node(name, db_b.node_id)
        self.assertTrue(described.inputs[0].is_connected)
        TS.disconnect_nodes(
            name, db_a.node_id, out_pin, db_b.node_id, in_pin)
        described = TS.describe_node(name, db_b.node_id)
        self.assertFalse(described.inputs[0].is_connected)

    def test_connect_raises_on_type_mismatch(self):
        name = self._patch()
        db = TS.add_node(name, 'UE', 'Decibels to Linear Gain', 'Float')
        multiply = TS.add_node(name, 'UE', 'Multiply', 'Audio by Float')
        audio_in = next(
            p.name for p in multiply.inputs if p.data_type == 'Audio')
        with self.assertToolRaisesRuntimeError():
            TS.connect_nodes(
                name, db.node_id, db.outputs[0].name,
                multiply.node_id, audio_in)

    def test_connect_raises_on_bad_pin_name(self):
        name = self._patch()
        db_a = TS.add_node(name, 'UE', 'Decibels to Linear Gain', 'Float')
        db_b = TS.add_node(name, 'UE', 'Decibels to Linear Gain', 'Float')
        with self.assertToolRaisesRuntimeError():
            TS.connect_nodes(
                name, db_a.node_id, 'No Such Pin',
                db_b.node_id, db_b.inputs[0].name)

    def test_set_node_input_default_reads_back(self):
        name = self._patch()
        db = TS.add_node(name, 'UE', 'Decibels to Linear Gain', 'Float')
        float_in = next(
            p.name for p in db.inputs if p.data_type == 'Float')
        value = TS.set_node_input_default(
            name, db.node_id, float_in, float_value=-6.0)
        self.assertAlmostEqual(value.float_value, -6.0, places=4)
        read = TS.get_node_input_default(name, db.node_id, float_in)
        self.assertAlmostEqual(read.float_value, -6.0, places=4)

    def test_set_node_input_default_raises_without_value(self):
        name = self._patch()
        db = TS.add_node(name, 'UE', 'Decibels to Linear Gain', 'Float')
        with self.assertToolRaisesRuntimeError() as cm:
            TS.set_node_input_default(name, db.node_id, db.inputs[0].name)
        self.assertIn('ValueError', str(cm.exception))

    def test_set_node_input_default_raises_on_two_values(self):
        name = self._patch()
        db = TS.add_node(name, 'UE', 'Decibels to Linear Gain', 'Float')
        with self.assertToolRaisesRuntimeError() as cm:
            TS.set_node_input_default(
                name, db.node_id, db.inputs[0].name,
                float_value=1.0, int_value=2)
        self.assertIn('ValueError', str(cm.exception))

    def test_describe_node_shows_class_defaults(self):
        # Review F8 / drill MEDIUM-HIGH: fresh pins read '' for the graph
        # override (correct - none is set) but the class default must be
        # visible or agents build blind.
        name = self._patch()
        wave = TS.add_node(name, 'UE', 'Wave Player', 'Mono')
        loop = next(p for p in wave.inputs if p.name == 'Loop')
        self.assertEqual(loop.default_value, '')
        self.assertEqual(loop.class_default_value, 'false')
        TS.set_node_input_default(name, wave.node_id, 'Loop',
                                  bool_value=True)
        described = TS.describe_node(name, wave.node_id)
        loop = next(p for p in described.inputs if p.name == 'Loop')
        self.assertEqual(loop.default_value, 'true')
        self.assertEqual(loop.class_default_value, 'false')

    def test_resolve_node_class_reports_class_defaults(self):
        info = TS.resolve_node_class('UE', 'Wave Player', 'Mono')
        loop = next(p for p in info.inputs if p.name == 'Loop')
        self.assertEqual(loop.class_default_value, 'false')

    def test_set_node_input_default_bool_reads_back(self):
        # The drill's release blocker: Bool pins crashed all three default
        # tools (5.8's GetBoolValueFromLiteral binding has no value
        # channel). Wave Player's 'Loop' pin is the canonical Bool pin.
        name = self._patch()
        wave = TS.add_node(name, 'UE', 'Wave Player', 'Mono')
        value = TS.set_node_input_default(
            name, wave.node_id, 'Loop', bool_value=True)
        self.assertEqual(value.literal_type, 'BOOLEAN')
        self.assertTrue(value.bool_value)
        read = TS.get_node_input_default(name, wave.node_id, 'Loop')
        self.assertTrue(read.bool_value)
        value = TS.set_node_input_default(
            name, wave.node_id, 'Loop', bool_value=False)
        self.assertFalse(value.bool_value)

    def test_get_node_input_default_raises_when_unset(self):
        name = self._patch()
        db = TS.add_node(name, 'UE', 'Decibels to Linear Gain', 'Float')
        with self.assertToolRaisesRuntimeError() as cm:
            TS.get_node_input_default(name, db.node_id, db.inputs[0].name)
        self.assertIn('LookupError', str(cm.exception))


class TestGraphIO(MetaSoundToolsetTestCase):

    def test_add_graph_input_with_default_reads_back(self):
        name = self._patch()
        member = TS.add_graph_input(
            name, 'TestGain', 'Float', float_value=-12.0)
        self.assertEqual(member.name, 'TestGain')
        value = TS.get_graph_input_default(name, 'TestGain')
        self.assertAlmostEqual(value.float_value, -12.0, places=4)

    def test_add_graph_input_trigger_without_default(self):
        name = self._patch()
        member = TS.add_graph_input(name, 'TestTrig', 'Trigger')
        self.assertEqual(member.data_type, 'Trigger')

    def test_add_graph_input_raises_on_bad_data_type(self):
        name = self._patch()
        unreal.AutomationLibrary.add_expected_plain_log_error(
            'Data type is not registered [Name:NotADataType]', 1)
        unreal.AutomationLibrary.add_expected_plain_log_error(
            "'NotADataType' is not a registered DataType", 1)
        with self.assertToolRaisesRuntimeError() as cm:
            TS.add_graph_input(name, 'Bad', 'NotADataType')
        self.assertIn('RuntimeError', str(cm.exception))

    def test_set_graph_input_default_parity(self):
        name = self._patch()
        TS.add_graph_input(name, 'Rate', 'Float', float_value=1.0)
        value = TS.set_graph_input_default(name, 'Rate', float_value=2.5)
        self.assertAlmostEqual(value.float_value, 2.5, places=4)
        read = TS.get_graph_input_default(name, 'Rate')
        self.assertAlmostEqual(read.float_value, 2.5, places=4)

    def test_bool_graph_input_default_parity(self):
        name = self._patch()
        member = TS.add_graph_input(name, 'Flag', 'Bool', bool_value=True)
        self.assertEqual(member.default_value, 'true')
        read = TS.get_graph_input_default(name, 'Flag')
        self.assertEqual(read.literal_type, 'BOOLEAN')
        self.assertTrue(read.bool_value)
        value = TS.set_graph_input_default(name, 'Flag', bool_value=False)
        self.assertFalse(value.bool_value)
        read = TS.get_graph_input_default(name, 'Flag')
        self.assertFalse(read.bool_value)

    def test_set_graph_input_default_raises_on_unknown_name(self):
        name = self._patch()
        with self.assertToolRaisesRuntimeError():
            TS.set_graph_input_default(name, 'NoSuchInput', float_value=1.0)

    def test_remove_graph_input(self):
        name = self._patch()
        TS.add_graph_input(name, 'Doomed', 'Float', float_value=0.0)
        TS.remove_graph_input(name, 'Doomed')
        input_names = [m.name for m in TS.get_graph_info(name).inputs]
        self.assertNotIn('Doomed', input_names)

    def test_remove_graph_input_raises_on_unknown(self):
        name = self._patch()
        with self.assertToolRaisesRuntimeError():
            TS.remove_graph_input(name, 'NeverExisted')

    def test_add_and_remove_graph_output(self):
        name = self._patch()
        member = TS.add_graph_output(name, 'OutValue', 'Float')
        self.assertEqual(member.data_type, 'Float')
        output_names = [m.name for m in TS.get_graph_info(name).outputs]
        self.assertIn('OutValue', output_names)
        TS.remove_graph_output(name, 'OutValue')
        output_names = [m.name for m in TS.get_graph_info(name).outputs]
        self.assertNotIn('OutValue', output_names)

    def test_connect_from_graph_input(self):
        name = self._patch()
        TS.add_graph_input(name, 'InDb', 'Float', float_value=0.0)
        db = TS.add_node(name, 'UE', 'Decibels to Linear Gain', 'Float')
        float_in = next(
            p.name for p in db.inputs if p.data_type == 'Float')
        TS.connect_from_graph_input(name, 'InDb', db.node_id, float_in)
        described = TS.describe_node(name, db.node_id)
        pin = next(p for p in described.inputs if p.name == float_in)
        self.assertTrue(pin.is_connected)

    def test_connect_to_graph_output(self):
        name = self._patch()
        TS.add_graph_output(name, 'OutGain', 'Float')
        db = TS.add_node(name, 'UE', 'Decibels to Linear Gain', 'Float')
        float_out = next(
            p.name for p in db.outputs if p.data_type == 'Float')
        TS.connect_to_graph_output(name, db.node_id, float_out, 'OutGain')
        described = TS.describe_node(name, db.node_id)
        pin = next(p for p in described.outputs if p.name == float_out)
        self.assertTrue(pin.is_connected)

    def test_node_ids_are_session_counters(self):
        name = self._patch()
        first = TS.add_node(name, 'UE', 'Decibels to Linear Gain', 'Float')
        second = TS.add_node(name, 'UE', 'Decibels to Linear Gain', 'Float')
        self.assertEqual(first.node_id, 'n1')
        self.assertEqual(second.node_id, 'n2')
        info = TS.get_graph_info(name)
        self.assertEqual(list(info.tracked_node_ids), ['n1', 'n2'])
        self.assertEqual(dict(info.tracked_node_classes)['n1'],
                         'UE.Decibels to Linear Gain.Float')

    def test_removed_node_id_is_not_reused(self):
        name = self._patch()
        first = TS.add_node(name, 'UE', 'Decibels to Linear Gain', 'Float')
        TS.remove_node(name, first.node_id)
        second = TS.add_node(name, 'UE', 'Decibels to Linear Gain', 'Float')
        self.assertEqual(second.node_id, 'n2')

    def test_connect_records_edge_and_disconnect_forgets(self):
        name = self._patch()
        db_a = TS.add_node(name, 'UE', 'Decibels to Linear Gain', 'Float')
        db_b = TS.add_node(name, 'UE', 'Decibels to Linear Gain', 'Float')
        out_pin = db_a.outputs[0].name
        in_pin = db_b.inputs[0].name
        TS.connect_nodes(name, db_a.node_id, out_pin, db_b.node_id, in_pin)
        edges = TS.get_graph_info(name).edges
        self.assertEqual(len(edges), 1)
        self.assertEqual(edges[0].from_node_id, db_a.node_id)
        self.assertEqual(edges[0].from_output, out_pin)
        self.assertEqual(edges[0].to_node_id, db_b.node_id)
        self.assertEqual(edges[0].to_input, in_pin)
        TS.disconnect_nodes(
            name, db_a.node_id, out_pin, db_b.node_id, in_pin)
        self.assertEqual(len(TS.get_graph_info(name).edges), 0)

    def test_graph_member_edges_use_empty_node_id(self):
        name = self._patch()
        TS.add_graph_input(name, 'InDb', 'Float', float_value=0.0)
        TS.add_graph_output(name, 'OutGain', 'Float')
        db = TS.add_node(name, 'UE', 'Decibels to Linear Gain', 'Float')
        float_in = next(
            p.name for p in db.inputs if p.data_type == 'Float')
        float_out = next(
            p.name for p in db.outputs if p.data_type == 'Float')
        TS.connect_from_graph_input(name, 'InDb', db.node_id, float_in)
        TS.connect_to_graph_output(name, db.node_id, float_out, 'OutGain')
        edges = TS.get_graph_info(name).edges
        self.assertEqual(len(edges), 2)
        from_member = next(e for e in edges if e.from_node_id == '')
        self.assertEqual(from_member.from_output, 'InDb')
        self.assertEqual(from_member.to_node_id, db.node_id)
        to_member = next(e for e in edges if e.to_node_id == '')
        self.assertEqual(to_member.to_input, 'OutGain')
        TS.remove_graph_input(name, 'InDb')
        edges = TS.get_graph_info(name).edges
        self.assertEqual(len(edges), 1)
        self.assertEqual(edges[0].to_input, 'OutGain')

    def test_edges_record_engine_names_and_forget_case_aliases(self):
        # Review pass-2 N6: pin names are FNames to the engine, so a
        # connect spelled one way and a disconnect spelled another both
        # succeed there - the ledger must record the engine's spelling
        # and forget the edge on the alias, not advertise dead wiring.
        name = self._patch()
        db_a = TS.add_node(name, 'UE', 'Decibels to Linear Gain', 'Float')
        db_b = TS.add_node(name, 'UE', 'Decibels to Linear Gain', 'Float')
        out_pin = db_a.outputs[0].name
        in_pin = db_b.inputs[0].name
        TS.connect_nodes(name, db_a.node_id, out_pin.upper(),
                         db_b.node_id, in_pin.lower())
        edges = TS.get_graph_info(name).edges
        self.assertEqual(len(edges), 1)
        self.assertEqual(edges[0].from_output, out_pin)
        self.assertEqual(edges[0].to_input, in_pin)
        TS.disconnect_nodes(name, db_a.node_id, out_pin.lower(),
                            db_b.node_id, in_pin.upper())
        self.assertEqual(len(TS.get_graph_info(name).edges), 0)

    def test_graph_member_edges_forget_case_aliases(self):
        # N6, member half: remove_graph_input('pitchshift') removes the
        # engine's 'PitchShift' and its wiring; the ledger must follow.
        name = self._patch()
        TS.add_graph_input(name, 'PitchShift', 'Float', float_value=0.0)
        TS.add_graph_output(name, 'OutGain', 'Float')
        db = TS.add_node(name, 'UE', 'Decibels to Linear Gain', 'Float')
        float_in = next(
            p.name for p in db.inputs if p.data_type == 'Float')
        float_out = next(
            p.name for p in db.outputs if p.data_type == 'Float')
        TS.connect_from_graph_input(name, 'PITCHSHIFT', db.node_id, float_in)
        TS.connect_to_graph_output(name, db.node_id, float_out, 'outgain')
        edges = TS.get_graph_info(name).edges
        self.assertEqual(
            sorted((e.from_output, e.to_input) for e in edges
                   if '' in (e.from_node_id, e.to_node_id)),
            sorted([('PitchShift', float_in), (float_out, 'OutGain')]))
        TS.remove_graph_input(name, 'pitchshift')
        TS.remove_graph_output(name, 'OUTGAIN')
        self.assertEqual(len(TS.get_graph_info(name).edges), 0)

    def test_remove_node_drops_its_edges(self):
        name = self._patch()
        db_a = TS.add_node(name, 'UE', 'Decibels to Linear Gain', 'Float')
        db_b = TS.add_node(name, 'UE', 'Decibels to Linear Gain', 'Float')
        TS.connect_nodes(name, db_a.node_id, db_a.outputs[0].name,
                         db_b.node_id, db_b.inputs[0].name)
        TS.remove_node(name, db_a.node_id)
        self.assertEqual(len(TS.get_graph_info(name).edges), 0)

    def test_case_aliases_share_one_node_namespace(self):
        # Review pass-2 N3: find_builder is an FName (case-insensitive)
        # lookup, so 'name' and 'NAME' are ONE live builder. Two spellings
        # must not mint two 'n1's - remove_node by the second node's id
        # has to remove the second node, not the first.
        name = self._patch()
        first = TS.add_node(name, 'UE', 'Decibels to Linear Gain', 'Float')
        second = TS.add_node(
            name.upper(), 'UE', 'Decibels to Linear Gain', 'Float')
        self.assertNotEqual(first.node_id, second.node_id)
        self.assertEqual(
            list(TS.get_graph_info(name.upper()).tracked_node_ids),
            [first.node_id, second.node_id])
        TS.remove_node(name, second.node_id)
        self.assertEqual(
            list(TS.get_graph_info(name).tracked_node_ids), [first.node_id])
        # The surviving id still resolves to the surviving live node.
        TS.describe_node(name.upper(), first.node_id)
        with self.assertToolRaisesRuntimeError() as cm:
            TS.describe_node(name, second.node_id)
        self.assertIn('LookupError', str(cm.exception))
        # The roster shows the registered spelling once.
        self.assertEqual(
            [b for b in TS.list_builders() if b.casefold() == name.casefold()],
            [name])

    def test_interface_add_has_remove_cycle(self):
        name = self._source(one_shot=False)
        self.assertFalse(TS.has_interface(name, 'UE.Source.OneShot'))
        self.assertTrue(TS.add_interface(name, 'UE.Source.OneShot'))
        self.assertTrue(TS.has_interface(name, 'UE.Source.OneShot'))
        self.assertFalse(TS.remove_interface(name, 'UE.Source.OneShot'))
        self.assertFalse(TS.has_interface(name, 'UE.Source.OneShot'))

    def test_add_interface_raises_on_unregistered(self):
        name = self._source(one_shot=False)
        with self.assertToolRaisesRuntimeError() as cm:
            TS.add_interface(name, 'UE.NoSuch.Interface')
        self.assertIn('LookupError', str(cm.exception))


class TestAudition(MetaSoundToolsetTestCase):

    def test_audition_lifecycle_on_builder(self):
        name = self._source(output_format='Mono', one_shot=False)
        status = TS.start_audition(name, live_updates=True)
        self.assertEqual(status.audition_name, name)
        self.assertIn(name, list(TS.list_auditions()))
        status = TS.get_audition_status(name)
        self.assertEqual(status.source_description, f'builder:{name}')
        TS.stop_audition(name, fade_out_seconds=0.0)
        with self.assertToolRaisesRuntimeError() as cm:
            TS.get_audition_status(name)
        self.assertIn('LookupError', str(cm.exception))

    def test_start_audition_raises_on_patch(self):
        name = self._patch()
        with self.assertToolRaisesRuntimeError() as cm:
            TS.start_audition(name)
        self.assertIn('TypeError', str(cm.exception))

    def test_start_audition_raises_on_unknown_builder(self):
        with self.assertToolRaisesRuntimeError() as cm:
            TS.start_audition('never_created')
        self.assertIn('LookupError', str(cm.exception))

    def test_set_audition_param_tracks_values(self):
        name = self._source(one_shot=False)
        TS.add_graph_input(name, 'TestRate', 'Float', float_value=1.0)
        TS.start_audition(name)
        status = TS.set_audition_param(name, 'TestRate', float_value=3.0)
        self.assertEqual(dict(status.params_set).get('TestRate'), '3.0')
        TS.stop_audition(name)

    def test_set_audition_param_raises_without_value(self):
        name = self._source(one_shot=False)
        TS.start_audition(name)
        with self.assertToolRaisesRuntimeError() as cm:
            TS.set_audition_param(name, 'TestRate')
        self.assertIn('ValueError', str(cm.exception))
        TS.stop_audition(name)

    def test_set_audition_param_raises_on_unknown_audition(self):
        with self.assertToolRaisesRuntimeError() as cm:
            TS.set_audition_param('never_started', 'X', float_value=1.0)
        self.assertIn('LookupError', str(cm.exception))

    def test_pulse_trigger_tracks(self):
        name = self._source(one_shot=False)
        TS.add_graph_input(name, 'Impulse', 'Trigger')
        TS.start_audition(name)
        status = TS.pulse_audition_trigger(name, 'Impulse')
        self.assertEqual(dict(status.params_set).get('Impulse'), 'trigger')
        TS.stop_audition(name)

    def test_stop_audition_caps_fade(self):
        # Review F11: a fading sound's session is forgotten at stop time,
        # so an unbounded fade is an unboundedly long uncontrollable
        # sound. The cap keeps the window finite.
        name = self._source(output_format='Mono', one_shot=False)
        TS.start_audition(name)
        with self.assertToolRaisesRuntimeError() as cm:
            TS.stop_audition(name, fade_out_seconds=11.0)
        self.assertIn('ValueError', str(cm.exception))
        TS.stop_audition(name, fade_out_seconds=0.0)

    def test_stop_all_auditions_sweeps_fading(self):
        name = self._source(output_format='Mono', one_shot=False)
        TS.start_audition(name)
        TS.stop_audition(name, fade_out_seconds=5.0)
        self.assertEqual(len(_core._fading_components), 1)
        TS.stop_all_auditions()
        self.assertEqual(len(_core._fading_components), 0)

    def test_stop_audition_raises_on_negative_fade(self):
        name = self._source(one_shot=False)
        TS.start_audition(name)
        with self.assertToolRaisesRuntimeError() as cm:
            TS.stop_audition(name, fade_out_seconds=-1.0)
        self.assertIn('ValueError', str(cm.exception))
        TS.stop_audition(name)

    def test_stop_all_auditions_counts(self):
        name = self._source(one_shot=False)
        TS.start_audition(name)
        stopped = TS.stop_all_auditions()
        self.assertTrue(stopped >= 1)
        self.assertEqual(list(TS.list_auditions()), [])

    def test_audition_asset_raises_on_missing(self):
        with self.assertToolRaisesRuntimeError() as cm:
            TS.audition_asset('/Game/__nope__/NoSuchSource')
        self.assertIn('FileNotFoundError', str(cm.exception))

    def test_audition_asset_plays_engine_reference(self):
        status = TS.audition_asset(
            '/Metasound/DefaultAssets/Sources/MSS_PlayRandom_Oneshot',
            audition_name='t_engine_ref')
        self.assertEqual(status.audition_name, 't_engine_ref')
        TS.stop_audition('t_engine_ref', fade_out_seconds=0.0)


class TestLiteralTypes(MetaSoundToolsetTestCase):
    """A mismatched default is a fatal engine assert at build time."""

    @staticmethod
    def _expect_engine_refusal(count: int = 1) -> None:
        # The check logs an engine Error when it refuses, and automation
        # fails any test that sees one it was not told to expect.
        unreal.AutomationLibrary.add_expected_plain_log_error(
            'with unsupported literal type', count)

    def test_node_default_refuses_a_mismatched_literal(self):
        name = self._source()
        node = TS.add_node(name, 'UE', 'Wave Player', 'Stereo')
        self._expect_engine_refusal(2)
        with self.assertToolRaisesRuntimeError() as cm:
            TS.set_node_input_default(
                name, node.node_id, 'Wave Asset', string_value='/Game/X')
        self.assertIn('TypeError', str(cm.exception))
        float_pin = next(p.name for p in node.inputs
                         if p.data_type == 'Time')
        with self.assertToolRaisesRuntimeError() as cm:
            TS.set_node_input_default(
                name, node.node_id, float_pin, string_value='not a number')
        self.assertIn('TypeError', str(cm.exception))

    def test_node_default_accepts_a_matching_literal(self):
        name = self._source()
        node = TS.add_node(name, 'UE', 'Decibels to Linear Gain', 'Float')
        pin = next(p.name for p in node.inputs if p.data_type == 'Float')
        value = TS.set_node_input_default(
            name, node.node_id, pin, float_value=-6.0)
        self.assertAlmostEqual(value.float_value, -6.0, places=4)

    def test_graph_input_refuses_a_mismatched_literal(self):
        name = self._source()
        self._expect_engine_refusal()
        with self.assertToolRaisesRuntimeError() as cm:
            TS.add_graph_input(name, 'Gain', 'Float', string_value='x')
        self.assertIn('TypeError', str(cm.exception))
        self.assertNotIn(
            'Gain', [m.name for m in TS.get_graph_info(name).inputs])

    def test_literal_check_leaves_no_scratch_builder(self):
        name = self._source()
        TS.add_graph_input(name, 'Gain', 'Float', float_value=1.0)
        self.assertEqual(
            [b for b in TS.list_builders() if b.startswith('__msts')], [])


class TestToolSurfacePin(MetaSoundToolsetTestCase):
    """The advertised surface cannot drift without the pin changing."""

    def test_surface_matches_the_committed_pin(self):
        live = surface.current_surface()
        pin = surface.read_pin()
        self.assertEqual(live['skill_sha256'], pin['skill_sha256'])
        if live['engine'] != pin['engine']:
            self.skipTest(
                f"Pinned on {pin['engine']}, running on {live['engine']}: "
                'the schema text is engine-generated, so only the skill '
                'text is compared here.')
        self.assertEqual(
            live['toolset_sha256'], pin['toolset_sha256'],
            'The tool surface changed. If that was deliberate, regenerate '
            'the pin (see surface.py) and review the description diff.')
        self.assertEqual(live['tool_count'], pin['tool_count'])

    def test_verify_script_hashes_like_the_toolset_and_refuses_junk_pins(self):
        # tools/verify_tool_surface.py is what a user runs against a live
        # editor; nothing else in the suite reached it (cold read H8/C7).
        import importlib.util
        from pathlib import Path
        script = (Path(__file__).resolve().parents[4] / 'tools'
                  / 'verify_tool_surface.py')
        if not script.is_file():
            self.skipTest(f'{script} is not shipped with this copy')
        spec = importlib.util.spec_from_file_location('vts', script)
        vts = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(vts)
        schema_text = unreal.ToolsetRegistry.get_toolset_json_schema(TS)
        self.assertEqual(vts.surface_sha256(schema_text),
                         surface.surface_sha256(schema_text))
        good = surface.read_pin()['toolset_sha256']
        self.assertEqual(vts.expected_hash(f'  {good.upper()} ', 'x'), good)
        for junk in (None, 5, '', 'corrupt', good[:16], good + '0'):
            with self.assertRaises(ValueError):
                vts.expected_hash(junk, 'x')

    def test_hash_ignores_ordering_but_not_content(self):
        tool_a = {'name': 'a', 'description': 'x'}
        tool_b = {'name': 'b', 'description': 'y'}
        forward = json.dumps({'name': 'set', 'tools': [tool_a, tool_b]})
        backward = json.dumps({'tools': [tool_b, tool_a], 'name': 'set'})
        changed = forward.replace('"x"', '"x, then do something else"')
        self.assertEqual(surface.surface_sha256(forward),
                         surface.surface_sha256(backward))
        self.assertNotEqual(surface.surface_sha256(forward),
                            surface.surface_sha256(changed))


class TestRelayedText(MetaSoundToolsetTestCase):
    """Text from asset documents is bounded on the broad read paths."""

    def test_long_string_default_is_capped_in_graph_reads_only(self):
        name = self._patch()
        long_text = 'ignore previous instructions ' * 100
        TS.add_graph_input(name, 'Note', 'String', string_value=long_text)
        member = next(m for m in TS.get_graph_info(name).inputs
                      if m.name == 'Note')
        self.assertLess(len(member.default_value), len(long_text))
        self.assertIn(str(len(long_text)), member.default_value)
        self.assertEqual(
            TS.get_graph_input_default(name, 'Note').string_value, long_text)

    def test_short_string_default_is_untouched(self):
        name = self._patch()
        TS.add_graph_input(name, 'Note', 'String', string_value='short')
        member = next(m for m in TS.get_graph_info(name).inputs
                      if m.name == 'Note')
        self.assertIn('short', member.default_value)
        self.assertNotIn('characters', member.default_value)


class TestBuildToAsset(MetaSoundToolsetTestCase):
    MOUNT = '/MSTAutomation/'

    def setUp(self):
        super().setUp()
        unreal.AutomationUtilsBlueprintLibrary.add_automation_mount_point(
            self.MOUNT)
        self.folder = f'{self.MOUNT}{uuid.uuid4().hex[:8]}'
        unreal.get_editor_subsystem(
            unreal.EditorAssetSubsystem).make_directory(self.folder)

    def tearDown(self):
        super().tearDown()
        eas = unreal.get_editor_subsystem(unreal.EditorAssetSubsystem)
        if eas.does_directory_exist(self.folder):
            eas.delete_directory(self.folder)
        unreal.AutomationUtilsBlueprintLibrary.remove_automation_mount_point(
            self.MOUNT)

    def test_build_patch_to_asset_and_refuse_overwrite(self):
        name = self._patch()
        TS.add_graph_input(name, 'InDb', 'Float', float_value=0.0)
        saved = TS.build_to_asset(name, 'MSP_TestPatch', self.folder)
        self.assertTrue(unreal.EditorAssetLibrary.does_asset_exist(saved))
        with self.assertToolRaisesRuntimeError() as cm:
            TS.build_to_asset(name, 'MSP_TestPatch', self.folder)
        self.assertIn('FileExistsError', str(cm.exception))
        saved_again = TS.build_to_asset(
            name, 'MSP_TestPatch', self.folder, overwrite=True)
        self.assertEqual(saved, saved_again)

    def test_writes_to_engine_content_are_refused(self):
        name = self._patch()
        for package_path in ('/Engine/__nope__', '/Metasound/__nope__'):
            with self.assertToolRaisesRuntimeError() as cm:
                TS.build_to_asset(name, 'MSP_Refused', package_path)
            self.assertIn('PermissionError', str(cm.exception))
            with self.assertToolRaisesRuntimeError() as cm:
                TS.create_attenuation_asset('ATT_Refused', package_path)
            self.assertIn('PermissionError', str(cm.exception))
        with self.assertToolRaisesRuntimeError() as cm:
            TS.set_sound_routing(ENGINE_WAVE, submix_path='')
        self.assertIn('PermissionError', str(cm.exception))

    def test_attach_refuses_engine_assets(self):
        # Attached edits land in the live document in memory, and the
        # templates build on these assets, so a save refusal is too late.
        with self.assertToolRaisesRuntimeError() as cm:
            TS.attach_builder_to_asset(
                _unique('t_engine'),
                '/Metasound/DefaultAssets/Sources/MSS_PlayRandom_Oneshot')
        self.assertIn('PermissionError', str(cm.exception))

    def test_overwrite_refuses_a_target_that_is_not_a_metasound(self):
        # Overwrite force-deletes the target; it must never reach an asset
        # of another type that happens to share the name.
        name = self._patch()
        other = TS.create_attenuation_asset('ATT_NotAMetaSound', self.folder)
        with self.assertToolRaisesRuntimeError() as cm:
            TS.build_to_asset(
                name, 'ATT_NotAMetaSound', self.folder, overwrite=True)
        self.assertIn('TypeError', str(cm.exception))
        self.assertEqual(
            _core.package_asset_classes(other.asset_path),
            ['SoundAttenuation'])

    def test_overwrite_build_failure_restores_original(self):
        # A failed build during overwrite=True must leave the pre-existing
        # asset exactly where it was (the data-loss window of the old
        # delete-first design). No reachable engine state makes
        # build_to_asset fail deterministically headless, so the failure
        # is injected by swapping _core.editor_subsystem for a stub whose
        # build_to_asset reports FAILED.
        name = self._patch()
        TS.add_graph_input(name, 'InDb', 'Float', float_value=0.0)
        saved = TS.build_to_asset(name, 'MSP_Precious', self.folder)
        self.assertTrue(unreal.EditorAssetLibrary.does_asset_exist(saved))

        class _FailingSubsystem:
            def build_to_asset(self, *args, **kwargs):
                return None, unreal.MetaSoundBuilderResult.FAILED

        real_subsystem = _core.editor_subsystem
        _core.editor_subsystem = lambda: _FailingSubsystem()
        try:
            with self.assertToolRaisesRuntimeError() as cm:
                TS.build_to_asset(
                    name, 'MSP_Precious', self.folder, overwrite=True)
            self.assertIn('RuntimeError', str(cm.exception))
        finally:
            _core.editor_subsystem = real_subsystem
        # The original survived at its path and no scratch copy remains.
        self.assertTrue(unreal.EditorAssetLibrary.does_asset_exist(saved))
        listed = list(unreal.EditorAssetLibrary.list_assets(self.folder))
        self.assertFalse(
            [p for p in listed if 'overwrite_scratch' in p], listed)
        info = TS.describe_metasound(saved)
        self.assertIn('InDb', [m.name for m in info.inputs])

    def test_overwrite_refused_on_attached_asset(self):
        # overwrite=True onto the asset the builder itself is attached to
        # would tear down the builder's own document mid-build.
        name = self._patch()
        saved = TS.build_to_asset(name, 'MSP_Attached', self.folder)
        attached = _unique('t_att')
        TS.attach_builder_to_asset(attached, saved)
        self._builders.append(attached)
        with self.assertToolRaisesRuntimeError() as cm:
            TS.build_to_asset(
                attached, 'MSP_Attached', self.folder, overwrite=True)
        self.assertIn('ValueError', str(cm.exception))
        self.assertTrue(unreal.EditorAssetLibrary.does_asset_exist(saved))

    def test_overwrite_attached_guard_survives_case_drift(self):
        # Review F2: find_builder is an FName (case-insensitive) lookup and
        # package paths are FNames too, so a case-drifted builder name or
        # target path still hits the same builder/asset - the guard must
        # not be bypassable by case.
        name = self._patch()
        saved = TS.build_to_asset(name, 'MSP_CaseGuard', self.folder)
        attached = _unique('t_case')
        TS.attach_builder_to_asset(attached, saved)
        self._builders.append(attached)
        with self.assertToolRaisesRuntimeError() as cm:
            TS.build_to_asset(attached.upper(), 'MSP_CaseGuard',
                              self.folder, overwrite=True)
        self.assertIn('ValueError', str(cm.exception))
        with self.assertToolRaisesRuntimeError() as cm:
            TS.build_to_asset(attached, 'msp_caseguard',
                              self.folder, overwrite=True)
        self.assertIn('ValueError', str(cm.exception))
        self.assertTrue(unreal.EditorAssetLibrary.does_asset_exist(saved))

    def _register_nameless_builder_on(self, saved: str):
        # A builder the toolset never tracked, registered straight with the
        # subsystem under the engine's empty name: the slot '_0' reached
        # before the R1 fix. Returns a cleanup.
        asset = unreal.EditorAssetLibrary.load_asset(saved)
        builder, result = _core.editor_subsystem().find_or_begin_building(
            asset)
        _core.check_result(result, 'begin building for the R1 replay')
        subsystem = _core.builder_subsystem()
        subsystem.register_builder('None', builder)
        self.assertIsNotNone(subsystem.find_builder('_1'))  # the trap
        return lambda: subsystem.unregister_builder('None')

    def test_overwrite_refused_through_an_empty_name_alias(self):
        # Verify-cold R1, sequence 1: A attached as '_0', then overwritten
        # through '_1'. The engine hands back the attached builder, the
        # records knew nothing under '_1', and both self-overwrite guards
        # were skipped: A force-deleted under its own live document.
        saved = TS.build_to_asset(self._patch(), 'MSP_NamelessA', self.folder)
        with self.assertToolRaisesRuntimeError() as cm:
            TS.attach_builder_to_asset('_0', saved)
        self.assertIn('ValueError', str(cm.exception))
        cleanup = self._register_nameless_builder_on(saved)
        try:
            with self.assertToolRaisesRuntimeError() as cm:
                TS.build_to_asset(
                    '_1', 'MSP_NamelessA', self.folder, overwrite=True)
            self.assertIn('ValueError', str(cm.exception))
            self.assertTrue(unreal.EditorAssetLibrary.does_asset_exist(saved))
            # Sequence 2: a document read through the alias is refused
            # before the builder is fetched, so the dead-document guard is
            # never the one standing between it and the engine assert.
            for read in (TS.get_graph_info, TS.auto_layout_nodes):
                with self.assertToolRaisesRuntimeError() as cm:
                    read('_1')
                self.assertIn('ValueError', str(cm.exception))
        finally:
            cleanup()

    def test_overwrite_refused_on_renamed_attached_asset(self):
        # Review pass-2 N2: after an editor rename (+ redirector fixup) the
        # attach-time path is stale, so the string guard passes - but the
        # asset at the target is still the builder's own live document.
        # The rename's effect on the record is modeled directly (a real
        # rename + fixup needs editor ticks a tool call never gets).
        name = self._patch()
        saved = TS.build_to_asset(name, 'MSP_RenamedTo', self.folder)
        attached = _unique('t_ren')
        TS.attach_builder_to_asset(attached, saved)
        self._builders.append(attached)
        _core.track_attachment(attached, f'{self.folder}/MSP_RenamedFrom')
        with self.assertToolRaisesRuntimeError() as cm:
            TS.build_to_asset(
                attached, 'MSP_RenamedTo', self.folder, overwrite=True)
        self.assertIn('ValueError', str(cm.exception))
        self.assertIn('Nothing was deleted', str(cm.exception))
        self.assertTrue(unreal.EditorAssetLibrary.does_asset_exist(saved))

    def test_attached_builder_may_overwrite_another_asset(self):
        # The N2 identity check refuses only the builder's OWN document:
        # an attached builder replacing some other asset still works.
        name = self._patch()
        saved = TS.build_to_asset(name, 'MSP_AttachHome', self.folder)
        attached = _unique('t_att')
        TS.attach_builder_to_asset(attached, saved)
        self._builders.append(attached)
        other = self._patch()
        other_saved = TS.build_to_asset(other, 'MSP_Elsewhere', self.folder)
        replaced = TS.build_to_asset(
            attached, 'MSP_Elsewhere', self.folder, overwrite=True)
        self.assertEqual(replaced, other_saved)
        self.assertTrue(unreal.EditorAssetLibrary.does_asset_exist(saved))
        self.assertTrue(
            unreal.EditorAssetLibrary.does_asset_exist(other_saved))

    def test_overwrite_refused_on_saved_referencer(self):
        # Review F1 (2026-10-24), widened 2026-11-02: replacing an asset
        # force-deletes it and the rebuild gets a new class identity, so
        # overwrite must refuse while any saved asset references it, naming
        # it, instead of silently severing the reference.
        if _reference_restrictions_block_the_test_mount():
            self.skipTest(_REFERENCE_RESTRICTIONS_REASON)
        target_builder = self._patch()
        TS.add_graph_input(target_builder, 'InVal', 'Float', float_value=0.0)
        target = TS.build_to_asset(
            target_builder, 'MSP_RefTarget', self.folder)
        host = self._patch()
        TS.add_node_from_asset(host, target)
        referencer = TS.build_to_asset(host, 'MSP_Referencer', self.folder)
        self.assertTrue(
            unreal.EditorAssetLibrary.does_asset_exist(referencer))
        # The overwrite would null the loaded reference and orphan the
        # parent's node (a rebuild gets a new class identity): it refuses.
        with self.assertToolRaisesRuntimeError() as cm:
            TS.build_to_asset(
                target_builder, 'MSP_RefTarget', self.folder, overwrite=True)
        self.assertIn('RuntimeError', str(cm.exception))
        self.assertIn('MSP_Referencer', str(cm.exception))
        # Nothing was deleted, replaced or left behind.
        self.assertTrue(unreal.EditorAssetLibrary.does_asset_exist(target))
        listed = list(unreal.EditorAssetLibrary.list_assets(self.folder))
        self.assertFalse(
            [p for p in listed if 'overwrite_scratch' in p], listed)

    def test_overwrite_refused_while_a_builder_holds_a_node_of_it(self):
        target_builder = self._patch()
        target = TS.build_to_asset(
            target_builder, 'MSP_HeldNode', self.folder)
        host = self._patch()
        TS.add_node_from_asset(host, target)
        with self.assertToolRaisesRuntimeError() as cm:
            TS.build_to_asset(host, 'MSP_HeldNode', self.folder,
                              overwrite=True)
        self.assertIn('hold a node', str(cm.exception))
        self.assertTrue(unreal.EditorAssetLibrary.does_asset_exist(target))

    def test_overwrite_refused_on_a_released_session_edge(self):
        # Cold read C9: the live-builder check forgets what release_builder
        # forgot, and the node stays in the host's document.
        a = TS.build_to_asset(self._patch(), 'MSP_RelA', self.folder)
        b = TS.build_to_asset(self._patch(), 'MSP_RelB', self.folder)
        edit_b = _unique('t_relb')
        TS.attach_builder_to_asset(edit_b, b)
        TS.add_node_from_asset(edit_b, a)
        TS.release_builder(edit_b)
        with self.assertToolRaisesRuntimeError() as cm:
            TS.build_to_asset(self._patch(), 'MSP_RelA', self.folder,
                              overwrite=True)
        self.assertIn('unsaved edits', str(cm.exception))
        self.assertIn('MSP_RelB', str(cm.exception))
        self.assertTrue(unreal.EditorAssetLibrary.does_asset_exist(a))

    def test_overwrite_gives_the_asset_a_new_class_identity(self):
        # Why the referencer guard exists: parents bind by class, and this
        # is what they would lose.
        name = self._patch()
        saved = TS.build_to_asset(name, 'MSP_NewClass', self.folder)
        before = TS.describe_metasound(saved).class_name
        TS.build_to_asset(self._patch(), 'MSP_NewClass', self.folder,
                          overwrite=True)
        self.assertNotEqual(before, TS.describe_metasound(saved).class_name)

    def test_add_node_refuses_a_self_reference(self):
        # The engine adds this without complaint and the next registration
        # overflows the stack. A crash here takes the whole run down.
        name = self._patch()
        saved = TS.build_to_asset(name, 'MSP_SelfRef', self.folder)
        attached = _unique('t_self')
        TS.attach_builder_to_asset(attached, saved)
        self._builders.append(attached)
        with self.assertToolRaisesRuntimeError() as cm:
            TS.add_node_from_asset(attached, saved)
        self.assertIn('ValueError', str(cm.exception))

    def test_add_node_refuses_asset_classes(self):
        # A MetaSound's class name comes back from describe_metasound, and
        # add_node has no cycle check, so it must not accept one.
        name = self._patch()
        saved = TS.build_to_asset(name, 'MSP_ByClass', self.folder)
        class_name = TS.describe_metasound(saved).class_name
        attached = _unique('t_class')
        TS.attach_builder_to_asset(attached, saved)
        self._builders.append(attached)
        # 'UE' reaches the GUID-name branch, which no test ran before (C6).
        for namespace in ('', 'None', 'none', 'UE'):
            with self.assertToolRaisesRuntimeError() as cm:
                TS.add_node(attached, namespace, class_name)
            self.assertIn('add_node_from_asset', str(cm.exception))
        # resolve_node_class still resolves it; its doc says add_node won't.
        TS.resolve_node_class('', class_name)

    def test_cycle_guard_survives_accent_case_aliases(self):
        # Cold read C1, scenario alias_accent (fired: stack overflow): two
        # attached builders whose names differ only in accent case must
        # keep separate records, or the self-reference check asks about
        # the other builder's asset and the editor dies.
        a = TS.build_to_asset(self._patch(), 'MSP_AliasA', self.folder)
        b = TS.build_to_asset(self._patch(), 'MSP_AliasB', self.folder)
        suffix = uuid.uuid4().hex[:8]
        upper, lower = f'Äther_{suffix}', f'äther_{suffix}'
        TS.attach_builder_to_asset(upper, a)
        self._builders.append(upper)
        TS.attach_builder_to_asset(lower, b)
        self._builders.append(lower)
        self.assertEqual(_core.attached_asset_path(upper), a)
        self.assertEqual(_core.attached_asset_path(lower), b)
        with self.assertToolRaisesRuntimeError() as cm:
            TS.add_node_from_asset(upper, a)
        self.assertIn('ValueError', str(cm.exception))

    def test_attachment_survives_releasing_a_casefold_alias(self):
        # Cold read C1, scenario alias_cycle (fired): releasing 'strasse'
        # wiped the records of 'straße', a different FName, so the host
        # came back None and the cycle check was skipped.
        a = TS.build_to_asset(self._patch(), 'MSP_Strasse', self.folder)
        suffix = uuid.uuid4().hex[:8]
        sharp, plain = f'straße_{suffix}', f'strasse_{suffix}'
        TS.attach_builder_to_asset(sharp, a)
        self._builders.append(sharp)
        TS.create_metasound_patch(plain)
        TS.release_builder(plain)
        self.assertEqual(_core.attached_asset_path(sharp), a)
        self.assertIn(sharp, list(TS.list_builders()))
        with self.assertToolRaisesRuntimeError() as cm:
            TS.add_node_from_asset(sharp, a)
        self.assertIn('ValueError', str(cm.exception))
        # S3's guard is keyed the same way: the document is still readable.
        TS.get_graph_info(sharp)

    def test_self_reference_refused_without_session_records(self):
        # The engine's own builder identity backs the records: after a
        # toolset reload (S23) the attachment records are empty, and a
        # direct self-reference must still be refused.
        saved = TS.build_to_asset(self._patch(), 'MSP_NoRecords', self.folder)
        attached = _unique('t_norec')
        TS.attach_builder_to_asset(attached, saved)
        self._builders.append(attached)
        key = _core.builder_key(attached)
        path = _core._attached_assets.pop(key)
        obj = _core._attached_objects.pop(key)
        try:
            with self.assertToolRaisesRuntimeError() as cm:
                TS.add_node_from_asset(attached, saved)
            self.assertIn('ValueError', str(cm.exception))
        finally:
            _core._attached_assets[key] = path
            _core._attached_objects[key] = obj

    def test_cycle_check_survives_releasing_the_builder(self):
        # Release forgets the builder, but the unsaved node stays in the
        # asset's in-memory document.
        first = TS.build_to_asset(self._patch(), 'MSP_Keep1', self.folder)
        second = TS.build_to_asset(self._patch(), 'MSP_Keep2', self.folder)
        edit_first = _unique('t_rel1')
        TS.attach_builder_to_asset(edit_first, first)
        TS.add_node_from_asset(edit_first, second)
        TS.release_builder(edit_first)
        edit_second = _unique('t_rel2')
        TS.attach_builder_to_asset(edit_second, second)
        self._builders.append(edit_second)
        with self.assertToolRaisesRuntimeError() as cm:
            TS.add_node_from_asset(edit_second, first)
        self.assertIn('ValueError', str(cm.exception))

    def test_reused_builder_name_cannot_erase_a_retained_edge(self):
        # Verify-cold R2, sequence 1: node ids restart at 'n1' under a
        # reused name, and removing the new incarnation's n1 erased the
        # released one's retained A -> B edge, so B could take A (an
        # indirect cycle) and B could be overwritten under A's node.
        a = TS.build_to_asset(self._patch(), 'MSP_ReuseA', self.folder)
        b = TS.build_to_asset(self._patch(), 'MSP_ReuseB', self.folder)
        edit = _unique('t_reuse')
        TS.attach_builder_to_asset(edit, a)
        first = TS.add_node_from_asset(edit, b)
        TS.release_builder(edit)  # A keeps the unsaved node of B
        TS.create_metasound_patch(edit)
        self._builders.append(edit)
        other = TS.add_node(edit, 'UE', 'Decibels to Linear Gain', 'Float')
        self.assertEqual(other.node_id, first.node_id)  # the reused id
        TS.remove_node(edit, other.node_id)
        self.assertEqual(_core.session_hosts_referencing(b), [a])
        edit_b = _unique('t_reuseb')
        TS.attach_builder_to_asset(edit_b, b)
        self._builders.append(edit_b)
        with self.assertToolRaisesRuntimeError() as cm:
            TS.add_node_from_asset(edit_b, a)
        self.assertIn('not saved yet', str(cm.exception))
        with self.assertToolRaisesRuntimeError() as cm:
            TS.build_to_asset(self._patch(), 'MSP_ReuseB', self.folder,
                              overwrite=True)
        self.assertIn('unsaved edits', str(cm.exception))
        self.assertTrue(unreal.EditorAssetLibrary.does_asset_exist(b))

    def test_reattach_keeps_the_released_edge(self):
        # Verify-cold R2, sequence 2, no removal: the reattached builder's
        # new n1 was assigned over the retained A -> B edge.
        a = TS.build_to_asset(self._patch(), 'MSP_ReattA', self.folder)
        b = TS.build_to_asset(self._patch(), 'MSP_ReattB', self.folder)
        c = TS.build_to_asset(self._patch(), 'MSP_ReattC', self.folder)
        edit = _unique('t_reatt')
        TS.attach_builder_to_asset(edit, a)
        first = TS.add_node_from_asset(edit, b)
        TS.release_builder(edit)
        TS.attach_builder_to_asset(edit, a)
        self._builders.append(edit)
        second = TS.add_node_from_asset(edit, c)
        self.assertEqual(second.node_id, first.node_id)
        self.assertEqual(_core.session_hosts_referencing(b), [a])
        self.assertEqual(_core.session_hosts_referencing(c), [a])
        # The new incarnation forgets only its own edge.
        TS.remove_node(edit, second.node_id)
        self.assertEqual(_core.session_hosts_referencing(c), [])
        self.assertEqual(_core.session_hosts_referencing(b), [a])
        edit_b = _unique('t_reattb')
        TS.attach_builder_to_asset(edit_b, b)
        self._builders.append(edit_b)
        with self.assertToolRaisesRuntimeError() as cm:
            TS.add_node_from_asset(edit_b, a)
        self.assertIn('not saved yet', str(cm.exception))

    def test_add_node_refuses_a_cycle_through_a_saved_asset(self):
        if _reference_restrictions_block_the_test_mount():
            self.skipTest(_REFERENCE_RESTRICTIONS_REASON)
        inner = TS.build_to_asset(self._patch(), 'MSP_Inner', self.folder)
        host = self._patch()
        TS.add_node_from_asset(host, inner)
        outer = TS.build_to_asset(host, 'MSP_Outer', self.folder)
        attached = _unique('t_cycle')
        TS.attach_builder_to_asset(attached, inner)
        self._builders.append(attached)
        with self.assertToolRaisesRuntimeError() as cm:
            TS.add_node_from_asset(attached, outer)
        self.assertIn('ValueError', str(cm.exception))

    def test_add_node_refuses_a_cycle_through_unsaved_edits(self):
        first = TS.build_to_asset(self._patch(), 'MSP_First', self.folder)
        second = TS.build_to_asset(self._patch(), 'MSP_Second', self.folder)
        edit_first = _unique('t_first')
        edit_second = _unique('t_second')
        TS.attach_builder_to_asset(edit_first, first)
        self._builders.append(edit_first)
        TS.attach_builder_to_asset(edit_second, second)
        self._builders.append(edit_second)
        TS.add_node_from_asset(edit_first, second)  # in memory, not saved
        with self.assertToolRaisesRuntimeError() as cm:
            TS.add_node_from_asset(edit_second, first)
        self.assertIn('ValueError', str(cm.exception))
        # The refusal says which kind of edge closed the loop (C3).
        self.assertIn('not saved yet', str(cm.exception))

    def test_removed_node_frees_the_reverse_reference(self):
        # Cold read C3, scenario remove_then_reverse (fired): a session
        # edge outlived remove_node, a save and a release, so a legitimate
        # graph was refused for the rest of the session.
        a = TS.build_to_asset(self._patch(), 'MSP_RevA', self.folder)
        b = TS.build_to_asset(self._patch(), 'MSP_RevB', self.folder)
        edit_a = _unique('t_reva')
        TS.attach_builder_to_asset(edit_a, a)
        node = TS.add_node_from_asset(edit_a, b)
        TS.remove_node(edit_a, node.node_id)
        TS.save_builder_asset(edit_a)
        TS.release_builder(edit_a)
        edit_b = _unique('t_revb')
        TS.attach_builder_to_asset(edit_b, b)
        self._builders.append(edit_b)
        TS.add_node_from_asset(edit_b, a)

    def test_save_settles_the_session_edges(self):
        # After save_builder_asset the registry is the truth: the session
        # record is dropped, and the saved edge still refuses the loop.
        a = TS.build_to_asset(self._patch(), 'MSP_SetA', self.folder)
        b = TS.build_to_asset(self._patch(), 'MSP_SetB', self.folder)
        edit_a = _unique('t_seta')
        TS.attach_builder_to_asset(edit_a, a)
        self._builders.append(edit_a)
        TS.add_node_from_asset(edit_a, b)
        self.assertEqual(_core.session_hosts_referencing(b), [a])
        if _reference_restrictions_block_the_test_mount():
            self.skipTest(_REFERENCE_RESTRICTIONS_REASON)
        TS.save_builder_asset(edit_a)
        self.assertEqual(_core.session_hosts_referencing(b), [])
        edit_b = _unique('t_setb')
        TS.attach_builder_to_asset(edit_b, b)
        self._builders.append(edit_b)
        with self.assertToolRaisesRuntimeError() as cm:
            TS.add_node_from_asset(edit_b, a)
        self.assertIn('ValueError', str(cm.exception))
        self.assertNotIn('not saved yet', str(cm.exception))

    def test_overwrite_clears_the_old_documents_session_edges(self):
        # Cold read C3, scenario overwrite_stale_edge (fired): A was
        # replaced by a graph holding no node of B, yet B could not take A.
        a = TS.build_to_asset(self._patch(), 'MSP_StaleA', self.folder)
        b = TS.build_to_asset(self._patch(), 'MSP_StaleB', self.folder)
        edit_a = _unique('t_stalea')
        TS.attach_builder_to_asset(edit_a, a)
        TS.add_node_from_asset(edit_a, b)
        TS.release_builder(edit_a)
        TS.build_to_asset(self._patch(), 'MSP_StaleA', self.folder,
                          overwrite=True)
        edit_b = _unique('t_staleb')
        TS.attach_builder_to_asset(edit_b, b)
        self._builders.append(edit_b)
        TS.add_node_from_asset(edit_b, a)

    def test_saved_referencer_packages_sees_saved_reference(self):
        # The guard's engine mechanics: a saved MetaSound composed into
        # another saved MetaSound shows up as a hard referencer.
        if _reference_restrictions_block_the_test_mount():
            self.skipTest(_REFERENCE_RESTRICTIONS_REASON)
        target_builder = self._patch()
        target = TS.build_to_asset(
            target_builder, 'MSP_DepTarget', self.folder)
        self.assertEqual(_core.saved_referencer_packages(target), [])
        host = self._patch()
        TS.add_node_from_asset(host, target)
        referencer = TS.build_to_asset(host, 'MSP_DepHost', self.folder)
        self.assertIn(
            referencer, _core.saved_referencer_packages(target))

    def test_unindexed_disk_file_counts_as_existing(self):
        # Review F4: a .uasset on disk that the registry has not indexed
        # (external write racing the call) must count as existing, or the
        # build walks into the engine's confirmation dialog.
        import os
        import shutil
        name = self._patch()
        saved = TS.build_to_asset(name, 'MSP_OnDisk', self.folder)
        source_file = _core.package_file_on_disk(saved)
        self.assertIsNotNone(source_file)
        self.assertTrue(source_file.endswith('.uasset'))
        ghost_file = source_file.replace('MSP_OnDisk', 'MSP_Ghost')
        shutil.copyfile(source_file, ghost_file)
        try:
            ghost_path = saved.replace('MSP_OnDisk', 'MSP_Ghost')
            # The checked existence rescans the folder and sees it.
            self.assertTrue(_core.asset_exists_checked(ghost_path))
            with self.assertToolRaisesRuntimeError() as cm:
                TS.build_to_asset(name, 'MSP_Ghost', self.folder)
            self.assertIn('FileExistsError', str(cm.exception))
        finally:
            if os.path.isfile(ghost_file):
                os.remove(ghost_file)
            # The rescan above indexed the ghost; rescan again so the
            # registry forgets it, or tearDown's delete_directory can trip
            # on an indexed-but-unloadable asset (an intermittent Error-log
            # failure of this test, seen 3 in 9 runs on 2026-10-28).
            unreal.AssetRegistryHelpers.get_asset_registry(
            ).scan_paths_synchronous([self.folder], force_rescan=True)

    def test_package_file_on_disk_misses_cleanly(self):
        # A never-built package and an unmounted root both read as
        # not-on-disk, quietly.
        self.assertIsNone(
            _core.package_file_on_disk(f'{self.folder}/NeverBuilt'))
        self.assertIsNone(
            _core.package_file_on_disk('/NoSuchMountXyz/Nothing'))
        self.assertFalse(
            _core.asset_exists_checked(f'{self.folder}/NeverBuilt'))

    def test_attach_builder_roundtrips_graph_info(self):
        name = self._patch()
        TS.add_graph_input(name, 'InDb', 'Float', float_value=-3.0)
        saved = TS.build_to_asset(name, 'MSP_ToAttach', self.folder)
        attached = _unique('t_att')
        info = TS.attach_builder_to_asset(attached, saved)
        self._builders.append(attached)
        self.assertEqual(info.metasound_type, 'Patch')
        self.assertIn('InDb', [m.name for m in info.inputs])
        value = TS.get_graph_input_default(attached, 'InDb')
        self.assertAlmostEqual(value.float_value, -3.0, places=4)
        # The attached builder takes edits like any other.
        node = TS.add_node(attached, 'UE', 'Decibels to Linear Gain', 'Float')
        self.assertTrue(node.node_id)

    def test_attach_builder_raises_on_non_metasound(self):
        if not _engine_wave_available():
            self.skipTest(f'{ENGINE_WAVE} not available')
        with self.assertToolRaisesRuntimeError() as cm:
            TS.attach_builder_to_asset(_unique('t'), ENGINE_WAVE)
        self.assertIn('TypeError', str(cm.exception))

    def test_add_node_from_asset_returns_pins(self):
        patch_builder = self._patch()
        TS.add_graph_input(patch_builder, 'InVal', 'Float', float_value=0.0)
        TS.add_graph_output(patch_builder, 'OutVal', 'Float')
        saved = TS.build_to_asset(patch_builder, 'MSP_AsNode', self.folder)
        host = self._patch()
        node = TS.add_node_from_asset(host, saved)
        self.assertTrue(node.node_id)
        self.assertIn('InVal', [p.name for p in node.inputs])
        self.assertIn('OutVal', [p.name for p in node.outputs])
        described = TS.describe_node(host, node.node_id)
        self.assertEqual([p.name for p in node.inputs],
                         [p.name for p in described.inputs])

    def test_add_node_from_asset_raises_on_non_metasound(self):
        if not _engine_wave_available():
            self.skipTest(f'{ENGINE_WAVE} not available')
        host = self._patch()
        with self.assertToolRaisesRuntimeError() as cm:
            TS.add_node_from_asset(host, ENGINE_WAVE)
        self.assertIn('TypeError', str(cm.exception))

    def test_add_node_from_asset_raises_on_unknown_builder(self):
        with self.assertToolRaisesRuntimeError() as cm:
            TS.add_node_from_asset('never_created', ENGINE_WAVE)
        self.assertIn('LookupError', str(cm.exception))

    def test_source_render_takes_attached_builder(self):
        # Drill MEDIUM: the asset-render recipe. Mechanics only - no
        # game-thread ticks happen in-test, so no signal lands (same
        # deal as test_source_render_lifecycle); the live A/B against
        # the record-around-audition recipe is a sitting check.
        import os
        name = self._source(output_format='Mono', one_shot=False)
        saved = TS.build_to_asset(name, 'MS_RenderMe', self.folder)
        attached = _unique('t_rsrc')
        TS.attach_builder_to_asset(attached, saved)
        self._builders.append(attached)
        status = self._start_render(
            attached, expected_duration_seconds=5.0)
        self.assertEqual(status.recording_name, attached)
        info = TS.finish_source_render(attached, analyze=False)
        self.assertTrue(info.wav_path.endswith(f'{attached}.wav'))
        from metasound_toolset import rendering as _rendering
        if _rendering.wait_for_file(info.wav_path, 5.0):
            os.remove(info.wav_path)

    def test_save_builder_asset_saves_attached_edits(self):
        name = self._patch()
        saved = TS.build_to_asset(name, 'MSP_SaveTarget', self.folder)
        attached = _unique('t_att')
        TS.attach_builder_to_asset(attached, saved)
        self._builders.append(attached)
        TS.add_graph_input(attached, 'AddedLater', 'Float', float_value=1.0)
        result = TS.save_builder_asset(attached)
        self.assertEqual(result, saved)
        input_names = [m.name for m in TS.get_graph_info(attached).inputs]
        self.assertIn('AddedLater', input_names)

    def test_save_builder_asset_refuses_replaced_asset(self):
        # Review F10: the attachment captures a path string; if another
        # asset sits at that path by save time, saving it would persist
        # the wrong asset and report success.
        name = self._patch()
        saved = TS.build_to_asset(name, 'MSP_Replaced', self.folder)
        attached = _unique('t_repl')
        TS.attach_builder_to_asset(attached, saved)
        self._builders.append(attached)
        self.assertTrue(unreal.EditorAssetLibrary.delete_asset(saved))
        other = self._patch()
        TS.add_graph_input(other, 'Other', 'Float', float_value=1.0)
        TS.build_to_asset(other, 'MSP_Replaced', self.folder)
        with self.assertToolRaisesRuntimeError() as cm:
            TS.save_builder_asset(attached)
        self.assertIn('no longer', str(cm.exception))

    def test_save_builder_asset_raises_when_asset_gone(self):
        name = self._patch()
        saved = TS.build_to_asset(name, 'MSP_Gone', self.folder)
        attached = _unique('t_gone')
        TS.attach_builder_to_asset(attached, saved)
        self._builders.append(attached)
        self.assertTrue(unreal.EditorAssetLibrary.delete_asset(saved))
        with self.assertToolRaisesRuntimeError() as cm:
            TS.save_builder_asset(attached)
        self.assertIn('FileNotFoundError', str(cm.exception))

    def test_builder_refuses_reads_after_its_asset_is_deleted(self):
        # Reading a builder whose attached asset is gone is a fatal engine
        # assert, not an exception. A crash here takes the whole run down.
        name = self._patch()
        saved = TS.build_to_asset(name, 'MSP_Deleted', self.folder)
        attached = _unique('t_dead')
        TS.attach_builder_to_asset(attached, saved)
        self._builders.append(attached)
        self.assertTrue(unreal.EditorAssetLibrary.delete_asset(saved))
        for call in (lambda: TS.get_graph_info(attached),
                     lambda: TS.add_node(attached, 'UE', 'Wave Player',
                                         'Stereo'),
                     lambda: TS.start_audition(attached, False)):
            with self.assertToolRaisesRuntimeError() as cm:
                call()
            self.assertIn('release_builder', str(cm.exception))
        TS.release_builder(attached)
        self._builders.remove(attached)
        self.assertNotIn(attached, list(TS.list_builders()))

    def test_overwrite_ends_another_builder_cleanly(self):
        name = self._patch()
        saved = TS.build_to_asset(name, 'MSP_Shared', self.folder)
        attached = _unique('t_other')
        TS.attach_builder_to_asset(attached, saved)
        self._builders.append(attached)
        TS.build_to_asset(self._patch(), 'MSP_Shared', self.folder,
                          overwrite=True)
        with self.assertToolRaisesRuntimeError() as cm:
            TS.get_graph_info(attached)
        self.assertIn('release_builder', str(cm.exception))

    def test_save_builder_asset_raises_on_transient_builder(self):
        name = self._patch()
        with self.assertToolRaisesRuntimeError() as cm:
            TS.save_builder_asset(name)
        self.assertIn('TypeError', str(cm.exception))

    def test_save_builder_asset_raises_on_unknown_builder(self):
        with self.assertToolRaisesRuntimeError() as cm:
            TS.save_builder_asset('never_created')
        self.assertIn('LookupError', str(cm.exception))

    def test_build_to_asset_raises_on_bad_name(self):
        name = self._patch()
        with self.assertToolRaisesRuntimeError() as cm:
            TS.build_to_asset(name, 'bad name!', self.folder)
        self.assertIn('ValueError', str(cm.exception))

    def test_build_to_asset_raises_on_bad_path(self):
        name = self._patch()
        with self.assertToolRaisesRuntimeError() as cm:
            TS.build_to_asset(name, 'MSP_Ok', 'Not/Rooted')
        self.assertIn('ValueError', str(cm.exception))

    def test_auto_layout_counts_tracked_nodes(self):
        name = self._patch()
        TS.add_node(name, 'UE', 'Decibels to Linear Gain', 'Float')
        TS.add_node(name, 'UE', 'Decibels to Linear Gain', 'Float')
        self.assertEqual(TS.auto_layout_nodes(name), 2)

    def test_open_in_editor_raises_on_missing(self):
        with self.assertToolRaisesRuntimeError() as cm:
            TS.open_in_editor('/Game/__nope__/NoSuchAsset')
        self.assertIn('FileNotFoundError', str(cm.exception))

    def test_open_in_editor_refuses_levels(self):
        # A World's open action is LoadMap: it replaces the current level
        # and can raise a modal over an unsaved one.
        with self.assertToolRaisesRuntimeError() as cm:
            TS.open_in_editor('/Engine/Maps/Entry')
        self.assertIn('TypeError', str(cm.exception))
        self.assertIn('level', str(cm.exception))

    def test_open_in_editor_refuses_anything_outside_audio(self):
        with self.assertToolRaisesRuntimeError() as cm:
            TS.open_in_editor('/Engine/BasicShapes/Cube')
        self.assertIn('audio assets only', str(cm.exception))


class TestTemplates(MetaSoundToolsetTestCase):

    def test_create_wave_player_graph_shape(self):
        if not _engine_wave_available():
            self.skipTest(f'{ENGINE_WAVE} not available')
        name = _unique('t_wp')
        info = TS.create_wave_player(
            name, ENGINE_WAVE, stereo=True, gain_db=-6.0,
            pitch_shift_semitones=0.0)
        self._builders.append(name)
        input_names = [m.name for m in info.inputs]
        self.assertIn('GainDb', input_names)
        self.assertIn('PitchShift', input_names)
        # 4 tracked nodes: wave player, db->gain, 2x multiply (stereo).
        self.assertEqual(len(list(info.tracked_node_ids)), 4)
        value = TS.get_graph_input_default(name, 'GainDb')
        self.assertAlmostEqual(value.float_value, -6.0, places=4)
        self.assertTrue(TS.has_interface(name, 'UE.Source.OneShot'))

    def test_create_wave_player_loop_drops_oneshot(self):
        if not _engine_wave_available():
            self.skipTest(f'{ENGINE_WAVE} not available')
        name = _unique('t_wploop')
        TS.create_wave_player(name, ENGINE_WAVE, stereo=False, loop=True)
        self._builders.append(name)
        self.assertFalse(TS.has_interface(name, 'UE.Source.OneShot'))

    def test_create_wave_player_reports_edges(self):
        if not _engine_wave_available():
            self.skipTest(f'{ENGINE_WAVE} not available')
        name = _unique('t_wpedges')
        info = TS.create_wave_player(name, ENGINE_WAVE, stereo=False,
                                     loop=True)
        self._builders.append(name)
        # Mono loop wiring: OnPlay->Play, PitchShift->, GainDb->Db,
        # Wave->Multiply, Db->Multiply, Multiply->graph out.
        self.assertEqual(len(list(info.edges)), 6)
        # The wave player is the template's first tracked node.
        wave_id = list(info.tracked_node_ids)[0]
        self.assertEqual(
            dict(info.tracked_node_classes)[wave_id],
            'UE.Wave Player.Mono')
        self.assertTrue(any(
            e.to_node_id == wave_id and e.to_input == 'Play'
            for e in info.edges))
        # Graph-member pins carry the empty node id per the convention.
        self.assertTrue(any(e.from_node_id == '' for e in info.edges))
        self.assertTrue(any(e.to_node_id == '' for e in info.edges))

    def test_interface_removal_prunes_its_edges(self):
        # Review pass-2 N5: removing UE.Source.OneShot deletes its
        # OnFinished member and that connection in the engine; the ledger
        # must drop the completion edge too, and re-adding the interface
        # reconnects nothing, so the edge must stay gone.
        if not _engine_wave_available():
            self.skipTest(f'{ENGINE_WAVE} not available')
        name = _unique('t_wpos')
        info = TS.create_wave_player(name, ENGINE_WAVE, stereo=False,
                                     loop=False)
        self._builders.append(name)
        finished = 'UE.Source.OneShot.OnFinished'

        def completion_edges(edges):
            return [e for e in edges
                    if e.to_node_id == '' and e.to_input == finished]

        self.assertEqual(len(completion_edges(info.edges)), 1)
        before = len(list(info.edges))
        TS.remove_interface(name, 'UE.Source.OneShot')
        edges = list(TS.get_graph_info(name).edges)
        self.assertEqual(completion_edges(edges), [])
        self.assertEqual(len(edges), before - 1)
        # Members outside the interface keep their wiring (OnPlay -> Play).
        self.assertTrue(any(e.from_node_id == '' and e.to_input == 'Play'
                            for e in edges))
        TS.add_interface(name, 'UE.Source.OneShot')
        self.assertEqual(
            completion_edges(TS.get_graph_info(name).edges), [])

    def test_wave_player_mid_build_failure_unregisters(self):
        # Review F9: a mid-build raise must not strand a registered,
        # half-built builder (the module promise). Force a failure after
        # the first node lands.
        if not _engine_wave_available():
            self.skipTest(f'{ENGINE_WAVE} not available')
        name = _unique('t_wpfail')
        original = _core.register_node_handle
        calls = []

        def boom(builder_name, handle, class_display=''):
            calls.append(1)
            if len(calls) >= 2:
                raise RuntimeError('forced mid-build failure')
            return original(builder_name, handle, class_display)

        _core.register_node_handle = boom
        try:
            with self.assertToolRaisesRuntimeError() as cm:
                TS.create_wave_player(name, ENGINE_WAVE, stereo=False)
        finally:
            _core.register_node_handle = original
        self.assertIn('forced mid-build failure', str(cm.exception))
        self.assertNotIn(name, TS.list_builders())
        # Engine-side registration is gone too: the name is reusable.
        TS.create_wave_player(name, ENGINE_WAVE, stereo=False)
        self._builders.append(name)

    def test_create_wave_player_raises_on_bad_gain(self):
        if not _engine_wave_available():
            self.skipTest(f'{ENGINE_WAVE} not available')
        with self.assertToolRaisesRuntimeError() as cm:
            TS.create_wave_player(_unique('t'), ENGINE_WAVE, gain_db=99.0)
        self.assertIn('ValueError', str(cm.exception))

    def test_create_wave_player_raises_on_missing_wave(self):
        with self.assertToolRaisesRuntimeError() as cm:
            TS.create_wave_player(_unique('t'), '/Game/__nope__/NoWave')
        self.assertIn('FileNotFoundError', str(cm.exception))

    def test_create_random_oneshot_sets_inputs(self):
        if not _engine_wave_available():
            self.skipTest(f'{ENGINE_WAVE} not available')
        name = _unique('t_rnd')
        info = TS.create_random_oneshot(
            name, [ENGINE_WAVE], pitch_random_spread_semitones=2.0,
            volume_random_spread=0.25)
        self._builders.append(name)
        self.assertTrue(info.is_preset)
        value = TS.get_graph_input_default(name, 'PitchRandomSpread')
        self.assertAlmostEqual(value.float_value, 2.0, places=4)
        sounds = TS.get_graph_input_default(name, 'Sounds')
        self.assertTrue(list(sounds.asset_paths))

    def test_random_oneshot_schema_miss_unwinds_builder(self):
        # Review pass-2 F9 residual: the reference asset's input schema can
        # only be checked once the preset builder exists, so the docstrings
        # promise an unwind, not pre-creation validation - prove it. No
        # headless state changes the engine asset, so the schema miss is
        # injected: a subsystem proxy hands back a builder proxy that
        # reports no graph inputs; registration goes to the real subsystem.
        if not _engine_wave_available():
            self.skipTest(f'{ENGINE_WAVE} not available')
        real_subsystem_fn = _core.builder_subsystem
        real_subsystem = real_subsystem_fn()

        class _NoInputsBuilder:
            def __init__(self, inner):
                self.inner = inner

            def get_graph_input_names(self):
                return [], unreal.MetaSoundBuilderResult.SUCCEEDED

            def __getattr__(self, attr):
                return getattr(self.inner, attr)

        class _Subsystem:
            def create_source_preset_builder(self, name, reference):
                builder, result = (
                    real_subsystem.create_source_preset_builder(
                        name, reference))
                return _NoInputsBuilder(builder), result

            def register_builder(self, name, builder):
                return real_subsystem.register_builder(name, builder.inner)

            def __getattr__(self, attr):
                return getattr(real_subsystem, attr)

        name = _unique('t_rnd')
        _core.builder_subsystem = lambda: _Subsystem()
        try:
            with self.assertToolRaisesRuntimeError() as cm:
                TS.create_random_oneshot(name, [ENGINE_WAVE])
            self.assertIn('LookupError', str(cm.exception))
            self.assertIn('Sounds', str(cm.exception))
        finally:
            _core.builder_subsystem = real_subsystem_fn
        self.assertNotIn(name, list(TS.list_builders()))
        self.assertIsNone(real_subsystem.find_builder(name))

    def test_create_random_oneshot_raises_on_empty_waves(self):
        with self.assertToolRaisesRuntimeError() as cm:
            TS.create_random_oneshot(_unique('t'), [])
        self.assertIn('ValueError', str(cm.exception))

    def test_create_random_oneshot_raises_on_negative_spread(self):
        if not _engine_wave_available():
            self.skipTest(f'{ENGINE_WAVE} not available')
        with self.assertToolRaisesRuntimeError() as cm:
            TS.create_random_oneshot(
                _unique('t'), [ENGINE_WAVE],
                pitch_random_spread_semitones=-1.0)
        self.assertIn('ValueError', str(cm.exception))


class TestRenderedAudio(MetaSoundToolsetTestCase):
    """Rendered-audio capture + WAV analysis (the objective read-back)."""

    def _write_wav(self, path, seconds=0.25, freq=440.0, amplitude=0.5,
                   sample_rate=8000):
        import math
        import struct as _struct
        frames = int(seconds * sample_rate)
        samples = b''.join(
            _struct.pack('<h', int(amplitude * 32767 * math.sin(
                2 * math.pi * freq * i / sample_rate)))
            for i in range(frames))
        header = _struct.pack(
            '<4sI4s4sIHHIIHH4sI', b'RIFF', 36 + len(samples), b'WAVE',
            b'fmt ', 16, 1, 1, sample_rate, sample_rate * 2, 2, 16,
            b'data', len(samples))
        with open(path, 'wb') as f:
            f.write(header + samples)

    def _write_float_wav(self, path, samples, sample_rate=8000):
        import struct as _struct
        data = _struct.pack(f'<{len(samples)}f', *samples)
        header = _struct.pack(
            '<4sI4s4sIHHIIHH4sI', b'RIFF', 36 + len(data), b'WAVE',
            b'fmt ', 16, 3, 1, sample_rate, sample_rate * 4, 4, 32,
            b'data', len(data))
        with open(path, 'wb') as f:
            f.write(header + data)

    def test_analysis_refuses_non_finite_float_samples(self):
        # Verify-cold R7: all-NaN read as peak -144, rms -144, silent; one
        # NaN poisoned the RMS; inf passed as a peak.
        import tempfile
        nan, inf = float('nan'), float('inf')
        cases = (('all_nan', [nan] * 400),
                 ('one_nan', [0.5] * 399 + [nan]),
                 ('pos_inf', [0.25] * 200 + [inf] + [0.25] * 199),
                 ('neg_inf', [-inf] + [0.0] * 399))
        with tempfile.TemporaryDirectory(dir=_scratch_root()) as tmp:
            for label, samples in cases:
                path = os.path.join(tmp, f'{label}.wav')
                self._write_float_wav(path, samples)
                with self.assertToolRaisesRuntimeError() as cm:
                    TS.analyze_rendered_wav(path)
                self.assertIn('ValueError', str(cm.exception), label)
                self.assertIn('NaN or infinite', str(cm.exception), label)
            # Finite float data still measures.
            path = os.path.join(tmp, 'finite.wav')
            self._write_float_wav(path, [0.5, -0.5] * 200)
            info = TS.analyze_rendered_wav(path)
            self.assertAlmostEqual(info.peak_db, -6.02, places=1)
            self.assertFalse(info.is_silent)

    def test_analyze_rendered_wav_measures_sine(self):
        import os
        import tempfile
        with tempfile.TemporaryDirectory(dir=_scratch_root()) as tmp:
            path = os.path.join(tmp, 'sine.wav')
            self._write_wav(path, seconds=0.25, amplitude=0.5)
            info = TS.analyze_rendered_wav(path)
            self.assertAlmostEqual(info.duration_seconds, 0.25, places=2)
            self.assertEqual(info.sample_rate, 8000)
            self.assertEqual(info.channels, 1)
            self.assertEqual(info.bits_per_sample, 16)
            # 0.5 amplitude sine: peak -6 dB, RMS ~ -9 dB.
            self.assertAlmostEqual(info.peak_db, -6.0, delta=0.3)
            self.assertAlmostEqual(info.rms_db, -9.0, delta=0.5)
            self.assertFalse(info.is_silent)

    def test_analyze_rendered_wav_flags_silence(self):
        import os
        import tempfile
        with tempfile.TemporaryDirectory(dir=_scratch_root()) as tmp:
            path = os.path.join(tmp, 'quiet.wav')
            self._write_wav(path, amplitude=0.0001)
            info = TS.analyze_rendered_wav(path)
            self.assertTrue(info.is_silent)

    def test_analyze_rendered_wav_raises_on_missing(self):
        with self.assertToolRaisesRuntimeError() as cm:
            TS.analyze_rendered_wav(
                os.path.join(_scratch_root(), 'definitely_not_here.wav'))
        self.assertIn('FileNotFoundError', str(cm.exception))

    def test_analyze_rendered_wav_refuses_paths_outside_the_project(self):
        import tempfile
        outside = [
            os.path.join(tempfile.gettempdir(), 'anything.wav'),
            os.path.join(
                unreal.Paths.convert_relative_path_to_full(
                    unreal.Paths.engine_dir()), 'anything.wav'),
            os.path.join(_scratch_root(), '..', '..', '..', '..', 'up.wav'),
        ]
        for path in outside:
            with self.assertToolRaisesRuntimeError() as cm:
                TS.analyze_rendered_wav(path)
            self.assertIn('PermissionError', str(cm.exception))

    def test_analyze_rendered_wav_refuses_degenerate_headers(self):
        import struct as _struct
        import tempfile
        headers = {
            'no_channels': _struct.pack(
                '<4sI4s4sIHHIIHH4sI', b'RIFF', 36, b'WAVE', b'fmt ', 16,
                1, 0, 8000, 0, 2, 16, b'data', 0),
            'no_rate': _struct.pack(
                '<4sI4s4sIHHIIHH4sI', b'RIFF', 36, b'WAVE', b'fmt ', 16,
                1, 1, 0, 0, 2, 16, b'data', 0),
            'short_fmt': _struct.pack(
                '<4sI4s4sI4s', b'RIFF', 16, b'WAVE', b'fmt ', 4, b'abcd'),
        }
        with tempfile.TemporaryDirectory(dir=_scratch_root()) as tmp:
            for label, header in headers.items():
                path = os.path.join(tmp, f'{label}.wav')
                with open(path, 'wb') as f:
                    f.write(header)
                with self.assertToolRaisesRuntimeError() as cm:
                    TS.analyze_rendered_wav(path)
                self.assertIn('ValueError', str(cm.exception))

    def test_analyze_rendered_wav_refuses_network_paths_lexically(self):
        # realpath opens the path, so a network path has to be refused
        # before it: the refusal must not need the network to answer. The
        # NT object-manager forms carry no leading '\\' (cold read C2).
        saved = unreal.Paths.convert_relative_path_to_full(
            unreal.Paths.project_saved_dir()).replace('/', '\\').rstrip('\\')
        if saved.startswith('\\\\'):
            self.skipTest('this project lives on a network share')
        for path in ('\\\\example.invalid\\share\\x.wav',
                     '//example.invalid/share/x.wav',
                     '\\\\?\\C:\\x.wav',
                     '\\??\\UNC\\example.invalid\\share\\x.wav',
                     '/??/UNC/example.invalid/share/x.wav',
                     '\\.\\??\\UNC\\example.invalid\\share\\x.wav',
                     '\\??\\GLOBALROOT\\Device\\Mup\\example.invalid\\s\\x.wav',
                     '\\\\?\\UNC\\example.invalid\\share\\x.wav',
                     '\\\\.\\UNC\\example.invalid\\share\\x.wav',
                     # Spelled under Saved but through a \\?\ prefix: the
                     # allowlist is on the plain spelling only.
                     f'\\\\?\\{saved}\\x.wav',
                     'relative\\x.wav'):
            with self.assertRaises(PermissionError) as cm:
                rendering.ensure_inside_project(path)
            self.assertIn('before anything is opened', str(cm.exception))
        for path in ('', 'x\x00.wav'):
            with self.assertRaises(PermissionError) as cm:
                rendering.ensure_inside_project(path)
            self.assertIn('not a usable file path', str(cm.exception))
        # Plain spellings under the roots pass the lexical gate, however
        # cased or slashed, and '..' that stays inside does too.
        for path in (f'{saved}\\x.wav', f'{saved.lower()}/x.wav',
                     f'{saved}\\sub\\..\\x.wav'):
            rendering.ensure_inside_project(path)

    def test_analyze_rendered_wav_refuses_links_leading_out(self):
        # The resolved check still runs after the lexical one, for a
        # junction or symlink inside the project that leads back out.
        import tempfile
        link = os.path.join(_scratch_root(), _unique('out'))
        try:
            os.symlink(tempfile.gettempdir(), link, target_is_directory=True)
        except OSError as exc:
            self.skipTest(f'cannot make a symlink here: {exc}')
        try:
            with self.assertRaises(PermissionError) as cm:
                rendering.ensure_inside_project(os.path.join(link, 'x.wav'))
            self.assertIn('link or junction', str(cm.exception))
        finally:
            os.rmdir(link)

    def test_analyze_rendered_wav_refuses_other_file_types(self):
        import tempfile
        with tempfile.TemporaryDirectory(dir=_scratch_root()) as tmp:
            path = os.path.join(tmp, 'settings.ini')
            with open(path, 'wb') as f:
                f.write(b'RIFF\x00\x00\x00\x00WAVE')
            with self.assertToolRaisesRuntimeError() as cm:
                TS.analyze_rendered_wav(path)
            self.assertIn('not a .wav', str(cm.exception))

    def test_analysis_refuses_a_flood_of_empty_chunks(self):
        # Each zero-size chunk costs one loop, so a large file of them
        # would hold the game thread for minutes.
        import struct as _struct
        import tempfile
        with tempfile.TemporaryDirectory(dir=_scratch_root()) as tmp:
            path = os.path.join(tmp, 'chunks.wav')
            with open(path, 'wb') as f:
                f.write(_struct.pack('<4sI4s', b'RIFF', 0, b'WAVE'))
                f.write(b'JUNK\x00\x00\x00\x00' * 5000)
            with self.assertToolRaisesRuntimeError() as cm:
                TS.analyze_rendered_wav(path)
            self.assertIn('RIFF chunks', str(cm.exception))

    def test_analysis_stops_at_the_byte_cap(self):
        # max_seconds trusts the header's sample rate, so the byte cap is
        # what bounds a file that lies about it.
        import tempfile
        with tempfile.TemporaryDirectory(dir=_scratch_root()) as tmp:
            path = os.path.join(tmp, 'capped.wav')
            self._write_wav(path, seconds=0.25, sample_rate=8000)
            saved_cap = rendering.MAX_ANALYSIS_BYTES
            rendering.MAX_ANALYSIS_BYTES = 800
            try:
                info = TS.analyze_rendered_wav(path)
            finally:
                rendering.MAX_ANALYSIS_BYTES = saved_cap
        self.assertAlmostEqual(info.duration_seconds, 0.25, places=2)
        self.assertAlmostEqual(info.analyzed_seconds, 0.05, places=3)

    def test_analyze_rendered_wav_raises_on_junk(self):
        import os
        import tempfile
        with tempfile.TemporaryDirectory(dir=_scratch_root()) as tmp:
            path = os.path.join(tmp, 'junk.wav')
            with open(path, 'wb') as f:
                f.write(b'not a riff file at all, not even close')
            with self.assertToolRaisesRuntimeError() as cm:
                TS.analyze_rendered_wav(path)
            self.assertIn('ValueError', str(cm.exception))

    def test_analyze_rendered_wav_flags_partial_flush(self):
        # A WAV whose data chunk is shorter than its header claims (the
        # async exporter mid-flush) must raise the friendly retry
        # guidance, not a raw struct.error or a misreported duration.
        import os
        import tempfile
        with tempfile.TemporaryDirectory(dir=_scratch_root()) as tmp:
            path = os.path.join(tmp, 'partial.wav')
            self._write_wav(path, seconds=0.25)
            with open(path, 'rb+') as f:
                f.truncate(os.path.getsize(path) - 101)
            with self.assertToolRaisesRuntimeError() as cm:
                TS.analyze_rendered_wav(path)
            self.assertIn('ValueError', str(cm.exception))
            self.assertIn('finalize', str(cm.exception))

    def test_analyze_rendered_wav_flags_sub_header_file(self):
        # Review pass-2 H-F8 residual: a file shorter than the 12-byte RIFF
        # header (an export caught before its header landed) must get the
        # friendly retry guidance, not a raw struct.error.
        import os
        import tempfile
        with tempfile.TemporaryDirectory(dir=_scratch_root()) as tmp:
            path = os.path.join(tmp, 'eight.wav')
            with open(path, 'wb') as f:
                f.write(b'RIFF\x00\x00\x00\x00')
            with self.assertToolRaisesRuntimeError() as cm:
                TS.analyze_rendered_wav(path)
            self.assertIn('ValueError', str(cm.exception))
            self.assertIn('finalize', str(cm.exception))
            self.assertNotIn('struct.error', str(cm.exception))

    def test_analyze_rendered_wav_raises_on_bad_cap(self):
        with self.assertToolRaisesRuntimeError() as cm:
            TS.analyze_rendered_wav('whatever.wav', max_seconds=0.0)
        self.assertIn('ValueError', str(cm.exception))

    def test_start_recording_raises_on_bad_name(self):
        with self.assertToolRaisesRuntimeError() as cm:
            TS.start_output_recording('bad name!')
        self.assertIn('ValueError', str(cm.exception))

    def test_start_recording_raises_on_filesystem_hazards(self):
        for bad in ('NUL', 'con', 'ctl\x01', 'nul\x00byte', '../up'):
            with self.assertToolRaisesRuntimeError() as cm:
                TS.start_output_recording(bad)
            self.assertIn('ValueError', str(cm.exception))

    def test_start_recording_raises_on_bad_duration(self):
        with self.assertToolRaisesRuntimeError() as cm:
            TS.start_output_recording(_unique('t_rec'), 0.0)
        self.assertIn('ValueError', str(cm.exception))

    def test_finish_recording_raises_on_unknown(self):
        with self.assertToolRaisesRuntimeError() as cm:
            TS.finish_output_recording(_unique('t_nope'))
        self.assertIn('LookupError', str(cm.exception))

    def test_recording_lifecycle(self):
        import os
        name = _unique('t_rec')
        status = self._start_recording(name, 5.0)
        try:
            self.assertEqual(status.recording_name, name)
            self.assertIn(name, list(TS.list_output_recordings()))
            # Duplicate name refused while in flight.
            with self.assertToolRaisesRuntimeError() as cm:
                TS.start_output_recording(name, 5.0)
            self.assertIn('RuntimeError', str(cm.exception))
        finally:
            info = TS.finish_output_recording(name, analyze=False)
        self.assertTrue(info.wav_path.endswith(f'{name}.wav'))
        self.assertNotIn(name, list(TS.list_output_recordings()))
        # The export is asynchronous; tolerate (and clean) the file if it
        # landed, but do not require an audio device to have produced one.
        from metasound_toolset import rendering as _rendering
        if _rendering.wait_for_file(info.wav_path, 5.0):
            os.remove(info.wav_path)

    async def test_teardown_finishes_only_recordings_the_test_started(self):
        # Micro-delta M5: teardown finished every listed recording, so one
        # test run inside a working editor stopped and exported a user's
        # take. A recording already running at a test's setUp is left
        # recording; the one the test started is finished.
        reverb = '/Engine/EngineSounds/Submixes/MasterReverbSubmixDefault'
        users = _unique('t_usertake')
        own = _unique('t_owntake')
        self._start_recording(users, 5.0)
        try:
            probe = self._probe()
            probe._start_recording(own, 5.0, reverb)
            await probe.asyncTearDown()
            probe.tearDown()
            self.assertIn(users, list(TS.list_output_recordings()))
            self.assertNotIn(own, list(TS.list_output_recordings()))
        finally:
            await self._discard(own)
            await self._discard(users)

    def _probe(self) -> 'TestRenderedAudio':
        """A second case run by hand inside this one, as a nested test."""
        probe = TestRenderedAudio('test_validate_recording_name_rejects_junk')
        probe.setUp()
        return probe

    async def test_teardown_consumes_only_state_the_test_owns(self):
        # Micro-delta N3: teardown took set differences of names, which
        # protects the names present at setUp and proves no ownership. A
        # recording already running at setUp that the cap stopped mid-test
        # lost its cap report to teardown; one someone else started during
        # the test's awaits was finished by it; a same-name replacement could
        # not be told apart. Teardown now consumes only the sessions the test
        # started and the cap reports made from them, each by identity.
        reverb = '/Engine/EngineSounds/Submixes/MasterReverbSubmixDefault'
        users, other = _unique('t_usercap'), _unique('t_othertake')
        owns = [_unique('t_owncap') for _ in range(3)]
        try:
            # 1. A take already running when the probe starts is capped by
            # the real tick callback while the probe runs.
            self._start_recording(users, 5.0)
            user_session = rendering._recordings[users.casefold()]
            probe = self._probe()
            probe._start_recording(owns[0], 5.0, reverb)
            user_session.started_at -= rendering.RECORDING_CAP_SECONDS
            deadline = time.monotonic() + 15.0
            while (users.casefold() not in rendering._capped
                   and time.monotonic() < deadline):
                await asyncio.sleep(0.05)
            report = rendering._capped.get(users.casefold())
            self.assertIsNotNone(report, 'the cap did not stop the take')
            self.assertIs(report[2], user_session)
            # The take's export holds the writer first. With nothing playing
            # it writes no WAV, and the no-file wait scales with its age,
            # about 26 s once backdated past the cap: longer than teardown's
            # own settle. Let it go on real ticks before the nested teardown.
            no_file_wait = (rendering.EXPORT_NO_FILE_SECONDS
                            + rendering.EXPORT_NO_FILE_PER_SECOND
                            * (rendering.RECORDING_CAP_SECONDS + 60.0))
            self.assertTrue(await _settle_exports(no_file_wait + 5.0))
            await probe.asyncTearDown()
            probe.tearDown()
            self.assertIs(rendering._capped.get(users.casefold()), report)
            self.assertNotIn(owns[0], list(TS.list_output_recordings()))
            # 2. A take someone else starts during the probe's test stays.
            probe = self._probe()
            probe._start_recording(owns[1], 5.0, reverb)
            self._start_recording(other, 5.0)  # not the probe's
            await probe.asyncTearDown()
            probe.tearDown()
            self.assertIn(other, list(TS.list_output_recordings()))
            self.assertNotIn(owns[1], list(TS.list_output_recordings()))
            # 3. Under the probe's own name: a replacement session, and a cap
            # report made from another session, are not the probe's.
            probe = self._probe()
            probe._start_recording(owns[2], 5.0, reverb)
            key = owns[2].casefold()
            rendering.finish_recording(owns[2])
            self.assertTrue(await _settle_exports())
            rendering._pending_wavs.pop(key, None)  # no WAV: nothing played
            self._start_recording(owns[2], 5.0, reverb)
            replacement = rendering._recordings[key]
            foreign_report = (owns[2], 'elsewhere.wav', object())
            rendering._capped[key] = foreign_report
            await probe.asyncTearDown()
            probe.tearDown()
            self.assertIs(rendering._recordings.get(key), replacement)
            self.assertIs(rendering._capped.get(key), foreign_report)
            del rendering._capped[key]
        finally:
            for name in (users, other, *owns):
                await self._discard(name)

    async def test_teardown_finishes_a_render_whose_audition_failed(self):
        # Micro-delta MD3-2: start_source_render creates its recording first.
        # When the audition then fails while another export holds the
        # writer, its rollback finish is refused, so the recording stays
        # listed and the audition error is raised. The helper took ownership
        # only once the tool returned, so that recording escaped teardown
        # and kept the master slot. The session the start created is owned
        # even when the start raises; one already listed under the name,
        # met by a start refused before it creates anything, is not.
        from unittest import mock
        from metasound_toolset import toolset as toolset_module
        src = self._source(output_format='Mono', one_shot=False)
        wav_name = _unique('t_halfstart')
        key = rendering._name_key(wav_name)

        def no_component(**_kwargs):
            # As _start_component_audition raises when create_sound2d
            # returns None.
            raise RuntimeError('Failed to create an audition audio '
                               'component (modeled).')

        await _settle_exports()
        busy = self._hold_writer()
        orphan = None
        try:
            probe = self._probe()
            with mock.patch.object(toolset_module,
                                   '_start_component_audition',
                                   no_component):
                with self.assertToolRaisesRuntimeError() as cm:
                    probe._start_render(src, wav_name=wav_name)
            self.assertIn('modeled', str(cm.exception))
            orphan = rendering._recordings.get(key)
            self.assertIsNotNone(orphan, 'the failed start left nothing '
                                 'listed: the writer hold did not apply')
            self.assertTrue(any(s is orphan for s in probe._owned_recordings),
                            'the start created the recording, and its '
                            'test does not own it')
            # A second start under the name is refused before it creates
            # anything, and does not claim the session it found listed.
            other = self._probe()
            with self.assertToolRaisesRuntimeError():
                other._start_render(src, wav_name=wav_name)
            self.assertEqual(other._owned_recordings, [])
            other.tearDown()
            # Once the writer is free, the probe's teardown finishes it.
            self._release_writer(busy)
            await probe.asyncTearDown()
            probe.tearDown()
            self.assertNotIn(key, rendering._recordings)
        finally:
            self._release_writer(busy)
            if orphan is not None and not any(
                    s is orphan for s in self._owned_recordings):
                self._owned_recordings.append(orphan)  # this test made it
            await self._discard(wav_name)

    async def test_recording_cap_stops_exports_and_reports(self):
        # Security pass S12: nothing in the engine stops a recording, so
        # one left running grows until memory runs out. The cap stops and
        # exports it, and the finish call says so with the WAV path.
        name = _unique('t_cap')
        self._start_recording(name, 5.0)
        key = name.casefold()
        try:
            self.assertIsNotNone(rendering._cap_handle)
            session = rendering._recordings[key]
            # Not yet old enough: nothing happens.
            self.assertEqual(rendering.enforce_recording_cap(), [])
            self.assertIn(name, list(TS.list_output_recordings()))
            # Older than the cap: stopped and exported by the tick check.
            stopped = rendering.enforce_recording_cap(
                now=session.started_at + rendering.RECORDING_CAP_SECONDS)
            self.assertEqual(stopped, [name])
            self.assertNotIn(name, list(TS.list_output_recordings()))
            await _settle_exports()
            self.assertIsNone(rendering._cap_handle)
            with self.assertToolRaisesRuntimeError() as cm:
                TS.finish_output_recording(name, analyze=False)
            self.assertIn('ran past the cap', str(cm.exception))
            self.assertIn(f'{name}.wav', str(cm.exception))
            # Reported once; the name is free again.
            with self.assertToolRaisesRuntimeError() as cm:
                TS.finish_output_recording(name, analyze=False)
            self.assertIn('LookupError', str(cm.exception))
        finally:
            await self._discard(name)

    async def test_recording_cap_releases_its_tick_callback_when_idle(self):
        name = _unique('t_capidle')
        self._start_recording(name, 5.0)
        self.assertIsNotNone(rendering._cap_handle)
        info = TS.finish_output_recording(name, analyze=False)
        # Released once its export no longer holds the engine writer (R4).
        await _settle_exports()
        self.assertIsNone(rendering._cap_handle)
        if rendering.wait_for_file(info.wav_path, 5.0):
            os.remove(info.wav_path)
        rendering._pending_wavs.pop(name.casefold(), None)

    async def test_recording_cap_waits_out_pie_and_then_stops(self):
        # Verify-cold R3: during PIE no world context reaches the editor's
        # audio device (the engine mutes the editor world), so the cap must
        # keep the session and stop it once PIE ends - never report a stop
        # that did not happen (the first R3 fix did, fired live 2026-11-03).
        # PIE is modeled by the accessor's refusal.
        name = _unique('t_cappie')
        self._start_recording(name, 5.0)
        key = name.casefold()
        session = rendering._recordings[key]
        overdue = session.started_at + rendering.RECORDING_CAP_SECONDS
        real_editor_world = _core.editor_world

        def refuse():
            raise RuntimeError('PIE is running (modeled).')

        _core.editor_world = refuse
        try:
            self.assertEqual(rendering.enforce_recording_cap(now=overdue), [])
            self.assertEqual(rendering.enforce_recording_cap(now=overdue), [])
            self.assertIn(name, list(TS.list_output_recordings()))
            self.assertTrue(session.cap_wait_logged)
            self.assertIsNotNone(rendering._cap_handle)
            self.assertNotIn(key, rendering._capped)
        finally:
            _core.editor_world = real_editor_world
        try:
            # PIE over: the next tick stops and exports it.
            self.assertEqual(
                rendering.enforce_recording_cap(now=overdue), [name])
            self.assertNotIn(name, list(TS.list_output_recordings()))
        finally:
            await self._discard(name)

    def test_reload_adopts_a_running_recording(self):
        # Verify-cold R3, reload half: a reload re-ran this module with
        # fresh dicts, orphaning the engine recording and the tick callback.
        import importlib
        name = _unique('t_reload')
        self._start_recording(name, 5.0)
        key = name.casefold()
        handle = rendering._cap_handle
        held = rendering._recordings[key].start_submix
        try:
            importlib.reload(rendering)
            self.assertIn(key, rendering._recordings)
            self.assertIs(rendering._cap_handle, handle)
            self.assertIn(name, list(TS.list_output_recordings()))
            # Micro-delta N1: the start object rides the reload with the
            # session, so the stop is still given that very object.
            self.assertIsNotNone(held)
            self.assertIs(rendering._recordings[key].start_submix, held)
            self.assertIs(rendering._session_submix(
                rendering._recordings[key]), held)
        finally:
            info = TS.finish_output_recording(name, analyze=False)
            if rendering.wait_for_file(info.wav_path, 2.0):
                os.remove(info.wav_path)
            rendering._pending_wavs.pop(key, None)

    async def asyncSetUp(self):
        # One export at a time: each test starts with the writer free,
        # released by real ticks (an earlier test's export may still hold
        # it).
        await _settle_exports()

    def test_reload_drops_the_queue_codes_stale_cap_report(self):
        # Micro-delta M6: the pause-and-queue code recorded a cap-expired
        # queued session in both its queue and _capped. The reload that
        # re-lists it kept the cap report, so a finish raised "nothing is
        # recording" and the source finish consumed its pairing and audition
        # first, while the recording stayed listed. Modeled session (no
        # engine recorder behind it) and modeled held writer.
        import importlib
        import time
        name = _unique('t_queued')
        key = name.casefold()
        queued = rendering.RecordingSession(
            name=name, directory=rendering.renders_dir(),
            expected_duration=1.0, submix_path='/Fake/QueuedSubmix',
            started_at=time.monotonic())
        rendering._export_queue = [queued]
        rendering._capped[key] = (name, os.path.join(
            rendering.renders_dir(), f'{name}.wav'))
        busy = None
        try:
            importlib.reload(rendering)
            self.assertIs(rendering._recordings.get(key), queued)
            self.assertNotIn(key, rendering._capped)
            self.assertFalse(hasattr(rendering, '_export_queue'))
            # A surviving report still loses to the listed session.
            rendering._capped[key] = (name, 'stale.wav')
            rendering.track_source_render(name, f'__render_{name}')
            busy = self._hold_writer()
            with self.assertRaises(RuntimeError) as cm:
                rendering.refuse_if_cannot_stop(name)
            self.assertIn(busy, str(cm.exception))
            with self.assertToolRaisesRuntimeError() as cm:
                TS.finish_source_render(name, analyze=False)
            self.assertIn('still recording and listed', str(cm.exception))
            self.assertIn(key, rendering._source_renders)
            with self.assertRaises(RuntimeError) as cm:
                rendering.finish_recording(name)
            self.assertIn('still recording and listed', str(cm.exception))
            self.assertNotIn('ran past the cap', str(cm.exception))
            self.assertIs(rendering._recordings.get(key), queued)
        finally:
            if busy is not None:
                self._release_writer(busy)
            rendering._recordings.pop(key, None)
            rendering._capped.pop(key, None)
            rendering._source_renders.pop(key, None)
            globals_ = vars(rendering)
            globals_.pop('_export_queue', None)

    def _install_fake(self, flight: dict) -> dict:
        """Install a modeled export as the one holding the engine writer.

        Never over a real one (micro-delta M4): a modeled flight replacing
        a real export's would let the next stop cancel its WAV. Fakes are
        marked, and only marked flights are ever cleared by a test.
        """
        current = rendering._export_in_flight
        if current is not None and not current.get('fake'):
            self.skipTest('a real export still holds the engine writer; a '
                          'modeled one must not replace it')
        flight['fake'] = True
        rendering._export_in_flight = flight
        return flight

    def _clear_fake(self) -> None:
        current = rendering._export_in_flight
        if current is not None and current.get('fake'):
            rendering._export_in_flight = None

    def _hold_writer(self) -> str:
        """Model another export holding the engine writer; returns its WAV.

        The held export's file is incomplete (a header claiming more than
        the file holds) and no completion is ever counted for it, so no
        release rule can free it during the test.
        """
        import struct
        busy = os.path.join(rendering.renders_dir(),
                            f"{_unique('t_busy')}.wav")
        with open(busy, 'wb') as f:
            f.write(b'RIFF' + struct.pack('<I', 1 << 20) + b'WAVE'
                    + bytes(64))
        self._flight(busy)
        return busy

    def _release_writer(self, busy: str) -> None:
        self._clear_fake()
        if os.path.exists(busy):
            os.remove(busy)

    async def _discard(self, name: str) -> None:
        """Finish and forget this test's own sessions under the name.

        Only sessions it owns and reports made from them (micro-delta N3);
        the WAV and the pending fence under the name are its own render's.
        """
        key = name.casefold()
        for session in self._owned_recordings:
            if rendering._name_key(session.name) != key:
                continue
            self._drop_cap_report(session)
            if self._still_listed(session):
                TS.finish_output_recording(session.name, analyze=False)
        await _settle_exports()
        wav_path = os.path.join(rendering.renders_dir(), f'{name}.wav')
        if rendering.wait_for_file(wav_path, 1.0):
            os.remove(wav_path)
        rendering._pending_wavs.pop(key, None)

    async def test_finish_refused_while_another_export_holds_the_writer(self):
        # Verify-cold R4 + delta D2: every export goes through ONE engine
        # writer, and a stop while another export holds it can cancel that
        # WAV. The finish is refused instead, and the recording is left
        # exactly as it was: recording, listed, under the cap. Nothing is
        # recording into it here, so this is also D2's empty capture, the
        # one the old pause call silently failed on.
        import inspect
        busy = self._hold_writer()
        name = _unique('t_held')
        key = name.casefold()
        self._start_recording(name, 5.0)
        try:
            with self.assertToolRaisesRuntimeError() as cm:
                TS.finish_output_recording(name, analyze=False)
            self.assertIn(busy, str(cm.exception))
            self.assertIn('still recording and listed', str(cm.exception))
            self.assertIn(name, list(TS.list_output_recordings()))
            self.assertIn(key, rendering._recordings)
            self.assertNotIn(key, rendering._pending_wavs)
            self.assertIsNotNone(rendering._cap_handle)
            # Nothing paused, nothing queued: the pause call was the D2 hole.
            self.assertNotIn('pause_recording_output',
                             inspect.getsource(rendering))
            self.assertFalse(hasattr(rendering, '_export_queue'))
            # The writer free again, the same finish goes through.
            self._release_writer(busy)
            info = TS.finish_output_recording(name, analyze=False)
            self.assertTrue(info.wav_path.endswith(f'{name}.wav'))
            self.assertNotIn(name, list(TS.list_output_recordings()))
        finally:
            self._release_writer(busy)
            await self._discard(name)

    async def test_finish_never_blocks_on_another_export(self):
        # Delta D3: the finish slept the game thread until the previous
        # export's file appeared, on a deadline of 0.5 s + 0.02 s per second
        # of that recording's whole age: about 432 s behind an empty capture
        # the cap stopped after six hours of PIE, before the timeout was
        # even checked, and before this recording was stopped. Now the
        # finish is refused at once and this recording keeps recording.
        import inspect
        import time
        await _settle_exports()
        missing = os.path.join(rendering.renders_dir(),
                               f"{_unique('t_never')}.wav")
        self._flight(missing, ran_seconds=6 * 3600.0)
        name = _unique('t_noblock')
        self._start_recording(name, 5.0)
        try:
            began = time.monotonic()
            with self.assertToolRaisesRuntimeError() as cm:
                TS.finish_output_recording(name, analyze=False)
            self.assertLess(time.monotonic() - began, 0.5)
            self.assertIn(missing, str(cm.exception))
            self.assertIn(name, list(TS.list_output_recordings()))
            self.assertNotIn(
                'wait', inspect.signature(rendering._export_writer_free)
                .parameters)
            self.assertNotIn('time.sleep',
                             inspect.getsource(rendering._export_writer_free))
        finally:
            self._clear_fake()
            await self._discard(name)

    async def test_refused_recording_stays_under_the_cap(self):
        # Delta D2: the queued session left _recordings, so the cap could
        # not reach it. A refused one stays, and the cap stops it once the
        # writer is free.
        busy = self._hold_writer()
        name = _unique('t_heldcap')
        key = name.casefold()
        self._start_recording(name, 5.0)
        session = rendering._recordings[key]
        overdue = session.started_at + rendering.RECORDING_CAP_SECONDS
        try:
            self.assertEqual(rendering.enforce_recording_cap(now=overdue), [])
            self.assertIn(key, rendering._recordings)
            self.assertNotIn(key, rendering._capped)
            self.assertTrue(session.cap_wait_logged)
            self._release_writer(busy)
            self.assertEqual(
                rendering.enforce_recording_cap(now=overdue), [name])
            self.assertNotIn(key, rendering._recordings)
            self.assertIn(key, rendering._capped)
        finally:
            self._release_writer(busy)
            await self._discard(name)

    async def test_cap_wait_warns_once_not_every_tick(self):
        # Delta D4: the queue drain warned on every tick it could not export,
        # so a queued export during PIE flooded the log at tick rate. With no
        # queue, every wait goes through the cap's once-per-recording flag:
        # here a held writer for fifty ticks, then modeled PIE for fifty.
        busy = self._hold_writer()
        name = _unique('t_once')
        key = name.casefold()
        self._start_recording(name, 5.0)
        session = rendering._recordings[key]
        overdue = session.started_at + rendering.RECORDING_CAP_SECONDS
        warnings = []
        real_warning = unreal.log_warning
        real_editor_world = _core.editor_world

        def counting(message):
            if name in str(message):
                warnings.append(str(message))
            real_warning(message)

        def refuse():
            raise RuntimeError('PIE is running (modeled).')

        unreal.log_warning = counting
        try:
            for _ in range(50):
                rendering._tick_count += 1
                rendering.enforce_recording_cap(now=overdue)
            self._release_writer(busy)
            _core.editor_world = refuse
            for _ in range(50):
                rendering._tick_count += 1
                rendering.enforce_recording_cap(now=overdue)
            self.assertEqual(len(warnings), 1, warnings)
            self.assertIn('cannot be stopped yet', warnings[0])
            self.assertIn(key, rendering._recordings)
            _core.editor_world = real_editor_world
            self.assertEqual(
                rendering.enforce_recording_cap(now=overdue), [name])
            self.assertEqual(len(warnings), 2)
            self.assertIn('was stopped and exported', warnings[1])
        finally:
            unreal.log_warning = real_warning
            _core.editor_world = real_editor_world
            self._release_writer(busy)
            await self._discard(name)

    async def test_refused_source_render_finish_keeps_the_render(self):
        # Delta D2 follow-through: finish_source_render consumed the render
        # pairing and stopped the audition before the recording's finish;
        # a refusal now comes first and leaves all three in place.
        src = self._source(one_shot=False)
        wav_name = _unique('t_heldrnd')
        self._start_render(src, wav_name=wav_name)
        busy = self._hold_writer()
        try:
            with self.assertToolRaisesRuntimeError() as cm:
                TS.finish_source_render(wav_name, analyze=False)
            self.assertIn(busy, str(cm.exception))
            self.assertIn(wav_name.casefold(), rendering._source_renders)
            self.assertIn(f'__render_{wav_name}', list(TS.list_auditions()))
            self.assertIn(wav_name, list(TS.list_output_recordings()))
            self._release_writer(busy)
            info = TS.finish_source_render(wav_name, analyze=False)
            self.assertTrue(info.wav_path.endswith(f'{wav_name}.wav'))
            self.assertNotIn(wav_name.casefold(), rendering._source_renders)
        finally:
            self._release_writer(busy)
            rendering._source_renders.pop(wav_name.casefold(), None)
            await self._discard(wav_name)

    async def test_cap_exports_two_overdue_recordings_in_turn(self):
        # Verify-cold R4: the cap loop stopped every overdue recording in
        # one tick, two exports racing for the one writer. The first stop
        # takes the writer; the second recording keeps recording, listed,
        # and is stopped on a later tick, once the writer is free.
        import time
        await _settle_exports()
        first, second = _unique('t_capa'), _unique('t_capb')
        self._start_recording(first, 5.0)
        self._start_recording(
            second, 5.0,
            '/Engine/EngineSounds/Submixes/MasterReverbSubmixDefault')
        keys = [first.casefold(), second.casefold()]
        try:
            latest = max(rendering._recordings[k].started_at for k in keys)
            overdue = latest + rendering.RECORDING_CAP_SECONDS
            stopped = rendering.enforce_recording_cap(now=overdue)
            self.assertEqual(stopped, [first])
            self.assertIsNotNone(rendering._export_in_flight)
            self.assertIn(keys[1], rendering._recordings)
            # Later ticks, real ones: the second goes once the first's
            # export is released (by its own completion, or by the no-file
            # rule when nothing played).
            deadline = time.monotonic() + 15.0
            while second not in stopped and time.monotonic() < deadline:
                await asyncio.sleep(0.05)
                stopped = rendering.enforce_recording_cap(now=overdue)
            self.assertEqual(stopped, [second])
            self.assertEqual(sorted(rendering._capped),
                             sorted(keys))
        finally:
            for name in (first, second):
                await self._discard(name)

    def _flight(self, path: str, subscribed: bool = True, age: float = 0.0,
                ran_seconds: float = 0.0) -> dict:
        import time
        flight = {
            'path': path, 'stopped_at': time.monotonic() - age,
            'stop_tick': rendering._tick_count, 'ran_seconds': ran_seconds,
            'subscribed': subscribed, 'ack_tick': None, 'stray_acks': 0,
            'ack_target': None, 'ack_fn': None,
            'seen_tick': None, 'seen_at': None, 'hold_logged': False,
            'fit_gated': True}  # issued by this version (micro-delta N2)
        return self._install_fake(flight)

    def _ticks(self, count: int) -> None:
        rendering._tick_count += count

    async def test_export_is_released_by_its_genuine_completion(self):
        # Delta D1: the writer is released by the engine's own completion,
        # which broadcasts the submix the stop was given. Micro-delta M4:
        # this test used to fire the completion handler by hand on real
        # exports, releasing the writer with nothing complete. Now a quiet
        # looping wave plays, each export is awaited on real ticks, and the
        # engine's completion releases it. Before the third, a Sound Submix
        # asset is created: micro-delta M1's trigger, after which the
        # round-3 stand-in submix stopped a recorder of its own.
        if not _engine_wave_available():
            self.skipTest(f'{ENGINE_WAVE} not available')
        src = _unique('t_ackwp')
        TS.create_wave_player(src, ENGINE_WAVE, loop=True, gain_db=-40.0)
        self._builders.append(src)
        TS.start_audition(src)
        reverb = '/Engine/EngineSounds/Submixes/MasterReverbSubmixDefault'
        mount = '/MSTRenderAuto/'
        unreal.AutomationUtilsBlueprintLibrary.add_automation_mount_point(
            mount)
        folder = f'{mount}{uuid.uuid4().hex[:8]}'
        eas = unreal.get_editor_subsystem(unreal.EditorAssetSubsystem)
        eas.make_directory(folder)
        landed = []
        try:
            for step, submix in enumerate((None, reverb, None)):
                if step == 2:
                    unreal.AssetToolsHelpers.get_asset_tools().create_asset(
                        _unique('SM_Trigger'), folder, unreal.SoundSubmix,
                        unreal.SoundSubmixFactory())
                self.assertTrue(await _settle_exports())
                name = _unique('t_ack')
                self._start_recording(name, 5.0, submix)
                await asyncio.sleep(0.5)  # real ticks: the recorder captures
                info = TS.finish_output_recording(name, analyze=False)
                self.assertNotIn(name, list(TS.list_output_recordings()))
                flight = rendering._export_in_flight
                self.assertIsNotNone(flight)
                self.assertTrue(flight['subscribed'])
                self.assertIsNone(flight['ack_tick'])
                if submix is None:
                    # M1: the master object itself, the one its start was
                    # given, never a stand-in.
                    master = rendering._master_submix()
                    self.assertIsInstance(master, unreal.SoundSubmix)
                    self.assertEqual(flight['ack_target'].get_path_name(),
                                     master.get_path_name())
                else:
                    self.assertTrue(flight['ack_target'].get_path_name()
                                    .startswith(submix))
                self.assertTrue(await _settle_exports())
                if rendering._wav_complete(info.wav_path):
                    # Released after a completion that fit it, none stray.
                    self.assertIsNotNone(flight['ack_tick'])
                    self.assertEqual(flight['stray_acks'], 0)
                    landed.append(step)
                    os.remove(info.wav_path)
                rendering._pending_wavs.pop(name.casefold(), None)
        finally:
            TS.stop_all_auditions()
            if eas.does_directory_exist(folder):
                eas.delete_directory(folder)
            unreal.AutomationUtilsBlueprintLibrary \
                .remove_automation_mount_point(mount)
        if 0 in landed:
            # The master captured before the submix asset; after it too.
            self.assertIn(2, landed, 'the master export after a submix '
                          "asset's creation did not land (micro-delta M1)")
        if landed != [0, 1, 2]:
            self.skipTest(f'exports {landed} of [0, 1, 2] wrote a WAV: no '
                          'audio rendered for the rest, so their release '
                          'was the no-file rule, not a completion')

    async def test_master_session_stops_through_the_object_it_started_with(self):
        # Micro-delta M1: the round-3 stand-in submix was registered as a
        # recorder of its own by any submix asset's creation, and master
        # stops then stopped it while the master kept recording. Start and
        # stop now pass the master submix object, the one AudioSettings
        # names and the device loaded.
        master = rendering._master_submix()
        self.assertIsInstance(master, unreal.SoundSubmix)
        settings = unreal.find_object(None, rendering.AUDIO_SETTINGS_DEFAULT)
        self.assertEqual(
            master.get_path_name(),
            settings.get_editor_property('MasterSubmix').export_text())
        name = _unique('t_master')
        self._start_recording(name, 5.0)
        session = rendering._recordings[name.casefold()]
        self.assertEqual(session.master_path, master.get_path_name())
        self.assertEqual(session.start_submix.get_path_name(),
                         master.get_path_name())
        self.assertIs(rendering._session_submix(session),
                      session.start_submix)
        await self._discard(name)
        # An explicit submix keeps no master path, and holds its own object.
        reverb = '/Engine/EngineSounds/Submixes/MasterReverbSubmixDefault'
        name = _unique('t_master')
        self._start_recording(name, 5.0, reverb)
        session = rendering._recordings[name.casefold()]
        self.assertIsNone(session.master_path)
        self.assertTrue(session.start_submix.get_path_name()
                        .startswith(reverb))
        await self._discard(name)

    def _occupy(self, path: str, occupant):
        """Model another submix renamed into a path the session started on.

        Every lookup this module could make of that path now answers with
        the occupant, as after a rename and a redirector fix-up (micro-delta
        N1). The Master Submix setting's lookup goes through find_object
        too, so a master path occupied this way is what the setting names.
        Returns the restore callable.
        """
        real_find, real_load = unreal.find_object, rendering._load_submix
        package = path.split('.')[0]

        def find(outer, name, *args, **kwargs):
            if name and name.split('.')[0] == package:
                return occupant
            return real_find(outer, name, *args, **kwargs)

        def load(submix_path):
            if submix_path and submix_path.split('.')[0] == package:
                return occupant
            return real_load(submix_path)

        unreal.find_object = find
        rendering._load_submix = load

        def restore():
            unreal.find_object = real_find
            rendering._load_submix = real_load
        return restore

    async def test_stop_addresses_the_started_submix_after_its_path_changes(
            self):
        # Micro-delta N1: the session kept the master's path and looked it up
        # again at stop, so a different submix renamed into that path (the
        # master renamed away, its redirector fixed up) would be stopped
        # while the started recorder ran on outside the cap. Explicit
        # submixes loaded their stored path again the same way. The stop is
        # now given the held start object, whatever the path names.
        reverb_path = '/Engine/EngineSounds/Submixes/MasterReverbSubmixDefault'
        master = rendering._master_submix()
        reverb = _core.load_asset_checked(reverb_path, unreal.SoundSubmix)
        for started_on, occupant in ((None, reverb), (reverb_path, master)):
            await _settle_exports()
            name = _unique('t_occupied')
            self._start_recording(name, 5.0, started_on)
            session = rendering._recordings[name.casefold()]
            held = session.start_submix.get_path_name()
            restore = self._occupy(held, occupant)
            try:
                # Every lookup now names the occupant; the stop does not.
                self.assertEqual(
                    unreal.find_object(None, held).get_path_name(),
                    occupant.get_path_name())
                rendering.finish_recording(name)
                flight = rendering._export_in_flight
                self.assertIsNotNone(flight)
                self.assertEqual(flight['ack_target'].get_path_name(), held)
            finally:
                restore()
                await self._discard(name)

    def test_session_without_a_held_object_never_looks_up_the_master(self):
        # Micro-delta N1, adopted sessions: one from a version that held no
        # object (round 4 kept only the path, earlier ones nothing) and one
        # whose start was given None stop through None, which the device
        # resolves to its own master by identity, never through a path or
        # the setting. An adopted explicit-submix session has one lookup
        # left, its stored path, since None would address the master.
        # Modeled sessions; no engine recorder behind them.
        master = rendering._master_submix()
        reverb_path = '/Engine/EngineSounds/Submixes/MasterReverbSubmixDefault'
        reverb = _core.load_asset_checked(reverb_path, unreal.SoundSubmix)
        restore = self._occupy(master.get_path_name(), reverb)
        try:
            for marker in (master.get_path_name(), '', None):
                adopted = rendering.RecordingSession(
                    name='t_adopted', directory=rendering.renders_dir(),
                    expected_duration=1.0, submix_path='',
                    master_path=marker)
                self.assertIsNone(adopted.start_submix)
                self.assertIsNone(rendering._session_submix(adopted))
        finally:
            restore()
        adopted = rendering.RecordingSession(
            name='t_adopted', directory=rendering.renders_dir(),
            expected_duration=1.0, submix_path=reverb_path)
        self.assertTrue(rendering._session_submix(adopted).get_path_name()
                        .startswith(reverb_path))

    async def test_complete_file_does_not_free_a_subscribed_export(self):
        # Delta D1: a complete WAV plus settle ticks was taken as proof the
        # completion had run; the worker can still be preempted before it
        # posts it (SampleBufferIO.cpp:770-775). Subscribed, only the
        # completion frees the writer.
        import time
        await _settle_exports()
        done = os.path.join(rendering.renders_dir(),
                            f"{_unique('t_done')}.wav")
        self._write_wav(done)
        flight = self._flight(done, age=5.0)
        try:
            for _ in range(5):
                self._ticks(1)
                time.sleep(0.05)
                self.assertFalse(rendering._export_writer_free())
            rendering._completion_handler(flight)(None)
            self._ticks(rendering.EXPORT_ACK_TICKS)
            self.assertTrue(rendering._export_writer_free())
        finally:
            self._clear_fake()
            os.remove(done)

    async def test_completion_frees_the_writer_only_after_its_broadcast(self):
        # Micro-delta M3: the engine resets the writer only after its whole
        # completion broadcast returns, and subscribers run one after
        # another, so a finish called by a later subscriber of the same
        # broadcast found the writer free and installed one the reset then
        # destroyed. The handler only records; a later post-tick releases.
        await _settle_exports()
        done = os.path.join(rendering.renders_dir(),
                            f"{_unique('t_bcast')}.wav")
        self._write_wav(done)
        flight = self._flight(done)
        reentrant = rendering.RecordingSession(
            name='t_reentrant', directory=rendering.renders_dir(),
            expected_duration=1.0, submix_path='/Fake/Submix')
        try:
            on_done = rendering._completion_handler(flight)
            on_done(None)  # this subscriber, inside the broadcast
            # The next subscriber, same broadcast: still held, refused.
            self.assertFalse(rendering._export_writer_free())
            with self.assertRaises(RuntimeError) as cm:
                rendering._refuse_while_writer_held(reentrant)
            self.assertIn(done, str(cm.exception))
            self.assertIs(rendering._export_in_flight, flight)
            # A double fire inside the broadcast changes nothing.
            on_done(None)
            self.assertFalse(rendering._export_writer_free())
            # The broadcast and the reset over, the next post-tick frees it.
            self._ticks(rendering.EXPORT_ACK_TICKS)
            self.assertTrue(rendering._export_writer_free())
            self.assertIsNone(rendering._export_in_flight)
        finally:
            self._clear_fake()
            os.remove(done)

    async def test_only_a_completion_that_fits_the_export_counts(self):
        # Micro-delta M2: the completion broadcast names no export, and the
        # submix's own Start/Finish Recording Submix Output writer broadcasts
        # it too, so any completion on the submix freed the writer. One now
        # counts only if it passes no sound wave (a WAV export's) and finds
        # this export's own WAV complete (it is posted after the file is
        # closed). Isolated fake flights; no engine export.
        import struct
        await _settle_exports()
        path = os.path.join(rendering.renders_dir(),
                            f"{_unique('t_fits')}.wav")
        flight = self._flight(path)
        on_done = rendering._completion_handler(flight)
        try:
            # No file yet (this export's worker still queued): stray.
            on_done(None)
            # The file there but incomplete (still being written): stray.
            with open(path, 'wb') as f:
                f.write(b'RIFF' + struct.pack('<I', 1 << 20) + b'WAVE'
                        + bytes(64))
            on_done(None)
            self._write_wav(path)
            # A SoundWave export's completion, the file complete: stray.
            on_done(object())
            self.assertEqual(flight['stray_acks'], 3)
            self.assertIsNone(flight['ack_tick'])
            self._ticks(rendering.EXPORT_ACK_TICKS + 5)
            self.assertFalse(rendering._export_writer_free())
            # A WAV completion with this export's file complete counts.
            on_done(None)
            first = flight['ack_tick']
            self.assertIsNotNone(first)
            self._ticks(rendering.EXPORT_ACK_TICKS - 1)
            # A later fitting one is counted and never moves the release
            # (micro-delta N4).
            on_done(None)
            self.assertEqual(flight['ack_tick'], first)
            self.assertEqual(flight['late_fits'], 1)
            self.assertFalse(rendering._export_writer_free())
            self._ticks(1)
            self.assertTrue(rendering._export_writer_free())
        finally:
            self._clear_fake()
            if os.path.exists(path):
                os.remove(path)

    async def test_a_stream_of_fitting_completions_cannot_hold_the_writer(
            self):
        # Micro-delta N4: every fitting completion moved the release, so
        # another producer on the submix completing a WAV every tick (the
        # native Finish Recording Submix Output re-exports a retained
        # capture) held the writer, idle since this export's own completion,
        # without end, and every other finish and the cap with it. The first
        # fit now fixes the release; the stream is only counted.
        await _settle_exports()
        done = os.path.join(rendering.renders_dir(),
                            f"{_unique('t_stream')}.wav")
        self._write_wav(done)
        flight = self._flight(done)
        on_done = rendering._completion_handler(flight)
        try:
            on_done(None)  # this export's own
            first = rendering._tick_count
            freed_at = None
            for _ in range(20):
                self._ticks(1)
                if rendering._export_writer_free():  # the post-tick check
                    freed_at = rendering._tick_count
                    break
                on_done(None)  # another producer's, every tick
            self.assertEqual(freed_at, first + rendering.EXPORT_ACK_TICKS)
            self.assertEqual(flight['ack_tick'], first)
            self.assertGreaterEqual(flight['late_fits'], 1)
        finally:
            self._clear_fake()
            os.remove(done)

    async def test_long_hold_is_logged_while_an_acknowledged_export_holds(
            self):
        # Micro-delta N4: the acknowledged branch returned before the hold
        # warning, so the diagnostic went silent once any completion had
        # counted. Modeled: a completion counted on an export held past the
        # timeout, and no post-tick since. The warning and the refusal say
        # what releases it, not the failed-export way out.
        await _settle_exports()
        done = os.path.join(rendering.renders_dir(),
                            f"{_unique('t_ackheld')}.wav")
        self._write_wav(done)
        flight = self._flight(done, age=rendering.EXPORT_TIMEOUT_SECONDS + 60.0)
        warnings = []
        real_warning = unreal.log_warning

        def counting(message):
            if done in str(message):
                warnings.append(str(message))
            real_warning(message)

        unreal.log_warning = counting
        try:
            rendering._completion_handler(flight)(None)
            for _ in range(5):
                self.assertFalse(rendering._export_writer_free())
            self.assertEqual(len(warnings), 1, warnings)
            self.assertIn('still holds it', warnings[0])
            self.assertIn('Its completion has arrived', warnings[0])
            self.assertNotIn('deleting or moving', warnings[0])
            waiting = rendering.RecordingSession(
                name='t_ackwait', directory=rendering.renders_dir(),
                expected_duration=1.0, submix_path='/Fake/Submix')
            with self.assertRaises(RuntimeError) as cm:
                rendering._refuse_while_writer_held(waiting)
            self.assertIn('Its completion has arrived', str(cm.exception))
            self._ticks(rendering.EXPORT_ACK_TICKS)
            self.assertTrue(rendering._export_writer_free())
        finally:
            unreal.log_warning = real_warning
            self._clear_fake()
            os.remove(done)

    async def test_a_stray_completion_leaves_the_no_file_guess_in_charge(self):
        # An export that writes no file gets no counted completion; the
        # no-file release still applies to it, with its full threshold.
        await _settle_exports()
        missing = os.path.join(rendering.renders_dir(),
                               f"{_unique('t_strayno')}.wav")
        floor = rendering.EXPORT_NO_FILE_SECONDS
        flight = self._flight(missing, age=floor - 0.5)
        try:
            rendering._completion_handler(flight)(None)
            self._ticks(rendering.EXPORT_SETTLE_TICKS)
            self.assertFalse(rendering._export_writer_free())
            flight['stopped_at'] -= 1.0
            self.assertTrue(rendering._export_writer_free())
        finally:
            self._clear_fake()

    def _settles_by_the_guess_only(self, flight: dict) -> None:
        """Assert a complete-WAV flight is released by the settle guess.

        Held after enough ticks for a counted completion but before
        EXPORT_SETTLE_SECONDS: only the settle guess waits on the clock.
        """
        import time
        self.assertFalse(rendering._export_writer_free())  # seen complete
        self._ticks(rendering.EXPORT_ACK_TICKS + rendering.EXPORT_SETTLE_TICKS)
        self.assertFalse(rendering._export_writer_free())
        self.assertIs(rendering._export_in_flight, flight)
        time.sleep(rendering.EXPORT_SETTLE_SECONDS + 0.05)
        self.assertTrue(rendering._export_writer_free())

    async def test_a_latched_round3_completion_is_never_counted(self):
        # Micro-delta N2: fix round 3's handler stays bound across an
        # in-place reload and latches 'acked' True on any completion on the
        # submix, whatever it carried and whenever it came. The adapter read
        # that Boolean at a later check and tested the file only then, so a
        # SoundWave completion, or one that came while this WAV was still
        # incomplete, was counted once the file had closed. The Boolean has
        # lost both facts: the flight takes the settle guess, and nothing
        # the old handler latches, then or later, is counted.
        import time
        await _settle_exports()
        done = os.path.join(rendering.renders_dir(),
                            f"{_unique('t_legacy')}.wav")
        self._write_wav(done)  # complete by the time the Boolean is read
        legacy = {
            'path': done, 'stopped_at': time.monotonic(),
            'stop_tick': rendering._tick_count, 'ran_seconds': 0.0,
            'acked': True, 'ack_target': None, 'ack_fn': None,
            'seen_tick': None, 'seen_at': None, 'hold_logged': False}
        self._install_fake(legacy)
        try:
            self.assertFalse(rendering._export_writer_free())
            self.assertTrue(legacy['legacy'])
            self.assertFalse(legacy['subscribed'])
            self.assertIsNone(legacy['ack_tick'])
            legacy['acked'] = True  # the old handler fires again: ignored
            self._settles_by_the_guess_only(legacy)
            # An unsubscribed round-3 flight takes the same guess.
            legacy = {
                'path': done, 'stopped_at': time.monotonic(),
                'stop_tick': rendering._tick_count, 'ran_seconds': 0.0,
                'acked': None, 'ack_target': None, 'ack_fn': None,
                'seen_tick': None, 'seen_at': None, 'hold_logged': False}
            self._install_fake(legacy)
            self._settles_by_the_guess_only(legacy)
            self.assertFalse(legacy['subscribed'])
        finally:
            self._clear_fake()
            os.remove(done)

    async def test_a_94e4e63_flight_writing_its_own_ack_is_not_trusted(self):
        # Micro-delta N2, the intermediate version: a flight issued at
        # 94e4e63 already has 'subscribed' and 'ack_tick', and its retained
        # handler writes ack_tick itself, ignoring the event's argument and
        # the file. The adapter took the current-shaped dict as current and
        # released on it. The dict's shape is no evidence: without this
        # version's stamp the flight is legacy, and an ack_tick its handler
        # writes, before adoption or after, is never counted.
        import struct
        import time
        await _settle_exports()
        path = os.path.join(rendering.renders_dir(),
                            f"{_unique('t_94e')}.wav")
        with open(path, 'wb') as f:  # still being written
            f.write(b'RIFF' + struct.pack('<I', 1 << 20) + b'WAVE'
                    + bytes(64))
        flight = {
            'path': path, 'stopped_at': time.monotonic(),
            'stop_tick': rendering._tick_count, 'ran_seconds': 0.0,
            'subscribed': True, 'ack_tick': None, 'ack_target': None,
            'ack_fn': None, 'seen_tick': None, 'seen_at': None,
            'hold_logged': False}
        self._install_fake(flight)
        try:
            # A foreign completion while this WAV is incomplete, written by
            # the old handler before the reloaded module first looks.
            flight['ack_tick'] = rendering._tick_count
            self.assertFalse(rendering._export_writer_free())
            self.assertTrue(flight['legacy'])
            self.assertIsNone(flight['ack_tick'])
            # And another after adoption: held, file still incomplete.
            flight['ack_tick'] = rendering._tick_count
            self._ticks(rendering.EXPORT_ACK_TICKS + 5)
            self.assertFalse(rendering._export_writer_free())
            self.assertIsNotNone(rendering._export_in_flight)
            # Its file complete, only the settle guess releases it, though
            # the old handler's ack_tick has stood for many ticks.
            self._write_wav(path)
            self._settles_by_the_guess_only(flight)
        finally:
            self._clear_fake()
            if os.path.exists(path):
                os.remove(path)

    async def test_a_round3_flight_adopted_by_round4_takes_the_settle_guess(self):
        # Micro-delta MD3-1: round 4's adapter gave a subscribed round-3
        # flight 'legacy' True beside 'subscribed' True, and no stamp. After a
        # second reload the adapter took that flag for its own work done and
        # returned: the old acknowledgement ignored, the settle guess skipped
        # as subscribed, the no-file guess shut out by the complete WAV, so
        # nothing released it. Every flight without this version's stamp is
        # normalized, an earlier adapter's output included: the round-3 ->
        # round-4 -> current chain releases by the settle guess.
        import time
        await _settle_exports()
        done = os.path.join(rendering.renders_dir(),
                            f"{_unique('t_r4adopted')}.wav")
        self._write_wav(done)
        try:
            # As round 4 left it: 'acked' consumed, its completion either not
            # yet arrived or counted with fewer than EXPORT_ACK_TICKS run.
            for ack_tick in (None, rendering._tick_count):
                flight = {
                    'path': done, 'stopped_at': time.monotonic(),
                    'stop_tick': rendering._tick_count, 'ran_seconds': 0.0,
                    'acked': False, 'ack_target': None, 'ack_fn': None,
                    'seen_tick': None, 'seen_at': None, 'hold_logged': False,
                    'subscribed': True, 'ack_tick': ack_tick, 'legacy': True}
                self._install_fake(flight)
                # The round-3 handler, still bound, latches its completion.
                flight['acked'] = True
                self._settles_by_the_guess_only(flight)
                self.assertFalse(flight['subscribed'])
                self.assertIsNone(flight['ack_tick'])
        finally:
            self._clear_fake()
            os.remove(done)

    async def test_unsubscribed_export_falls_back_to_the_settle_guess(self):
        import time
        await _settle_exports()
        done = os.path.join(rendering.renders_dir(),
                            f"{_unique('t_settle')}.wav")
        self._write_wav(done)
        self._flight(done, subscribed=False)
        try:
            self.assertFalse(rendering._export_writer_free())
            self._ticks(rendering.EXPORT_SETTLE_TICKS)
            time.sleep(rendering.EXPORT_SETTLE_SECONDS + 0.05)
            self.assertTrue(rendering._export_writer_free())
        finally:
            self._clear_fake()
            os.remove(done)

    async def test_no_file_release_waits_its_full_threshold(self):
        # Delta D1: an empty capture makes no writer and no file
        # (AudioMixerBlueprintLibrary.cpp:271-276), but a capture with
        # samples has no file either until its worker opens one. Released
        # only past the threshold, scaled by how long the recording ran,
        # and after settle ticks.
        await _settle_exports()
        missing = os.path.join(rendering.renders_dir(),
                               f"{_unique('t_nofile')}.wav")
        floor = rendering.EXPORT_NO_FILE_SECONDS
        self._flight(missing, age=floor - 0.2)
        self._ticks(rendering.EXPORT_SETTLE_TICKS)
        self.assertFalse(rendering._export_writer_free())
        flight = self._flight(missing, age=floor + 0.2)
        self.assertFalse(rendering._export_writer_free())  # no ticks yet
        self._ticks(rendering.EXPORT_SETTLE_TICKS)
        self.assertTrue(rendering._export_writer_free())
        # Ten minutes of recording: twelve more seconds before the guess.
        self._flight(missing, age=floor + 0.2, ran_seconds=600.0)
        self._ticks(rendering.EXPORT_SETTLE_TICKS)
        self.assertFalse(rendering._export_writer_free())
        self._flight(missing,
                     age=floor + rendering.EXPORT_NO_FILE_PER_SECOND * 600.0
                     + 0.2, ran_seconds=600.0)
        self._ticks(rendering.EXPORT_SETTLE_TICKS)
        self.assertTrue(rendering._export_writer_free())
        self.assertFalse(flight['hold_logged'])

    async def test_long_hold_is_logged_once_and_never_let_through(self):
        # Delta D1: past 300 s the next stop went ahead anyway, warning
        # that it might abandon the held export. Now the hold stays, is
        # logged once, and the refusal names the way out.
        import struct
        await _settle_exports()
        part = os.path.join(rendering.renders_dir(),
                            f"{_unique('t_stuck')}.wav")
        with open(part, 'wb') as f:
            f.write(b'RIFF' + struct.pack('<I', 1 << 20) + b'WAVE'
                    + bytes(64))
        self._flight(part, age=rendering.EXPORT_TIMEOUT_SECONDS + 60.0)
        warnings = []
        real_warning = unreal.log_warning

        def counting(message):
            if part in str(message):
                warnings.append(str(message))
            real_warning(message)

        name = _unique('t_stuckrec')
        self._start_recording(name, 5.0)
        unreal.log_warning = counting
        try:
            for _ in range(20):
                self._ticks(1)
                self.assertFalse(rendering._export_writer_free())
            self.assertEqual(len(warnings), 1, warnings)
            self.assertIn('still holds it', warnings[0])
            with self.assertToolRaisesRuntimeError() as cm:
                TS.finish_output_recording(name, analyze=False)
            self.assertIn('deleting or moving its file', str(cm.exception))
            # The way out names its real wait (micro-delta boundary text).
            self.assertIn('2 s after its stop', str(cm.exception))
            self.assertIn('2 s after its stop', warnings[0])
            self.assertIn('434 s after its stop', rendering._recovery_wait(
                {'ran_seconds': 6 * 3600.0}))
            self.assertIn(name, list(TS.list_output_recordings()))
            # The way out: the failed export's file removed, the no-file
            # release frees the writer.
            os.remove(part)
            self.assertTrue(rendering._export_writer_free())
        finally:
            unreal.log_warning = real_warning
            self._clear_fake()
            if os.path.exists(part):
                os.remove(part)
            await self._discard(name)

    def test_second_recording_on_same_output_refused(self):
        # Review F5: the engine recorder is addressed by submix with no
        # per-session handle, so a second session on the same output
        # would hijack the first's capture.
        name_a = _unique('rec_a')
        self._start_recording(name_a, expected_duration_seconds=5.0)
        try:
            with self.assertToolRaisesRuntimeError() as cm:
                TS.start_output_recording(
                    _unique('rec_b'), expected_duration_seconds=5.0)
            self.assertIn(name_a, str(cm.exception))
            # A source render records the master output too: same guard.
            src = self._source(one_shot=False)
            with self.assertToolRaisesRuntimeError() as cm:
                TS.start_source_render(src)
            self.assertIn(name_a, str(cm.exception))
        finally:
            TS.finish_output_recording(name_a, analyze=False)

    def test_start_recording_clears_stale_wav(self):
        # Review F6 (2026-10-24): a leftover <name>.wav from an earlier
        # render satisfies wait_for_file and would be analyzed as the new
        # recording's result. Start must clear it before the engine
        # recorder starts, so analysis can only ever read this recording's
        # own export.
        import os
        from metasound_toolset import rendering as _rendering
        name = _unique('t_rec')
        stale = os.path.join(_rendering.renders_dir(), f'{name}.wav')
        self._write_wav(stale)
        self.assertGreater(os.path.getsize(stale), 44)
        self._start_recording(name, 5.0)
        try:
            self.assertFalse(os.path.exists(stale))
        finally:
            info = TS.finish_output_recording(name, analyze=False)
            _rendering._pending_wavs.pop(name, None)
            if _rendering.wait_for_file(info.wav_path, 2.0):
                os.remove(info.wav_path)

    def test_start_recording_raises_on_unremovable_stale_wav(self):
        # The stale clear must refuse honestly (before any engine state
        # changes) when the old WAV cannot be removed, never record over
        # a name whose analysis could read the stale file.
        import os
        from metasound_toolset import rendering as _rendering
        name = _unique('t_rec')
        stale = os.path.join(_rendering.renders_dir(), f'{name}.wav')
        self._write_wav(stale)
        handle = open(stale, 'rb')  # Windows: an open handle blocks delete
        try:
            try:
                os.remove(stale)
                removable = True
            except OSError:
                removable = False
            if removable:
                self.skipTest('platform allows deleting open files')
            with self.assertToolRaisesRuntimeError() as cm:
                TS.start_output_recording(name, 5.0)
            self.assertIn('RuntimeError', str(cm.exception))
            self.assertIn('could not be removed', str(cm.exception))
            # Refused before the engine recorder started: no session.
            self.assertNotIn(name, list(TS.list_output_recordings()))
        finally:
            handle.close()
            if os.path.exists(stale):
                os.remove(stale)

    def test_refused_start_preserves_previous_wav(self):
        # Review pass-2 N4: a start refused for want of an editor world (PIE
        # running - the D3 state) must not delete the previous render
        # first. No headless state makes editor_world() refuse, so the
        # refusal is injected by swapping _core.editor_world for a stub.
        import os
        from metasound_toolset import rendering as _rendering
        name = _unique('t_rec')
        previous = os.path.join(_rendering.renders_dir(), f'{name}.wav')
        self._write_wav(previous)

        def _pie_world():
            raise RuntimeError('Play-In-Editor is running (stubbed).')

        real_world = _core.editor_world
        _core.editor_world = _pie_world
        try:
            with self.assertToolRaisesRuntimeError() as cm:
                TS.start_output_recording(name, 5.0)
            self.assertIn('Play-In-Editor', str(cm.exception))
            self.assertTrue(os.path.exists(previous))
            self.assertGreater(os.path.getsize(previous), 44)
            self.assertNotIn(name, list(TS.list_output_recordings()))
        finally:
            _core.editor_world = real_world
            if os.path.exists(previous):
                os.remove(previous)

    async def test_start_recording_refuses_while_export_pending(self):
        # A same-name restart while the previous finish's WAV has not
        # landed yet would let the OLD export satisfy the new analysis.
        import os
        from metasound_toolset import rendering as _rendering
        name = _unique('t_rec')
        self._start_recording(name, 5.0)
        info = TS.finish_output_recording(name, analyze=False)
        try:
            if os.path.exists(info.wav_path):
                # The export landed in-call (audio device flushed fast):
                # the restart path clears it instead - equally safe.
                await _settle_exports()  # one export at a time (delta D2)
                self._start_recording(name, 5.0)
                self.assertFalse(os.path.exists(info.wav_path))
                TS.finish_output_recording(name, analyze=False)
            else:
                with self.assertToolRaisesRuntimeError() as cm:
                    TS.start_output_recording(name, 5.0)
                self.assertIn('still exporting', str(cm.exception))
        finally:
            _rendering._pending_wavs.pop(name, None)
            if _rendering.wait_for_file(info.wav_path, 2.0):
                os.remove(info.wav_path)

    def test_pending_fence_survives_case_drift(self):
        # Review pass-2 N1: the recording name is the WAV file name and
        # Windows paths are case-insensitive, so 'Hit' pending means 'hit'
        # would export to the same file - the fence must catch the alias.
        # Deterministic: if the export landed in-call it is removed, which
        # leaves exactly the pending-without-file state the fence guards.
        import os
        from metasound_toolset import rendering as _rendering
        name = _unique('t_Rec')
        self._start_recording(name, 5.0)
        info = TS.finish_output_recording(name, analyze=False)
        try:
            if os.path.exists(info.wav_path):
                os.remove(info.wav_path)
            with self.assertToolRaisesRuntimeError() as cm:
                TS.start_output_recording(name.swapcase(), 5.0)
            self.assertIn('still exporting', str(cm.exception))
            self.assertNotIn(name.swapcase(),
                             list(TS.list_output_recordings()))
        finally:
            _rendering._pending_wavs.pop(name.casefold(), None)
            if _rendering.wait_for_file(info.wav_path, 2.0):
                os.remove(info.wav_path)

    async def test_analysis_acknowledges_completed_export(self):
        # Review pass-2 N7: a finished render whose WAV was analyzed and
        # then moved/deleted must not stay "still exporting" forever. The
        # landed export is simulated by writing a complete WAV at the
        # finish path, only once the real export has been released on real
        # ticks: a file written under a held export would be the evidence
        # its completion is checked against (micro-delta M4).
        import os
        from metasound_toolset import rendering as _rendering
        name = _unique('t_rec')
        self._start_recording(name, 5.0)
        info = TS.finish_output_recording(name, analyze=False)
        try:
            self.assertIn(name.casefold(), _rendering._pending_wavs)
            self.assertTrue(await _settle_exports())
            self._write_wav(info.wav_path)
            TS.analyze_rendered_wav(info.wav_path)
            self.assertNotIn(name.casefold(), _rendering._pending_wavs)
            os.remove(info.wav_path)  # moved to a library / scratch dropped
            self._start_recording(name, 5.0)  # no "still exporting"
            info = TS.finish_output_recording(name, analyze=False)
        finally:
            _rendering._pending_wavs.pop(name.casefold(), None)
            if _rendering.wait_for_file(info.wav_path, 2.0):
                os.remove(info.wav_path)

    def test_recording_lookup_is_case_insensitive(self):
        # N1's other half: a running session under 'Hit' is the same
        # session (and file) as 'hit' - finishing under the alias finds
        # it, and the listing shows the spelling it was started with.
        import os
        from metasound_toolset import rendering as _rendering
        name = _unique('t_Rec')
        self._start_recording(name, 5.0)
        try:
            self.assertIn(name, list(TS.list_output_recordings()))
        finally:
            info = TS.finish_output_recording(name.upper(), analyze=False)
        self.assertNotIn(name, list(TS.list_output_recordings()))
        self.assertTrue(info.wav_path.endswith(f'{name}.wav'))
        _rendering._pending_wavs.pop(name.casefold(), None)
        if _rendering.wait_for_file(info.wav_path, 2.0):
            os.remove(info.wav_path)

    def test_start_source_render_raises_on_patch(self):
        name = self._patch()
        with self.assertToolRaisesRuntimeError() as cm:
            TS.start_source_render(name)
        self.assertIn('TypeError', str(cm.exception))

    def test_start_source_render_raises_on_bad_duration(self):
        if not _engine_wave_available():
            self.skipTest(f'{ENGINE_WAVE} not available')
        name = _unique('t_rnd')
        TS.create_wave_player(name, ENGINE_WAVE, loop=True)
        self._builders.append(name)
        with self.assertToolRaisesRuntimeError() as cm:
            TS.start_source_render(name, expected_duration_seconds=0.0)
        self.assertIn('ValueError', str(cm.exception))

    def test_source_render_finishes_under_case_alias(self):
        # N1 keys recordings case-insensitively; the render/audition
        # pairing must follow, or finishing under an alias would stop the
        # recording but strand its audition (or refuse half-way).
        import os
        from metasound_toolset import rendering as _rendering
        name = self._source(output_format='Mono', one_shot=False)
        wav_name = _unique('t_Rnd')
        self._start_render(name, wav_name=wav_name)
        info = TS.finish_source_render(wav_name.upper(), analyze=False)
        self.assertTrue(info.wav_path.endswith(f'{wav_name}.wav'))
        self.assertNotIn(wav_name, list(TS.list_output_recordings()))
        with self.assertToolRaisesRuntimeError():
            TS.get_audition_status(f'__render_{wav_name}')
        _rendering._pending_wavs.pop(wav_name.casefold(), None)
        if _rendering.wait_for_file(info.wav_path, 2.0):
            os.remove(info.wav_path)

    def test_finish_source_render_raises_on_unknown(self):
        with self.assertToolRaisesRuntimeError() as cm:
            TS.finish_source_render(_unique('t_nope'))
        self.assertIn('LookupError', str(cm.exception))

    def test_source_render_lifecycle(self):
        # Mechanics only: in-test there are no game-thread ticks between
        # start and finish, so the audition never actually starts AND the
        # async WAV export cannot flush (both need ticks) - signal content
        # and the analyze path are live-editor checks (live-verified
        # 2026-10-23: the pair captures real audio there). analyze=False
        # keeps this deterministic.
        import os
        if not _engine_wave_available():
            self.skipTest(f'{ENGINE_WAVE} not available')
        name = _unique('t_rnd')
        TS.create_wave_player(name, ENGINE_WAVE, loop=True, gain_db=0.0)
        self._builders.append(name)
        status = self._start_render(name)
        self.assertEqual(status.recording_name, name)
        self.assertIn(name, list(TS.list_output_recordings()))
        info = TS.finish_source_render(name, analyze=False)
        try:
            self.assertTrue(info.wav_path.endswith(f'{name}.wav'))
            self.assertNotIn(name, list(TS.list_output_recordings()))
            # The paired audition session is cleaned up either way.
            with self.assertToolRaisesRuntimeError():
                TS.get_audition_status(f'__render_{name}')
            # A second finish is an error: the render is done.
            with self.assertToolRaisesRuntimeError() as cm:
                TS.finish_source_render(name, analyze=False)
            self.assertIn('LookupError', str(cm.exception))
        finally:
            from metasound_toolset import rendering as _rendering
            if _rendering.wait_for_file(info.wav_path, 5.0):
                os.remove(info.wav_path)

    def test_finish_source_render_raises_on_negative_fade(self):
        if not _engine_wave_available():
            self.skipTest(f'{ENGINE_WAVE} not available')
        name = _unique('t_rnd')
        TS.create_wave_player(name, ENGINE_WAVE, loop=True)
        self._builders.append(name)
        self._start_render(name)
        try:
            with self.assertToolRaisesRuntimeError() as cm:
                TS.finish_source_render(name, fade_out_seconds=-1.0)
            self.assertIn('ValueError', str(cm.exception))
        finally:
            TS.finish_source_render(name, analyze=False)

    def test_validate_recording_name_rejects_junk(self):
        from metasound_toolset import rendering as _rendering
        for bad in ('', 'has space', 'dot.ted', 'slash/y'):
            with self.assertRaises(ValueError):
                _rendering.validate_recording_name(bad)
        _rendering.validate_recording_name('Render_01')


class TestSoundRouting(MetaSoundToolsetTestCase):
    """Attenuation / sound class / submix routing tools."""

    MOUNT = '/MSTRoutingAuto/'

    def setUp(self):
        super().setUp()
        unreal.AutomationUtilsBlueprintLibrary.add_automation_mount_point(
            self.MOUNT)
        self.folder = f'{self.MOUNT}{uuid.uuid4().hex[:8]}'
        unreal.get_editor_subsystem(
            unreal.EditorAssetSubsystem).make_directory(self.folder)

    def tearDown(self):
        super().tearDown()
        eas = unreal.get_editor_subsystem(unreal.EditorAssetSubsystem)
        if eas.does_directory_exist(self.folder):
            eas.delete_directory(self.folder)
        unreal.AutomationUtilsBlueprintLibrary.remove_automation_mount_point(
            self.MOUNT)

    def _attenuation(self, **kwargs) -> str:
        info = TS.create_attenuation_asset(
            _unique('ATT'), self.folder, **kwargs)
        return info.asset_path

    def _built_source(self) -> str:
        name = self._source(output_format='Mono', one_shot=False)
        return TS.build_to_asset(name, _unique('MS_Routed'), self.folder)

    def test_create_attenuation_reads_back(self):
        info = TS.create_attenuation_asset(
            'ATT_Test', self.folder, inner_radius=250.0,
            falloff_distance=2000.0, spatialize=True,
            attenuate_with_lpf=True)
        self.assertAlmostEqual(info.inner_radius, 250.0, places=2)
        self.assertAlmostEqual(info.falloff_distance, 2000.0, places=2)
        self.assertTrue(info.spatialize)
        self.assertTrue(info.attenuate)
        self.assertTrue(info.attenuate_with_lpf)
        self.assertFalse(info.enable_occlusion)

    def test_create_attenuation_refuses_existing(self):
        TS.create_attenuation_asset('ATT_Dup', self.folder)
        with self.assertToolRaisesRuntimeError() as cm:
            TS.create_attenuation_asset('ATT_Dup', self.folder)
        self.assertIn('FileExistsError', str(cm.exception))

    def test_create_attenuation_raises_on_bad_values(self):
        with self.assertToolRaisesRuntimeError() as cm:
            TS.create_attenuation_asset('ATT_Bad', self.folder,
                                        inner_radius=-1.0)
        self.assertIn('ValueError', str(cm.exception))
        with self.assertToolRaisesRuntimeError() as cm:
            TS.create_attenuation_asset('ATT_Bad', self.folder,
                                        falloff_distance=0.0)
        self.assertIn('ValueError', str(cm.exception))

    def test_describe_attenuation_raises_on_wrong_type(self):
        if not _engine_wave_available():
            self.skipTest(f'{ENGINE_WAVE} not available')
        with self.assertToolRaisesRuntimeError() as cm:
            TS.describe_attenuation(ENGINE_WAVE)
        self.assertIn('TypeError', str(cm.exception))

    def test_set_and_clear_sound_routing(self):
        att = self._attenuation()
        sound = self._built_source()
        info = TS.set_sound_routing(sound, attenuation_path=att)
        self.assertEqual(info.attenuation_path, att)
        self.assertEqual(info.sound_class_path, '')
        # Read-back parity through the getter.
        again = TS.get_sound_routing(sound)
        self.assertEqual(again.attenuation_path, att)
        # Clear with ''.
        cleared = TS.set_sound_routing(sound, attenuation_path='')
        self.assertEqual(cleared.attenuation_path, '')

    def test_set_sound_routing_raises_without_slots(self):
        sound = self._built_source()
        with self.assertToolRaisesRuntimeError() as cm:
            TS.set_sound_routing(sound)
        self.assertIn('ValueError', str(cm.exception))

    def test_set_sound_routing_raises_on_wrong_target(self):
        att = self._attenuation()
        with self.assertToolRaisesRuntimeError() as cm:
            TS.set_sound_routing(att, attenuation_path=att)
        self.assertIn('TypeError', str(cm.exception))

    def test_get_sound_routing_raises_on_missing(self):
        with self.assertToolRaisesRuntimeError() as cm:
            TS.get_sound_routing('/Game/__nope__/NoSound')
        self.assertIn('FileNotFoundError', str(cm.exception))

    def test_list_audio_routing_assets_finds_created(self):
        att = self._attenuation()
        listed = TS.list_audio_routing_assets('Attenuation', self.folder)
        self.assertIn(att, list(listed))

    def test_list_audio_routing_assets_raises_on_bad_kind(self):
        with self.assertToolRaisesRuntimeError() as cm:
            TS.list_audio_routing_assets('Reverb')
        self.assertIn('ValueError', str(cm.exception))

    def test_build_to_asset_copies_template_wave_routing(self):
        if not _engine_wave_available():
            self.skipTest(f'{ENGINE_WAVE} not available')
        att = self._attenuation()
        # A project-local wave to carry the template settings (never touch
        # engine content): duplicate the engine wave, route it, template it.
        wave_path = f'{self.folder}/SW_Template'
        duplicated = unreal.EditorAssetLibrary.duplicate_asset(
            ENGINE_WAVE, wave_path)
        self.assertIsNotNone(duplicated)
        TS.set_sound_routing(wave_path, attenuation_path=att)

        name = self._source(output_format='Mono', one_shot=False)
        saved = TS.build_to_asset(
            name, _unique('MS_Templated'), self.folder,
            template_sound_wave_path=wave_path)
        info = TS.get_sound_routing(saved)
        self.assertEqual(info.attenuation_path, att)

    def test_build_to_asset_raises_on_bad_template_wave(self):
        name = self._patch()
        with self.assertToolRaisesRuntimeError() as cm:
            TS.build_to_asset(name, 'MSP_T', self.folder,
                              template_sound_wave_path='/Game/__nope__/W')
        self.assertIn('FileNotFoundError', str(cm.exception))


class TestCoreGuards(MetaSoundToolsetTestCase):
    """Direct coverage of _core guards that tools route through."""

    def test_validate_content_path_rejects_junk(self):
        for bad in ('', 'Game/A', '/Game/with space', '/Game/dot.dot'):
            with self.assertRaises(ValueError):
                _core.validate_content_path(bad)
        _core.validate_content_path('/Game/Audio/Designed')

    def test_validate_content_path_rejects_epic_invalid_chars(self):
        # Review F3: the old blacklist missed '#', '&', '!', '~', '@' and
        # control characters from Epic's INVALID_LONGPACKAGE_CHARACTERS.
        for bad in ('/Game/MS_Hit#Take1', '/Game/Audio&FX', '/Game/A!B',
                    '/Game/~tmp', '/Game/at@home', '/Game/ctl\x01'):
            with self.assertRaises(ValueError):
                _core.validate_content_path(bad)

    def test_validate_content_path_rejects_double_slashes(self):
        # The engine collapses these after every check here has passed, so
        # '//Engine/X' would land in /Engine/X.
        for bad in ('//Game/Audio', '//Engine/X', '/Game//Audio',
                    '/Game/Audio//'):
            with self.assertRaises(ValueError):
                _core.validate_content_path(bad)

    def test_validate_content_path_rejects_folding_lookalikes(self):
        # U+017F casefolds to 's', but FName compares ASCII case only.
        with self.assertRaises(ValueError):
            _core.validate_content_path('/Meta\u017found/X')

    def test_engine_guard_follows_case_and_fails_closed(self):
        with self.assertRaises(PermissionError):
            _core.ensure_not_engine_content('/engine/__nope__/X')
        with self.assertRaises(PermissionError):
            _core.ensure_not_engine_content('/NoSuchMountXyz/X')
        _core.ensure_not_engine_content('/Game/__nope__/X')

    def test_validate_content_path_rejects_unmounted_root(self):
        with self.assertRaises(ValueError) as cm:
            _core.validate_content_path('/NoSuchMountXyz/Audio')
        self.assertIn('mount', str(cm.exception))
        # Mounted roots pass whatever their spelling's case.
        _core.validate_content_path('/game/Audio')

    def test_validate_asset_name_rejects_junk(self):
        for bad in ('', 'has space', 'slash/y', 'dot.ted'):
            with self.assertRaises(ValueError):
                _core.validate_asset_name(bad)
        _core.validate_asset_name('MS_Fine_01')

    def test_validate_asset_name_rejects_epic_invalid_chars(self):
        for bad in ('MS_Hit#Take1', 'MS(1)', 'MS[2]', 'MS=3', 'MS;4',
                    'MS`5', 'MS%6', 'MS$7', 'MS^8', 'MS&9', 'ctl\x01'):
            with self.assertRaises(ValueError):
                _core.validate_asset_name(bad)

    def test_validators_reject_reserved_file_names(self):
        for bad in ('CON', 'nul', 'None', 'COM1', 'lpt9'):
            with self.assertRaises(ValueError):
                _core.validate_asset_name(bad)
            with self.assertRaises(ValueError):
                _core.validate_content_path(f'/Game/{bad}/Audio')
        # Whole-segment matches only: a name merely starting with one is fine.
        _core.validate_asset_name('CONTROL_Hit')
        _core.validate_content_path('/Game/NoneOfTheAbove')

    def test_overlong_names_raise_cleanly(self):
        # The engine's FName constructor is fatal at 1024 characters; the
        # Python binding must stop these first. A crash here takes the
        # whole test run down rather than failing one assertion.
        long_name = 'x' * 2000
        with self.assertToolRaisesRuntimeError():
            TS.get_graph_info(long_name)
        with self.assertToolRaisesRuntimeError():
            TS.create_metasound_source(long_name)
        with self.assertToolRaisesRuntimeError():
            TS.resolve_node_class('UE', long_name)
        with self.assertToolRaisesRuntimeError():
            TS.describe_metasound(f'/Game/{long_name}')

    def test_package_under_path_respects_boundaries(self):
        self.assertTrue(
            _core.package_under_path('/Game/Audio', '/Game/Audio'))
        self.assertTrue(
            _core.package_under_path('/Game/Audio/Sub/X', '/Game/Audio'))
        self.assertTrue(
            _core.package_under_path('/Game/Audio/X', '/Game/Audio/'))
        self.assertFalse(
            _core.package_under_path('/Game/AudioOld/X', '/Game/Audio'))
        self.assertFalse(
            _core.package_under_path('/Game/AudioBackup', '/Game/Audio'))
        self.assertTrue(_core.package_under_path('/Game/X', '/'))

    def test_list_inputs_are_bounded(self):
        # S21: every list item loads an asset or becomes a literal element
        # on the game thread, and nothing bounded the count.
        from metasound_toolset import templates
        too_many = _core.MAX_LIST_ITEMS + 1
        with self.assertRaises(ValueError) as cm:
            _core.make_literal(float_array=[0.0] * too_many)
        self.assertIn(str(_core.MAX_LIST_ITEMS), str(cm.exception))
        with self.assertRaises(ValueError):
            _core.make_literal(asset_paths=[ENGINE_WAVE] * too_many)
        with self.assertRaises(ValueError):
            templates._validate_wave_assets([ENGINE_WAVE] * too_many)
        _core.make_literal(float_array=[0.0] * _core.MAX_LIST_ITEMS)

    def test_asset_class_is_checked_from_registry_data(self):
        # S21: the class check ran after the load, so describe_metasound on
        # a level loaded the level before refusing it.
        if not _engine_wave_available():
            self.skipTest(f'{ENGINE_WAVE} not available')
        with self.assertRaises(TypeError) as cm:
            _core.ensure_asset_class(
                ENGINE_WAVE, _core.METASOUND_CLASSES, 'MetaSound')
        self.assertIn('SoundWave, not a MetaSound', str(cm.exception))
        with self.assertRaises(FileNotFoundError):
            _core.ensure_asset_class(
                '/Game/__nope__/Missing', _core.METASOUND_CLASSES, 'x')

    def test_make_literal_requires_exactly_one(self):
        with self.assertRaises(ValueError):
            _core.make_literal()
        with self.assertRaises(ValueError):
            _core.make_literal(float_value=1.0, int_value=2)
        literal = _core.make_literal(float_value=0.5)
        self.assertIsInstance(literal, unreal.MetasoundFrontendLiteral)
        roundtrip = _core.literal_to_dict(literal)
        self.assertAlmostEqual(roundtrip['float_value'], 0.5, places=4)

    def test_literal_to_dict_reads_bool_both_ways(self):
        # Bools round-trip via the engine's string form: 5.8's
        # GetBoolValueFromLiteral binding has no value channel.
        for flag in (True, False):
            roundtrip = _core.literal_to_dict(
                _core.make_literal(bool_value=flag))
            self.assertEqual(roundtrip['literal_type'], 'BOOLEAN')
            self.assertEqual(roundtrip['bool_value'], flag)
