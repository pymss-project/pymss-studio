"""UTF-8 text conversion using the task-scoped JSON-RPC protocol."""
import json
from pathlib import Path
import sys

MAX_FRAME = 256 * 1024


def receive():
    line = sys.stdin.buffer.readline(MAX_FRAME + 1)
    if not line or len(line) > MAX_FRAME or not line.endswith(b"\n"):
        raise RuntimeError("The host sent an invalid protocol frame")
    value = json.loads(line)
    if not isinstance(value, dict) or value.get("jsonrpc") != "2.0":
        raise RuntimeError("The host sent an invalid protocol message")
    return value


def send(value):
    frame = json.dumps({"jsonrpc": "2.0", **value}, ensure_ascii=True, allow_nan=False)
    if len(frame) + 1 > MAX_FRAME:
        raise RuntimeError("The response exceeds the protocol frame limit")
    print(frame, flush=True)


def reply(request, result):
    send({"id": request["id"], "result": result})


def host_request(identifier, method, params):
    send({"id": identifier, "method": method, "params": params})
    response = receive()
    if response.get("id") != identifier or "error" in response or "result" not in response:
        raise RuntimeError("The host service request failed")
    return response["result"]


def main():
    initialize = receive()
    if initialize.get("method") != "rpc.initialize" or initialize["params"].get("protocolVersion") != 1:
        raise RuntimeError("Unsupported task protocol")
    identity = initialize["params"]
    reply(initialize, {"protocolVersion": 1})
    request = receive()
    if request.get("method") != "operations.run":
        raise RuntimeError("The host did not start an operation")
    try:
        task = host_request("plugin:task", "host.taskInfo", {})
        if task.get("taskId") != identity["taskId"] or task.get("pluginId") != identity["pluginId"]:
            raise RuntimeError("The host returned a different task identity")
        info = host_request("plugin:file", "host.fileInfo", {"input": "source"})
        if info["size"] > 128 * 1024 * 1024:
            raise RuntimeError("The input text file exceeds the size limit")
        params = request["params"]
        content = Path(params["inputs"]["source"]["path"]).read_bytes().decode("utf-8")
        content = content.replace("\r\n", "\n").replace("\r", "\n")
        send({"method": "task.progress", "params": {"fraction": 0.5, "message": "Converting text"}})
        if params["params"]["line_ending"] == "crlf":
            content = content.replace("\n", "\r\n")
        Path(params["outputs"]["document"]["path"]).write_bytes(content.encode("utf-8"))
        reply(request, {"outputs": params["outputs"]})
    except (OSError, UnicodeError, RuntimeError) as error:
        message = "The input file must contain valid UTF-8 text" if isinstance(error, UnicodeError) else str(error)
        send({"id": request["id"], "error": {"code": -32000, "message": message}})
        return 1
    shutdown = receive()
    if shutdown.get("method") != "rpc.shutdown":
        raise RuntimeError("The host did not finish the task protocol")
    reply(shutdown, {})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
