<!-- markdownlint-disable MD013 MD033 MD041 -->

<p align="center">
  <img src="./src/assets/app-logo-unified.png" alt="Pymss Studio" width="120" />
</p>

<h1 align="center">Pymss Studio</h1>

<p align="center">
  A local workspace for music source separation, workflows, batch processing, stem editing, and mix export.
</p>

<p align="center">
  <img alt="Platform Windows" src="https://img.shields.io/badge/Platform-Windows-0078D4?style=flat-square&logo=windows11&logoColor=white" />
  <img alt="Platform macOS" src="https://img.shields.io/badge/Platform-macOS-111111?style=flat-square&logo=apple&logoColor=white" />
  <img alt="Platform Linux" src="https://img.shields.io/badge/Platform-Linux-FCC624?style=flat-square&logo=linux&logoColor=111111" />
</p>

<p align="center">
  <a href="./README.zh-CN.md">简体中文</a>
  ·
  <a href="https://github.com/pymss-project/pymss-studio/releases">Download</a>
  ·
  <a href="https://github.com/pymss-project/pymss">pymss core</a>
</p>

<p align="center">
  <img alt="Tauri 2" src="https://img.shields.io/badge/Tauri-2-24C8DB?style=for-the-badge&logo=tauri&logoColor=white" />
  <img alt="Vue 3" src="https://img.shields.io/badge/Vue-3-42B883?style=for-the-badge&logo=vuedotjs&logoColor=white" />
  <img alt="TypeScript" src="https://img.shields.io/badge/TypeScript-5-3178C6?style=for-the-badge&logo=typescript&logoColor=white" />
  <img alt="License: AGPL-3.0" src="https://img.shields.io/badge/License-AGPL--3.0-111827?style=for-the-badge" />
</p>

<p align="center">
  <img src="./images/screenshots/separate/batch/en-light.webp" alt="Pymss Studio batch audio separation workspace" width="900" />
</p>

## About

