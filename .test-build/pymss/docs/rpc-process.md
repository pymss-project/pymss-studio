# Experimental one-task RPC process protocol

This adapter is under development and is not included in published pymss
2.2.0. It extends the existing v2 plugin handle; plugin API, process protocol,
declaration format and Studio SDK versions remain separate.

```python
from pathlib import Path
from pymss.plugins.kit import register_rpc_processes

def setup(plugin):
    register_rpc_processes(plugin, Path(__file__).parent)
```

Declarations use `[tool.pymss.rpc_process]` with `format_version = 1` and an
`operations` array. Operation schemas, native target selection, optional v2
graph nodes and one-file publication follow the command adapter. Additional
fields are:

| Field | Meaning |
| --- | --- |
| `protocol_version = 1` | The native program implements the protocol below |
| `permissions` | A unique list drawn from `host.taskInfo`, `host.fileInfo` |
| `host_timeout` | Per-host-request deadline in seconds; default 5, maximum 30 |
| `arguments` | Fixed argv strings only; inputs, params and output references travel over RPC |

The package ID must match the registering handle's source before any
registration is committed. Native peers cannot choose their plugin identity,
operation, task ID, permissions or input grants.

## Transport and lifetime

One native process handles one task. stdin/stdout are UTF-8 NDJSON JSON-RPC
2.0; stderr is a bounded diagnostic tail. stdout logs, Worker event envelopes,
JSON batches, duplicate JSON keys, non-finite numbers and incomplete frames
are rejected. Each complete frame, including its newline, is at most 256 KiB.

The process supervisor still owns startup, timeout, normal descendants and
forced-exit cleanup. A gated process joins its Windows Job before launching
the native program; POSIX retains process-group and parent-lifetime behavior.
The gate releases its own stdout/stderr handles so native protocol EOF is
observable even if the native program remains alive. The POSIX relay uses non-blocking pipe I/O, so a peer that stops reading cannot block parent-EOF detection. Its pending data is capped at 8 MiB; the real POSIX integration case remains unverified on the current Windows host.

There are separate protocol-reader, protocol-writer and bounded host-service
workers. The task loop continues checking cancellation and deadlines while a
host request is executing or the native peer stops reading responses.

This is process supervision, not an OS sandbox. Native code has the user's
permissions. The broker's restricted services do not prevent a program from
using its own operating-system calls.

## Host-to-plugin requests

The host owns IDs `host:1`, `host:2` and `host:3` in sequence. The peer must
reply exactly once with the matching ID and exactly one `result` or `error`.

1. `rpc.initialize`: parameters contain `protocolVersion: 1`, `pluginId`,
   `operationId`, a fresh `taskId`, and declared `permissions`. The required
   result is exactly `{"protocolVersion":1}`.
2. `operations.run`: parameters contain the operation and task IDs, typed file
   inputs (`kind`, `ref`, `path`), validated Operation params and one granted
   output (`kind`, `path`). The successful result is exactly the supplied
   output-reference mapping wrapped in `{"outputs":...}`. It does not itself
   publish a user file.
3. `rpc.shutdown`: parameters are `{}`, and the peer replies with `{}` and
   exits with status zero. An acknowledgement without process exit fails with
   `RPC_EXIT_TIMEOUT`; it does not wait for the entire operation timeout.

Negotiation, shutdown acknowledgement and exit each use the protocol grace
period (currently the lesser of 10 seconds and the operation timeout).
Incomplete protocol EOF gets a short exit grace, then fails. A zero exit code
without the required protocol completion is not task success. Native errors
carry an integer code and a public message; the host forwards a bounded
message without echoing request payloads. Already-received protocol errors are drained before a bare nonzero exit status is reported, retaining the peer error and bounded exit diagnostics.

After protocol completion **and** process-scope cleanup, the host validates
the output as the same declared regular file and publishes it without
overwriting an existing user result. Pending host requests at task completion
are an error. Cancellation before publication rejects the candidate using the
existing Operation cancellation boundary.

## Plugin-to-host requests

