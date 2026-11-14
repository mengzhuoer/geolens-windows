; GeoLens 本地版 Windows 安装脚本 (NSIS 3.11, Unicode)
Unicode true
SetCompressor /SOLID zlib
CRCCheck on
XPStyle on

!include "MUI2.nsh"
!include "FileFunc.nsh"

!define APPNAME "GeoLens 本地版"
!define COMPANY "GeoLens Desktop"
!define VERSION "1.2.8"
!define PAYLOAD "..\dist\GeoLens"

Name "${APPNAME}"
OutFile "..\dist\release\GeoLens-Setup-${VERSION}-win64.exe"
InstallDir "$LOCALAPPDATA\GeoLens"
InstallDirRegKey HKCU "Software\GeoLensDesktop" "InstallDir"
RequestExecutionLevel user
BrandingText "${APPNAME} ${VERSION}"

VIProductVersion "${VERSION}.0"
VIAddVersionKey "ProductName" "${APPNAME}"
VIAddVersionKey "CompanyName" "${COMPANY}"
VIAddVersionKey "FileDescription" "本地遥感影像自动整理与空间数据目录"
VIAddVersionKey "FileVersion" "${VERSION}"
VIAddVersionKey "LegalCopyright" "GeoLens is Apache-2.0"
VIAddVersionKey "Comments" "Bundled PostgreSQL + PostGIS + pgvector + TiTiler"

!define MUI_ICON "..\build\geolens.ico"
!define MUI_UNICON "..\build\geolens.ico"
!define MUI_ABORTWARNING
!define MUI_WELCOMEPAGE_TITLE "欢迎安装 ${APPNAME}"
!define MUI_WELCOMEPAGE_TEXT "本软件把 GeoLens 空间数据目录做成 Windows 桌面程序，并支持自动扫描、整理、入库本地遥感影像。$\r$\n$\r$\n所有数据只保存在本机，服务只监听 127.0.0.1。$\r$\n$\r$\n安装后首次启动需要 30–60 秒初始化数据库。"

!insertmacro MUI_PAGE_WELCOME
!insertmacro MUI_PAGE_DIRECTORY
!insertmacro MUI_PAGE_INSTFILES
!define MUI_FINISHPAGE_RUN "$INSTDIR\GeoLens.exe"
!define MUI_FINISHPAGE_RUN_TEXT "立即运行 ${APPNAME}"
!define MUI_FINISHPAGE_SHOWREADME "$INSTDIR\README-使用说明.md"
!define MUI_FINISHPAGE_SHOWREADME_TEXT "查看使用说明"
!insertmacro MUI_PAGE_FINISH

!insertmacro MUI_UNPAGE_CONFIRM
!insertmacro MUI_UNPAGE_INSTFILES

!insertmacro MUI_LANGUAGE "SimpChinese"

Section "GeoLens 本地版" SEC01
  SetOutPath "$INSTDIR"
  ; 原有 data 目录不打包，升级安装时保持不动
  File /r "${PAYLOAD}\*.*"

  WriteRegStr HKCU "Software\GeoLensDesktop" "InstallDir" "$INSTDIR"
  WriteRegStr HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\GeoLensDesktop" "DisplayName" "${APPNAME}"
  WriteRegStr HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\GeoLensDesktop" "DisplayVersion" "${VERSION}"
  WriteRegStr HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\GeoLensDesktop" "Publisher" "${COMPANY}"
  WriteRegStr HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\GeoLensDesktop" "DisplayIcon" "$INSTDIR\GeoLens.exe"
  WriteRegStr HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\GeoLensDesktop" "UninstallString" "$INSTDIR\Uninstall.exe"
  WriteRegDWORD HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\GeoLensDesktop" "NoModify" 1
  WriteRegDWORD HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\GeoLensDesktop" "NoRepair" 1

  WriteUninstaller "$INSTDIR\Uninstall.exe"

  CreateDirectory "$SMPROGRAMS\GeoLens"
  CreateShortCut "$SMPROGRAMS\GeoLens\GeoLens 本地版.lnk" "$INSTDIR\GeoLens.exe" "" "$INSTDIR\GeoLens.exe" 0
  CreateShortCut "$SMPROGRAMS\GeoLens\使用说明.lnk" "$INSTDIR\README-使用说明.md"
  CreateShortCut "$SMPROGRAMS\GeoLens\卸载.lnk" "$INSTDIR\Uninstall.exe"
  CreateShortCut "$DESKTOP\GeoLens 本地版.lnk" "$INSTDIR\GeoLens.exe" "" "$INSTDIR\GeoLens.exe" 0
SectionEnd

Section "Uninstall"
  Delete "$SMPROGRAMS\GeoLens\GeoLens 本地版.lnk"
  Delete "$SMPROGRAMS\GeoLens\使用说明.lnk"
  Delete "$SMPROGRAMS\GeoLens\卸载.lnk"
  RMDir "$SMPROGRAMS\GeoLens"
  Delete "$DESKTOP\GeoLens 本地版.lnk"

  Delete "$INSTDIR\GeoLens.exe"
  Delete "$INSTDIR\Uninstall.exe"
  Delete "$INSTDIR\README-使用说明.md"
  Delete "$INSTDIR\THIRD_PARTY-LICENSES.md"
  Delete "$INSTDIR\LICENSE-GeoLens.txt"
  Delete "$INSTDIR\NOTICE-GeoLens.txt"
  RMDir /r "$INSTDIR\_internal"
  RMDir /r "$INSTDIR\app"
  RMDir /r "$INSTDIR\runtime"

  ; 数据默认保留；只有用户明确同意才删除
  IfFileExists "$INSTDIR\data\*.*" 0 no_data
    MessageBox MB_YESNO|MB_ICONQUESTION "是否同时删除影像库与数据库备份？$\r$\n$\r$\n选择“否”将保留 $INSTDIR\data 目录，便于以后重装继续使用。" /SD IDNO IDNO no_data IDYES delete_data
  delete_data:
    RMDir /r "$INSTDIR\data"
    Goto after_data
  no_data:
    ; 保留 data
  after_data:

  RMDir "$INSTDIR"

  DeleteRegKey HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\GeoLensDesktop"
  DeleteRegKey HKCU "Software\GeoLensDesktop"
SectionEnd
