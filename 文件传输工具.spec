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

a = Analysis(
    ['file_transfer.py'],
    pathex=[],
    binaries=[],
    datas=_tb_datas,
    hiddenimports=_tb_hidden + ['PIL._tkinter_finder'],
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
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
