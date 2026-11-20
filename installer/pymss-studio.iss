; Pymss Studio Windows CUDA installer.
; The source directory is prepared by GitHub Actions and passed through PYMSS_PORTABLE_DIR.

#define MyAppName "Pymss Studio"
#define MyAppVersion GetEnv("PYMSS_VERSION") == "" ? "0.0.1" : GetEnv("PYMSS_VERSION")
#define MyAppPublisher "TheSmallHanCat"
#define MyAppExeName "Pymss Studio.exe"
#define SourceDir GetEnv("PYMSS_PORTABLE_DIR")
#define OutputDir GetEnv("PYMSS_INSTALLER_OUTPUT") == "" ? "..\release" : GetEnv("PYMSS_INSTALLER_OUTPUT")
#define PackageSuffix GetEnv("PYMSS_PACKAGE_SUFFIX") == "" ? "windows-x64-cuda" : GetEnv("PYMSS_PACKAGE_SUFFIX")

#if SourceDir == ""
  #error PYMSS_PORTABLE_DIR is required. It must point to the staged portable directory.
#endif

[Setup]
; Unique AppId for Pymss Studio. Do not reuse AppIds from other projects.
AppId={{6A208087-F154-4C62-8916-E3D40B7C0F24}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppPublisher={#MyAppPublisher}
AppVerName={#MyAppName} {#MyAppVersion}
DefaultDirName={autopf}\{#MyAppName}
DefaultGroupName={#MyAppName}
DisableProgramGroupPage=yes
; Select the installer process architecture independently of the install mode.
SetupArchitecture=x64
ArchitecturesAllowed=x64
ArchitecturesInstallIn64BitMode=x64
OutputDir={#OutputDir}
OutputBaseFilename=Pymss-Studio-{#MyAppVersion}-{#PackageSuffix}-setup
Compression=lzma2/max
LZMAUseSeparateProcess=yes
LZMADictionarySize=1048576
SolidCompression=yes
WizardStyle=modern
SetupIconFile=..\src-tauri\icons\icon.ico
UninstallDisplayIcon={app}\{#MyAppExeName}
CloseApplications=yes
RestartApplications=no
ChangesEnvironment=yes
DirExistsWarning=no

[Languages]
Name: "chinesesimplified"; MessagesFile: "compiler:Languages\ChineseSimplified.isl"
Name: "english"; MessagesFile: "compiler:Default.isl"

[CustomMessages]
english.CliTask=Make pymss available in this user's terminal
chinesesimplified.CliTask=在当前用户的终端中使用 pymss
english.CliConflict=An existing pymss command was found. Its PATH will be preserved.%n%1%nStudio CLI: %2
chinesesimplified.CliConflict=已检测到现有 pymss 命令，将保留原有 PATH。%n%1%nStudio 命令行：%2
english.CliSkipped=User PATH registration was skipped. Studio CLI: %1
chinesesimplified.CliSkipped=已跳过用户 PATH 注册。Studio 命令行：%1

[Tasks]
Name: "clipath"; Description: "{cm:CliTask}"
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"; Flags: unchecked

[Files]
Source: "cli-path.ps1"; Flags: dontcopy
Source: "cli-path.ps1"; DestDir: "{app}"; Flags: ignoreversion
Source: "{#SourceDir}\{#MyAppExeName}"; DestDir: "{app}"; Flags: ignoreversion
Source: "{#SourceDir}\pymss.exe"; DestDir: "{app}"; Flags: ignoreversion
Source: "{#SourceDir}\*"; DestDir: "{app}"; Excludes: "\pymss.exe,python-runtime\runtime-envs\*"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "{#SourceDir}\python-runtime\runtime-envs\*"; DestDir: "{app}\python-runtime\runtime-envs"; Excludes: "active-runtime.json"; Flags: ignoreversion recursesubdirs createallsubdirs skipifsourcedoesntexist; Check: ShouldInstallBundledRuntimeEnvs
; The bootstrap runtime stays read-only; only runtime-envs is writable for managed installs.

[Dirs]
; Keep the bootstrap interpreter read-only. Only managed environments need user write access.
Name: "{app}\python-runtime\runtime-envs"; Permissions: users-modify

[Icons]
Name: "{autoprograms}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; Tasks: desktopicon

[Run]
Filename: "{app}\bin\VC_redist.x64.exe"; Parameters: "/install /quiet /norestart"; StatusMsg: "Installing Microsoft Visual C++ Runtime..."; Flags: waituntilterminated runhidden skipifdoesntexist; Check: not IsVCRedistX64Installed
Filename: "{cmd}"; Parameters: "/C del /F /Q ""{app}\bin\VC_redist.x64.exe"""; Flags: runhidden waituntilterminated skipifdoesntexist; Check: not IsVCRedistX64Installed
Filename: "{app}\{#MyAppExeName}"; Description: "{cm:LaunchProgram,{#StringChange(MyAppName, '&', '&&')}}"; Flags: nowait postinstall skipifsilent

[UninstallDelete]
Type: files; Name: "{app}\pymss-studio.cli-uninstall-v1"

[Code]
var
  RegisterCliPath: Boolean;
  FreshInstallKnown: Boolean;
  FreshInstallResult: Boolean;

function IsVCRedistX64Installed(): Boolean;
var
  Installed: Cardinal;
begin
  Result :=
    RegQueryDWordValue(HKLM64, 'SOFTWARE\Microsoft\VisualStudio\14.0\VC\Runtimes\x64', 'Installed', Installed)
    and (Installed = 1);
end;

function IsFreshInstall(): Boolean;
begin
  Result := not FileExists(ExpandConstant('{app}\pymss-studio.inno-install'));
end;

function CachedFreshInstall(): Boolean;
begin
  if not FreshInstallKnown then
  begin
    FreshInstallResult := IsFreshInstall();
    FreshInstallKnown := True;
  end;
  Result := FreshInstallResult;
end;

function BundledBackend(): string;
begin
  if Pos('cpu', LowerCase('{#PackageSuffix}')) > 0 then
    Result := 'cpu'
  else if Pos('cuda', LowerCase('{#PackageSuffix}')) > 0 then
    Result := 'cuda'
  else if Pos('rocm', LowerCase('{#PackageSuffix}')) > 0 then
    Result := 'rocm'
  else
    Result := '';
end;

function ExistingBundledBackendPython(): Boolean;
var
  Backend: string;
begin
  Backend := BundledBackend();
  if Backend = '' then
  begin
    Result := False;
    Exit;
  end;
  Result := FileExists(
    ExpandConstant('{app}\python-runtime\runtime-envs\') + Backend + '\Scripts\python.exe'
  );
end;

function ShouldInstallBundledRuntimeEnvs(): Boolean;
begin
  { Fresh installs and upgrades from layouts without the current backend receive the package env.
    A valid current backend is preserved so an upgrade does not replace a user-managed env. }
  Result := CachedFreshInstall() or not ExistingBundledBackendPython();
end;

procedure RemoveIfExists(Path: string);
begin
  if DirExists(Path) then
  begin
    DelTree(Path, True, True, True);
  end
  else if FileExists(Path) then
  begin
    DeleteFile(Path);
  end;
end;

procedure CleanupInstallTree();
begin
  { Remove known development leftovers if they ever slip into the staged directory. }
  RemoveIfExists(ExpandConstant('{app}') + '\.git');
  RemoveIfExists(ExpandConstant('{app}') + '\.github');
  RemoveIfExists(ExpandConstant('{app}') + '\.claude');
  RemoveIfExists(ExpandConstant('{app}') + '\.omc');
  RemoveIfExists(ExpandConstant('{app}') + '\.spec-workflow');
  RemoveIfExists(ExpandConstant('{app}') + '\__pycache__');
  RemoveIfExists(ExpandConstant('{app}') + '\python-runtime\Doc');
  RemoveIfExists(ExpandConstant('{app}') + '\python-runtime\include');
  RemoveIfExists(ExpandConstant('{app}') + '\python-runtime\libs');
end;

procedure RepairVenvConfig(Backend: string);
var
  EnvDir: string;
  ConfigPath: string;
  PythonRuntimeDir: string;
  PythonExe: string;
  Content: string;
begin
  EnvDir := ExpandConstant('{app}') + '\python-runtime\runtime-envs\' + Backend;
  ConfigPath := EnvDir + '\pyvenv.cfg';
  PythonRuntimeDir := ExpandConstant('{app}') + '\python-runtime';
  PythonExe := PythonRuntimeDir + '\python.exe';

  if (not FileExists(ConfigPath)) or (not FileExists(PythonExe)) then
  begin
    Exit;
  end;

  Content :=
    'home = ' + PythonRuntimeDir + #13#10 +
    'include-system-site-packages = false' + #13#10 +
    'executable = ' + PythonExe + #13#10 +
    'command = ' + PythonExe + ' -m venv ' + EnvDir + #13#10;
  if not SaveStringToFile(ConfigPath, Content, False) then
  begin
    RaiseException('Failed to write Python venv config: ' + ConfigPath);
  end;

  if Backend = 'rocm' then
  begin
    { ROCm's pip console launcher embeds the CI Python path and is not relocatable. }
    RemoveIfExists(EnvDir + '\Scripts\offload-arch.exe');
  end;
end;

procedure RepairBundledRuntimeEnvs();
begin
  RepairVenvConfig('cpu');
  RepairVenvConfig('cuda');
  RepairVenvConfig('rocm');
end;

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
var
  Script: string;
  Code: Integer;
begin
  if CurUninstallStep <> usUninstall then Exit;
  { The uninstall helper runs elevated, including for a custom writable install directory. }
  Script := ExpandConstant('{tmp}\studio-cli-uninstall.ps1');
  if not CopyFile(ExpandConstant('{app}\cli-path.ps1'), Script, False) then begin
    Log('Skipping PATH cleanup: the installed helper could not be copied.');
    Exit;
  end;
  if CompareText(GetSHA256OfFile(Script), '{#GetSHA256OfFile(SourcePath + "cli-path.ps1")}') <> 0 then begin
    Log('Skipping PATH cleanup: the installed helper could not be verified.');
    Exit;
  end;
  if not Exec(ExpandConstant('{sys}\WindowsPowerShell\v1.0\powershell.exe'),
    '-NoProfile -NonInteractive -ExecutionPolicy Bypass -File "' + Script + '" Remove "' +
    ExpandConstant('{app}') + '"', '', SW_HIDE, ewWaitUntilTerminated, Code) then
    Log('Unable to start PATH cleanup: ' + SysErrorMessage(Code))
  else if Code <> 0 then
    Log('PATH cleanup failed with exit code ' + IntToStr(Code));
end;

procedure ConfigureCliPath(Action: string);
var
  Script, Report, Params, MessageText, Launcher: string;
  Found: AnsiString;
  Code: Integer;
begin
  RegisterCliPath := False;
  Launcher := ExpandConstant('{app}\pymss.exe');
  Report := ExpandConstant('{tmp}\pymss-command.txt');
  if Action = 'Check' then begin
    ExtractTemporaryFile('cli-path.ps1');
    Script := ExpandConstant('{tmp}\cli-path.ps1');
  end else Script := ExpandConstant('{app}\cli-path.ps1');
  Params := '-NoProfile -NonInteractive -ExecutionPolicy Bypass -File "' + Script + '" ' +
    Action + ' "' + ExpandConstant('{app}') + '" -Report "' + Report + '"';
  DeleteFile(Report);
  if ExecAsOriginalUser(ExpandConstant('{sys}\WindowsPowerShell\v1.0\powershell.exe'),
      Params, '', SW_HIDE, ewWaitUntilTerminated, Code) and (Code = 0) then begin
    RegisterCliPath := True;
    Exit;
  end;
  MessageText := FmtMessage(CustomMessage('CliSkipped'), [Launcher]);
  if (Code = 2) and LoadStringFromFile(Report, Found) then
    MessageText := FmtMessage(CustomMessage('CliConflict'), [UTF8Decode(Found), Launcher]);
  Log(MessageText);
  SuppressibleMsgBox(MessageText, mbInformation, MB_OK, IDOK);
end;

procedure CurStepChanged(CurStep: TSetupStep);
begin
  if CurStep = ssInstall then
  begin
    FreshInstallResult := IsFreshInstall();
    FreshInstallKnown := True;
    if WizardIsTaskSelected('clipath') then ConfigureCliPath('Check');
  end;
  if CurStep = ssPostInstall then
  begin
    if RegisterCliPath then ConfigureCliPath('Add');
    RepairBundledRuntimeEnvs();
    CleanupInstallTree();
    SaveStringToFile(ExpandConstant('{app}') + '\pymss-studio.inno-install', 'managed' + #13#10, False);
    SaveStringToFile(ExpandConstant('{app}\pymss-studio.cli-uninstall-v1'), '1' + #13#10, False);
  end;
end;
