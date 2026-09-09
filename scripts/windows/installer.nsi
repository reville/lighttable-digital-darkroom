!ifndef VERSION
  !error "VERSION is required"
!endif
!ifndef PAYLOAD
  !error "PAYLOAD is required"
!endif
!ifndef OUTPUT
  !error "OUTPUT is required"
!endif
!ifndef UNINSTALL_MANIFEST
  !error "UNINSTALL_MANIFEST is required"
!endif

!include "LogicLib.nsh"
!include "FileFunc.nsh"
!include "WinMessages.nsh"
!include "x64.nsh"

!define PRESET_PROTOCOL_KEY "Software\Classes\lighttable"
!define UNINSTALL_KEY "Software\Microsoft\Windows\CurrentVersion\Uninstall\LightTable"

Unicode True
Name "LightTable"
OutFile "${OUTPUT}"
InstallDir "$LOCALAPPDATA\Programs\LightTable"
RequestExecutionLevel user
SetCompressor /SOLID lzma
Var UpdateOwner

Function .onInit
  ${IfNot} ${RunningX64}
    MessageBox MB_OK|MB_ICONSTOP "LightTable requires 64-bit Windows." /SD IDOK
    SetErrorLevel 1
    Abort
  ${EndIf}
  StrCpy $UpdateOwner "direct"
  ${GetOptions} $CMDLINE "/UPDATEOWNER=" $R1
  ${If} $R1 == "winget"
  ${OrIf} $R1 == "chocolatey"
  ${OrIf} $R1 == "scoop"
    StrCpy $UpdateOwner $R1
  ${EndIf}
  SetShellVarContext current
  SetRegView 64
  ; InstallDirRegKey cannot use the selected 64-bit registry view. Reuse an
  ; existing install location only when the caller has not supplied /D.
  ClearErrors
  ${GetOptions} $CMDLINE "/D=" $R0
  ${If} ${Errors}
    ReadRegStr $R0 HKCU "${UNINSTALL_KEY}" "InstallLocation"
    ${If} $R0 != ""
      StrCpy $INSTDIR $R0
    ${EndIf}
  ${EndIf}
FunctionEnd

Function un.onInit
  SetShellVarContext current
  SetRegView 64
FunctionEnd

Page directory
Page instfiles
UninstPage uninstConfirm
UninstPage instfiles

