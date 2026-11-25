import io
import json
import os
import sys
import unittest
from unittest import mock

import worker


class WarmWorkerProtocolTests(unittest.TestCase):
    def serve(self, command, *, poisoned=False):
        requests = [
            {"requestId": task_id, "command": "infer", "payload": {"taskId": task_id}}
            for task_id in ("first", "second")
        ]
        stdin = io.StringIO("".join(json.dumps(request) + "\n" for request in requests))
        stdout = io.StringIO()
        stderr = io.StringIO()
        with (
            mock.patch.dict(os.environ, {"PYMSS_STUDIO_LOG_TRANSPORT": ""}),
            mock.patch.object(sys, "stdin", stdin),
            mock.patch.object(sys, "stdout", stdout),
            mock.patch.object(sys, "stderr", stderr),
            mock.patch.object(worker, "run_command", side_effect=command),
            mock.patch.object(worker, "rocm_worker_poisoned", return_value=poisoned),
        ):
            result = worker.serve()
        events = [json.loads(line) for line in stdout.getvalue().splitlines()]
        return result, events, stderr.getvalue()

    def test_frames_both_requests_and_keeps_plain_output_off_stdout(self):
        def command(_name, payload):
            print("kernel diagnostic")
            worker.emit("task_done", task_id=payload["taskId"])
            return 0

        code, events, stderr = self.serve(command)
        self.assertEqual(code, 0)
        self.assertIn("kernel diagnostic", stderr)
        self.assertEqual(
            [(event["requestId"], event["type"]) for event in events],
            [
                ("first", "task_done"),
                ("first", "command_finished"),
                ("second", "task_done"),
                ("second", "command_finished"),
            ],
        )

    def test_each_request_reads_the_current_model_catalog(self):
        import worker_models

        with mock.patch.object(worker_models, "reset_model_catalog_cache") as reset:
            code, _, _ = self.serve(lambda _name, _payload: 0)
        self.assertEqual(code, 0)
        self.assertEqual(reset.call_count, 2)

    def test_poisoned_context_exits_even_when_inference_returns_an_error_code(self):
        def command(_name, payload):
            return worker.emit_error("ROCM_CONTEXT_LOST", "Context lost", task_id=payload["taskId"])

        code, events, _ = self.serve(command, poisoned=True)
        self.assertEqual(code, 1)
        self.assertEqual([(event["requestId"], event["type"]) for event in events], [
            ("first", "error"), ("first", "command_finished"),
        ])


if __name__ == "__main__":
    unittest.main()
