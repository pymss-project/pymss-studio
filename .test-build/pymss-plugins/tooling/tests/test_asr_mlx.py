from __future__ import annotations

import json
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import numpy as np

from plugins.asr.backend import asr_mlx


class MlxModelLocationTests(unittest.TestCase):
    def test_selected_model_download_stays_in_the_studio_model_root(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            checkpoint = root / 'models/selected'
            download = Mock(return_value=str(checkpoint))
            transcribe = Mock(return_value={'text': 'unused'})
            api = types.SimpleNamespace(download_model=download, transcribe=transcribe)
            with patch.dict(sys.modules, {'funasr_mlx': types.ModuleType('funasr_mlx'), 'funasr_mlx.api': api}), \
                 patch.object(asr_mlx, '_transcribe_mlx_ctc', return_value='transcript') as ctc:
                result = asr_mlx.transcribe(root / 'audio.wav', 'paraformer-zh', None, str(root / 'models'))
            self.assertEqual(result['text'], 'transcript')
            download.assert_called_once()
            self.assertEqual(download.call_args.args, ('paraformer-mlx-fp16',))
            self.assertTrue(Path(download.call_args.kwargs['dest']).is_relative_to(root / 'models'))
            transcribe.assert_not_called()
            ctc.assert_called_once_with(root / 'audio.wav', checkpoint, 'paraformer', None, root / 'models/_tool_models/asr/funasr-mlx', preset='paraformer-zh')

    def test_explicit_local_model_never_downloads_or_uses_the_cache_root(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            checkpoint = root / 'local'
            checkpoint.mkdir()
            download = Mock()
            transcribe = Mock(return_value={'text': 'local transcript'})
            api = types.SimpleNamespace(download_model=download, transcribe=transcribe)
            with patch.dict(sys.modules, {'funasr_mlx': types.ModuleType('funasr_mlx'), 'funasr_mlx.api': api}):
                asr_mlx.transcribe(root / 'audio.wav', 'local', str(checkpoint), str(root / 'unused-cache'))
            download.assert_not_called()
            self.assertFalse((root / 'unused-cache').exists())
            transcribe.assert_called_once_with(str(root / 'audio.wav'), model=str(checkpoint))

    def test_long_ctc_audio_is_chunked_and_sensevoice_receives_language(self):
        for family in ('sensevoice', 'paraformer'):
            with self.subTest(family=family):
                samples = np.zeros(16000 * 65, dtype=np.float32)
                read_audio = Mock(return_value=samples)
                frontend = Mock(return_value=np.ones((5, 80)))
                config = types.SimpleNamespace(fs=16000, max_seconds=30.0)
                model = types.SimpleNamespace(greedy_decode=Mock(return_value=[[4, 5]]))
                modules = {
                    'funasr_mlx.frontend': types.SimpleNamespace(
                        read_audio=read_audio, WavFrontend=Mock(return_value=frontend),
                        FbankConfig=types.SimpleNamespace(from_dict=Mock(return_value=config))),
                    'funasr_mlx.pipeline': types.SimpleNamespace(load_model=Mock(return_value=(model, {}))),
                    'mlx': types.ModuleType('mlx'),
                    'mlx.core': types.SimpleNamespace(array=np.array, int32=np.int32),
                }
                with patch.dict(sys.modules, modules), \
                     patch('soundfile.info', return_value=types.SimpleNamespace(duration=65.0)), \
                     patch.object(asr_mlx, '_detok_pieces', side_effect=['first', 'second', 'third']):
                    text = asr_mlx._transcribe_mlx_ctc(Path('voice.wav'), Path('model'), family, 'en', Path('cache'))
                self.assertEqual(text, 'first\nsecond\nthird')
                self.assertEqual([len(call.args[0]) for call in frontend.call_args_list], [480000, 480000, 80000])
                self.assertGreaterEqual(read_audio.call_args.kwargs['max_seconds'], 65)
                self.assertEqual(model.greedy_decode.call_args.kwargs, {'language': 'en'} if family == 'sensevoice' else {})

    def test_failed_transcript_export_does_not_leave_a_partial_final_file(self):
        from plugins.asr.backend import asr

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            def fail_write(path, *args, **kwargs):
                path.write_bytes(b'partial')
                raise OSError('export interrupted')
            transcribe = Mock(return_value={'text': 'recognized'})
            with patch.object(Path, 'write_text', fail_write), patch.object(asr, 'emit'), \
                 self.assertRaisesRegex(OSError, 'export interrupted'):
                asr._transcribe_audio_mlx({}, root / 'voice.wav', root, {'txt'},
                                          {'id': 'sensevoice-small', 'language': 'en'}, transcribe)
            self.assertEqual(list(root.iterdir()), [])
            self.assertEqual(transcribe.call_args.kwargs, {'language': 'en'})

    def test_english_ctc_tokens_are_decoded_as_pieces_with_the_english_tokenizer(self):
        with tempfile.TemporaryDirectory() as temporary:
            checkpoint = Path(temporary)
            (checkpoint / 'tokens.json').write_text(json.dumps(['<blank>', '<s>', '</s>', '▁hello', '▁world']), encoding='utf-8')
            (checkpoint / 'bpe.model').write_bytes(b'tokenizer')
            processor = Mock()
            processor.decode.return_value = 'hello world'
            with patch.dict(sys.modules, {'sentencepiece': types.SimpleNamespace(SentencePieceProcessor=Mock(return_value=processor))}), \
                 patch.object(asr_mlx, '_download_to') as download:
                text = asr_mlx._detok_pieces('paraformer', checkpoint, [0, 1, 3, 4, 2, 100], checkpoint, 'paraformer-en')
            self.assertEqual(text, 'hello world')
            processor.decode.assert_called_once_with(['▁hello', '▁world'])
            download.assert_not_called()

    def test_english_paraformer_requires_a_local_checkpoint_before_downloading(self):
        download = Mock()
        with patch.dict(sys.modules, {'funasr_mlx.api': types.SimpleNamespace(download_model=download)}), \
             self.assertRaisesRegex(ValueError, 'local MLX model directory'):
            asr_mlx.transcribe(Path('voice.wav'), 'paraformer-en', None)
        download.assert_not_called()

    def test_chinese_paraformer_reads_the_official_json_vocabulary(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            vocabulary = root / 'tokens.json'
            vocabulary.write_text(json.dumps(['<blank>', '<s>', '</s>', '你', '好']), encoding='utf-8')
            with patch.object(asr_mlx, '_download_to', return_value=vocabulary) as download:
                text = asr_mlx._detok_pieces('paraformer', root / 'checkpoint', [0, 1, 3, 4, 2], root, 'paraformer-zh')
            self.assertEqual(text, '你好')
            self.assertTrue(download.call_args.args[0].endswith('/tokens.json'))

    def test_tokenizer_download_failure_removes_partial_cache_file(self):
        response = Mock()
        response.read.side_effect = [b'partial', OSError('download interrupted')]
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with patch('urllib.request.urlopen', return_value=response), self.assertRaisesRegex(OSError, 'download interrupted'):
                asr_mlx._download_to('https://example.invalid/tokens', root / 'tokens.json')
            self.assertEqual(list(root.iterdir()), [])
