"""A protocol peer used by subprocess integration tests."""
import json
import os
from pathlib import Path
import sys
import time


def read():
    line = sys.stdin.buffer.readline()
    if not line:
        raise SystemExit(3)
    return json.loads(line)


def send(value):
    sys.stdout.write(json.dumps(value) + "\n")
    sys.stdout.flush()


def response(request, value):
    send({"jsonrpc": "2.0", "id": request["id"], "result": value})


scenario = sys.argv[1]
initialize = read()
if scenario == "exit_nonzero":
    print("Backend initialization failed", file=sys.stderr, flush=True)
    raise SystemExit(7)
if scenario == "bad_json":
    print('{"jsonrpc":"2.0","jsonrpc":"2.0"}', flush=True)
    time.sleep(5)
if scenario == "oversized":
    print("x" * (256 * 1024 + 1), flush=True)
    time.sleep(5)
if scenario == "wrong_version":
    response(initialize, {"protocolVersion": 2})
    time.sleep(5)
if scenario == "bool_version":
    response(initialize, {"protocolVersion": True})
    time.sleep(5)
if scenario == "overflow_number":
    print('{"jsonrpc":"2.0","id":"plugin:1","method":"host.taskInfo","params":{"value":1e999}}', flush=True)
    time.sleep(5)
if scenario == "no_initialize":
    time.sleep(5)
response(initialize, {"protocolVersion": 1})
if scenario == "ignore_run":
    time.sleep(5)
run = read()
if scenario == "foreign_response":
    send({"jsonrpc": "2.0", "id": "host:999", "result": {}})
    time.sleep(5)
if scenario == "worker_envelope":
    send({"type": "operation_result", "payload": {"outputs": {}}})
    time.sleep(5)
if scenario == "early_exit":
    raise SystemExit(0)
if scenario == "stdout_eof":
    os.close(1)
    time.sleep(5)
if scenario == "run_error":
    send({"jsonrpc": "2.0", "id": run["id"], "error": {"code": 12, "message": "Input format is invalid"}})
    raise SystemExit(1)
if scenario == "bad_progress":
    send({"jsonrpc": "2.0", "method": "task.progress", "params": {"fraction": 2, "message": "invalid"}})
    time.sleep(5)

results = []
if scenario not in {"no_callback", "wrong_output", "missing_shutdown", "duplicate_response"}:
    method = "host.fileInfo" if scenario in {"file", "denied", "forged_path", "unselected"} else "host.taskInfo"
    params = {"input": "source"} if method == "host.fileInfo" else {}
    if scenario == "forged_path":
        params = {"path": "/outside/private.txt"}
    if scenario == "unselected":
        params = {"input": "other"}
    if scenario == "unknown":
        method = "host.deleteFiles"
    count = 4 if scenario == "concurrent" else 1
    for index in range(count):
        send({"jsonrpc": "2.0", "id": f"plugin:{index}", "method": method, "params": params})
    if scenario == "callback_exit":
        time.sleep(.05)
        raise SystemExit(0)
    if scenario == "pending":
        response(run, {"outputs": run["params"]["outputs"]})
        time.sleep(5)
    for index in range(count):
        results.append(read())
    if scenario == "duplicate":
        send({"jsonrpc": "2.0", "id": "plugin:0", "method": method, "params": params})
        time.sleep(5)
    if scenario == "hang_after_callback":
        time.sleep(5)

for fraction in ([index / 5000 for index in range(5000)] if scenario == "progress_flood" else [0.1, 0.5, 0.8]):
    send({"jsonrpc": "2.0", "method": "task.progress", "params": {"fraction": fraction, "message": "Processing"}})
output = next(iter(run["params"]["outputs"].values()))["path"]
Path(output).write_text(json.dumps({"callbacks": results, "identity": initialize["params"]}), encoding="utf-8")
result = {"outputs": run["params"]["outputs"]}
if scenario == "wrong_output":
    result = {"outputs": {"document": {"kind": "file", "path": "/outside/not-granted.txt"}}}
response(run, result)
if scenario == "duplicate_response":
    response(run, result)
    time.sleep(5)
if scenario == "missing_shutdown":
    raise SystemExit(0)
shutdown = read()
response(shutdown, {})
if scenario == "shutdown_hang":
    time.sleep(5)
