# -*- coding: utf-8 -*-
"""判据：安装包脚本的**升级**与**卸载**纪律。

【为什么这些要写成判据】安装包是唯一一处"跑在别人机器上、出错无法当面修"的代码。
它的问题不会在 CI 里冒出来，只会在医院现场变成"装了两份""卸载后残留 900MB"
"升级后数据没了"。下面每一条都对应一次真实的坑：

    · 没有 `InstallDirRegKey` → 升级变成并列安装两份，用户在旧图标上点开的是旧版；
    · 没有降级确认       → 拿错包（旧版）一装，悄无声息地退回旧版本；
    · 卸载只删 all 上下文 → 从老版本（用户级快捷方式）升级上来的机器，
                            桌面上留一个点不开的图标；
    · 卸载不区分数据     → 要么把用户配置一起删掉（不可接受），
                            要么永远留一堆没人清的数据（也不干净）。
"""
import os
import re

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
NSI = os.path.join(ROOT, "installer", "installer.nsi")


def _nsi():
    with open(NSI, "r", encoding="utf-8-sig") as f:
        return f.read()


def _code_only(text):
    """去掉注释行与行尾注释，只留下真正会执行的指令。"""
    lines = []
    for ln in text.splitlines():
        stripped = ln.split(";", 1)[0]
        lines.append(stripped)
    return "\n".join(lines)


def test_script_and_assets_exist():
    assert os.path.exists(NSI)
    for name in ("license.txt", "部署说明.txt", "build.py", "fetch-webview2.py"):
        assert os.path.exists(os.path.join(ROOT, "installer", name)), name


def test_upgrade_installs_into_same_directory():
    """升级必须落在原目录，否则同一台机器上会出现两份安装。"""
    assert "InstallDirRegKey" in _code_only(_nsi())


def test_downgrade_asks_before_proceeding():
    code = _code_only(_nsi())
    assert "VersionCompare" in code, "没有版本比较 → 旧包会静默覆盖新包"
    assert "属于降级" in code and "Abort" in code, "识别到降级后必须能中止"


def test_old_payload_is_removed_on_upgrade():
    """上一版的程序文件要先删再写：改名/删掉的文件不能永远留在目录里。"""
    code = _code_only(_nsi())
    fn = code.split("Function RemovePrevPayload", 1)
    assert len(fn) == 2, "缺少 RemovePrevPayload"
    body = fn[1].split("FunctionEnd", 1)[0]
    for needle in ("PRODUCT_EXE", "license.txt", "部署说明.txt", "uninstall.exe"):
        assert needle in body, "RemovePrevPayload 没清理 %s" % needle
    # 绝不能整目录删：那会把 data\ 一并带走
    assert "RMDir /r \"$INSTDIR\"" not in body


def test_uninstall_removes_both_shortcut_contexts():
    """安装用 all 上下文，但老版本用 current —— 两套都得删。

    【别按字面量数】快捷方式名用的是 `${PRODUCT_NAME}` 宏，不是"文件传输工具"；
    这里数的是 `SetShellVarContext` 的切换与 `.lnk` 的删除次数。
    """
    code = _code_only(_nsi())
    un = code.split('Section "Uninstall"', 1)
    assert len(un) == 2
    body = un[1]
    assert "SetShellVarContext all" in body and "SetShellVarContext current" in body
    assert body.count(".lnk") >= 4, "两套上下文的桌面/开始菜单快捷方式都要删（%d 条）" % body.count(".lnk")


def test_uninstall_asks_about_user_data():
    code = _code_only(_nsi())
    assert "DELETE_DATA" in code, "静默卸载无法清数据 → 批量部署后残留"
    assert "$INSTDIR\\data" in code
    assert "LOCALAPPDATA" in code, "兜底数据目录（Program Files 不可写时的落点）没清"


def test_silent_mode_never_shows_message_box():
    """静默部署时弹框 = 批处理卡死在无人值守的机器上。

    两种合法写法都算：
        ${IfNot} ${Silent}  → 弹框
        ${If} ${Silent} … ${Else} → 弹框     （静默分支先处理掉）
    """
    code = _code_only(_nsi())
    for m in re.finditer(r"MessageBox", code):
        head = code[max(0, m.start() - 400):m.start()]
        assert ("${IfNot} ${Silent}" in head[-300:] or "${If} ${Silent}" in head[-300:]), \
            "这个 MessageBox 没有静默守卫：\n%s" % code[max(0, m.start() - 160):m.start() + 80]


def test_vendor_runtime_is_not_committed():
    """.gitignore 必须挡住 215MB 的第三方安装器与产物。"""
    with open(os.path.join(ROOT, ".gitignore"), "r", encoding="utf-8") as f:
        gi = f.read()
    assert "/installer/vendor/" in gi
    assert "/installer/out/" in gi


def test_build_reads_version_from_config():
    """版本号单一来源：安装包版本必须来自 config.APP_VERSION，不能手写两份。"""
    import importlib.util
    spec = importlib.util.spec_from_file_location("inst_build",
                                                  os.path.join(ROOT, "installer", "build.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    with open(os.path.join(ROOT, "config.py"), "r", encoding="utf-8") as f:
        m = re.search(r'^APP_VERSION\s*=\s*"([^"]+)"', f.read(), re.M)
    assert m, "config.py 里没有 APP_VERSION"
    assert mod.app_version() == m.group(1)
    assert "PRODUCT_VERSION" in _nsi()
