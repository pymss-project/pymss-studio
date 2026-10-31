# Repository maintenance and SDK reference

Independently installable audio tools for [pymss](https://github.com/pymss-project/pymss) and pymss Studio. Each directory under `plugins/` is a complete plugin package; `registry.json` lists the packages available from this repository.

Commands in this reference run from the repository root unless stated otherwise. For a new plugin, start with the [independent template](../examples/audio_gain/README.md).

## Tools

| Plugin | Studio tool | Processing |
|---|---|---|
| `convert` | Batch format conversion | Sample rate, channels, WAV/FLAC depth, MP3/OGG bitrate |
| `inspect` | Audio information | Full FFprobe container, stream and metadata reports |
| `sdr` | Separation quality | SDR and SI-SDR for reference and estimated audio |
| `slicer` | Audio slicer | Silence detection, batch segmentation and export |
| `merge` | Audio merge | Folder input, natural/modified/regex ordering |
| `loudnorm` | Loudness matching | LUFS, RMS, sample/true peak, reference matching and limiter |
| `asr` | Speech recognition | Model profiles, local models, component management, TXT/JSON/SRT |
| `game` | Vocal to MIDI | GAME model weights, tempo detection and MIDI export |
| `tempo_key` | BPM and key | Studio's shared native analysis service |
| `analysis` | Audio measurement | LUFS, true peak, RMS and spectrum |
| `dynamics` | Dynamics | Compressor, limiter and de-esser |
| `spectral` | Filters and EQ | High-pass, low-pass and peak EQ |
| `timing` | Pitch and time | Pitch shift and time stretch |

Studio ships without preinstalled tool plugins by default. Users browse the official catalog and install the tools they need. Studio declares the default repository and an optional offline preinstallation list in `python/plugin-manifest.json`; an empty list requires no plugin repository access during its build. Existing installed and disabled plugins are preserved. This repository owns plugin source, interfaces, built pages and independent versions.

Plugins own their interfaces, validation, progress, cancellation and structured results. Existing Python capabilities and Operation IDs remain available. Rich file tools use additional `*.tool` Operations. `tempo_key` contributes a view backed by the public native analysis service rather than a Python processing placeholder.

Apple Silicon ASR uses `funasr-mlx` and the available SenseVoice/Paraformer profiles. Its view offers TXT/JSON, without claiming timestamps or hotword support. Other platforms retain the FunASR backend and its full profile/output options. Backend installation and removal are independent of plugin source installation.

## Installation

Use **Studio → Plugins → Install from source** and select an individual directory, such as `plugins/loudnorm`. Installing the collection root is not supported. Enabled, successfully loaded tool views appear automatically in **Tools**.

Independent public GitHub repositories can be installed using their HTTPS repository URL without a local Git installation. Studio downloads an archive of the resolved commit. With no explicit ref, installs and updates track the latest commit on the default branch; the install receipt records the source, selected ref and resolved commit. Other Git sources still require Git.

For the pymss CLI, install a local package directly:

```sh
pymss install ./plugins/loudnorm
pymss plugins list
```

Plugin ids (for example `pymss install pymss.loudnorm`) use the registry configured in pymss. Every package uses the pymss v2 manifest and installs into a directory named after its plugin id. This checkout's `registry.json` points at the official `pymss-project/pymss-plugins` repository; unpublished local changes are available only through a local source installation or the staged Studio resources.

Each package declares dependencies in `pyproject.toml`. Studio's managed installer resolves and validates them. Offline preinstalled ASR and loudness views explicitly defer dependency checks only for the file-tool Operations that own their backend checks; their original Operations still require the declared dependencies.

ASR declares `runtime_packages = ["funasr"]` in `[tool.pymss.studio]` so all its Operations can use the optional ASR components installed by Studio. The host selects FunASR on Windows, Linux and Intel macOS, or `funasr-mlx` on Apple Silicon. This package-wide activation is separate from `deferred_dependency_operations`, which only postpones dependency validation for the named Operations and does not select component directories.

## Plugin author reference

[examples/audio_gain](../examples/audio_gain/README.md) is a complete, independently buildable reference with real audio processing, local English/Chinese translations and a Studio SDK interface. It is kept outside the production catalog. Copy it into your own repository or contribute it under `plugins/<unique-name>/`. Install its own dependencies with `pnpm --dir examples/audio_gain install --frozen-lockfile` before using `pnpm --dir tooling build:example`; `pnpm --dir tooling test:example` runs its Python checks.

See [the development guide](development.md) for publication routes, plugin-owned translations, compatibility boundaries and the remaining multi-source limitations.

## Repository structure and builds

```text
tooling/catalog.toml        Official repository and source ref
registry.json               Generated marketplace catalog
plugins/<name>/
  pyproject.toml             v2 manifest: identity, declared Operations, dependencies, Studio views and localized metadata
  __init__.py                Plugin entry point (setup) and public API exports
  backend/                   Authoritative Python implementation (shared helpers come from pymss.plugins.kit)
  ui/src/                    Vue components, settings and schema configuration
  ui/src/locales/            Plugin-owned page translations; en.json is required
  ui/dist/index.html         Built, self-contained Studio page
  README.md
  LICENSE
tooling/frontend/src/       Shared UI components, theme and Studio SDK adapter
tooling/scripts/            UI build and catalog generation
tooling/tests/              Unit, browser and lifecycle checks
docs/                       Developer and maintainer documentation
```

Python source is maintained directly inside its plugin. There is no backend-source copy step. GAME model code lives under `plugins/game/backend/engine/`. Plugins use published pymss APIs and the declared Studio SDK; they do not import Studio repository files or another plugin's implementation.

Each package declares the Operations its `setup()` registers, and its own Studio UI entry:

```toml
[tool.pymss.plugin]
id = "pymss.convert"
api_version = 2
requires_pymss = ">=2.2"

[[tool.pymss.plugin.operations]]
id = "pymss.convert.batch"
title = "Batch Audio Conversion"

[tool.pymss.studio]
sdk_version = 1
required_capabilities = ["app.info", "settings.read", "settings.write", "files.pickAudio", "files.pickDirectory", "files.scanAudio", "files.reveal", "operations.start", "operations.cancel", "ui.overlay"]

[[tool.pymss.studio.views]]
id = "tool"
title = "Audio Conversion"
component = "ui/src/ConvertTool.vue"
entry = "ui/dist/index.html"
placements = ["tool"]
operation = "pymss.convert.batch"
permissions = ["operations", "files", "settings", "app.info", "ui.overlay"]
```

pymss reads only `[tool.pymss.plugin]`; Studio reads `[tool.pymss.studio]`. Views built by the shared frontend may set `file_tool` to the request name their file-tool Operation expects; it is a build-time value and not part of Studio's plugin identity. The component is compiled at plugin build time. Studio loads the built page and does not compile plugin sources. CLI Operations do not require the UI.

A package may declare `[tool.pymss.studio] build_script = "scripts/build.mjs"` to keep its frontend, translations and build independent. The batch builder runs that package-local Node script (with `--check` during verification); it must produce the declared page and third-party notices. Build scripts must stay inside the package. Studio does not execute them during installation or processing.

The official tooling and standalone template require Python 3.11+ during UI builds to read TOML with `tomllib`. This build requirement is separate from a package's Python runtime range. Both builders inject the SDK requirements from the package manifest into the page, keeping connection checks and catalog metadata consistent.

```sh
pnpm --dir tooling install --frozen-lockfile
pnpm --dir tooling build
pnpm --dir tooling test:frontend
python -m unittest discover -s tooling/tests -v
```

`pnpm --dir tooling build` checks components, builds their self-contained pages and generates `registry.json` (keyed by plugin id, with each plugin's declared Operations) from `plugins/*/pyproject.toml`. Edit package metadata rather than editing catalog entries by hand. `pnpm --dir tooling catalog:check` verifies that the catalog matches its sources.

For a release, increase the versions of changed plugins, build and test them, and commit the package sources and pages. Set `tooling/catalog.toml`'s `repository.ref` to that full commit ID, then run `python tooling/scripts/generate_registry.py --release`. Publish the generated catalog after its source commit is available remotely. Updating an SDK-compatible plugin does not require a Studio release. The development catalog may use `main`; release catalog generation rejects mutable refs.

Studio reads each catalog entry's ref automatically and supports full immutable commit IDs. The current pymss CLI accepts tag refs (`pymss install pymss.sdr@<tag>`) or local package paths after checking out the desired commit. GUI pages remain optional for CLI use.

ToolPanelShell supplies consistent tool headers, forms, results and a log dialog. Studio forwards theme, font and locale changes. A source-validated resize notification keeps the sandbox at its natural content height, with scrolling owned by the outer Tools page.

Builds include third-party license notices. Check built pages with `node tooling/scripts/build_tool_views.mjs --check`. Studio's optional offline packaging validates file hashes; ordinary Studio builds with an empty preinstallation list do not fetch this repository. Develop against a local checkout through Studio's diagnostic plugin-source setting, then install or update the desired plugin through its marketplace.

## Validation

Use Python with `pymss>=2.2`, NumPy, SciPy, pyloudnorm, librosa and CPU PyTorch. The unit tests use fake speech/MIDI backends and do not download model weights:

```sh
python -m unittest discover -s tooling/tests -v
pnpm --dir tooling test:frontend
```

With Playwright and Chromium installed:

```sh
python tooling/tests/manual/tool_views_smoke.py
python tooling/tests/manual/convert_view_smoke.py
python tooling/tests/manual/file_tool_views_smoke.py
python tooling/tests/manual/preinstalled_tools_smoke.py --studio-root ../pymss-studio
```

Browser checks exercise actual packaged pages against a simulated Studio SDK host, including component management, platform-specific ASR controls and cancellation. The preinstalled suite uses a temporary plugin store and real FFmpeg file workflows, plus fake ASR/MIDI inference. It checks disable, remove and explicit restore without modifying the active Studio runtime. Native desktop dialogs, real models and Apple Silicon inference require separate device acceptance.

## Studio SDK

Views run in a sandboxed iframe and communicate through JSON-RPC `postMessage`. Only manifest-granted services are accepted; a view cannot invoke arbitrary Tauri commands.

`sdk_version` declares an exact SDK major, currently `1`, independently of pymss's Python plugin `api_version = 2`. It is required for packages with Studio views; packages without views may omit it and receive `sdkVersion: null` in the catalog. `required_capabilities` lists exact service names; permission families such as `files` grant access but are not capability names. `host.ready` and `app.info` report `sdkVersion` and `capabilities`, and pages verify their manifest requirements before initialization.

Optional package-wide `platforms` (`win32`, `linux`, `darwin`), `architectures` (`x86_64`, `arm64`) and `backends` (`torch`, `mlx`) constrain supported runtime environments. Omitted or empty arrays are unrestricted; `backends` requires any listed base framework, not all of them. Framework presence does not imply an available GPU, optional components or downloaded model weights. The catalog carries these declarations in a normalized `requirements` object for compatibility presentation.

| Method | Parameters | Result |
|---|---|---|
| `app.info` | `{}` | SDK version, capability names, locale, default directories and runtime information |
| `settings.read` | `{}` | Settings for the current plugin/view namespace |
| `settings.write` | `{data: object}` | Saved namespace object |
| `files.pickAudio` | `{multiple?: boolean}` | `{paths: string[]}`, `{path: string}` or `null` |
| `files.pickDirectory` | `{}` | `{path: string}` or `null` |
| `files.pickModel` | `{title?: string}` | `{path: string}` or `null` |
| `files.scanAudio` | `{paths: string[], recursive: boolean}` | Audio scan report |
| `files.reveal` | `{path: string}` | `{ok: true}` |
| `clipboard.writeText` | `{text: string}` | `{ok: true}` |
| `shell.openExternal` | `{url: string}` | Result for an allowed HTTPS documentation host |
| `operations.describe` | `{operation: string}` | Parameter and input/output schema |
| `operations.start` | `{operation, inputs, params, outputDir?}` | Terminal Operation result |
| `operations.cancel` | `{id: number}` using the start RPC ID | Native cancellation acknowledgement |
| `audio.analyze` | `{payload: object}` | Native tempo/key or MIDI BPM result |
| `runtime.packageStatus` | `{package: 'funasr'}` | ASR component status |
| `runtime.managePackage` | `{payload: {package: 'funasr', action, ...}}` | Terminal component status |
| `models.deleteTool` | `{payload: object}` for ASR | Validated model-cache removal result |

Long jobs send `task.progress` notifications. Cancellation terminates the start request with `error.data.code = "TASK_CANCELLED"`; domain errors retain their code and recovery metadata. A cancellation failure does not discard a job still running. The host waits for terminal cleanup before closing a busy view.

### Marketplace names and categories

Each package owns its Studio presentation in `pyproject.toml`. Set `[tool.pymss.studio] category` to `convert`, `analyze`, `recognize`, or `edit`. The same value controls marketplace filtering and Tools navigation; missing categories are shown as Other.

Use `[tool.pymss.studio.locales.en]` and `[tool.pymss.studio.locales."zh-CN"]` for `title`, `description`, optional `tags`, and an `operations` table mapping operation IDs to display names. The catalog generator copies `category`, `locales`, and `version` from the package. Studio switches these labels with its language and falls back to English or the original plugin names.

Run `python -m unittest discover -s tooling/tests -p test_plugin_presentation.py` to check registry/package consistency. Rebuild components with `pnpm --dir tooling build:tools` before publishing. Publish a new package version and regenerate the catalog for remote updates; local development checks additionally compare file contents. Existing installations retain their component files until explicitly updated through the marketplace.
