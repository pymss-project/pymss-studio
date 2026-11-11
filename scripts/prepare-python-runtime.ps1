param(
    [ValidateSet("cuda", "default", "rocm", "mps", "mlx")]
    [string]$Variant = "cuda",
    [string]$Python = "python",
    [string]$RuntimeDir = "python-runtime",
    [string]$TorchVersion = "2.7.1",
    [string]$TorchIndexUrl = "https://download.pytorch.org/whl/cu128",
    [string]$RocmProfile = "",
    [string[]]$RocmTargets = @(),
    [ValidateSet("", "legacy", "preview", "stable")]
    [string]$RocmChannel = "",
    [string]$RuntimeArtifactLock = "",
    [string]$RuntimeArtifactWheelhouse = "",
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
# Changes to the embedded Python or venv layout require a new manifest baseGeneration.
if ($InitialBackend -eq "mps") {
    # MPS is the historical name; the runtime directory and manifest backend are both mlx.
    $InitialBackend = "mlx"
}

function Get-ManifestCommonRequirements {
    @($runtimeManifest.common.PSObject.Properties |
        Where-Object { $_.Name -notin @("pymss", "pymss-core") } |
        ForEach-Object { [string]$_.Value })
}

function Get-ManifestBootstrapRequirements {
    @($runtimeManifest.bootstrap.PSObject.Properties |
        ForEach-Object { [string]$_.Value })
}

function Get-ManifestRequirement {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Name
    )
    $property = $runtimeManifest.common.PSObject.Properties[$Name]
    if ($runtimeManifest.releaseCore -and $runtimeManifest.releaseCore.requirements) {
        $pin = $runtimeManifest.releaseCore.requirements.PSObject.Properties[$Name]
        if ($pin) { $property = $pin }
    }
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
    return @($backendProperty.Value.extras |
        Where-Object { ![string]::IsNullOrWhiteSpace([string]$_) } |
        ForEach-Object { [string]$_ })
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
$manifestBootstrapRequirements = Get-ManifestBootstrapRequirements
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

function Assert-BootstrapRuntime {
    param(
        [Parameter(Mandatory = $true)]
        [string]$PythonPath
    )

    Invoke-NativeChecked -FilePath $PythonPath -Arguments @(
        '-I', '-X', 'utf8',
        '-c',
        "import requests, socks; from pip._vendor.packaging.requirements import Requirement; from pip._vendor.packaging.version import Version; assert Requirement('pymss>=2.1').name == 'pymss'; assert Version('2.1.5') > Version('2.1.4')"
    )
}

function Install-BootstrapRequirements {
    param(
        [Parameter(Mandatory = $true)]
        [string]$PythonPath
    )

    if ($manifestBootstrapRequirements.Count -gt 0) {
        Invoke-NativeChecked -FilePath $PythonPath -Arguments (@('-I', '-X', 'utf8', '-m', 'pip', 'install', '--no-cache-dir') + $manifestBootstrapRequirements)
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
    $pythonPath = Join-Path $EnvironmentDir "Scripts\python.exe"
    if (!(Test-Path -LiteralPath $pythonPath)) { $pythonPath = Join-Path $EnvironmentDir "python.exe" }
    $paths = Get-RocmRuntimePaths -PythonPath $pythonPath -EnvironmentDir $EnvironmentDir
    $nativeTools = @($paths.nativeToolDirs | ForEach-Object { Join-Path $EnvironmentDir $_ })
    if (!@($nativeTools | Where-Object { Test-Path -LiteralPath (Join-Path $_ "offload-arch.exe") }).Count) {
        throw "ROCm native offload-arch tool was not found in the selected environment"
    }
    if (!$paths.nativeLibraryDirs.Count) { throw "ROCm runtime library directories were not found" }
    if (Test-Path -LiteralPath $launcher) {
        Remove-Item -LiteralPath $launcher -Force
        Write-Host "Removed relocatability-breaking ROCm launcher $launcher"
    }
    return @($nativeTools) + @($paths.nativeLibraryDirs | ForEach-Object { Join-Path $EnvironmentDir $_ })
}

function Get-RocmInstallPlan {
    $planScript = @'
import json, platform, sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
from worker_runtime_profiles import runtime_install_plan, resolve_profile
manifest = json.loads(Path(sys.argv[2]).read_text(encoding='utf-8'))
profile_id = None if sys.argv[3] == '__default__' else sys.argv[3]
profile = resolve_profile(manifest, 'rocm', profile_id, platform_name='win32')
targets = sys.argv[4:] or (['all'] if profile.get('installMode') == 'multiarch' else [])
plan = runtime_install_plan(manifest, 'rocm', profile_id, targets,
                            platform_name='win32', machine_name=platform.machine(),
                            python_version=platform.python_version())
if not plan['installationEnabled']:
    raise SystemExit('The selected ROCm profile is not enabled for installation or packaging')
print(json.dumps(plan))
'@
    $selectedProfile = if ($RocmProfile) { $RocmProfile } else { '__default__' }
    $output = @(Invoke-NativeChecked -FilePath $Python -Arguments (@('-I', '-B', '-c', $planScript,
        (Join-Path $root 'python'), $manifestPath, $selectedProfile) + $RocmTargets))
    return ($output[-1] | ConvertFrom-Json)
}

function Get-RocmRuntimePaths {
    param([string]$PythonPath, [string]$EnvironmentDir)
    $output = @(Invoke-NativeChecked -FilePath $PythonPath -Arguments @('-I', '-B',
        (Join-Path $root 'python\worker_runtime_paths.py'), $EnvironmentDir, '--python', $PythonPath))
    return ($output[-1] | ConvertFrom-Json)
}

function Install-LockedRocmRuntime {
    param([string]$PythonPath, [string]$EnvironmentDir)
    $arguments = @('-I', '-B', (Join-Path $root 'scripts/runtime-artifacts.py'), 'install',
        '--lock', $RuntimeArtifactLock, '--profile', $rocmPlan.profileId,
        '--profile-revision', [string]$rocmPlan.profileRevision, '--python', $PythonPath)
    if ($RuntimeArtifactWheelhouse) { $arguments += @('--wheelhouse', $RuntimeArtifactWheelhouse) }
    if ($RocmTargets.Count) { $arguments += @('--targets') + $RocmTargets }
    Invoke-NativeChecked -FilePath $Python -Arguments $arguments
    Copy-Item -LiteralPath $RuntimeArtifactLock -Destination (Join-Path $EnvironmentDir 'pymss-runtime-artifact-lock.json')
}

function Save-AcceleratorSnapshot {
    param([string]$PythonPath, [string]$EnvironmentDir)
    if ($manifestBackend -ne 'rocm') { return @() }
    [System.IO.File]::WriteAllText((Join-Path $EnvironmentDir 'pymss-runtime-install-plan.json'),
        ($rocmPlan | ConvertTo-Json -Depth 20), [System.Text.UTF8Encoding]::new($false))
    $snapshotScript = @'
import json, platform, sys
from importlib import metadata
from pathlib import Path
sys.path.insert(0, sys.argv[1])
from worker_runtime_profiles import (accelerator_versions, accelerator_constraints, resolve_profile,
                                     runtime_fingerprints, accelerator_source_records,
                                     verify_profile_accelerator_sources, runtime_state_from_artifact_plan)
from worker_runtime_artifacts import load_artifact_lock, resolve_artifact_plan, verify_installed_coverage
from pip._vendor.packaging.version import Version
from urllib.parse import urlsplit, urlunsplit
root = Path(sys.argv[2])
plan = json.loads((root / 'pymss-runtime-install-plan.json').read_text(encoding='utf-8'))
versions = {d.metadata['Name']: d.version for d in metadata.distributions() if d.metadata['Name']}
lock_path = root / 'pymss-runtime-artifact-lock.json'
if lock_path.is_file():
    lock = load_artifact_lock(lock_path)
    plan.update(resolve_artifact_plan(lock, plan['profileId'], plan['targets'], profile_revision=plan['profileRevision']))
    plan['resolutionStatus'] = 'locked'
    coverage = verify_installed_coverage(plan, versions)
    fixed = accelerator_versions(versions, plan['devicePackageNames'])
    sources = {a['name']: {'version': a['version'], 'url': a['url'], 'hashes': {'sha256': a['sha256']},
                          'provenance': a['provenance']} for a in plan['artifacts'] if a['name'] in fixed}
    state = runtime_state_from_artifact_plan(plan, platform.python_version(), versions)
    state.update(coverage)
    (root / 'pymss-accelerator-constraints.txt').write_text(accelerator_constraints(fixed), encoding='utf-8')
    (root / 'pymss-accelerator-state.json').write_text(json.dumps({'versions': fixed, 'sources': sources,
        'acceleratorPackageSources': sources, **state}, indent=2), encoding='utf-8')
    raise SystemExit(0)
# Existing legacy snapshots remain inspectable; all new release builds require
# the binary lock before directories are changed.
fixed = accelerator_versions(versions, plan['packages'])
for name, expected in plan['packages'].items():
    if name not in fixed or Version(fixed[name]) != Version(expected):
        raise RuntimeError('Installed accelerator does not match the selected profile: ' + name)
sources = {}
stages = ['sdk', 'torch'] if plan['torch'].get('rocmRequirements') else ['torch']
for stage in stages:
    report_path = root / f'pymss-install-{stage}.json'
    report = json.loads(report_path.read_text(encoding='utf-8'))
    sources.update(accelerator_source_records(report))
    for item in report['install']:
        download = item.get('download_info') or {}
        if download.get('url'):
            url = urlsplit(str(download['url']))
            download['url'] = urlunsplit((url.scheme, url.hostname or '', url.path, '', ''))
    report_path.write_text(json.dumps(report, indent=2), encoding='utf-8')
verify_profile_accelerator_sources(plan, sources)
profile = resolve_profile(json.loads(Path(sys.argv[3]).read_text(encoding='utf-8')), 'rocm', plan['profileId'])
fingerprints = runtime_fingerprints(profile, platform.python_version(), fixed, plan['targets'])
(root / 'pymss-accelerator-constraints.txt').write_text(accelerator_constraints(fixed), encoding='utf-8')
(root / 'pymss-accelerator-state.json').write_text(json.dumps({'versions': fixed, 'sources': sources,
    'acceleratorPackageSources': sources, **fingerprints}, indent=2), encoding='utf-8')
'@
    Invoke-NativeChecked -FilePath $PythonPath -Arguments @('-I', '-B', '-c', $snapshotScript,
        (Join-Path $root 'python'), $EnvironmentDir, $manifestPath)
    return @('--constraint', (Join-Path $EnvironmentDir 'pymss-accelerator-constraints.txt'))
}

function Assert-AcceleratorSnapshot {
    param([string]$PythonPath, [string]$EnvironmentDir)
    if ($manifestBackend -ne 'rocm') { return }
    $verifyScript = @'
import json, sys
from importlib import metadata
from pathlib import Path
sys.path.insert(0, sys.argv[1])
from worker_runtime_profiles import accelerator_versions, verify_accelerator_versions
expected = json.loads((Path(sys.argv[2]) / 'pymss-accelerator-state.json').read_text(encoding='utf-8'))['versions']
actual = {d.metadata['Name']: d.version for d in metadata.distributions() if d.metadata['Name']}
verify_accelerator_versions(expected, accelerator_versions(actual, expected))
'@
    Invoke-NativeChecked -FilePath $PythonPath -Arguments @('-I', '-B', '-c', $verifyScript,
        (Join-Path $root 'python'), $EnvironmentDir)
}

function Assert-DependencyInstallReport {
    param([string]$PythonPath, [string]$EnvironmentDir, [string]$ReportPath, [string]$Stage)
    $reportScript = @'
import json, sys
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit
sys.path.insert(0, sys.argv[1])
from worker_runtime_profiles import accelerator_source_records, is_base_accelerator_package
root, report_path, stage = Path(sys.argv[2]), Path(sys.argv[3]), sys.argv[4]
report = json.loads(report_path.read_text(encoding='utf-8'))
records = accelerator_source_records(report)
frozen = json.loads((root / 'pymss-accelerator-state.json').read_text(encoding='utf-8'))['versions']
changed = {str((item.get('metadata') or {}).get('name') or '') for item in report['install']
           if is_base_accelerator_package(str((item.get('metadata') or {}).get('name') or ''), frozen)}
if records or changed:
    raise RuntimeError('The ' + stage + ' dependencies attempted to reinstall base accelerator packages: ' + ', '.join(sorted(changed)))
for item in report['install']:
    download = item.get('download_info') or {}
    if download.get('url'):
        url = urlsplit(str(download['url']))
        download['url'] = urlunsplit((url.scheme, url.hostname or '', url.path, '', ''))
(root / f'pymss-install-{stage}.json').write_text(json.dumps(report, indent=2), encoding='utf-8')
'@
    Invoke-NativeChecked -FilePath $PythonPath -Arguments @('-I', '-B', '-c', $reportScript,
        (Join-Path $root 'python'), $EnvironmentDir, $ReportPath, $Stage)
}

function Invoke-RuntimeDependencyInstall {
    param([string]$PythonPath, [string]$EnvironmentDir, [string]$Stage, [object[]]$Arguments)
    if ($manifestBackend -ne 'rocm') {
        Invoke-NativeChecked -FilePath $PythonPath -Arguments (@('-I', '-X', 'utf8', '-m', 'pip', 'install', '--no-cache-dir') + $Arguments)
        return
    }
    # Keep complete pip resolver reports, sanitizing download URLs before they
    # enter the portable runtime. Raw temporary reports are removed on failure.
    $reportPath = [System.IO.Path]::GetTempFileName()
    try {
        Invoke-NativeChecked -FilePath $PythonPath -Arguments (@('-I', '-X', 'utf8', '-m', 'pip', 'install', '--no-cache-dir', '--report', $reportPath) + $Arguments)
        Assert-DependencyInstallReport -PythonPath $PythonPath -EnvironmentDir $EnvironmentDir -ReportPath $reportPath -Stage $Stage
        Assert-AcceleratorSnapshot -PythonPath $PythonPath -EnvironmentDir $EnvironmentDir
    } finally {
        Remove-Item -LiteralPath $reportPath -Force -ErrorAction SilentlyContinue
    }
}

function Assert-RocmRuntime {
    param([string]$PythonPath, [string]$EnvironmentDir, [string]$Phase)
    if ($manifestBackend -ne 'rocm') { return }
    Assert-AcceleratorSnapshot -PythonPath $PythonPath -EnvironmentDir $EnvironmentDir
    Invoke-NativeChecked -FilePath $PythonPath -Arguments @('-I', '-X', 'utf8', '-m', 'pip', '--isolated', 'check')
    if ($RuntimeArtifactLock) {
        $verifyArgs = @('-I', '-B', (Join-Path $root 'scripts\runtime-artifacts.py'), 'verify',
            '--lock', $RuntimeArtifactLock, '--profile', $rocmPlan.profileId,
            '--profile-revision', [string]$rocmPlan.profileRevision, '--python', $PythonPath)
        if ($RocmTargets.Count) { $verifyArgs += @('--targets') + $RocmTargets }
        Invoke-NativeChecked -FilePath $Python -Arguments $verifyArgs
    }
    $paths = Get-RocmRuntimePaths -PythonPath $PythonPath -EnvironmentDir $EnvironmentDir
    if (!$paths.nativeLibraryDirs.Count -or !$paths.nativeToolDirs.Count) {
        throw 'The selected ROCm SDK did not report native library and tool directories'
    }
    $previousPath = $env:PATH
    try {
        $env:PATH = (@($paths.nativeToolDirs + $paths.nativeLibraryDirs | ForEach-Object { Join-Path $EnvironmentDir $_ }) + $previousPath) -join ';'
        Invoke-NativeChecked -FilePath $PythonPath -Arguments @('-I', '-B', '-c',
            "import torch, torchaudio, torchvision; assert torch.version.hip; x = torch.arange(64, dtype=torch.float32); torch.testing.assert_close(torch.fft.irfft(torch.fft.rfft(x), n=64), x, atol=1e-4, rtol=1e-5); print('ROCm host imports and audio FFT passed')")
        Invoke-NativeChecked -FilePath $PythonPath -Arguments @('-I', '-X', 'utf8', '-B', (Join-Path $root 'python\runtime_audio_probe.py'), '--check-core')
    } finally { $env:PATH = $previousPath }
    $report = $paths | ConvertTo-Json -Depth 6
    [System.IO.File]::WriteAllText((Join-Path $EnvironmentDir "pymss-native-paths-$Phase.json"), $report, [System.Text.UTF8Encoding]::new($false))
    if ($Phase -eq 'post-prune') {
        $before = Get-Content -Raw (Join-Path $EnvironmentDir 'pymss-native-paths-pre-prune.json') | ConvertFrom-Json
        if (($before.nativeLibraryDirs | ConvertTo-Json -Compress) -ne ($paths.nativeLibraryDirs | ConvertTo-Json -Compress) -or
            ($before.nativeToolDirs | ConvertTo-Json -Compress) -ne ($paths.nativeToolDirs | ConvertTo-Json -Compress)) {
            throw 'ROCm SDK paths changed during runtime pruning'
        }
    }
}

if ($RewriteRuntimeEnvConfigs -or $TemplateRuntimeEnvConfigs) {
    Rewrite-WindowsRuntimeEnvConfigs -EnvsDir $RuntimeEnvsDir -PythonRuntimeDir $RuntimeDir -Template:$TemplateRuntimeEnvConfigs
    exit 0
}

# Resolve release policy and locked targets before changing runtime directories.
if ($manifestBackend -eq 'rocm' -and !$Minimal) {
    if ($torchVersionOverride -or $torchIndexOverride) {
        throw 'ROCm versions and sources must be selected through a runtime profile'
    }
    $rocmPlan = Get-RocmInstallPlan
    $RocmTargets = @($rocmPlan.requestedTargets)
    if (!$RocmChannel) { $RocmChannel = [string]$rocmPlan.releaseState }
    $manifestTorch = $rocmPlan.torch
    if (!$RuntimeArtifactLock -or !(Test-Path -LiteralPath $RuntimeArtifactLock)) {
        throw 'ROCm packaging requires a resolved binary artifact lock; run scripts/runtime-artifacts.py resolve first'
    }
    $validateLockScript = @'
import json, platform, sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
from worker_runtime_artifacts import load_artifact_lock, resolve_artifact_plan, build_locked_requirements
from worker_runtime_profiles import runtime_install_plan
from pip._vendor.packaging.utils import canonicalize_name
from pip._vendor.packaging.version import Version
lock = load_artifact_lock(sys.argv[2])
plan = runtime_install_plan(json.loads(Path(sys.argv[4]).read_text(encoding='utf-8')), 'rocm', sys.argv[3],
                            sys.argv[7:], platform_name='win32', machine_name=platform.machine(),
                            python_version=platform.python_version())
if lock['channel'] != sys.argv[5] or lock['channel'] != plan['releaseState']:
    raise RuntimeError('The artifact lock channel does not match the requested release channel')
locked = resolve_artifact_plan(lock, sys.argv[3], sys.argv[7:], profile_revision=plan['profileRevision'],
                               python_abi='cp' + plan['python'].replace('.', ''), platform_tag='win_amd64')
for name, version in plan['packages'].items():
    actual = locked['resolvedAcceleratorPackages'].get(canonicalize_name(name))
    if actual is None or Version(actual) != Version(version):
        raise RuntimeError('The binary artifact lock differs from the selected recipe package: ' + name)
if sys.argv[6] != '__remote__':
    # Verify the complete selected local closure before the existing runtime can
    # be removed. Installation repeats these checks rather than trusting a cache.
    build_locked_requirements(locked, wheelhouse=Path(sys.argv[6]))
'@
    $preflightWheelhouse = if ($RuntimeArtifactWheelhouse) { $RuntimeArtifactWheelhouse } else { '__remote__' }
    Invoke-NativeChecked -FilePath $Python -Arguments (@('-I', '-B', '-c', $validateLockScript,
        (Join-Path $root 'python'), $RuntimeArtifactLock, $rocmPlan.profileId, $manifestPath,
        $RocmChannel, $preflightWheelhouse) + $RocmTargets)
} elseif ($RocmProfile -or $RocmTargets.Count) {
    throw 'ROCm profile and target options require a ROCm runtime build'
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
    robocopy $pythonHome $runtime /E /XD __pycache__ /XF *.pyc pymss-runtime-state.json pymss-runtime-artifact-lock.json pymss-accelerator-state.json | Out-Host
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
    Invoke-NativeChecked -FilePath $runtimePython -Arguments @('-I', '-X', 'utf8', '-m', 'ensurepip', '--upgrade')
    Invoke-NativeChecked -FilePath $runtimePython -Arguments @('-I', '-X', 'utf8', '-m', 'pip', 'install', '--upgrade', 'pip', 'setuptools', 'wheel')
    Install-BootstrapRequirements -PythonPath $runtimePython
    Write-Host "Bootstrap runtime created at $runtime"

    # Step 2: Create venv for the initial backend
    $envsDir = if ([System.IO.Path]::IsPathRooted($effectiveRuntimeEnvsDir)) { $effectiveRuntimeEnvsDir } else { Join-Path $root $effectiveRuntimeEnvsDir }
    $envDir = Join-Path $envsDir $InitialBackend
    if (Test-Path -LiteralPath $envDir) {
        Remove-Item -LiteralPath $envDir -Recurse -Force
    }
    Write-Host "Creating venv at $envDir"
    Invoke-NativeChecked -FilePath $runtimePython -Arguments @('-I', '-X', 'utf8', '-m', 'venv', $envDir)
    $envPython = Join-Path $envDir "Scripts\python.exe"
    if (!(Test-Path -LiteralPath $envPython)) {
        throw "venv python.exe was not created at $envPython"
    }
    & (Join-Path $PSScriptRoot "prune-python-runtime.ps1") -RuntimeDir $runtime -KeepVenv
    Assert-BootstrapRuntime -PythonPath $runtimePython
    Invoke-NativeChecked -FilePath $envPython -Arguments @('-I', '-X', 'utf8', '-m', 'pip', 'install', '--upgrade', 'pip', 'setuptools', 'wheel')

    # Step 3: Install packages for the backend
    $torchRequirements = if ($manifestTorch.requirements -and !$torchVersionOverride) { @($manifestTorch.requirements) } else { @($effectiveTorchRequirement) }
    if ($InitialBackend -eq "rocm") {
        Install-LockedRocmRuntime -PythonPath $envPython -EnvironmentDir $envDir
    } elseif ([string]::IsNullOrWhiteSpace($effectiveTorchIndexUrl)) {
        Invoke-NativeChecked -FilePath $envPython -Arguments (@('-I', '-X', 'utf8', '-m', 'pip', 'install', '--no-cache-dir') + $torchRequirements)
    } else {
        Invoke-NativeChecked -FilePath $envPython -Arguments (@('-I', '-X', 'utf8', '-m', 'pip', 'install', '--no-cache-dir', '--index-url', $effectiveTorchIndexUrl) + $torchRequirements)
    }
    $acceleratorConstraintArgs = @(Save-AcceleratorSnapshot -PythonPath $envPython -EnvironmentDir $envDir)
    if ($manifestBackend -ne 'rocm') {
        Invoke-RuntimeDependencyInstall -PythonPath $envPython -EnvironmentDir $envDir -Stage 'common' -Arguments (@('--only-binary=:all:', '--prefer-binary') + $acceleratorConstraintArgs + $manifestCommonRequirements)
        if ($manifestBackendExtras.Count -gt 0) {
            Invoke-RuntimeDependencyInstall -PythonPath $envPython -EnvironmentDir $envDir -Stage 'extras' -Arguments ($acceleratorConstraintArgs + $manifestBackendExtras)
        }
        Invoke-RuntimeDependencyInstall -PythonPath $envPython -EnvironmentDir $envDir -Stage 'pymss' -Arguments (@('--upgrade') + $acceleratorConstraintArgs + @($manifestPymssRequirement, $manifestPymssCoreRequirement))
    }
    $rocmToolDirs = if ($InitialBackend -eq "rocm") { Remove-RocmOffloadArchLauncher -EnvironmentDir $envDir } else { @() }
    Assert-RocmRuntime -PythonPath $envPython -EnvironmentDir $envDir -Phase 'pre-prune'
    & (Join-Path $PSScriptRoot "prune-python-runtime.ps1") -RuntimeDir $envDir -KeepScripts -Backend $manifestBackend
    Assert-RocmRuntime -PythonPath $envPython -EnvironmentDir $envDir -Phase 'post-prune'
    Invoke-NativeChecked -FilePath $envPython -Arguments @('-I', '-X', 'utf8', '-m', 'pip', '--version')

    # Step 4: Verify the environment
    $previousDontWriteBytecode = $env:PYTHONDONTWRITEBYTECODE
    $previousPath = $env:PATH
    try {
        $env:PYTHONDONTWRITEBYTECODE = "1"
        if ($rocmToolDirs.Count -gt 0) {
            $env:PATH = ($rocmToolDirs + $previousPath) -join ";"
        }
        Invoke-NativeChecked -FilePath $envPython -Arguments @('-I', '-X', 'utf8', '-c', "import importlib.util, pymss, pymss.graph, torch, librosa, av, yaml, tqdm; print('pymss', getattr(pymss, '__version__', 'unknown'), pymss.__file__); print('torch', torch.__version__, 'cuda', torch.version.cuda, 'cuda_available', torch.cuda.is_available()); print('librosa', librosa.__version__); print('av', av.__version__); print('mlx', importlib.util.find_spec('mlx') is not None)")
        Invoke-NativeChecked -FilePath $envPython -Arguments @('-I', '-X', 'utf8', (Join-Path $root "python\runtime_audio_probe.py"), '--check-core')

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
        $manifestMappingJson = '{"pysocks":"socks","pyyaml":"yaml","pymss-core":"pymss_core","typing-extensions":"typing_extensions"}'
        # Base64 avoids Windows PowerShell's native argument quoting removing JSON quotes.
        $manifestPackageData = [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($manifestPackageJson))
        $manifestMappingData = [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($manifestMappingJson))
        $probeScript = @'
import base64, importlib.util, json, platform
from importlib import metadata
names = json.loads(base64.b64decode('%NAMES%'))
mapping = json.loads(base64.b64decode('%MAPPING%'))
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
'@.Replace('%NAMES%', $manifestPackageData).Replace('%MAPPING%', $manifestMappingData)
        $probeOutput = @(Invoke-NativeChecked -FilePath $envPython -Arguments @('-I', '-X', 'utf8', '-c', $probeScript))
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

    $envStateData = @{
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
    }
    if ($manifestBackend -eq 'rocm') {
        $paths = Get-RocmRuntimePaths -PythonPath $envPython -EnvironmentDir $envDir
        $snapshot = Get-Content -Raw (Join-Path $envDir 'pymss-accelerator-state.json') | ConvertFrom-Json
        foreach ($name in @('profileId', 'profileRevision', 'profileSnapshot', 'releaseState', 'requestedTargets',
                'resolvedTargets', 'installedTargets', 'devicePackageNames', 'packageRoles',
                'fingerprintSchemaVersion', 'artifactLockId', 'hostLockId', 'basePackageArtifacts',
                'acceleratorPackageArtifacts', 'baseFingerprint', 'targetFingerprint',
                'coverageVerified', 'hardwareValidation')) {
            if ($snapshot.PSObject.Properties[$name]) { $envStateData[$name] = $snapshot.$name }
        }
        $envStateData.acceleratorPackages = $snapshot.versions
        $envStateData.resolvedAcceleratorPackages = $snapshot.versions
        $envStateData.acceleratorPackageSources = $snapshot.acceleratorPackageSources
        $envStateData.nativeLibraryDirs = @($paths.nativeLibraryDirs)
        $envStateData.nativeToolDirs = @($paths.nativeToolDirs)
        $envStateData.nativePathsSource = $paths.nativePathsSource
        $envStateData.nativePathsModules = @($paths.nativePathsModules)
        $envStateData.nativePathsQueryStatus = $paths.nativePathsQueryStatus
    }
    $envState = $envStateData | ConvertTo-Json -Depth 8
    $envStatePath = Join-Path $envDir "pymss-runtime-state.json"
    Set-Content -Path $envStatePath -Value $envState -Encoding UTF8
    Write-Host "Wrote environment state to $envStatePath"

    # Use relative pythonPath (relative to runtime-envs dir) so it works on any machine
    $relativePythonPath = Join-Path $InitialBackend "Scripts\python.exe"
    $activeStateData = $envStateData.Clone()
    $activeStateData.pythonPath = $relativePythonPath
    $activeStateData.activatedAt = $now
    $activeState = $activeStateData | ConvertTo-Json -Depth 8
    $activeRuntimePath = Join-Path $envsDir "active-runtime.json"
    Set-Content -Path $activeRuntimePath -Value $activeState -Encoding UTF8
    Write-Host "Wrote active runtime to $activeRuntimePath"

    & (Join-Path $PSScriptRoot "prune-python-runtime.ps1") -RuntimeDir $envDir -KeepScripts -Backend $manifestBackend
    Assert-RocmRuntime -PythonPath $envPython -EnvironmentDir $envDir -Phase 'post-prune'
    Invoke-NativeChecked -FilePath $envPython -Arguments @('-I', '-X', 'utf8', '-m', 'pip', '--version')
    Invoke-NativeChecked -FilePath $envPython -Arguments @('-I', '-X', 'utf8', '-B', (Join-Path $PSScriptRoot "verify-pymss-plugin-api.py"))
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
robocopy $pythonHome $runtime /E /XD __pycache__ /XF *.pyc pymss-runtime-state.json pymss-runtime-artifact-lock.json pymss-accelerator-state.json | Out-Host
if ($LASTEXITCODE -gt 7) { throw "robocopy failed with exit code $LASTEXITCODE" }
$global:LASTEXITCODE = 0

$runtimePython = Join-Path $runtime "python.exe"
if (!(Test-Path -LiteralPath $runtimePython)) {
    throw "python.exe was not copied to $runtime"
}

Invoke-NativeChecked -FilePath $runtimePython -Arguments @('-I', '-X', 'utf8', '-m', 'pip', 'install', '--upgrade', 'pip', 'setuptools', 'wheel')
if ($Minimal) {
    $sitePackages = Join-Path $runtime "Lib\site-packages"
    if (Test-Path -LiteralPath $sitePackages) {
        Remove-Item -LiteralPath $sitePackages -Recurse -Force
    }
    New-Item -ItemType Directory -Force -Path $sitePackages | Out-Null
    Invoke-NativeChecked -FilePath $runtimePython -Arguments @('-I', '-X', 'utf8', '-m', 'ensurepip', '--upgrade')
    Install-BootstrapRequirements -PythonPath $runtimePython
    Invoke-NativeChecked -FilePath $runtimePython -Arguments @('-I', '-X', 'utf8', '-m', 'pip', '--version')
    Assert-BootstrapRuntime -PythonPath $runtimePython
    Write-Host "Prepared minimal Python runtime without inference dependencies"
    exit 0
}
Install-BootstrapRequirements -PythonPath $runtimePython
$torchRequirements = if ($manifestTorch.requirements -and !$torchVersionOverride) { @($manifestTorch.requirements) } else { @($effectiveTorchRequirement) }
if ($Variant -eq "rocm") {
    Install-LockedRocmRuntime -PythonPath $runtimePython -EnvironmentDir $runtime
} elseif ([string]::IsNullOrWhiteSpace($effectiveTorchIndexUrl)) {
    Invoke-NativeChecked -FilePath $runtimePython -Arguments (@('-I', '-X', 'utf8', '-m', 'pip', 'install', '--no-cache-dir') + $torchRequirements)
} else {
    Invoke-NativeChecked -FilePath $runtimePython -Arguments (@('-I', '-X', 'utf8', '-m', 'pip', 'install', '--no-cache-dir', '--index-url', $effectiveTorchIndexUrl) + $torchRequirements)
}
$acceleratorConstraintArgs = @(Save-AcceleratorSnapshot -PythonPath $runtimePython -EnvironmentDir $runtime)
if ($manifestBackend -ne 'rocm') {
    Invoke-RuntimeDependencyInstall -PythonPath $runtimePython -EnvironmentDir $runtime -Stage 'common' -Arguments (@('--only-binary=:all:', '--prefer-binary') + $acceleratorConstraintArgs + $manifestCommonRequirements)
    if ($manifestBackendExtras.Count -gt 0) {
        Invoke-RuntimeDependencyInstall -PythonPath $runtimePython -EnvironmentDir $runtime -Stage 'extras' -Arguments ($acceleratorConstraintArgs + $manifestBackendExtras)
    }
    Invoke-RuntimeDependencyInstall -PythonPath $runtimePython -EnvironmentDir $runtime -Stage 'pymss' -Arguments (@('--upgrade') + $acceleratorConstraintArgs + @($manifestPymssRequirement, $manifestPymssCoreRequirement))
}

$rocmToolDirs = if ($Variant -eq 'rocm') { Remove-RocmOffloadArchLauncher -EnvironmentDir $runtime } else { @() }
Assert-RocmRuntime -PythonPath $runtimePython -EnvironmentDir $runtime -Phase 'pre-prune'
& (Join-Path $PSScriptRoot "prune-python-runtime.ps1") -RuntimeDir $runtime -KeepVenv -Backend $manifestBackend
Assert-RocmRuntime -PythonPath $runtimePython -EnvironmentDir $runtime -Phase 'post-prune'
Invoke-NativeChecked -FilePath $runtimePython -Arguments @('-I', '-X', 'utf8', '-m', 'pip', '--version')
Assert-BootstrapRuntime -PythonPath $runtimePython
$previousDontWriteBytecode = $env:PYTHONDONTWRITEBYTECODE
$previousPath = $env:PATH
try {
    $env:PYTHONDONTWRITEBYTECODE = "1"
    if ($rocmToolDirs.Count -gt 0) { $env:PATH = ($rocmToolDirs + $previousPath) -join ';' }
    Invoke-NativeChecked -FilePath $runtimePython -Arguments @('-I', '-X', 'utf8', '-c', "import importlib.util, pymss, pymss.graph, torch, librosa, av, yaml, tqdm; print('pymss', getattr(pymss, '__version__', 'unknown'), pymss.__file__); print('torch', torch.__version__, 'cuda', torch.version.cuda, 'cuda_available', torch.cuda.is_available()); print('librosa', librosa.__version__); print('av', av.__version__); print('mlx', importlib.util.find_spec('mlx') is not None)")
    Invoke-NativeChecked -FilePath $runtimePython -Arguments @('-I', '-X', 'utf8', '-B', (Join-Path $PSScriptRoot "verify-pymss-plugin-api.py"))
    Invoke-NativeChecked -FilePath $runtimePython -Arguments @('-I', '-X', 'utf8', (Join-Path $root "python\runtime_audio_probe.py"), '--check-core')
} finally {
    if ($null -eq $previousDontWriteBytecode) {
        Remove-Item Env:\PYTHONDONTWRITEBYTECODE -ErrorAction SilentlyContinue
    } else {
        $env:PYTHONDONTWRITEBYTECODE = $previousDontWriteBytecode
    }
    $env:PATH = $previousPath
}
& (Join-Path $PSScriptRoot "prune-python-runtime.ps1") -RuntimeDir $runtime -KeepVenv -Backend $manifestBackend
Assert-RocmRuntime -PythonPath $runtimePython -EnvironmentDir $runtime -Phase 'post-prune'
Invoke-NativeChecked -FilePath $runtimePython -Arguments @('-I', '-X', 'utf8', '-m', 'pip', '--version')
