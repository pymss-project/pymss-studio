import io
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

if __package__:
    from . import _bootstrap as _worker_test_bootstrap
else:
    import _bootstrap as _worker_test_bootstrap

import worker_custom_models as ccm

# mock.patch.dict needs the mapping object itself; the module reads os.environ directly.
os_environ_target = ccm.os.environ


def link_directory(link: Path, target: Path) -> None:
    if ccm.os.name == 'nt':
        subprocess.run(['cmd.exe', '/c', 'mklink', '/J', str(link), str(target)], check=True, capture_output=True)
    else:
        link.symlink_to(target, target_is_directory=True)


class FakeRegisteredEntry:
    """Stand-in for what pymss's register_model returns.

    Not a Mock: `name` is a reserved constructor argument on Mock, so `entry.name` would come
    back as a Mock and fail to serialise into the emitted event."""

    def __init__(self, name, model_type, model_path, config_path=None, aliases=(), inference_params=None):
        self.name = name
        self.model_type = model_type
        self.model_path = model_path
        self.config_path = config_path
        self.aliases = tuple(aliases)
        self.inference_params = inference_params

# The registrable architectures, as pymss defines them. Mirrored here so the detection tests do
# not depend on pymss being importable by whatever interpreter runs the suite.
KNOWN_TYPES = sorted({
    "apollo", "bandit", "bandit_v2", "bs_conformer", "bs_roformer", "bs_roformer_hyperace",
    "demucs", "htdemucs", "legacy_demucs", "legacy_tasnet", "mdx23c", "mel_band_conformer",
    "mel_band_roformer", "scnet", "tasnet", "vr",
})
CONFIG_OPTIONAL_TYPES = sorted({"vr", "demucs", "tasnet", "legacy_demucs", "legacy_tasnet"})


def types_of(suggestions):
    return [item["modelType"] for item in suggestions]


class ConfigDetectionTests(unittest.TestCase):
    """Each marker is a constructor argument unique to one architecture in
    pymss_core.utils.get_model_from_config, so one hit is conclusive."""

    def test_freqs_per_bands_identifies_bs_roformer(self):
        found = ccm.detect_from_config({"model": {"dim": 384, "freqs_per_bands": [2, 4]}})
        self.assertEqual(types_of(found), ["bs_roformer"])
        self.assertEqual(found[0]["confidence"], "high")
        self.assertEqual(found[0]["basisCode"], "config_model_key")
        self.assertEqual(found[0]["basisDetail"], "freqs_per_bands")

    def test_num_bands_identifies_mel_band_roformer(self):
        self.assertEqual(types_of(ccm.detect_from_config({"model": {"num_bands": 60}})), ["mel_band_roformer"])

    def test_the_two_roformer_families_are_never_confused(self):
        # Together these are 74% of the shipped catalog, so this is the distinction that matters.
        bs = ccm.detect_from_config({"model": {"freqs_per_bands": [2]}})
        mel = ccm.detect_from_config({"model": {"num_bands": 60}})
        self.assertEqual(types_of(bs), ["bs_roformer"])
        self.assertEqual(types_of(mel), ["mel_band_roformer"])

    def test_band_sr_identifies_scnet(self):
        self.assertEqual(types_of(ccm.detect_from_config({"model": {"band_SR": [0.175]}})), ["scnet"])

    def test_bottleneck_factor_identifies_mdx23c(self):
        self.assertEqual(types_of(ccm.detect_from_config({"model": {"bottleneck_factor": 4}})), ["mdx23c"])

    def test_band_specs_identifies_bandit(self):
        self.assertEqual(types_of(ccm.detect_from_config({"model": {"band_specs": "musical"}})), ["bandit"])

    def test_feature_dim_identifies_apollo(self):
        self.assertEqual(types_of(ccm.detect_from_config({"model": {"feature_dim": 256}})), ["apollo"])

    def test_a_kwargs_section_identifies_bandit_v2(self):
        # bandit_v2 is the only architecture constructed from config.kwargs.
        found = ccm.detect_from_config({"kwargs": {"in_channels": 2}, "model": {"num_bands": 60}})
        self.assertEqual(types_of(found), ["bandit_v2"])
        self.assertEqual(found[0]["basisCode"], "config_kwargs_section")

    def test_an_unrecognisable_config_yields_no_suggestion(self):
        # Better to say nothing than to guess: the user still picks from the full list.
        self.assertEqual(ccm.detect_from_config({"model": {"channels": 48, "depth": 6}}), [])

    def test_malformed_configs_are_tolerated(self):
        for value in (None, [], "text", {}, {"model": "not-a-dict"}, {"model": None}):
            self.assertEqual(ccm.detect_from_config(value), [])


class StateDictDetectionTests(unittest.TestCase):
    def test_segm_keys_identify_the_hyperace_variant(self):
        # pymss's own _runtime_model_type() promotes on exactly this marker, so it is authoritative.
        found = ccm.detect_from_state_dict_keys(["net.0.segm.weight", "net.1.bias"])
        self.assertEqual(types_of(found), ["bs_roformer_hyperace"])
        self.assertEqual(found[0]["basisCode"], "state_dict_key")

    def test_vr_networks_are_identified_by_their_stage_module(self):
        # Both VR generations name this attribute, and VR takes no YAML config at all.
        found = ccm.detect_from_state_dict_keys(["stg1_low_band_net.enc1.conv.weight"])
        self.assertEqual(types_of(found), ["vr"])

    def test_ordinary_keys_yield_no_suggestion(self):
        self.assertEqual(ccm.detect_from_state_dict_keys(["net.0.weight", "mask_estimator.bias"]), [])

    def test_absent_or_empty_keys_are_tolerated(self):
        for value in (None, (), []):
            self.assertEqual(ccm.detect_from_state_dict_keys(value), [])


class SuggestionMergeTests(unittest.TestCase):
    def test_config_findings_outrank_state_dict_findings(self):
        # The config is the architecture pymss will construct, so keep it ahead of refinements
        # inferred from tensor names.
        merged = ccm.merge_suggestions(
            ccm.detect_from_config({"model": {"freqs_per_bands": [2]}}),
            ccm.detect_from_state_dict_keys(["a.segm.weight"]),
        )
        self.assertEqual(types_of(merged), ["bs_roformer", "bs_roformer_hyperace"])

    def test_the_family_survives_as_a_second_choice(self):
        # Registering the family still works — pymss promotes at load time — so it must remain
        # selectable rather than being replaced outright.
        merged = ccm.merge_suggestions(
            ccm.detect_from_config({"model": {"freqs_per_bands": [2]}}),
            ccm.detect_from_state_dict_keys(["a.segm.weight"]),
        )
        self.assertIn("bs_roformer", types_of(merged))

    def test_duplicates_collapse_to_the_highest_ranked_occurrence(self):
        state = [ccm._suggestion("vr", "high", "state_dict_key", "stg1_low_band_net")]
        config = [ccm._suggestion("vr", "high", "config_model_key", "whatever")]
        merged = ccm.merge_suggestions(config, state)
        self.assertEqual(len(merged), 1)
        self.assertEqual(merged[0]["basisCode"], "config_model_key")

    def test_no_signal_yields_no_suggestion(self):
        self.assertEqual(ccm.merge_suggestions([], []), [])


