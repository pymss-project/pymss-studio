param(
    [ValidateSet("cuda", "default", "rocm", "mps", "mlx")]
    [string]$Variant = "cuda",
    [string]$Python = "python",
    [string]$RuntimeDir = "python-runtime",
    [string]$TorchVersion = "2.7.1",
    [string]$TorchIndexUrl = "https://download.pytorch.org/whl/cu128",
    [switch]$Minimal,
    [string]$InitialBackend = "",
    [string]$RuntimeEnvsDir = "",
    [switch]$RewriteRuntimeEnvConfigs,
    [switch]$TemplateRuntimeEnvConfigs
)

$ErrorActionPreference = "Stop"
$root = Resolve-Path (Join-Path $PSScriptRoot "..")
$runtime = Join-Path $root $RuntimeDir
$effectiveRuntimeEnvsDir = if ($RuntimeEnvsDir) { $RuntimeEnvsDir } else { Join-Path $runtime "runtime-envs" }
$manifestPath = Join-Path $root "python\runtime-manifest.json"
$runtimeManifest = Get-Content -Raw $manifestPath | ConvertFrom-Json
if ($InitialBackend -eq "mps") {
    # MPS is the historical name; the runtime directory and manifest backend are both mlx.
    $InitialBackend = "mlx"
}

function Get-ManifestCommonRequirements {
    @($runtimeManifest.common.PSObject.Properties |
        Where-Object { $_.Name -notin @("pymss", "pymss-core") } |
        ForEach-Object { [string]$_.Value })
}

function Get-ManifestRequirement {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Name
    )
    $property = $runtimeManifest.common.PSObject.Properties[$Name]
    if (!$property) {
        throw "Runtime manifest is missing common requirement '$Name'"
    }
    return [string]$property.Value
}

function Get-ManifestBackendExtras {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Backend
    )
    $backendProperty = $runtimeManifest.backends.PSObject.Properties[$Backend]
    if (!$backendProperty) { return @() }
    return @($backendProperty.Value.extras | ForEach-Object { [string]$_ })
}

function Resolve-ManifestBackend {
    if ($InitialBackend -in @("mps", "mlx")) { return "mlx" }
    if ($InitialBackend) { return $InitialBackend }
    if ($Variant -in @("mps", "mlx")) { return "mlx" }
    if ($Variant -eq "rocm") { return "rocm" }
    if ($Variant -eq "cuda") { return "cuda" }
    return "cpu"
}

$manifestBackend = Resolve-ManifestBackend
$manifestCommonRequirements = Get-ManifestCommonRequirements
$manifestPymssRequirement = Get-ManifestRequirement "pymss"
$manifestPymssCoreRequirement = Get-ManifestRequirement "pymss-core"
$manifestBackendExtras = Get-ManifestBackendExtras $manifestBackend
$manifestBackendProperty = $runtimeManifest.backends.PSObject.Properties[$manifestBackend]
if (!$manifestBackendProperty -or !$manifestBackendProperty.Value.torch) {
    throw "Runtime manifest is missing torch configuration for backend '$manifestBackend'"
}
$manifestTorch = $manifestBackendProperty.Value.torch
$torchVersionOverride = $PSBoundParameters.ContainsKey("TorchVersion")
$torchIndexOverride = $PSBoundParameters.ContainsKey("TorchIndexUrl")
$effectiveTorchRequirement = if ($torchVersionOverride) {
    if ([string]::IsNullOrWhiteSpace($TorchVersion)) { "torch" } else { "torch==$TorchVersion" }
} else {
    [string]$manifestTorch.requirement
}
$effectiveTorchIndexUrl = if ($torchIndexOverride) {
    [string]$TorchIndexUrl
} else {
    [string]$manifestTorch.indexUrl
}
if ([string]::IsNullOrWhiteSpace($effectiveTorchRequirement) -and $manifestBackend -ne "rocm") {
    throw "Runtime manifest is missing a torch requirement for backend '$manifestBackend'"
}

