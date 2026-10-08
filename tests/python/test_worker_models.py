import io
import json
import shutil
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

if __package__:
    from . import _bootstrap as _worker_test_bootstrap
else:
    import _bootstrap as _worker_test_bootstrap

import worker_models


class FakeUserModelEntry:
    """Duck-type of pymss's UserModelEntry.

    Deliberately a local stub rather than the real class: these tests must assert how this
    module treats a user entry, not whether pymss happens to be installed for the interpreter
    running the suite."""

    def __init__(self, name, model_path, config_path=None, **overrides):
        self.name = name
        self.model_path = str(model_path)
        self.config_path = str(config_path) if config_path else None
        self.source = "user"
        self.aliases = ()
        self.model_type = overrides.get("model_type", "bs_roformer")
        self.architecture = self.model_type
        self.supported = True
        self.unsupported_reason = ""
        # The fields that make a catalog-style path computation silently wrong.
        self.relpath = ""
        self.config_relpath = ""
        self.auxiliary_relpaths = ()
        self.size_bytes = overrides.get("size_bytes", 0)
        self.sha256 = ""
        self.primary_category = "user"
        self.primary_category_cn = "用户"
        self.secondary_category = "custom"
        self.secondary_category_cn = "自定义"
        self.target_stem = ""
        self.config_instruments = ""
        self.config_target_instrument = ""
        self.classification_confidence = "user"
        self.classification_basis = "user_registered"
        self.inference_params = {}

    @property
    def category_path(self):
        return "user/custom"


CATALOG_ENTRY = worker_models.ModelEntry.from_dict({
    "name": "catalog_model.ckpt",
    "relpath": "vocals/catalog_model.ckpt",
    "config_relpath": "vocals/catalog_model.yaml",
    "model_type": "bs_roformer",
    "size_bytes": 4096,
    "supported": True,
})


class CatalogTargetOverrideTests(unittest.TestCase):
    def test_target_override_survives_catalog_overlay_and_entry_serialization(self):
        base = {
            "models": [{
                "name": "model.ckpt",
                "relpath": "vocals/model.ckpt",
                "target_instrument_override": "Vocals",
            }],
        }
        overlay = {
            "storage_format": "overlay-v1",
            "overrides": {"model.ckpt": {"primary_category_cn": "人声"}},
        }

        effective = worker_models._apply_debug_catalog(base, overlay)
        entry = worker_models.ModelEntry.from_dict(effective["models"][0])

        self.assertEqual(entry.target_instrument_override, "Vocals")
        self.assertEqual(worker_models._entry_to_catalog_dict(entry)["target_instrument_override"], "Vocals")
        self.assertEqual(entry.primary_category_cn, "人声")

    def test_catalog_entries_without_a_target_override_keep_the_default(self):
        entry = worker_models.ModelEntry.from_dict({"name": "model.ckpt", "relpath": "model.ckpt"})

        self.assertEqual(entry.target_instrument_override, "")


class LightweightModelDiscoveryTests(unittest.TestCase):
    def setUp(self):
        worker_models._model_catalog_path.cache_clear()
        self.root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.root, True)

    def tearDown(self):
        worker_models._model_catalog_path.cache_clear()

    def test_catalog_path_uses_distribution_metadata_without_importing_pymss(self):
        catalog = self.root / "pymss" / "resources" / "model_catalog.json"
        catalog.parent.mkdir(parents=True)
        catalog.write_text('{"models": []}', encoding="utf-8")
        package_file = Path("pymss/resources/model_catalog.json")
        distribution = mock.Mock(files=[package_file])
        distribution.locate_file.return_value = catalog

        with mock.patch("importlib.metadata.distribution", return_value=distribution) as locate:
            resolved = worker_models._model_catalog_path()

        self.assertEqual(resolved, catalog.resolve())
        locate.assert_called_once_with("pymss")

    def test_user_registry_is_read_without_importing_pymss(self):
        weights = self.root / "model.ckpt"
        config = self.root / "model.yaml"
        registry = self.root / "user-models.json"
        registry.write_text(json.dumps({
            "version": 1,
            "models": [{
                "name": "custom_model",
                "model_type": "bs_roformer",
                "model_path": str(weights),
                "config_path": str(config),
                "aliases": ["custom_alias"],
                "inference_params": {"overlap_size": 4},
            }],
        }), encoding="utf-8")

        with mock.patch.dict("os.environ", {"PYMSS_USER_MODELS": str(registry)}):
            entries = worker_models._load_registered_user_models()

        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0].name, "custom_model")
        self.assertEqual(entries[0].model_path, str(weights))
        self.assertEqual(entries[0].config_path, str(config))
        self.assertEqual(entries[0].aliases, ("custom_alias",))
        self.assertEqual(entries[0].inference_params, {"overlap_size": 4})
        self.assertEqual(entries[0].category_path, "user/custom")


