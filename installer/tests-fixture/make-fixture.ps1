# Generates the installer smoke-test fixture tree under installer/tests-fixture/.
# The staged package mimics the release pipeline output (CUDA variant):
#   - fake app exe, bundled tools, python worker, bootstrap runtime
#   - runtime-envs with a cuda env whose pyvenv.cfg embeds a fake CI path
#   - active-runtime.json (must never be installed)
#   - dev leftovers (.git, __pycache__) that the script must clean
#   - a >260-char deep path to prove long-path support
$ErrorActionPreference = "Stop"
$root = $PSScriptRoot
$stageName = "Pymss-Studio-9.9.9-windows-x64-cuda"
$stage = Join-Path $root ("stage/" + $stageName)
if (Test-Path (Join-Path $root "stage")) { Remove-Item (Join-Path $root "stage") -Recurse -Force }

function Write-Fixture([string]$Rel, [string]$Content = "fixture") {
  $p = Join-Path $stage $Rel
  New-Item -ItemType Directory -Force -Path (Split-Path $p -Parent) | Out-Null
  Set-Content -Path $p -Value $Content -Encoding ASCII
}

Write-Fixture "Pymss Studio.exe" "MZ-fake-exe"
Write-Fixture "README.txt" "Pymss Studio 9.9.9 smoke fixture"
Write-Fixture "bin/aria2c.exe" "fake"
Write-Fixture "bin/ffmpeg.exe" "fake"
Write-Fixture "python/worker.py" "# fixture worker"
Write-Fixture "python-runtime/python.exe" "fake-bootstrap"
Write-Fixture "python-runtime/runtime-envs/active-runtime.json" '{"backend":"cuda"}'
Write-Fixture "python-runtime/runtime-envs/cuda/Scripts/python.exe" "fake-cuda"
Write-Fixture "python-runtime/runtime-envs/cuda/pyvenv.cfg" "home = D:\a\ci-workspace\python-runtime`r`ninclude-system-site-packages = false`r`n"
Write-Fixture "python-runtime/runtime-envs/cuda/Lib/site-packages/pymss/__init__.py" "# pkg"
$seg = "very_long_directory_name_segment_0123456789_" * 4
Write-Fixture ("python-runtime/runtime-envs/cuda/Lib/site-packages/deep/" + $seg + "/" + $seg + "/marker.txt") "deep"
Write-Fixture ".git/HEAD" "ref: refs/heads/main"
Write-Fixture "__pycache__/x.pyc" "pyc"
Write-Host "fixture staged at $stage"