class NameSuggestionTests(unittest.TestCase):
    def test_whitespace_is_collapsed_because_pymss_rejects_it(self):
        # user_models._validate_name() refuses any whitespace, so a raw filename stem would hand
        # the user a value that cannot be submitted.
        self.assertEqual(ccm.suggested_model_name(Path("/x/My Great Model.ckpt")), "My_Great_Model")

    def test_an_ordinary_stem_is_kept(self):
        self.assertEqual(ccm.suggested_model_name(Path("/x/bs_roformer_voc.ckpt")), "bs_roformer_voc")

    def test_every_filename_yields_a_registrable_suggestion(self):
        # The value is prefilled into an editable field, so the exact string matters less than it
        # always satisfying what pymss will accept: non-empty, no whitespace, no leading dot.
        for filename in (".ckpt", "  spaced  .ckpt", "...pth", "模型 A.th", "x.ckpt"):
            with self.subTest(filename=filename):
                name = ccm.suggested_model_name(Path("/x") / filename)
                self.assertTrue(name)
                self.assertFalse(any(ch.isspace() for ch in name))
                self.assertFalse(name.startswith("."))


class ConfigStemTests(unittest.TestCase):
    def test_instruments_and_target_are_read(self):
        instruments, target = ccm.config_stems({
            "training": {"instruments": ["vocals", "other"], "target_instrument": "vocals"},
        })
        self.assertEqual(instruments, ["vocals", "other"])
        self.assertEqual(target, "vocals")

    def test_a_single_string_instrument_is_accepted(self):
        instruments, _ = ccm.config_stems({"training": {"instruments": "vocals"}})
        self.assertEqual(instruments, ["vocals"])

    def test_a_config_without_training_yields_nothing(self):
        self.assertEqual(ccm.config_stems({}), ([], ""))
        self.assertEqual(ccm.config_stems({"training": None}), ([], ""))

    def test_blank_instrument_names_are_dropped(self):
        instruments, _ = ccm.config_stems({"training": {"instruments": ["vocals", "  ", ""]}})
        self.assertEqual(instruments, ["vocals"])


class InspectCommandTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.root, True)
        self.weights = self.root / "my model.ckpt"
        self.weights.write_bytes(b"x" * 4096)
        self.config = self.root / "my_model.yaml"
        self.config.write_text(
            "model:\n  num_bands: 60\ntraining:\n  instruments: [vocals, other]\n"
            "  target_instrument: vocals\n",
            encoding="utf-8",
        )
        self.stdout = io.StringIO()

    def _run(self, payload, state_dict_keys=(), state_dict_error=None):
        with mock.patch.object(ccm, "_known_model_types", return_value=(KNOWN_TYPES, CONFIG_OPTIONAL_TYPES)), \
             mock.patch.object(ccm, "_read_state_dict_keys", return_value=(list(state_dict_keys), state_dict_error)), \
             redirect_stdout(self.stdout):
            code = ccm.cmd_inspect_custom_model(payload)
        events = [json.loads(line) for line in self.stdout.getvalue().strip().splitlines() if line.strip()]
        return code, events[-1] if events else None

    def test_a_config_backed_inspection_reports_everything_the_form_needs(self):
        code, event = self._run({"modelPath": str(self.weights), "configPath": str(self.config)})
        self.assertEqual(code, 0)
        self.assertEqual(event["type"], "custom_model_inspected")
        payload = event["payload"]
        self.assertEqual(payload["suggestedModelType"], "mel_band_roformer")
        self.assertEqual(payload["instruments"], ["vocals", "other"])
        self.assertEqual(payload["targetInstrument"], "vocals")
        self.assertEqual(payload["sizeBytes"], 4096)
        # The filename had a space in it; the suggested name must be registrable.
        self.assertEqual(payload["suggestedName"], "my_model")
        self.assertIs(payload["configRequired"], True)

    def test_config_suggestion_beats_weight_marker_when_both_exist(self):
        code, event = self._run(
            {"modelPath": str(self.weights), "configPath": str(self.config)},
            state_dict_keys=["stg1_low_band_net.enc1.weight"],
        )
        self.assertEqual(code, 0)
        self.assertEqual(event["payload"]["suggestedModelType"], "mel_band_roformer")

    def test_a_config_optional_suggestion_says_so(self):
        code, event = self._run(
            {"modelPath": str(self.weights)},
            state_dict_keys=["stg1_low_band_net.enc1.weight"],
        )
        self.assertEqual(code, 0)
        self.assertEqual(event["payload"]["suggestedModelType"], "vr")
        self.assertIs(event["payload"]["configRequired"], False)

    def test_unreadable_weights_do_not_fail_the_inspection(self):
        # Detection degrades to the config; the real load during import is the arbiter anyway.
        code, event = self._run(
            {"modelPath": str(self.weights), "configPath": str(self.config)},
            state_dict_error="RuntimeError: bad magic",
        )
        self.assertEqual(code, 0)
        self.assertIs(event["payload"]["stateDictReadable"], False)
        self.assertIn("bad magic", event["payload"]["stateDictError"])
        self.assertEqual(event["payload"]["suggestedModelType"], "mel_band_roformer")

    def test_no_signal_reports_no_suggestion_rather_than_a_guess(self):
        plain = self.root / "plain.yaml"
        plain.write_text("model:\n  channels: 48\n", encoding="utf-8")
        code, event = self._run({"modelPath": str(self.weights), "configPath": str(plain)})
        self.assertEqual(code, 0)
        self.assertEqual(event["payload"]["suggestions"], [])
        self.assertIsNone(event["payload"]["suggestedModelType"])
        # Null, not a bogus boolean: with no suggestion there is nothing to require a config for.
        self.assertIsNone(event["payload"]["configRequired"])
        # The full list is still offered so the user can pick unaided.
        self.assertEqual(event["payload"]["knownModelTypes"], KNOWN_TYPES)

    def test_a_type_this_pymss_build_rejects_is_never_suggested(self):
        with mock.patch.object(ccm, "_known_model_types", return_value=(["bs_roformer"], [])), \
             mock.patch.object(ccm, "_read_state_dict_keys", return_value=([], None)), \
             redirect_stdout(self.stdout):
            ccm.cmd_inspect_custom_model({"modelPath": str(self.weights), "configPath": str(self.config)})
        payload = json.loads(self.stdout.getvalue().strip().splitlines()[-1])["payload"]
        self.assertEqual(payload["suggestions"], [])

    def test_a_missing_weight_file_is_an_error(self):
        code, event = self._run({"modelPath": str(self.root / "absent.ckpt")})
        self.assertEqual(code, 1)
        self.assertEqual(event["payload"]["code"], "CUSTOM_MODEL_FILE_MISSING")

    def test_a_missing_config_file_is_an_error(self):
        code, event = self._run({"modelPath": str(self.weights), "configPath": str(self.root / "absent.yaml")})
        self.assertEqual(code, 1)
        self.assertEqual(event["payload"]["code"], "CUSTOM_MODEL_FILE_MISSING")

    def test_an_empty_payload_is_an_error(self):
        code, event = self._run({})
        self.assertEqual(code, 1)
        self.assertEqual(event["payload"]["code"], "CUSTOM_MODEL_INVALID")

    def test_safetensors_is_not_offered_as_an_accepted_format(self):
        # pymss loads weights with torch.load and has no safetensors path; accepting one would
        # only fail later with a confusing error.
        self.assertNotIn(".safetensors", ccm.MODEL_FILE_EXTENSIONS)


