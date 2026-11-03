# -*- coding: utf-8 -*-
r"""判据：界面**装得下**，日志区不被挤扁。

【为什么要有这条】这是项目技术债 T4 的可执行版本，原文写得很清楚：
「尺寸仍是**人肉量出来的常数**，不是按实际字体算的……谁再往界面里加一行
（或再换一次字体/主题），缺口照样**全部**落在日志区身上 —— 而 `resizable(False, False)`
意味着用户自己拉不动。」

2026-11-03 升级 ttkbootstrap 1.20.4 → 2.2.6 时，这条正是判据：
换主题 = 换字体 = 换行高，而**没有任何东西会报错**，只是日志区少两行。
当年功能 C1 换肤就把它撬动过一次（余量 17px → 1px），靠人肉量才发现。

判据本身很简单：**`winfo_reqheight()` ≤ 窗口高度**。
（原作者特别提醒：`main.winfo_reqheight()` 才是对的，各行 `reqheight` 的算术和
会漏掉 pady，方向正好反过来。）

⚠️ 这是全套判据里**唯一会开真窗口**的一条。它不需要人来点，也不阻塞：
建完立刻 withdraw，量完 destroy。万一哪天它卡住，先看是不是弹了模态框
（状态库里残留未完成任务时会弹恢复对话框）。
"""
import tkinter as tk

import pytest

# 窗口高度的**唯一来源**是应用自己 set 的 geometry 字符串，不在这里抄一份。
# 抄一份 = 第二份真相：尺寸改了这边还在按老值判，而且不会报错。


def _make_root():
    try:
        return tk.Tk()
    except Exception as e:                       # noqa: BLE001 —— 无显示环境
        pytest.skip("这个环境起不了 Tk 窗口：%s" % e)


def test_window_fits_its_content():
    import file_transfer

    root = _make_root()
    try:
        file_transfer.FileTransferApp(root)
        root.withdraw()
        root.update_idletasks()

        geom = root.geometry()                   # 形如 "888x717+52+52"
        assert "x" in geom, "应用没有设置窗口尺寸，geometry=%r" % geom
        size = geom.split("+")[0]
        win_w, win_h = (int(x) for x in size.split("x"))

        need_w, need_h = root.winfo_reqwidth(), root.winfo_reqheight()
        assert need_h <= win_h, (
            "界面内容比窗口还高：需要 %d px，窗口只有 %d px。\n"
            "缺口会全部由唯一带 weight=1 的日志区承担 —— 用户看到的是"
            "「日志只剩两三行」，而不是任何一条报错。" % (need_h, win_h))
        assert need_w <= win_w, (
            "界面内容比窗口还宽：需要 %d px，窗口只有 %d px（窗口不可缩放）。"
            % (need_w, win_w))
    finally:
        try:
            root.destroy()
        except Exception:                        # noqa: BLE001
            pass
