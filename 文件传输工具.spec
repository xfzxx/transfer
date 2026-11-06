# -*- mode: python ; coding: utf-8 -*-
#
# ============================ 功能C1：ttkbootstrap 的打包资源 ============================
# 【这个 spec 原来是什么样】`datas=[]` / `hiddenimports=[]` —— 因为在那之前
#   本项目**一个第三方依赖都没有**（全是 stdlib + tkinter），没有任何东西需要在
#   Analysis 里额外声明。功能C1 引入了 ttkbootstrap，这一节是跟着来的。
#
# 【用户给的约束原文】"打包时显式 `--add-data` 打包 ttkbootstrap 的 png"。
#   ⚠️ **实际去盘上数过之后，情况和那句话的前提不一样，如实记在这里**：
#     * 侦察文档 `_nightly\05_prep_C.md` §1 里那张表（"12 个 png + 3 个 json +
#       1 个 ttf + 1 个 ico"）量的是 **ttkbootstrap 2.2.3** 的 wheel；
#     * 但本项目最终**钉在 1.20.4**（理由见 `config.py` 的 `C_UI_THEME` 那一段：
#       2.x 把 `litera`/`cosmo` 这两个主题名**删掉了**）。
#     * **1.20.4 里一个 png 都没有。** 实测该包全部内容：
#         `.py` 38 个 / `.pyi` 1 个 / `py.typed` 1 个 / `localization\README.md` 1 个
#       —— 也就是**纯 Python，零二值资源**。
#     * 那它靠什么画进度条/单选/复选的样式？**靠 PIL 在内存里现画**：
#         `style.py:1562  ImageTk.PhotoImage(Image.new("RGB", hsize, background))`
#       这类调用在 `style.py` 里有 51 处 `PhotoImage` / 41 处 `ImageTk`。
#       没有文件要读，自然也没有文件要打。
#
#   → 所以"显式 --add-data 那个 png"这句**在 1.20.4 上无对象可指**。
#     但**那句话的意图仍然成立，而且更要紧**：ttkbootstrap 有一批**运行时按名字
#     动态导入**的东西（主题、控件、对话框），PyInstaller 的静态分析**跟不到**；
#     漏掉的症状正是那类"开发态一切正常、打包后控件空白或直接崩"的 bug。
#     下面两条就把这个意图落到实处（比手写 `--add-data` 更全，且换版本也不会漏）：
#
#       ① `collect_data_files('ttkbootstrap')`
#          —— 把包内**真实存在**的非 .py 资源全带上（1.20.4 上是 3 个：
#             `py.typed` / `__init__.pyi` / `localization\README.md`）。
#             现在看是"没用的小文件"，但它保证**将来升级到有 png 的版本时自动生效**，
#             不需要谁记得回来改这个 spec。
#       ② `collect_submodules('ttkbootstrap')`
#          —— 把 38 个子模块**全部**登记为 hiddenimports。
#             ttkbootstrap 的 `builder` 按**字符串名字**去 getattr 那些模块/方法
#             （`builder.name_to_method(...)`，本项目的 `_quiet_option_menu` 正好
#             走过这条路径），静态分析看不到这种引用。
#
# 【还有一条是 Pillow 的老坑，跟数据文件无关但**同一类**】
#   `PIL._tkinter_finder` 是 `ImageTk` 的运行时依赖，**不由 `import PIL` 带出来**。
#   缺了它，打包后的 exe 会在 `ImageTk.PhotoImage(...)` 那一刻抛
#   `ModuleNotFoundError` —— 而那正是**上面 style.py:1562 那一行**，
#   也就是**每次建主题都会走到的路径**。开发态永远不会暴露。
#   PyInstaller 自带的 PIL hook 通常会补上，但这里**显式写一遍**：
#   它零成本，而漏掉它的失败模式是"点开 exe 就崩"。
# ============================ 功能C1：ttkbootstrap 的打包资源 ============================

from PyInstaller.utils.hooks import collect_data_files, collect_submodules

_tb_datas = collect_data_files('ttkbootstrap')
_tb_hidden = collect_submodules('ttkbootstrap')

# ============================ 功能C3/C4：Web 前端资源 ============================
# 【为什么必须写在这里】Web 档（C3=pywebview / C4=stdlib HTTP）把界面渲染成了
#   `web\index.html` + `web\app.css` + `web\app.js` 这套**纯静态文件**，由 `web_app.py`
#   在运行时按 `WEB_DIR` 从盘上读。PyInstaller 的静态分析**只认 import**，
#   这三份文件没有任何 `import` 指着它们 ⇒ 不打进去的话，开发态一切正常，
#   **打包后的 exe 一开 web 档就白屏**（找不到 index.html）——正是那一类
#   "开发态看不见、打包才炸"的 bug。
#   ⇒ 用 `datas` 把整个 `web\` 目录原样带上，运行时落点在 `sys._MEIPASS\web\`，
#     与 `web_app.WEB_DIR` 的探测逻辑对齐。
#
# 【pywebview 的资源同理，且更隐蔽】C3 壳靠 pywebview，它运行时会把自带的
#   `js\*.js`（JS 桥）注入到页面里。这些是非 .py 文件，PyInstaller 默认不带 ⇒
#   C3 下 `window.pywebview` 永远不出现（`pump` 节拍器失效）。所以
#   `collect_data_files('webview')` 一并打进来。
import os
from PyInstaller.utils.hooks import collect_data_files as _cdf