class ImportPipelineTests(unittest.TestCase):
    """Copy → verify → register. A model that cannot load must never reach the registry, where
    it would sit in the model list and only fail at separation time."""

    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.root, True)
        self.weights = self.root / "src" / "my_model.ckpt"
        self.weights.parent.mkdir()
        self.weights.write_bytes(b"w" * (9 * 1024 * 1024))  # spans several copy blocks
        self.config = self.root / "src" / "my_model.yaml"
        self.config.write_text("model:\n  num_bands: 60\n", encoding="utf-8")
        self.models_dir = self.root / "models"
        self.models_dir.mkdir()
        self.sidecar = self.root / "custom-models-meta.json"
        self.stdout = io.StringIO()
        self.registered: list[dict] = []
        self.unregistered: list[str] = []

    def _registry(self, register_error=None):
        def register_model(name, model_type, model_path, **kwargs):
            if register_error:
                raise register_error
            self.registered.append({"name": name, "model_type": model_type, "model_path": model_path, **kwargs})
            return FakeRegisteredEntry(name, model_type, model_path, kwargs.get("config_path"))

        def unregister_model(name):
            self.unregistered.append(name)
            return FakeRegisteredEntry(name, "mel_band_roformer", "")

        return mock.Mock(register_model=register_model, unregister_model=unregister_model)

    def _run(self, payload, register_error=None, verify_error=None):
        verify = mock.Mock(side_effect=verify_error) if verify_error else mock.Mock()
        # Patched by the name the import pipeline actually calls.
        env = {"PYMSS_MODEL_DIR": str(self.models_dir), "PYMSS_STUDIO_CUSTOM_MODEL_META": str(self.sidecar)}
        with mock.patch.dict(os_environ_target, env, clear=False), \
             mock.patch.dict("sys.modules", {
                 "pymss": mock.Mock(),
                 "pymss.model_registry": self._registry(register_error),
             }), \
             mock.patch.object(ccm, "_verify_model_loads", verify), \
             redirect_stdout(self.stdout):
            code = ccm.cmd_import_custom_model(payload)
        events = [json.loads(line) for line in self.stdout.getvalue().strip().splitlines() if line.strip()]
        return code, events, verify

    def _base_payload(self, **overrides):
        return {
            "name": "my_model",
            "modelType": "mel_band_roformer",
            "modelPath": str(self.weights),
            "configPath": str(self.config),
            **overrides,
        }

    def test_reference_import_registers_the_original_paths(self):
        code, events, _ = self._run(self._base_payload())
        self.assertEqual(code, 0)
        self.assertEqual(self.registered[0]["model_path"], str(self.weights))
        self.assertEqual(self.registered[0]["config_path"], str(self.config))
        # Nothing was copied into the model directory.
        self.assertEqual(list(self.models_dir.iterdir()), [])
        self.assertEqual(events[-1]["type"], "custom_model_import_finished")

    def test_copy_import_places_files_under_app_management(self):
        code, events, _ = self._run(self._base_payload(importMode="copy"))
        self.assertEqual(code, 0)
        managed = (self.models_dir / "custom" / "my_model").resolve()
        self.assertTrue((managed / "my_model.ckpt").is_file())
        self.assertTrue((managed / "my_model.yaml").is_file())
        self.assertEqual(self.registered[0]["model_path"], str(managed / "my_model.ckpt"))
        # The original is left untouched.
        self.assertTrue(self.weights.is_file())
        self.assertEqual(events[-1]["payload"]["importMode"], "copy")

    def test_copy_reports_progress_so_a_large_file_does_not_look_frozen(self):
        _, events, _ = self._run(self._base_payload(importMode="copy"))
        weights_progress = [
            e["payload"] for e in events
            if e["type"] == "custom_model_import_progress"
            and e["payload"].get("stage") == "copying"
            and e["payload"].get("file") == self.weights.name
        ]
        # Several updates, not just a single jump to 100 — the point is a moving bar.
        self.assertGreater(len(weights_progress), 1)
        self.assertEqual(weights_progress[-1]["progress"], 100)
        self.assertEqual(weights_progress[-1]["totalBytes"], self.weights.stat().st_size)
        # Each file is reported against its own size, so the bar never exceeds itself.
        self.assertTrue(all(p["copiedBytes"] <= p["totalBytes"] for p in weights_progress))

    def test_no_partial_file_survives_a_failed_copy(self):
        # A half-written checkpoint would later load as corrupt, so it must not be left behind.
        with mock.patch.dict(os_environ_target, {"PYMSS_MODEL_DIR": str(self.models_dir)}, clear=False), \
             mock.patch.dict("sys.modules", {"pymss": mock.Mock(), "pymss.model_registry": self._registry()}), \
             mock.patch.object(Path, "replace", side_effect=OSError("disk full")), \
             redirect_stdout(self.stdout):
            code = ccm.cmd_import_custom_model(self._base_payload(importMode="copy"))
        self.assertEqual(code, 1)
        self.assertFalse((self.models_dir / "custom" / "my_model").exists())

    def test_verification_runs_by_default(self):
        _, events, verify = self._run(self._base_payload())
        verify.assert_called_once()
        self.assertIs(events[-1]["payload"]["verified"], True)

    def test_verification_can_be_skipped(self):
        _, events, verify = self._run(self._base_payload(verify=False))
        verify.assert_not_called()
        self.assertIs(events[-1]["payload"]["verified"], False)

    def test_a_failed_verification_never_reaches_the_registry(self):
        code, events, _ = self._run(
            self._base_payload(),
            verify_error=RuntimeError("size mismatch for layer.0"),
        )
        self.assertEqual(code, 1)
        self.assertEqual(self.registered, [], "a model that cannot load must not be registered")
        payload = events[-1]["payload"]
        self.assertEqual(payload["code"], "CUSTOM_MODEL_VERIFY_FAILED")
        self.assertIn("size mismatch", payload["message"])

    def test_a_failed_reimport_leaves_the_existing_registration_intact(self):
        # Regression guard: verifying after registering meant a force re-import with the wrong
        # architecture would unregister on failure, destroying a model that worked until then.
        code, _, _ = self._run(
            self._base_payload(force=True),
            verify_error=RuntimeError("bad architecture"),
        )
        self.assertEqual(code, 1)
        self.assertEqual(self.unregistered, [], "the previous registration must be left alone")
        self.assertEqual(self.registered, [])

    def test_a_failed_copy_reimport_leaves_existing_files_intact(self):
        managed = self.models_dir / "custom" / "my_model"
        managed.mkdir(parents=True)
        old_weights = managed / "my_model.ckpt"
        old_weights.write_bytes(b"old")

        code, _, _ = self._run(
            self._base_payload(importMode="copy", force=True),
            verify_error=RuntimeError("bad architecture"),
        )

        self.assertEqual(code, 1)
        self.assertEqual(old_weights.read_bytes(), b"old")
        self.assertEqual(self.registered, [])

    def test_a_failed_copy_registration_restores_existing_files(self):
        managed = self.models_dir / "custom" / "my_model"
        managed.mkdir(parents=True)
        old_weights = managed / "my_model.ckpt"
        old_weights.write_bytes(b"old")

        code, events, _ = self._run(
            self._base_payload(importMode="copy", force=True),
            register_error=ValueError("name conflict"),
        )

        self.assertEqual(code, 1)
        self.assertEqual(events[-1]["payload"]["code"], "CUSTOM_MODEL_IMPORT_FAILED")
        self.assertEqual(old_weights.read_bytes(), b"old")

    def test_failed_registration_keeps_backup_when_new_files_cannot_be_removed(self):
        managed = (self.models_dir / "custom" / "my_model").resolve()
        managed.mkdir(parents=True)
        (managed / "my_model.ckpt").write_bytes(b"old")
        original_rmtree = shutil.rmtree

        def remove_tree(path, *args, **kwargs):
            if Path(path) == managed:
                raise PermissionError("model file is in use")
            return original_rmtree(path, *args, **kwargs)

        with mock.patch.object(ccm.shutil, "rmtree", side_effect=remove_tree):
            code, events, _ = self._run(
                self._base_payload(importMode="copy", force=True),
                register_error=ValueError("name conflict"),
            )

        self.assertEqual(code, 1)
        backups = list(managed.parent.glob(".my_model.backup-*"))
        self.assertEqual(len(backups), 1)
        self.assertEqual((backups[0] / "my_model.ckpt").read_bytes(), b"old")
        message = events[-1]["payload"]["message"]
        self.assertIn(str(backups[0]), message)
        self.assertIn("name conflict", message)
        self.assertIn("model file is in use", message)

    def test_failed_registration_keeps_backup_when_restoring_it_fails(self):
        managed = (self.models_dir / "custom" / "my_model").resolve()
        managed.mkdir(parents=True)
        (managed / "my_model.ckpt").write_bytes(b"old")
        original_replace = Path.replace

        def replace(path, target):
            if path.name.startswith(".my_model.backup-"):
                raise PermissionError("restore denied")
            return original_replace(path, target)

        with mock.patch.object(Path, "replace", replace):
            code, events, _ = self._run(
                self._base_payload(importMode="copy", force=True),
                register_error=ValueError("name conflict"),
            )

        self.assertEqual(code, 1)
        backups = list(managed.parent.glob(".my_model.backup-*"))
        self.assertEqual(len(backups), 1)
        self.assertEqual((backups[0] / "my_model.ckpt").read_bytes(), b"old")
        self.assertIn(str(backups[0]), events[-1]["payload"]["message"])
        self.assertIn("restore denied", events[-1]["payload"]["message"])

    def test_verification_runs_before_registration(self):
        # Ordering is the whole guarantee; assert it rather than trusting the call sites.
        order = []
        verify = mock.Mock(side_effect=lambda *a, **k: order.append("verify"))

        def register_model(name, model_type, model_path, **kwargs):
            order.append("register")
            return FakeRegisteredEntry(name, model_type, model_path, kwargs.get("config_path"))

        with mock.patch.dict(os_environ_target, {"PYMSS_MODEL_DIR": str(self.models_dir)}, clear=False),              mock.patch.dict("sys.modules", {
                 "pymss": mock.Mock(),
                 "pymss.model_registry": mock.Mock(register_model=register_model),
             }),              mock.patch.object(ccm, "_verify_model_loads", verify),              redirect_stdout(self.stdout):
            ccm.cmd_import_custom_model(self._base_payload())
        self.assertEqual(order, ["verify", "register"])

    def test_a_failed_verification_also_discards_copied_files(self):
        code, _, _ = self._run(
            self._base_payload(importMode="copy"),
            verify_error=RuntimeError("bad architecture"),
        )
        self.assertEqual(code, 1)
        self.assertFalse((self.models_dir / "custom" / "my_model").exists())

    def test_a_name_conflict_from_pymss_is_surfaced(self):
        code, events, _ = self._run(
            self._base_payload(),
            register_error=ValueError("name/alias conflicts with built-in catalog: htdemucs"),
        )
        self.assertEqual(code, 1)
        self.assertEqual(events[-1]["payload"]["code"], "CUSTOM_MODEL_IMPORT_FAILED")
        self.assertIn("conflicts with built-in catalog", events[-1]["payload"]["message"])

    def test_a_name_with_whitespace_is_rejected_before_any_work(self):
        # pymss would reject it anyway; failing early avoids copying gigabytes first.
        code, events, _ = self._run(self._base_payload(name="my model", importMode="copy"))
        self.assertEqual(code, 1)
        self.assertEqual(events[-1]["payload"]["code"], "CUSTOM_MODEL_INVALID")
        self.assertEqual(list(self.models_dir.iterdir()), [])

    def test_a_missing_weight_file_is_rejected(self):
        code, events, _ = self._run(self._base_payload(modelPath=str(self.root / "absent.ckpt")))
        self.assertEqual(code, 1)
        self.assertEqual(events[-1]["payload"]["code"], "CUSTOM_MODEL_FILE_MISSING")

    def test_paths_and_reserved_names_are_rejected_without_changing_existing_files(self):
        sibling = self.models_dir / 'sibling'
        sibling.mkdir()
        marker = sibling / 'keep.bin'
        marker.write_bytes(b'keep')
        for name in ['.', '..', '../sibling', str(self.root / 'outside'), r'C:\outside', r'a\b', 'a/b', 'a:b', 'a.', 'CON', 'con.ckpt', 'LPT9', 'a\x00b']:
            with self.subTest(name=name):
                code, events, verify = self._run(self._base_payload(name=name, importMode='copy'))
                self.assertEqual(code, 1)
                self.assertEqual(events[-1]['payload']['code'], 'CUSTOM_MODEL_INVALID')
                verify.assert_not_called()
                self.assertEqual(marker.read_bytes(), b'keep')
                self.assertFalse((self.models_dir / 'custom').exists())
        self.assertEqual(self.registered, [])

    def test_managed_directory_link_is_rejected_before_copy(self):
        target = self.models_dir / 'custom' / 'my_model'
        target.parent.mkdir()
        outside = self.root / 'outside'
        outside.mkdir()
        marker = outside / 'keep.bin'
        marker.write_bytes(b'keep')
        try:
            link_directory(target, outside)
        except OSError:
            self.skipTest('Directory symbolic links are unavailable')
        code, _, verify = self._run(self._base_payload(importMode='copy'))
        self.assertEqual(code, 1)
        verify.assert_not_called()
        self.assertEqual(list(outside.iterdir()), [marker])

    def test_custom_root_link_is_rejected_before_copy(self):
        outside = self.root / 'outside'
        outside.mkdir()
        marker = outside / 'keep.bin'
        marker.write_bytes(b'keep')
        link_directory(self.models_dir / 'custom', outside)
        code, _, verify = self._run(self._base_payload(importMode='copy'))
        self.assertEqual(code, 1)
        verify.assert_not_called()
        self.assertEqual(list(outside.iterdir()), [marker])

    def test_the_import_mode_is_remembered_for_later_removal(self):
        self._run(self._base_payload(importMode="copy"))
        with mock.patch.dict(os_environ_target, {"PYMSS_STUDIO_CUSTOM_MODEL_META": str(self.sidecar)}, clear=False):
            meta = ccm.sidecar_entry("my_model")
        self.assertEqual(meta["importMode"], "copy")
        self.assertTrue(meta["managedDir"].endswith("my_model"))


