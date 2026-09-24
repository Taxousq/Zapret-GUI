; ===========================================================================
;  Zapret GUI - скрипт установщика для Inno Setup 6
;
;  Как собрать:
;    1) сначала соберите exe: build.bat  (должен появиться dist\ZapretGUI.exe)
;    2) установите Inno Setup: https://jrsoftware.org/isdl.php
;    3) откройте этот файл в Inno Setup Compiler и нажмите Compile (F9)
;    Результат: Output\ZapretGUI-Setup-<версия>.exe
;
;  Агент Harness этот установщик не собирает - только готовит скрипт.
;
;  Что делает установщик:
;    * ставит ZapretGUI.exe в {autopf}\Zapret GUI (Program Files);
;    * создаёт ярлыки в меню Пуск и (по желанию) на рабочем столе;
;    * предлагает запустить приложение после установки;
;    * папку запрета НЕ ставит: она скачивается при первом запуске GUI.
;
;  Portable-сборка (не требовать установки, права администратора не нужны):
;      iscc /dPORTABLE installer.iss
;    В этом режиме рядом с exe кладётся маркер portable.flag: настройки и
;    журнал хранятся в config.ini рядом с exe (см. core/portable.py),
;    реестр Windows не используется.
; ===========================================================================

#define MyAppName "Zapret GUI"
#define MyAppExeName "ZapretGUI.exe"
#define MyAppPublisher "Zapret GUI"
#define MyAppUrl "https://github.com/Flowseal/zapret-discord-youtube"
#define MyAppSource "dist\ZapretGUI.exe"
#define PortableFlagName "portable.flag"

; ---------------------------------------------------------------------------
;  Версия приложения. Держите её в соответствии с APP_VERSION в config.py:
;  Inno Setup не умеет читать Python-константы, поэтому версия задана здесь
;  одним числом (при обновлении приложения поменяйте её и тут).
;  Имя готового установщика — ZapretGUI-Setup-<версия>.exe: именно его ищет
;  автообновление в ассетах релиза (core/app_updater.py).
; ---------------------------------------------------------------------------
#define MyAppVersion "1.0.1"

; Разрядность: /dARCH=x64 или /dARCH=arm64, по умолчанию x64.
#ifndef ARCH
  #define ARCH "x64"
#endif

; Portable-режим: iscc /dPORTABLE installer.iss
#ifndef PORTABLE
  #define PORTABLE
#endif

[Setup]
; AppId — «личность» установки. МЕНЯТЬ ЕГО НЕЛЬЗЯ между версиями: по нему
; Inno Setup находит уже установленное приложение и обновляет его на месте.
; Новый GUID означает для Windows другую программу — установщик поставит
; вторую копию рядом вместо обновления существующей.
AppId={{8E3B1D42-6C7A-4F19-9A3E-2B7C5D91F0A4}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppVerName={#MyAppName} {#MyAppVersion}
AppPublisher={#MyAppPublisher}
AppPublisherURL={#MyAppUrl}
AppSupportURL={#MyAppUrl}
DefaultDirName={autopf}\{#MyAppName}
DefaultGroupName={#MyAppName}
DisableProgramGroupPage=yes
OutputDir=Output
#ifdef PORTABLE
  #define OutputBase "ZapretGUI-Portable-{#MyAppVersion}"
#else
  #define OutputBase "ZapretGUI-Setup-{#MyAppVersion}"
#endif
OutputBaseFilename={#OutputBase}
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
ArchitecturesAllowed={#ARCH}
ArchitecturesInstallIn64BitMode={#ARCH}
; Portable-версия не требует прав администратора.
#ifdef PORTABLE
PrivilegesRequired=lowest
#else
PrivilegesRequired=admin
#endif
UninstallDisplayName={#MyAppName}
UninstallDisplayIcon={app}\{#MyAppExeName}
; Автообновление: установщик сам закрывает работающий ZapretGUI.exe
; (CloseApplications) и запускает его заново после установки
; (RestartApplications), поэтому обновление «поверх» проходит без ошибок
; «файл занят другим процессом».
CloseApplications=yes
RestartApplications=yes
; Иконка установщика: assets\icon.ico, если файл есть.
#if FileExists("assets\icon.ico")
SetupIconFile=assets\icon.ico
#endif

[Languages]
Name: "russian"; MessagesFile: "compiler:Languages\Russian.isl"
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"; Flags: unchecked
#ifdef PORTABLE
Name: "portableflag"; Description: "Portable-режим: хранить настройки в config.ini рядом с exe (не в реестре)"; GroupDescription: "Portable:"; Flags: checkedonce
#endif

[Files]
Source: "{#MyAppSource}"; DestDir: "{app}"; Flags: ignoreversion
Source: "README.md"; DestDir: "{app}"; Flags: ignoreversion isreadme
#ifdef PORTABLE
Source: "portable.flag.example"; DestDir: "{app}"; DestName: "{#PortableFlagName}"; Flags: ignoreversion; Tasks: portableflag
#endif

[Icons]
Name: "{group}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"
Name: "{group}\{cm:UninstallProgram,{#MyAppName}}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#MyAppExeName}"; Description: "{cm:LaunchProgram,{#StringChange(MyAppName, '&', '&&')}}"; Flags: nowait postinstall skipifsilent

[UninstallDelete]
; В portable-режиме рядом с exe живут настройки и журнал — удаляем их.
Type: files; Name: "{app}\config.ini"
Type: filesandordirs; Name: "{app}\logs"
