; Inno Setup script for TjipTemp.
; Built by packaging\windows\build.ps1; needs Inno Setup 6.

#define AppName "TjipTemp"
#define AppVersion "0.1.0"
#define AppPublisher "TjipTemp"
#define AppURL "https://github.com/tjiptemp/tjiptemp"
#define AppExeName "tjiptemp.exe"

[Setup]
AppId={{6F0D0001-B5A3-F393-E0A9-E50E24DCCA9E}
AppName={#AppName}
AppVersion={#AppVersion}
AppPublisher={#AppPublisher}
AppPublisherURL={#AppURL}
DefaultDirName={autopf}\{#AppName}
DefaultGroupName={#AppName}
DisableProgramGroupPage=yes
OutputDir=..\..\dist
OutputBaseFilename=TjipTemp-Setup-{#AppVersion}
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
ArchitecturesInstallIn64BitMode=x64compatible
; Per-user install by default, so no UAC prompt and no admin rights needed.
PrivilegesRequiredOverridesAllowed=dialog

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "Create a desktop shortcut"; \
  GroupDescription: "Additional shortcuts"; Flags: unchecked

[Files]
Source: "..\..\dist\tjiptemp\*"; DestDir: "{app}"; \
  Flags: ignoreversion recursesubdirs createallsubdirs
Source: "..\..\docs\protocol.md"; DestDir: "{app}\docs"; Flags: ignoreversion
Source: "..\..\firmware-ref\tjip_proto.h"; DestDir: "{app}\firmware-ref"; Flags: ignoreversion
Source: "..\..\firmware-ref\tjip_proto.c"; DestDir: "{app}\firmware-ref"; Flags: ignoreversion

[Icons]
Name: "{group}\{#AppName}"; Filename: "{app}\{#AppExeName}"
Name: "{group}\Wire protocol specification"; Filename: "{app}\docs\protocol.md"
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\{#AppExeName}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#AppExeName}"; Description: "Launch {#AppName}"; \
  Flags: nowait postinstall skipifsilent
