from __future__ import annotations

import builtins
import math
import shutil
import subprocess
import sys
import unittest
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import numpy as np
import soundfile as sf

if __package__:
    from . import _bootstrap as _worker_test_bootstrap
else:
    import _bootstrap as _worker_test_bootstrap

from worker_audio import _DiskMixBuffer, _read_audio, _resample_audio, cmd_export_editor_mix


class EditorEffectTests(unittest.TestCase):
    def test_pcm_read_preserves_stereo_samples_without_loading_librosa(self) -> None:
        with tempfile.TemporaryDirectory() as temp_value:
            source = Path(temp_value) / 'stereo.wav'
            samples = np.array([[0.1, -0.3], [0.5, -0.2], [-0.4, 0.7]], dtype=np.float32)
            sf.write(source, samples, 44100, subtype='FLOAT')
            original_import = builtins.__import__

            def guard_import(name, *args, **kwargs):
                if name.split('.')[0] in {'librosa', 'numba'}:
                    raise AssertionError('PCM audio must not initialize librosa or Numba')
                return original_import(name, *args, **kwargs)

            with mock.patch('builtins.__import__', side_effect=guard_import):
                audio, rate = _read_audio(source)
            self.assertEqual(rate, 44100)
            self.assertEqual(audio.dtype, np.float32)
            np.testing.assert_array_equal(audio, samples.T)

    def test_pcm_read_resamples_mono_without_loading_librosa(self) -> None:
        with tempfile.TemporaryDirectory() as temp_value:
            source = Path(temp_value) / 'mono.wav'
            samples = (np.sin(np.arange(4001) * 2 * np.pi * 440 / 40000) * .4).astype(np.float32)
            sf.write(source, samples, 40000, subtype='FLOAT')
            original_import = builtins.__import__

            def guard_import(name, *args, **kwargs):
                if name.split('.')[0] in {'librosa', 'numba'}:
                    raise AssertionError('PCM resampling must not initialize librosa or Numba')
                return original_import(name, *args, **kwargs)

            with mock.patch('builtins.__import__', side_effect=guard_import):
                audio, rate = _read_audio(source, 44100)
            self.assertEqual(rate, 44100)
            self.assertEqual(audio.shape, (1, math.ceil(len(samples) * 44100 / 40000)))
            self.assertEqual(audio.dtype, np.float32)
            self.assertTrue(np.isfinite(audio).all())
            self.assertLessEqual(float(np.max(np.abs(audio))), .41)

    def test_resampling_preserves_stereo_polarity_and_rounded_duration(self) -> None:
        signal = (np.sin(np.arange(4001) * 2 * np.pi * 440 / 40000) * .4).astype(np.float32)
        audio = np.stack([signal, -signal])
        rendered = _resample_audio(audio, 40000, 44100)
        self.assertEqual(rendered.shape, (2, math.ceil(len(signal) * 44100 / 40000)))
        np.testing.assert_allclose(rendered[0], -rendered[1], atol=1e-6)

    def test_read_keeps_decoder_fallback_for_unsupported_formats(self) -> None:
        decoder = mock.Mock()
        samples = np.array([0.1, -0.2, 0.3], dtype=np.float32)
        decoder.load.return_value = (samples, 22050)
        with mock.patch('soundfile.read', side_effect=RuntimeError('Unsupported format')), \
                mock.patch.dict(sys.modules, {'librosa': decoder}):
            audio, rate = _read_audio(Path('source.m4a'), 22050)
        decoder.load.assert_called_once_with('source.m4a', sr=22050, mono=False)
        self.assertEqual(rate, 22050)
        np.testing.assert_array_equal(audio, samples.reshape(1, -1))

    def _export_payload(self, root: Path, source: Path, *, name: str = "effects") -> dict:
        return {
            "project": {
                "id": "project",
                "name": name,
                "masterVolume": 1,
                "masterPan": 0,
                "assets": [{
                    "id": "source",
                    "path": str(source),
                    "duration": 0.2,
                    "sampleRate": 1000,
                    "channels": 1,
                }],
                "tracks": [{
                    "id": "track",
                    "sourceId": "source",
                    "volume": 1,
                    "pan": 0,
                    "muted": False,
                    "solo": False,
                    "clips": [{
                        "assetId": "source",
                        "start": 0,
                        "offset": 0,
                        "duration": 0.2,
                        "volume": 1,
                        "fadeIn": 0,
                        "fadeOut": 0,
                        "muted": False,
                    }],
                }],
            },
            "exportDir": str(root / "output"),
            "format": "wav",
        }

    def test_disk_mix_buffer_grows_without_losing_mono_or_stereo_content(self) -> None:
        with tempfile.TemporaryDirectory() as temp_value:
            root = Path(temp_value)
            buffer = _DiskMixBuffer(np, root, ".mix-test-", 1, 4)
            original_path = buffer.path
            buffer.add(0, np.ones((1, 4), dtype=np.float32))
            buffer.add(4, np.array([[0.5, 0.5], [0.25, 0.25]], dtype=np.float32))
            rendered = buffer.block(0, 6, 2)
            grown_path = buffer.path
            buffer.close()

            np.testing.assert_array_equal(rendered[:, :4], np.ones((2, 4), dtype=np.float32))
            np.testing.assert_array_equal(rendered[:, 4:], np.array([[0.5, 0.5], [0.25, 0.25]], dtype=np.float32))
            self.assertFalse(original_path.exists())
            self.assertFalse(grown_path.exists())

    def test_editor_export_ignores_legacy_track_effects(self) -> None:
        with tempfile.TemporaryDirectory() as temp_value:
            root = Path(temp_value)
            source = root / "source.wav"
            samples = np.zeros(200, dtype=np.float32)
            samples[0] = 0.5
            sf.write(source, samples, 1000, subtype="FLOAT")
            payload = self._export_payload(root, source)
            payload["project"]["tracks"][0]["effects"] = {
                "reverb": 1, "delay": 1, "delayTime": 0.1, "clarity": 1, "compressor": 1,
            }
            with mock.patch("worker_audio.emit"):
                self.assertEqual(cmd_export_editor_mix(payload), 0)
            rendered, sample_rate = sf.read(str(root / "output" / "effects_mix.wav"))

        self.assertEqual(sample_rate, 1000)
        self.assertEqual(len(rendered), len(samples))
        self.assertGreater(float(rendered[0]), 0.0)
        self.assertEqual(float(np.abs(rendered[1:]).max()), 0.0)

    def test_repeated_editor_exports_preserve_the_previous_mix(self) -> None:
        with tempfile.TemporaryDirectory() as temp_value:
            root = Path(temp_value)
            source = root / "source.wav"
            sf.write(source, np.ones(200, dtype=np.float32) * 0.1, 1000)
            payload = self._export_payload(root, source, name="repeat")
            events: list[dict] = []
            with mock.patch("worker_audio.emit", side_effect=lambda kind, value, **_kwargs: events.append(value) if kind == "editor_mix_exported" else None):
                self.assertEqual(cmd_export_editor_mix(payload), 0)
                first = root / "output" / "repeat_mix.wav"
                first_bytes = first.read_bytes()
                self.assertEqual(cmd_export_editor_mix(payload), 0)

            second = root / "output" / "repeat_mix_2.wav"
            self.assertTrue(second.is_file())
            self.assertEqual(first.read_bytes(), first_bytes)
            self.assertEqual([Path(event["path"]).name for event in events], [first.name, second.name])

    def test_export_fails_instead_of_silently_skipping_a_missing_source(self) -> None:
        with tempfile.TemporaryDirectory() as temp_value:
            root = Path(temp_value)
            source = root / "source.wav"
            sf.write(source, np.ones(100, dtype=np.float32) * 0.1, 1000)
            payload = self._export_payload(root, source, name="missing")
            payload["project"]["assets"].append({
                "id": "missing",
                "path": str(root / "missing.wav"),
                "name": "missing.wav",
                "duration": 0.1,
                "sampleRate": 1000,
                "channels": 1,
            })
            payload["project"]["tracks"].append({
                "id": "missing-track",
                "sourceId": "missing",
                "volume": 1,
                "pan": 0,
                "muted": False,
                "solo": False,
                "clips": [{
                    "assetId": "missing",
                    "start": 0,
                    "offset": 0,
                    "duration": 0.1,
                    "volume": 1,
                    "muted": False,
                }],
            })
            with mock.patch("worker_audio.emit"), \
                 mock.patch("worker_audio.emit_error", return_value=1) as emit_error:
                self.assertEqual(cmd_export_editor_mix(payload), 1)

            self.assertEqual(emit_error.call_args.args[0], "EDITOR_EXPORT_SOURCE_MISSING")
            self.assertIn("missing.wav", emit_error.call_args.args[1])
            self.assertFalse((root / "output" / "missing_mix.wav").exists())

    def test_auto_sample_rate_uses_the_highest_active_rate_with_a_48khz_cap(self) -> None:
        with tempfile.TemporaryDirectory() as temp_value:
            root = Path(temp_value)
            first = root / "first.wav"
            second = root / "second.wav"
            sf.write(first, np.ones(1600, dtype=np.float32) * 0.1, 32000)
            sf.write(second, np.ones(4800, dtype=np.float32) * 0.1, 96000)
            payload = self._export_payload(root, first, name="rates")
            payload["project"]["assets"][0].update(duration=0.05, sampleRate=32000)
            payload["project"]["tracks"][0]["clips"][0]["duration"] = 0.05
            payload["project"]["assets"].append({
                "id": "second",
                "path": str(second),
                "name": second.name,
                "duration": 0.05,
                "sampleRate": 96000,
                "channels": 1,
            })
            payload["project"]["tracks"].append({
                "id": "second-track",
                "sourceId": "second",
                "volume": 1,
                "pan": 0,
                "muted": False,
                "solo": False,
                "clips": [{
                    "assetId": "second",
                    "start": 0,
                    "offset": 0,
                    "duration": 0.05,
                    "volume": 1,
                    "muted": False,
                }],
            })
            events: list[dict] = []
            with mock.patch("worker_audio.emit", side_effect=lambda kind, value, **_kwargs: events.append(value) if kind == "editor_mix_exported" else None):
                self.assertEqual(cmd_export_editor_mix(payload), 0)

            _rendered, sample_rate = sf.read(root / "output" / "rates_mix.wav")
            self.assertEqual(sample_rate, 48000)
            self.assertEqual(events[-1]["sampleRate"], 48000)

            payload["fileName"] = "rates_44100.flac"
            payload["format"] = "flac"
            payload["audioParams"] = {"sampleRate": 44100, "flacBitDepth": "PCM_24"}
            with mock.patch("worker_audio.emit"):
                self.assertEqual(cmd_export_editor_mix(payload), 0)
            _rendered, sample_rate = sf.read(root / "output" / "rates_44100.flac")
            self.assertEqual(sample_rate, 44100)

    def test_pre_rendered_float_audio_preserves_samples_and_original_auto_rate(self) -> None:
        with tempfile.TemporaryDirectory() as temp_value:
            root = Path(temp_value)
            source = root / 'rendered.wav'
            signal = np.sin(np.arange(9600) * 2 * np.pi * 440 / 48000).astype(np.float32) * .1
            samples = np.stack([signal, -signal], axis=1)
            sf.write(source, samples, 48000, subtype='FLOAT')
            payload = self._export_payload(root, source, name='native')
            payload['project']['assets'][0].update(sampleRate=48000, channels=2)
            payload['audioParams'] = {'wavBitDepth': 'FLOAT', 'peakProtection': False}
            with mock.patch('worker_audio.emit'):
                self.assertEqual(cmd_export_editor_mix(payload), 0)
            actual, rate = sf.read(root / 'output/native_mix.wav', dtype='float32', always_2d=True)
            self.assertEqual(rate, 48000)
            np.testing.assert_array_equal(actual, samples)
            payload['project']['sourceSampleRate'] = 44100
            with mock.patch('worker_audio.emit'):
                self.assertEqual(cmd_export_editor_mix(payload), 0)
            self.assertEqual(sf.info(root / 'output/native_mix_2.wav').samplerate, 44100)

    def test_peak_protection_is_optional_and_reports_its_adjustment(self) -> None:
        with tempfile.TemporaryDirectory() as temp_value:
            root = Path(temp_value)
            source = root / "source.wav"
            sf.write(source, np.ones(100, dtype=np.float32) * 0.8, 1000, subtype="FLOAT")
            payload = self._export_payload(root, source, name="peak")
            payload["project"]["tracks"][0]["volume"] = 2
            payload["project"]["tracks"][0]["clips"][0]["duration"] = 0.1
            payload["audioParams"] = {"wavBitDepth": "FLOAT", "peakProtection": True}
            protected_events: list[dict] = []
            with mock.patch("worker_audio.emit", side_effect=lambda kind, value, **_kwargs: protected_events.append(value) if kind == "editor_mix_exported" else None):
                self.assertEqual(cmd_export_editor_mix(payload), 0)
            protected, _sample_rate = sf.read(root / "output" / "peak_mix.wav")

            payload["fileName"] = "peak_raw.wav"
            payload["audioParams"]["peakProtection"] = False
            raw_events: list[dict] = []
            with mock.patch("worker_audio.emit", side_effect=lambda kind, value, **_kwargs: raw_events.append(value) if kind == "editor_mix_exported" else None):
                self.assertEqual(cmd_export_editor_mix(payload), 0)
            raw, _sample_rate = sf.read(root / "output" / "peak_raw.wav")

            self.assertLessEqual(float(np.max(np.abs(protected))), 0.981)
            self.assertGreater(float(np.max(np.abs(raw))), 1.5)
            self.assertTrue(protected_events[-1]["peakProtectionApplied"])
            self.assertLess(protected_events[-1]["peakAdjustmentDb"], 0)
            self.assertFalse(raw_events[-1]["peakProtectionApplied"])

            payload['fileName'] = 'peak_default.wav'
            payload['audioParams'].pop('peakProtection')
            with mock.patch('worker_audio.emit'):
                self.assertEqual(cmd_export_editor_mix(payload), 0)
            unchanged, _ = sf.read(root / 'output/peak_default.wav')
            np.testing.assert_array_equal(unchanged, raw)

            payload["fileName"] = "peak_pcm.wav"
            payload["audioParams"] = {"wavBitDepth": "PCM_24", "peakProtection": False}
            pcm_events: list[dict] = []
            with mock.patch('worker_audio.emit'), mock.patch('worker_audio.emit_error', return_value=1) as error:
                self.assertEqual(cmd_export_editor_mix(payload), 1)
            self.assertEqual(error.call_args.args[0], 'EDITOR_EXPORT_PEAK_EXCEEDED')
            self.assertFalse((root / 'output/peak_pcm.wav').exists())
            payload['audioParams']['peakProtection'] = True
            with mock.patch("worker_audio.emit", side_effect=lambda kind, value, **_kwargs: pcm_events.append(value) if kind == "editor_mix_exported" else None):
                self.assertEqual(cmd_export_editor_mix(payload), 0)
            pcm, _sample_rate = sf.read(root / "output" / "peak_pcm.wav")
            self.assertLessEqual(float(np.max(np.abs(pcm))), 0.981)
            self.assertTrue(pcm_events[-1]["peakProtectionApplied"])

    def test_export_mix_uses_only_audible_tracks_and_preserves_their_set_balance(self) -> None:
        with tempfile.TemporaryDirectory() as temp_value:
            root = Path(temp_value)
            voice, background = root / 'voice.wav', root / 'background.wav'
            sf.write(voice, np.tile([.2, .4], (200, 1)), 1000, subtype='FLOAT')
            sf.write(background, np.full(200, .1, dtype=np.float32), 1000, subtype='FLOAT')
            payload = self._export_payload(root, voice, name='audible')
            payload['audioParams'] = {'wavBitDepth': 'FLOAT'}
            project = payload['project']
            project['masterVolume'], project['masterPan'] = 1.25, -.2
            project['assets'][0]['channels'] = 2
            project['assets'].append({'id': 'bg', 'path': str(background), 'duration': .2, 'sampleRate': 1000, 'channels': 1})
            project['assets'].append({'id': 'original', 'path': str(root / 'missing-original.wav'), 'duration': .2, 'sampleRate': 1000, 'channels': 1})
            project['tracks'].append({'id': 'bg', 'sourceId': 'bg', 'volume': 1, 'pan': 0, 'muted': False, 'solo': False})
            project['tracks'].append({'id': 'original', 'sourceId': 'original', 'volume': 1, 'muted': True, 'solo': False})
            with mock.patch('worker_audio.emit'):
                self.assertEqual(cmd_export_editor_mix(payload), 0)
            rendered, _ = sf.read(root / 'output/audible_mix.wav', dtype='float32', always_2d=True)
            np.testing.assert_allclose(rendered, np.tile([.375, .5], (200, 1)), atol=1e-7)

            project['tracks'][0]['solo'] = True
            payload['fileName'] = 'solo.wav'
            with mock.patch('worker_audio.emit'):
                self.assertEqual(cmd_export_editor_mix(payload), 0)
            rendered, _ = sf.read(root / 'output/solo.wav', dtype='float32', always_2d=True)
            np.testing.assert_allclose(rendered, np.tile([.25, .4], (200, 1)), atol=1e-7)

            project['tracks'][0]['solo'] = False
            project['tracks'][2]['solo'] = True
            payload['fileName'] = 'silent_solo.wav'
            with mock.patch('worker_audio.emit'):
                self.assertEqual(cmd_export_editor_mix(payload), 0)
            rendered, _ = sf.read(root / 'output/silent_solo.wav', dtype='float32', always_2d=True)
            self.assertEqual(rendered.shape, (200, 2))
            np.testing.assert_array_equal(rendered, 0)

    def test_inaudible_multichannel_tracks_do_not_change_export_channels_or_rate(self) -> None:
        with tempfile.TemporaryDirectory() as temp_value:
            root = Path(temp_value)
            source = root / "mono.wav"
            sf.write(source, np.full(200, .1, dtype=np.float32), 1000, subtype="FLOAT")
            payload = self._export_payload(root, source, name="mono")
            payload["audioParams"] = {"wavBitDepth": "FLOAT"}
            project = payload["project"]
            project["tracks"][0]["effects"] = {}
            # An offline excluded source must never be decoded, even when its
            # metadata advertises more channels and a higher sample rate.
            project["assets"].append({"id": "surround", "path": str(root / "offline.wav"),
                                      "sampleRate": 96000, "channels": 6, "duration": 1})
            for index, state in enumerate(({"muted": True}, {"volume": 0}, {"solo": False})):
                with self.subTest(state=state):
                    project["tracks"][0]["solo"] = index == 2
                    project["tracks"] = project["tracks"][:1] + [{
                        "id": "excluded", "sourceId": "surround", "volume": 1,
                        "muted": False, "solo": False, **state,
                    }]
                    payload["fileName"] = f"excluded_{index}"
                    with mock.patch("worker_audio.emit"):
                        self.assertEqual(cmd_export_editor_mix(payload), 0)
                    actual, rate = sf.read(root / "output" / f"excluded_{index}.wav", dtype="float32", always_2d=True)
                    self.assertEqual(rate, 1000)
                    self.assertEqual(actual.shape, (1000, 1))
                    np.testing.assert_array_equal(actual[:200, 0], np.full(200, .1, dtype=np.float32))
                    np.testing.assert_array_equal(actual[200:], 0)

    @unittest.skipUnless(shutil.which("ffmpeg"), "FFmpeg is required for preview downmix parity")
    def test_multichannel_export_matches_preview_downmix_and_pan(self) -> None:
        with tempfile.TemporaryDirectory() as temp_value:
            root = Path(temp_value)
            source = root / "surround.wav"
            samples = np.zeros((9600, 6), dtype=np.float32)
            time = np.arange(len(samples)) / 48000
            # Center and surround content must remain audible after the stereo downmix.
            samples[:, 2] = .1 * np.sin(2 * np.pi * 440 * time)
            samples[:, 4] = .05 * np.sin(2 * np.pi * 880 * time)
            samples[:, 5] = .03 * np.sin(2 * np.pi * 660 * time)
            sf.write(source, samples, 48000, subtype="FLOAT")
            reference = subprocess.run([
                shutil.which("ffmpeg"), "-nostdin", "-v", "error", "-threads", "1",
                "-i", str(source), "-map", "0:a:0", "-vn", "-ar", "48000", "-ac", "2",
                "-c:a", "pcm_f32le", "-f", "f32le", "pipe:1",
            ], check=True, capture_output=True)
            expected = np.frombuffer(reference.stdout, dtype="<f4").reshape(-1, 2)
            self.assertGreater(float(np.max(np.abs(expected))), .01)
            for pan in (0, -1, 1):
                with self.subTest(pan=pan):
                    payload = self._export_payload(root, source)
                    payload["project"]["assets"][0].update(sampleRate=48000, channels=6)
                    payload["project"]["tracks"][0].update(effects={}, pan=pan)
                    payload["audioParams"] = {"wavBitDepth": "FLOAT"}
                    payload["fileName"] = f"surround_{pan}"
                    events = []
                    with mock.patch("worker_audio.emit", side_effect=lambda kind, value, **_: events.append(value) if kind == "editor_mix_exported" else None):
                        self.assertEqual(cmd_export_editor_mix(payload), 0)
                    actual, rate = sf.read(root / "output" / f"surround_{pan}.wav", dtype="float32", always_2d=True)
                    self.assertEqual(rate, 48000)
                    self.assertEqual(actual.shape, expected.shape)
                    gains = [1 if pan <= 0 else 1 - pan, 1 if pan >= 0 else 1 + pan]
                    np.testing.assert_allclose(actual, expected * gains, atol=1e-7)
                    self.assertEqual(events[-1]["channels"], 2)

    def test_multichannel_decode_failure_never_falls_back_to_surround_output(self) -> None:
        with tempfile.TemporaryDirectory() as temp_value:
            root = Path(temp_value)
            source = root / "surround.wav"
            sf.write(source, np.zeros((100, 6), dtype=np.float32), 48000, subtype="FLOAT")
            for executable, status, data, message in (
                (None, 0, b"", "FFmpeg is required"),
                ("ffmpeg", 1, b"", "decoder failed"),
                ("ffmpeg", 0, b"bad", "invalid stereo PCM"),
            ):
                with self.subTest(message=message):
                    payload = self._export_payload(root, source)
                    payload["project"]["assets"][0].update(sampleRate=48000, channels=6)
                    payload["project"]["tracks"][0]["effects"] = {}
                    result = subprocess.CompletedProcess([], status, data, b"decoder failed")
                    with mock.patch("worker_audio.shutil.which", return_value=executable), \
                            mock.patch("worker_audio.subprocess.run", return_value=result), \
                            mock.patch("worker_audio.emit"), \
                            mock.patch("worker_audio.emit_error", return_value=1) as error:
                        self.assertEqual(cmd_export_editor_mix(payload), 1)
                    self.assertIn(message, error.call_args.args[1])
                    self.assertEqual(list((root / "output").iterdir()), [])

    def test_export_fades_match_the_linear_preview_curve(self) -> None:
        with tempfile.TemporaryDirectory() as temp_value:
            root = Path(temp_value)
            source = root / "source.wav"
            sf.write(source, np.ones(100, dtype=np.float32), 1000, subtype="FLOAT")
            payload = self._export_payload(root, source, name="fade")
            payload["project"]["tracks"][0]["clips"][0].update(duration=0.1, fadeIn=0.1)
            with mock.patch("worker_audio.emit"):
                self.assertEqual(cmd_export_editor_mix(payload), 0)
            rendered, _sample_rate = sf.read(root / "output" / "fade_mix.wav")

            self.assertAlmostEqual(float(rendered[50]), 50 / 99, places=3)

    def test_multi_clip_preallocation_preserves_timeline_overlap(self) -> None:
        with tempfile.TemporaryDirectory() as temp_value:
            root = Path(temp_value)
            source = root / "source.wav"
            sf.write(source, np.ones(100, dtype=np.float32) * 0.1, 1000, subtype="FLOAT")
            payload = self._export_payload(root, source, name="timeline")
            track = payload["project"]["tracks"][0]
            track["clips"] = [
                {
                    "id": "first",
                    "assetId": "source",
                    "start": 0,
                    "offset": 0,
                    "duration": 0.1,
                    "volume": 1,
                    "muted": False,
                },
                {
                    "id": "second",
                    "assetId": "source",
                    "start": 0.05,
                    "offset": 0,
                    "duration": 0.1,
                    "volume": 1,
                    "muted": False,
                },
            ]
            with mock.patch("worker_audio.emit"):
                self.assertEqual(cmd_export_editor_mix(payload), 0)
            rendered, _sample_rate = sf.read(root / "output" / "timeline_mix.wav")

            self.assertEqual(len(rendered), 150)
            self.assertAlmostEqual(float(rendered[25]), 0.1, places=3)
            self.assertAlmostEqual(float(rendered[75]), 0.2, places=3)
            self.assertAlmostEqual(float(rendered[125]), 0.1, places=3)

    def test_export_preserves_the_timeline_through_a_muted_tail(self) -> None:
        with tempfile.TemporaryDirectory() as temp_value:
            root = Path(temp_value)
            source = root / "source.wav"
            sf.write(source, np.full(4800, 0.125, dtype=np.float32), 48000, subtype="FLOAT")
            payload = self._export_payload(root, source, name="muted-tail")
            payload["project"]["assets"][0].update(sampleRate=48000, duration=0.1)
            track = payload["project"]["tracks"][0]
            track["clips"][0]["duration"] = 0.1
            track["clips"].append({
                "id": "muted-tail",
                "assetId": "offline-source",
                "start": 6.2,
                "offset": 0,
                "duration": 0.1,
                "volume": 1,
                "muted": True,
            })
            for format_name in ("wav", "flac"):
                with self.subTest(format=format_name):
                    payload["format"] = format_name
                    with mock.patch("worker_audio.emit") as emit:
                        self.assertEqual(cmd_export_editor_mix(payload), 0)
                    result = emit.call_args.args[1]
                    rendered, rate = sf.read(root / "output" / f"muted-tail_mix.{format_name}")
                    self.assertEqual(rate, 48000)
                    self.assertEqual(len(rendered), 302400)
                    self.assertEqual(result["duration"], 6.3)
                    np.testing.assert_array_equal(rendered[:4800], 0.125)
                    np.testing.assert_array_equal(rendered[4800:], 0)

    def test_export_creates_an_equal_length_silent_file_when_all_clips_are_muted(self) -> None:
        with tempfile.TemporaryDirectory() as temp_value:
            root = Path(temp_value)
            payload = self._export_payload(root, root / "offline.wav", name="silent")
            project = payload["project"]
            project["assets"][0].update(sampleRate=32000, duration=0.25, channels=2)
            project["tracks"][0]["clips"][0].update(start=0.25, duration=0.25, muted=True)
            for format_name in ("wav", "flac"):
                with self.subTest(format=format_name):
                    payload["format"] = format_name
                    with mock.patch("worker_audio.emit") as emit, mock.patch("worker_audio._read_editor_audio") as read:
                        self.assertEqual(cmd_export_editor_mix(payload), 0)
                    read.assert_not_called()
                    result = emit.call_args.args[1]
                    rendered, rate = sf.read(root / "output" / f"silent_mix.{format_name}")
                    self.assertEqual(rate, 32000)
                    self.assertEqual(rendered.shape, (16000, 2))
                    self.assertEqual(result["duration"], 0.5)
                    np.testing.assert_array_equal(rendered, 0)

    def test_master_pan_reports_the_final_stereo_channel_count(self) -> None:
        with tempfile.TemporaryDirectory() as temp_value:
            root = Path(temp_value)
            source = root / "source.wav"
            sf.write(source, np.ones(100, dtype=np.float32) * 0.1, 1000)
            payload = self._export_payload(root, source, name="master-pan")
            payload["project"]["masterPan"] = 0.5
            payload["project"]["tracks"][0]["clips"][0]["duration"] = 0.1
            events: list[dict] = []
            with mock.patch("worker_audio.emit", side_effect=lambda kind, value, **_kwargs: events.append(value) if kind == "editor_mix_exported" else None):
                self.assertEqual(cmd_export_editor_mix(payload), 0)
            rendered, _sample_rate = sf.read(root / "output" / "master-pan_mix.wav", always_2d=True)

            self.assertEqual(rendered.shape[1], 2)
            self.assertEqual(events[-1]["channels"], 2)

    def test_windows_reserved_export_names_are_made_safe(self) -> None:
        with tempfile.TemporaryDirectory() as temp_value:
            root = Path(temp_value)
            source = root / "source.wav"
            sf.write(source, np.ones(100, dtype=np.float32) * 0.1, 1000)
            payload = self._export_payload(root, source)
            payload["fileName"] = "CON.wav"
            payload["project"]["tracks"][0]["clips"][0]["duration"] = 0.1
            with mock.patch("worker_audio.emit"):
                self.assertEqual(cmd_export_editor_mix(payload), 0)

            self.assertTrue((root / "output" / "_CON.wav").is_file())

    def test_background_export_reports_monotonic_progress_with_the_task_id(self) -> None:
        with tempfile.TemporaryDirectory() as temp_value:
            root = Path(temp_value)
            source = root / "source.wav"
            sf.write(source, np.ones(100, dtype=np.float32) * 0.1, 1000)
            payload = self._export_payload(root, source, name="progress")
            payload["taskId"] = "editor-export-task"
            payload["project"]["tracks"][0]["clips"][0]["duration"] = 0.1
            events: list[tuple[str, dict, str | None]] = []
            with mock.patch(
                "worker_audio.emit",
                side_effect=lambda kind, value, task_id=None: events.append((kind, value, task_id)),
            ):
                self.assertEqual(cmd_export_editor_mix(payload), 0)

            progress = [value["completed"] for kind, value, _task_id in events if kind == "editor_export_progress"]
            self.assertEqual(progress, sorted(progress))
            self.assertEqual(progress[-1], 100)
            self.assertTrue(all(task_id == "editor-export-task" for _kind, _value, task_id in events))
            self.assertEqual(events[-1][0], "editor_mix_exported")

    def test_export_write_failure_removes_hidden_and_visible_partial_files(self) -> None:
        with tempfile.TemporaryDirectory() as temp_value:
            root = Path(temp_value)
            source = root / "source.wav"
            sf.write(source, np.ones(100, dtype=np.float32) * 0.1, 1000)
            payload = self._export_payload(root, source, name="write-failure")
            payload["project"]["tracks"][0]["clips"][0]["duration"] = 0.1
            with mock.patch("worker_audio.emit"), \
                 mock.patch("soundfile.SoundFile.write", side_effect=RuntimeError("write failed")), \
                 mock.patch("worker_audio.emit_error", return_value=1):
                self.assertEqual(cmd_export_editor_mix(payload), 1)

            output_dir = root / "output"
            self.assertFalse((output_dir / "write-failure_mix.wav").exists())
            self.assertEqual(list(output_dir.glob(".pymss-export-*.part")), [])

    def test_cooperative_cancellation_during_write_removes_the_partial_file(self) -> None:
        with tempfile.TemporaryDirectory() as temp_value:
            root = Path(temp_value)
            source = root / "source.wav"
            sf.write(source, np.ones(100, dtype=np.float32) * 0.1, 1000)
            payload = self._export_payload(root, source, name="cancelled")
            payload["taskId"] = "cancel-write"
            payload["project"]["tracks"][0]["clips"][0]["duration"] = 0.1
            output_dir = root / "output"
            cancel_path = output_dir / ".pymss-export-cancel-write.cancel"
            events: list[str] = []

            def cancel_during_write(*_args, **_kwargs):
                cancel_path.write_text("cancel", encoding="utf-8")

            with mock.patch("soundfile.SoundFile.write", side_effect=cancel_during_write), \
                 mock.patch("worker_audio.emit", side_effect=lambda kind, _value, **_kwargs: events.append(kind)):
                self.assertEqual(cmd_export_editor_mix(payload), 0)

            self.assertIn("task_cancelled", events)
            self.assertFalse((output_dir / "cancelled_mix.wav").exists())
            self.assertEqual(list(output_dir.glob(".pymss-export-*.part")), [])
            self.assertFalse(cancel_path.exists())


