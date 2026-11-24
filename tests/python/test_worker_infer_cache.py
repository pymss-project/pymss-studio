import os
import unittest
from unittest import mock

import worker_infer


class _Separator:
    def __init__(self):
        self.closed = False
        self._studio_cache_key = ("model",)

    def close(self):
        self.closed = True


class SeparatorCacheTests(unittest.TestCase):
    def setUp(self):
        worker_infer._SEPARATOR_CACHE["key"] = None
        worker_infer._SEPARATOR_CACHE["separator"] = None

    def tearDown(self):
        worker_infer._SEPARATOR_CACHE["key"] = None
        worker_infer._SEPARATOR_CACHE["separator"] = None

    def test_successful_job_keeps_the_loaded_separator(self):
        separator = _Separator()
        with mock.patch.dict(os.environ, {"PYMSS_STUDIO_KEEP_MODEL": "1"}):
            worker_infer._release_separator(separator, reuse=True)
        self.assertIs(worker_infer._SEPARATOR_CACHE["separator"], separator)
        self.assertFalse(separator.closed)

    def test_failed_job_unloads_the_separator(self):
        separator = _Separator()
        with mock.patch.dict(os.environ, {"PYMSS_STUDIO_KEEP_MODEL": "1"}):
            worker_infer._release_separator(separator, reuse=False)
        self.assertIsNone(worker_infer._SEPARATOR_CACHE["separator"])
        self.assertTrue(separator.closed)

    def test_cache_key_changes_when_the_model_changes(self):
        first = worker_infer._separator_cache_key(
            {"model_type": "bs_roformer", "model_path": "a.ckpt", "config_path": "a.yaml"},
            device="xpu", device_ids=[0], use_tta=False, inference_params={"chunk_size": 1}, audio_params={},
        )
        second = worker_infer._separator_cache_key(
            {"model_type": "bs_roformer", "model_path": "b.ckpt", "config_path": "a.yaml"},
            device="xpu", device_ids=[0], use_tta=False, inference_params={"chunk_size": 1}, audio_params={},
        )
        self.assertNotEqual(first, second)

    def test_cache_miss_releases_old_model_before_loading_new_model(self):
        previous = _Separator()
        resolved = {"model_type": "bs_roformer", "model_path": "new.ckpt"}

        def load_model(**kwargs):
            self.assertTrue(previous.closed)
            self.assertIsNone(worker_infer._SEPARATOR_CACHE["separator"])
            self.assertEqual(kwargs["model_path"], "new.ckpt")
            return _Separator()

        with (
            mock.patch.dict(os.environ, {"PYMSS_STUDIO_KEEP_MODEL": "1"}),
            mock.patch.object(worker_infer, "_studio_separator_type", return_value=load_model),
            mock.patch.object(worker_infer, "_resolve_studio_model", return_value=resolved),
            mock.patch.object(worker_infer, "emit"),
        ):
            worker_infer._release_separator(previous, reuse=True)
            current = worker_infer._prepare_separator(
                payload={"model": "next", "output": ".", "device": "cpu"},
                task_id="next", progress_callback=None, logger=None,
            )

        self.assertIsNot(current, previous)


if __name__ == "__main__":
    unittest.main()
