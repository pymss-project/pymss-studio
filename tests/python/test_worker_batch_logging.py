from __future__ import annotations

import io
import json
import logging
import os
import sys
import tempfile
import threading
import types
import unittest
import warnings
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

from . import _bootstrap  # noqa: F401
import worker
import worker_infer
import worker_workflows
from worker_workflow_logs import workflow_logging


class BatchLogAttributionTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.inputs = [self.root / name for name in ("first.wav", "second.wav")]
        for path in self.inputs:
            path.touch()
        self.logger = logging.Logger("batch-sdk", logging.DEBUG)
        self.console = logging.StreamHandler(io.StringIO())
        self.console.setLevel(logging.INFO)
        self.logger.console_handler = self.console
        self.logger.addHandler(self.console)
        pymss = types.ModuleType("pymss")
        pymss.get_separation_logger = lambda: self.logger
        self.enterContext(patch.dict(sys.modules, {"pymss": pymss}))

    def payload(self):
        return {
            "taskId": "first", "output": str(self.root / "outputs"),
            "tasks": [{"taskId": path.stem, "input": str(path)} for path in self.inputs],
        }

    def run_hosted(self, command, payload, run):
        output = io.StringIO()
        root = logging.getLogger()
        previous_handlers = root.handlers[:]
        previous_level = root.level
        with patch.dict(os.environ, {"PYMSS_STUDIO_LOG_TRANSPORT": "protocol"}), \
             patch.object(worker, "run_command", side_effect=lambda _command, data: run(data)), \
             redirect_stdout(output), redirect_stderr(io.StringIO()), warnings.catch_warnings():
            warnings.simplefilter("always")
            result = worker.main(["worker.py", command, "--payload", json.dumps(payload)])
        self.assertEqual(root.handlers, previous_handlers)
        self.assertEqual(root.level, previous_level)
        self.assertEqual(self.logger.handlers, [self.console])
        self.assertEqual(self.console.filters, [])
        return result, [json.loads(line) for line in output.getvalue().splitlines()]

    def log_file(self, task_id):
        logging.getLogger("library.decoder").warning("%s library warning", task_id)
        warnings.warn(f"{task_id} Python warning", UserWarning)
        self.logger.warning("%s SDK warning", task_id)

    def log_background(self):
        logging.getLogger("library.decoder").warning("Background library warning")
        warnings.warn("Background Python warning", UserWarning)
        self.logger.warning("Background SDK warning")

    def assert_attribution(self, events):
        by_message = {}
        for event in events:
            if event["type"] in {"worker_log", "task_log"}:
                message = event["payload"]["message"]
                self.assertNotIn(message, by_message, f"Duplicate log: {message}")
                by_message[message] = event
        for task_id in ("first", "second"):
            for source in ("library", "Python", "SDK"):
                event = by_message[f"{task_id} {source} warning"]
                self.assertEqual(event["taskId"], task_id)
                self.assertEqual(event["payload"]["taskId"], task_id)
                self.assertNotIn("taskScope", event["payload"]["context"])
        for source in ("library", "Python", "SDK"):
            event = by_message[f"Background {source} warning"]
            self.assertIsNone(event["taskId"])
            self.assertNotIn("taskId", event["payload"])
            self.assertEqual(event["payload"]["context"]["taskScope"], "operation")

    def test_separation_batch_binds_root_logs_and_warnings_to_each_file(self):
        def prepare(**_kwargs):
            def process(input_path, _index, _audio_tracks=None):
                task_id = Path(input_path).stem
                self.log_file(task_id)
                if task_id == "second":
                    thread = threading.Thread(target=self.log_background)
                    thread.start()
                    thread.join(timeout=5)
                    self.assertFalse(thread.is_alive())
                return [Path(input_path).name]

            def close():
                logging.getLogger("library.decoder").warning("Shared model released")

            return types.SimpleNamespace(process_folder=process, studio_outputs=lambda: [], close=close)

        with patch.object(worker_infer, "_prepare_separator", side_effect=prepare):
            result, events = self.run_hosted("infer", self.payload(), worker_infer.cmd_infer)
        self.assertEqual(result, 0)
        self.assert_attribution(events)
        release = next(event for event in events if event["payload"].get("message") == "Shared model released")
        self.assertIsNone(release["taskId"])
        self.assertEqual(release["payload"]["context"]["taskScope"], "operation")

    def test_workflow_batch_restores_file_scope_after_a_failure(self):
        def run(_payload, task_id, **_kwargs):
            with workflow_logging(task_id, batch=isinstance(_payload.get("tasks"), list) and bool(_payload["tasks"])):
                self.log_file(task_id)
                if task_id == "first":
                    raise RuntimeError("First input failed")
                thread = threading.Thread(target=self.log_background)
                thread.start()
                thread.join(timeout=5)
                self.assertFalse(thread.is_alive())
            return {}

        def cleanup(_task_id):
            logging.getLogger("library.decoder").warning("Workflow file released")

        payload = {**self.payload(), "workflow": {"nodes": []}}
        with patch.object(worker_workflows, "_run_pymss", side_effect=run), \
             patch.object(worker_workflows, "_cleanup_workflow_file", side_effect=cleanup):
            result, events = self.run_hosted("infer_workflow", payload, worker_workflows.cmd_infer_workflow)
        self.assertEqual(result, 1)
        # Cleanup runs once per file; exclude those repeated messages from the once-only check.
        self.assert_attribution([event for event in events if event["payload"].get("message") != "Workflow file released"])
        releases = [event for event in events if event["payload"].get("message") == "Workflow file released"]
        self.assertEqual(len(releases), 2)
        self.assertTrue(all(event["taskId"] is None for event in releases))
        self.assertTrue(all(event["payload"]["context"]["taskScope"] == "operation" for event in releases))

    def test_empty_workflow_batch_uses_single_file_background_attribution(self):
        def run(payload, task_id, **_kwargs):
            with workflow_logging(task_id, batch=isinstance(payload.get("tasks"), list) and bool(payload["tasks"])):
                thread = threading.Thread(target=self.log_background)
                thread.start()
                thread.join(timeout=5)
                self.assertFalse(thread.is_alive())
            return {}

        payload = {**self.payload(), "tasks": [], "workflow": {"nodes": []}}
        with patch.object(worker_workflows, "_run_pymss", side_effect=run):
            result, events = self.run_hosted("infer_workflow", payload, worker_workflows.cmd_infer_workflow)
        self.assertEqual(result, 0)
        background = [event for event in events if event["payload"].get("message", "").startswith("Background ")]
        self.assertEqual(len(background), 3)
        self.assertTrue(all(event["taskId"] == "first" for event in background))
        self.assertTrue(all(event["payload"]["taskId"] == "first" for event in background))
        self.assertTrue(all("taskScope" not in event["payload"]["context"] for event in background))
