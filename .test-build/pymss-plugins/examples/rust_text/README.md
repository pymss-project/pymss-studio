# Rust text command plugin

This development template normalizes UTF-8 text line endings with a native
Rust program. It exercises the experimental command adapter through pymss's
existing v2 plugin API. It is not listed in the official catalog or bundled
with Studio.

The published pymss 2.2.0 does **not** include `register_commands` or
`FileArtifact`. Use the corresponding local pymss changes and a Studio build
that advertises `files.pickFile`. The package's `requires_pymss >=2.2` expresses
the underlying v2 contract; `setup()` also checks the experimental helper.
The minimum released version must be updated before distributing this plugin.

## Package responsibilities

- `native/main.rs`: the algorithm and existing CLI argument contract. It uses
  only Rust's standard library and does not link to Python or pymss.
- `pyproject.toml`: the Operation schema, argv mapping, platform executables,
  graph contribution and Studio permissions.
- `__init__.py`: one call to `register_commands`; it contains no subprocess or
  algorithm wrapper.
- `ui/index.html`: a small SDK page using a generic file picker, an output
  directory, task notifications, cancellation and a file result.

Command declarations have `format_version = 1`; this is independent of
`api_version = 2` and `sdk_version = 1`. The command adapter is still experimental.
Keeping this compatibility entry does not mean a new language requires a new
plugin API version.

## Build for development

Run this explicitly from a development machine with Rust installed:

```sh
python scripts/build.py
```

The script builds the current platform and architecture into `bin/<target>/`.
It does not install Rust, use Cargo, download dependencies or cross-compile.
Normal plugin installation never invokes this script. A distributable package
must include the executable declared for its target. The loader rejects an
unbuilt source checkout before activation.

For distribution, build each supported target in CI and include its prebuilt
binary. The target entries in this template name possible artifacts; they do
not assert that all six binaries have been built or tested. Windows x86_64 has
been tested locally; macOS/Linux and ARM64 need their own build and runtime
validation. Native signing, system-library requirements and minimum OS
versions remain packaging responsibilities.

## Run through the shared operation contract

Point `PYMSS_PLUGINS_DIR` at a directory containing this package, then run:

```sh
pymss ops describe example.rust_text.normalize
pymss ops run example.rust_text.normalize --input source=input.txt --param line_ending=lf --output-dir output
```

The result contains a file descriptor with `kind`, `path` and `mediaType`.
`--output-dir` is required for command operations. A graph can feed a
`StringConstant` path into `example_rust_text_normalize`; its file output can
feed another file command and is recorded as a persistent graph result.

Install the built local directory through Studio's existing plugin manager to
open its tool page. The program is never launched just to describe the
operation. Registration checks that the current platform's executable exists.

## Execution and callback boundaries

Arguments are passed as an argv array without a shell. `{input=...}`,
`{param=...}` and `{output=...}` are typed references, not string interpolation.
File inputs resolve to existing absolute paths. String values starting with
`-` (or `/` on Windows) are rejected; fixed command options belong in literal argv entries.

The native program receives a private output path and must create exactly one
declared regular file. After exit and process-scope cleanup, pymss validates
the candidate and publishes it without overwriting existing user files. Empty
text files are valid. Native code runs with user permissions; the staging
directory and SDK permissions are not an OS sandbox.

The adapter reports start and publication stages through `OperationContext`;
the page observes them through the existing task progress callback. Raw native
stdout/stderr are bounded diagnostic tails, never trusted Worker messages.
This command adapter does **not** provide arbitrary callback functions,
structured progress from an unmodified CLI, reverse host requests, application
hooks or reusable sessions. Those belong to the later `rpc-process` protocol.

Timeout and cancellation terminate normal descendants. Windows uses a named
Job Object verified by the gated process; POSIX uses an owned process group
and parent-lifetime control pipe. This is process supervision, not protection
against a program deliberately escaping its ownership scope.