class UnregisterTests(unittest.TestCase):
    """Removal must never delete a file the app did not put there."""

    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.root, True)
        self.sidecar = self.root / "meta.json"
        self.stdout = io.StringIO()
        self.unregistered: list[str] = []

    def _sidecar(self, models):
        self.sidecar.write_text(json.dumps({"version": 1, "models": models}), encoding="utf-8")

    def _run(self, payload, model_path=None):
        def unregister_model(name):
            self.unregistered.append(name)
            directory = Path(payload.get("modelDir") or self.root)
            weights = model_path or directory / "custom" / name / "w.ckpt"
            return FakeRegisteredEntry(name, "mel_band_roformer", str(weights))

        with mock.patch.dict(os_environ_target, {"PYMSS_STUDIO_CUSTOM_MODEL_META": str(self.sidecar), "PYMSS_MODEL_DIR": str(self.root)}, clear=False), \
             mock.patch.dict("sys.modules", {
                 "pymss": mock.Mock(),
                 "pymss.model_registry": mock.Mock(unregister_model=unregister_model),
             }), \
             redirect_stdout(self.stdout):
            code = ccm.cmd_unregister_custom_model(payload)
        events = [json.loads(line) for line in self.stdout.getvalue().strip().splitlines() if line.strip()]
        return code, events[-1]["payload"]

    def test_a_referenced_model_is_unregistered_without_touching_its_file(self):
        weights = self.root / "elsewhere.ckpt"
        weights.write_bytes(b"keep")
        self._sidecar({"my_model": {"importMode": "reference", "managedDir": None}})
        code, payload = self._run({"name": "my_model", "deleteFiles": True})
        self.assertEqual(code, 0)
        self.assertTrue(weights.is_file(), "a referenced model's own file must never be deleted")
        self.assertIs(payload["deletedFiles"], False)
        self.assertIs(payload["fileDeletionSupported"], False)
        self.assertEqual(self.unregistered, ["my_model"])

    def test_a_copied_model_can_have_its_managed_files_deleted(self):
        managed = self.root / "custom" / "my_model"
        managed.mkdir(parents=True)
        (managed / "my_model.ckpt").write_bytes(b"copy")
        self._sidecar({"my_model": {"importMode": "copy", "managedDir": str(managed)}})
        code, payload = self._run({"name": "my_model", "deleteFiles": True})
        self.assertEqual(code, 0)
        self.assertFalse(managed.exists())
        self.assertIs(payload["deletedFiles"], True)

    def test_a_copied_model_keeps_its_files_when_deletion_was_not_requested(self):
        managed = self.root / "custom" / "my_model"
        managed.mkdir(parents=True)
        (managed / "w.ckpt").write_bytes(b"copy")
        self._sidecar({"my_model": {"importMode": "copy", "managedDir": str(managed)}})
        code, payload = self._run({"name": "my_model"})
        self.assertEqual(code, 0)
        self.assertTrue(managed.exists())
        self.assertIs(payload["deletedFiles"], False)
        # Still reported as eligible, so the UI can offer the choice next time.
        self.assertIs(payload["fileDeletionSupported"], True)

    def test_stale_ownership_cannot_delete_a_folder_the_model_no_longer_uses(self):
        old_managed = self.root / "custom" / "my_model"
        old_managed.mkdir(parents=True)
        file_to_keep = old_managed / "keep.ckpt"
        file_to_keep.write_bytes(b"unrelated")
        self._sidecar({"my_model": {"importMode": "copy", "managedDir": str(old_managed)}})
        code, payload = self._run(
            {"name": "my_model", "deleteFiles": True},
            model_path=self.root / "migrated" / "w.ckpt",
        )
        self.assertEqual(code, 0)
        self.assertFalse(payload["fileDeletionSupported"])
        self.assertEqual(file_to_keep.read_bytes(), b"unrelated")

    def test_a_model_registered_outside_the_app_is_treated_as_a_reference(self):
        # `pymss register` on the command line leaves no side-car; assuming 'copy' would risk
        # deleting a file the app never owned.
        self._sidecar({})
        weights = self.root / "cli.ckpt"
        weights.write_bytes(b"keep")
        code, payload = self._run({"name": "cli_model", "deleteFiles": True})
        self.assertEqual(code, 0)
        self.assertTrue(weights.is_file())
        self.assertIs(payload["fileDeletionSupported"], False)

    def test_an_unknown_model_reports_not_found(self):
        def unregister_model(name):
            raise KeyError(f"Unknown user model: {name}")

        with mock.patch.dict(os_environ_target, {"PYMSS_STUDIO_CUSTOM_MODEL_META": str(self.sidecar)}, clear=False), \
             mock.patch.dict("sys.modules", {
                 "pymss": mock.Mock(),
                 "pymss.model_registry": mock.Mock(unregister_model=unregister_model),
             }), \
             redirect_stdout(self.stdout):
            self.assertEqual(ccm.cmd_unregister_custom_model({"name": "ghost"}), 1)
        payload = json.loads(self.stdout.getvalue().strip().splitlines()[-1])["payload"]
        self.assertEqual(payload["code"], "CUSTOM_MODEL_NOT_FOUND")

    def test_a_missing_name_is_rejected(self):
        with redirect_stdout(self.stdout):
            self.assertEqual(ccm.cmd_unregister_custom_model({}), 1)
        self.assertEqual(
            json.loads(self.stdout.getvalue().strip().splitlines()[-1])["payload"]["code"],
            "CUSTOM_MODEL_INVALID",
        )

    def test_polluted_sidecar_cannot_delete_outside_the_current_managed_root(self):
        outside = self.root / 'outside'
        outside.mkdir()
        marker = outside / 'keep.bin'
        marker.write_bytes(b'keep')
        for name in ['my_model', '../outside', str(outside)]:
            with self.subTest(name=name):
                self._sidecar({name: {'importMode': 'copy', 'managedDir': str(outside)}})
                code, payload = self._run({'name': name, 'deleteFiles': True})
                self.assertEqual(code, 0)
                self.assertFalse(payload['deletedFiles'])
                self.assertFalse(payload['fileDeletionSupported'])
                self.assertEqual(marker.read_bytes(), b'keep')

    def test_deletion_uses_the_requested_model_directory(self):
        configured = self.root / 'configured'
        managed = configured / 'custom' / 'my_model'
        managed.mkdir(parents=True)
        (managed / 'weights.ckpt').write_bytes(b'copy')
        self._sidecar({'my_model': {'importMode': 'copy', 'managedDir': str(managed)}})
        code, payload = self._run({'name': 'my_model', 'modelDir': str(configured), 'deleteFiles': True})
        self.assertEqual(code, 0)
        self.assertTrue(payload['deletedFiles'])
        self.assertFalse(managed.exists())

    def test_linked_managed_directory_is_never_deleted(self):
        outside = self.root / 'outside'
        outside.mkdir()
        marker = outside / 'keep.bin'
        marker.write_bytes(b'keep')
        linked = self.root / 'custom' / 'my_model'
        linked.parent.mkdir()
        try:
            link_directory(linked, outside)
        except OSError:
            self.skipTest('Directory symbolic links are unavailable')
        self._sidecar({'my_model': {'importMode': 'copy', 'managedDir': str(linked)}})
        _, payload = self._run({'name': 'my_model', 'deleteFiles': True})
        self.assertFalse(payload['deletedFiles'])
        self.assertTrue(marker.exists())


class RelinkTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.root, True)
        self.moved = self.root / "moved.ckpt"
        self.moved.write_bytes(b"w")
        self.stdout = io.StringIO()
        self.registered: list[dict] = []

    def _run(self, payload, existing=None):
        def register_model(name, model_type, model_path, **kwargs):
            self.registered.append({"name": name, "model_type": model_type, "model_path": model_path, **kwargs})
            return FakeRegisteredEntry(name, model_type, model_path, kwargs.get("config_path"))

        entry = existing if existing is not None else FakeRegisteredEntry(
            "my_model", "scnet", "/old/path.ckpt", aliases=("alias1",)
        )
        user_models = mock.Mock(get_user_model_entry=mock.Mock(return_value=entry))
        with mock.patch.dict("sys.modules", {
            "pymss": mock.Mock(),
            "pymss.user_models": user_models,
            "pymss.model_registry": mock.Mock(register_model=register_model),
        }), redirect_stdout(self.stdout):
            code = ccm.cmd_relink_custom_model(payload)
        events = [json.loads(line) for line in self.stdout.getvalue().strip().splitlines() if line.strip()]
        return code, events[-1]

    def test_relinking_keeps_the_existing_type_and_aliases(self):
        code, event = self._run({"name": "my_model", "modelPath": str(self.moved)})
        self.assertEqual(code, 0)
        self.assertEqual(event["type"], "custom_model_relinked")
        self.assertEqual(self.registered[0]["model_type"], "scnet")
        self.assertEqual(self.registered[0]["aliases"], ["alias1"])
        # Must overwrite the existing registration rather than colliding with it.
        self.assertIs(self.registered[0]["force"], True)

    def test_the_type_can_be_overridden(self):
        self._run({"name": "my_model", "modelPath": str(self.moved), "modelType": "bs_roformer"})
        self.assertEqual(self.registered[0]["model_type"], "bs_roformer")

    def test_relinking_to_a_missing_file_is_rejected(self):
        code, event = self._run({"name": "my_model", "modelPath": str(self.root / "nope.ckpt")})
        self.assertEqual(code, 1)
        self.assertEqual(event["payload"]["code"], "CUSTOM_MODEL_FILE_MISSING")

    def test_relinking_an_unregistered_model_reports_not_found(self):
        user_models = mock.Mock(get_user_model_entry=mock.Mock(side_effect=KeyError("Unknown user model: ghost")))
        with mock.patch.dict("sys.modules", {
            "pymss": mock.Mock(), "pymss.user_models": user_models,
            "pymss.model_registry": mock.Mock(),
        }), redirect_stdout(self.stdout):
            self.assertEqual(ccm.cmd_relink_custom_model({"name": "ghost", "modelPath": str(self.moved)}), 1)
        payload = json.loads(self.stdout.getvalue().strip().splitlines()[-1])["payload"]
        self.assertEqual(payload["code"], "CUSTOM_MODEL_NOT_FOUND")