class LegacyCatalogEntry:
    def __init__(self, name, relpath):
        self.name = name
        self.aliases = ()
        self.model_type = "vr"
        self.architecture = "vr"
        self.supported = True
        self.unsupported_reason = ""
        self.relpath = relpath
        self.config_relpath = ""
        self.auxiliary_relpaths = ()
        self.size_bytes = 0
        self.sha256 = ""
        self.primary_category = "vocal"
        self.primary_category_cn = "人声"
        self.secondary_category = ""
        self.secondary_category_cn = ""
        self.target_stem = "vocals"

    @property
    def category_path(self):
        return self.primary_category


class UserModelPathTests(unittest.TestCase):
    """A user entry stores absolute paths and leaves relpath empty, so the catalog computation
    `model_root / relpath` collapses to the model directory itself — an existing path pointing
    at the wrong thing."""

    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.root, True)
        self.model_dir = self.root / "models"
        self.model_dir.mkdir()
        self.weights = self.root / "elsewhere" / "my_model.ckpt"
        self.weights.parent.mkdir()
        self.weights.write_bytes(b"x" * 2048)
        self.config = self.root / "elsewhere" / "my_model.yaml"
        self.config.write_text("training:\n  instruments: [vocals, other]\n", encoding="utf-8")

    def test_paths_come_from_the_registration_not_the_model_directory(self):
        entry = FakeUserModelEntry("my_model", self.weights, self.config)
        self.assertEqual(worker_models.model_path_for(entry, str(self.model_dir)), self.weights)
        self.assertEqual(worker_models.config_path_for(entry, str(self.model_dir)), self.config)

    def test_the_model_directory_is_never_mistaken_for_a_model_file(self):
        # Regression guard for the actual defect: with relpath == "" the catalog computation
        # returns the model directory, which is_dir() and would confuse every caller.
        entry = FakeUserModelEntry("my_model", self.weights, self.config)
        resolved = worker_models.model_path_for(entry, str(self.model_dir))
        self.assertNotEqual(resolved, self.model_dir)
        self.assertTrue(resolved.is_file())

    def test_a_registration_without_a_config_reports_none(self):
        entry = FakeUserModelEntry("my_model", self.weights)
        self.assertIsNone(worker_models.config_path_for(entry, str(self.model_dir)))

    def test_catalog_entries_keep_resolving_against_the_model_directory(self):
        self.assertEqual(
            worker_models.model_path_for(CATALOG_ENTRY, str(self.model_dir)),
            self.model_dir / "vocals" / "catalog_model.ckpt",
        )
        self.assertEqual(
            worker_models.config_path_for(CATALOG_ENTRY, str(self.model_dir)),
            self.model_dir / "vocals" / "catalog_model.yaml",
        )

    def test_catalog_config_ignores_legacy_debug_override(self):
        debug_dir = self.root / "debug"
        override = debug_dir / "model-configs" / "catalog_model.ckpt.yaml"
        override.parent.mkdir(parents=True)
        override.write_text("debug: true\n", encoding="utf-8")
        with mock.patch.dict("os.environ", {"PYMSS_STUDIO_DEBUG_DIR": str(debug_dir)}):
            self.assertEqual(
                worker_models.config_path_for(CATALOG_ENTRY, str(self.model_dir)),
                self.model_dir / "vocals" / "catalog_model.yaml",
            )

    def test_auxiliary_paths_of_a_user_entry_are_used_as_given(self):
        entry = FakeUserModelEntry("my_model", self.weights, self.config)
        extra = self.root / "elsewhere" / "extra.bin"
        entry.auxiliary_relpaths = (str(extra),)
        self.assertEqual(worker_models.auxiliary_paths_for(entry, str(self.model_dir)), [extra])

    def test_only_user_entries_are_treated_as_user_entries(self):
        self.assertTrue(worker_models.is_user_model_entry(FakeUserModelEntry("m", self.weights)))
        self.assertFalse(worker_models.is_user_model_entry(CATALOG_ENTRY))