function Invoke-NativeChecked {
    param(
        [Parameter(Mandatory = $true)]
        [string]$FilePath,
        [Parameter(Mandatory = $true)]
        [object[]]$Arguments
    )

    & $FilePath @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw "$FilePath failed with exit code $LASTEXITCODE"
    }
}

function Rewrite-WindowsRuntimeEnvConfigs {
    param(
        [Parameter(Mandatory = $true)]
        [string]$EnvsDir,
        [Parameter(Mandatory = $true)]
        [string]$PythonRuntimeDir,
        [switch]$Template
    )

    $resolvedEnvsDir = Resolve-Path -LiteralPath $EnvsDir
    $resolvedRuntimeDir = if ($Template) { "__PYMSS_STUDIO_PYTHON_RUNTIME__" } else { (Resolve-Path -LiteralPath $PythonRuntimeDir).Path }
    $pythonExe = Join-Path $resolvedRuntimeDir "python.exe"
    if (!$Template -and !(Test-Path -LiteralPath $pythonExe)) {
        throw "python.exe not found at $pythonExe"
    }

    Get-ChildItem -LiteralPath $resolvedEnvsDir -Directory | ForEach-Object {
        $cfg = Join-Path $_.FullName "pyvenv.cfg"
        if (Test-Path -LiteralPath $cfg) {
            $envDir = if ($Template) { "__PYMSS_STUDIO_RUNTIME_ENV__" } else { $_.FullName }
            $content = @(
                "home = $resolvedRuntimeDir"
                "include-system-site-packages = false"
                "executable = $pythonExe"
                "command = $pythonExe -m venv $envDir"
                ""
            ) -join "`r`n"

            [System.IO.File]::WriteAllText($cfg, $content, [System.Text.UTF8Encoding]::new($false))
            if ($Template) {
                Write-Host "Templated $cfg"
            } else {
                Write-Host "Rewrote $cfg"
            }
        }
    }
}

function Remove-RocmOffloadArchLauncher {
    param(
        [Parameter(Mandatory = $true)]
        [string]$EnvironmentDir
    )

    # ROCm's pip console-script wrapper embeds the build interpreter path. The SDK can use the
    # relocatable native tool shipped under _rocm_sdk_core when this wrapper is absent from Scripts.
    $launcher = Join-Path $EnvironmentDir "Scripts\offload-arch.exe"
    $sitePackages = Join-Path $EnvironmentDir "Lib\site-packages"
    $sdkPackage = Get-ChildItem -LiteralPath $sitePackages -Directory -Filter "_rocm_sdk_core*" | Select-Object -First 1
    if (!$sdkPackage) {
        throw "ROCm SDK core package was not found in $sitePackages"
    }
    $nativeTools = Join-Path $sdkPackage.FullName "lib\llvm\bin"
    $runtimeBin = Join-Path $sdkPackage.FullName "bin"
    if (!(Test-Path -LiteralPath (Join-Path $nativeTools "offload-arch.exe"))) {
        throw "ROCm native offload-arch tool was not found in $nativeTools"
    }
    if (!(Test-Path -LiteralPath $runtimeBin)) {
        throw "ROCm runtime DLL directory was not found in $runtimeBin"
    }
    if (Test-Path -LiteralPath $launcher) {
        Remove-Item -LiteralPath $launcher -Force
        Write-Host "Removed relocatability-breaking ROCm launcher $launcher"
    }
    return @($nativeTools, $runtimeBin)
}

if ($RewriteRuntimeEnvConfigs -or $TemplateRuntimeEnvConfigs) {
    Rewrite-WindowsRuntimeEnvConfigs -EnvsDir $RuntimeEnvsDir -PythonRuntimeDir $RuntimeDir -Template:$TemplateRuntimeEnvConfigs
    exit 0
}

