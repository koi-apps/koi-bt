; KOI BT 安装包（Inno Setup 6）。CI 里用：iscc /DMyAppVersion=1.4.0 installer\koi-bt.iss
#ifndef MyAppVersion
  #define MyAppVersion "0.0.0"
#endif
#define MyAppName "KOI BT"
#define MyAppExe "KOI BT.exe"

[Setup]
AppId={{2FB11707-20C1-4672-91FB-CEDCC212238B}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppPublisher=KOI Apps Studio
AppPublisherURL=https://github.com/koi-apps/koi-bt
DefaultDirName={localappdata}\Programs\{#MyAppName}
DefaultGroupName={#MyAppName}
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
OutputDir=..\dist
OutputBaseFilename=KOI-BT-{#MyAppVersion}-setup
SetupIconFile=..\koi.ico
UninstallDisplayIcon={app}\{#MyAppExe}
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
; 安装界面语言跟随系统，不弹选择框
ShowLanguageDialog=auto
LanguageDetectionMethod=uilanguage
CloseApplications=yes
LicenseFile=..\LICENSE

[Languages]
Name: "en"; MessagesFile: "compiler:Default.isl"
Name: "chs"; MessagesFile: "ChineseSimplified.isl"
Name: "cht"; MessagesFile: "ChineseTraditional.isl"

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"

[Files]
Source: "..\dist\KOI BT\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\{#MyAppName}"; Filename: "{app}\{#MyAppExe}"
Name: "{group}\{cm:UninstallProgram,{#MyAppName}}"; Filename: "{uninstallexe}"
Name: "{userdesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExe}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#MyAppExe}"; Description: "{cm:LaunchProgram,{#MyAppName}}"; Flags: nowait postinstall skipifsilent

[UninstallDelete]
Type: filesandordirs; Name: "{app}"