class AudioTrackProbeTests(unittest.TestCase):
    def test_batch_probe_uses_kernel_api_and_keeps_per_file_errors(self) -> None:
        from worker_audio import cmd_audio_metadata
        tracks = [{"index": 1, "language": "jpn", "title": "", "channels": 1, "sampleRate": 44100},
                  {"index": 2, "language": "eng", "title": "Dub", "channels": 2, "sampleRate": 48000}]
        probe = mock.Mock(side_effect=[tracks, FileNotFoundError("media is unavailable")])
        with mock.patch.dict(sys.modules, {"pymss": SimpleNamespace(list_audio_tracks=probe)}),                 mock.patch("worker_audio.emit") as emit:
            self.assertEqual(cmd_audio_metadata({"paths": ["movie.mkv", "missing.mkv", "movie.mkv"]}), 0)
        result = emit.call_args.args[1]
        self.assertTrue(result["audioTrackSelectionSupported"])
        self.assertEqual(result["audioTracksByPath"], {"movie.mkv": tracks, "missing.mkv": []})
        self.assertEqual(result["audioTrackErrorsByPath"], {"missing.mkv": "media is unavailable"})
        self.assertEqual(probe.call_args_list, [mock.call("movie.mkv"), mock.call("missing.mkv")])

    def test_old_kernel_disables_audio_track_selection(self) -> None:
        from worker_audio import _audio_tracks_by_path
        with mock.patch.dict(sys.modules, {"pymss": SimpleNamespace()}):
            result = _audio_tracks_by_path(["movie.mkv"])
        self.assertEqual(result, {"audioTrackSelectionSupported": False, "audioTracksByPath": {}, "audioTrackErrorsByPath": {}})

    def test_metadata_keeps_ordinary_audio_available_with_an_old_kernel(self) -> None:
        from worker_audio import _audio_metadata
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "mono.wav"
            sf.write(source, np.zeros(1000, dtype=np.float32), 8000)
            with mock.patch.dict(sys.modules, {"pymss": SimpleNamespace()}):
                metadata = _audio_metadata(source)
        self.assertEqual(metadata["sampleRate"], 8000)
        self.assertEqual(metadata["channels"], 1)
        self.assertFalse(metadata["audioTrackSelectionSupported"])
        self.assertEqual(metadata["audioTracks"], [])


if __name__ == "__main__":
    unittest.main()
