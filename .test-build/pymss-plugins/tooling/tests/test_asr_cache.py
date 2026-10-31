from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest

from plugins.asr.backend.asr import _existing_model_cache_recovery
from plugins.asr.backend.asr_cache import is_complete_asr_model_cache


class AsrCacheTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.cache = Path(temporary.name) / "asr" / "modelscope"
        self.model = self.cache / "models" / "org--voice"
        self.model.mkdir(parents=True)
        (self.model / "model.pt").write_bytes(b"\0" * 1024)

    def test_non_object_configuration_offers_model_redownload(self):
        for value in ([], None, "invalid", 42, True):
            with self.subTest(configuration=value):
                (self.model / "configuration.json").write_text(json.dumps(value), encoding="utf-8")
                self.assertFalse(is_complete_asr_model_cache(self.model))
                recovery = _existing_model_cache_recovery({"model": "org/voice"}, self.cache)
                self.assertEqual(recovery["action"], "redownload_asr_models")
                self.assertEqual(recovery["modelIds"], ["org--voice"])

    def test_valid_configuration_preserves_complete_cache(self):
        configuration = {"file_path_metas": {"model": "model.pt"}}
        (self.model / "configuration.json").write_text(json.dumps(configuration), encoding="utf-8")
        self.assertTrue(is_complete_asr_model_cache(self.model))
        self.assertIsNone(_existing_model_cache_recovery({"model": "org/voice"}, self.cache))
