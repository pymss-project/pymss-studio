# Runs with the original user's token for installation. The uninstaller may run elevated.
[CmdletBinding()]
param(
    [Parameter(Mandatory)][ValidateSet('Check','Add','Remove','Status','Enable','Disable','Diagnostics')][string] $Action,
    [Parameter(Mandatory)][string] $StudioDir,
    [string] $Report
)
$ErrorActionPreference = 'Stop'
$ownerKey = 'Software\Pymss Studio\CLI'
$environmentKey = 'Environment'
$registryRaw = [Microsoft.Win32.RegistryValueOptions]::DoNotExpandEnvironmentNames
$expandString = [Microsoft.Win32.RegistryValueKind]::ExpandString

function Get-ComparablePath([string] $Value) {
    $value = [Environment]::ExpandEnvironmentVariables($Value.Trim().Trim('"'))
    if ($value.StartsWith('\\?\UNC\', [StringComparison]::OrdinalIgnoreCase)) { $value = '\\' + $value.Substring(8) }
    elseif ($value.StartsWith('\\?\')) { $value = $value.Substring(4) }
    if (-not $value -or -not [IO.Path]::IsPathRooted($value)) { return '' }
    try {
        $full = [IO.Path]::GetFullPath($value)
        $root = [IO.Path]::GetPathRoot($full)
        $trimmed = $full.TrimEnd('\')
        if ($trimmed.Length -lt $root.Length) { return $root }
        return $trimmed
    } catch { return '' }
}

function Write-PathReport([string] $Value) {
    if (-not $Report) { return }
    # Some elevation policies make Setup's temporary folder read-only for the
    # original user. Reporting is optional; the user's PATH operation still works.
    try { [IO.File]::WriteAllText($Report, $Value, [Text.UTF8Encoding]::new($false)) } catch { }
}

function Find-PathCommand([string[]] $Paths, [switch] $SkipStudio) {
    $extensions = @('.exe','.cmd','.bat','.com','.ps1') + ($env:PATHEXT -split ';')
    foreach ($part in (($paths -join ';') -split ';')) {
        $directory = Get-ComparablePath $part
        if (-not $directory -or ($SkipStudio -and $directory -eq $StudioDir)) { continue }
        foreach ($extension in $extensions) {
            if ($extension -notmatch '^\.[A-Za-z0-9]+$') { continue }
            $candidate = [IO.Path]::Combine($directory, "pymss$extension")
            if ([IO.File]::Exists($candidate)) { return $candidate }
        }
    }
    return $null
}

function Test-StudioPathEntry([string] $Paths) {
    foreach ($part in ($Paths -split ';')) {
        if ((Get-ComparablePath $part) -eq $StudioDir) { return $true }
    }
    return $false
}

function Get-Registration([Microsoft.Win32.RegistryKey] $Hive, [string] $MachinePath, [string] $ProcessPath, [switch] $Diagnostics) {
    $key = $Hive.OpenSubKey($environmentKey)
    try { $userPath = if ($key) { $key.GetValue('Path', '', $registryRaw) } } finally { if ($key) { $key.Close() } }
    $owner = $Hive.OpenSubKey($ownerKey)
    try { $previous = if ($owner) { $owner.GetValue('Directory') } } finally { if ($owner) { $owner.Close() } }

    $hasRecord = $previous -and (($userPath -split ';') -contains $previous)
    $result = [ordered]@{ status = 'available'; owned = [bool]($hasRecord -and $previous -eq $StudioDir); path = $null; changed = $false }
    # New terminals see the persisted system PATH followed by user PATH. Ignore
    # our own entry in the parent's stale PATH after disabling in this session.
    $persistedCommand = Find-PathCommand @($MachinePath, $userPath)
    $command = $persistedCommand
    if (-not $command) { $command = Find-PathCommand @($ProcessPath) -SkipStudio }
    if ($command) {
        if ([IO.Path]::GetDirectoryName($command) -eq $StudioDir) { $result.status = 'enabled' }
        else { $result.status = 'conflict'; $result.path = $command }
    } elseif ($hasRecord -and $previous -ne $StudioDir) {
        $result.status = 'moved'
        $result.path = $previous
    }
    if ($Diagnostics) {
        $launcher = [IO.Path]::Combine($StudioDir, 'pymss.exe')
        $result.diagnostics = [ordered]@{
            launcher = $launcher
            launcherExists = [IO.File]::Exists($launcher)
            userPathContainsStudio = Test-StudioPathEntry $userPath
            systemPathContainsStudio = Test-StudioPathEntry $MachinePath
            registeredDirectory = $(if ($hasRecord) { $previous } else { $null })
            persistedCommand = $persistedCommand
            processCommand = Find-PathCommand @($ProcessPath)
        }
    }
    return $result
}

function Remove-OwnedPath([Microsoft.Win32.RegistryKey] $Hive) {
    $owner = $Hive.OpenSubKey($ownerKey, $true)
    if (-not $owner) { return }
    try {
        if ($owner.GetValue('Directory') -ne $StudioDir) { return }
        $key = $Hive.OpenSubKey($environmentKey, $true)
        if ($key) {
            try {
                $raw = $key.GetValue('Path', $null, $registryRaw)
                if ($null -ne $raw) {
                    $parts = [Collections.Generic.List[string]]::new()
                    $parts.AddRange([string[]]($raw -split ';'))
                    # Remove a single exact entry, preserving other spellings and duplicates.
                    for ($i = $parts.Count-1; $i -ge 0; --$i) {
                        if ($parts[$i] -ieq $StudioDir) {
                            $parts.RemoveAt($i)
                            if ($parts.Count -eq 0 -and $owner.GetValue('CreatedPath') -eq 1) {
                                $key.DeleteValue('Path',$false)
                            } else { $key.SetValue('Path', ($parts -join ';'), $key.GetValueKind('Path')) }
                            break
                        }
                    }
                }
            } finally { $key.Close() }
        }
        $owner.DeleteValue('Directory',$false)
        $owner.DeleteValue('CreatedPath',$false)
    } finally { $owner.Close() }
}

function Add-OwnedPath([Microsoft.Win32.RegistryKey] $Hive, [string] $PreviousDir) {
    $key = $hive.CreateSubKey($environmentKey)
    try {
        $raw = $key.GetValue('Path',$null,$registryRaw)
        if (Test-StudioPathEntry $raw) { return }
        $kind = if ($null -eq $raw) { $expandString } else { $key.GetValueKind('Path') }
        $value = if ([string]::IsNullOrEmpty($raw)) { $StudioDir } else { "$raw;$StudioDir" }
        if ($PreviousDir) {
            $parts = [Collections.Generic.List[string]]::new()
            $parts.AddRange([string[]]($raw -split ';'))
            $index = $parts.FindLastIndex([Predicate[string]]{ param($part) $part -ieq $PreviousDir })
            if ($index -lt 0) { throw 'The previous PATH entry changed. Refresh and retry.' }
            $parts[$index] = $StudioDir
            $value = $parts -join ';'
        }
        $owner = $hive.CreateSubKey($ownerKey)
        try {
            $oldDirectory = $owner.GetValue('Directory')
            $oldCreated = $owner.GetValue('CreatedPath')
            if (-not $PreviousDir) { $owner.SetValue('CreatedPath', [int]($null -eq $raw)) }
            $owner.SetValue('Directory',$StudioDir)
            try { $key.SetValue('Path',$value,$kind) } catch {
                if ($null -eq $oldDirectory) { $owner.DeleteValue('Directory',$false) } else { $owner.SetValue('Directory',$oldDirectory) }
                if ($null -eq $oldCreated) { $owner.DeleteValue('CreatedPath',$false) } else { $owner.SetValue('CreatedPath',$oldCreated) }
                throw
            }
        } finally { $owner.Close() }
    } finally { $key.Close() }
}

function Invoke-UserRegistration([string] $Operation, [Microsoft.Win32.RegistryKey] $Hive, [string] $MachinePath, [string] $ProcessPath) {
    $state = Get-Registration $Hive $MachinePath $ProcessPath -Diagnostics:($Operation -eq 'Diagnostics')
    if ($Operation -in 'Status','Diagnostics') { return $state }
    if ($Operation -eq 'Disable' -and $state.owned) {
        Remove-OwnedPath $Hive
    } elseif ($Operation -in 'Enable','Add' -and $state.status -in 'available','moved') {
        Add-OwnedPath $Hive $state.path
    } else { return $state }
    $next = Get-Registration $Hive $MachinePath $ProcessPath
    $next.changed = $true
    return $next
}

function Remove-OfflineProfilePath([Microsoft.Win32.RegistryKey] $Users, [string] $File) {
    $mount = 'PymssCli-' + [guid]::NewGuid().ToString('N')
    $reg = Join-Path ([Environment]::SystemDirectory) 'reg.exe'
    & $reg load ("HKU\" + $mount) $File | Out-Null
    if ($LASTEXITCODE -ne 0) { throw 'Unable to open the registered user profile.' }
    try {
        $hive = $Users.OpenSubKey($mount)
        try { Remove-OwnedPath $hive } finally { $hive.Close() }
    } finally {
        & $reg unload ("HKU\" + $mount) | Out-Null
        if ($LASTEXITCODE -ne 0) { throw 'Unable to close the registered user profile.' }
    }
}

function Remove-UserRegistrations([Microsoft.Win32.RegistryKey] $Users, [Microsoft.Win32.RegistryKey] $Profiles) {
    # Settings can register PATH on any account, including users now signed out.
    $pattern = '^S-1-5-21-(\d+-){3}\d+$|^S-1-12-1-(\d+-){3}\d+$'
    $sids = @($Users.GetSubKeyNames())
    if ($Profiles) { $sids += $Profiles.GetSubKeyNames() }
    foreach ($sid in ($sids | Sort-Object -Unique)) {
        if ($sid -notmatch $pattern) { continue }
        try {
            $hive = $Users.OpenSubKey($sid)
            if ($hive) {
                try { Remove-OwnedPath $hive } finally { $hive.Close() }
                continue
            }
            if (-not $Profiles) { continue }
            $profile = $Profiles.OpenSubKey($sid)
            if (-not $profile) { continue }
            try { $directory = $profile.GetValue('ProfileImagePath') } finally { $profile.Close() }
            if (-not $directory) { continue }
            $file = Join-Path ([Environment]::ExpandEnvironmentVariables($directory)) 'NTUSER.DAT'
            if ([IO.File]::Exists($file)) { Remove-OfflineProfilePath $Users $file }
        } catch {
            Write-Warning "PATH cleanup failed for $($sid): $($_.Exception.Message)"
        }
    }
}

$StudioDir = Get-ComparablePath $StudioDir
if (-not $StudioDir) { throw 'A valid Studio directory is required.' }
if ($StudioDir.Contains(';')) { throw 'Windows PATH entries cannot contain a semicolon.' }
if ($MyInvocation.InvocationName -eq '.') { return }
function Invoke-RegistrationAction {
    if ($Action -eq 'Remove') {
        $profiles = [Microsoft.Win32.Registry]::LocalMachine.OpenSubKey('SOFTWARE\Microsoft\Windows NT\CurrentVersion\ProfileList')
        try { Remove-UserRegistrations ([Microsoft.Win32.Registry]::Users) $profiles }
        finally { if ($profiles) { $profiles.Close() } }
        exit 0
    }

    $identity = [Security.Principal.WindowsIdentity]::GetCurrent()
    if ($Action -in 'Check','Add' -and ([Security.Principal.WindowsPrincipal]::new($identity)).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) { exit 3 }
    $machinePath = [Environment]::GetEnvironmentVariable('Path','Machine')
    $state = Invoke-UserRegistration $Action ([Microsoft.Win32.Registry]::CurrentUser) $machinePath $env:Path
    if ($Action -in 'Status','Enable','Disable','Diagnostics') {
        [Console]::OutputEncoding = [Text.UTF8Encoding]::new($false)
        $state | ConvertTo-Json -Compress
    } elseif ($state.status -eq 'conflict') {
        Write-PathReport $state.path
        exit 2
    }
}
$mutex = [Threading.Mutex]::new($false, 'Local\PymssStudio-CliPath')
$locked = $false
try {
    try { $locked = $mutex.WaitOne(3000) } catch [Threading.AbandonedMutexException] { $locked = $true }
    if (-not $locked) { throw 'PATH registration is busy. Retry shortly.' }
    Invoke-RegistrationAction
} finally {
    if ($locked) { $mutex.ReleaseMutex() }
    $mutex.Dispose()
}