Pymss Studio brings the [pymss](https://github.com/pymss-project/pymss) music source-separation engine to a desktop workspace. Download models, import audio, run separation jobs, audition results, and edit stems in one application. Use it for vocal and instrumental extraction, instrument stems, audio cleanup, batch processing, speech transcription, and vocal-to-MIDI conversion.

Separation and editing run locally. Preparing runtimes, models, or optional components requires a network connection; downloaded models can then process local audio.

[Features](#features) · [Download](#download-and-install) · [Quick start](#quick-start) · [Preview](#preview) · [Troubleshooting](#troubleshooting) · [Development](#development-and-verification)

## Features

| Area | Capabilities |
| --- | --- |
| Source separation | Import audio, video, or folders; process batches; select model outputs, device, format, folder layout, and naming rules. |
| Model ensembles | Select multiple models and output stems, configure an ensemble algorithm and weights, and save the combined output. |
| Model library | Search by name, purpose, architecture, or source; switch card/list views; manage favorites, notes, resumable downloads, storage, and default inference parameters. |
| Custom models | Import weights and configurations for supported architectures and use existing models in the separation workflow. |
| Workflows | Arrange separation, audio processing, ensemble, and save steps in the simple editor; use the advanced node editor for flexible connections, parameters, and import/export. |
| Task queue | Track queued and running jobs, progress and logs; cancel or retry jobs and organize completed session entries. |
| Results and projects | Review output stems and processing parameters, audition audio, open output folders, and continue in the editor. |
| Stem editor | Multitrack waveforms, clip editing, transport, volume/pan, mute/solo, fades, track effects, recording, project persistence, asset relinking, and mix export. |
| Audio tools | Format conversion, audio inspection, SDR / SI-SDR, ASR, vocal-to-MIDI conversion, silence slicing, and audio merging. |
| Runtimes and settings | Manage platform-appropriate CPU, CUDA, ROCm, and MLX environments; configure download sources, proxies, concurrency, paths, language, zoom, and themes. |

Available stems, parameters, and devices vary by model and runtime. Acceleration depends on the model, hardware, drivers, and task configuration.

## Download and install

Choose a version on [GitHub Releases](https://github.com/pymss-project/pymss-studio/releases), read its release notes, and check **Assets**. If GitHub is inaccessible, use a mirror linked in those release notes.

> [!NOTE]
> The project is under active development and current releases include prereleases. Read the relevant release notes and back up important projects and settings before upgrading.

### Platforms and runtimes

| Platform / hardware | Runtime or package identifier | Notes |
| --- | --- | --- |
| Windows x64, NVIDIA GPU | `windows-x64-cuda` | CUDA requires a compatible GPU and driver. |
| Windows x64, CPU inference | `windows-x64-cpu` | A starting point when GPU compatibility is uncertain. |
| Windows x64, supported AMD GPU | `windows-x64-rocm` | Check the selected release's GPU and driver requirements; ROCm has a narrower support range. |
| Windows x64, smaller initial download | `windows-x64-online` | Includes the bootstrap runtime; install the required inference environment after first launch. |
| macOS 14+, Apple Silicon | `macos-arm64-mlx` | Uses MLX. Download the DMG and drag the application into `/Applications`. |
| Linux | Build from source | The current release workflow does not produce Linux installers; see development prerequisites below. |
| Intel Mac | No dedicated release package | The existing MLX package targets Apple Silicon; source builds still require checking inference dependency compatibility. |

Release packages include a bootstrap Python runtime, so end users do not need to install system Python separately. Open **Settings → Runtime** to inspect, install, or switch environments supported by your platform.

### File types

| File | Purpose |
| --- | --- |
| Windows `*-setup.exe` | Installer; application data defaults to the user directory. |
| Windows `*.7z` | Portable package. Extract the complete directory and run `Pymss Studio.exe`; keep its runtime and tools directories. |
| macOS `*.dmg` | Apple Silicon installation image. |
| Windows `*-update.zip` | Update package for an existing installation, rather than a first-time installer. Follow the corresponding release notes. |
| Split files such as `*.part-000` | Download every part and follow the release's `SPLIT-ASSETS-README.txt` to merge them before extraction. |

If quarantine attributes prevent a macOS application from a trusted source from opening, apply this command to the installed application:

```bash
xattr -cr '/Applications/Pymss Studio.app'
```

## Quick start

1. **Finish onboarding.** Choose language, theme, data directory, and runtime. The Online package needs to finish installing an inference environment.
2. **Prepare a model.** Open **Models**, download a model for the target source, or import a supported custom model.
3. **Configure separation.** Open **Separate**, import audio/video files or a folder, then choose the model, output stems, directory, and format. Start with the model's default parameters.
4. **Run and review.** Select **Start Separation**, monitor progress and logs in the task queue, and audition the finished stems or open **Results**.
5. **Edit and export.** Open a result in the stem editor, adjust clips, volume, pan, and effects, save the project, and use **Export Mix** to create an audio file.

For a repeatable processing chain, create and save a workflow under **Workflows**, then switch **Separate** to **Workflow** mode. A short input can help confirm the model, device, and outputs before a larger batch.

## Preview

These previews show the main features in English and Chinese, with light and dark themes at 1920 × 1080. Click an image to view it at full size.

| Feature | Light theme | Dark theme |
| --- | --- | --- |
| Batch separation and output configuration | <a href="./images/screenshots/separate/batch/en-light.webp"><img src="./images/screenshots/separate/batch/en-light.webp" alt="Batch separation and output configuration" width="420" /></a> | <a href="./images/screenshots/separate/batch/en-dark.webp"><img src="./images/screenshots/separate/batch/en-dark.webp" alt="Batch separation and output configuration" width="420" /></a> |
| Model library and categories | <a href="./images/screenshots/models/library/en-light.webp"><img src="./images/screenshots/models/library/en-light.webp" alt="Model library and categories" width="420" /></a> | <a href="./images/screenshots/models/library/en-dark.webp"><img src="./images/screenshots/models/library/en-dark.webp" alt="Model library and categories" width="420" /></a> |
| Simple workflow editor | <a href="./images/screenshots/workflows/simple/en-light.webp"><img src="./images/screenshots/workflows/simple/en-light.webp" alt="Simple workflow editor" width="420" /></a> | <a href="./images/screenshots/workflows/simple/en-dark.webp"><img src="./images/screenshots/workflows/simple/en-dark.webp" alt="Simple workflow editor" width="420" /></a> |
| Advanced node workflows | <a href="./images/screenshots/workflows/advanced/en-light.webp"><img src="./images/screenshots/workflows/advanced/en-light.webp" alt="Advanced node workflows" width="420" /></a> | <a href="./images/screenshots/workflows/advanced/en-dark.webp"><img src="./images/screenshots/workflows/advanced/en-dark.webp" alt="Advanced node workflows" width="420" /></a> |
| Results and audio stems | <a href="./images/screenshots/results/en-light.webp"><img src="./images/screenshots/results/en-light.webp" alt="Results and audio stems" width="420" /></a> | <a href="./images/screenshots/results/en-dark.webp"><img src="./images/screenshots/results/en-dark.webp" alt="Results and audio stems" width="420" /></a> |
| Stem editing and mixing | <a href="./images/screenshots/editor/workspace/en-light.webp"><img src="./images/screenshots/editor/workspace/en-light.webp" alt="Stem editing and mixing" width="420" /></a> | <a href="./images/screenshots/editor/workspace/en-dark.webp"><img src="./images/screenshots/editor/workspace/en-dark.webp" alt="Stem editing and mixing" width="420" /></a> |

<details>
<summary>Audio tools, settings, and operation dialogs</summary>

| Feature | Light theme | Dark theme |
| --- | --- | --- |
| Multi-model ensemble configuration | <a href="./images/screenshots/separate/ensemble/en-light.webp"><img src="./images/screenshots/separate/ensemble/en-light.webp" alt="Multi-model ensemble configuration" width="420" /></a> | <a href="./images/screenshots/separate/ensemble/en-dark.webp"><img src="./images/screenshots/separate/ensemble/en-dark.webp" alt="Multi-model ensemble configuration" width="420" /></a> |
| Custom model import | <a href="./images/screenshots/models/import/en-light.webp"><img src="./images/screenshots/models/import/en-light.webp" alt="Custom model import" width="420" /></a> | <a href="./images/screenshots/models/import/en-dark.webp"><img src="./images/screenshots/models/import/en-dark.webp" alt="Custom model import" width="420" /></a> |
| Mix export | <a href="./images/screenshots/editor/export/en-light.webp"><img src="./images/screenshots/editor/export/en-light.webp" alt="Mix export" width="420" /></a> | <a href="./images/screenshots/editor/export/en-dark.webp"><img src="./images/screenshots/editor/export/en-dark.webp" alt="Mix export" width="420" /></a> |
| ASR speech recognition | <a href="./images/screenshots/tools/asr/en-light.webp"><img src="./images/screenshots/tools/asr/en-light.webp" alt="ASR speech recognition" width="420" /></a> | <a href="./images/screenshots/tools/asr/en-dark.webp"><img src="./images/screenshots/tools/asr/en-dark.webp" alt="ASR speech recognition" width="420" /></a> |
| Vocal-to-MIDI conversion | <a href="./images/screenshots/tools/midi/en-light.webp"><img src="./images/screenshots/tools/midi/en-light.webp" alt="Vocal-to-MIDI conversion" width="420" /></a> | <a href="./images/screenshots/tools/midi/en-dark.webp"><img src="./images/screenshots/tools/midi/en-dark.webp" alt="Vocal-to-MIDI conversion" width="420" /></a> |
| Runtime environment management | <a href="./images/screenshots/settings/runtime/en-light.webp"><img src="./images/screenshots/settings/runtime/en-light.webp" alt="Runtime environment management" width="420" /></a> | <a href="./images/screenshots/settings/runtime/en-dark.webp"><img src="./images/screenshots/settings/runtime/en-dark.webp" alt="Runtime environment management" width="420" /></a> |

</details>

Screenshots render the current Vue pages with isolated presentation data. Task progress, download states, and waveforms illustrate the interface and do not represent performance measurements.

## Audio tools

Open **Tools** to use these utilities independently of the separation workflow.

| Tool | Capabilities and input requirements |
| --- | --- |
| Audio conversion | Batch output to WAV, FLAC, MP3, or OGG, with sample rate, channel, and encoding settings. |
| Audio inspection | Read containers, audio streams, codec parameters, and metadata tags with FFprobe. |
| SDR evaluation | Align reference and estimated audio, then calculate per-channel SDR and SI-SDR. Requires corresponding reference audio. |
| ASR | Use FunASR presets or local models to generate TXT, JSON, and SRT. Supported languages vary by preset. |
| Vocal to MIDI | Extract pitch and note timing with GAME's native Torch model. Extract a complete GAME model archive, then select its `model.pt`. |
| Audio slicing | Analyze silence and export clips, with minimum-length, threshold, and boundary-silence settings. |
| Audio merging | Order a directory's files by name, modification time, or a regular expression, then export a WAV with consistent audio settings. |

The optional FunASR component installs on demand; ASR models download and cache on first use. Download a model archive from the [GAME release](https://github.com/openvpi/GAME/releases/tag/v1.0.0) linked in the MIDI tool. **Keep the complete extracted directory: `model.pt` requires `config.yaml` beside it, and language-conditioned models also require `lang_map.json`.** Do not move the weights alone. Language, output, and parameter options depend on the selected tool and model.

## Data, runtimes, and updates

### Data locations

Settings, models, outputs, projects, logs, and temporary files share a data root. Inspect actual paths under **Settings → Data Directory**; model and output directories can be configured separately.

| Distribution | Default data root |
| --- | --- |
| `PYMSS_STUDIO_DATA_ROOT` is set | The supplied path takes priority. |
| Development binary run directly from Cargo `target/debug` or `target/release` | `data/` in the project root. |
| Windows portable package with a `pymss-studio.portable` marker beside the executable | `data/` beside the executable. |
| Regular installed / other release packages | `.pymss-studio/` in the user directory, such as `%USERPROFILE%\.pymss-studio` on Windows. |

The root defaults to `settings/`, `models/`, `outputs/`, `editor-projects/`, `logs/`, and `temp/`. Inference environments are managed separately: Windows / Linux generally use the installation resources' `python-runtime/runtime-envs/`; user-installed macOS environments use `runtime-envs/` under the data root.

**Editor projects reference audio files by path.** Back up the associated audio as well as the project. Relink missing assets in the editor after moving or deleting original outputs. Use the application's migration workflow when changing the model directory.

### Updates and environment management

Open **Settings → About** for version and update information. Official Windows builds that support in-app updates can check and install updates there. Use the release page for other platforms or builds without automatic update support.

Application updates and Python inference environment management are separate operations. Use **Settings → Runtime** to switch or maintain CPU / GPU environments. Dependency combinations are defined in [`python/runtime-manifest.json`](./python/runtime-manifest.json); avoid mixing unrelated Torch or backend versions into the bundled environments.

## Troubleshooting

| Problem | What to check |
| --- | --- |
| Model absent from the separation page | Only downloaded models that support inference appear. Check download status, model support, and runtime readiness, then refresh the model list. |
| GPU missing or inference fails | Confirm the active backend, GPU/driver support, and model compatibility. Compare with CPU inference and inspect task logs. |
| Insufficient GPU or system memory | Keep task concurrency at 1 initially. Reduce batch or chunk size where supported; parameter meanings vary between architectures. |
| Runtime or model download fails | Check download source, method, and proxy under **Settings → Defaults**. Runtime installation also offers a PyPI mirror selector. Retry after confirming connectivity. |
| ASR or MIDI cannot start | Check FunASR installation and ASR model source. For GAME, use the complete extracted directory and check for `config.yaml` and, where needed, `lang_map.json` beside the weights. |
| Missing project assets | Check whether audio files moved or were deleted, relink them in the editor, and save the project again. |
| Update fails or a new version behaves unexpectedly | Record version, package type, and errors; read the release notes. Back up projects and settings before manually installing a complete package. |

For unresolved issues, include application version and package type, OS version, GPU model, separation model, reproduction steps, and relevant log excerpts in [GitHub Issues](https://github.com/pymss-project/pymss-studio/issues). Remove personal paths, proxy credentials, and other sensitive information from logs before sharing.

## Development and verification

### Prerequisites

- Node.js 22, matching CI, and `pnpm@10.33.2`.
- Rust stable / Cargo and [Tauri's platform prerequisites](https://v2.tauri.app/start/prerequisites/): C++ build tools and WebView2 on Windows, Xcode Command Line Tools on macOS, and the appropriate WebKitGTK/system libraries on Linux.
- Python 3.12. Audio features use media tools such as FFmpeg / FFprobe; release scripts stage these tools for packaged builds.

```bash
git clone https://github.com/pymss-project/pymss-studio.git
cd pymss-studio
pnpm install --frozen-lockfile
```

For a first full desktop development session, prepare bootstrap Python, then install and activate an inference environment through onboarding or **Settings → Runtime**. Initial installation needs network access, and download size depends on the backend. These commands prepare the bootstrap interpreter; the application installs Python inference dependencies according to its runtime manifest. Download model weights separately in the model library.

Windows PowerShell:

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install "requests[socks]>=2.32,<3"
$env:PYMSS_STUDIO_PYTHON = (Resolve-Path .\.venv\Scripts\python.exe).Path
pnpm tauri dev
```

macOS / Linux:

```bash
python3.12 -m venv .venv
.venv/bin/python -m pip install "requests[socks]>=2.32,<3"
export PYMSS_STUDIO_PYTHON="$PWD/.venv/bin/python"
pnpm tauri dev
```

You can reuse an existing bundled runtime or activated environment. Overriding bootstrap Python does not force replacement of an active inference environment. Media features in development also need FFmpeg / FFprobe on `PATH` or in a prepared tools directory.

### Commands

| Command | Purpose |
| --- | --- |
| `pnpm dev` | Frontend server on port `1420` for UI development and browser previews. |
| `pnpm tauri dev` | Full desktop application with frontend hot reload and Rust / Python orchestration. |
| `pnpm build` | TypeScript checks and the frontend production build. |
| `pnpm tauri build` | Desktop build; runs the frontend build first. A complete inference runtime must be prepared separately. |
| `pnpm test` / `pnpm test:frontend` | Frontend logic tests. |
| `pnpm test:python` | Python Worker module tests. |
| `pnpm test:rust` | Rust backend tests. |
| `pnpm test:all` | Run the three layers sequentially after preparing their dependencies. |

For documentation changes, verify content and links. Frontend changes should run relevant tests and `pnpm build`; Worker/backend changes should run the corresponding layer's tests. CI uses Node.js 22, Python 3.12, and Rust stable, and runs `pnpm test:all` plus the frontend build.

<details>
<summary>Python Worker debugging and environment variables</summary>

Use a dedicated Python 3.12 environment for standalone Worker debugging. Prepare compatible Torch and `pymss` dependencies for the selected backend first. [`python/requirements.txt`](./python/requirements.txt) describes Worker development dependencies; [`python/requirements-ci.txt`](./python/requirements-ci.txt) describes CI test dependencies. Managed application environments follow the runtime manifest.

```bash
python python/worker.py --help
python python/worker.py env_info
python python/worker.py list_models --payload '{}'
```

These commands use the terminal's current Python interpreter, rather than automatically selecting the desktop application's active environment. Use the bundled or active environment's Python path when verifying a release package.

| Variable | Purpose |
| --- | --- |
| `PYMSS_STUDIO_DATA_ROOT` | Override the application data root. |
| `PYMSS_STUDIO_PYTHON` | Override bootstrap Python. Without an override, the backend tries the bundled runtime, then system `python` on Windows or `python3` elsewhere. Inference may still use an activated environment. |
| `PYMSS_STUDIO_DEFAULT_OUTPUT_DIR` | Worker output fallback when none is specified; the desktop application passes its configured output directory to the Worker. |

</details>

<details>
<summary>Architecture, repository layout, and releases</summary>

| Layer | Stack and responsibilities |
| --- | --- |
| Frontend | Vue 3, TypeScript, Vite, Pinia, Vue Router, Vue I18n, and Naive UI; workflow node editing uses LiteGraph. |
| Desktop backend | Tauri v2 and Rust for dialogs, process orchestration, persistence, updates, and windows. |
| Worker | Python JSON protocol for models, inference, workflows, and audio processing. |
| Separation engine | External `pymss` / `pymss-core` packages. This repository integrates the desktop application rather than implementing core separation algorithms. |

```mermaid
flowchart LR
  UI[Vue / Naive UI] --> API[Tauri / Rust]
  API --> Worker[Python Worker]
  Worker --> Core[pymss / pymss-core]
  Core --> Audio[Separated stems]
  Audio --> Editor
  UI --> Editor[Stem editing and mix export]
```

```text
.
├── src/                   # Views, components, stores, workflows, tools, and i18n
├── src-tauri/             # Rust backend, desktop configuration, and bundled resources
├── python/                # Worker, audio modules, and runtime manifest
├── tests/                 # Frontend/Python tests, manual checks, and fixtures
├── scripts/               # Runtime preparation and release checks
├── .github/workflows/     # CI and Windows/macOS release pipelines
├── installer/             # Windows installer resources
└── images/screenshots/    # English/Chinese light/dark screenshots
```

Windows releases prepare bootstrap Python and a backend environment, build the Tauri executable, stage the Worker/runtime/media tools, and create installers and portable archives. macOS releases prepare arm64 MLX and media tools before creating a DMG.

```powershell
./scripts/prepare-python-runtime.ps1 -Variant cuda -InitialBackend cuda
./scripts/prepare-python-runtime.ps1 -Variant default -InitialBackend cpu
```

These are alternative runtime preparation examples; select one rather than running them sequentially. Preparation writes `python-runtime/`, requires network access, and involves large downloads. See the [Windows](./.github/workflows/release-windows.yml) and [macOS](./.github/workflows/release-macos.yml) workflows for complete staging, signing, validation, and asset splitting.

</details>

## Future directions

- Continue improving Intel / AMD model compatibility, runtime validation, and packaging.
- Explore iOS / Android GPU and NPU inference. Mobile support is a future research direction; no mobile application release is currently provided.

## Community and contributing

Use [GitHub Issues](https://github.com/pymss-project/pymss-studio/issues) for bug reports and [GitHub Discussions](https://github.com/pymss-project/pymss-studio/discussions) for features and usage discussions. Chinese-speaking users can also join the [Pymss Studio community group](https://qm.qq.com/q/YLLou4NucE).

See [CONTRIBUTING.md](./CONTRIBUTING.md) for contribution options and the current policy on external pull requests. Reproduction reports, compatibility checks, documentation, and shared usage experience help improve the project.

## License

Pymss Studio is licensed under the GNU Affero General Public License v3.0. See [LICENSE](./LICENSE). Dependencies, model weights, and third-party tools retain their own licenses and usage terms.
