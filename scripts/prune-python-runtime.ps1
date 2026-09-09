param(
    [Parameter(Mandatory = $true)]
    [string]$RuntimeDir,
    [switch]$KeepScripts,
    [switch]$KeepVenv
)

$ErrorActionPreference = "Stop"
$runtime = Resolve-Path $RuntimeDir

$removeDirs = @(
    "Doc",
    "docs",
    "include",
    "libs",
    "share",
    "tcl",
    "Lib\idlelib",
    "Lib\lib2to3",
    "Lib\tkinter"
)
if (!$KeepVenv) {
    $removeDirs += @("Lib\ensurepip", "Lib\venv")
}
if (-not $KeepScripts) {
    $removeDirs += "Scripts"
}
foreach ($name in $removeDirs) {
    $path = Join-Path $runtime $name
    if (Test-Path -LiteralPath $path) {
        Remove-Item -LiteralPath $path -Recurse -Force -ErrorAction SilentlyContinue
    }
}

Get-ChildItem -LiteralPath $runtime -Recurse -Force -Directory -ErrorAction SilentlyContinue |
    Where-Object { $_.Name -in @("__pycache__", "test", "tests") } |
    ForEach-Object { Remove-Item -LiteralPath $_.FullName -Recurse -Force -ErrorAction SilentlyContinue }

Get-ChildItem -LiteralPath $runtime -Recurse -Force -File -ErrorAction SilentlyContinue |
    Where-Object { $_.Extension -in @(".pyc", ".pyo", ".a", ".la", ".h", ".hpp") } |
    ForEach-Object { Remove-Item -LiteralPath $_.FullName -Force -ErrorAction SilentlyContinue }

# Windows wheel ships import libraries that are not needed at runtime.
Get-ChildItem -LiteralPath $runtime -Recurse -Force -File -Filter "*.lib" -ErrorAction SilentlyContinue |
    Where-Object { $_.FullName -match "[\\/]site-packages[\\/]torch[\\/]" } |
    ForEach-Object { Remove-Item -LiteralPath $_.FullName -Force -ErrorAction SilentlyContinue }

if ($IsWindows -or $env:OS -eq "Windows_NT") {
    $sitePackages = Join-Path $runtime "Lib\site-packages"
} else {
    $sitePackages = Get-ChildItem -LiteralPath (Join-Path $runtime "lib") -Directory -Filter "python*" -ErrorAction SilentlyContinue |
        ForEach-Object { Join-Path $_.FullName "site-packages" } |
        Where-Object { Test-Path -LiteralPath $_ } |
        Select-Object -First 1
}

if ($sitePackages -and (Test-Path -LiteralPath $sitePackages)) {
    # Keep pip: runtime core updates invoke the environment interpreter with `-m pip`.
    Get-ChildItem -LiteralPath $sitePackages -Force -Directory -ErrorAction SilentlyContinue |
        Where-Object { $_.Name -match "(?i)(^setuptools-|^wheel-|^setuptools$|^wheel$)" } |
        ForEach-Object { Remove-Item -LiteralPath $_.FullName -Recurse -Force -ErrorAction SilentlyContinue }

    Get-ChildItem -LiteralPath $sitePackages -Recurse -Force -Directory -ErrorAction SilentlyContinue |
        Where-Object {
            $_.Name -in @("__pycache__", "test", "tests", "testsuite", "examples", "example", "benchmarks", "docs", "doc") -and
            $_.FullName -notmatch "[\\/]site-packages[\\/]torch[\\/]testing([\\/]|$)"
        } |
        Sort-Object FullName -Descending |
        ForEach-Object { Remove-Item -LiteralPath $_.FullName -Recurse -Force -ErrorAction SilentlyContinue }

    # Keep package identity and installer markers for importlib.metadata, plugin discovery,
    # and future pip updates. Remove only WHEEL bookkeeping; ROCm ships many distributions,
    # so retaining every wheel metadata file makes the LZMA2 installer enumerate more files.
    Get-ChildItem -LiteralPath $sitePackages -Recurse -Force -Directory -ErrorAction SilentlyContinue |
        Where-Object { $_.Name -match "(?i)\.dist-info$|\.egg-info$" } |
        ForEach-Object {
            Get-ChildItem -LiteralPath $_.FullName -Force -File -ErrorAction SilentlyContinue |
                Where-Object {
                    $_.Name -match '^(WHEEL)$'
                } |
                ForEach-Object { Remove-Item -LiteralPath $_.FullName -Force -ErrorAction SilentlyContinue }
        }

}

# Prune torch build metadata that is not needed at runtime.
# Do not remove torch Python subpackages here: modules such as distributed/_functorch
# are imported by PyTorch during normal startup or lazy feature registration.
if ($sitePackages -and (Test-Path -LiteralPath $sitePackages)) {
    $torchDir = Join-Path $sitePackages "torch"
    if (Test-Path -LiteralPath $torchDir) {
        $cmake = Join-Path $torchDir "share\cmake"
        if (Test-Path -LiteralPath $cmake) {
            Remove-Item -LiteralPath $cmake -Recurse -Force -ErrorAction SilentlyContinue
        }
    }
}

$size = (Get-ChildItem -LiteralPath $runtime -Recurse -Force -File -ErrorAction SilentlyContinue | Measure-Object Length -Sum).Sum
Write-Host ("Pruned runtime size: {0:N2} GB" -f ($size / 1GB))
