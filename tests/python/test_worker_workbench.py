from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from . import _bootstrap  # noqa: F401
import worker_workflows


class WorkbenchNativeTests(unittest.TestCase):
    def setUp(self):
        import numpy as np
        from pymss.audio_io import save_audio, load_audio
        self.np, self.save_audio, self.load_audio = np, save_audio, load_audio
        self.temp = self.enterContext(tempfile.TemporaryDirectory())
        self.root = Path(self.temp)
        self.first, self.second = self.root / 'first.wav', self.root / 'second.wav'
        save_audio(str(self.first), np.full((80, 2), 0.6, dtype=np.float32), 44100, 'wav', {'wav_bit_depth': 'FLOAT'})
        save_audio(str(self.second), np.full((80, 2), 0.2, dtype=np.float32), 44100, 'wav', {'wav_bit_depth': 'FLOAT'})

    def run_graph(self, definition, targets):
        task_id = 'workbench-native'
        self.addCleanup(worker_workflows._cleanup_workflow_file, task_id)
        with patch.object(worker_workflows, 'emit'):
            return worker_workflows._run_pymss(
                {'workflow': definition, 'workbench': True, 'workbenchOutputs': targets, 'outputFormat': 'wav'},
                task_id, str(self.first), {'lead': str(self.first), 'reference': str(self.second)}, str(self.root / 'default'), 'flat',
            )

    def test_distinct_input_slots_and_two_output_directories_keep_their_audio(self):
        definition = {
            'nodes': [
                {'id': 1, 'type': 'pymss_load_audio', 'widgets_values': ['', 'lead'], 'inputs': [], 'outputs': [{'name': 'audio', 'type': 'AUDIO'}]},
                {'id': 2, 'type': 'pymss_load_audio', 'widgets_values': ['', 'reference'], 'inputs': [], 'outputs': [{'name': 'audio', 'type': 'AUDIO'}]},
                {'id': 3, 'type': 'pymss_save_audio', 'widgets_values': ['wav', 'Default', '0', 'FLOAT'], 'inputs': [{'name': 'audio', 'type': 'AUDIO', 'link': 1}]},
                {'id': 4, 'type': 'pymss_save_audio', 'widgets_values': ['wav', 'Default', '0', 'FLOAT'], 'inputs': [{'name': 'audio', 'type': 'AUDIO', 'link': 2}]},
            ], 'links': [[1, 1, 0, 3, 0, 'AUDIO'], [2, 2, 0, 4, 0, 'AUDIO']],
            'extra': {'studioEnsemble': {'outputStem': 'Vocals'}},
        }
        destinations = {'3': str(self.root / 'a'), '4': str(self.root / 'b')}
        result = self.run_graph(definition, destinations)
        self.assertEqual([item['nodeId'] for item in result['outputs']], ['3', '4'])
        self.assertEqual(set(result['outputDirectories']), {str(Path(path).resolve()) for path in destinations.values()})
        self.assertIn(result['outputDir'], result['outputDirectories'])
        for item, expected in zip(result['outputs'], (0.6, 0.2)):
            audio, rate = self.load_audio(item['path'], sr=None, mono=False)
            self.np.testing.assert_allclose(audio, expected, atol=1e-6)
            self.assertEqual(rate, 44100)
            self.assertEqual(Path(item['path']).parent, Path(destinations[item['nodeId']]).resolve())
        again = self.run_graph(definition, destinations)
        self.assertNotEqual(result['files'], again['files'])
        self.assertTrue(all(Path(path).is_file() for path in result['files'] + again['files']))

    def test_simple_operation_publishes_to_its_bound_directory(self):
        definition = {'version': 1, 'studio': {'editor': 'simple'}, 'steps': [], 'ensembles': [{
            'id': 'phase', 'algorithm': 'invert', 'output_stem': 'Inverted', 'save': 'Default',
            'inputs': [{'source': 'input', 'weight': 1}],
        }]}
        result = self.run_graph(definition, {'studio:ensemble-save:phase': str(self.root / 'phase')})
        self.assertEqual(Path(result['files'][0]).parent, (self.root / 'phase').resolve())
        audio, _ = self.load_audio(result['files'][0], sr=None, mono=False)
        self.np.testing.assert_allclose(audio, -0.6, atol=1e-6)

    def test_structured_node_progress_reaches_the_frontend(self):
        with patch.object(worker_workflows, 'emit') as emit:
            worker_workflows._emit_progress('task')({'node_id': '4', 'node_type': 'AudioMerge', 'node_fraction': .5, 'overall_fraction': .25})
        payload = emit.call_args.args[1]
        self.assertEqual((payload['nodeId'], payload['nodeProgress']), ('4', .5))
        self.assertEqual(payload['overallFraction'], .25)

    def test_plugin_files_are_published_and_string_paths_follow_the_final_destination(self):
        from pymss.graph import AUDIO, STRING, DAGOutputRecord, NodeResult, NodeSignature, PortSpec, StringArtifact
        from pymss.plugins import get_registry

        registry = get_registry()
        node_type = 'StudioTestMidiOutput'

        def execute(ctx, inputs):
            target = ctx.output_dir / 'voice.mid'
            target.write_bytes(b'MThd')
            record = DAGOutputRecord(str(target), node_id=2, node_type=node_type, format='mid')
            return NodeResult(outputs={0: StringArtifact(str(target))}, saved_paths=[str(target)], saved_records=[record])

        registry.register_node(node_type, execute, source='plugin:studio.test', signature=lambda node: NodeSignature(
            inputs=[PortSpec('audio', AUDIO)], output_names=['path'], output_types=[STRING], is_output_node=True,
        ))
        self.addCleanup(registry.nodes.pop, node_type, None)
        definition = {'nodes': [
            {'id': 1, 'type': 'pymss_load_audio', 'widgets_values': ['', 'lead'], 'inputs': []},
            {'id': 2, 'type': node_type, 'inputs': [{'name': 'audio', 'type': 'AUDIO', 'link': 1}]},
        ], 'links': [[1, 1, 0, 2, 0, 'AUDIO']]}
        for targets, directory in [({}, self.root / 'default'), ({'2': str(self.root / 'chosen')}, self.root / 'chosen')]:
            with self.subTest(targets=targets), patch('worker_nodes.collect_plugin_nodes', return_value=[]):
                result = self.run_graph(definition, targets)
            output = Path(result['files'][0])
            self.assertEqual(output.parent, directory.resolve())
            self.assertEqual(output.read_bytes(), b'MThd')
            midi_value = next(value for value in result['nodeValues'] if str(value['nodeId']) == '2')
            self.assertEqual(midi_value['value'], str(output))
            self.assertEqual(result['outputs'][0]['nodeId'], '2')
            self.assertFalse(list((self.root / 'default').glob('.pymss-workflow-*')))

    def test_unavailable_plugin_is_reported_before_graph_execution(self):
        declarations = [{'type': 'UnavailableAsr', 'pluginId': 'pymss.asr', 'registered': False,
                         'unavailableReason': 'Missing plugin dependency: funasr'}]
        definition = {'nodes': [{'id': 1, 'type': 'UnavailableAsr', 'inputs': []}], 'links': []}
        with patch('worker_nodes.collect_plugin_nodes', return_value=declarations), \
             self.assertRaisesRegex(RuntimeError, 'pymss.asr.*funasr'):
            self.run_graph(definition, {})

    def test_builtin_save_nodes_keep_dynamic_prefixes_and_separate_same_named_outputs(self):
        definition = {
            'nodes': [
                {'id': 1, 'type': 'pymss_load_audio', 'widgets_values': ['', 'lead'], 'inputs': [], 'outputs': [{'name': 'audio', 'type': 'AUDIO'}]},
                {'id': 2, 'type': 'pymss_load_audio', 'widgets_values': ['', 'reference'], 'inputs': [], 'outputs': [{'name': 'audio', 'type': 'AUDIO'}]},
                {'id': 5, 'type': 'StringConstant', 'widgets_values': ['shared/name'], 'inputs': [], 'outputs': [{'name': 'STRING', 'type': 'STRING'}]},
                {'id': 3, 'type': 'SaveAudio', 'widgets_values': ['ignored'], 'inputs': [{'name': 'audio', 'type': 'AUDIO', 'link': 1}, {'name': 'filename_prefix', 'type': 'STRING', 'link': 3}]},
                {'id': 4, 'type': 'SaveAudio', 'widgets_values': ['ignored'], 'inputs': [{'name': 'audio', 'type': 'AUDIO', 'link': 2}, {'name': 'filename_prefix', 'type': 'STRING', 'link': 4}]},
            ], 'links': [[1, 1, 0, 3, 0, 'AUDIO'], [2, 2, 0, 4, 0, 'AUDIO'], [3, 5, 0, 3, 1, 'STRING'], [4, 5, 0, 4, 1, 'STRING']],
        }
        directory = str(self.root / 'combined')
        result = self.run_graph(definition, {'3': directory, '4': directory})
        self.assertEqual(len(set(result['files'])), 2)
        self.assertEqual({Path(path).name for path in result['files']}, {'name.flac', 'name_2.flac'})
        expected_directory = (Path(directory) / 'shared').resolve()
        self.assertTrue(all(Path(path).parent == expected_directory for path in result['files']))
        self.assertEqual(result['outputDirectories'], [str(expected_directory)])
        self.assertEqual(result['outputDir'], str(expected_directory))
        for item, expected in zip(result['outputs'], (0.6, 0.2)):
            audio, _ = self.load_audio(item['path'], sr=None, mono=False)
            self.np.testing.assert_allclose(audio, expected, atol=1e-6)

    def test_legacy_save_layout_keeps_named_parameter_links_when_folder_is_inserted(self):
        import pymss.graph as graph
        definition = {'nodes': [
            {'id': 1, 'type': 'StringConstant', 'widgets_values': ['44100'], 'outputs': [{'name': 'STRING', 'type': 'STRING'}]},
            {'id': 2, 'type': 'pymss_save_audio', 'widgets_values': ['wav', '44100', 'FLOAT', 'PCM_24', '320k'], 'inputs': [
                {'name': 'audio', 'link': None}, {'name': 'filename', 'link': None}, {'name': 'output_format', 'link': None}, {'name': 'sample_rate', 'link': 1},
            ]},
        ], 'links': [[1, 1, 0, 2, 3, 'STRING']]}
        dag = graph.load_comfy_graph(definition)
        worker_workflows._prepare_workbench_outputs(dag, {'2': str(self.root / 'output')})
        save = next(node for node in dag.nodes if node.id == 2)
        self.assertIsNone(save.inputs[3])
        self.assertEqual(save.inputs[4].source_node_id, 1)
        self.assertEqual(save.inputs[4].target_slot, 4)

    def test_publication_failure_reports_completed_files_without_removing_them(self):
        from types import SimpleNamespace
        source_dir = self.root / 'private'
        source_dir.mkdir()
        first, second = source_dir / 'a.wav', source_dir / 'b.wav'
        first.write_bytes(b'first')
        second.write_bytes(b'second')
        blocked = self.root / 'blocked'
        blocked.write_bytes(b'keep')
        records = [SimpleNamespace(node_id='a', stem='First'), SimpleNamespace(node_id='b', stem='Second')]
        with self.assertRaises(worker_workflows.WorkbenchPublishError) as caught:
            worker_workflows._publish_workbench_outputs(
                [str(first), str(second)], records, [], {'a': str(self.root / 'published'), 'b': str(blocked)}, source_dir,
            )
        self.assertEqual(caught.exception.node_id, 'b')
        self.assertEqual(len(caught.exception.outputs), 1)
        self.assertEqual(Path(caught.exception.outputs[0]['path']).read_bytes(), b'first')
        self.assertEqual(blocked.read_bytes(), b'keep')
