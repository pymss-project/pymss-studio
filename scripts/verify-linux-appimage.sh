#!/usr/bin/env bash
set -euo pipefail

mapfile -d '' IMAGES < <(find src-tauri/target/release/bundle/appimage -maxdepth 1 -name '*.AppImage' -print0)
test "${#IMAGES[@]}" -eq 1
IMAGE="$(realpath "${IMAGES[0]}")"
PROBE_ROOT="$(mktemp -d)"
trap 'rm -rf "$PROBE_ROOT"' EXIT
(cd "$PROBE_ROOT" && "$IMAGE" --appimage-extract > /dev/null)
APPDIR="$PROBE_ROOT/squashfs-root"
# Inspect the GUI bundle without launching GTK or sourcing AppRun hooks.
python3 - "$APPDIR" <<'PY'
from pathlib import Path
import os
import shlex
import sys

appdir = Path(sys.argv[1]).resolve(strict=True)
# Only check AppRun's shared search directories. The media tools also have
# private dependencies under their resource directory, outside the GUI stack.
for directory in ("usr/lib", "usr/lib64", "usr/lib/x86_64-linux-gnu", "lib", "lib64", "lib/x86_64-linux-gnu"):
    for pattern in ("libwayland-client.so*", "libEGL.so*", "libEGL_mesa.so*", "libgbm.so*"):
        for library in (appdir / directory).glob(pattern):
            raise SystemExit(f"Host graphics library must not be bundled in the GUI search path: {library.relative_to(appdir)}")

hooks = appdir / "apprun-hooks"
gtk_hook = hooks / "linuxdeploy-plugin-gtk.sh"
if not gtk_hook.is_file():
    raise SystemExit("AppImage is missing its GTK startup hook")
exports = {}
for line in gtk_hook.read_text(encoding="utf-8").splitlines():
    if line.lstrip().startswith("export "):
        for token in shlex.split(line, comments=True)[1:]:
            name, separator, value = token.partition("=")
            if separator:
                exports[name] = value
if "GIO_EXTRA_MODULES" in exports:
    raise SystemExit("AppImage contains the obsolete GIO_EXTRA_MODULES override")
value = exports.get("GIO_MODULE_DIR", "")
prefix = next((prefix for prefix in ("$APPDIR/", "${APPDIR}/") if value.startswith(prefix)), None)
if prefix is None:
    raise SystemExit("AppImage GIO_MODULE_DIR must be relative to APPDIR")
module_dir = (appdir / value[len(prefix):].lstrip("/")).resolve()
if not module_dir.is_relative_to(appdir) or not module_dir.is_dir():
    raise SystemExit("AppImage GIO_MODULE_DIR does not contain bundled GIO modules")
modules = list(module_dir.glob("*.so"))
if not modules or any(not module.is_file() or not module.resolve().is_relative_to(appdir) for module in modules):
    raise SystemExit("AppImage GIO modules are missing or resolve outside the bundle")
build_paths = [value for key in ("GITHUB_WORKSPACE", "RUNNER_TEMP") if (value := os.environ.get(key))]
for hook in sorted(hooks.glob("*.sh")):
    content = hook.read_text(encoding="utf-8")
    if any(path in content for path in build_paths):
        raise SystemExit(f"AppImage startup hook contains a build-machine path: {hook.name}")
print("AppImage GUI library exclusions and relocatable GIO module paths verified")
PY
RESOURCE_ID="$(find "$APPDIR/usr" -path '*/python-runtime/bootstrap-id' -print -quit)"
test -n "$RESOURCE_ID"
RESOURCES="$(dirname "$(dirname "$RESOURCE_ID")")"
test -f "$RESOURCES/python/worker.py"
test -s "$RESOURCES/resources/plugin-sdk/pymss-sdk.js"
test -s "$RESOURCES/resources/plugin-sdk/pymss-sdk.css"
test -s "$RESOURCES/bin/THIRD_PARTY_TOOLS.txt"
# linuxdeploy also scans ELF files inside resources. Reject a rewritten RPATH
# that ties the cached interpreter to the build machine or the AppImage mount.
while IFS= read -r -d '' binary; do
  [[ "$(file -b "$binary")" == ELF* ]] || continue
  IFS=: read -r -a paths <<< "$(patchelf --print-rpath "$binary")"
  for path in "${paths[@]}"; do
    case "$path" in
      ''|'$ORIGIN'*|'${ORIGIN}'*) ;;
      *) echo "Non-relocatable Python RPATH: $binary -> $path" >&2; exit 1 ;;
    esac
  done