class ManagedRootTests(unittest.TestCase):
    """Copies must land beside the catalog models, wherever the user put those."""

    def test_the_configured_model_directory_wins_over_the_environment(self):
        # PYMSS_MODEL_DIR always holds the default location; the directory the user actually
        # configured arrives per command, the same way list_models receives it.
        with mock.patch.dict(os_environ_target, {"PYMSS_MODEL_DIR": r"/default/models"}, clear=False):
            self.assertEqual(ccm.managed_root("/configured/models"), Path("/configured/models/custom"))

    def test_the_environment_is_the_fallback(self):
        with mock.patch.dict(os_environ_target, {"PYMSS_MODEL_DIR": r"/default/models"}, clear=False):
            self.assertEqual(ccm.managed_root(None), Path("/default/models/custom"))


class PathRemapTests(unittest.TestCase):
    """After the model directory moves, registrations still hold absolute paths into the old one."""

    def test_a_path_under_the_old_root_is_rebased(self):
        moved = ccm._remapped_path(
            str(Path("/old/models/custom/m/w.ckpt")), str(Path("/old/models")), str(Path("/new/models")))
        self.assertEqual(Path(moved), Path("/new/models/custom/m/w.ckpt"))

    def test_a_path_outside_the_old_root_is_left_alone(self):
        self.assertIsNone(ccm._remapped_path(
            str(Path("/elsewhere/w.ckpt")), str(Path("/old/models")), str(Path("/new/models"))))

    def test_a_sibling_directory_is_not_treated_as_inside(self):
        # 'models-backup' starts with 'models' as a string but is not under it.
        self.assertIsNone(ccm._remapped_path(
            str(Path("/old/models-backup/w.ckpt")), str(Path("/old/models")), str(Path("/new/models"))))

    def test_the_root_itself_is_rebased(self):
        moved = ccm._remapped_path(str(Path("/old/models")), str(Path("/old/models")), str(Path("/new/models")))
        self.assertEqual(Path(moved), Path("/new/models"))


class RemapCommandTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp()).resolve()
        self.addCleanup(shutil.rmtree, self.root, True)
        self.old_root = self.root / "old-models"
        self.new_root = self.root / "new-models"
        self.sidecar = self.root / "meta.json"
        self.registry = self.root / "user_models.json"
        self.stdout = io.StringIO()
        self.registered: list[dict] = []

    def _entry(self, name, model_path, config_path=None):
        return {"name": name, "model_type": "mel_band_roformer", "model_path": str(model_path),
                "config_path": str(config_path) if config_path else None}

    def _run(self, payload, entries=None):
        if entries is not None:
            self.registry.write_text(json.dumps({"version": 1, "models": entries}), encoding="utf-8")
        with mock.patch.dict(os_environ_target, {
            "PYMSS_USER_MODELS": str(self.registry),
            "PYMSS_STUDIO_CUSTOM_MODEL_META": str(self.sidecar),
        }), \
             redirect_stdout(self.stdout):
            code = ccm.cmd_remap_custom_model_paths(payload)
        self.registered = ccm.read_custom_model_registry(self.registry)["models"]
        events = [json.loads(line) for line in self.stdout.getvalue().strip().splitlines() if line.strip()]
        return code, events[-1]["payload"]

    def _payload(self, *migrated_files):
        for relative in migrated_files:
            destination = self.new_root / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(b"migrated file")
        return {"fromRoot": str(self.old_root), "toRoot": str(self.new_root),
                "copiedFiles": list(migrated_files)}

    def test_a_copied_model_follows_the_model_directory(self):
        entry = self._entry("copied", self.old_root / "custom" / "copied" / "w.ckpt",
                            self.old_root / "custom" / "copied" / "w.yaml")
        entry["inference_params"] = {"normalize": True}
        entry["aliases"] = ["copied_alias"]
        entry["target_stem"] = "vocals"
        code, payload = self._run(self._payload("custom/copied/w.ckpt", "custom/copied/w.yaml"), [entry])
        self.assertEqual(code, 0)
        self.assertEqual(payload["count"], 1)
        self.assertEqual(Path(self.registered[0]["model_path"]),
                         self.new_root / "custom" / "copied" / "w.ckpt")
        self.assertEqual(Path(self.registered[0]["config_path"]),
                         self.new_root / "custom" / "copied" / "w.yaml")
        self.assertEqual(self.registered[0]["inference_params"], {"normalize": True})
        self.assertEqual(self.registered[0]["aliases"], ["copied_alias"])
        self.assertEqual(self.registered[0]["target_stem"], "vocals")

    def test_a_model_referenced_from_outside_is_untouched(self):
        entry = self._entry("outside", self.root / "downloads" / "w.ckpt")
        code, payload = self._run(self._payload("downloads/w.ckpt"), [entry])
        self.assertEqual(code, 0)
        self.assertEqual(payload["count"], 0)
        self.assertEqual(self.registered, [entry])

    def test_a_referenced_model_that_lived_inside_the_model_dir_is_remapped_too(self):
        entry = self._entry("inside", self.old_root / "loose" / "w.ckpt")
        code, payload = self._run(self._payload("loose/w.ckpt"), [entry])
        self.assertEqual(code, 0)
        self.assertEqual(payload["count"], 1)
        self.assertEqual(Path(self.registered[0]["model_path"]), self.new_root / "loose" / "w.ckpt")

    def test_a_skipped_weight_and_its_ownership_remain_at_the_source(self):
        source = self.old_root / "custom" / "skipped" / "w.ckpt"
        source.parent.mkdir(parents=True)
        source.write_bytes(b"registered model")
        self.sidecar.write_text(json.dumps({"version": 1, "models": {
            "skipped": {"importMode": "copy", "managedDir": str(source.parent)},
        }}), encoding="utf-8")
        request = self._payload("another.ckpt")
        destination = self.new_root / "custom" / "skipped" / "w.ckpt"
        destination.parent.mkdir(parents=True)
        destination.write_bytes(b"unrelated model")

        _, payload = self._run(request, [self._entry("skipped", source)])

        self.assertEqual(payload["count"], 0)
        self.assertEqual(Path(self.registered[0]["model_path"]), source)
        meta = json.loads(self.sidecar.read_text(encoding="utf-8"))["models"]["skipped"]
        self.assertEqual(Path(meta["managedDir"]), source.parent)
        self.assertEqual(destination.read_bytes(), b"unrelated model")

    def test_the_sidecar_managed_directory_follows_along(self):
        self.sidecar.write_text(json.dumps({"version": 1, "models": {
            "copied": {"importMode": "copy", "managedDir": str(self.old_root / "custom" / "copied")},
        }}), encoding="utf-8")
        entry = self._entry("copied", self.old_root / "custom" / "copied" / "w.ckpt")
        self._run(self._payload("custom/copied/w.ckpt"), [entry])
        with mock.patch.dict(os_environ_target, {"PYMSS_STUDIO_CUSTOM_MODEL_META": str(self.sidecar)}, clear=False):
            meta = ccm.sidecar_entry("copied")
        self.assertEqual(Path(meta["managedDir"]), self.new_root / "custom" / "copied")

    def test_a_retained_config_stays_in_place_when_its_weight_moves(self):
        source_dir = self.old_root / "custom" / "copied"
        config = source_dir / "w.yaml"
        source_dir.mkdir(parents=True)
        config.write_bytes(b"registered config")
        self.sidecar.write_text(json.dumps({"version": 1, "models": {
            "copied": {"importMode": "copy", "managedDir": str(source_dir)},
        }}), encoding="utf-8")
        request = self._payload("custom/copied/w.ckpt")
        destination = self.new_root / "custom" / "copied"
        (destination / "w.yaml").write_bytes(b"unrelated config")

        _, payload = self._run(request, [self._entry("copied", source_dir / "w.ckpt", config)])

        self.assertEqual(payload["count"], 1)
        self.assertEqual(Path(self.registered[0]["model_path"]), destination / "w.ckpt")
        self.assertEqual(Path(self.registered[0]["config_path"]), config)
        meta = json.loads(self.sidecar.read_text(encoding="utf-8"))["models"]["copied"]
        self.assertEqual(meta, {"importMode": "reference", "managedDir": None})

    def test_a_destination_with_other_files_is_never_marked_as_managed(self):
        source_dir = self.old_root / "custom" / "copied"
        self.sidecar.write_text(json.dumps({"version": 1, "models": {
            "copied": {"importMode": "copy", "managedDir": str(source_dir)},
        }}), encoding="utf-8")
        request = self._payload("custom/copied/w.ckpt")
        (self.new_root / "custom" / "copied" / "keep.dat").write_bytes(b"unrelated data")

        _, payload = self._run(request, [self._entry("copied", source_dir / "w.ckpt")])

        self.assertEqual(payload["count"], 1)
        meta = json.loads(self.sidecar.read_text(encoding="utf-8"))["models"]["copied"]
        self.assertEqual(meta, {"importMode": "reference", "managedDir": None})

    def _assert_copy_stays_managed_when_target_is_spelled(self, to_root):
        source_dir = self.old_root / "custom" / "copied"
        self.sidecar.write_text(json.dumps({"version": 1, "models": {
            "copied": {"importMode": "copy", "managedDir": str(source_dir)},
        }}), encoding="utf-8")
        request = {**self._payload("custom/copied/w.ckpt"), "toRoot": to_root}

        _, payload = self._run(request, [self._entry("copied", source_dir / "w.ckpt")])

        self.assertEqual(payload["count"], 1)
        meta = json.loads(self.sidecar.read_text(encoding="utf-8"))["models"]["copied"]
        self.assertEqual(meta["importMode"], "copy")
        self.assertEqual(Path(meta["managedDir"]), self.new_root / "custom" / "copied")

    def test_a_copy_stays_managed_when_the_target_is_reached_through_a_link(self):
        link = self.root / "linked"
        if os.name == "nt":
            import _winapi
            _winapi.CreateJunction(str(self.root), str(link))
            self.addCleanup(os.rmdir, link)
        else:
            link.symlink_to(self.root, target_is_directory=True)
        self._assert_copy_stays_managed_when_target_is_spelled(str(link / "new-models"))

    def test_remapping_onto_the_same_root_does_nothing(self):
        entry = self._entry("x", self.old_root / "w.ckpt")
        code, payload = self._run({"fromRoot": str(self.old_root), "toRoot": str(self.old_root)},
                                  [entry])
        self.assertEqual(code, 0)
        self.assertIs(payload["unchanged"], True)
        self.assertEqual(self.registered, [entry])

    def test_no_registry_requires_no_write(self):
        code, payload = self._run(self._payload("model.ckpt"))
        self.assertEqual(code, 0)
        self.assertEqual(payload, {"committed": True, "count": 0, "ownershipUpdateFailed": False})
        self.assertFalse(self.registry.exists())

    def test_an_ownership_write_failure_keeps_the_model_usable_and_reports_the_warning(self):
        from worker_models import _user_model_import_mode

        copied = "custom/copied/w.ckpt"
        source = self.old_root / copied
        self.sidecar.write_text(json.dumps({"version": 1, "models": {
            "copied": {"importMode": "copy", "managedDir": str(source.parent)},
        }}), encoding="utf-8")
        original_sidecar = self.sidecar.read_bytes()
        replace = ccm.os.replace

        def replace_writable_file(staged, destination):
            if destination == self.sidecar:
                raise PermissionError("ownership metadata is read-only")
            replace(staged, destination)

        with mock.patch.object(ccm.os, "replace", side_effect=replace_writable_file):
            code, payload = self._run(self._payload(copied), [self._entry("copied", source)])

        self.assertEqual(code, 0)
        self.assertEqual(payload, {"committed": True, "count": 1, "ownershipUpdateFailed": True})
        self.assertEqual(Path(self.registered[0]["model_path"]), self.new_root / copied)
        self.assertEqual(self.sidecar.read_bytes(), original_sidecar)
        model = FakeRegisteredEntry("copied", "mel_band_roformer", str(self.new_root / copied))
        model.source = "user"
        with mock.patch.dict(os_environ_target, {"PYMSS_STUDIO_CUSTOM_MODEL_META": str(self.sidecar)}):
            self.assertEqual(_user_model_import_mode(model, str(self.new_root)), "reference")

    def test_an_unsuccessful_commit_preserves_all_registry_and_sidecar_entries(self):
        entries = [self._entry(name, self.old_root / f"{name}.ckpt") for name in ("a", "b")]
        self.sidecar.write_text(json.dumps({"version": 1, "models": {"a": {"importMode": "reference"}}}), encoding="utf-8")
        original_sidecar = self.sidecar.read_bytes()
        with mock.patch.object(ccm.os, "replace", side_effect=PermissionError("registry is read-only")):
            code, payload = self._run(self._payload("a.ckpt", "b.ckpt"), entries)
        self.assertEqual(code, 1)
        self.assertEqual(payload["code"], "CUSTOM_MODEL_REMAP_FAILED")
        self.assertIn("registry is read-only", payload["message"])
        self.assertEqual(self.registered, entries)
        self.assertEqual(self.sidecar.read_bytes(), original_sidecar)
        self.assertEqual(list(self.root.glob(".user_models.json.*.tmp")), [])

    def test_a_missing_destination_prevents_the_entire_registry_update(self):
        entries = [self._entry(name, self.old_root / f"{name}.ckpt") for name in ("a", "b")]
        request = self._payload("a.ckpt", "b.ckpt")
        (self.new_root / "b.ckpt").unlink()
        code, payload = self._run(request, entries)
        self.assertEqual(code, 1)
        self.assertEqual(payload["code"], "CUSTOM_MODEL_REMAP_FAILED")
        self.assertEqual(self.registered, entries)

    def test_missing_roots_are_rejected(self):
        with redirect_stdout(self.stdout):
            self.assertEqual(ccm.cmd_remap_custom_model_paths({"fromRoot": "", "toRoot": "/x"}), 1)
        self.assertEqual(
            json.loads(self.stdout.getvalue().strip().splitlines()[-1])["payload"]["code"],
            "CUSTOM_MODEL_INVALID",
        )


class SidecarTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.root, True)

    def test_the_sidecar_sits_beside_the_registry_by_default(self):
        # So that both travel together when the data root moves.
        registry = self.root / "settings" / "user_models.json"
        with mock.patch.dict(os_environ_target, {"PYMSS_USER_MODELS": str(registry)}, clear=False):
            self.assertEqual(ccm._sidecar_path(), registry.parent / ccm.SIDECAR_FILENAME)

    def test_a_corrupt_sidecar_reads_as_empty_rather_than_raising(self):
        broken = self.root / "meta.json"
        broken.write_text("{not json", encoding="utf-8")
        with mock.patch.dict(os_environ_target, {"PYMSS_STUDIO_CUSTOM_MODEL_META": str(broken)}, clear=False):
            self.assertEqual(ccm._read_sidecar(), {})
            self.assertEqual(ccm.sidecar_entry("anything"), {})


if __name__ == "__main__":
    unittest.main()
