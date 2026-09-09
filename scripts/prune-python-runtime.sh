#!/usr/bin/env bash
set -euo pipefail

RUNTIME_DIR="${1:?runtime dir required}"
KEEP_VENV="${2:-}"

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
if [[ "$KEEP_VENV" != "--keep-venv" ]]; then
  find "$RUNTIME_DIR/lib" -maxdepth 2 -type d \( \
    -name 'ensurepip' -o \
    -name 'venv' -o \
    -name 'idlelib' -o \
    -name 'lib2to3' -o \
    -name 'tkinter' -o \
    -name 'turtledemo' -o \
    -name 'xmlrpc' \
  \) -prune -exec rm -rf {} + 2>/dev/null || true
else
  find "$RUNTIME_DIR/lib" -maxdepth 2 -type d \( \
    -name 'idlelib' -o \
    -name 'lib2to3' -o \
    -name 'tkinter' -o \
    -name 'turtledemo' -o \
    -name 'xmlrpc' \
  \) -prune -exec rm -rf {} + 2>/dev/null || true
fi
find "$RUNTIME_DIR/lib" -maxdepth 2 -type f \( -name 'turtle.py' \) -delete 2>/dev/null || true
find "$RUNTIME_DIR" -type d \( -name '__pycache__' -o -name 'test' -o -name 'tests' -o -name 'testsuite' -o -name 'examples' -o -name 'example' -o -name 'benchmarks' -o -name 'docs' -o -name 'doc' \) \
  -not -path '*/site-packages/torch/testing' \
  -prune -exec rm -rf {} + || true
find "$RUNTIME_DIR" -type f \( -name '*.pyc' -o -name '*.pyo' -o -name '*.a' -o -name '*.la' -o -name '*.h' -o -name '*.hpp' \) -delete || true
find -L "$RUNTIME_DIR" -type l -exec rm -f {} + || true
find "$RUNTIME_DIR" -path '*/site-packages/torch/*' -type f -name '*.lib' -delete || true
find "$RUNTIME_DIR" -path '*/site-packages/torch/include' -type d -prune -exec rm -rf {} + || true
find "$RUNTIME_DIR" -path '*/site-packages/torch/share' -type d -prune -exec rm -rf {} + || true
find "$RUNTIME_DIR" -path '*/site-packages/torch/utils/benchmark' -type d -prune -exec rm -rf {} + || true
# Keep pip: runtime core updates invoke the environment interpreter with "-m pip".
find "$RUNTIME_DIR" -type d -name 'site-packages' -print0 |
  while IFS= read -r -d '' site_packages; do
    find "$site_packages" -maxdepth 1 -type d \( \
      -name 'setuptools' -o \
      -name 'setuptools-*' -o \
      -name 'wheel' -o \
      -name 'wheel-*' \
    \) -prune -exec rm -rf {} + || true
  done
# Keep package identity and installer markers for importlib.metadata, plugin discovery,
# and future pip updates. Remove only WHEEL bookkeeping; ROCm ships many distributions,
# so retaining every wheel metadata file makes the LZMA2 installer enumerate more files.
find "$RUNTIME_DIR" -type d \( -name '*.dist-info' -o -name '*.egg-info' \) -print0 |
  while IFS= read -r -d '' meta_dir; do
    find "$meta_dir" -maxdepth 1 -type f -iname 'WHEEL' -delete || true
  done
du -sh "$RUNTIME_DIR" || true

# Note: keep stdlib pydoc/pydoc_data. SciPy imports pydoc from runtime code paths.
# Note: do not remove site-packages/torch/testing. PyTorch imports torch.testing during normal startup.