# ---------------------------------------------------------------------------
# InitialBackend mode: create minimal bootstrap + initial backend env
# ---------------------------------------------------------------------------
if ($InitialBackend) {
    Write-Host "=== InitialBackend mode: base runtime + $InitialBackend environment ==="

    # Step 1: Create minimal bootstrap runtime
    if (Test-Path -LiteralPath $runtime) {
        Remove-Item -LiteralPath $runtime -Recurse -Force
    }
    $pythonExe = (Get-Command $Python).Source
    $pythonHome = Split-Path -Parent $pythonExe
    Write-Host "Copying bootstrap Python from $pythonHome"
    robocopy $pythonHome $runtime /E /XD __pycache__ /XF *.pyc | Out-Host
    if ($LASTEXITCODE -gt 7) { throw "robocopy failed with exit code $LASTEXITCODE" }
    $global:LASTEXITCODE = 0
    $runtimePython = Join-Path $runtime "python.exe"
    if (!(Test-Path -LiteralPath $runtimePython)) {
        throw "python.exe was not copied to $runtime"
    }
    $sitePackages = Join-Path $runtime "Lib\site-packages"
    if (Test-Path -LiteralPath $sitePackages) {
        Remove-Item -LiteralPath $sitePackages -Recurse -Force
    }
    New-Item -ItemType Directory -Force -Path $sitePackages | Out-Null
    Invoke-NativeChecked -FilePath $runtimePython -Arguments @('-m', 'ensurepip', '--upgrade')
    Invoke-NativeChecked -FilePath $runtimePython -Arguments @('-m', 'pip', 'install', '--upgrade', 'pip', 'setuptools', 'wheel')
    Write-Host "Bootstrap runtime created at $runtime"

    # Step 2: Create venv for the initial backend
    $envsDir = if ([System.IO.Path]::IsPathRooted($effectiveRuntimeEnvsDir)) { $effectiveRuntimeEnvsDir } else { Join-Path $root $effectiveRuntimeEnvsDir }
    $envDir = Join-Path $envsDir $InitialBackend
    if (Test-Path -LiteralPath $envDir) {
        Remove-Item -LiteralPath $envDir -Recurse -Force
    }
    Write-Host "Creating venv at $envDir"
    Invoke-NativeChecked -FilePath $runtimePython -Arguments @('-m', 'venv', $envDir)
    $envPython = Join-Path $envDir "Scripts\python.exe"
    if (!(Test-Path -LiteralPath $envPython)) {
        throw "venv python.exe was not created at $envPython"
    }
    & (Join-Path $PSScriptRoot "prune-python-runtime.ps1") -RuntimeDir $runtime -KeepVenv
    Invoke-NativeChecked -FilePath $envPython -Arguments @('-m', 'pip', 'install', '--upgrade', 'pip', 'setuptools', 'wheel')

    # Step 3: Install packages for the backend
    $torchRequirement = $effectiveTorchRequirement
    if ($InitialBackend -eq "rocm") {
        $rocmSdkWheels = @($manifestTorch.rocmRequirements | ForEach-Object { [string]$_ })
        Invoke-NativeChecked -FilePath $envPython -Arguments (@('-m', 'pip', 'install', '--no-cache-dir') + $rocmSdkWheels)
        $rocmWheels = @($manifestTorch.requirements | ForEach-Object { [string]$_ })
        Invoke-NativeChecked -FilePath $envPython -Arguments (@('-m', 'pip', 'install', '--no-cache-dir', '--no-deps') + $rocmWheels)
    } elseif ([string]::IsNullOrWhiteSpace($effectiveTorchIndexUrl)) {
        Invoke-NativeChecked -FilePath $envPython -Arguments @('-m', 'pip', 'install', '--no-cache-dir', $torchRequirement)
    } else {
        Invoke-NativeChecked -FilePath $envPython -Arguments @('-m', 'pip', 'install', '--no-cache-dir', $torchRequirement, '--index-url', $effectiveTorchIndexUrl)
    }
    Invoke-NativeChecked -FilePath $envPython -Arguments (@('-m', 'pip', 'install', '--no-cache-dir', '--only-binary=:all:', '--prefer-binary') + $manifestCommonRequirements)
    if ($manifestBackendExtras.Count -gt 0) {
        Invoke-NativeChecked -FilePath $envPython -Arguments (@('-m', 'pip', 'install', '--no-cache-dir') + $manifestBackendExtras)
    }
    Invoke-NativeChecked -FilePath $envPython -Arguments (@('-m', 'pip', 'install', '--no-cache-dir', '--upgrade') + @($manifestPymssRequirement, $manifestPymssCoreRequirement))
    $rocmToolDirs = if ($InitialBackend -eq "rocm") { Remove-RocmOffloadArchLauncher -EnvironmentDir $envDir } else { @() }
    & (Join-Path $PSScriptRoot "prune-python-runtime.ps1") -RuntimeDir $envDir -KeepScripts
    Invoke-NativeChecked -FilePath $envPython -Arguments @('-m', 'pip', '--version')

    # Step 4: Verify the environment
    $previousDontWriteBytecode = $env:PYTHONDONTWRITEBYTECODE
    $previousPath = $env:PATH
    try {
        $env:PYTHONDONTWRITEBYTECODE = "1"
        if ($rocmToolDirs.Count -gt 0) {
            $env:PATH = ($rocmToolDirs + $previousPath) -join ";"
        }
        Invoke-NativeChecked -FilePath $envPython -Arguments @('-c', "import importlib.util, pymss, pymss.graph, torch, librosa, av, yaml, tqdm; print('pymss', getattr(pymss, '__version__', 'unknown'), pymss.__file__); print('torch', torch.__version__, 'cuda', torch.version.cuda, 'cuda_available', torch.cuda.is_available()); print('librosa', librosa.__version__); print('av', av.__version__); print('mlx', importlib.util.find_spec('mlx') is not None)")

        # Step 5: Read manifest version and write state files
        $manifestVersion = $runtimeManifest.manifestVersion

        # Probe the complete manifest state from the environment itself. The bundled state is a
        # cache, so it must describe this interpreter rather than the build machine's active env.
        $manifestBackendExtraNames = @($manifestBackendExtras | ForEach-Object {
                $match = [regex]::Match([string]$_, '^[A-Za-z0-9_.-]+')
                if ($match.Success) { $match.Value }
            })
        $manifestPackageNames = @($runtimeManifest.common.PSObject.Properties | ForEach-Object { $_.Name }) + $manifestBackendExtraNames
        $manifestPackageJson = $manifestPackageNames | ConvertTo-Json -Compress
        $manifestMappingJson = '{"pyyaml":"yaml","pymss-core":"pymss_core","typing-extensions":"typing_extensions"}'
        $probeScript = @'
import importlib.util, json, platform
from importlib import metadata
names = json.loads(%NAMES%)
mapping = json.loads(%MAPPING%)
result = {'pythonVersion': platform.python_version(), 'torchVersion': None, 'torchBackend': 'missing', 'acceleratorAvailable': False, 'packages': {}, 'packageVersions': {}, 'pymssVersion': None, 'pymssCoreVersion': None, 'pymssGraphAvailable': False}
for name in names:
    result['packages'][name] = importlib.util.find_spec(mapping.get(name, name)) is not None
    try:
        result['packageVersions'][name] = metadata.version(name)
    except metadata.PackageNotFoundError:
        result['packageVersions'][name] = None
result['pymssVersion'] = result['packageVersions'].get('pymss')
result['pymssCoreVersion'] = result['packageVersions'].get('pymss-core')
try:
    result['pymssGraphAvailable'] = importlib.util.find_spec('pymss.graph') is not None
except Exception:
    result['pymssGraphAvailable'] = False
try:
    import torch
    result['torchVersion'] = torch.__version__
    result['torchBackend'] = 'rocm' if getattr(torch.version, 'hip', None) else 'cuda' if getattr(torch.version, 'cuda', None) else 'cpu'
    result['acceleratorAvailable'] = torch.cuda.is_available()
except Exception as exc:
    result['torchBackend'] = 'error:' + str(exc)
print(json.dumps(result))
'@.Replace('%NAMES%', $manifestPackageJson).Replace('%MAPPING%', $manifestMappingJson)
        $probeOutput = @(Invoke-NativeChecked -FilePath $envPython -Arguments @('-c', $probeScript))
    } finally {
        if ($null -eq $previousDontWriteBytecode) {
            Remove-Item Env:\PYTHONDONTWRITEBYTECODE -ErrorAction SilentlyContinue
        } else {
            $env:PYTHONDONTWRITEBYTECODE = $previousDontWriteBytecode
        }
        $env:PATH = $previousPath
    }
    $probeJson = $probeOutput |
        ForEach-Object { $_.ToString().Trim() } |
        Where-Object { $_ -match '^\s*\{.*\}\s*$' } |
        Select-Object -Last 1
    if ([string]::IsNullOrWhiteSpace($probeJson)) {
        throw "Runtime probe did not produce a JSON result"
    }
    $probed = $probeJson | ConvertFrom-Json
    $now = (Get-Date).ToUniversalTime().ToString("yyyy-MM-ddTHH:mm:ssZ")

    $envState = @{
        backend = $InitialBackend
        manifestVersion = $manifestVersion
        stateVersion = 2
        installedAt = $now
        pythonVersion = $probed.pythonVersion
        torchVersion = $probed.torchVersion
        torchBackend = $probed.torchBackend
        acceleratorAvailable = $probed.acceleratorAvailable
        packages = $probed.packages
        packageVersions = $probed.packageVersions
        pymssVersion = $probed.pymssVersion
        pymssCoreVersion = $probed.pymssCoreVersion
        pymssGraphAvailable = [bool]$probed.pymssGraphAvailable
    } | ConvertTo-Json -Depth 4
    $envStatePath = Join-Path $envDir "pymss-runtime-state.json"
    Set-Content -Path $envStatePath -Value $envState -Encoding UTF8
    Write-Host "Wrote environment state to $envStatePath"

    # Use relative pythonPath (relative to runtime-envs dir) so it works on any machine
    $relativePythonPath = Join-Path $InitialBackend "Scripts\python.exe"
    $activeState = @{
        backend = $InitialBackend
        manifestVersion = $manifestVersion
        stateVersion = 2
        installedAt = $now
        pythonVersion = $probed.pythonVersion
        torchVersion = $probed.torchVersion
        torchBackend = $probed.torchBackend
        acceleratorAvailable = $probed.acceleratorAvailable
        packages = $probed.packages
        packageVersions = $probed.packageVersions
        pymssVersion = $probed.pymssVersion
        pymssCoreVersion = $probed.pymssCoreVersion
        pymssGraphAvailable = [bool]$probed.pymssGraphAvailable
        pythonPath = $relativePythonPath
        activatedAt = $now
    } | ConvertTo-Json -Depth 4
    $activeRuntimePath = Join-Path $envsDir "active-runtime.json"
    Set-Content -Path $activeRuntimePath -Value $activeState -Encoding UTF8
    Write-Host "Wrote active runtime to $activeRuntimePath"

    & (Join-Path $PSScriptRoot "prune-python-runtime.ps1") -RuntimeDir $envDir -KeepScripts
    Invoke-NativeChecked -FilePath $envPython -Arguments @('-m', 'pip', '--version')
    Write-Host "=== InitialBackend complete: $InitialBackend environment ready ==="
    exit 0
}

