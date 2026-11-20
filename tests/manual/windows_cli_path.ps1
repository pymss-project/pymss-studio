$ErrorActionPreference = 'Stop'
$fixture = 'Software\PymssStudioTests-' + [guid]::NewGuid().ToString('N')
$hive = [Microsoft.Win32.Registry]::CurrentUser.CreateSubKey($fixture)
$root = Join-Path ([IO.Path]::GetTempPath()) ("pymss cli " + [guid]::NewGuid().ToString('N'))
$portable = Join-Path $root "Studio's copy"
$other = Join-Path $root 'other python'
[IO.Directory]::CreateDirectory($portable) | Out-Null
[IO.Directory]::CreateDirectory($other) | Out-Null
try {
    . ./installer/cli-path.ps1 -Action Check -StudioDir $portable
    function Run([string]$Verb, [string]$MachinePath = '', [string]$ProcessPath = '') {
        Invoke-UserRegistration $Verb $hive $MachinePath $ProcessPath
    }
    $launcher = Join-Path $portable 'pymss.exe'
    $missing = Run Diagnostics
    if ($missing.diagnostics.launcher -ne $launcher -or $missing.diagnostics.launcherExists -or $missing.changed -or $hive.GetSubKeyNames().Count -ne 0) {
        throw 'Diagnostics did not report the missing launcher without writing registry keys'
    }
    $json = ($missing | ConvertTo-Json -Compress | ConvertFrom-Json).diagnostics
    if ($null -ne $json.persistedCommand -or $null -ne $json.processCommand -or $null -ne $json.registeredDirectory) {
        throw 'Diagnostics must serialize absent paths as JSON null'
    }
    [IO.File]::WriteAllText($launcher,'')
    if ((Run Status).status -ne 'available' -or $hive.GetSubKeyNames().Count -ne 0) { throw 'Inspection created registry keys' }
    $key = $hive.CreateSubKey('Environment')
    $raw = '%USERPROFILE%\bin;C:\Other Dir'
    $key.SetValue('Path',$raw,[Microsoft.Win32.RegistryValueKind]::ExpandString)

    [IO.File]::WriteAllText((Join-Path $other 'pymss.cmd'),'')
    $blocked = Run Enable $other
    if ($blocked.status -ne 'conflict' -or $blocked.path -ne (Join-Path $other 'pymss.cmd') -or $blocked.changed) { throw 'Missed external pymss.cmd' }
    if ((Run Enable '' $other).status -ne 'conflict') { throw 'Missed an active shell environment' }
    if ($key.GetValue('Path',$null,$registryRaw) -cne $raw) { throw 'Conflict modified PATH' }

    $added = Run Enable
    if ($added.status -ne 'enabled' -or -not $added.owned -or -not $added.changed) { throw 'Enable did not register this copy' }
    if ((Run Enable).changed -or $key.GetValue('Path',$null,$registryRaw) -cne "$raw;$portable") { throw 'Duplicate PATH registration' }
    $otherCommand = Join-Path $other 'pymss.cmd'
    $diagnostics = (Run Diagnostics '' $other).diagnostics
    if (-not $diagnostics.launcherExists -or -not $diagnostics.userPathContainsStudio -or $diagnostics.systemPathContainsStudio -or
        $diagnostics.registeredDirectory -ne $portable -or $diagnostics.persistedCommand -ne $launcher -or $diagnostics.processCommand -ne $otherCommand) {
        throw 'Diagnostics did not distinguish saved PATH from the process PATH'
    }
    $diagnostics = (Run Diagnostics $other $portable).diagnostics
    if ($diagnostics.persistedCommand -ne $otherCommand -or $diagnostics.processCommand -ne $launcher) {
        throw 'Diagnostics did not preserve system PATH precedence'
    }
    $key.SetValue('Path',"$raw;$portable;C:\Later",[Microsoft.Win32.RegistryValueKind]::ExpandString)
    $StudioDir = Join-Path $root 'moved'
    [IO.Directory]::Move($portable,$StudioDir)
    $moved = Run Status
    if ($moved.status -ne 'moved' -or $moved.path -ne $portable) { throw 'Moved copy not detected' }
    $diagnostics = (Run Diagnostics).diagnostics
    if ($diagnostics.registeredDirectory -ne $portable -or $diagnostics.userPathContainsStudio -or $diagnostics.persistedCommand) {
        throw 'Diagnostics did not report the previous registration of a moved copy'
    }
    if ((Run Enable $other).status -ne 'conflict') { throw 'Move shadowed an existing command' }
    if (-not (Run Enable).owned -or $key.GetValue('Path',$null,$registryRaw) -cne "$raw;$StudioDir;C:\Later") { throw 'Move corrupted PATH' }
    $disabled = Run Disable '' $StudioDir
    if ($disabled.status -ne 'available' -or -not $disabled.changed -or (Run Disable).changed) { throw 'Disable used stale process PATH' }
    if ($key.GetValue('Path',$null,$registryRaw) -cne "$raw;C:\Later" -or $key.GetValueKind('Path') -ne 'ExpandString') { throw 'Unregister modified unrelated entries or value kind' }

    $key.SetValue('Path',$StudioDir,[Microsoft.Win32.RegistryValueKind]::String)
    $external = Run Enable
    if ($external.owned -or $external.changed -or $external.status -ne 'enabled' -or (Run Disable).changed) { throw 'Claimed manually registered directory' }
    if ($key.GetValue('Path') -cne $StudioDir) { throw 'Removed a manual entry' }
    $key.DeleteValue('Path')
    if ((Run Enable $StudioDir).owned -or $null -ne $key.GetValue('Path')) { throw 'Duplicated system PATH registration' }
    $diagnostics = (Run Diagnostics $StudioDir).diagnostics
    if (-not $diagnostics.systemPathContainsStudio -or $diagnostics.userPathContainsStudio -or $diagnostics.registeredDirectory) {
        throw 'Diagnostics did not distinguish a manually registered system entry'
    }
    $null = Run Add
    if (-not (Run Status).owned) { throw 'Settings failed to recognize installer registration' }
    Remove-OwnedPath $hive
    if ($null -ne $key.GetValue('Path')) { throw 'Installer cleanup failed' }

    $profiles = $hive.CreateSubKey('ProfileList')
    $signedOut = $hive.CreateSubKey('signed-out')
    $manual = $hive.CreateSubKey('manual')
    try {
        if (-not (Invoke-UserRegistration Enable $signedOut '' '').owned) { throw 'Settings registration failed' }
        $manualEnv = $manual.CreateSubKey('Environment')
        try { $manualEnv.SetValue('Path', $StudioDir) } finally { $manualEnv.Close() }
        foreach ($entry in @{ 'S-1-5-21-1-2-3-1001' = 'signed-out'; 'S-1-5-21-1-2-3-1002' = 'manual' }.GetEnumerator()) {
            $profileDir = Join-Path $root $entry.Value
            [IO.Directory]::CreateDirectory($profileDir) | Out-Null
            [IO.File]::WriteAllText((Join-Path $profileDir 'NTUSER.DAT'), '')
            $profile = $profiles.CreateSubKey($entry.Key)
            try { $profile.SetValue('ProfileImagePath', $profileDir) } finally { $profile.Close() }
        }
        # Simulate mounting the two offline hives without loading a real account.
        function Remove-OfflineProfilePath([Microsoft.Win32.RegistryKey] $Users, [string] $File) {
            $profileHive = $Users.OpenSubKey([IO.Path]::GetFileName([IO.Path]::GetDirectoryName($File)))
            try { Remove-OwnedPath $profileHive } finally { $profileHive.Close() }
        }
        Remove-UserRegistrations $hive $profiles
        if ((Invoke-UserRegistration Status $signedOut '' '').status -ne 'available') { throw 'Offline settings registration was left behind' }
        $manualEnv = $manual.OpenSubKey('Environment')
        try {
            if ($manualEnv.GetValue('Path') -ne $StudioDir) { throw 'Uninstall changed an unowned PATH entry' }
        } finally { $manualEnv.Close() }
    } finally {
        $signedOut.Close()
        $manual.Close()
        $profiles.Close()
    }
    'Isolated registry: inspect, enable, conflict, move, disable and signed-out user cleanup passed'
} finally {
    if ($key) { $key.Close() }
    $hive.Close()
    [Microsoft.Win32.Registry]::CurrentUser.DeleteSubKeyTree($fixture,$false)
    [IO.Directory]::Delete($root,$true)
}
