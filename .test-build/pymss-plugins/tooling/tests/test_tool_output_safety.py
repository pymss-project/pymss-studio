from __future__ import annotations

import os
import ctypes
import json
import errno
import types
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from pymss.plugins import kit as common
from plugins.merge.backend import merge
from plugins.slicer.backend import slicer
worker_audio = common


class AudioOutputSafetyTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.source = self.root / 'source.wav'
        self.source.write_bytes(b'source')
        self.output = self.root / 'output'
        self.output.mkdir()

    def test_managed_partials_identify_the_native_task_and_worker(self):
        token = 'audio-aBC123def456XYZ789'
        record = self.root / 'scope.json'
        record.write_text(json.dumps({'token': token, 'pid': os.getpid(), 'directory': str(self.output)}))
        observed = []
        def render(temporary):
            observed.append(temporary.name)
            temporary.write_bytes(b'complete')
        with mock.patch.dict(os.environ, {'PYMSS_STUDIO_AUDIO_TEMP_TOKEN': token, 'PYMSS_STUDIO_AUDIO_TEMP_STATE': str(record)}):
            common.render_output(self.output / 'final.wav', render)
        self.assertTrue(observed[0].startswith(f'.pymss-audio-{token}-{os.getpid()}-'))

    def test_mismatched_native_receipt_prevents_untracked_output(self):
        token = 'audio-aBC123def456XYZ789'
        record = self.root / 'scope.json'
        record.write_text(json.dumps({'token': token, 'pid': 0, 'directory': str(self.output)}))
        with mock.patch.dict(os.environ, {'PYMSS_STUDIO_AUDIO_TEMP_TOKEN': token, 'PYMSS_STUDIO_AUDIO_TEMP_STATE': str(record)}), \
             mock.patch.object(common.time, 'sleep'):
            with self.assertRaisesRegex(RuntimeError, 'state is unavailable'):
                common.render_output(self.output / 'final.wav', lambda _: self.fail('must not render'))
        self.assertEqual(list(self.output.iterdir()), [])

    def test_reexecuted_worker_keeps_the_native_launcher_identity(self):
        token = 'audio-aBC123def456XYZ789'
        record = self.root / 'scope.json'
        worker = '''import json, sys
from pathlib import Path
from pymss.plugins.kit import render_output as _render_output
sys.stdin.readline()
root = Path(sys.argv[1])
observed = []
def render(path):
    observed.append(path.name)
    path.write_bytes(b'complete')
result = _render_output(root / 'completed.wav', render)
print(json.dumps({'partial': observed[0], 'result': str(result)}))
'''
        env = {**os.environ, 'PYTHONPATH': os.pathsep.join(filter(None, [str(Path(__file__).resolve().parents[2]), os.environ.get('PYTHONPATH')])),
               'PYMSS_STUDIO_AUDIO_TEMP_TOKEN': token, 'PYMSS_STUDIO_AUDIO_TEMP_STATE': str(record)}
        with subprocess.Popen([sys.executable, '-X', 'utf8', '-c', worker, str(self.output)], env=env,
                              stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                              text=True, encoding='utf-8') as process:
            try:
                # Match the native host: record the launched child's PID. On Windows
                # a venv launcher may then start the actual interpreter beneath it.
                record.write_text(json.dumps({'token': token, 'pid': process.pid, 'directory': str(self.output)}))
                stdout, stderr = process.communicate('\n', timeout=20)
            except BaseException:
                process.kill()
                process.communicate()
                raise
        self.assertEqual(process.returncode, 0, stderr)
        observed = json.loads(stdout)
        self.assertTrue(observed['partial'].startswith(f'.pymss-audio-{token}-{process.pid}-'))
        self.assertEqual(Path(observed['result']).read_bytes(), b'complete')

    def test_an_unrelated_process_identity_cannot_claim_the_native_receipt(self):
        token = 'audio-aBC123def456XYZ789'
        record = self.root / 'scope.json'
        unrelated = max(os.getpid(), os.getppid()) + 1000000
        record.write_text(json.dumps({'token': token, 'pid': unrelated, 'directory': str(self.output)}))
        with mock.patch.dict(os.environ, {'PYMSS_STUDIO_AUDIO_TEMP_TOKEN': token, 'PYMSS_STUDIO_AUDIO_TEMP_STATE': str(record)}), \
             mock.patch.object(common.time, 'sleep'):
            with self.assertRaisesRegex(RuntimeError, 'state is unavailable'):
                common.render_output(self.output / 'final.wav', lambda _: self.fail('must not render'))
        self.assertEqual(list(self.output.iterdir()), [])

    def test_success_publishes_only_complete_audio_and_never_overwrites_existing_files(self):
        target = self.output / 'mix.wav'
        target.write_bytes(b'original')

        def render(temporary):
            self.assertNotEqual(temporary, target)
            self.assertEqual(temporary.suffix, '.wav')
            self.assertEqual(target.read_bytes(), b'original')
            temporary.write_bytes(b'complete')

        published = common.render_output(target, render)
        self.assertEqual(published.name, 'mix_2.wav')
        self.assertEqual(published.read_bytes(), b'complete')
        self.assertEqual(target.read_bytes(), b'original')
        self.assertEqual(sorted(p.name for p in self.output.iterdir()), ['mix.wav', 'mix_2.wav'])


    def test_unsupported_hard_links_never_create_final_placeholders(self):
        target = self.output / 'mix.wav'
        with mock.patch.object(worker_audio.os, 'link', side_effect=OSError('unsupported')):
            published = common.render_output(target, lambda temporary: temporary.write_bytes(b'complete'))
            self.assertEqual(published.read_bytes(), b'complete')

    def test_posix_publish_requests_exclusive_rename_and_preserves_collision_errors(self):
        temporary, target = self.output / 'temporary.wav', self.output / 'mix.wav'
        for platform, name, flag in [('darwin', 'renamex_np', 4), ('linux', 'renameat2', 1)]:
            with self.subTest(platform=platform):
                rename = mock.Mock(return_value=0)
                library = types.SimpleNamespace(**{name: rename})
                with mock.patch.object(worker_audio.os, 'name', 'posix'), \
                     mock.patch.object(worker_audio.sys, 'platform', platform), \
                     mock.patch.object(ctypes, 'CDLL', return_value=library), \
                     mock.patch.object(ctypes, 'get_errno', return_value=errno.EEXIST):
                    worker_audio._rename_export_path(temporary, target)
                    self.assertEqual(rename.call_args.args[-1], flag)
                    self.assertIn(os.fsencode(temporary), rename.call_args.args)
                    self.assertIn(os.fsencode(target), rename.call_args.args)
                    rename.return_value = -1
                    with self.assertRaises(FileExistsError):
                        worker_audio._rename_export_path(temporary, target)

    def test_missing_exclusive_rename_cannot_publish_a_placeholder(self):
        temporary, target = self.output / 'temporary.wav', self.output / 'mix.wav'
        with mock.patch.object(worker_audio.os, 'name', 'posix'), \
             mock.patch.object(worker_audio.sys, 'platform', 'linux'), \
             mock.patch.object(ctypes, 'CDLL', return_value=types.SimpleNamespace()):
            with self.assertRaises(OSError):
                worker_audio._rename_export_path(temporary, target)
        self.assertFalse(target.exists())

    def test_failed_windows_rename_does_not_leave_a_final_placeholder(self):
        if os.name != 'nt':
            self.skipTest('Windows atomic rename fallback')
        target = self.output / 'mix.wav'
        with mock.patch.object(worker_audio.os, 'link', side_effect=OSError('unsupported')), \
             mock.patch.object(worker_audio.os, 'rename', side_effect=OSError('rename failed')):
            with self.assertRaises(OSError):
                common.render_output(target, lambda temporary: temporary.write_bytes(b'complete'))
        self.assertEqual(list(self.output.iterdir()), [])

    def test_slicer_failure_does_not_publish_a_partial_segment(self):
        def interrupted(source, target, *args):
            target.write_bytes(b'half')
            raise subprocess.CalledProcessError(143, 'ffmpeg')

        with mock.patch.object(slicer, '_slice_intervals', return_value=[(0, 1)]), \
             mock.patch.object(slicer, '_render_segment', side_effect=interrupted), mock.patch.object(slicer, 'emit'):
            with self.assertRaises(subprocess.CalledProcessError):
                slicer._slice_audio({'inputs': [str(self.source)], 'outputDir': str(self.output)})
        self.assertEqual(list(self.output.iterdir()), [])

    def test_merge_failure_does_not_publish_partial_concatenation(self):
        def render(arguments):
            Path(arguments[-1]).write_bytes(b'half')
            if 'concat' in arguments:
                raise subprocess.CalledProcessError(143, 'ffmpeg')

        with mock.patch.object(merge, '_run_ffmpeg', side_effect=render), mock.patch.object(merge, 'emit'):
            with self.assertRaises(RuntimeError):
                merge._merge_audio({'inputDir': str(self.root), 'outputDir': str(self.output)})
        self.assertEqual(list(self.output.iterdir()), [])

    def test_merge_rejects_the_input_directory_and_its_alias_before_running_ffmpeg(self):
        with mock.patch.object(merge, '_run_ffmpeg') as ffmpeg:
            for value in [str(self.root), str(self.root) + os.sep + '.']:
                with self.subTest(path=value), self.assertRaises(common.ToolError) as caught:
                    merge._merge_audio({'inputDir': str(self.root), 'outputDir': value})
                self.assertEqual(caught.exception.code, 'MERGE_SAME_DIRECTORY')
            ffmpeg.assert_not_called()
        self.assertEqual(list(self.root.glob('merged_audio*')), [])

    def test_merge_ignores_abandoned_tool_partials(self):
        (self.root / '.pymss-audio-abandoned.wav').write_bytes(b'half')
        (self.root / '.pymss-loudness-abandoned.wav').write_bytes(b'half')
        self.assertEqual(merge._audio_files_in_folder(self.root), [self.source.resolve()])

    def test_killed_worker_cannot_leave_an_incomplete_final_output(self):
        worker = '''from pathlib import Path
import sys, time
from pymss.plugins.kit import render_output as _render_output
root = Path(sys.argv[1])
def render(temporary):
    temporary.write_bytes(b'half')
    (root / 'ready').touch()
    time.sleep(60)
_render_output(root / 'finished.wav', render)
'''
        env = {**os.environ, 'PYTHONPATH': os.pathsep.join(filter(None, [str(Path(__file__).resolve().parents[2]), os.environ.get('PYTHONPATH')]))}
        child = subprocess.Popen([sys.executable, '-c', worker, str(self.output)], env=env, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        try:
            deadline = time.monotonic() + 10
            while not (self.output / 'ready').exists() and time.monotonic() < deadline:
                if child.poll() is not None:
                    self.fail(child.stderr.read().decode(errors='replace'))
                time.sleep(.02)
            self.assertTrue((self.output / 'ready').exists())
        finally:
            child.terminate()
            child.communicate(timeout=10)
        self.assertFalse((self.output / 'finished.wav').exists())
        self.assertEqual(len(list(self.output.glob('.pymss-audio-*.wav'))), 1)
        self.assertEqual(merge._audio_files_in_folder(self.output), [])
