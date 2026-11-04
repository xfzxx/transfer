# -*- coding: utf-8 -*-
"""判据：扫描期文案的**尺寸**与**格式**。

【为什么文案也要判据】速度标签是**定宽**控件（`pack_propagate(False)`，预算 209px）。
超出部分不是换行、不是省略号，是**静默裁掉** —— 用户看到半句话，而日志里一切正常。
项目文档里记过这个坑（"1023.9 MB/s · 已扫 999999 个目录 · 已传 999999 个文件"
量出来 300+px），所以这条判据守的是**物理宽度**，不是文字内容好不好看。
"""
import pytest

import ui_channel

BUDGET_PX = 209          # 速度标签的实测预算，见 ui_channel 构造处的说明


def test_fmt_size_units():
    assert ui_channel.UIChannel._fmt_size(0) == "0 B"
    assert ui_channel.UIChannel._fmt_size(12) == "12 B"
    assert ui_channel.UIChannel._fmt_size(1536) == "1.5 KB"
    assert ui_channel.UIChannel._fmt_size(1024 ** 2 * 456) == "456.0 MB"
    assert ui_channel.UIChannel._fmt_size(1024 ** 3 * 12) == "12.0 GB"
    assert ui_channel.UIChannel._fmt_size(1024 ** 4 * 2) == "2.0 TB"
    # None / 负数不该炸（扫描期第一次可能还没有值）
    assert ui_channel.UIChannel._fmt_size(None) == "0 B"


@pytest.fixture(scope="module")
def measure():
    """用**真实字体**量像素。起不了 Tk 就跳过（CI 正常能起）。"""
    tk = pytest.importorskip("tkinter")
    from tkinter import font as tkfont
    try:
        root = tk.Tk()
    except Exception as e:                       # noqa: BLE001
        pytest.skip("这个环境起不了 Tk：%s" % e)
    root.withdraw()
    f = tkfont.Font(family="Microsoft YaHei UI", size=9)
    yield f.measure
    root.destroy()


def test_scan_text_fits_budget(measure):
    """扫描期最坏情况：六位文件数 + 三位 GB。"""
    worst = "已发现 999999 个 / 999.9 GB"
    assert measure(worst) <= BUDGET_PX, "%d px 超预算：%s" % (measure(worst), worst)


def test_transfer_text_fits_budget(measure):
    """传送期最坏情况：四位数速率 + 六位文件数。

    【这一档为什么不拼体积】`1023.9 MB/s · 已传 999999 个 / 999.9 GB` 实测 **235px**，
    超预算 26px。既然塞不下，就把体积留在扫描档 —— 而不是硬塞进定宽标签被静默裁掉。
    """
    worst = "1023.9 MB/s · 已传 999999 个文件"
    w = measure(worst)
    assert w <= BUDGET_PX, "%d px 超预算（预算 %d）：%s" % (w, BUDGET_PX, worst)


def test_scan_text_shape():
    """文案句式钉住：必须能一眼看出"已发现"多少，而不是只有目录数。"""
    import inspect
    src = inspect.getsource(ui_channel.UIChannel._speed_text)
    assert "已发现 %d 个" in src, "扫描档文案里没有「已发现 N 个」"
    assert "_fmt_size" in src, "扫描档没有拼上体积"