Reverse request IDs are strings matching `plugin:[A-Za-z0-9._-]{1,80}` and
cannot be reused within a task. Requests contain exactly `jsonrpc`, `id`,
`method` and an object `params`. Identity and authorization come from the
session, not from request parameters.

| Service | Parameters | Result |
| --- | --- | --- |
| `host.taskInfo` | `{}` | Host name, task/plugin/operation identity, `interactive: false`, available declared read-only capabilities |
| `host.fileInfo` | `{"input":"source"}` | The granted input's name, size and extension |

`host.fileInfo` accepts an input **name**, not an arbitrary path. A different
path, undeclared input, unavailable method or missing permission returns a
structured error. A host that supplies no callback returns `HOST_UNAVAILABLE`;
headless execution never assumes user consent or opens a dialog.

The CLI explicitly provides `ReadonlyHost("cli", operation_id)`. Studio's
Operation Worker provides `ReadonlyHost("studio-worker", operation_id)` only
when the new core API exists. `run_dag(..., host_request=...)` passes an explicit
broker to graph nodes; Studio opts in only when the kernel's signature supports
it. Existing runtimes keep their old kwargs and execution paths. A plain Python
Runtime with no supplied broker can still execute peers that do not need host
services or handle the unavailable response themselves.

Error responses use JSON-RPC numeric codes and `error.data.code` for stable
labels, including `HOST_PERMISSION_DENIED`, `HOST_INVALID_PARAMS`,
`HOST_METHOD_NOT_FOUND`, `HOST_UNAVAILABLE`, `HOST_BUSY`, `HOST_TIMEOUT`,
`HOST_FILE_UNAVAILABLE`, `HOST_CANCELLED` and `HOST_SERVICE_FAILED`.

## Progress callbacks and bounds

`task.progress` is an ID-less notification with `fraction` in `[0,1]` and an
optional message of at most 1024 characters. Progress is coalesced; reliable
control and response messages are not evicted by progress. Messages are
normalized before reaching host text sinks. The runner binds task identity
and maps native progress below the final publication stage. Notifications do
not complete requests or grant access to other tasks.

The reliable incoming and outgoing windows each hold 32 frames. Each task has
at most 128 distinct reverse requests, four active services and a bounded
completion queue. All sessions in a process share an eight-service admission
limit. Busy and denied attempts still consume the request budget: clients
must not busy-spin or retry without backoff.

Host callbacks are synchronous functions executed off the protocol reader.
They must observe `HostRequest.deadline`, `cancelled()` and `check_active()`;
they must return bounded finite JSON. Timeout invalidates the response and
signals cancellation. Late responses are discarded. The supplied host performs
read-only metadata operations, checks activity before and after I/O and does
not expose environment variables, credentials or arbitrary filesystem reads.
Python cannot forcibly interrupt a stuck OS call such as network filesystem
`stat()`. The process-wide admission limit bounds those outstanding workers;
closing a task does not claim such an OS call was interrupted. Studio's
one-command Worker is also reclaimed by its existing parent supervisor.

## Current scope

This is task-scoped reverse RPC, not a generic function-pointer mechanism.
It supports native requests, responses, progress callbacks, deadlines and
closing pending requests on cancellation/exit. It does not yet provide GUI
questions, arbitrary recursive Operation calls, application-wide event
subscriptions, blocking business hooks, persistent sessions or a bidirectional
Tauri-to-Worker interaction channel. These require explicit capabilities,
ownership and product-specific policies before being opened.

`pymss-plugins/examples/rust_rpc_text` contains a real Rust peer using
`serde_json`, a small reusable protocol module and a locked developer build.
Installation consumes prebuilt target binaries and never invokes Cargo. Local
validation is Windows x86_64; macOS/Linux, ARM64 and installed desktop packages
still require their own runtime and signing validation.


RPC file references currently require Unicode filenames representable by both endpoints. Native JSON libraries may reject Python surrogate-escaped filesystem names. The command adapter retains the host's filesystem-encoding path behavior; RPC support for raw, undecodable filename bytes is not implemented.