Section "Install"
  ; Resolve the prerequisite before modifying an existing LightTable install.
  InitPluginsDir
  SetOutPath "$PLUGINSDIR"
  File /oname=ensure-webview2.ps1 "ensure-webview2.ps1"
  DetailPrint "Checking Microsoft Edge WebView2 Runtime…"
  ClearErrors
  ExecWait '"$SYSDIR\WindowsPowerShell\v1.0\powershell.exe" -NoProfile -NonInteractive -WindowStyle Hidden -ExecutionPolicy Bypass -File "$PLUGINSDIR\ensure-webview2.ps1"' $0
  ${If} ${Errors}
  ${OrIf} $0 != 0
    MessageBox MB_OK|MB_ICONSTOP "Microsoft Edge WebView2 Runtime could not be installed. Connect to the internet and run setup again, or install the Runtime from https://developer.microsoft.com/microsoft-edge/webview2 first." /SD IDOK
    SetErrorLevel 1
    Abort
  ${EndIf}
  ${If} $UpdateOwner == "direct"
    ClearErrors
    FileOpen $0 "$INSTDIR\install-channel.txt" r
    ${IfNot} ${Errors}
      FileRead $0 $1
      FileClose $0
      ${If} $1 == "winget"
      ${OrIf} $1 == "chocolatey"
      ${OrIf} $1 == "scoop"
        StrCpy $UpdateOwner $1
      ${EndIf}
    ${EndIf}
  ${EndIf}
  SetOutPath "$INSTDIR"
  File /r "${PAYLOAD}\*"
  ; Explicit package ownership prevents a second updater from overwriting a
  ; package-managed installation. Preserve an existing manager on repair.
  FileOpen $0 "$INSTDIR\install-channel.txt" w
  FileWrite $0 "$UpdateOwner"
  FileClose $0
  WriteUninstaller "$INSTDIR\Uninstall.exe"
  ; Use a dedicated bin directory: putting INSTDIR on PATH would resolve the
  ; GUI LightTable.exe before the CLI lighttable.cmd on case-insensitive Windows.
  ClearErrors
  ExecWait '"$SYSDIR\WindowsPowerShell\v1.0\powershell.exe" -NoProfile -NonInteractive -WindowStyle Hidden -ExecutionPolicy Bypass -File "$INSTDIR\update-user-path.ps1" -Action Add -BinPath "$INSTDIR\bin"' $0
  ${If} ${Errors}
  ${OrIf} $0 != 0
    MessageBox MB_OK|MB_ICONSTOP "LightTable could not register its command. The installer can be run again." /SD IDOK
    SetErrorLevel 1
    Abort
  ${EndIf}
  SendMessage ${HWND_BROADCAST} ${WM_SETTINGCHANGE} 0 "STR:Environment" /TIMEOUT=5000
  CreateShortcut "$DESKTOP\LightTable.lnk" "$INSTDIR\LightTable.exe" "" "$INSTDIR\LightTable.exe"
  CreateDirectory "$SMPROGRAMS\LightTable"
  CreateShortcut "$SMPROGRAMS\LightTable\LightTable.lnk" "$INSTDIR\LightTable.exe" "" "$INSTDIR\LightTable.exe"
  CreateShortcut "$SMPROGRAMS\LightTable\Uninstall.lnk" "$INSTDIR\Uninstall.exe"
  WriteRegStr HKCU "${UNINSTALL_KEY}" "DisplayName" "LightTable"
  WriteRegStr HKCU "${UNINSTALL_KEY}" "DisplayVersion" "${VERSION}"
  WriteRegStr HKCU "${UNINSTALL_KEY}" "Publisher" "Nicholas Reville"
  WriteRegStr HKCU "${UNINSTALL_KEY}" "URLInfoAbout" "https://github.com/reville/lighttable-digital-darkroom"
  WriteRegStr HKCU "${UNINSTALL_KEY}" "InstallLocation" "$INSTDIR"
  WriteRegStr HKCU "${UNINSTALL_KEY}" "DisplayIcon" "$INSTDIR\LightTable.exe"
  WriteRegStr HKCU "${UNINSTALL_KEY}" "UninstallString" '"$INSTDIR\Uninstall.exe"'
  WriteRegStr HKCU "${UNINSTALL_KEY}" "QuietUninstallString" '"$INSTDIR\Uninstall.exe" /S'
  ; Protocol arguments always name a catalog entry; the native host validates
  ; the complete ID before opening its gallery. Quote both executable and URL.
  WriteRegStr HKCU "${PRESET_PROTOCOL_KEY}" "" "URL:LightTable Preset"
  WriteRegStr HKCU "${PRESET_PROTOCOL_KEY}" "URL Protocol" ""
  WriteRegStr HKCU "${PRESET_PROTOCOL_KEY}\DefaultIcon" "" '$\"$INSTDIR\LightTable.exe$\",0'
  WriteRegStr HKCU "${PRESET_PROTOCOL_KEY}\shell\open\command" "" '$\"$INSTDIR\LightTable.exe$\" --preset-url $\"%1$\"'
  WriteRegDWORD HKCU "${UNINSTALL_KEY}" "NoModify" 1
  WriteRegDWORD HKCU "${UNINSTALL_KEY}" "NoRepair" 1
SectionEnd

Section "Uninstall"
  ClearErrors
  ExecWait '"$SYSDIR\WindowsPowerShell\v1.0\powershell.exe" -NoProfile -NonInteractive -WindowStyle Hidden -ExecutionPolicy Bypass -File "$INSTDIR\update-user-path.ps1" -Action Remove -BinPath "$INSTDIR\bin"' $0
  ${If} ${Errors}
  ${OrIf} $0 != 0
    MessageBox MB_OK|MB_ICONSTOP "LightTable could not remove its command from PATH. Run the uninstaller again." /SD IDOK
    SetErrorLevel 1
    Abort
  ${EndIf}
  SendMessage ${HWND_BROADCAST} ${WM_SETTINGCHANGE} 0 "STR:Environment" /TIMEOUT=5000
  Delete "$DESKTOP\LightTable.lnk"
  Delete "$SMPROGRAMS\LightTable\LightTable.lnk"
  Delete "$SMPROGRAMS\LightTable\Uninstall.lnk"
  RMDir "$SMPROGRAMS\LightTable"
  !include "${UNINSTALL_MANIFEST}"
  Delete "$INSTDIR\Uninstall.exe"
  RMDir "$INSTDIR"
  ; Preserve a protocol claimed later by another installation.
  ReadRegStr $0 HKCU "${PRESET_PROTOCOL_KEY}\shell\open\command" ""
  ${If} $0 == '$\"$INSTDIR\LightTable.exe$\" --preset-url $\"%1$\"'
    DeleteRegKey HKCU "${PRESET_PROTOCOL_KEY}"
  ${EndIf}
  DeleteRegKey HKCU "${UNINSTALL_KEY}"
  ; Catalogs, preferences, caches, and photos are deliberately preserved.
SectionEnd
