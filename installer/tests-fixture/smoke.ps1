# End-to-end installer smoke test for the MoInstaller migration.
# Builds the fixture setup.exe with mo, then exercises:
#   1. fresh /VERYSILENT install  (files, conditional rules, pyvenv repair,
#      cleanup, marker, shortcuts, uninstall key)
#   2. in-place upgrade           (user-managed env preserved, legacy
#      unins000.exe removed, pyvenv.cfg rewritten)
#   3. silent uninstall           (app dir fully removed, shortcuts,
#      registry key gone, uninstaller self-delete)
param(
  # Path to the mo CLI (download from MoInstaller releases or built locally:
  # https://github.com/HuanLinOTO/MoInstaller)
  [string]$MoPath = (Join-Path $PSScriptRoot "../moinstaller/target/release/mo.exe")
)
$ErrorActionPreference = "Stop"
$root = $PSScriptRoot
$mo = $MoPath
if (!(Test-Path $mo)) { throw "mo CLI not found at $mo - pass -MoPath or see installer/README.md" }
$work = Join-Path ([System.IO.Path]::GetTempPath()) ("pymss-mo-smoke-" + [guid]::NewGuid().ToString("N").Substring(0,8))
New-Item -ItemType Directory -Force -Path $work | Out-Null
$setup = Join-Path $work "pymss-smoke-setup.exe"
$app = Join-Path $work "app"
$failures = 0

# clean slate: stale group dir from a previous run would skip CreatedDir
# bookkeeping, making the uninstall assertion a false negative
$grp = Join-Path $env:APPDATA "Microsoft/Windows/Start Menu/Programs/Pymss Studio CI"
if (Test-Path $grp) { Remove-Item $grp -Recurse -Force }
$desktopLnk = Join-Path ([Environment]::GetFolderPath('Desktop')) "Pymss Studio CI.lnk"
if (Test-Path $desktopLnk) { Remove-Item $desktopLnk -Force }

function Assert([string]$Name, [bool]$Cond) {
  if ($Cond) { Write-Host "  PASS $Name" }
  else { Write-Host "  FAIL $Name"; $script:failures++ }
}

Write-Host "== mo build=="
& $mo build (Join-Path $root "installer.toml") --out $setup
if ($LASTEXITCODE -ne 0) { throw "mo build failed" }
Assert "setup.exe produced" (Test-Path $setup)

Write-Host "== fresh install =="
& $setup /VERYSILENT ("/DIR=" + $app) | Out-Null
Assert "exit code 0" ($LASTEXITCODE -eq 0)
Assert "app exe installed" (Test-Path (Join-Path $app "Pymss Studio.exe"))
Assert "bootstrap runtime installed" (Test-Path (Join-Path $app "python-runtime/python.exe"))
Assert "bundled cuda env installed" (Test-Path (Join-Path $app "python-runtime/runtime-envs/cuda/Scripts/python.exe"))
Assert "active-runtime.json not installed" (-not (Test-Path (Join-Path $app "python-runtime/runtime-envs/active-runtime.json")))
Assert "dev .git leftover cleaned" (-not (Test-Path (Join-Path $app ".git")))
Assert "__pycache__ cleaned" (-not (Test-Path (Join-Path $app "__pycache__")))
Assert "install marker written" ((Get-Content (Join-Path $app "pymss-studio.inno-install") -Raw).Trim() -eq "managed")
$cfg = Get-Content (Join-Path $app "python-runtime/runtime-envs/cuda/pyvenv.cfg") -Raw
Assert "pyvenv.cfg home rewritten" ($cfg.Contains("home = " + ($app -replace '/', '\') + "\python-runtime"))
$seg = "very_long_directory_name_segment_0123456789_" * 4
$deep = Join-Path $app ("python-runtime/runtime-envs/cuda/Lib/site-packages/deep/" + $seg + "/" + $seg + "/marker.txt")
Assert "deep >260 char path installed" (Test-Path $deep)
Assert "uninstaller staged" (Test-Path (Join-Path $app "mo-uninstall.exe"))
$grp = Join-Path $env:APPDATA "Microsoft/Windows/Start Menu/Programs/Pymss Studio CI"
Assert "start menu shortcut" (Test-Path (Join-Path $grp "Pymss Studio CI.lnk"))
Assert "desktop shortcut not created by default" (-not (Test-Path (Join-Path ([Environment]::GetFolderPath('Desktop')) "Pymss Studio CI.lnk")))
$reg = reg query "HKCU\Software\Microsoft\Windows\CurrentVersion\Uninstall\CI-Smoke-Pymss-Studio" /v DisplayIcon 2>$null
Assert "uninstall key DisplayIcon" (($reg -join " ").Contains("Pymss Studio.exe"))

Write-Host "== in-place upgrade =="
# simulate a user-managed env change + legacy Inno leftovers
Set-Content (Join-Path $app "python-runtime/runtime-envs/cuda/Scripts/python.exe") -Value "user-managed-python"
Set-Content (Join-Path $app "python-runtime/runtime-envs/cuda/user-site.txt") -Value "user data"
Set-Content (Join-Path $app "unins000.exe") -Value "legacy"
Set-Content (Join-Path $app "unins000.dat") -Value "legacy"
& $setup /VERYSILENT ("/DIR=" + $app) | Out-Null
Assert "upgrade exit 0" ($LASTEXITCODE -eq 0)
Assert "user-managed python preserved" ((Get-Content (Join-Path $app "python-runtime/runtime-envs/cuda/Scripts/python.exe") -Raw).Trim() -eq "user-managed-python")
Assert "user file preserved" (Test-Path (Join-Path $app "python-runtime/runtime-envs/cuda/user-site.txt"))
Assert "legacy unins000 removed" ((-not (Test-Path (Join-Path $app "unins000.exe"))) -and (-not (Test-Path (Join-Path $app "unins000.dat"))))
$cfg2 = Get-Content (Join-Path $app "python-runtime/runtime-envs/cuda/pyvenv.cfg") -Raw
Assert "pyvenv.cfg repaired on upgrade" ($cfg2.Contains("home = " + ($app -replace '/', '\') + "\python-runtime"))

Write-Host "== silent uninstall =="
& (Join-Path $app "mo-uninstall.exe") /VERYSILENT | Out-Null
Assert "uninstall exit 0" ($LASTEXITCODE -eq 0)
Start-Sleep -Seconds 4
Assert "app dir fully removed" (-not (Test-Path $app))
# explorer may hold the start-menu group briefly; the uninstaller retries rmdir
$grpGone = -not (Test-Path $grp)
for ($i = 0; $i -lt 10 -and -not $grpGone; $i++) { Start-Sleep -Seconds 1; $grpGone = -not (Test-Path $grp) }
Assert "start menu group removed" $grpGone
reg query "HKCU\Software\Microsoft\Windows\CurrentVersion\Uninstall\CI-Smoke-Pymss-Studio" 2>$null | Out-Null
Assert "uninstall registry key removed" ($LASTEXITCODE -ne 0)

Remove-Item $work -Recurse -Force -ErrorAction SilentlyContinue
if ($failures -gt 0) { throw "$failures smoke assertion(s) failed" }
Write-Host "ALL SMOKE ASSERTIONS PASSED"
exit 0
