# -*- coding: utf-8 -*-
"""判据：运行期产物不许漏进版本库。

【从哪来】`_legacy_checks/run_all.py` 里那道「产物污染扫描」（原作者在回归 #15 里
把它跑成 PASS 的那一项），加上 `check_sandbox_entry` 那条"落点必须收敛"的精神。
原实现跟着套件一起跑、依赖固定盘符；这里剥出来变成独立的、跑在 CI 里的判据。

【守的是什么】这个项目栽过一次：每轮回归往项目根堆产物，堆到 **149 个**文件。
那种污染的形态是"git status 里突然一片 ??，而没人知道哪个是该提交的"。
更坏的一种是**把带 FTP 明文凭据的 `data\\` 提交上去** —— 它在 .gitignore 里，
但只要哪天有人改了 ignore 规则或者用了 `git add -f`，就进去了。
"""
import os
import shutil
import subprocess

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# 已知的运行期产物目录：必须全部被 ignore 覆盖
ARTIFACT_DIRS = [
    "data",            # 状态库 / 端口落点 / UX 状态（**历史上有过 FTP 明文凭据**）
    "__pycache__",
    "_tmp",
    "_checks",         # 旧测试套件原位
    "_legacy_checks",  # 旧测试套件隔离位（2026-11-03 移植后）
    "dist",
    "build",
]

GIT = shutil.which("git")


def _git(*args):
    return subprocess.run([GIT] + list(args), cwd=ROOT, capture_output=True,
                          text=True, encoding="utf-8", errors="replace")


@pytest.mark.skipif(not GIT, reason="环境里没有 git")
def test_artifact_dirs_are_fully_ignored():
    """已经存在的产物目录，`git status` 对它们必须一个字都不报。"""
    problems = []
    for name in ARTIFACT_DIRS:
        path = os.path.join(ROOT, name)
        if not os.path.isdir(path):
            continue
        out = _git("status", "--porcelain", "--", name).stdout.strip()
        if out:
            problems.append("%s/ 里有未被忽略的文件：\n%s" % (name, out[:500]))
    assert not problems, "\n\n".join(problems)


@pytest.mark.skipif(not GIT, reason="环境里没有 git")
def test_no_artifact_is_tracked():
    """反过来查：产物目录里的东西**一个都不该被跟踪**（防 `git add -f`）。"""
    tracked = _git("ls-files").stdout.splitlines()
    bad = [p for p in tracked
           if p.startswith("data/") or p.endswith(".pyc") or "/__pycache__/" in p]
    assert not bad, "这些产物被误提交了：%s" % bad[:20]


def test_gitignore_covers_every_artifact_dir():
    """`.gitignore` 必须显式覆盖每个产物目录 —— 不靠通配符碰运气。"""
    with open(os.path.join(ROOT, ".gitignore"), "r", encoding="utf-8") as f:
        content = f.read()
    missing = [n for n in ARTIFACT_DIRS
               if ("/%s/" % n) not in content and ("%s/" % n) not in content]
    assert not missing, "`.gitignore` 没有覆盖这些产物目录：%s" % missing
