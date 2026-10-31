# Rust RPC text plugin

This development template converts UTF-8 text line endings while exercising
task-scoped reverse RPC. It requires the unpublished pymss RPC process adapter
and a Studio build that supports generic file inputs. PyPI pymss 2.2.0 does not
include this adapter. Update the minimum released version before distribution;
the current setup performs an explicit feature check.

The plugin remains API v2. Its process protocol and declaration format are both
independently versioned as 1. It is not in the official catalog or default
tool set.

## Callback flow

1. The host initializes the process with fixed plugin, operation and task IDs.
2. The process receives `operations.run` with file references and validated
   parameters.
3. Rust calls `host.taskInfo`, verifies task ownership, then calls
   `host.fileInfo` for its named `source` input. These are request/response
   callbacks, not output events.
4. Rust converts the text, reports `task.progress`, and returns the exact
   granted output reference.
5. After `rpc.shutdown` acknowledgement and zero exit, the host validates and
   publishes the file without overwriting earlier results.

`native/src/protocol.rs` is a small language-side protocol helper; the algorithm
stays in Rust. `setup()` only calls the public registration helper. The UI uses
the existing Studio SDK and does not handle native process messages directly.
The protocol contract is documented in the adjacent pymss repository at
`docs/rpc-process.md`.

## Development build

```sh
python scripts/build.py
```

Cargo uses the committed lockfile and writes build caches under `.cache`,
which is excluded from Studio's source acquisition. The resulting current
platform binary is copied to `bin/<platform-architecture>/`. If the dependency
cache is already present, `python scripts/build.py --offline` prevents Cargo
network access. Developers need Rust/Cargo; normal plugin installation never
compiles source or installs a toolchain.

`serde_json` is pinned in Cargo.toml and resolved with checksums in Cargo.lock.
Package only the needed prebuilt native target for distribution. The six target
names in the manifest are possible mappings, not evidence of six tested builds.
Current local evidence is Windows x86_64 only. Other platforms require actual
build, dependency, launch and signing verification.

## Run

Point `PYMSS_PLUGINS_DIR` at a directory containing this package and use the
local core with RPC support:

```sh
pymss ops run example.rust_rpc_text.normalize --input source=input.txt --param line_ending=lf --output-dir output
```

For Python graph calls, explicitly pass
`host_request=ReadonlyHost("graph")` to `run_dag`. Studio supplies its read-only
broker automatically when the selected kernel supports it. A host with no
callback service responds `HOST_UNAVAILABLE`; this plugin requires both
declared services and therefore fails clearly on such a host.

The permitted services expose task metadata and selected-file metadata only.
They cannot open dialogs, read arbitrary paths, call other operations or
subscribe to global application events. Native code still has user-level OS
permissions: process isolation and broker permissions are not an OS sandbox.

RPC stdout is reserved for bounded JSON-RPC frames; use stderr for diagnostics.
A command-only program that prints progress lines is not automatically an RPC
peer. Use the separate `rust_text` template for an unmodified CLI.