class ModelStorageSummaryTests(unittest.TestCase):
    def test_tool_model_cache_is_counted_but_not_reported_as_residual(self):
        with tempfile.TemporaryDirectory() as temp_value:
            root = Path(temp_value)
            catalog_model = root / CATALOG_ENTRY.relpath
            catalog_config = root / CATALOG_ENTRY.config_relpath
            asr_model = (
                root / "_tool_models" / "asr" / "models"
                / "iic--speech_paraformer-large_asr_nat-zh-cn-16k-common-vocab8404-pytorch"
                / "snapshots" / "master" / "model.pt"
            )
            catalog_model.parent.mkdir(parents=True)
            asr_model.parent.mkdir(parents=True)
            catalog_model.write_bytes(b"m" * 7)
            catalog_config.write_bytes(b"c" * 5)
            asr_model.write_bytes(b"a" * 2048)
            asr_config = asr_model.parent / "config.yaml"
            asr_configuration = asr_model.parent / "configuration.json"
            asr_config.write_bytes(b"config")
            asr_configuration.write_text(json.dumps({
                "file_path_metas": {"init_param": "model.pt", "config": "config.yaml"},
            }), encoding="utf-8")
            asr_bytes = asr_model.stat().st_size + asr_config.stat().st_size + asr_configuration.stat().st_size
            residual = root / "partial-download.tmp"
            residual.write_bytes(b"r" * 13)

            registry = mock.Mock()
            registry.model_root.return_value = root
            registry.list_models.return_value = [CATALOG_ENTRY]
            registry.model_path_for.side_effect = lambda entry, _: root / entry.relpath
            registry.config_path_for.side_effect = lambda entry, _: root / entry.config_relpath
            registry.auxiliary_paths_for.return_value = []
            with mock.patch.dict("sys.modules", {
                "pymss": mock.Mock(),
                "pymss.model_registry": registry,
            }):
                summary = worker_models._storage_summary_payload(str(root))

        self.assertEqual(summary["toolModelsBytes"], asr_bytes)
        self.assertEqual(summary["totalBytes"], 12 + asr_bytes)
        self.assertEqual(summary["downloadedCount"], 2)
        self.assertEqual(len(summary["toolModels"]), 1)
        self.assertEqual(
            summary["toolModels"][0]["name"],
            "iic/speech_paraformer-large_asr_nat-zh-cn-16k-common-vocab8404-pytorch",
        )
        self.assertEqual(summary["toolModels"][0]["role"], "recognition")
        self.assertEqual(summary["toolModels"][0]["fileCount"], 3)
        self.assertEqual(summary["residualBytes"], 13)
        self.assertEqual([Path(item["path"]).name for item in summary["residualFiles"]], [residual.name])

    def test_incomplete_tool_model_is_hidden_and_reported_as_residual(self):
        with tempfile.TemporaryDirectory() as temp_value:
            root = Path(temp_value)
            partial_model = (
                root / "_tool_models" / "asr" / "models"
                / "iic--punc_ct-transformer_cn-en-common-vocab471067-large"
                / "snapshots" / "master"
            )
            partial_model.mkdir(parents=True)
            (partial_model / "config.yaml").write_bytes(b"config")
            (partial_model / "model.pt.incomplete").write_bytes(b"partial-weight")

            registry = mock.Mock()
            registry.model_root.return_value = root
            registry.list_models.return_value = []
            with mock.patch.dict("sys.modules", {
                "pymss": mock.Mock(),
                "pymss.model_registry": registry,
            }):
                summary = worker_models._storage_summary_payload(str(root))

        self.assertEqual(summary["downloadedCount"], 0)
        self.assertEqual(summary["toolModels"], [])
        self.assertEqual(summary["toolModelsBytes"], 0)
        self.assertEqual(summary["totalBytes"], 0)
        self.assertEqual(summary["residualBytes"], len(b"config") + len(b"partial-weight"))
        self.assertEqual(
            {Path(item["path"]).name for item in summary["residualFiles"]},
            {"config.yaml", "model.pt.incomplete"},
        )

    def test_residual_cleanup_removes_empty_incomplete_tool_model_directories(self):
        with tempfile.TemporaryDirectory() as temp_value:
            root = Path(temp_value)
            models_root = root / "_tool_models" / "asr" / "models"
            cache_root = models_root / "iic--incomplete-model"
            snapshot = cache_root / "snapshots" / "master"
            snapshot.mkdir(parents=True)
            (snapshot / "config.yaml").write_bytes(b"config")
            (snapshot / "model.pt.incomplete").write_bytes(b"partial")

            registry = mock.Mock()
            registry.model_root.return_value = root
            registry.list_models.return_value = []
            output = io.StringIO()
            with mock.patch.dict("sys.modules", {
                "pymss": mock.Mock(),
                "pymss.model_registry": registry,
            }), redirect_stdout(output):
                code = worker_models.cmd_cleanup_model_residual_files({"modelDir": str(root)})

            event = json.loads(output.getvalue().strip())
            self.assertEqual(code, 0)
            self.assertEqual(event["type"], "model_residual_cleaned")
            self.assertFalse(cache_root.exists())
            self.assertTrue(models_root.is_dir())
            self.assertEqual(event["payload"]["modelStorageSummary"]["residualFiles"], [])

    def test_tool_model_without_a_weight_is_not_counted_as_downloaded(self):
        with tempfile.TemporaryDirectory() as temp_value:
            root = Path(temp_value)
            metadata_only = (
                root / "_tool_models" / "asr" / "models"
                / "iic--metadata-only" / "snapshots" / "master" / "config.yaml"
            )
            metadata_only.parent.mkdir(parents=True)
            metadata_only.write_bytes(b"config")

            registry = mock.Mock()
            registry.model_root.return_value = root
            registry.list_models.return_value = []
            with mock.patch.dict("sys.modules", {
                "pymss": mock.Mock(),
                "pymss.model_registry": registry,
            }):
                summary = worker_models._storage_summary_payload(str(root))

        self.assertEqual(summary["toolModels"], [])
        self.assertEqual(summary["downloadedCount"], 0)
        self.assertEqual(summary["residualBytes"], len(b"config"))

    def test_tool_model_with_missing_declared_file_is_reported_as_residual(self):
        with tempfile.TemporaryDirectory() as temp_value:
            root = Path(temp_value)
            snapshot = (
                root / "_tool_models" / "asr" / "models"
                / "iic--missing-tokenizer" / "snapshots" / "master"
            )
            snapshot.mkdir(parents=True)
            (snapshot / "model.pt").write_bytes(b"weight" * 512)
            (snapshot / "config.yaml").write_bytes(b"config")
            (snapshot / "configuration.json").write_text(json.dumps({
                "file_path_metas": {
                    "init_param": "model.pt",
                    "config": "config.yaml",
                    "tokenizer_conf": {"token_list": "tokens.json"},
                },
            }), encoding="utf-8")

            registry = mock.Mock()
            registry.model_root.return_value = root
            registry.list_models.return_value = []
            with mock.patch.dict("sys.modules", {
                "pymss": mock.Mock(),
                "pymss.model_registry": registry,
            }):
                summary = worker_models._storage_summary_payload(str(root))

        self.assertEqual(summary["toolModels"], [])
        self.assertEqual(summary["downloadedCount"], 0)
        self.assertGreater(summary["residualBytes"], 0)

    def test_delete_tool_model_removes_only_the_selected_managed_directory(self):
        with tempfile.TemporaryDirectory() as temp_value:
            root = Path(temp_value)
            models_root = root / "_tool_models" / "asr" / "models"
            selected = models_root / "iic--speech_paraformer"
            retained = models_root / "iic--speech_fsmn_vad"
            selected.mkdir(parents=True)
            retained.mkdir(parents=True)
            (selected / "model.pt").write_bytes(b"selected")
            (retained / "model.pt").write_bytes(b"retained" * 256)
            (retained / "config.yaml").write_bytes(b"config")
            (retained / "configuration.json").write_text(json.dumps({
                "file_path_metas": {"init_param": "model.pt", "config": "config.yaml"},
            }), encoding="utf-8")

            registry = mock.Mock()
            registry.model_root.return_value = root
            registry.list_models.return_value = []
            output = io.StringIO()
            with mock.patch.dict("sys.modules", {
                "pymss": mock.Mock(),
                "pymss.model_registry": registry,
            }), redirect_stdout(output):
                code = worker_models.cmd_delete_tool_model({
                    "modelDir": str(root),
                    "tool": "asr",
                    "id": selected.name,
                })

            event = json.loads(output.getvalue().strip())
            self.assertEqual(code, 0)
            self.assertEqual(event["type"], "tool_model_deleted")
            self.assertFalse(selected.exists())
            self.assertTrue(retained.is_dir())
            self.assertEqual(
                [item["id"] for item in event["payload"]["modelStorageSummary"]["toolModels"]],
                [retained.name],
            )

    def test_delete_tool_model_rejects_paths_outside_the_managed_cache(self):
        output = io.StringIO()
        with redirect_stdout(output):
            code = worker_models.cmd_delete_tool_model({
                "modelDir": "models",
                "tool": "asr",
                "id": "../outside",
            })

        event = json.loads(output.getvalue().strip())
        self.assertEqual(code, 1)
        self.assertEqual(event["type"], "error")
        self.assertEqual(event["payload"]["code"], "TOOL_MODEL_DELETE_FAILED")

    def test_delete_tool_model_can_ignore_an_already_missing_recovery_target(self):
        with tempfile.TemporaryDirectory() as temp_value:
            root = Path(temp_value)
            registry = mock.Mock()
            registry.model_root.return_value = root
            registry.list_models.return_value = []
            output = io.StringIO()
            with mock.patch.dict("sys.modules", {
                "pymss": mock.Mock(),
                "pymss.model_registry": registry,
            }), redirect_stdout(output):
                code = worker_models.cmd_delete_tool_model({
                    "modelDir": str(root),
                    "tool": "asr",
                    "id": "iic--missing-model",
                    "missingOk": True,
                })

        event = json.loads(output.getvalue().strip())
        self.assertEqual(code, 0)
        self.assertEqual(event["type"], "tool_model_deleted")


class UserModelSerializationTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.root, True)
        self.weights = self.root / "my_model.ckpt"
        self.weights.write_bytes(b"x" * 3072)
        self.config = self.root / "my_model.yaml"
        self.config.write_text(
            "training:\n  instruments: [vocals, instrumental]\n  target_instrument: vocals\n",
            encoding="utf-8",
        )

    def test_an_imported_model_is_labelled_as_a_user_model(self):
        payload = worker_models.model_to_dict(FakeUserModelEntry("my_model", self.weights, self.config))
        self.assertEqual(payload["source"], "user")
        self.assertEqual(payload["modelPath"], str(self.weights))
        self.assertEqual(payload["configPath"], str(self.config))
        self.assertTrue(payload["downloaded"])
        self.assertEqual(payload["missingPaths"], [])

    def test_catalog_models_are_labelled_as_catalog(self):
        payload = worker_models.model_to_dict(CATALOG_ENTRY, str(self.root), include_local_state=False)
        self.assertEqual(payload["source"], "catalog")
        # Catalog models have no import mode; the UI uses its absence to keep its own actions apart.
        self.assertIsNone(payload["importMode"])

    def test_the_import_mode_travels_with_the_model(self):
        # This is what lets the removal dialog state whether files will actually be deleted,
        # instead of describing both cases and leaving the user to guess.
        with mock.patch.dict("sys.modules", {
            "worker_custom_models": mock.Mock(sidecar_entry=mock.Mock(return_value={"importMode": "copy"})),
        }):
            payload = worker_models.model_to_dict(FakeUserModelEntry("m", self.weights))
        self.assertEqual(payload["importMode"], "copy")

    def test_a_model_registered_outside_the_app_reads_as_a_reference(self):
        # `pymss register` leaves no side-car. Assuming 'copy' would let the UI promise a file
        # deletion that must never happen.
        with mock.patch.dict("sys.modules", {
            "worker_custom_models": mock.Mock(sidecar_entry=mock.Mock(return_value={})),
        }):
            payload = worker_models.model_to_dict(FakeUserModelEntry("m", self.weights))
        self.assertEqual(payload["importMode"], "reference")

    def test_an_unreadable_sidecar_falls_back_to_reference(self):
        with mock.patch.dict("sys.modules", {
            "worker_custom_models": mock.Mock(sidecar_entry=mock.Mock(side_effect=OSError("denied"))),
        }):
            payload = worker_models.model_to_dict(FakeUserModelEntry("m", self.weights))
        self.assertEqual(payload["importMode"], "reference")

    def test_a_size_is_measured_when_the_registration_records_none(self):
        # Registration never stores a size, so without measuring, every card would read 0 bytes.
        payload = worker_models.model_to_dict(FakeUserModelEntry("my_model", self.weights, self.config))
        self.assertEqual(payload["sizeBytes"], 3072)

    def test_a_recorded_size_is_preferred_over_measuring(self):
        entry = FakeUserModelEntry("my_model", self.weights, self.config, size_bytes=999)
        self.assertEqual(worker_models.model_to_dict(entry)["sizeBytes"], 999)

    def test_a_missing_weight_file_is_reported_rather_than_measured_as_zero(self):
        entry = FakeUserModelEntry("gone", self.root / "absent.ckpt")
        payload = worker_models.model_to_dict(entry)
        self.assertFalse(payload["downloaded"])
        self.assertEqual(payload["missingPaths"], [str(self.root / "absent.ckpt")])
        self.assertEqual(payload["sizeBytes"], 0)

    def test_stems_are_read_from_the_registered_config(self):
        # This is what lets the separation page offer stem selection for an imported model.
        payload = worker_models.model_to_dict(FakeUserModelEntry("my_model", self.weights, self.config))
        self.assertEqual(payload["configInstruments"], "vocals|instrumental")
        self.assertEqual(payload["configTargetInstrument"], "vocals")
        self.assertEqual(payload["targetStem"], "")

    def test_inference_step_metadata_reuses_the_loaded_config(self):
        self.config.write_text(
            "model:\n  stft_hop_length: 512\n"
            "training:\n  instruments: [vocals, instrumental]\n  target_instrument: vocals\n",
            encoding="utf-8",
        )
        with mock.patch.object(worker_models, "_load_yaml_config", wraps=worker_models._load_yaml_config) as load:
            payload = worker_models.model_to_dict(FakeUserModelEntry("my_model", self.weights, self.config))

        self.assertEqual(payload["inferenceParamMeta"], {
            "recommendedSampleStep": 512,
            "source": "model.stft_hop_length",
        })
        load.assert_called_once_with(self.config)

    def test_legacy_catalog_entries_without_config_stem_fields_serialize(self):
        entry = LegacyCatalogEntry("old_model.pth", "old_model.pth")
        (self.root / entry.relpath).write_bytes(b"x")

        payload = worker_models.model_to_dict(entry, str(self.root))

        self.assertEqual(payload["name"], "old_model.pth")
        self.assertEqual(payload["targetStem"], "vocals")
        self.assertEqual(payload["configInstruments"], "")
        self.assertEqual(payload["configTargetInstrument"], "")
        self.assertTrue(payload["downloaded"])


