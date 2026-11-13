Unicode true
!include "MUI2.nsh"
Name "BeeCode Rust"
OutFile "${OUTPUT}\BeeCode Rust Setup 5.0.2.exe"
InstallDir "$LOCALAPPDATA\Programs\BeeCode Rust"
RequestExecutionLevel user
SetCompressor /SOLID lzma
!define MUI_ABORTWARNING
!insertmacro MUI_PAGE_WELCOME
!insertmacro MUI_PAGE_DIRECTORY
!insertmacro MUI_PAGE_INSTFILES
!insertmacro MUI_PAGE_FINISH
!insertmacro MUI_UNPAGE_CONFIRM
!insertmacro MUI_UNPAGE_INSTFILES
!insertmacro MUI_LANGUAGE "Russian"
Section "BeeCode Rust"
  SetOutPath "$INSTDIR"
  File /r "${PAYLOAD}\*"
  WriteUninstaller "$INSTDIR\Uninstall.exe"
  CreateDirectory "$SMPROGRAMS\BeeCode Rust"
  CreateShortcut "$SMPROGRAMS\BeeCode Rust\BeeCode Rust.lnk" "$INSTDIR\beecode-desktop-rust.exe"
  CreateShortcut "$SMPROGRAMS\BeeCode Rust\Uninstall.lnk" "$INSTDIR\Uninstall.exe"
  WriteRegStr HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\BeeCodeRust" "DisplayName" "BeeCode Rust"
  WriteRegStr HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\BeeCodeRust" "DisplayVersion" "5.0.2"
  WriteRegStr HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\BeeCodeRust" "UninstallString" '"$INSTDIR\Uninstall.exe"'
SectionEnd
Section "Uninstall"
  Delete "$SMPROGRAMS\BeeCode Rust\BeeCode Rust.lnk"
  Delete "$SMPROGRAMS\BeeCode Rust\Uninstall.lnk"
  RMDir "$SMPROGRAMS\BeeCode Rust"
  Delete "$INSTDIR\beecode-desktop-rust.exe"
  Delete "$INSTDIR\Uninstall.exe"
  RMDir /r "$INSTDIR\resources\ui"
  RMDir /r "$INSTDIR\resources\agent-src"
  RMDir "$INSTDIR\resources"
  RMDir "$INSTDIR"
  DeleteRegKey HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\BeeCodeRust"
SectionEnd
