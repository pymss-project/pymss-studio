# AGENTS.md

Guidance for future OpenCode sessions working on pymss-desktop.

## Architecture

Tauri desktop app for audio source separation. Three layers:
- **Frontend**: Vue 3 + TypeScript + Vite + Naive UI (component auto-import)
- **Backend**: Tauri (Rust) orchestrates Python worker
- **Worker**: `python/worker.py` wraps the separate [pymss-project/pymss](https://github.com/pymss-project/pymss) library

The desktop app is a wrapper — core separation logic lives in the external pymss package.

## Development Commands

```bash
# Frontend only (port 1420)
pnpm dev

# Full app (Tauri + frontend hot reload)
pnpm tauri dev

# Typecheck + build frontend
pnpm build

# Production Tauri build (runs pnpm build first)
pnpm tauri build
```

Always run `pnpm build` before committing frontend changes to catch type errors.

## Python Worker Layout

`python/worker.py` imports the published `pymss` package from the active Python environment.
Install it for local development with `python -m pip install pymss`.

**Portable/release layout**:
```
<root>/python/worker.py
<root>/python-runtime/python.exe  (embedded interpreter)
```

## Release Process

Windows builds come in two variants: **CUDA** and **CPU** (different PyTorch builds).

Build stages:
1. **Prepare**: Run `scripts/prepare-python-runtime.ps1` to embed Python + deps → `python-runtime/`
2. **Build**: `pnpm tauri build --no-bundle` produces exe
3. **Stage**: Assemble portable directory with exe, worker, embedded Python runtime, and tools (ffmpeg, VC redist)
4. **Package**: Create 7z archive or Inno Setup installer

CI splits jobs: `prepare` → `7z` / `exe` for each variant to parallelize compression.

Key script: `scripts/prepare-python-runtime.ps1 -Variant cuda|default -TorchVersion ... -TorchIndexUrl ...`

Staged build verification runs `python worker.py env_info` and `list_models` to confirm Python environment.

## Release Artifacts

GitHub releases include software packages smaller than 2 GiB, required update archives, the version-pinned macOS repair manifest, and public checksums. Larger packages remain single complete files on Hugging Face and ModelScope, linked from the release notes. Runtime wheels are shared by content hash under each mirror's `wheels/` directory and are not uploaded as software release assets.

## Component Auto-Import

`unplugin-vue-components` with `NaiveUiResolver` auto-imports Naive UI components. No manual imports needed for NButton, NCard, etc. Typings are disabled (`dts: false`) to avoid conflicts.

## Environment Variables

- `PYMSS_STUDIO_PYTHON`: Python interpreter path (defaults to `python`)
- `PYMSS_STUDIO_DEFAULT_OUTPUT_DIR`: Default separation output directory

## Important Files

- `python/worker.py`: JSON-based worker protocol for model ops, separation, audio processing
- `python/requirements.txt`: Documents runtime deps (actual deps installed by prepare script)
- `src-tauri/tauri.conf.json`: Bundles `python/*` as resources, defines asset protocol scope
- `scripts/prepare-python-runtime.ps1`: Stages portable Python with PyTorch + deps
- `.gitignore`: Excludes `*.md` and `docs/` — this file is an exception

## Testing

Automated tests are split by layer: `pnpm test` covers frontend logic and `pnpm test:python` covers Python Worker modules. Verification also includes manual checks and CI smoke tests (`env_info`, `list_models`).