# ---------------------------------------------------------------------------
# Standard mode (existing behavior)
# ---------------------------------------------------------------------------
if (Test-Path -LiteralPath $runtime) {
    Remove-Item -LiteralPath $runtime -Recurse -Force
}

$pythonExe = (Get-Command $Python).Source
$pythonHome = Split-Path -Parent $pythonExe
Write-Host "Copying portable Python runtime from $pythonHome"
robocopy $pythonHome $runtime /E /XD __pycache__ /XF *.pyc | Out-Host
if ($LASTEXITCODE -gt 7) { throw "robocopy failed with exit code $LASTEXITCODE" }
$global:LASTEXITCODE = 0

$runtimePython = Join-Path $runtime "python.exe"
if (!(Test-Path -LiteralPath $runtimePython)) {
    throw "python.exe was not copied to $runtime"
}

Invoke-NativeChecked -FilePath $runtimePython -Arguments @('-m', 'pip', 'install', '--upgrade', 'pip', 'setuptools', 'wheel')
if ($Minimal) {
    $sitePackages = Join-Path $runtime "Lib\site-packages"
    if (Test-Path -LiteralPath $sitePackages) {
        Remove-Item -LiteralPath $sitePackages -Recurse -Force
    }
    New-Item -ItemType Directory -Force -Path $sitePackages | Out-Null
    Invoke-NativeChecked -FilePath $runtimePython -Arguments @('-m', 'ensurepip', '--upgrade')
    Invoke-NativeChecked -FilePath $runtimePython -Arguments @('-m', 'pip', '--version')
    Write-Host "Prepared minimal Python runtime without inference dependencies"
    exit 0
}
$torchRequirement = $effectiveTorchRequirement
if ($Variant -eq "rocm") {
    $rocmSdkWheels = @($manifestTorch.rocmRequirements | ForEach-Object { [string]$_ })
    Invoke-NativeChecked -FilePath $runtimePython -Arguments (@('-m', 'pip', 'install', '--no-cache-dir') + $rocmSdkWheels)
    $rocmWheels = @($manifestTorch.requirements | ForEach-Object { [string]$_ })
    Invoke-NativeChecked -FilePath $runtimePython -Arguments (@('-m', 'pip', 'install', '--no-cache-dir', '--no-deps') + $rocmWheels)
} elseif ([string]::IsNullOrWhiteSpace($effectiveTorchIndexUrl)) {
    Invoke-NativeChecked -FilePath $runtimePython -Arguments @('-m', 'pip', 'install', '--no-cache-dir', $torchRequirement)
} else {
    Invoke-NativeChecked -FilePath $runtimePython -Arguments @('-m', 'pip', 'install', '--no-cache-dir', $torchRequirement, '--index-url', $effectiveTorchIndexUrl)
}
Invoke-NativeChecked -FilePath $runtimePython -Arguments (@('-m', 'pip', 'install', '--no-cache-dir', '--only-binary=:all:', '--prefer-binary') + $manifestCommonRequirements)
if ($manifestBackendExtras.Count -gt 0) {
    Invoke-NativeChecked -FilePath $runtimePython -Arguments (@('-m', 'pip', 'install', '--no-cache-dir') + $manifestBackendExtras)
}
Invoke-NativeChecked -FilePath $runtimePython -Arguments (@('-m', 'pip', 'install', '--no-cache-dir', '--upgrade') + @($manifestPymssRequirement, $manifestPymssCoreRequirement))