class DebugCatalogPayloadValidationTests(unittest.TestCase):
    def _payload(self, **overrides):
        model = {
            "name": "debug_model.ckpt",
            "relpath": "debug/debug_model.ckpt",
            "config_relpath": "debug/debug_model.yaml",
            "auxiliary_relpaths": ["debug/debug_model.json"],
        }
        model.update(overrides)
        return {"schema_version": 1, "models": [model]}

    def test_normalizes_valid_auxiliary_relpaths(self):
        result = worker_models._validate_catalog_payload(self._payload(auxiliary_relpaths=["debug\\extra.json"]))

        self.assertEqual(result["models"][0]["auxiliary_relpaths"], ["debug/extra.json"])

    def test_rejects_auxiliary_relpaths_when_not_an_array(self):
        with self.assertRaisesRegex(ValueError, "auxiliary_relpaths must be an array"):
            worker_models._validate_catalog_payload(self._payload(auxiliary_relpaths="debug/extra.json"))

    def test_rejects_unsafe_auxiliary_relpath(self):
        with self.assertRaisesRegex(ValueError, "safe relative path"):
            worker_models._validate_catalog_payload(self._payload(auxiliary_relpaths=["../outside.json"]))

    def test_rejects_missing_weight_relpath(self):
        with self.assertRaisesRegex(ValueError, "relpath is required"):
            worker_models._validate_catalog_payload(self._payload(relpath=""))


class DebugCatalogOverlayTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.root, True)
        self.debug_catalog = self.root / "model-catalog.json"
        self.base = {
            "schema_version": 1,
            "source_repository": {"ref": "v2"},
            "models": [{
                "name": "model.ckpt",
                "aliases": [],
                "model_type": "bs_roformer",
                "architecture": "bs_roformer",
                "supported": True,
                "unsupported_reason": "",
                "relpath": "vocal/model.ckpt",
                "config_relpath": "vocal/model.yaml",
                "auxiliary_relpaths": [],
                "size_bytes": 1024,
                "primary_category": "vocal",
                "primary_category_cn": "人声",
                "secondary_category": "vocals_instrumental",
                "secondary_category_cn": "人声/伴奏",
                "target_stem": "vocals",
            }],
        }

    def test_legacy_snapshot_ignores_implicit_optional_defaults(self):
        legacy = worker_models._canonical_catalog_data(self.base)
        self.debug_catalog.write_text(json.dumps(legacy), encoding="utf-8")

        with mock.patch.object(worker_models, "_debug_catalog_path", return_value=self.debug_catalog):
            status = worker_models._debug_catalog_status(self.base)

        self.assertEqual(status["changedCount"], 0)
        self.assertEqual(status["addedCount"], 0)
        self.assertEqual(status["removedCount"], 0)

    def test_overlay_contains_only_explicit_model_changes(self):
        desired = worker_models._canonical_catalog_data(self.base)
        desired["models"][0]["primary_category_cn"] = "新分类"

        overlay = worker_models._build_debug_catalog_overlay(desired, self.base)

        self.assertEqual(overlay["models"], [])
        self.assertEqual(overlay["removed"], [])
        self.assertEqual(
            overlay["overrides"],
            {"model.ckpt": {"primary_category_cn": "新分类"}},
        )

    def test_overlay_inherits_unchanged_fields_from_updated_core(self):
        desired = worker_models._canonical_catalog_data(self.base)
        desired["models"][0]["primary_category_cn"] = "新分类"
        overlay = worker_models._build_debug_catalog_overlay(desired, self.base)
        updated_base = json.loads(json.dumps(self.base))
        updated_base["models"][0]["architecture"] = "bs_roformer_v2"

        effective = worker_models._apply_debug_catalog(updated_base, overlay)

        self.assertEqual(effective["models"][0]["architecture"], "bs_roformer_v2")
        self.assertEqual(effective["models"][0]["primary_category_cn"], "新分类")

    def test_explicit_removed_list_is_preserved_and_applied(self):
        desired = worker_models._canonical_catalog_data(self.base)
        desired["removed"] = ["model.ckpt"]

        overlay = worker_models._build_debug_catalog_overlay(desired, self.base)
        effective = worker_models._apply_debug_catalog(self.base, overlay)

        self.assertEqual(overlay["removed"], ["model.ckpt"])
        self.assertEqual(effective["models"], [])

    def test_debug_added_model_merges_with_a_new_same_named_core_model(self):
        previous_base = {"schema_version": 1, "models": []}
        desired = {
            "schema_version": 1,
            "models": [{"name": "model.ckpt", "relpath": "debug/model.ckpt"}],
        }
        overlay = worker_models._build_debug_catalog_overlay(desired, previous_base)
        updated_base = json.loads(json.dumps(self.base))
        updated_base["models"][0]["new_core_field"] = "preserved"

        effective = worker_models._apply_debug_catalog(updated_base, overlay)

        self.assertEqual(len(effective["models"]), 1)
        self.assertEqual(effective["models"][0]["name"], "model.ckpt")
        self.assertEqual(effective["models"][0]["relpath"], "debug/model.ckpt")
        self.assertEqual(effective["models"][0]["new_core_field"], "preserved")

    def test_overlay_preserves_an_explicit_model_order(self):
        base = json.loads(json.dumps(self.base))
        base["models"].append({"name": "second.ckpt", "relpath": "vocal/second.ckpt"})
        desired = worker_models._canonical_catalog_data(base)
        desired["models"].insert(0, {"name": "new.ckpt", "relpath": "debug/new.ckpt"})

        overlay = worker_models._build_debug_catalog_overlay(desired, base)
        effective = worker_models._apply_debug_catalog(base, overlay)

        expected_order = ["new.ckpt", "model.ckpt", "second.ckpt"]
        self.assertEqual(overlay["model_order"], expected_order)
        self.assertEqual([item["name"] for item in effective["models"]], expected_order)

    def test_save_command_persists_overlay_instead_of_full_snapshot(self):
        desired = worker_models._canonical_catalog_data(self.base)
        desired["models"][0]["primary_category_cn"] = "新分类"
        stdout = io.StringIO()

        with mock.patch.object(worker_models, "_base_catalog_data", return_value=self.base), \
             mock.patch.object(worker_models, "_model_catalog_path", return_value=self.root / "base.json"), \
             mock.patch.object(worker_models, "_debug_catalog_path", return_value=self.debug_catalog), \
             redirect_stdout(stdout):
            code = worker_models.cmd_debug_catalog_save({"catalog": desired})

        saved = json.loads(self.debug_catalog.read_text(encoding="utf-8"))
        self.assertEqual(code, 0)
        self.assertEqual(saved["storage_format"], "overlay-v1")
        self.assertEqual(saved["models"], [])
        self.assertEqual(
            saved["overrides"],
            {"model.ckpt": {"primary_category_cn": "新分类"}},
        )

    def tearDown(self):
        worker_models.load_model_catalog.cache_clear()
        worker_models._model_index.cache_clear()


