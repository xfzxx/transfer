# -*- coding: utf-8 -*-
"""判据：生产代码里禁止**硬编码家目录**。

【从哪来】`_legacy_checks/check_home_paths.py`（原作者写的，311 行）。
原脚本靠扫描 `PROJECT/*.py` 找 `C:\\Users\\` / `/home/` 字面量，守护的是
「测试沙箱改道 `USERPROFILE` 不失效」这个前提。本文件**只搬判据、不搬脚本**：
重写成 pytest，扫仓库根的生产模块，测试环境依赖（`_sandbox`、老盘符）全部去掉。

【守的是什么】数据落点必须**在进程运行时**从环境算出来。只要有人在某个生产模块里
写一句 `STATE_DB = r"C:\\Users\\xxx\\....db"`，任何"改道隔离"都会静默失效——
测试照样绿，而它对着**用户真库**动手。那种失效的症状不是红，是"用户的库某天没打招呼就没了"。

【尺度（照搬原脚本的克制）】只禁家目录字面量，**不禁**其它绝对路径。
一条会误报的检查比没有检查更糟：它训练所有人无视红色。

【两条照抄原脚本的纪律】
  · **判据要能被证伪**：`test_positive_control_*` 两个方向都验一遍（该拦的拦得住、
    该放的放得出去）。只验一个方向的判据，写成 `return True` 也能全绿。
  · **注释不算违规**：本项目注释密度极高，跳过以 `#` 开头的行，否则满屏假红。
"""
import os
import re

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

FORBIDDEN = [
    (re.compile(r"[Cc]:[\\/]+Users[\\/]"), "Windows 家目录字面量"),
    (re.compile(r"['\"]/(?:home|Users)/"), "POSIX 家目录字面量"),
    (re.compile(r"Documents and Settings", re.I), "旧版 Windows 家目录字面量"),
]

# 允许直接读家目录的文件 —— **白名单要短，加人要写理由**。
#   config.py  : 5 个数据落点常量在这里一次性算出来（LOCALAPPDATA 优先）。
#   web_app.py : `fs_list` 的默认起始目录，只读不写。
ALLOWED_HOME_READERS = {"config.py", "web_app.py"}

HOME_READ = re.compile(r"expanduser\s*\(\s*['\"]~|environ\.get\(\s*['\"](?:USERPROFILE|HOME)['\"]")


def scan_hardcoded_home(name, source):
    """→ 违规行列表。纯函数：喂合成源码就能做阳性对照。"""
    hits = []
    for lineno, line in enumerate(source.splitlines(), 1):
        if line.strip().startswith("#"):
            continue
        for pattern, why in FORBIDDEN:
            if pattern.search(line):
                hits.append("%s:%d  %s  →  %s" % (name, lineno, why, line.strip()[:90]))
    return hits


def scan_home_readers(name, source):
    hits = []
    for lineno, line in enumerate(source.splitlines(), 1):
        if line.strip().startswith("#"):
            continue
        if HOME_READ.search(line):
            hits.append("%s:%d  %s" % (name, lineno, line.strip()[:90]))
    return hits


def _production_modules():
    for name in sorted(os.listdir(ROOT)):
        path = os.path.join(ROOT, name)
        if name.endswith(".py") and os.path.isfile(path):
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                yield name, f.read()


def test_no_hardcoded_home_paths():
    hits = []
    for name, source in _production_modules():
        hits += scan_hardcoded_home(name, source)
    assert not hits, "生产代码里出现硬编码家目录：\n" + "\n".join(hits)


def test_home_dir_readers_are_whitelisted():
    """直接与家目录打交道的模块必须登记在 `ALLOWED_HOME_READERS` 里。

    这条比上一条更严：上一条管"不许写死路径"，这条管"读家目录这件事不许扩散"。
    落点越散，"改道隔离"越容易漏一个。
    """
    offenders = []
    for name, source in _production_modules():
        if name in ALLOWED_HOME_READERS:
            continue
        offenders += scan_home_readers(name, source)
    assert not offenders, (
        "这些模块直接读了家目录，但没有登记在白名单里：\n%s\n"
        "优先改成从 config.DATA_DIR 派生；确需例外再登记并写明理由。" % "\n".join(offenders))


# ---------------------------------------------------------------- 阳性对照
def test_positive_control_catches_windows_home():
    bad = 'DB = r"C:\\Users\\someone\\.state.db"\n'
    assert scan_hardcoded_home("synthetic.py", bad), "该拦的没拦住——判据是空的"


def test_positive_control_lets_normal_paths_through():
    good = 'DB = os.path.join(config.DATA_DIR, "state.db")\n# C:\\Users 只出现在注释里\n'
    assert not scan_hardcoded_home("synthetic.py", good), "该放的没放过去——判据会制造假红"
