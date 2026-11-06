; Inno Setup 6 script: the per-user installer FlangeCutter-Setup-<version>.exe.
;
; build\build_windows.ps1 compiles it and passes:
;   /DAppVersion=1.0.0  /DAppNumericVersion=1.0.0.0  /DDistDir=<the one-folder build>  /DOutputDir=<build\out>
;   and, only when signing: /DSign /Sflangesign=<signtool command with $f>
;
; Per-user install without admin rights: Setup never shows a UAC prompt and installs to
; %LOCALAPPDATA%\Programs\FlangeCutter. The user's settings and logs in %LOCALAPPDATA%\FlangeCutter
; are never touched, on install, upgrade or uninstall.

#ifndef AppVersion
  #error Run build\build_windows.ps1, or pass /DAppVersion=... /DAppNumericVersion=... /DDistDir=... to ISCC.
#endif
#ifndef OutputDir
  #define OutputDir "out"
#endif
#define AppName "Flange Cutter"
#define AppExe "FlangeCutter.exe"

[Setup]
; Never change the AppId: it is how a newer version finds and replaces an older one.
AppId={{508B22D7-9243-4A8F-A8C6-B0873931CFEB}
AppName={#AppName}
AppVersion={#AppVersion}
AppVerName={#AppName} {#AppVersion}
AppPublisher=SilvestrTk
AppCopyright=Copyright (c) SilvestrTk. MIT License.
VersionInfoVersion={#AppNumericVersion}
VersionInfoProductVersion={#AppNumericVersion}
VersionInfoCompany=SilvestrTk
VersionInfoProductName={#AppName}
VersionInfoDescription={#AppName} Setup
; Per user, never elevated. {autopf} is %LOCALAPPDATA%\Programs in this mode.
PrivilegesRequired=lowest
DefaultDirName={autopf}\FlangeCutter
DisableDirPage=yes
DisableProgramGroupPage=yes
AlwaysShowDirOnReadyPage=yes
UninstallDisplayName={#AppName}
UninstallDisplayIcon={app}\{#AppExe}
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
MinVersion=10.0
OutputDir={#OutputDir}
OutputBaseFilename=FlangeCutter-Setup-{#AppVersion}
SetupIconFile=flangecutter.ico
WizardStyle=modern
Compression=lzma2/max
SolidCompression=yes
SetupMutex=FlangeCutterSetup
; A running Flange Cutter is first asked to quit ([Code] below). If it does not, Restart Manager
; closes it (the user confirms it on the "Preparing to Install" page; silent installs close it).
CloseApplications=force
CloseApplicationsFilter=*.exe,*.dll,*.pyd
RestartApplications=no
#ifdef Sign
SignTool=flangesign
SignedUninstaller=yes
#endif

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"; Flags: unchecked

[InstallDelete]
; Remove the libraries of an older version first, so none is left behind after an upgrade.
Type: filesandordirs; Name: "{app}\_internal"

[Files]
Source: "{#DistDir}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{autoprograms}\{#AppName}"; Filename: "{app}\{#AppExe}"; WorkingDir: "{app}"; Comment: "Cut mold STLs and add a bolted flange"
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\{#AppExe}"; WorkingDir: "{app}"; Comment: "Cut mold STLs and add a bolted flange"; Tasks: desktopicon

[Run]
Filename: "{app}\{#AppExe}"; Description: "{cm:LaunchProgram,{#AppName}}"; Flags: nowait postinstall skipifsilent

[UninstallDelete]
; Only the program folder. The user's settings and logs (%LOCALAPPDATA%\FlangeCutter) stay.
Type: filesandordirs; Name: "{app}\_internal"

[Code]
const
  OpenReadWrite = $0002;    // TFileStream mode values
  ShareDenyNone = $0040;
  ProxyDirect = 1;          // WinHttpRequest: never use a proxy for 127.0.0.1

// The app's data folder, by the same rule as app_dir() in flangecutter/session.py.
function AppDataDir(): String;
begin
  Result := GetEnv('FLANGECUTTER_HOME');
  if Result = '' then
    Result := ExpandConstant('{localappdata}\FlangeCutter');
end;

// True while the installed FlangeCutter.exe runs: Windows does not let anyone open a running EXE for writing.
function AppIsRunning(): Boolean;
var
  Exe: String;
  Stream: TFileStream;
begin
  Result := False;
  Exe := ExpandConstant('{app}\{#AppExe}');
  if not FileExists(Exe) then
    Exit;
  try
    Stream := TFileStream.Create(Exe, OpenReadWrite or ShareDenyNone);
    Stream.Free;
  except
    Result := True;
  end;
end;

// The port of the running app, from its lock file ("port": 50123, ...); 0 if unknown.
function AppPort(): Integer;
var
  Lock: AnsiString;
  Text: String;
  I: Integer;
begin
  Result := 0;
  if not LoadStringFromFile(AddBackslash(AppDataDir()) + 'flangecutter.lock', Lock) then
    Exit;
  Text := String(Lock);
  I := Pos('"port":', Text);
  if I = 0 then
    Exit;
  I := I + 7;
  while (I <= Length(Text)) and (Text[I] = ' ') do
    I := I + 1;
  while (I <= Length(Text)) and (Text[I] >= '0') and (Text[I] <= '9') and (Result < 65536) do begin
    Result := Result * 10 + Ord(Text[I]) - Ord('0');
    I := I + 1;
  end;
end;

// Asks the app to quit, as its Quit button does (POST /api/shutdown), and waits up to 10 s.
function CloseApp(): Boolean;
var
  Port, Status, I: Integer;
  Request: Variant;
begin
  Result := False;
  Port := AppPort();
  Log(Format('Flange Cutter is running; asking it to quit (port %d).', [Port]));
  if Port = 0 then
    Exit;
  try
    Request := CreateOleObject('WinHttp.WinHttpRequest.5.1');
    Request.SetProxy(ProxyDirect);
    Request.SetTimeouts(2000, 2000, 2000, 5000);
    Request.Open('POST', 'http://127.0.0.1:' + IntToStr(Port) + '/api/shutdown', False);
    Request.SetRequestHeader('Content-Type', 'application/json');
    Request.Send('{}');
    Status := Request.Status;
    Log(Format('Flange Cutter answered HTTP %d.', [Status]));
  except
    Log('Could not reach Flange Cutter: ' + GetExceptionMessage);
    Exit;
  end;
  for I := 1 to 50 do begin
    if not AppIsRunning() then begin
      Log('Flange Cutter has quit.');
      Result := True;
      Exit;
    end;
    Sleep(200);
  end;
end;

function PrepareToInstall(var NeedsRestart: Boolean): String;
begin
  Result := '';
  if not AppIsRunning() then
    Exit;
  if SuppressibleMsgBox('Flange Cutter is running. Setup will close it.' + #13#10#13#10 +
       'Click OK to close it and continue, or Cancel to save your work first.',
       mbConfirmation, MB_OKCANCEL, IDOK) <> IDOK then
    Result := 'Flange Cutter is still running. Close it, then start Setup again.'
  else if not CloseApp() then
    Log('Flange Cutter did not quit; Restart Manager will close it.');
end;

function InitializeUninstall(): Boolean;
begin
  Result := True;
  if not AppIsRunning() then
    Exit;
  Result := SuppressibleMsgBox('Flange Cutter is running. It will be closed before it is removed.',
                               mbConfirmation, MB_OKCANCEL, IDOK) = IDOK;
  if Result and not CloseApp() then begin
    SuppressibleMsgBox('Flange Cutter could not be closed. Close its window, then try again.', mbError, MB_OK, IDOK);
    Result := False;
  end;
end;
