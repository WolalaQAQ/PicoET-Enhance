#ifndef BundleDir
  #error BundleDir must name the verified PC Studio bundle directory
#endif
#ifndef OutputDir
  #error OutputDir must name the installer output directory
#endif
#ifndef AppVersion
  #error AppVersion must specify the release version
#endif

[Setup]
AppId={{8E71D432-5F08-4E64-9B3C-7C02E6260A19}
AppName=PicoET PC Studio
AppVersion={#AppVersion}
AppVerName=PicoET PC Studio {#AppVersion}
AppPublisher=PicoFacialBridge
AppPublisherURL=https://github.com/WolalaQAQ/PicoET-Enhance
AppSupportURL=https://github.com/WolalaQAQ/PicoET-Enhance/releases
AppUpdatesURL=https://github.com/WolalaQAQ/PicoET-Enhance/releases
DefaultDirName={localappdata}\Programs\PicoET PC Studio
DefaultGroupName=PicoET PC Studio
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
OutputDir={#OutputDir}
OutputBaseFilename=PicoET-PC-Studio-Setup-v{#AppVersion}
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
CloseApplications=yes
RestartApplications=no
UninstallDisplayIcon={app}\PicoET-PC-Studio.exe
SetupIconFile={#BundleDir}\favicon.ico
LicenseFile={#BundleDir}\LICENSE

[Languages]
Name: "en"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"; Flags: unchecked

[InstallDelete]
Type: filesandordirs; Name: "{app}\_internal"
Type: filesandordirs; Name: "{app}\pc-studio"

[Files]
Source: "{#BundleDir}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\PicoET PC Studio"; Filename: "{app}\PicoET-PC-Studio.exe"; WorkingDir: "{app}"; IconFilename: "{app}\PicoET-PC-Studio.exe"
Name: "{autodesktop}\PicoET PC Studio"; Filename: "{app}\PicoET-PC-Studio.exe"; WorkingDir: "{app}"; IconFilename: "{app}\PicoET-PC-Studio.exe"; Tasks: desktopicon

[Run]
Filename: "{app}\PicoET-PC-Studio.exe"; Description: "{cm:LaunchProgram,PicoET PC Studio}"; Flags: nowait postinstall skipifsilent

[Code]
procedure StopRunningStudio();
var
  ExePath: String;
  ResultCode: Integer;
begin
  ExePath := ExpandConstant('{app}\PicoET-PC-Studio.exe');
  if FileExists(ExePath) then
  begin
    Exec(ExePath, '--stop', ExpandConstant('{app}'), SW_HIDE, ewWaitUntilTerminated, ResultCode);
  end;
end;

function PrepareToInstall(var NeedsRestart: Boolean): String;
begin
  StopRunningStudio();
  Result := '';
end;

function InitializeUninstall(): Boolean;
begin
  StopRunningStudio();
  Result := True;
end;
