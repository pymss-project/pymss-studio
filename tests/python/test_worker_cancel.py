import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

import worker_cancel


class CooperativeCancellationTests(unittest.TestCase):
    def setUp(self):
        self.addCleanup(worker_cancel.watch, {})
        self.request = Path(tempfile.mkdtemp()) / "cancel"
        self.addCleanup(self.request.parent.rmdir)
        self.addCleanup(self.request.unlink, missing_ok=True)

    def test_progress_continues_until_studio_creates_the_request(self):
        worker_cancel.watch({"cancelFile": str(self.request)})
        reports = []
        progress = worker_cancel.cancellable(lambda done, total, message: reports.append(done))

        progress(1, 3, "Processing audio")
        self.request.touch()
        with self.assertRaises(worker_cancel.TaskCancelled):
            progress(2, 3, "Processing audio")
        self.assertEqual(reports, [1])

    def test_model_code_catching_exception_cannot_swallow_the_cancellation(self):
        worker_cancel.watch({"cancelFile": str(self.request)})
        self.request.touch()
        with self.assertRaises(worker_cancel.TaskCancelled):
            try:
                worker_cancel.raise_if_cancelled()
            except Exception:  # noqa: BLE001 - mirrors broad handlers in separation code
                self.fail("cancellation was caught as an ordinary error")

    def test_a_request_without_a_cancel_file_is_never_cancelled(self):
        self.request.touch()
        worker_cancel.watch({})
        worker_cancel.raise_if_cancelled()
        self.assertIsNone(worker_cancel.cancellable(None))

    def test_queued_work_is_finished_only_on_initialized_accelerators(self):
        cuda = types.SimpleNamespace(is_initialized=lambda: True, synchronize=mock.Mock())
        xpu = types.SimpleNamespace(is_initialized=lambda: False, synchronize=mock.Mock())
        with mock.patch.dict("sys.modules", {"torch": types.SimpleNamespace(cuda=cuda, xpu=xpu)}):
            worker_cancel.finish_device_work()
        cuda.synchronize.assert_called_once_with()
        xpu.synchronize.assert_not_called()


if __name__ == "__main__":
    unittest.main()
