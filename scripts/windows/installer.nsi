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

!define UNINSTALL_KEY "Software\Microsoft\Windows\CurrentVersion\Uninstall\LightTable"

Unicode True
Name "LightTable"
OutFile "${OUTPUT}"
InstallDir "$LOCALAPPDATA\Programs\LightTable"
RequestExecutionLevel user
SetCompressor /SOLID lzma

Function .onInit
  ${IfNot} ${RunningX64}
    MessageBox MB_OK|MB_ICONSTOP "LightTable requires 64-bit Windows." /SD IDOK
    SetErrorLevel 1
    Abort
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
  SetOutPath "$INSTDIR"
  File /r "${PAYLOAD}\*"
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
  DeleteRegKey HKCU "${UNINSTALL_KEY}"
  ; Catalogs, preferences, caches, and photos are deliberately preserved.
SectionEnd
