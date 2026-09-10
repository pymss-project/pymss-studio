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
DirExistsWarning=no

[Languages]
Name: "chinesesimplified"; MessagesFile: "compiler:Languages\ChineseSimplified.isl"
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"; Flags: unchecked

[Files]
Source: "{#SourceDir}\{#MyAppExeName}"; DestDir: "{app}"; Flags: ignoreversion
Source: "{#SourceDir}\*"; DestDir: "{app}"; Excludes: "python-runtime\runtime-envs\*"; Flags: ignoreversion recursesubdirs createallsubdirs
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

[Code]
var
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

procedure CurStepChanged(CurStep: TSetupStep);
begin
  if CurStep = ssInstall then
  begin
    FreshInstallResult := IsFreshInstall();
    FreshInstallKnown := True;
  end;
  if CurStep = ssPostInstall then
  begin
    RepairBundledRuntimeEnvs();
    CleanupInstallTree();
    SaveStringToFile(ExpandConstant('{app}') + '\pymss-studio.inno-install', 'managed' + #13#10, False);
  end;
end;
