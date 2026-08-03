; Inno Setup script for the keycut Windows installer.
;
; Expects PyInstaller to have already written dist\keycut.exe, and takes the
; version on the command line. There is deliberately no default, so a build
; that forgets it fails loudly rather than shipping a wrong version number:
;
;   ISCC.exe /DAppVersion=0.1.0 installer\keycut.iss
;
; See .github/workflows/release.yml for the whole sequence.

#define AppName "keycut"
#define AppURL "https://github.com/Back-Road-Creative/keycut"
; keycut shells out to ffmpeg and ffprobe and this installer does not bundle
; them: an ffmpeg build dwarfs keycut, and which licence a given build falls
; under depends on how it was configured. Said on the way out, not buried.
#define FFmpegNote "keycut runs ffmpeg and ffprobe. They are NOT included and must be installed separately, on PATH:%n%n    winget install -e --id Gyan.FFmpeg%n%nOpen a new terminal, then run:  keycut --help"

[Setup]
; Fixed GUID. This is how Windows tells an upgrade of an existing install from
; a second copy of it, and how the uninstaller finds its own entry. Never change.
AppId={{87D77FBD-CF06-4187-A5A5-29A3000D3108}
AppName={#AppName}
AppVersion={#AppVersion}
AppPublisher=Joe Petrucelli
AppPublisherURL={#AppURL}
AppSupportURL={#AppURL}/issues
LicenseFile=..\LICENSE
DefaultDirName={autopf}\{#AppName}
DisableProgramGroupPage=yes
; Program Files and a machine-wide PATH entry both need elevation.
PrivilegesRequired=admin
; Without this, Setup runs in 32-bit mode and {autopf} resolves to
; "Program Files (x86)". x64compatible needs Inno Setup 6.3+ (the older plain
; "x64" is deprecated as of 6.3); choco installs current Inno, and the
; workflow fails loudly if ISCC is missing.
ArchitecturesInstallIn64BitMode=x64compatible
; Broadcast WM_SETTINGCHANGE so a shell started after this picks up the PATH
; edit below without a logoff.
ChangesEnvironment=yes
UninstallDisplayName={#AppName} {#AppVersion}
UninstallDisplayIcon={app}\keycut.exe
OutputDir=..\dist
OutputBaseFilename=keycut-setup-{#AppVersion}
Compression=lzma
SolidCompression=yes

[Files]
Source: "..\dist\keycut.exe"; DestDir: "{app}"; Flags: ignoreversion

[Registry]
; Append the install directory to the machine PATH. The Check skips the append
; when it is already present, so reinstalling does not grow PATH without bound.
; Deliberately one long line: the `\` continuation is an ISPP line-spanning
; feature that nothing in this repo can compile-test, and a wrapped line that
; silently mis-parses would corrupt the machine PATH. One line cannot.
Root: HKLM; Subkey: "SYSTEM\CurrentControlSet\Control\Session Manager\Environment"; ValueType: expandsz; ValueName: "Path"; ValueData: "{olddata};{app}"; Check: NeedsAddPath

[Messages]
; Which of the two the wizard shows depends on whether it created any icons,
; so both carry the note.
FinishedLabel=Setup has installed [name] on your computer.%n%n{#FFmpegNote}
FinishedLabelNoIcons=Setup has installed [name] on your computer.%n%n{#FFmpegNote}

[Code]
const
  EnvKey = 'SYSTEM\CurrentControlSet\Control\Session Manager\Environment';

{ Compare against PATH with both sides ';'-padded, so a directory is only a
  match as a whole entry, never as the prefix of a longer path. }
function NeedsAddPath(): Boolean;
var
  Path: string;
begin
  if not RegQueryStringValue(HKEY_LOCAL_MACHINE, EnvKey, 'Path', Path) then
    Result := True
  else
    Result := Pos(';' + UpperCase(ExpandConstant('{app}')) + ';',
                  ';' + UpperCase(Path) + ';') = 0;
end;

procedure RemoveFromPath();
var
  Path, Dir: string;
  P: Integer;
begin
  Dir := ExpandConstant('{app}');
  if not RegQueryStringValue(HKEY_LOCAL_MACHINE, EnvKey, 'Path', Path) then
    exit;
  P := Pos(';' + UpperCase(Dir) + ';', ';' + UpperCase(Path) + ';');
  if P = 0 then
    exit;
  { P indexes the padded copy, so real-string index i sits at P = i+1, and the
    ';' introducing the entry is at P-1. The exception is a first entry, which
    has no leading ';', so there the trailing one is removed instead. Either way
    exactly one separator goes with the directory. }
  if P = 1 then
    Delete(Path, 1, Length(Dir) + 1)
  else
    Delete(Path, P - 1, Length(Dir) + 1);
  RegWriteExpandStringValue(HKEY_LOCAL_MACHINE, EnvKey, 'Path', Path);
end;

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
begin
  if CurUninstallStep = usPostUninstall then
    RemoveFromPath();
end;
