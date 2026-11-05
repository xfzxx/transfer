; ============================================================================
; 文件传输工具 —— 机器级安装包（NSIS）
;
; 【为什么带一个 215MB 的 WebView2 离线安装器】
;   程序默认走 C3（pywebview + WebView2 内嵌窗口）。Windows 上的 WebView2 是
;   **系统组件**，不能编进 exe（pythonnet 需要系统 CLR，WebView2 需要系统运行时）。
;   医院内网常常不联网，所以不能带 1.9MB 的在线引导器 —— 那玩意儿装的时候要下载。
;   因此带**离线独立安装器**：有网没网都能装，一次装好全机所有 C3 应用共用。
;
; 【.NET Framework 为什么不带】Win10 1809+ / Win11 自带 4.8；带一份 .NET 4.8 离线包
;   还要再加 110MB，而缺它的机器本来就极少。这里只**检测并明确告知**：
;   缺 .NET 时装完仍可用，只是自动降级到 C4（系统浏览器）。
;
; 【升级（覆盖安装）的四条纪律】
;   ① `InstallDirRegKey`：装到上次那个目录，而不是"用户这次挑的目录"；
;   ② 已装版本更高 → 明确询问是否降级（不静默降级）；
;   ③ 先删掉上一版的**已知载荷**再写新文件 —— 否则改名/删掉的文件会永远留在
;      安装目录里，两代文件混在一起，出问题无法判断在跑哪一版；
;   ④ `data\` 全程不碰：配置、状态库、失败清单、续传记录都在里面，升级必须保住。
;
; 【卸载的纪律：干净，但不替人做主】
;   删掉自己装的全部文件/快捷方式/注册表项；`data\` 与 %LOCALAPPDATA% 的兜底数据
;   **询问**后删（静默卸载默认保留，要删得显式给 /DELETE_DATA=1）；
;   WebView2 是系统共享组件（别的程序也可能用），**不自动卸载**，只给明确指引。
;
; 构建：installer/build.py（自动从 config.py 读版本号）
; 静默安装：Setup.exe /S             静默卸载：uninstall.exe /S [/DELETE_DATA=1]
; 换目录：  Setup.exe /S /D=D:\xxx   （/D 必须最后，路径不加引号）
; ============================================================================

Unicode True
SetCompressor /SOLID lzma

!include "MUI2.nsh"
!include "LogicLib.nsh"
!include "FileFunc.nsh"
; `${VersionCompare}` 在 WordFunc.nsh 里，不在 FileFunc —— 漏了它就是
; "Invalid command: ${VersionCompare}" 直接中止构建。
!include "WordFunc.nsh"

!define PRODUCT_NAME      "文件传输工具"
!define PRODUCT_EXE       "文件传输工具.exe"
!define PRODUCT_PUBLISHER "院内文件传输"
!define UNINST_KEY        "Software\Microsoft\Windows\CurrentVersion\Uninstall\FileTransferTool"
!define WEBVIEW2_GUID     "{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}"
!define WEBVIEW2_SETUP    "MicrosoftEdgeWebView2RuntimeInstallerX64.exe"

!ifndef PRODUCT_VERSION
  !define PRODUCT_VERSION "0.0.0"
!endif

; TESTMODE：不申请管理员、装到临时目录 —— 只用于本机验证安装/升级/卸载流程。
!ifdef TESTMODE
  RequestExecutionLevel user
  InstallDir "$TEMP\ft-installtest"
!else
  RequestExecutionLevel admin
  InstallDir "$PROGRAMFILES64\${PRODUCT_NAME}"
!endif

Name "${PRODUCT_NAME} ${PRODUCT_VERSION}"
!ifdef TESTMODE
  OutFile "out\_test-安装包.exe"
!else
  OutFile "out\文件传输工具-${PRODUCT_VERSION}-安装包.exe"
!endif
ShowInstDetails show
ShowUninstDetails show
BrandingText "${PRODUCT_NAME} ${PRODUCT_VERSION}"

VIProductVersion "${PRODUCT_VERSION}.0"
VIAddVersionKey /LANG=2052 "ProductName"      "${PRODUCT_NAME}"
VIAddVersionKey /LANG=2052 "FileDescription"  "${PRODUCT_NAME} 安装程序"
VIAddVersionKey /LANG=2052 "FileVersion"      "${PRODUCT_VERSION}"
VIAddVersionKey /LANG=2052 "ProductVersion"   "${PRODUCT_VERSION}"
VIAddVersionKey /LANG=2052 "LegalCopyright"   "${PRODUCT_PUBLISHER}"
VIAddVersionKey /LANG=2052 "CompanyName"      "${PRODUCT_PUBLISHER}"

; ---------------------------------------------------------------- 界面
!define MUI_ABORTWARNING
!define MUI_FINISHPAGE_RUN "$INSTDIR\${PRODUCT_EXE}"
!define MUI_FINISHPAGE_RUN_TEXT "立即运行 ${PRODUCT_NAME}"

!insertmacro MUI_PAGE_LICENSE "license.txt"
!insertmacro MUI_PAGE_DIRECTORY
!insertmacro MUI_PAGE_INSTFILES
!insertmacro MUI_PAGE_FINISH

!insertmacro MUI_UNPAGE_CONFIRM
!insertmacro MUI_UNPAGE_INSTFILES

!insertmacro MUI_LANGUAGE "SimpChinese"

; 升级时默认落在上次装的目录（①）
InstallDirRegKey HKLM "${UNINST_KEY}" "InstallLocation"

; ---------------------------------------------------------------- 检测函数
; WebView2：三条注册表路径依次试（与 host_probe.detect 的判据同源）。$0 = 1/0。
Function CheckWebView2
  StrCpy $0 "0"
  ReadRegStr $1 HKLM "SOFTWARE\WOW6432Node\Microsoft\EdgeUpdate\Clients\${WEBVIEW2_GUID}" "pv"
  StrCmp $1 "" 0 wv_yes
  ReadRegStr $1 HKLM "SOFTWARE\Microsoft\EdgeUpdate\Clients\${WEBVIEW2_GUID}" "pv"
  StrCmp $1 "" 0 wv_yes
  ReadRegStr $1 HKCU "SOFTWARE\Microsoft\EdgeUpdate\Clients\${WEBVIEW2_GUID}" "pv"
  StrCmp $1 "" wv_no wv_yes
wv_yes:
  StrCpy $0 "1"
wv_no:
FunctionEnd

; .NET Framework：Release >= 461808 即 4.7.2+（pythonnet 的门槛）。$2 = 1/0。
Function CheckDotNet
  StrCpy $2 "0"
  ReadRegDWORD $3 HKLM "SOFTWARE\Microsoft\NET Framework Setup\NDP\v4\Full" "Release"
  StrCmp $3 "" dn_done
  IntCmpU $3 461808 dn_done dn_ok dn_ok
dn_ok:
  StrCpy $2 "1"
dn_done:
FunctionEnd

; 清掉上一版的已知载荷（③）。**只删我们自己装的东西**，data\ 一律不碰。
; 【维护注意】这里是一张**白名单**，不是"清空目录"。将来若改了文件名（比如 exe 改名），
; 必须把**旧名字**补进这个函数，否则升级后旧文件会永远留在安装目录里 —— 两代文件
; 同时存在，出问题时连"跑的是哪一版"都判断不了。
; 反过来说，目录里用户自己放的东西（临时放个 exe、自己的说明文件）**刻意不动**：
; 升级不是"重置目录"，替人删文件比留垃圾更危险。
Function RemovePrevPayload
  Delete "$INSTDIR\${PRODUCT_EXE}"
  Delete "$INSTDIR\license.txt"
  Delete "$INSTDIR\部署说明.txt"
  Delete "$INSTDIR\uninstall.exe"
  RMDir /r "$INSTDIR\vendor"
FunctionEnd

; ---------------------------------------------------------------- .onInit
Function .onInit
  SetShellVarContext all

  ; ② 已装版本更高 → 问一句。降级通常是误操作（装回旧包），不该静默发生。
  ReadRegStr $0 HKLM "${UNINST_KEY}" "DisplayVersion"
  ${If} $0 != ""
    ${VersionCompare} "$0" "${PRODUCT_VERSION}" $1
    ${If} $1 == 1
      ; 静默部署（/S）时没法问人：**直接中止**。让它静默降级才是最坏的结果 ——
      ; 一批机器被错包退回旧版，事后没有任何痕迹。
      ${If} ${Silent}
        DetailPrint "[中止] 已安装 $0，高于本包 ${PRODUCT_VERSION}，静默模式下不降级"
        SetErrorLevel 3
        Abort
      ${Else}
        MessageBox MB_YESNO|MB_ICONQUESTION \
          "本机已安装更高的版本（$0）。$\r$\n继续安装 ${PRODUCT_VERSION} 属于降级，是否继续？" \
          IDYES downgrade_ok
        Abort
      downgrade_ok:
      ${EndIf}
    ${EndIf}
  ${EndIf}
FunctionEnd

; ---------------------------------------------------------------- 安装
Section "主程序" SecMain
  SectionIn RO
  SetShellVarContext all

  ; 升级时先结束旧实例，否则 exe 被占用写不进去（没在跑时这一步无害）。
  nsExec::Exec 'taskkill /F /IM "${PRODUCT_EXE}"'
  Pop $0
  Sleep 500

  Call RemovePrevPayload

  SetOutPath "$INSTDIR"
  File "..\dist\${PRODUCT_EXE}"
  File "license.txt"
  File "部署说明.txt"

  ; 把 WebView2 离线安装器随包带上：找不到系统运行时时用它就地静默装。
  SetOutPath "$INSTDIR\vendor"
  File "vendor\${WEBVIEW2_SETUP}"
  SetOutPath "$INSTDIR"

  ; ---------------- WebView2 ----------------
  DetailPrint "检查 WebView2 运行时…"
  Call CheckWebView2
  ${If} $0 == "0"
    DetailPrint "未检测到 WebView2，正在静默安装（离线包，约需 1-2 分钟）…"
    ExecWait '"$INSTDIR\vendor\${WEBVIEW2_SETUP}" /silent /install' $1
    DetailPrint "WebView2 安装器退出码：$1"
    Call CheckWebView2
    ${If} $0 == "0"
      ; 静默安装不弹框：/S 部署时没人点确定，弹框会把批处理卡死在这一步。
      DetailPrint "[警告] WebView2 未装上，程序将降级为浏览器模式"
      ${IfNot} ${Silent}
        MessageBox MB_ICONEXCLAMATION|MB_OK \
          "WebView2 运行时安装未成功。$\r$\n$\r$\n不影响使用：程序会自动以「浏览器模式」启动（界面开在系统浏览器里）。$\r$\n如需内嵌窗口，请让信息科手动运行安装目录下 vendor\${WEBVIEW2_SETUP} 后再启动。"
      ${EndIf}
    ${Else}
      WriteRegDWORD HKLM "${UNINST_KEY}" "WebView2InstalledByUs" 1
      DetailPrint "WebView2 已就绪（由本程序安装，已记录）"
    ${EndIf}
  ${Else}
    DetailPrint "WebView2 已存在，跳过安装"
  ${EndIf}

  ; 运行时已可用 → 删掉安装目录里那份 215MB 离线包，别每台机器白占 205MB。
  ; （将来要修复/重装 WebView2，重跑本安装包即可，包里一直带着它。）
  ${If} $0 == "1"
    Delete "$INSTDIR\vendor\${WEBVIEW2_SETUP}"
    RMDir "$INSTDIR\vendor"
  ${EndIf}

  ; ---------------- .NET Framework ----------------
  Call CheckDotNet
  ${If} $2 == "0"
    DetailPrint "[提示] 未检测到 .NET Framework 4.7.2+，程序将以浏览器模式启动"
    ${IfNot} ${Silent}
      MessageBox MB_ICONINFORMATION|MB_OK \
        "本机似乎没有 .NET Framework 4.7.2 或更高版本。$\r$\n$\r$\n程序仍可正常使用，但会自动以「浏览器模式」启动（界面开在系统浏览器里），而不是内嵌窗口。Win10 1809 之后的系统一般都自带，无需处理。"
    ${EndIf}
  ${EndIf}

  ; ---------------- 卸载信息（每次覆盖更新，升级后版本号才是新的）----------------
  WriteRegStr HKLM "${UNINST_KEY}" "DisplayName"     "${PRODUCT_NAME}"
  WriteRegStr HKLM "${UNINST_KEY}" "DisplayVersion"  "${PRODUCT_VERSION}"
  WriteRegStr HKLM "${UNINST_KEY}" "Publisher"       "${PRODUCT_PUBLISHER}"
  WriteRegStr HKLM "${UNINST_KEY}" "InstallLocation" "$INSTDIR"
  WriteRegStr HKLM "${UNINST_KEY}" "DisplayIcon"     "$INSTDIR\${PRODUCT_EXE}"
  WriteRegStr HKLM "${UNINST_KEY}" "UninstallString" '"$INSTDIR\uninstall.exe"'
  WriteRegDWORD HKLM "${UNINST_KEY}" "NoModify" 1
  WriteRegDWORD HKLM "${UNINST_KEY}" "NoRepair" 1

  WriteUninstaller "$INSTDIR\uninstall.exe"
SectionEnd

Section "桌面快捷方式" SecDesktop
  ; 【为什么要删一遍当前用户那份】老版本用的是 user 上下文（快捷方式在用户桌面），
  ; 从它升级上来时，那一条会一直留着，桌面上出现两个同名图标。
  SetShellVarContext current
  Delete "$DESKTOP\${PRODUCT_NAME}.lnk"
  SetShellVarContext all
  CreateShortCut "$DESKTOP\${PRODUCT_NAME}.lnk" "$INSTDIR\${PRODUCT_EXE}" "" "$INSTDIR\${PRODUCT_EXE}" 0
SectionEnd

Section "开始菜单" SecStartMenu
  SetShellVarContext all
  CreateDirectory "$SMPROGRAMS\${PRODUCT_NAME}"
  CreateShortCut "$SMPROGRAMS\${PRODUCT_NAME}\${PRODUCT_NAME}.lnk" "$INSTDIR\${PRODUCT_EXE}" "" "$INSTDIR\${PRODUCT_EXE}" 0
  CreateShortCut "$SMPROGRAMS\${PRODUCT_NAME}\卸载 ${PRODUCT_NAME}.lnk" "$INSTDIR\uninstall.exe"
SectionEnd

; ---------------------------------------------------------------- 卸载
; 【$R0 的语义】"1" = 连配置数据一起删。默认 "0"（保留）。
; · 交互卸载：弹一句询问，用户选；
; · 静默卸载：默认保留（安全），要删须显式 /DELETE_DATA=1。
Function un.onInit
  SetShellVarContext all
  StrCpy $R0 "0"

  ${GetParameters} $R1
  ${GetOptions} $R1 "/DELETE_DATA=" $R2
  ${IfNot} ${Errors}
    StrCpy $R0 "$R2"
  ${EndIf}

  ${IfNot} ${Silent}
    MessageBox MB_YESNO|MB_ICONQUESTION \
      "是否同时删除配置与传输状态数据？$\r$\n（失败清单、最近路径、上次任务结果、续传记录都在里面）$\r$\n$\r$\n选「否」可保留，重装后继续使用。" \
      IDYES un_setdel
    StrCpy $R0 "0"
    Goto un_init_done
  un_setdel:
    StrCpy $R0 "1"
  un_init_done:
  ${EndIf}
FunctionEnd

Section "Uninstall"
  SetShellVarContext all

  nsExec::Exec 'taskkill /F /IM "${PRODUCT_EXE}"'
  Pop $0
  Sleep 500

  ; ---- 程序本体：逐个删（比 RMDir /r 安全 —— 不会顺带清掉 data\）----
  Delete "$INSTDIR\${PRODUCT_EXE}"
  Delete "$INSTDIR\license.txt"
  Delete "$INSTDIR\部署说明.txt"
  Delete "$INSTDIR\uninstall.exe"
  RMDir /r "$INSTDIR\vendor"

  ; ---- 配置与状态数据（询问后才删）----
  ${If} $R0 == "1"
    RMDir /r "$INSTDIR\data"
    ; 兜底数据目录：Program Files 不可写时程序退到 %LOCALAPPDATA%
    RMDir /r "$LOCALAPPDATA\文件传输工具"
    DetailPrint "已删除配置与状态数据"
  ${Else}
    DetailPrint "已保留配置与状态数据：$INSTDIR\data"
  ${EndIf}

  RMDir "$INSTDIR"            ; 只有空了才删得掉；data 留着时这里会保留目录，正是我们要的

  ; ---- 快捷方式：两套上下文都删 ----
  ; 安装用 all（公共桌面 / 所有用户开始菜单），但老版本用的是 current；
  ; 从旧版升级上来的机器两处都可能有，只删一套就会留一个点不开的图标。
  SetShellVarContext all
  Delete "$DESKTOP\${PRODUCT_NAME}.lnk"
  Delete "$SMPROGRAMS\${PRODUCT_NAME}\${PRODUCT_NAME}.lnk"
  Delete "$SMPROGRAMS\${PRODUCT_NAME}\卸载 ${PRODUCT_NAME}.lnk"
  RMDir  "$SMPROGRAMS\${PRODUCT_NAME}"
  SetShellVarContext current
  Delete "$DESKTOP\${PRODUCT_NAME}.lnk"
  Delete "$SMPROGRAMS\${PRODUCT_NAME}\${PRODUCT_NAME}.lnk"
  Delete "$SMPROGRAMS\${PRODUCT_NAME}\卸载 ${PRODUCT_NAME}.lnk"
  RMDir  "$SMPROGRAMS\${PRODUCT_NAME}"
  SetShellVarContext all

  ; ---- WebView2：只提示，不代删 ----
  ReadRegDWORD $0 HKLM "${UNINST_KEY}" "WebView2InstalledByUs"
  ${If} $0 == "1"
    ${IfNot} ${Silent}
      MessageBox MB_ICONINFORMATION|MB_OK \
        "WebView2 运行时是本程序安装的，但它是 Windows 的共享组件（其它程序也可能在使用），$\r$\n因此卸载程序不会自动移除它。$\r$\n$\r$\n如需一并移除：设置 → 应用 → 找到「Microsoft Edge WebView2 Runtime」→ 卸载。"
    ${EndIf}
  ${EndIf}

  DeleteRegKey HKLM "${UNINST_KEY}"
SectionEnd
