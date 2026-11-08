#!/usr/bin/env bash
set -euo pipefail

RUNTIME_DIR="${1:?runtime dir required}"
KEEP_VENV="${2:-}"
BACKEND="cpu"
for prune_option in "${@:2}"; do
  case "$prune_option" in
    --keep-venv) KEEP_VENV="--keep-venv" ;;
    --backend=rocm) BACKEND="rocm" ;;
  esac
done
# Exclude only ROCm payloads from destructive recursive passes. SDK/devel
# archives, headers, device resources and SDK metadata are runtime inputs.
ROCM_EXCLUDES=()
if [[ "$BACKEND" == "rocm" ]]; then
  ROCM_EXCLUDES=(
    -not -path '*/site-packages/_rocm*' -not -path '*/site-packages/rocm*'
    -not -path '*/site-packages/amd_torch_device*' -not -path '*/site-packages/_amd_torch_device*'
    -not -path '*/site-packages/amd_torchvision_device*' -not -path '*/site-packages/_amd_torchvision_device*'
    -not -path '*/site-packages/torch/include' -not -path '*/site-packages/torch/include/*'
  )
fi

rm -rf \
  "$RUNTIME_DIR/Doc" \
  "$RUNTIME_DIR/docs" \
  "$RUNTIME_DIR/include" \
  "$RUNTIME_DIR/libs" \
  "$RUNTIME_DIR/tcl" \
  "$RUNTIME_DIR/pkgs" \
  "$RUNTIME_DIR/envs" \
  "$RUNTIME_DIR/conda-meta" \
  "$RUNTIME_DIR/condabin" \
  "$RUNTIME_DIR/libexec" \
  "$RUNTIME_DIR/shell"
# Keep xmlrpc: pip installation commands require xmlrpc.client.
if [[ "$KEEP_VENV" != "--keep-venv" ]]; then
  find "$RUNTIME_DIR/lib" -maxdepth 2 -type d \( \
    -name 'ensurepip' -o \
    -name 'venv' -o \
    -name 'idlelib' -o \
    -name 'lib2to3' -o \
    -name 'tkinter' -o \
    -name 'turtledemo' \
  \) -prune -exec rm -rf {} + 2>/dev/null || true
else
  find "$RUNTIME_DIR/lib" -maxdepth 2 -type d \( \
    -name 'idlelib' -o \
    -name 'lib2to3' -o \
    -name 'tkinter' -o \
    -name 'turtledemo' \
  \) -prune -exec rm -rf {} + 2>/dev/null || true
fi
find "$RUNTIME_DIR/lib" -maxdepth 2 -type f \( -name 'turtle.py' \) -delete 2>/dev/null || true
# Standalone Python also ships native Tcl/Tk components. Removing only tkinter's
# Python package leaves ELF dependencies that linuxdeploy still tries to bundle.
find "$RUNTIME_DIR/lib" -maxdepth 3 \( -type f -o -type l \) -name '_tkinter*.so' -delete
find "$RUNTIME_DIR/lib" -maxdepth 1 \( -type f -o -type l \) \( \
  -name 'libtcl*.so*' -o -name 'libtk*.so*' -o \
  -name 'libtcl*.dylib' -o -name 'libtk*.dylib' \
\) -delete
find "$RUNTIME_DIR/lib" -maxdepth 1 -type d \( \
  -name 'tcl[0-9]*' -o -name 'tk[0-9]*' -o \
  -name 'itcl[0-9]*' -o -name 'thread[0-9]*' \
\) -prune -exec rm -rf {} +
find "$RUNTIME_DIR" -type d \( -name '__pycache__' -o -name 'test' -o -name 'tests' -o -name 'testsuite' -o -name 'examples' -o -name 'example' -o -name 'benchmarks' -o -name 'docs' -o -name 'doc' \) \
  -not -path '*/site-packages/torch/testing' \
  "${ROCM_EXCLUDES[@]}" \
  -prune -exec rm -rf {} + || true
find "$RUNTIME_DIR" -type f \( -name '*.pyc' -o -name '*.pyo' -o -name '*.a' -o -name '*.la' -o -name '*.h' -o -name '*.hpp' \) "${ROCM_EXCLUDES[@]}" -delete || true
find -L "$RUNTIME_DIR" -type l -exec rm -f {} + || true
find "$RUNTIME_DIR" -path '*/site-packages/torch/*' -type f -name '*.lib' -delete || true
find "$RUNTIME_DIR" -path '*/site-packages/torch/include' -type d "${ROCM_EXCLUDES[@]}" -prune -exec rm -rf {} + || true
find "$RUNTIME_DIR" -path '*/site-packages/torch/share' -type d -prune -exec rm -rf {} + || true
find "$RUNTIME_DIR" -path '*/site-packages/torch/utils/benchmark' -type d -prune -exec rm -rf {} + || true
# Keep pip: runtime core updates invoke the environment interpreter with "-m pip".
# Keep setuptools and its metadata: Torch requires it on Python 3.12+,
# including when pip resolves an update against the installed base offline.
find "$RUNTIME_DIR" -type d -name 'site-packages' -print0 |
  while IFS= read -r -d '' site_packages; do
    find "$site_packages" -maxdepth 1 -type d \( \
      -name 'wheel' -o \
      -name 'wheel-*' \
    \) -prune -exec rm -rf {} + || true
  done
# Keep package identity and installer markers for importlib.metadata, plugin discovery,
# and future pip updates. Remove only WHEEL bookkeeping; ROCm ships many distributions,
# so retaining every wheel metadata file makes the LZMA2 installer enumerate more files.
find "$RUNTIME_DIR" -type d \( -name '*.dist-info' -o -name '*.egg-info' \) "${ROCM_EXCLUDES[@]}" -print0 |
  while IFS= read -r -d '' meta_dir; do
    find "$meta_dir" -maxdepth 1 -type f -iname 'WHEEL' -delete || true
  done
du -sh "$RUNTIME_DIR" || true

# Note: keep stdlib pydoc/pydoc_data. SciPy imports pydoc from runtime code paths.
# Note: do not remove site-packages/torch/testing. PyTorch imports torch.testing during normal startup.
