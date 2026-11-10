; TS404 Panel installer (Inno Setup 6). Built by ..\build.bat after PyInstaller.
#define AppVersion "1.1.2"

[Setup]
AppId={{6E3B9D2A-4C71-4F0E-9A5B-2D8F1C7E4A60}
AppName=404ever
AppVersion={#AppVersion}
AppVerName=404ever {#AppVersion}
AppPublisher=Pafu Igniters
AppPublisherURL=https://github.com/MickeyFLStudio/404ever
AppSupportURL=https://github.com/MickeyFLStudio/404ever/issues
DefaultDirName={autopf}\404ever
DefaultGroupName=404ever
DisableDirPage=yes
DisableProgramGroupPage=yes
; admin: the "TS404 Panel" MIDI port is a system-wide MME driver (HKLM\...\Drivers32)
PrivilegesRequired=admin
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
OutputDir=Output
OutputBaseFilename=404ever-Setup-{#AppVersion}
Compression=lzma2/ultra64
SolidCompression=yes
; same dark Pafu look as ReWasp and FL Strip Club
WizardStyle=modern dark hidebevels
WizardBackColor=#0B0B0F
DisableWelcomePage=no
UninstallDisplayName=404ever: the TS404's window, back in FL Studio
UninstallDisplayIcon={app}\TS404Panel.exe
SetupIconFile=..\brand\app.ico
WizardImageFile=..\brand\wizard_small.png,..\brand\wizard.png
WizardSmallImageFile=..\brand\corner_small.png,..\brand\corner.png
WizardImageBackColor=#0B0B0F

[Tasks]
Name: "desktopicon"; Description: "Create a desktop shortcut for the free-standing panel window"; GroupDescription: "Options:"; Flags: unchecked

[Files]
Source: "..\dist\TS404Panel\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs
; the MIDI port driver goes to System32 like every MME driver (winmm cannot load a driver from a path with
; spaces); every 64-bit program that uses MIDI loads it, so it may be in use (replaced at restart then)
Source: "..\ts404_midi.dll"; DestDir: "{sys}"; Flags: ignoreversion restartreplace uninsrestartdelete
; puts the driver's registry entry back whenever Windows drops it (scheduled task, see the script)
Source: "driver_guard.ps1"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\README.md"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\LICENSE"; DestDir: "{app}"; Flags: ignoreversion
; the controller script FL Studio runs (Options > MIDI settings > controller type "TS404 Panel")
Source: "..\fl\device_TS404Panel.py"; DestDir: "{userdocs}\Image-Line\FL Studio\Settings\Hardware\TS404 Panel"; Flags: ignoreversion

[Icons]
Name: "{group}\TS404 Panel"; Filename: "{app}\TS404Panel.exe"; Parameters: "--panel"; Comment: "Free-standing TS404 panel window"
Name: "{group}\TS404 Panel service"; Filename: "{app}\TS404Panel.exe"; Comment: "Puts the panel inside FL's TS404 windows (runs in the background)"
Name: "{group}\Uninstall 404ever"; Filename: "{uninstallexe}"
Name: "{autodesktop}\TS404 Panel"; Filename: "{app}\TS404Panel.exe"; Parameters: "--panel"; Tasks: desktopicon

[Run]
Filename: "{app}\TS404Panel.exe"; Description: "Start the panel service now (it quits by itself when no TS404 is in use)"; Flags: nowait postinstall skipifsilent runasoriginaluser

[UninstallRun]
Filename: "{sys}\WindowsPowerShell\v1.0\powershell.exe"; Parameters: "-NoProfile -NonInteractive -ExecutionPolicy Bypass -File ""{app}\driver_guard.ps1"" -Uninstall"; Flags: runhidden; RunOnceId: "driverguard"
Filename: "taskkill.exe"; Parameters: "/IM TS404Panel.exe /F"; Flags: runhidden; RunOnceId: "killservice"

[UninstallDelete]
Type: filesandordirs; Name: "{localappdata}\TS404 Panel"
Type: filesandordirs; Name: "{userdocs}\Image-Line\FL Studio\Settings\Hardware\TS404 Panel"
; the TS404 picker entry the service copies into FL's plugin database
Type: files; Name: "{userdocs}\Image-Line\FL Studio\Presets\Plugin database\Generators\Synth classic\TS404 bassline synthesizer.fst"
Type: files; Name: "{userdocs}\Image-Line\FL Studio\Presets\Plugin database\Installed\Generators\Fruity\TS404 bassline synthesizer.fst"

[Messages]
WelcomeLabel1=404ever
WelcomeLabel2=FL Studio 2025 and 2026 dropped the TS404's window but still play it. 404ever puts a window back inside FL: open a TS404 channel and its knobs are there again, driving FL's own TS404 engine, so your old projects keep working as they are.%n%nMade by Mickey Perry, aka Pafu Igniters (breakbeat, trance and drum & bass), who also does motion design and animation. I made this for my own 2000s projects. I hope it brings yours back too.
FinishedHeadingLabel=404ever is installed
FinishedLabel=Two things to finish in FL Studio (once):%n%n1. If FL Studio is running, restart it.%n2. Options > MIDI settings: under Input, select "TS404 Panel" and switch Enable on. The controller type picks itself.%n%nThen open any TS404 channel. The panel service starts by itself whenever a project with a TS404 is open, and quits when it is not needed.
FinishedLabelNoIcons=Two things to finish in FL Studio (once):%n%n1. If FL Studio is running, restart it.%n2. Options > MIDI settings: under Input, select "TS404 Panel" and switch Enable on. The controller type picks itself.%n%nThen open any TS404 channel. The panel service starts by itself whenever a project with a TS404 is open, and quits when it is not needed.

[Code]
const
  Drivers32 = 'SOFTWARE\Microsoft\Windows NT\CurrentVersion\Drivers32';

procedure OpenLink(Sender: TObject);
var
  Code: Integer;
begin
  ShellExec('open', TNewStaticText(Sender).Hint, '', '', SW_SHOWNORMAL, ewNoWait, Code);
end;

// links under the finish text: the music, then the motion design / animation work
procedure AddLink(var Y: Integer; const Caption, Url: String; Heading: Boolean);
var
  L: TNewStaticText;
begin
  L := TNewStaticText.Create(WizardForm);
  L.Parent := WizardForm.FinishedPage;
  L.Left := WizardForm.FinishedLabel.Left;
  L.Top := Y;
  L.Caption := Caption;
  if Heading then begin
    L.Font.Style := [fsBold];
    Y := Y + L.Height + ScaleY(4);
  end else begin
    L.Hint := Url;
    L.Cursor := crHand;
    L.Font.Color := $872DFF;   // #ff2d87 (BGR)
    L.Font.Style := [fsUnderline];
    L.OnClick := @OpenLink;
    L.Left := L.Left + ScaleX(12);
    Y := Y + L.Height + ScaleY(2);
  end;
end;

procedure AddLinks;
var
  Y: Integer;
begin
  Y := WizardForm.RunList.Top + ScaleY(44);
  AddLink(Y, 'Pafu Igniters (music)', '', True);
  AddLink(Y, 'SoundCloud', 'https://soundcloud.com/pafu', False);
  AddLink(Y, 'Bandcamp', 'https://pafu.bandcamp.com', False);
  Y := Y + ScaleY(12);
  AddLink(Y, 'Mickey Perry (motion design and animation)', '', True);
  AddLink(Y, 'mickeyperry.github.io', 'https://mickeyperry.github.io/', False);
end;

procedure CurPageChanged(CurPageID: Integer);
begin
  if CurPageID = wpFinished then AddLinks;
end;

// The panel is drawn by Microsoft Edge WebView2 (built into Windows 11 and current Windows 10).
function HasWebView2: Boolean;
var
  V, K: String;
begin
  K := 'Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}';
  Result := (RegQueryStringValue(HKLM, 'SOFTWARE\WOW6432Node\' + K, 'pv', V) and (V <> '') and (V <> '0.0.0.0')) or
            (RegQueryStringValue(HKLM, 'SOFTWARE\' + K, 'pv', V) and (V <> '') and (V <> '0.0.0.0')) or
            (RegQueryStringValue(HKCU, 'Software\' + K, 'pv', V) and (V <> '') and (V <> '0.0.0.0'));
end;

function IsRunning(const Exe: String): Boolean;
var
  Code: Integer;
  Lines: TArrayOfString;
  Tmp: String;
begin
  // tasklist prints the exe name only when a process of that name exists
  Tmp := ExpandConstant('{tmp}\tl.txt');
  Exec(ExpandConstant('{cmd}'), '/C tasklist /FI "IMAGENAME eq ' + Exe + '" /NH > "' + Tmp + '"', '', SW_HIDE, ewWaitUntilTerminated, Code);
  Result := LoadStringsFromFile(Tmp, Lines) and (GetArrayLength(Lines) > 0) and (Pos(Lowercase(Exe), Lowercase(Lines[0])) > 0);
end;

function InitializeSetup: Boolean;
var
  Code: Integer;
begin
  Result := True;
  if not HasWebView2 then
    if MsgBox('404ever needs the Microsoft Edge WebView2 Runtime, which is not on this PC.' + #13#10#13#10 +
              'Open the download page now? (Install it, then run this setup again.)', mbConfirmation, MB_YESNO) = IDYES then begin
      ShellExec('open', 'https://developer.microsoft.com/microsoft-edge/webview2/#download-section', '', '', SW_SHOWNORMAL, ewNoWait, Code);
      Result := False;
      exit;
    end;
end;

function PrepareToInstall(var NeedsRestart: Boolean): String;
var
  Code: Integer;
begin
  Result := '';
  // on an upgrade FL Studio has the MIDI port driver loaded: it cannot be replaced while FL runs
  if FileExists(ExpandConstant('{sys}\ts404_midi.dll')) and IsRunning('FL64.exe') then begin
    Result := 'FL Studio is running. Close it, then run this setup again.';
    exit;
  end;
  // the service holds its files open; stop it before they are replaced
  Exec('taskkill.exe', '/IM TS404Panel.exe /F', '', SW_HIDE, ewWaitUntilTerminated, Code);
end;

// The "TS404 Panel" MIDI port is ts404_midi.dll, a user-mode MME driver: Windows loads it into every
// 64-bit program that uses MIDI from an entry midi1..midi9 in Drivers32 (there are 10 slots in all).
// Ours is the slot whose value names ts404_midi.dll (any older path included); else the first free one.
function DriverSlot(var Slot: String): Boolean;
var
  I: Integer;
  V, Name: String;
begin
  Result := False;
  Slot := '';
  for I := 9 downto 1 do begin   // highest first: Windows fills its own entries from the low end
    Name := 'midi' + IntToStr(I);
    if RegQueryStringValue(HKLM64, Drivers32, Name, V) then begin
      if CompareText(ExtractFileName(V), 'ts404_midi.dll') = 0 then begin Slot := Name; Result := True; exit; end;
    end else if Slot = '' then
      Slot := Name;   // highest free one
  end;
end;

procedure RegisterDriver;
var
  Slot: String;
begin
  if (not DriverSlot(Slot)) and (Slot = '') then begin
    MsgBox('All ten MIDI driver slots of Windows (Drivers32: midi, midi1 ... midi9) are taken, so the "TS404 Panel" MIDI port cannot be added.' + #13#10#13#10 +
           'Free one (an uninstalled MIDI driver usually leaves a stale entry) and run this setup again, or make a loopMIDI port named "TS404 Panel" instead.', mbError, MB_OK);
    exit;
  end;
  RegWriteStringValue(HKLM64, Drivers32, Slot, 'ts404_midi.dll');
end;

procedure UnregisterDriver;
var
  Slot: String;
begin
  if DriverSlot(Slot) then RegDeleteValue(HKLM64, Drivers32, Slot);
end;

procedure CurStepChanged(CurStep: TSetupStep);
var
  GuardCode: Integer;
begin
  if CurStep = ssPostInstall then begin
    RegisterDriver;
    // Windows can drop the entry when it reinstalls audio devices: a scheduled task puts it back
    Exec(ExpandConstant('{sys}\WindowsPowerShell\v1.0\powershell.exe'),
         '-NoProfile -NonInteractive -ExecutionPolicy Bypass -File "' + ExpandConstant('{app}\driver_guard.ps1') + '" -Install',
         '', SW_HIDE, ewWaitUntilTerminated, GuardCode);
    // tells the MIDI driver (inside FL) how to start the service when a project with a TS404 is open:
    // program, arguments, FL script heartbeat, service alive file
    ForceDirectories(ExpandConstant('{localappdata}\TS404 Panel'));
    SaveStringToFile(ExpandConstant('{localappdata}\TS404 Panel\launch.txt'),
                     ExpandConstant('{app}\TS404Panel.exe') + #10 + #10 +
                     ExpandConstant('{userdocs}\Image-Line\FL Studio\Settings\Hardware\TS404 Panel\heartbeat.json') + #10 +
                     ExpandConstant('{userdocs}\Image-Line\FL Studio\Settings\Hardware\TS404 Panel\server.json') + #10, False);
  end;
end;

function InitializeUninstall: Boolean;
var
  Code: Integer;
begin
  Exec('taskkill.exe', '/IM TS404Panel.exe /F', '', SW_HIDE, ewWaitUntilTerminated, Code);
  Result := True;
end;

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
begin
  if CurUninstallStep = usUninstall then UnregisterDriver;
end;
