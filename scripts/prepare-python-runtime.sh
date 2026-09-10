#!/usr/bin/env bash
set -euo pipefail

VARIANT="${1:-cuda}"
PYTHON_BIN="${PYTHON_BIN:-python3}"
RUNTIME_DIR="${RUNTIME_DIR:-python-runtime}"
RUNTIME_HOME="$(cd "$(dirname "$RUNTIME_DIR")" && pwd)/$(basename "$RUNTIME_DIR")"
TORCH_VERSION_WAS_SET="${TORCH_VERSION+x}"
TORCH_INDEX_URL_WAS_SET="${TORCH_INDEX_URL+x}"
TORCH_VERSION="${TORCH_VERSION-}"
TORCH_INDEX_URL="${TORCH_INDEX_URL-}"
PBS_TAG="${PBS_TAG:-20260602}"
PBS_PYTHON_VERSION="${PBS_PYTHON_VERSION:-3.12.13}"

rm -rf "$RUNTIME_DIR"

RUNTIME_ENVS_DIR="${RUNTIME_ENVS_DIR:-$RUNTIME_DIR/runtime-envs}"
INITIAL_BACKEND="${INITIAL_BACKEND:-}"

MANIFEST_PATH="$(cd "$(dirname "$0")/../python" && pwd)/runtime-manifest.json"
if [[ "$INITIAL_BACKEND" == "mlx" || "$INITIAL_BACKEND" == "mps" ]]; then
  MANIFEST_BACKEND="mlx"
elif [[ -n "$INITIAL_BACKEND" ]]; then
  MANIFEST_BACKEND="$INITIAL_BACKEND"
elif [[ "$VARIANT" == "mlx" || "$VARIANT" == "mps" ]]; then
  MANIFEST_BACKEND="mlx"
elif [[ "$VARIANT" == "rocm" ]]; then
  MANIFEST_BACKEND="rocm"
elif [[ "$VARIANT" == "cuda" ]]; then
  MANIFEST_BACKEND="cuda"
else
  MANIFEST_BACKEND="cpu"
fi

manifest_values() {
  local kind="$1"
  "$PYTHON_BIN" - "$MANIFEST_PATH" "$MANIFEST_BACKEND" "$kind" <<'PY'
import json
import sys

manifest = json.loads(open(sys.argv[1], encoding="utf-8").read())
backend = manifest.get("backends", {}).get(sys.argv[2], {})
kind = sys.argv[3]
if kind == "common":
    for name, requirement in manifest.get("common", {}).items():
        if name not in {"pymss", "pymss-core"}:
            print(requirement)
elif kind == "pymss":
    print(manifest["common"]["pymss"])
elif kind == "pymss-core":
    print(manifest["common"]["pymss-core"])
elif kind == "extras":
    for requirement in backend.get("extras", []) or []:
        print(requirement)
elif kind == "torch-requirement":
    print((backend.get("torch") or {}).get("requirement", ""))
elif kind == "torch-index-url":
    print((backend.get("torch") or {}).get("indexUrl", ""))
PY
}

MANIFEST_COMMON_REQUIREMENTS=()
while IFS= read -r requirement; do
  [[ -n "$requirement" ]] && MANIFEST_COMMON_REQUIREMENTS+=("$requirement")
done < <(manifest_values common)
MANIFEST_PYMSS_REQUIREMENT="$(manifest_values pymss)"
MANIFEST_PYMSS_CORE_REQUIREMENT="$(manifest_values 'pymss-core')"
MANIFEST_TORCH_REQUIREMENT="$(manifest_values torch-requirement)"
MANIFEST_TORCH_INDEX_URL="$(manifest_values torch-index-url)"
MANIFEST_BACKEND_EXTRAS=()
while IFS= read -r requirement; do
  [[ -n "$requirement" ]] && MANIFEST_BACKEND_EXTRAS+=("$requirement")
done < <(manifest_values extras)

if [[ "$TORCH_VERSION_WAS_SET" == "x" ]]; then
  if [[ -n "$TORCH_VERSION" ]]; then
    TORCH_REQUIREMENT="torch==${TORCH_VERSION}"
  else
    TORCH_REQUIREMENT="torch"
  fi
else
  TORCH_REQUIREMENT="$MANIFEST_TORCH_REQUIREMENT"
fi
if [[ "$MANIFEST_BACKEND" != "rocm" && -z "$TORCH_REQUIREMENT" ]]; then
  echo "Runtime manifest is missing a torch requirement for backend $MANIFEST_BACKEND" >&2
  exit 1
fi
if [[ "$TORCH_INDEX_URL_WAS_SET" != "x" ]]; then
  TORCH_INDEX_URL="$MANIFEST_TORCH_INDEX_URL"
fi

if [[ "$OSTYPE" == darwin* ]]; then
  ARCHIVE="cpython-${PBS_PYTHON_VERSION}+${PBS_TAG}-aarch64-apple-darwin-install_only_stripped.tar.gz"
  URL="https://github.com/astral-sh/python-build-standalone/releases/download/${PBS_TAG}/${ARCHIVE//+/%2B}"
  TMP_DIR="$(mktemp -d)"
  trap 'rm -rf "$TMP_DIR"' EXIT
  curl -L --fail --retry 3 --retry-delay 2 -o "$TMP_DIR/pbs.tar.gz" "$URL"
  tar -xzf "$TMP_DIR/pbs.tar.gz" -C "$TMP_DIR"
  mv "$TMP_DIR/python" "$RUNTIME_DIR"
  PY="$RUNTIME_DIR/bin/python3"
  if [[ ! -x "$PY" ]]; then
    echo "Bundled macOS standalone python executable not found in $RUNTIME_DIR/bin/python3" >&2
    exit 1
  fi