class ListMergesImportedModelsTests(unittest.TestCase):
    """Imported models must appear alongside catalog ones, or importing a model would leave it
    invisible everywhere except inference."""

    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.root, True)
        self.weights = self.root / "my_model.ckpt"
        self.weights.write_bytes(b"x" * 512)
        self.stdout = io.StringIO()

    def _list(self, payload, user_entries, raises=None):
        loader = mock.Mock(side_effect=raises) if raises is not None else mock.Mock(return_value=user_entries)
        with mock.patch.object(worker_models, "_load_registered_user_models", loader), \
             mock.patch.object(worker_models, "list_catalog_models", return_value=[CATALOG_ENTRY]), \
             mock.patch.object(worker_models, "model_root", return_value=self.root), \
             redirect_stdout(self.stdout):
            code = worker_models.cmd_list_models(payload)
        events = [json.loads(line) for line in self.stdout.getvalue().strip().splitlines() if line.strip()]
        return code, events[-1]["payload"]

    def test_imported_models_are_listed_with_catalog_models(self):
        entry = FakeUserModelEntry("my_model", self.weights)
        code, payload = self._list({}, [entry])
        self.assertEqual(code, 0)
        by_source = {m["name"]: m["source"] for m in payload["models"]}
        self.assertEqual(by_source, {"catalog_model.ckpt": "catalog", "my_model": "user"})
        self.assertEqual(payload["count"], 2)

    def test_imported_models_can_be_excluded(self):
        entry = FakeUserModelEntry("my_model", self.weights)
        _, payload = self._list({"includeCustom": False}, [entry])
        self.assertEqual([m["name"] for m in payload["models"]], ["catalog_model.ckpt"])

    def test_an_unreadable_registry_does_not_break_the_model_list(self):
        # The catalog is what the app primarily needs; a broken registry must degrade, not fail.
        code, payload = self._list({}, [], raises=RuntimeError("corrupt registry"))
        self.assertEqual(code, 0)
        self.assertEqual([m["name"] for m in payload["models"]], ["catalog_model.ckpt"])

    def test_the_custom_category_is_offered_for_filtering(self):
        entry = FakeUserModelEntry("my_model", self.weights)
        _, payload = self._list({}, [entry])
        self.assertIn("user/custom", payload["categories"])

    def test_a_category_filter_applies_to_imported_models_too(self):
        entry = FakeUserModelEntry("my_model", self.weights)
        kept = worker_models.list_registered_user_models
        with mock.patch.object(worker_models, "_load_registered_user_models", return_value=[entry]):
            self.assertEqual(kept(category="user"), [entry])
            self.assertEqual(kept(category="user/custom"), [entry])
            self.assertEqual(kept(category="vocal"), [])


class DeleteRefusesImportedModelsTests(unittest.TestCase):
    """Deleting an imported model would delete the user's own file, often outside the app.
    cmd_delete_model must refuse and point at the unregister path instead."""

    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.root, True)
        self.weights = self.root / "keep_me.ckpt"
        self.weights.write_bytes(b"x" * 1024)
        self.stdout = io.StringIO()

    def _run(self, entry):
        registry = mock.Mock()
        registry.get_model_entry.return_value = entry
        with mock.patch.dict("sys.modules", {"pymss": mock.Mock(), "pymss.model_registry": registry}), \
             redirect_stdout(self.stdout):
            return worker_models.cmd_delete_model({"model": entry.name})

    def _events(self):
        return [json.loads(line) for line in self.stdout.getvalue().strip().splitlines() if line.strip()]

    def test_deleting_an_imported_model_fails_and_touches_nothing(self):
        code = self._run(FakeUserModelEntry("my_model", self.weights))
        self.assertEqual(code, 1)
        self.assertTrue(self.weights.is_file(), "the user's own weights must survive")
        event = self._events()[-1]
        self.assertEqual(event["type"], "model_delete_failed")
        self.assertIn("custom model", event["payload"]["message"])
        self.assertEqual(event["payload"]["deleted"], [])

    def test_a_missing_model_name_still_fails_cleanly(self):
        with redirect_stdout(self.stdout):
            self.assertEqual(worker_models.cmd_delete_model({}), 1)
        self.assertEqual(self._events()[-1]["type"], "model_delete_failed")


class DeleteLegacyCatalogEntryTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.root, True)
        self.weights = self.root / "old_model.pth"
        self.weights.write_bytes(b"x" * 1024)
        self.stdout = io.StringIO()

    def test_delete_model_reports_model_info_for_legacy_catalog_entries(self):
        entry = LegacyCatalogEntry("old_model.pth", "old_model.pth")
        registry = mock.Mock()
        registry.get_model_entry.return_value = entry

        with mock.patch.dict("sys.modules", {"pymss": mock.Mock(), "pymss.model_registry": registry}), \
             redirect_stdout(self.stdout):
            code = worker_models.cmd_delete_model({"model": entry.name, "modelDir": str(self.root)})

        self.assertEqual(code, 0)
        self.assertFalse(self.weights.exists())
        event = [json.loads(line) for line in self.stdout.getvalue().strip().splitlines() if line.strip()][-1]
        self.assertEqual(event["type"], "model_deleted")
        self.assertEqual(event["payload"]["modelInfo"]["configInstruments"], "")


class EnvInfoTorchDiagnosticsTests(unittest.TestCase):
    def _run_env_info(self):
        output = io.StringIO()
        with mock.patch.object(worker_models, "import_available", return_value=False), \
             mock.patch.object(worker_models, "package_version", return_value=None), \
             redirect_stdout(output):
            self.assertEqual(worker_models.cmd_env_info(), 0)
        return json.loads(output.getvalue().strip())["payload"]

    def test_collects_device_count_even_when_accelerator_is_unavailable(self):
        torch = mock.Mock()
        torch.__version__ = "test-torch"
        torch.version.hip = None
        torch.version.cuda = None
        torch.cuda.is_available.return_value = False
        torch.cuda.device_count.return_value = 2
        torch.cuda.get_device_name.side_effect = ["GPU A", "GPU B"]
        torch.backends.mps.is_available.return_value = False

        with mock.patch.dict("sys.modules", {"torch": torch}):
            payload = self._run_env_info()

        self.assertFalse(payload["cudaAvailable"])
        self.assertEqual(payload["cudaDeviceCount"], 2)
        self.assertEqual([item["name"] for item in payload["cudaDevices"]], ["GPU A", "GPU B"])

    def test_records_device_enumeration_error_without_failing_env_info(self):
        torch = mock.Mock()
        torch.__version__ = "test-torch"
        torch.version.hip = "7.2"
        torch.version.cuda = None
        torch.cuda.is_available.return_value = True
        torch.cuda.device_count.return_value = 1
        torch.cuda.get_device_name.side_effect = RuntimeError("device query failed")
        torch.backends.mps.is_available.return_value = False

        with mock.patch.dict("sys.modules", {"torch": torch}):
            payload = self._run_env_info()

        self.assertEqual(payload["cudaDeviceCount"], 1)
        self.assertEqual(payload["cudaDevices"], [])
        self.assertIn("device 0: device query failed", payload["cudaDeviceNamesError"])

    def test_keeps_readable_devices_when_one_device_name_fails(self):
        torch = mock.Mock()
        torch.__version__ = "test-torch"
        torch.version.hip = None
        torch.version.cuda = "12.8"
        torch.cuda.is_available.return_value = True
        torch.cuda.device_count.return_value = 2
        torch.cuda.get_device_name.side_effect = [RuntimeError("device 0 unavailable"), "GPU B"]
        torch.backends.mps.is_available.return_value = False

        with mock.patch.dict("sys.modules", {"torch": torch}):
            payload = self._run_env_info()

        self.assertEqual(payload["cudaDeviceCount"], 2)
        self.assertEqual(payload["cudaDevices"], [{"id": 1, "name": "GPU B"}])
        self.assertIn("device 0: device 0 unavailable", payload["cudaDeviceNamesError"])

    def test_records_availability_error_without_skipping_device_diagnostics(self):
        torch = mock.Mock()
        torch.__version__ = "test-torch"
        torch.version.hip = "7.2"
        torch.version.cuda = None
        torch.cuda.is_available.side_effect = RuntimeError("availability query failed")
        torch.cuda.device_count.return_value = 1
        torch.cuda.get_device_name.return_value = "GPU A"
        torch.backends.mps.is_available.return_value = False

        with mock.patch.dict("sys.modules", {"torch": torch}):
            payload = self._run_env_info()

        self.assertFalse(payload["cudaAvailable"])
        self.assertIn("availability query failed", payload["cudaAvailableError"])
        self.assertEqual(payload["cudaDeviceCount"], 1)
        self.assertEqual([item["name"] for item in payload["cudaDevices"]], ["GPU A"])


if __name__ == "__main__":
    unittest.main()