done < <(find "$RESOURCES/python-runtime" -type f -print0)

# Reflect the AppRun library search paths while probing the extracted resources.
export LD_LIBRARY_PATH="$APPDIR/usr/lib:$APPDIR/usr/lib/x86_64-linux-gnu:${LD_LIBRARY_PATH:-}"
export PATH="$RESOURCES/bin:/usr/bin:/bin"
unset PYTHONHOME PYTHONPATH
python3 scripts/verify-native-plugins.py "$APPDIR/usr/bin/pymss-studio" --platform linux
for tool in ffmpeg ffprobe aria2c; do
  test -x "$RESOURCES/bin/$tool"
  if ldd "$RESOURCES/bin/$tool" | grep -q 'not found'; then
    echo "Missing bundled shared library for $tool" >&2
    exit 1
  fi
  # Do not allow a passing probe to borrow FFmpeg/aria2 dependencies from the
  # build host. Disable AppRun's search path to verify the resources' own RPATH.
  while read -r name arrow library remainder; do
    [[ "$arrow" == '=>' ]] || continue
    case "$name" in
      libc.so.*|libm.so.*|libpthread.so.*|libdl.so.*|librt.so.*|libresolv.so.*|libutil.so.*) continue ;;
    esac
    case "$(realpath "$library")" in
      "$APPDIR/"*) ;;
      *) echo "Unbundled tool dependency: $tool -> $name ($library)" >&2; exit 1 ;;
    esac
  done < <(env -u LD_LIBRARY_PATH ldd "$RESOURCES/bin/$tool")
done
"$RESOURCES/bin/ffmpeg" -version
"$RESOURCES/bin/ffprobe" -version
"$RESOURCES/bin/aria2c" --version

# Installed environments must survive removal of the original mounted interpreter.
cp -a "$RESOURCES/python-runtime" "$PROBE_ROOT/stable-python"
unset LD_LIBRARY_PATH
PY="$PROBE_ROOT/stable-python/bin/python3"
export PYTHONDONTWRITEBYTECODE=1
export PYMSS_STUDIO_DATA_ROOT="$PROBE_ROOT/data"
export PYMSS_STUDIO_RUNTIME_ENVS_DIR="$PROBE_ROOT/data/runtime-envs"
export PYMSS_STUDIO_ACTIVE_RUNTIME_FILE="$PYMSS_STUDIO_RUNTIME_ENVS_DIR/active-runtime.json"
export PYMSS_STUDIO_BOOTSTRAP_PYTHON="$PY"
"$PY" -c 'import sys, ssl, venv, requests, socks; assert sys.prefix == sys.base_prefix; print(sys.version)'
"$PY" -m pip install --help > /dev/null
"$PY" "$RESOURCES/python/worker.py" runtime_info --payload '{}'
"$PY" "$RESOURCES/python/worker.py" install_runtime --payload '{"backend":"cpu","mirror":"pypi","locale":"en"}'
ENV_PY="$PYMSS_STUDIO_RUNTIME_ENVS_DIR/cpu/bin/python"
"$ENV_PY" -c 'import sys, pymss, pymss_core, torch; assert sys.prefix != sys.base_prefix; assert torch.version.cuda is None; print(torch.__version__)'
"$ENV_PY" "$RESOURCES/python/worker.py" env_info
"$ENV_PY" "$RESOURCES/python/worker.py" list_models --payload '{}'
"$ENV_PY" "$RESOURCES/python/runtime_audio_probe.py" --check-core
LD_LIBRARY_PATH="$APPDIR/usr/lib:$APPDIR/usr/lib/x86_64-linux-gnu" "$ENV_PY" -c 'import ssl, torch, pymss, pymss_core'
# A different AppImage mount must not become part of the venv configuration.
"$ENV_PY" - "$PROBE_ROOT/stable-python" <<'PY'
import sys
from pathlib import Path
assert Path(sys.base_prefix).resolve() == Path(sys.argv[1]).resolve()
PY
python3 tests/manual/linux_appimage_update_smoke.py "$IMAGE" "$APPDIR"
