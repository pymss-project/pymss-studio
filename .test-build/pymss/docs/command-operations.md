# Experimental command operations

The command adapter adds a native executable backend while retaining the v2
Operation registry, validation, plugin activation and rollback. It has not been
released in pymss 2.2.0. Do not advertise that version as supporting this helper.

```python
from pathlib import Path
from pymss.plugins.kit import register_commands

def setup(plugin):
    register_commands(plugin, Path(__file__).parent)
```

`[tool.pymss.commands]` has `format_version = 1` and an `operations` array.
Each operation contains its existing Operation schema and a command mapping:

| Field | Contract |
| --- | --- |
| `id`, `schema_version`, `title`, `description` | Existing Operation identity and display fields |
| `inputs` | Named `type = "file"` ports, optional lowercase extension filters |
| `params` | Existing scalar Operation parameter schema and defaults |
| `outputs` | Exactly one `type = "file"` with a portable `filename` and optional `media_type` |
| `targets` | `win32`, `darwin`, `linux` plus `x86_64` or `arm64`, mapped to package-relative executables |
| `arguments` | Literal argv strings or a single `input`, `param` or `output` reference |
| `timeout` | Positive finite seconds, at most 86400; default 300 |
| `max_output_bytes` | Maximum declared candidate size; default 256 MiB |
| `node_type` | Optional v2-declared graph node with matching named ports |

The complete Rust template is in the adjacent plugin repository at
`examples/rust_text`. This format is intentionally constrained; unsupported
fields fail at registration. Multiple final files, stdout parsing rules,
implicit PATH lookup, shell commands, environment installation and automatic
source compilation are not part of this first adapter.

`FileArtifact` is a non-audio reference. Command inputs accept it, a file
descriptor (`{"kind":"file","path":"..."}`), a path, or a legacy graph
`StringArtifact` path. They are never decoded as audio. CLI output uses the
JSON file descriptor; Studio maps it to a file result. Graph bridges retain
the artifact and record its persistent path with `kind = "file"`.

The current v2 graph schema uses `STRING` slots for non-audio values. This
keeps old graphs loadable; it does not imply that arbitrary text can be used
as a filename. Execution validates that the referenced input exists. Numeric and boolean graph parameters must retain their typed values in named params; STRING links are not implicitly coerced. `FileArtifact.value` exposes a path to existing STRING consumers. Native
commands require an explicit `OperationContext.workdir`; CLI callers supply
`pymss ops run ... --output-dir ...`.

## Lifetime and output publication

The runner starts a minimal, isolated Python gate and only releases argv after
creating the ownership scope. On Windows the base interpreter avoids venv
redirector races, and the gate verifies membership in the exact named Job
Object. On POSIX it owns a session, watches parent EOF, observes process exit
without reaping the leader before group termination (`waitid` or macOS kqueue), and checks for remaining
live group members. Windows waits for the job's active-process count to reach
zero. Cancellation, timeout and normal completion all close the scope.

Diagnostics use rolling bounded tails while both streams are drained. They
are never parsed as task protocol frames or used to determine output success.
The native program's successful exit and a validated regular output file are
both required. Missing, linked, oversized and non-regular outputs fail before
publication. String parameters cannot introduce native options by starting
with `-` (or `/` for Windows programs); fixed options must be literal argv tokens.

Each call gets private staging under the host's scratch directory. The
existing exclusive output publisher preserves previous user files and handles
collisions. One-file publication is the scope of the guarantee; there is no
multi-file transaction or OS sandbox. The final pre-publication cancellation
check preserves the existing host cancellation policy. A richer cross-process
cancel-versus-publish handshake belongs to the upcoming task control bridge.
Cleanup of temporary files cannot turn an already-published result into a
failed task; Studio also owns scratch reclamation after forced Worker exit.

## Callbacks

This adapter provides host-side task notifications through the existing
`OperationContext.progress` callback and cooperative cancellation checks.
Unmodified command output is diagnostic text. It does not implement reverse
host requests, reusable sessions, event subscriptions or business hooks.
`rpc-process` will define those independently of the plugin API, with explicit
request ownership, deadlines, capability checks, EOF/exit handling and
headless-host behavior. Do not represent a Python function pointer as a
cross-language callback.

## Validation limits

Local tests use Windows x86_64, a real Rust CLI, and the source version of the
adapter. The Studio browser check substitutes native IPC while running actual
Worker command handlers. It is not an installer, signing, updater or
macOS/Linux executable validation. These must pass before public release.
