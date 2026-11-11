from __future__ import annotations

import copy
import os
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

import yaml

if __package__:
    from . import _bootstrap as _worker_test_bootstrap
else:
    import _bootstrap as _worker_test_bootstrap

import worker_infer
import worker_models
import worker_workflows
import worker_inference_snapshot


class InferenceParameterCompatibilityTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.config_path = self.root / "model.yaml"
        self.config = {
            "audio": {"chunk_size": 588800},
            "inference": {"chunk_size": 882000, "num_overlap": 2, "batch_size": 1},
        }
        self.entry = types.SimpleNamespace(model_type="bs_roformer")
        self.pymss_config = types.ModuleType("pymss.config")
        self.pymss_config.load_config = lambda path: yaml.safe_load(Path(path).read_text(encoding="utf-8"))
        self.pymss_config.to_plain = lambda config: config
        pymss = types.ModuleType("pymss")
        pymss.config = self.pymss_config
        modules = mock.patch.dict(sys.modules, {"pymss": pymss, "pymss.config": self.pymss_config})
        modules.start()
        self.addCleanup(modules.stop)
        self.write_config()

    def write_config(self) -> None:
        self.config_path.write_text(yaml.safe_dump(self.config), encoding="utf-8")

    def runtime_params(self, params=None, model_type="bs_roformer"):
        return worker_infer._enrich_inference_params_for_model(
            model_type=model_type,
            config_path=str(self.config_path),
            inference_params=params or {},
        )

    def test_model_defaults_prefer_inference_chunk_size(self) -> None:
        defaults = worker_models.resolve_default_inference_params(
            self.entry, self.root / "model.ckpt", self.config_path,
        )
        self.assertEqual(defaults["chunk_size"], 882000)
        self.assertEqual(defaults["num_overlap"], 2)

    def test_resolved_ensemble_factory_uses_each_models_frozen_parameters(self) -> None:
        resolution = worker_models.resolve_inference_param_meta(self.entry, self.config_path)["resolution"]
        parameters = {**resolution["defaults"], "overlap_size": 0}
        snapshot = {"version": 1, "mode": "custom", "intent": {}, "sources": {}, "adjustments": {}, "fingerprint": resolution["fingerprint"], "parameters": parameters, "useTta": True}
        separator_type = mock.Mock()
        factory = worker_workflows._workflow_snapshot_factory({"resolvedInferences": {"model-a": snapshot}})
        with (
            mock.patch.object(worker_infer, "_resolve_studio_model", return_value={"model_type": "bs_roformer", "config_path": str(self.config_path), "model_path": str(self.root / "model.ckpt")}),
            mock.patch.object(worker_infer, "_studio_separator_type", return_value=separator_type),
        ):
            factory(model_name="model-a", inference_params={"chunk_size": 1}, use_tta=False)
        kwargs = separator_type.from_model_name.call_args.kwargs
        self.assertEqual(kwargs["inference_params"], parameters)
        self.assertIs(kwargs["use_tta"], True)
        self.assertIs(worker_workflows._workflow_snapshot_factory({}), worker_workflows._workflow_separator_factory)

    def test_mdx_chunk_validation_rejects_custom_size_before_constructing_a_model(self) -> None:
        from worker_inference_constraints import InferenceParameterError

        self.config["audio"] = {"hop_length": 1024, "n_fft": 8192, "chunk_size": 261120}
        self.config["model"] = {"num_scales": 5, "scale": [2, 2]}
        self.config["inference"].pop("chunk_size")
        self.write_config()
        meta = worker_models.resolve_inference_param_meta(types.SimpleNamespace(model_type="mdx23c"), self.config_path)
        self.assertEqual(meta["chunkSizeConstraint"], {"step": 32768, "offset": 31744, "min": 31744})
        with self.assertRaisesRegex(InferenceParameterError, "457728 or 490496"):
            self.runtime_params({"chunk_size": 465920, "overlap_size": 350208}, "mdx23c")
        for chunk in [261120, 457728, 490496]:
            params = self.runtime_params({"chunk_size": chunk, "overlap_size": 350208}, "mdx23c")
            self.assertEqual(params, {"chunk_size": chunk, "overlap_size": 350208})
        self.assertNotIn("chunk_size", self.runtime_params({}, "mdx23c"))

    def test_recommended_sample_step_uses_architecture_hop(self) -> None:
        cases = (
            ("bs_roformer", {"model": {"stft_hop_length": 512, "fft_size": 2048}}, 512, "model.stft_hop_length"),
            ("bs_roformer", {"audio": {"hop_length": 441}, "model": {"fft_size": 2048}}, 441, "audio.hop_length"),
            ("mdx23c", {"audio": {"hop_length": 1024}}, 1024, "audio.hop_length"),
            ("scnet", {"model": {"hop_size": 960}}, 960, "model.hop_size"),
            ("bandit_v2", {"kwargs": {"hop_length": 256}}, 256, "kwargs.hop_length"),
            ("apollo", {"model": {"sr": 44100, "win": 20}}, 441, "model.sr+win"),
        )
        for model_type, config, expected_step, expected_source in cases:
            with self.subTest(model_type=model_type):
                self.config_path.write_text(yaml.safe_dump(config), encoding="utf-8")
                entry = types.SimpleNamespace(model_type=model_type)
                meta = worker_models.resolve_inference_param_meta(entry, self.config_path)
                resolution = meta.pop("resolution")
                self.assertEqual(meta, {
                    "recommendedSampleStep": expected_step,
                    "source": expected_source,
                })
                self.assertEqual(resolution["version"], 1)
                self.assertEqual((resolution["sampleRate"], resolution["sampleRateSource"]), (44100, "sdk-default"))
                self.assertNotIn("num_overlap", resolution["defaults"])

    def test_recommended_sample_step_ignores_vr_and_invalid_values(self) -> None:
        self.config_path.write_text(yaml.safe_dump({"model": {"hop_size": 0, "fft_size": -1}}), encoding="utf-8")
        scnet = worker_models.resolve_inference_param_meta(types.SimpleNamespace(model_type="scnet"), self.config_path)
        self.assertEqual(set(scnet), {"resolution"})
        self.assertEqual(scnet["resolution"]["capabilities"]["chunk_size"]["kind"], "unknown")
        vr = worker_models.resolve_inference_param_meta(types.SimpleNamespace(model_type="vr"), self.config_path)
        # VR never had hop metadata; it now only carries the versioned resolution.
        self.assertEqual(set(vr), {"resolution"})
        self.assertEqual(vr["resolution"]["sampleRate"], 44100)

    def test_roformer_does_not_fall_back_to_fft_or_training_hop(self) -> None:
        self.config_path.write_text(yaml.safe_dump({
            "model": {"fft_size": 2048, "multi_stft_hop_size": 147},
            "audio": {"n_fft": 2048},
        }), encoding="utf-8")
        meta = worker_models.resolve_inference_param_meta(types.SimpleNamespace(model_type="bs_roformer"), self.config_path)
        self.assertNotIn("recommendedSampleStep", meta)
        self.assertNotIn("source", meta)
        self.assertEqual(set(meta), {"resolution"})

    def test_separator_receives_overlap_for_the_effective_chunk_size(self) -> None:
        separator_type = mock.Mock()
        resolved = {
            "model_type": "bs_roformer",
            "model_path": str(self.root / "model.ckpt"),
            "config_path": str(self.config_path),
        }
        payload = {
            "model": "bs_roformer", "download": False,
            "output": str(self.root / "output"),
            "inferenceParamsVersion": 2, "inferenceParams": {},
        }
        with (
            mock.patch.object(worker_infer, "_resolve_separator_device", return_value=("cpu", [0], "CPU")),
            mock.patch.object(worker_infer, "_studio_separator_type", return_value=separator_type),
            mock.patch.object(worker_infer, "_resolve_studio_model", return_value=resolved),
            mock.patch.object(worker_infer, "emit"),
        ):
            worker_infer._prepare_separator(
                payload=payload, task_id="separation-1", logger=mock.Mock(), progress_callback=None,
            )

        self.assertEqual(separator_type.call_args.kwargs["inference_params"], {"overlap_size": 441000})
        self.assertEqual(payload["inferenceParams"], {})

    def test_catalog_target_override_reaches_separator_without_affecting_user_models(self) -> None:
        name = "model_mel_band_roformer_ep_0_sdr_11.4805.ckpt"
        weights = self.root / "model.ckpt"
        weights.touch()
        self.config["training"] = {
            "instruments": ["Vocals", "Instrumental"],
            "target_instrument": "Instrumental",
        }
        self.write_config()
        original_config = self.config_path.read_bytes()
        catalog = {
            "name": name, "model_type": "mel_band_roformer", "supported": True,
            "relpath": weights.name, "config_relpath": self.config_path.name,
        }
        user = worker_models.RegisteredUserModelEntry(
            name=name, model_type="mel_band_roformer",
            model_path=str(weights), config_path=str(self.config_path),
        )
        cases = (
            (worker_models.ModelEntry.from_dict({**catalog, "target_instrument_override": "Vocals"}), "Vocals"),
            (worker_models.ModelEntry.from_dict(catalog), None),
            (user, None),
        )
        for entry, expected_override in cases:
            with self.subTest(source=type(entry).__name__, override=expected_override):
                separator_type = mock.Mock()
                with (
                    mock.patch.object(worker_infer, "get_any_model_entry", return_value=entry),
                    mock.patch.object(worker_infer, "_resolve_separator_device", return_value=("cpu", [0], "CPU")),
                    mock.patch.object(worker_infer, "_studio_separator_type", return_value=separator_type),
                    mock.patch.object(worker_infer, "emit"),
                ):
                    worker_infer._prepare_separator(
                        payload={
                            "model": name, "modelDir": str(self.root), "download": False,
                            "output": str(self.root / "output"),
                            "inferenceParamsVersion": 2, "inferenceParams": {},
                        },
                        task_id="separation-1", logger=mock.Mock(), progress_callback=None,
                    )

                kwargs = separator_type.call_args.kwargs
                self.assertEqual(kwargs.get("target_instrument_override"), expected_override)
                if expected_override is None:
                    self.assertNotIn("target_instrument_override", kwargs)
                self.assertEqual(kwargs["model_path"], str(weights))
                self.assertEqual(self.config_path.read_bytes(), original_config)

    def test_legacy_separator_accepts_an_unused_target_override(self) -> None:
        class ConstructorReached(Exception):
            pass

        class LegacySeparator:
            def __init__(self, *, model_type):
                raise ConstructorReached(model_type)

        sys.modules["pymss"].MSSeparator = LegacySeparator
        with mock.patch("worker_rocm.configure_rocm_runtime"), self.assertRaisesRegex(ConstructorReached, "roformer"):
            worker_infer._studio_separator_type()(model_type="roformer", target_instrument_override=None)

    def test_legacy_separator_rejects_a_required_override_before_gpu_initialization(self) -> None:
        class LegacySeparator:
            def __init__(self, *, model_type):
                self.model_type = model_type

        sys.modules["pymss"].MSSeparator = LegacySeparator
        with mock.patch("worker_rocm.configure_rocm_runtime") as configure:
            with self.assertRaisesRegex(RuntimeError, "Update the kernel") as caught:
                worker_infer._studio_separator_type()(model_type="roformer", target_instrument_override="Vocals")
            configure.assert_not_called()
        with mock.patch.object(worker_infer, "emit_error", return_value=1) as emit_error:
            self.assertEqual(worker_infer._emit_inference_error(caught.exception, "legacy-task"), 1)
            self.assertEqual(emit_error.call_args.args[0], "RUNTIME_OUTDATED")
            self.assertFalse(emit_error.call_args.kwargs.get("recoverable", False))

    def test_modern_separator_retains_a_required_override(self) -> None:
        class ConstructorReached(Exception):
            pass

        class ModernSeparator:
            def __init__(self, *, target_instrument_override, **kwargs):
                raise ConstructorReached(target_instrument_override)

        sys.modules["pymss"].MSSeparator = ModernSeparator
        with mock.patch("worker_rocm.configure_rocm_runtime"), self.assertRaisesRegex(ConstructorReached, "Vocals"):
            worker_infer._studio_separator_type()(model_type="roformer", target_instrument_override="Vocals")

    def test_audio_chunk_size_is_used_when_inference_chunk_size_is_missing_or_null(self) -> None:
        for include_null in (False, True):
            with self.subTest(include_null=include_null):
                self.config["inference"].pop("chunk_size", None)
                if include_null:
                    self.config["inference"]["chunk_size"] = None
                self.write_config()
                defaults = worker_models.resolve_default_inference_params(
                    self.entry, self.root / "model.ckpt", self.config_path,
                )
                self.assertEqual(defaults["chunk_size"], 588800)
                self.assertEqual(self.runtime_params(), {"overlap_size": 294400})

    def test_user_chunk_size_and_overlap_count_take_precedence(self) -> None:
        for overrides, expected in (
            ({"chunk_size": 262144}, {"chunk_size": 262144, "overlap_size": 131072}),
            ({"chunk_size": 262144, "num_overlap": 8}, {"chunk_size": 262144, "overlap_size": 229376}),
            ({"num_overlap": 8}, {"overlap_size": 771750}),
            ({"num_overlap": 1}, {"overlap_size": 0}),
        ):
            with self.subTest(overrides=overrides):
                original = copy.deepcopy(overrides)
                self.assertEqual(self.runtime_params(overrides), expected)
                self.assertEqual(overrides, original)

    def test_explicit_overlap_size_is_preserved(self) -> None:
        overrides = {"chunk_size": 262144, "num_overlap": 8, "overlap_size": 12000}
        self.assertEqual(self.runtime_params(overrides), {"chunk_size": 262144, "overlap_size": 12000})

    def test_configured_overlap_size_is_left_to_the_core(self) -> None:
        self.config["inference"]["overlap_size"] = 12000
        self.write_config()
        self.assertEqual(self.runtime_params(), {})
        self.assertEqual(self.runtime_params({"num_overlap": 8}), {"overlap_size": 771750})

    def test_vr_and_apollo_do_not_receive_msst_overlap_conversion(self) -> None:
        for model_type in ("vr", "apollo"):
            with self.subTest(model_type=model_type):
                self.assertEqual(self.runtime_params({"num_overlap": 8}, model_type), {})


