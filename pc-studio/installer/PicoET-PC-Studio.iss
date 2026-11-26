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
SetupIconFile=..\src-tauri\icons\icon.ico
LicenseFile={#BundleDir}\LICENSE

[Languages]
Name: "en"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"; Flags: unchecked

[InstallDelete]
Type: filesandordirs; Name: "{app}\_internal"
Type: filesandordirs; Name: "{app}\pc-studio"

[Files]
Source: "{#BundleDir}\PicoET-PC-Studio.exe"; DestDir: "{app}"; Flags: ignoreversion
Source: "{#BundleDir}\*"; DestDir: "{app}"; Excludes: "PicoET-PC-Studio.exe"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\PicoET PC Studio"; Filename: "{app}\PicoET-PC-Studio.exe"; WorkingDir: "{app}"; IconFilename: "{app}\PicoET-PC-Studio.exe"
Name: "{autodesktop}\PicoET PC Studio"; Filename: "{app}\PicoET-PC-Studio.exe"; WorkingDir: "{app}"; IconFilename: "{app}\PicoET-PC-Studio.exe"; Tasks: desktopicon

[Run]
Filename: "{app}\PicoET-PC-Studio.exe"; Description: "{cm:LaunchProgram,PicoET PC Studio}"; Flags: nowait postinstall skipifsilent

[Code]
function HasValidWebView2Version(const RootKey: Integer; const SubKey: String): Boolean;
var
  Ver: String;
begin
  Result := RegQueryStringValue(RootKey, SubKey, 'pv', Ver) and
            (Trim(Ver) <> '') and (Trim(Ver) <> '0.0.0.0');
end;

function IsWebView2Installed(): Boolean;
const
  WV2KeyWow64 = 'SOFTWARE\WOW6432Node\Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}';
  WV2KeyNative = 'SOFTWARE\Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}';
  WV2KeyUser = 'Software\Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}';
begin
  Result := HasValidWebView2Version(HKLM, WV2KeyWow64) or
            HasValidWebView2Version(HKLM, WV2KeyNative) or
            HasValidWebView2Version(HKCU, WV2KeyUser);
end;

function InitializeSetup(): Boolean;
begin
  Result := True;
  if (not WizardSilent()) and (not IsWebView2Installed()) then
  begin
    if MsgBox(
      'Microsoft Edge WebView2 Runtime was not detected on this PC.' + #13#10 + #13#10 +
      'PicoET PC Studio requires the Evergreen Microsoft Edge WebView2 Runtime to run.' + #13#10 +
      'Download it from: https://developer.microsoft.com/en-us/microsoft-edge/webview2/' + #13#10 + #13#10 +
      'Continue installing PicoET PC Studio anyway?',
      mbConfirmation, MB_YESNO) = IDNO then
    begin
      Result := False;
    end;
  end;
end;

procedure RunStopCommand(const ExePath, WorkDir: String; const WaitMs: Integer);
var
  ResultCode: Integer;
begin
  if FileExists(ExePath) then
  begin
    Exec(ExePath, '--stop', WorkDir, SW_HIDE, ewWaitUntilTerminated, ResultCode);
    if WaitMs > 0 then
      Sleep(WaitMs);
  end;
end;

function PrepareToInstall(var NeedsRestart: Boolean): String;
var
  TempExe: String;
  StoppedViaTemp: Boolean;
begin
  StoppedViaTemp := False;
  try
    ExtractTemporaryFile('PicoET-PC-Studio.exe');
    TempExe := ExpandConstant('{tmp}\PicoET-PC-Studio.exe');
    if FileExists(TempExe) then
    begin
      RunStopCommand(TempExe, ExpandConstant('{tmp}'), 500);
      StoppedViaTemp := True;
    end;
  except
    StoppedViaTemp := False;
  end;

  if not StoppedViaTemp then
    RunStopCommand(ExpandConstant('{app}\PicoET-PC-Studio.exe'), ExpandConstant('{app}'), 500);

  Result := '';
end;

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
begin
  if CurUninstallStep = usUninstall then
  begin
    RunStopCommand(ExpandConstant('{app}\PicoET-PC-Studio.exe'), ExpandConstant('{app}'), 300);
  end;
end;