else
  "$PYTHON_BIN" -m venv "$RUNTIME_DIR"
  PY="$RUNTIME_DIR/bin/python"
fi

if [[ "$OSTYPE" == darwin* && -z "$INITIAL_BACKEND" && "$VARIANT" != "mlx" && "$VARIANT" != "mps" ]]; then
  PYTHONHOME="$RUNTIME_HOME" "$PY" -m ensurepip --upgrade
  PYTHONHOME="$RUNTIME_HOME" "$PY" -m pip install --upgrade pip setuptools wheel
  bash "$(dirname "$0")/prune-python-runtime.sh" "$RUNTIME_DIR" --keep-venv
  PYTHONHOME="$RUNTIME_HOME" "$PY" -m pip --version
  exit 0
fi

PYTHONHOME="$RUNTIME_HOME" "$PY" -m pip install --upgrade pip setuptools wheel

if [[ -z "$TORCH_INDEX_URL" ]]; then
  PYTHONHOME="$RUNTIME_HOME" "$PY" -m pip install --no-cache-dir "$TORCH_REQUIREMENT"
else
  PYTHONHOME="$RUNTIME_HOME" "$PY" -m pip install --no-cache-dir "$TORCH_REQUIREMENT" --index-url "$TORCH_INDEX_URL"
fi
PYTHONHOME="$RUNTIME_HOME" "$PY" -m pip install --no-cache-dir --only-binary=:all: --prefer-binary "${MANIFEST_COMMON_REQUIREMENTS[@]}"
if [[ "${#MANIFEST_BACKEND_EXTRAS[@]}" -gt 0 ]]; then
  PYTHONHOME="$RUNTIME_HOME" "$PY" -m pip install --no-cache-dir "${MANIFEST_BACKEND_EXTRAS[@]}"
fi
PYTHONHOME="$RUNTIME_HOME" "$PY" -m pip install --no-cache-dir --upgrade "$MANIFEST_PYMSS_REQUIREMENT" "$MANIFEST_PYMSS_CORE_REQUIREMENT"

bash "$(dirname "$0")/prune-python-runtime.sh" "$RUNTIME_DIR" --keep-venv
PYTHONHOME="$RUNTIME_HOME" "$PY" -m pip --version

if [[ "$OSTYPE" == darwin* && "$VARIANT" == "mlx" ]]; then
  mkdir -p "$RUNTIME_ENVS_DIR"
  PYTHONHOME="$RUNTIME_HOME" "$PY" - "$RUNTIME_ENVS_DIR" "$(dirname "$0")/../python/runtime-manifest.json" <<'PY'
import json
import importlib.util
import platform
import sys
from datetime import datetime, timezone
from importlib import metadata
from pathlib import Path

envs = Path(sys.argv[1])
manifest = json.loads(Path(sys.argv[2]).read_text(encoding="utf-8"))
package_versions = {}
packages = {}
for name in (*manifest["common"], "mlx"):
    try:
        package_versions[name] = metadata.version(name)
        packages[name] = True
    except metadata.PackageNotFoundError:
        package_versions[name] = None
        packages[name] = False
if not all(packages.values()):
    missing = [name for name, available in packages.items() if not available]
    raise SystemExit(f"Bundled MLX runtime is missing packages: {missing}")
installed_at = datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
state = {
    "backend": "mlx",
    "manifestVersion": manifest["manifestVersion"],
    "stateVersion": 2,
    "installedAt": installed_at,
    "pythonVersion": platform.python_version(),
    "torchVersion": package_versions.get("torch"),
    "torchBackend": "cpu",
    "acceleratorAvailable": False,
    "packages": packages,
    "packageVersions": package_versions,
    "pymssVersion": package_versions.get("pymss"),
    "pymssCoreVersion": package_versions.get("pymss-core"),
    "pymssGraphAvailable": importlib.util.find_spec("pymss.graph") is not None,
}
(envs / "active-runtime.json").write_text(
    json.dumps({**state, "pythonPath": "../bin/python3", "source": "bundled"}, indent=2) + "\n",
    encoding="utf-8",
)
PY
fi
PYTHONDONTWRITEBYTECODE=1 PYTHONHOME="$RUNTIME_HOME" "$PY" - <<'PY'
import importlib.util
import pymss, pymss.graph, torch, librosa, av, yaml, tqdm
print('pymss', getattr(pymss, '__version__', 'unknown'), pymss.__file__)
print('torch', torch.__version__, 'cuda', torch.version.cuda, 'cuda_available', torch.cuda.is_available())
print('librosa', librosa.__version__)
print('av', av.__version__)
print('mlx', importlib.util.find_spec('mlx') is not None)
PY
bash "$(dirname "$0")/prune-python-runtime.sh" "$RUNTIME_DIR" --keep-venv
PYTHONHOME="$RUNTIME_HOME" "$PY" -m pip --version
