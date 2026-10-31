# Isolated Python process profiles

Experimental local implementation. Plugin API remains v2; command declarations,
RPC declarations and the task protocol remain version 1. This feature is not in
the published pymss 2.2.0 package. A release must set an appropriate minimum
version before distributing plugins that require it.

Python is an execution environment for the existing `command` and `rpc-process`
adapters. It does not introduce another business protocol or automatically move
legacy Python v2 handlers into an isolated environment.

## Package declarations

Replace an operation's native `targets` table with a package-relative entry:

```toml
[tool.pymss.python]
format_version = 1
lock = "python-lock.json"

[[tool.pymss.rpc_process.operations]]
id = "author.text.normalize"
python = "peer.py"
arguments = []
protocol_version = 1
permissions = ["host.taskInfo", "host.fileInfo"]
```

The rest of the operation schema and v2 contributions are unchanged. The same
`python` field works in command operations with argv references. Native targets
and a Python entry cannot be declared on the same operation. Registration never
falls back to the host interpreter when the profile is absent or invalid.

`python-lock.json` declares a complete local wheel closure:

```json
{
  "formatVersion": 1,
  "requiresPython": ">=3.11",
  "targets": {
    "any": [
      {"path": "wheels/package-1.0-py3-none-any.whl", "sha256": "<64 lowercase hex digits>"}
    ]
  }
}
```

The hash above is a schema illustration, not an installable value. Use actual
wheel hashes. The target keys are `win32`, `darwin` or `linux`, followed by
`-x86_64` or `-arm64`. A matching target replaces the `any` list. Use `any` only
for a compatible common wheel closure; an empty list is valid for stdlib-only
peers. pip validates wheel tags and requires the complete dependency closure to
pass `pip check`. This phase does not resolve requirements from indexes, compile
source distributions, install a different Python version or select a GPU stack.

`project.dependencies` continues to describe the host registration wrapper's
requirements. Isolated peer dependencies belong in the wheel lock. Keeping
heavy imports inside `setup()` would still import them in the host and would
defeat the intended boundary.

## Preparation and execution

```bash
pymss ops prepare-python /path/to/plugin --profiles-dir /stable/path/profiles
```

The public equivalent is `prepare_python_environment(package, profiles,
context=..., on_created=...)`. Preparation creates a fresh `python-<uuid>` profile
at its final location with `venv --copies --without-pip`. The host's pip provisions
the target using `--python`, `--no-index`, `--no-deps`, `--require-hashes` and
`--only-binary=:all:`. Installer Python/PIP environment overrides are removed;
`pip check` verifies closure. The target never persistently borrows host pip or
site-packages. Prepared peers start with `-I`, disabled bytecode writes and UTF-8
I/O. Only their own package and entry directory are added to their import path.

A successful preparation atomically writes `.pymss-python.json` in the package.
The binding records the stable profile and a plan digest that includes the full
lock, target, Python version/build, base interpreter identity and runtime package
resources. Peer code/resources are copied into `profile/package` before the
binding is published. Registered operations pin that profile and digest, so
updates cannot change imports underneath a running peer. Runtime resources are
limited to 10,000 files and 256 MiB; wheel payloads, UI/views, caches, environment
files and managed receipts are excluded from this code snapshot. A failure before atomic binding publication keeps the old binding. If an
interruption happens after publication, the committed environment is retained
to avoid leaving a dangling binding. Moving installed plugin code between an
installed and disabled directory does not move its environment. Moving the
profile store itself requires explicit re-preparation.

The base interpreter must provide `venv`; the host must provide pip with
`--python` support. This has been validated with standard Windows Python 3.12 and a bundled
python-build-standalone 3.12.13 interpreter inside a signed macOS 14 arm64 test
application. The application seal is checked again after plugin execution and
process regressions. Linux, other architectures and frozen Python hosts have
not been validated for this feature. Failure is explicit; there is no
`system-site-packages` or shared-sidecar fallback.

Execution uses the existing Job/process-group supervisor, file staging,
exclusive output publisher, RPC protocol and task-scoped read-only broker. A
shared profile lease covers execution and publication. Normal subprocess
descendants are stopped before the lease is released. This is dependency and
process isolation, not an OS security sandbox.

## Studio transactions and cleanup

Studio prepares the profile from its existing staged install transaction, then
verifies registration before publishing plugin code. An atomic journal protects
each candidate profile until publication. Runtime switching creates a new
profile from the recorded source and lock instead of copying the previous venv.
Updates always honor the isolated lock, including code updates that preserve
legacy sidecars. A failed update retains the previous binding.

Uninstall removes new-execution entry points through the existing inventory and
directory transaction. Idle, unreferenced profiles are retired under an exclusive
lease. A stable external tombstone records reclamation before the directory is
renamed and removed; a partial deletion can be retried without its inner marker.
Running profiles are retained until a later management action retries cleanup.
Read-only catalog queries do not collect profiles or import the runner.

Owners using the public preparation API outside Studio must track references
and call `remove_python_environment(profile)` for superseded profiles. A False
return means reclamation is deferred. Small lock files intentionally outlive
profiles so concurrent processes do not obtain different lock objects for the
same identity. Automatic sharing, deduplication and periodic background GC are
outside this phase.

## Validation

`test/test_python_profiles.py` installs two actual local wheels with conflicting
versions into separate profiles, executes both, verifies the host is invisible,
and covers RPC callbacks, missing closure, hash failure, cancellation,
descendant cleanup and interrupted retirement. Studio transaction tests cover
updates, failed registration rollback, switching runtimes and uninstall.
`examples/python_rpc_text` in pymss-plugins is a stdlib-only reference peer.


## macOS signed application verification (2026-10-31)

A real macOS 14 arm64 CI build exposed two issues that Windows-only validation
could not cover. Darwin may return EPERM when killpg sees only zombie members;
cleanup now defers that error only after observing the gate's exit, reaps it,
and still requires the process group to disappear before success. A real denied
signal to a live gate remains an error; persistent EPERM never proves cleanup.

All runner-owned isolated Python invocations explicitly include `-B`, and pip's
target-interpreter environment retains `PYTHONDONTWRITEBYTECODE=1`. `-I` ignores
that environment flag, so the gate, venv creator, host pip and prefix probe must
set their interpreter flag directly. Without this, stdlib imports can create
bytecode inside a sealed application. Verification checks the same signed app
before and after real Python/Rust callbacks and conflicting wheel environments,
and checks it again after process tests. Re-signing after a task is not used to
hide resource mutation.

CI run 41175441669 passed 23 early process checks and 81 bundled-interpreter
regressions, plus real Worker plugin checks and final codesign verification.
The package is an isolated test build, not a public release or notarized app.
macOS desktop interactions and model inference remain target-machine acceptance
items. No Linux/Intel/frozen-host support claim follows from this result.
