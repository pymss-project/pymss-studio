# Audio gain plugin

A complete reference for an independently maintained pymss tool. It provides real NumPy gain processing, a Studio page and package-local English/Chinese translations. The directory is intentionally outside `plugins/` and is not published in the official catalog.

See [README.zh-CN.md](README.zh-CN.md) for the Chinese guide.

## Package contents

```text
pyproject.toml             Identity, dependencies, tool metadata and permissions
__init__.py                Operation registration entry point
backend/gain.py            Audio processing using the public pymss API
ui/src/App.vue             User interface
ui/src/locales/            This plugin's translations
ui/src/i18n.ts             Local dictionary discovery and locale matching
ui/src/studio.ts           Typed usage of the host-provided SDK
ui/dist/index.html         Built page, included in the published package
scripts/build.mjs          Standalone UI build and --check verification
tests/                    Python processing and package-contract tests
```

## Build

Use Node.js 22.12 or later, pnpm 10.33.2 and Python 3.11+ (for reading `pyproject.toml` during the UI build):

```sh
pnpm install --frozen-lockfile
pnpm build
```

The output is `ui/dist/index.html`; third-party license notices are written to `THIRD-PARTY-NOTICES.txt`. Commit both outputs with the sources. `node scripts/build.mjs --check` verifies that the outputs match the sources.

The build does not import the official repository's frontend, message dictionaries or Studio source. The page loads `pymss-sdk.js` from Studio at runtime. Opening the HTML as an ordinary web page therefore shows a connection message; install it in Studio to use file selection and processing.

Add languages as `ui/src/locales/<language-code>.json`; the build includes them automatically. `en.json` is required. Locale selection tries an exact match, the base language, another variant of that language, then English. Missing messages also fall back to English. Naive UI's built-in component locale is configured separately in `App.vue` and currently covers Chinese and English.

Python requires a compatible pymss runtime and NumPy. With those already installed:

```sh
python -m unittest discover -s tests -v
```

From the official repository, run `python tooling/tests/manual/reference_plugin_view_smoke.py --studio-root ../pymss-studio` for the shipped SDK/browser contract, and `python tooling/tests/manual/reference_plugin_install_smoke.py --studio-root ../pymss-studio` for a temporary install, real WAV processing, update, disable/enable and uninstall. Neither suite changes the user's installed plugins or downloads models.

## Install and run

In Studio, choose **Plugins → Install from source → Choose folder**, select this directory, and install. It appears under **Tools → Edit**. Choose an audio file and output directory, set gain, and run. Peak protection is enabled by default and reduces overall gain when necessary; it is not a separate limiter algorithm.

The UI demonstrates preferences, live host language/theme changes, file selection, structured results, progress and cancellation, and an operation-log dialog. Cancellation acknowledgement keeps the task pending until its terminal result arrives.

For CLI use, install the built package directory with `pymss install /path/to/audio_gain`. The processing operation is `example.audio_gain.apply`; its input is an audio artifact, its parameters are `gain_db` and `protect_peak`, and its outputs are an audio artifact and a report. CLI processing does not load Vue or the Studio SDK.

## Create your own plugin

1. Create a dedicated Git repository and copy this directory's contents into its root. Each plugin has its own repository; forking the entire official collection is not required.
2. Give the repository/package directory a unique author-prefixed name. Replace `example.audio_gain` in `pyproject.toml`, `backend/gain.py` and `ui/src/studio.ts`; keep the manifest, Python and UI operation IDs identical.
3. Update the project name, metadata, permissions and README. Keep the plugin ID stable after release.
4. Replace the backend and UI, and maintain translations in this directory. No global Studio dictionary edit is needed.
5. Run the processing tests, build the page, and test install/run/cancel/update/disable/uninstall in Studio.
6. Increase the version when publishing changes. Publish sources and built assets before distributing the URL or updating a catalog.

`[tool.pymss.studio] build_script = "scripts/build.mjs"` lets the official repository delegate its batch build to this package's own builder. Studio ignores this build-time field and never runs Node or a package build script during installation.

Users install the independent repository by entering its URL in Studio's **Plugins → Install from source**. Public HTTPS GitHub repositories install without Git. The default follows the latest default-branch commit; an explicit tag can be selected with `https://github.com/author/repository@v0.1.0`. Studio records the source and resolved commit so later updates can identify code changes. Other Git sources require Git. A standalone plugin does not need the official collection's `tooling/`, `examples/` or `registry.json`. Fork the official collection only when contributing the package under `plugins/` for joint maintenance and catalog inclusion. Automatic indexing of external repositories and a multi-source marketplace are not implemented.

## Host contract

The page uses the shipped Studio SDK, checks required methods during connection, and uses bounded startup/settings requests. Long processing tasks remain open until success, failure or cancellation. It does not call Tauri commands directly.

`[tool.pymss.studio]` declares `sdk_version = 1` and the exact `required_capabilities` used by this page. The build injects these requirements from `pyproject.toml`; connection checks the host's `appInfo()` response before reading settings. Keep view permissions aligned with calls: permissions grant access, while required capabilities check that the host implements each service. The SDK major is independent of the Python plugin protocol version.

Optional `platforms` (`win32`, `linux`, `darwin`), `architectures` (`x86_64`, `arm64`) and `backends` (`torch`, `mlx`) declarations restrict supported runtime environments; this NumPy plugin does not need those restrictions. Omitted or empty arrays are unrestricted. A backend declaration means at least one listed base framework is installed, not that a GPU or model is available. New native services require a host update. Existing Python plugins execute local code; iframe restrictions apply to the UI bridge, not to Python execution.

Local installation and update comparison exclude `node_modules`, virtual environments, development caches and `.env` files. Keep user models, credentials and generated user data outside the plugin source tree.
