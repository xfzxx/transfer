# -*- coding: utf-8 -*-
"""判据：打包入口能认 `--run-task`（定时任务在 exe 里真的会跑）。

【这条判据守的是什么】`sched_tab` 往 schtasks 里写的 `/TR` 是
`<exe> --run-task <任务名>`（`config.schtasks_run_command`），而**打包后的入口是
`web_app.py`**。两边只要有一边不认这个参数，症状就一模一样、而且都很难发现：

    · 到点弹出一个界面，任务等于没跑；
    · 界面上那条任务显示"已注册"，一切正常；
    · `last_run.json` 永远不更新（而用户以为备份在跑）。

所以这里不测"代码里有没有那行"，而是**真的用打包入口跑一次**，看它是不是按
无头的规矩退出（退出码 + 无窗口）。
"""
import os
import subprocess
import sys

import config

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def test_packaged_entry_handles_run_task():
    """用 web_app（打包入口）跑一个不存在的任务：必须按无头的规矩退出 4。"""
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    proc = subprocess.run(
        [sys.executable, os.path.join(ROOT, "web_app.py"),
         "--run-task", "这条任务一定不存在-判据用"],
        cwd=ROOT, capture_output=True, text=True, encoding="utf-8",
        timeout=120, env=env)
    assert proc.returncode == config.SCHEDULE_EXIT_CANNOT_START, (
        "退出码 %d，期望 %d（起不来）。stdout=%r stderr=%r\n"
        "如果这里是 0 或超时，多半是 `--run-task` 没被分派，程序去开了 GUI。"
        % (proc.returncode, config.SCHEDULE_EXIT_CANNOT_START, proc.stdout, proc.stderr))
    assert "配置里没有" in (proc.stdout + proc.stderr)


def test_headless_dispatch_happens_before_any_gui():
    """分派必须排在单实例互斥 / build() 之前 —— 否则计划任务会去抢界面互斥量。

    【只能在 main() 里面找】`_acquire_instance_mutex` 在这个文件里还有定义与其它引用，
    全文 index 会撞到函数定义那一行，结论就反了（第一次写这条就是这么错的）。
    """
    with open(os.path.join(ROOT, "web_app.py"), "r", encoding="utf-8") as f:
        src = f.read()
    base = src.index("def main(argv=None):")
    body = src[base:]
    i_dispatch = body.index("_parse_run_task_arg(argv)")
    i_mutex = body.index("_acquire_instance_mutex()")
    i_build = body.index("bridge = build()")
    assert i_dispatch < i_mutex < i_build


def test_main_entry_uses_shared_run_headless():
    """两处入口共用同一个实现 —— 否则迟早演变成两套无头逻辑。"""
    with open(os.path.join(ROOT, "file_transfer.py"), "r", encoding="utf-8") as f:
        ft = f.read()
    assert "def run_headless(" in ft
    assert "sys.exit(run_headless(_headless_name))" in ft

    with open(os.path.join(ROOT, "web_app.py"), "r", encoding="utf-8") as f:
        wa = f.read()
    assert "file_transfer.run_headless(" in wa


def test_schtasks_command_matches_what_we_accept():
    """注册用的命令行与实际支持的参数必须是同一套（--run-task 两种写法都收）。"""
    cmd = config.schtasks_run_command(r"C:\x\文件传输工具.exe", "每日备份")
    assert "--run-task" in cmd and "每日备份" in cmd
    import sched_tab
    assert sched_tab._parse_run_task_arg(["--run-task", "每日备份"]) == "每日备份"
    assert sched_tab._parse_run_task_arg(["--run-task=每日备份"]) == "每日备份"
    assert sched_tab._parse_run_task_arg([]) is None
