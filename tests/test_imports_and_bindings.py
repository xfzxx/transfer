# -*- coding: utf-8 -*-
"""装配与绑定判据：模块能不能全拉起 + 前后端名字对不对得上。

`check_bindings.py` 用**子进程**跑，不是懒：它内部会 `web_app.build()`，
那是"往模块命名空间里装替身"的全局副作用（`web_kit.install()`），
放在 pytest 进程里做，会把别的用例看到的 `tk` / `ttkbootstrap` 也换掉。
"""
import importlib
import os
import subprocess
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

MODULES = ["config", "state_db", "ftp_client", "ftp_transfer", "transfer_core",
           "host_probe", "ui_channel", "ui_dialogs", "sched_tab",
           "maintenance_worker", "file_transfer", "transfer_form",
           "web_kit", "web_host", "web_app", "app_bootstrapper"]


@pytest.mark.parametrize("name", MODULES)
def test_module_imports(name):
    """16 个生产模块必须都能单独 import（替身层的 `_TARGET_MODULES` 依赖这一点）。"""
    assert importlib.import_module(name) is not None


def test_bindings_have_no_error():
    """前后端绑定零 ERROR：data-var / data-click / 语义动作 全部对得上。"""
    # 【为什么要显式给 PYTHONIOENCODING】它在 CI 上真红过一次：Windows runner 的子进程
    #   stdout 默认 cp1252，脚本里的中文 print 直接 UnicodeEncodeError —— 判据没毛病，
    #   是"输出编码"把判据判死了。脚本自己已经 reconfigure，这里再上一道保险，
    #   让"判据本身"与"跑判据的环境"彻底解耦。
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    proc = subprocess.run(
        [sys.executable, os.path.join(ROOT, "tools", "check_bindings.py")],
        cwd=ROOT, capture_output=True, text=True, encoding="utf-8",
        errors="replace", env=env, timeout=180)
    if proc.returncode != 0:
        pytest.fail("绑定检查有 ERROR：\n%s\n%s" % (proc.stdout, proc.stderr))
