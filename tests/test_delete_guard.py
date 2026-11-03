# -*- coding: utf-8 -*-
r"""判据：生产代码里任何**递归删除**都必须登记在案。

【从哪来】`_legacy_checks/check_archive_guard.py`（原作者写的，560 行）。
原脚本守的是「没有任何 `rmtree` 能碰到归档根 `D:\dcm_test\_archive`」，靠三层
（阳性对照 / 真删实验 / 归档根快照）。那三层里有大量依赖具体盘符与真删实验的部分，
搬到这台机器上只会假红。本文件**只保留最内核的那一层判据**：

    生产代码里的每一次递归删除，都必须出现在 `REGISTERED` 名单里，且名单里写了理由。

【为什么这条值钱】归档根被删掉这件事**不会报错、不会弹窗、不进回收站**——
盘上少一个目录，没人会收到通知。原作者的原话是"约定失效的症状是没有症状"。
所以把它变成一条会变成**一条 FAIL 行**的东西。

【为什么不直接照抄"禁碰归档根"】归档根是 `D:\dcm_test\_archive`，本机不存在，
照抄等于一条永远绿的空判据。改成"必须登记"更通用：**新增**一个 rmtree 会立刻红，
写的人被迫想一遍"我要删的这个目录，删错了会怎样"。

【当前状态】生产代码里**一处 `rmtree` 都没有**（已实测），所以名单为空——
这不是"没写判据"，是"这条判据现在正好守着零个调用点，一旦有人加就立刻生效"。
"""
import ast
import os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# 递归删除调用点白名单：{函数名: 理由}
#   —— 加进来之前先回答一句：**这个目录被误删，用户会损失什么？**
REGISTERED = {}

DELETE_FUNCS = {("shutil", "rmtree"), ("os", "rmdir")}


def _iter_production_modules():
    for name in sorted(os.listdir(ROOT)):
        path = os.path.join(ROOT, name)
        if name.endswith(".py") and os.path.isfile(path):
            yield name, path


def _qualified(func):
    """→ ('shutil', 'rmtree') 这样的二元组；认不出就返回 None。"""
    if isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name):
        return (func.value.id, func.attr)
    if isinstance(func, ast.Name):
        return ("", func.id)
    return None


def _looks_like_root(arg):
    """实参是不是盘根 / 过短的绝对路径字面量（`"D:\\\\"`、`"C:/"`）。"""
    if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
        s = arg.value
        return len(s) <= 4 and (s.endswith("\\") or s.endswith("/"))
    return False


def _scan_deletes(name, source):
    """→ [(位置, 是否已登记, 说明)]。纯函数，方便做阳性对照。"""
    out = []
    tree = ast.parse(source, name)
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        if _qualified(node.func) not in DELETE_FUNCS:
            continue
        key = "%s:%d" % (name, node.lineno)
        out.append((key, key in REGISTERED, node.func.attr))
        if node.args and _looks_like_root(node.args[0]):
            out.append((key, "盘根", getattr(node.args[0], "value", "")))
    return out


def test_every_recursive_delete_is_registered():
    problems = []
    for name, path in _iter_production_modules():
        with open(path, "r", encoding="utf-8") as f:
            source = f.read()
        for key, registered, _fn in _scan_deletes(name, source):
            if registered is False:
                problems.append("%s 未登记：先想清楚误删的后果，再写进 REGISTERED 并注明理由" % key)
    assert not problems, "\n".join(problems)


def test_no_delete_of_drive_root():
    """任何删除调用都不许直接指向盘根 —— 这条没有白名单，谁来都不许。"""
    bad = []
    for name, path in _iter_production_modules():
        with open(path, "r", encoding="utf-8") as f:
            for key, registered, value in _scan_deletes(name, f.read()):
                if registered == "盘根":
                    bad.append("%s  删除目标是盘根：%r" % (key, value))
    assert not bad, "\n".join(bad)


# ---------------------------------------------------------------- 阳性对照
def test_positive_control_catches_unregistered_rmtree():
    src = "import shutil\nshutil.rmtree(r'D:\\dcm_test\\_archive')\n"
    hits = _scan_deletes("synthetic.py", src)
    assert any(r is False for _k, r, _f in hits), "该拦的没拦住——判据是空的"


def test_positive_control_passes_clean_module():
    src = "import os\nos.path.join('a', 'b')\nname.rstrip('\\\\/')\n"
    assert not _scan_deletes("synthetic.py", src), "该放的没放过去——会把 rstrip 当成删除"