VR_BUILTIN_DEFAULTS = {
    "batch_size": 2, "window_size": 512, "aggression": 5, "enable_post_process": False,
    "post_process_threshold": 0.2, "high_end_process": False, "normalize": False,
}


class _ModelFilesMixin:
    def make_files(self, config: dict | None, model_type: str = "bs_roformer") -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.weights = self.root / "model.ckpt"
        self.weights.write_bytes(b"weights")
        self.config_path = self.root / "model.yaml" if config is not None else None
        if config is not None:
            self.config_path.write_text(yaml.safe_dump(config), encoding="utf-8")
        self.model_type = model_type

    def resolution(self) -> dict:
        meta = worker_models.resolve_inference_param_meta(
            types.SimpleNamespace(model_type=self.model_type), self.config_path, model_path=self.weights,
        )
        return meta["resolution"]


class ParameterResolutionMetadataTests(_ModelFilesMixin, unittest.TestCase):
    def test_vr_windows_match_runtime_architecture_without_loading_weights(self) -> None:
        for size, minimum, step in ((31191, 272, 16), (56817, 144, 16), (129605, 288, 32)):
            with self.subTest(size=size), mock.patch.object(worker_inference_snapshot, '_vr_model_metadata', return_value={}), mock.patch.object(worker_inference_snapshot, '_model_stat', return_value={'size': size * 1024}):
                cap = worker_inference_snapshot._vr_window_capability('model.pth')
                self.assertEqual(cap, {'kind': 'lattice', 'min': minimum, 'step': step, 'offset': 0})
        with mock.patch.object(worker_inference_snapshot, '_vr_model_metadata', return_value={'nout': 32, 'nout_lstm': 128}):
            self.assertEqual(worker_inference_snapshot._vr_window_capability(None)['min'], 144)
        with mock.patch.object(worker_inference_snapshot, '_vr_model_metadata', return_value=None):
            self.assertEqual(worker_inference_snapshot._vr_window_capability(None)['kind'], 'unknown')

    def test_invalid_declared_sample_rate_is_not_replaced_with_sdk_default(self) -> None:
        self.make_files({"audio": {"sample_rate": 0, "chunk_size": 573300}, "inference": {"batch_size": 1}})
        resolution = self.resolution()
        self.assertIsNone(resolution["sampleRate"])
        self.assertEqual(resolution["sampleRateSource"], "invalid-config:audio.sample_rate")

    def test_msst_count_overlap_resolves_to_samples_without_leaking_num_overlap(self) -> None:
        self.make_files({
            "audio": {"chunk_size": 588800},
            "inference": {"chunk_size": 882000, "num_overlap": 2, "batch_size": 1},
        })
        resolution = self.resolution()
        self.assertEqual(resolution["defaults"], {
            "batch_size": 1, "standardize": False, "normalize": False,
            "chunk_size": 882000, "overlap_size": 441000,
        })
        self.assertEqual(resolution["overlap"], {"kind": "count", "count": 2})
        self.assertEqual(resolution["capabilities"]["chunk_size"], {"kind": "free", "min": 1, "step": 1})
        self.assertEqual(resolution["capabilities"]["overlap_size"], {"kind": "free", "min": 0, "step": 1})
        # The legacy interface keeps its historical output for existing callers.
        legacy = worker_models.resolve_default_inference_params(
            types.SimpleNamespace(model_type="bs_roformer"), self.weights, self.config_path,
        )
        self.assertEqual(legacy["num_overlap"], 2)

    def test_sample_rate_comes_from_the_sdk_decode_path(self) -> None:
        self.make_files({"audio": {"chunk_size": 480000, "sample_rate": 48000}, "inference": {"batch_size": 2}})
        resolution = self.resolution()
        self.assertEqual((resolution["sampleRate"], resolution["sampleRateSource"]), (48000, "config:audio.sample_rate"))
        # Without overlap_size or num_overlap the SDK falls back to chunk // 2.
        self.assertEqual(resolution["defaults"]["overlap_size"], 240000)
        self.assertEqual(resolution["overlap"], {"kind": "samples"})

    def test_explicit_zero_overlap_and_yaml_standardize_are_preserved(self) -> None:
        self.make_files({
            "audio": {"chunk_size": 352800},
            "inference": {"batch_size": 1, "overlap_size": 0, "normalize": True, "num_overlap": 4},
        })
        defaults = self.resolution()["defaults"]
        self.assertEqual(defaults["overlap_size"], 0)
        # inference.normalize in YAML is input standardization; output normalize defaults off.
        self.assertIs(defaults["standardize"], True)
        self.assertIs(defaults["normalize"], False)

    def test_roformer_minimum_chunk_follows_the_stft_window(self) -> None:
        self.make_files({"audio": {"chunk_size": 485100}, "model": {"stft_n_fft": 2048}, "inference": {"batch_size": 1}})
        self.assertEqual(self.resolution()["capabilities"]["chunk_size"]["min"], 1025)

    def test_mdx23c_exposes_its_chunk_lattice(self) -> None:
        self.make_files({
            "audio": {"hop_length": 1024, "n_fft": 8192, "chunk_size": 261120},
            "model": {"num_scales": 5, "scale": [2, 2]},
            "inference": {"batch_size": 1, "num_overlap": 4},
        }, "mdx23c")
        resolution = self.resolution()
        self.assertEqual(
            resolution["capabilities"]["chunk_size"],
            {"kind": "lattice", "min": 31744, "step": 32768, "offset": 31744},
        )
        self.assertEqual(resolution["defaults"]["overlap_size"], 261120 - 261120 // 4)

    def test_mdx23c_without_derivable_lattice_is_not_claimed_editable(self) -> None:
        self.make_files({"audio": {"hop_length": 1024, "chunk_size": 261120}, "inference": {"batch_size": 1}}, "mdx23c")
        self.assertEqual(self.resolution()["capabilities"]["chunk_size"]["kind"], "unknown")

    def test_htdemucs_chunk_is_fixed_by_its_training_segment(self) -> None:
        config = {
            "training": {"samplerate": 44100, "segment": 11},
            "audio": {"chunk_size": 485100},
            "inference": {"batch_size": 4, "num_overlap": 4},
        }
        self.make_files(config, "htdemucs")
        resolution = self.resolution()
        self.assertEqual(resolution["capabilities"]["chunk_size"]["kind"], "fixed")
        self.assertEqual(resolution["defaults"]["chunk_size"], 485100)
        self.assertEqual(resolution["defaults"]["overlap_size"], 485100 - 485100 // 4)
        self.assertEqual(resolution["capabilities"]["overlap_size"]["kind"], "free")

        config["audio"]["chunk_size"] = 441000
        self.make_files(config, "htdemucs")
        resolution = self.resolution()
        self.assertNotIn("chunk_size", resolution["defaults"])
        self.assertNotIn("overlap_size", resolution["defaults"])
        self.assertEqual(resolution["capabilities"]["overlap_size"]["kind"], "unknown")

    def test_vr_uses_builtin_runtime_defaults_and_keeps_zero_strength(self) -> None:
        self.make_files(None, "vr")
        resolution = self.resolution()
        self.assertEqual(resolution["defaults"], VR_BUILTIN_DEFAULTS)
        self.assertEqual((resolution["sampleRate"], resolution["sampleRateSource"]), (44100, "sdk:vr"))
        for key in ("chunk_size", "overlap_size", "standardize"):
            self.assertEqual(resolution["capabilities"][key]["kind"], "unsupported")
        self.make_files({"inference": {"aggression": 0, "post_process_threshold": 0, "window_size": 320}}, "vr")
        defaults = self.resolution()["defaults"]
        self.assertEqual((defaults["aggression"], defaults["post_process_threshold"], defaults["window_size"]), (0, 0.0, 320))

    def test_legacy_demucs_and_unverified_architectures_do_not_claim_editability(self) -> None:
        self.make_files(None, "legacy_demucs")
        legacy = self.resolution()
        self.assertEqual(legacy["capabilities"]["chunk_size"]["kind"], "unsupported")
        self.assertNotIn("sampleRate", legacy)
        self.make_files({"audio": {"chunk_size": 100000}, "inference": {"batch_size": 1}}, "mystery_net")
        unknown = self.resolution()
        self.assertEqual(unknown["defaults"]["chunk_size"], 100000)
        for key in ("chunk_size", "overlap_size", "batch_size"):
            self.assertEqual(unknown["capabilities"][key]["kind"], "unknown")

    def test_metadata_never_imports_the_inference_runtime(self) -> None:
        self.make_files({"audio": {"chunk_size": 485100}, "inference": {"batch_size": 1}})
        with mock.patch.dict(sys.modules, {"torch": None, "pymss": None, "pymss_core": None}):
            self.assertEqual(self.resolution()["defaults"]["chunk_size"], 485100)
            self.model_type = "vr"
            self.assertEqual(self.resolution()["defaults"]["window_size"], 512)

    def test_fingerprint_tracks_config_and_weight_stat_without_reading_weights(self) -> None:
        self.make_files({"audio": {"chunk_size": 485100}, "inference": {"batch_size": 1}})
        first = self.resolution()["fingerprint"]
        self.assertEqual(self.resolution()["fingerprint"], first)
        stat = self.weights.stat()
        self.weights.write_bytes(b"WEIGHTS")  # same size, different content, restored mtime
        os.utime(self.weights, ns=(stat.st_atime_ns, stat.st_mtime_ns))
        self.assertEqual(self.resolution()["fingerprint"], first)
        self.weights.write_bytes(b"longer weights")
        second = self.resolution()["fingerprint"]
        self.assertNotEqual(second, first)
        self.config_path.write_text(
            yaml.safe_dump({"audio": {"chunk_size": 485100}, "inference": {"batch_size": 2}}), encoding="utf-8",
        )
        self.assertNotEqual(self.resolution()["fingerprint"], second)

    def test_execution_resolves_the_same_metadata_as_the_model_payload(self) -> None:
        self.make_files({"audio": {"chunk_size": 485100}, "inference": {"batch_size": 1}})
        self.assertEqual(
            worker_models.load_parameter_resolution("bs_roformer", str(self.config_path), str(self.weights)),
            self.resolution(),
        )


class ResolvedInferenceSnapshotTests(_ModelFilesMixin, unittest.TestCase):
    def setUp(self) -> None:
        self.make_files({
            "audio": {"chunk_size": 588800},
            "inference": {"chunk_size": 882000, "num_overlap": 2, "batch_size": 1},
        })

    def payload(self, parameters: dict | None = None, *, use_tta: bool = False, **snapshot_overrides) -> dict:
        resolution = self.resolution()
        parameters = dict(resolution["defaults"] if parameters is None else parameters)
        snapshot = {
            "version": 1, "fingerprint": resolution["fingerprint"], "parameters": parameters,
            "useTta": use_tta, "mode": "custom", "intent": {}, "sources": {}, "adjustments": {},
            **snapshot_overrides,
        }
        return {
            "model": "test-model", "download": False, "output": str(self.root / "output"),
            "inferenceParamsVersion": 4, "inferenceParams": copy.deepcopy(parameters),
            "useTta": use_tta, "resolvedInference": snapshot,
        }

    def prepare(self, payload: dict, separator_type: mock.Mock | None = None) -> mock.Mock:
        separator_type = separator_type or mock.Mock()
        resolved = {
            "model_type": self.model_type, "model_path": str(self.weights),
            "config_path": str(self.config_path) if self.config_path else None,
        }
        with (
            mock.patch.object(worker_infer, "_resolve_separator_device", return_value=("cpu", [0], "CPU")),
            mock.patch.object(worker_infer, "_studio_separator_type", return_value=separator_type),
            mock.patch.object(worker_infer, "_resolve_studio_model", return_value=resolved),
            mock.patch.object(worker_infer, "emit"),
        ):
            worker_infer._prepare_separator(payload=payload, task_id="t", logger=mock.Mock(), progress_callback=None)
        return separator_type

    def assert_rejected(self, payload: dict, message: str) -> None:
        from worker_inference_constraints import InferenceParameterError

        separator_type = mock.Mock()
        with self.assertRaisesRegex(InferenceParameterError, message):
            self.prepare(payload, separator_type)
        separator_type.assert_not_called()

    def test_frozen_parameters_reach_the_separator_verbatim_including_zero_overlap(self) -> None:
        parameters = {**self.resolution()["defaults"], "chunk_size": 441000, "overlap_size": 0, "normalize": True}
        kwargs = self.prepare(self.payload(parameters, use_tta=True)).call_args.kwargs
        self.assertEqual(kwargs["inference_params"], parameters)
        self.assertIs(kwargs["use_tta"], True)

    def test_resolved_snapshot_cannot_omit_a_managed_default_field(self) -> None:
        parameters = dict(self.resolution()["defaults"])
        parameters.pop("standardize")
        self.assert_rejected(self.payload(parameters), "incomplete.*standardize")

    def test_saved_native_library_values_survive_unknown_semantic_editability(self) -> None:
        self.model_type = "torchseg"
        parameters = {**self.resolution()["defaults"], "chunk_size": 441000, "overlap_size": 220500}
        payload = self.payload(parameters, sources={"chunk_size": "model-library", "overlap_size": "model-library"})
        self.assertEqual(self.prepare(payload).call_args.kwargs["inference_params"], parameters)

    def test_legacy_versions_still_treat_zero_overlap_as_default(self) -> None:
        for version in (None, 2, 3):
            with self.subTest(version=version):
                params = worker_infer._sanitize_runtime_inference_params(
                    worker_infer.normalize_inference_params({"overlap_size": 0, "chunk_size": 441000}, version)
                )
                self.assertNotIn("overlap_size", params)

    def test_structural_errors_are_rejected_before_device_or_runtime_setup(self) -> None:
        missing = self.payload()
        missing.pop("resolvedInference")
        mismatch = self.payload()
        mismatch["inferenceParams"]["batch_size"] = 2
        typed = self.payload()
        typed["inferenceParams"]["normalize"] = 0
        cases = (
            (missing, "requires a resolvedInference"),
            ({**self.payload(), "inferenceParamsVersion": 5}, "newer than this worker"),
            (self.payload(version=2), "Unsupported resolvedInference version"),
            (mismatch, "differing keys: batch_size"),
            (typed, "differing keys: normalize"),
            ({**self.payload(), "useTta": True}, "useTta"),
            (self.payload({**self.resolution()["defaults"], "num_overlap": 2}), "num_overlap"),
            (self.payload(mode="auto"), "mode"),
        )
        for payload, message in cases:
            with self.subTest(message=message):
                from worker_inference_constraints import InferenceParameterError

                with (
                    mock.patch.object(worker_infer, "_resolve_separator_device") as device,
                    mock.patch.object(worker_infer, "_studio_separator_type") as runtime,
                    self.assertRaisesRegex(InferenceParameterError, message),
                ):
                    worker_infer._prepare_separator(payload=payload, task_id="t", logger=mock.Mock(), progress_callback=None)
                device.assert_not_called()
                runtime.assert_not_called()

    def test_model_bound_errors_are_rejected_before_constructing_the_separator(self) -> None:
        defaults = self.resolution()["defaults"]
        for parameters, message in (
            ({**defaults, "chunk_size": 0, "overlap_size": 0}, "chunk_size"),
            ({**defaults, "overlap_size": 882000}, "0 <= overlap_size < chunk_size"),
            ({**defaults, "overlap_size": -1}, "overlap_size"),
            ({**defaults, "batch_size": 0}, "batch_size"),
        ):
            with self.subTest(parameters=parameters):
                self.assert_rejected(self.payload(parameters), message)

    def test_changed_config_after_submission_is_a_parameter_error(self) -> None:
        payload = self.payload()
        self.config_path.write_text(
            yaml.safe_dump({"audio": {"chunk_size": 588800}, "inference": {"batch_size": 1}}), encoding="utf-8",
        )
        self.assert_rejected(payload, "fingerprint mismatch")

    def test_fixed_and_lattice_capabilities_are_enforced_without_rounding(self) -> None:
        self.make_files({
            "training": {"samplerate": 44100, "segment": 11},
            "audio": {"chunk_size": 485100}, "inference": {"batch_size": 1},
        }, "htdemucs")
        self.assert_rejected(
            self.payload({**self.resolution()["defaults"], "chunk_size": 441000, "overlap_size": 0}), "fixed",
        )

        self.make_files({
            "audio": {"hop_length": 1024, "n_fft": 8192, "chunk_size": 261120},
            "model": {"num_scales": 5, "scale": [2, 2]}, "inference": {"batch_size": 1},
        }, "mdx23c")
        defaults = self.resolution()["defaults"]
        self.assert_rejected(self.payload({**defaults, "chunk_size": 465920, "overlap_size": 0}), r"31744 \+ k")
        kwargs = self.prepare(self.payload({**defaults, "chunk_size": 457728, "overlap_size": 0})).call_args.kwargs
        self.assertEqual(kwargs["inference_params"]["chunk_size"], 457728)
        self.assertEqual(kwargs["inference_params"]["overlap_size"], 0)

    def test_vr_zero_strength_and_threshold_are_valid(self) -> None:
        self.make_files(None, "vr")
        parameters = {
            **self.resolution()["defaults"], "aggression": 0, "post_process_threshold": 0, "enable_post_process": True,
        }
        kwargs = self.prepare(self.payload(parameters)).call_args.kwargs
        self.assertEqual(kwargs["inference_params"], parameters)
        self.assert_rejected(self.payload({**parameters, "aggression": -1}), "minimum")
        self.assert_rejected(self.payload({**parameters, "chunk_size": 441000}), "chunk_size")

    def test_weights_fetched_by_the_task_itself_match_the_pre_download_fingerprint(self) -> None:
        from worker_inference_constraints import InferenceParameterError
        from worker_inference_snapshot import compute_inference_fingerprint

        snapshot = self.payload()["resolvedInference"]
        snapshot["fingerprint"] = compute_inference_fingerprint(self.model_type, self.config_path, None)
        kwargs = dict(model_type=self.model_type, config_path=str(self.config_path), model_path=str(self.weights))
        with self.assertRaisesRegex(InferenceParameterError, "fingerprint mismatch"):
            worker_infer._snapshot_runtime_params(snapshot, **kwargs)
        self.assertEqual(
            worker_infer._snapshot_runtime_params(snapshot, downloaded_now=True, **kwargs),
            snapshot["parameters"],
        )

    def test_unreadable_config_keeps_its_root_cause(self) -> None:
        from worker_inference_constraints import InferenceParameterError

        payload = self.payload()
        self.config_path.write_text("audio: [unclosed", encoding="utf-8")
        with self.assertRaises(InferenceParameterError) as caught:
            self.prepare(payload)
        self.assertIsNotNone(caught.exception.__cause__)


if __name__ == "__main__":
    unittest.main()
