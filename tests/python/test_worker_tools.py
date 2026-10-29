"""File-tool Operations (pymss.plugins.kit.register_tool) through the worker's run_operation."""

from __future__ import annotations

import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

if __package__:
    from . import _bootstrap as _worker_test_bootstrap  # noqa: F401
else:
    import _bootstrap as _worker_test_bootstrap  # noqa: F401

import worker_operations
import worker_plugins


class WorkerToolsTests(unittest.TestCase):
    def run_tool(self, handler, request_id="tool-1"):
        from pymss.operations import Runtime, _RuntimePluginHandle
        from pymss.plugins import kit

        runtime = Runtime()
        kit.register_tool(_RuntimePluginHandle(runtime, source="plugin:example"), "example.tool", handler, title="Tool", tool="tool")
        output = io.StringIO()
        with tempfile.TemporaryDirectory() as temporary, \
             mock.patch.object(worker_operations, "_runtime", return_value=runtime), \
             mock.patch.object(worker_plugins, "validate_operation"), \
             redirect_stdout(output):
            code = worker_operations.cmd_run_operation({
                "operation": "example.tool", "inputs": {"request": {"operation": "tool"}}, "params": {},
                "outputDir": str(Path(temporary) / "out"), "requestId": request_id,
            })
        return code, [json.loads(line) for line in output.getvalue().splitlines()]

    def test_structured_progress_and_result_keep_the_request_identity(self):
        from pymss.plugins import kit

        def handler(request):
            kit.emit("audio_tool_progress", {"operation": "tool", "completed": 1, "total": 2, "current": "a.wav"})
            return {"succeeded": 1}

        code, events = self.run_tool(handler, "loudness-1")
        self.assertEqual(code, 0, events)
        self.assertEqual([event["type"] for event in events], ["operation_progress", "operation_result"])
        self.assertEqual([event["requestId"] for event in events], ["loudness-1", "loudness-1"])
        progress = events[0]["payload"]
        self.assertEqual((progress["fraction"], progress["message"]), (0.5, "a.wav"))
        self.assertEqual(progress["event"], {"type": "audio_tool_progress", "payload": {
            "operation": "tool", "completed": 1, "total": 2, "current": "a.wav",
        }})
        self.assertEqual(events[-1]["payload"]["outputs"]["report"]["value"], {"succeeded": 1})

    def test_tool_without_progress_emits_only_its_result(self):
        def handler(request):
            return {}

        code, events = self.run_tool(handler)
        self.assertEqual(code, 0, events)
        self.assertNotIn("operation_progress", [event["type"] for event in events])

    def test_recoverable_tool_error_preserves_recovery_payload(self):
        from pymss.plugins import kit

        def handler(request):
            raise kit.ToolError("ASR_MODEL_INCOMPLETE", "model cache incomplete", recoverable=True,
                                extra={"recovery": {"action": "redownload_asr_models", "modelIds": ["model-id"]}})

        code, events = self.run_tool(handler, "asr-1")
        self.assertEqual(code, 1)
        self.assertEqual(events[-1]["requestId"], "asr-1")
        self.assertEqual(events[-1]["payload"]["code"], "ASR_MODEL_INCOMPLETE")
        self.assertTrue(events[-1]["payload"]["recoverable"])
        self.assertEqual(events[-1]["payload"]["recovery"]["modelIds"], ["model-id"])

    def test_optional_backends_are_activated_only_for_declaring_operations(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            plugin = root / "installed/pymss.asr"
            plugin.mkdir(parents=True)
            plugin.joinpath("pyproject.toml").write_text(
                '[tool.pymss.plugin]\nid="pymss.asr"\napi_version=2\n\n'
                '[tool.pymss.studio]\ndeferred_dependency_operations=["pymss.asr.tool"]\n'
            )
            with mock.patch.object(worker_plugins, "_root", return_value=root):
                self.assertTrue(worker_plugins.operation_uses_optional_backend("pymss.asr.tool"))
                self.assertFalse(worker_plugins.operation_uses_optional_backend("pymss.asr.transcribe"))
                self.assertFalse(worker_plugins.operation_uses_optional_backend("other.tool"))


if __name__ == "__main__":
    unittest.main()