& (Join-Path $PSScriptRoot "prune-python-runtime.ps1") -RuntimeDir $runtime -KeepVenv
Invoke-NativeChecked -FilePath $runtimePython -Arguments @('-m', 'pip', '--version')
$previousDontWriteBytecode = $env:PYTHONDONTWRITEBYTECODE
$env:PYTHONDONTWRITEBYTECODE = "1"
Invoke-NativeChecked -FilePath $runtimePython -Arguments @('-c', "import importlib.util, pymss, pymss.graph, torch, librosa, av, yaml, tqdm; print('pymss', getattr(pymss, '__version__', 'unknown'), pymss.__file__); print('torch', torch.__version__, 'cuda', torch.version.cuda, 'cuda_available', torch.cuda.is_available()); print('librosa', librosa.__version__); print('av', av.__version__); print('mlx', importlib.util.find_spec('mlx') is not None)")
if ($null -eq $previousDontWriteBytecode) {
    Remove-Item Env:\PYTHONDONTWRITEBYTECODE -ErrorAction SilentlyContinue
} else {
    $env:PYTHONDONTWRITEBYTECODE = $previousDontWriteBytecode
}
& (Join-Path $PSScriptRoot "prune-python-runtime.ps1") -RuntimeDir $runtime -KeepVenv
Invoke-NativeChecked -FilePath $runtimePython -Arguments @('-m', 'pip', '--version')