# `SPECPATH` 由 PyInstaller 注入 = 本 spec 所在目录（可靠）；老版本没有时退回 cwd。
_here = globals().get("SPECPATH") or os.path.abspath(os.getcwd())
_web_datas = [(os.path.join(_here, 'web'), 'web')]
_pv_datas = _cdf('webview')
# pywebview 按**平台名**动态 import（如 `webview.platforms.edgechromium`），
# 静态分析跟不到 ⇒ 显式 collect_submodules。
_pv_hidden = collect_submodules('webview')
# 单文件 exe 红线：这些都进 `a.datas`，仍由 bootloader 解包到临时目录 ⇒ 不破坏单文件。
# ============================ 功能C3/C4：Web 前端资源 ============================

# ============================ 图标与版本资源（2026-11-06） ============================
# 【为什么必须在 spec 里做】打包产物原来**既没有图标也没有版本资源**（实测
#   `(Get-Item exe).VersionInfo` 四项全空）。后果不是好看不好看：
#     · 桌面/开始菜单上是 PyInstaller 的默认图标，医院的人认不出是哪个软件；
#     · IT 装完后**无法从文件属性确认版本**，只能启动看标题栏 —— 十几台机器
#       没法盘点是哪个版本。
# 【为什么版本号从 config.py 读，而不是在这里写一份】版本只有一处真相。
#   写两份的必然结局是 exe 是 2.6、安装包写着 2.5，谁也说不清在跑哪个。
import os as _os
import re as _re

from PyInstaller.utils.win32.versioninfo import (
    FixedFileInfo, StringFileInfo, StringStruct, StringTable, VarFileInfo,
    VarStruct, VSVersionInfo)


def _app_version():
    p = _os.path.join(SPECPATH, "config.py")
    with open(p, encoding="utf-8") as f:
        m = _re.search(r'^APP_VERSION\s*=\s*"([^"]+)"', f.read(), _re.M)
    return m.group(1) if m else "0.0.0"


_VER = _app_version()
_nums = [int(x) for x in (_VER.split(".") + ["0", "0", "0"])[:3]]
_ver4 = (_nums[0], _nums[1], _nums[2], 0)

_version_info = VSVersionInfo(
    ffi=FixedFileInfo(filevers=_ver4, prodvers=_ver4,
                      mask=0x3f, flags=0x0, OS=0x40004, fileType=0x1, subtype=0x0,
                      date=(0, 0)),
    kids=[
        StringFileInfo([
            StringTable('080404B0', [
                StringStruct('CompanyName', '院内文件传输'),
                StringStruct('FileDescription', '文件传输工具'),
                StringStruct('FileVersion', _VER),
                StringStruct('InternalName', 'transfer'),
                StringStruct('OriginalFilename', '文件传输工具.exe'),
                StringStruct('ProductName', '文件传输工具'),
                StringStruct('ProductVersion', _VER),
            ])
        ]),
        VarFileInfo([VarStruct('Translation', [0x0804, 1200])]),
    ])

_ver_path = _os.path.join(SPECPATH, "build", "version_info.txt")
_os.makedirs(_os.path.dirname(_ver_path), exist_ok=True)
with open(_ver_path, "w", encoding="utf-8") as _f:
    _f.write(str(_version_info))

a = Analysis(
    # 入口 = Web 宿主（内部 `import file_transfer` ⇒ 业务代码全部纳入）。
    # `host_probe.detect()` 开机自检决定 C3 / C4；命令行 `--c3/--c4` 可强制。
    ['web_app.py'],
    pathex=[],
    binaries=[],
    datas=_tb_datas + _web_datas + _pv_datas,
    # 【2026-11-04 记一笔】`notify_win` 是函数内 import（`_notify_run_end` 里），
    # 特意验证过 PyInstaller **能自动识别函数级 import**：不登记 hiddenimports 时
    # `build\...\Analysis-00.toc` 里也能查到它（实测 1 条）。所以这里**不加**登记
    # —— 保持 spec 少一处需要跟着代码改的地方。
    # ⚠️ 查它有没有进包，别用 `pyi-archive_viewer -l`（默认不递归，纯 py 模块在 PYZ 里，
    #   永远 grep 不到）；用 `-l -r`，或者直接 grep Analysis-00.toc。
    hiddenimports=_tb_hidden + ['PIL._tkinter_finder'] + ['web_kit', 'web_app', 'web_host'] + _pv_hidden,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name='文件传输工具',
    icon=_os.path.join(SPECPATH, 'app.ico'),
    version=_ver_path,
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    # 【打包红线，2026-10-14 裁定】`upx=True` 必须改成 `False`：
    #   UPX 压缩过的 exe **极易触发杀软启发式**（本项目实测环境有奇安信在跑）。
    #   省下的那点体积，远不值"成品被拦、用户装不上"的代价。
    upx=False,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
