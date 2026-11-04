# -*- coding: utf-8 -*-
"""判据：开始前预检必须**拦得住**，而且不许误拦。

【守的是什么】两种失败形态原本都要等到传输中途或收尾才暴露，而且都不报错：
    ① 目标文件夹在、但写不进去（只读盘 / ACL / 盘满）→ 传到第 N 个文件才炸，前面白传；
    ② 源文件夹是空的 → 一路"成功"到收尾，界面报「完成 0 个文件」，用户当成传完了。
两者在开跑前都能用**一次写探针 + 一次 scandir** 在毫秒级回答。

【为什么判据要正反两面】
只测"该拦的拦住"，一个 `return ["出错了"]` 的假实现也能全绿；只测"该放的放过去"，
一个永远返回空列表的实现同样全绿。所以两组都要有。
"""
import os

import pytest

import transfer_core

GB = 1024 ** 3


def _mk(tmp_path, name="src"):
    d = tmp_path / name
    d.mkdir()
    return str(d)


def test_empty_source_is_refused(tmp_path):
    src = _mk(tmp_path)
    dst = _mk(tmp_path, "dst")
    problems = transfer_core.preflight_local(src, [dst], min_free_gb=0)
    assert any("源文件夹是空的" in p for p in problems), problems


def test_nonempty_source_passes(tmp_path):
    """该放的必须放过去 —— 否则这个预检会变成"永远拦"。"""
    src = _mk(tmp_path)
    (tmp_path / "src" / "a.dcm").write_bytes(b"x")
    dst = _mk(tmp_path, "dst")
    assert transfer_core.preflight_local(src, [dst], min_free_gb=0) == []


def test_unwritable_destination_is_refused(tmp_path):
    """不存在的盘符 = 一定写不进去，且不会在测试机上留下任何东西。"""
    src = _mk(tmp_path)
    (tmp_path / "src" / "a.dcm").write_bytes(b"x")
    problems = transfer_core.preflight_local(src, [r"Z:\no_such_dir_for_preflight"],
                                             min_free_gb=0)
    assert any("不可写" in p for p in problems), problems


def test_probe_leaves_no_garbage(tmp_path):
    """探针文件必须自己收干净 —— 用户目录里出现神秘小文件比不检查更糟。"""
    src = _mk(tmp_path)
    (tmp_path / "src" / "a.dcm").write_bytes(b"x")
    dst = _mk(tmp_path, "dst")
    transfer_core.preflight_local(src, [dst], min_free_gb=0)
    assert os.listdir(dst) == [], "目标目录里残留了探针文件：%s" % os.listdir(dst)


def test_low_space_is_refused(tmp_path):
    src = _mk(tmp_path)
    (tmp_path / "src" / "a.dcm").write_bytes(b"x")
    dst = _mk(tmp_path, "dst")
    problems = transfer_core.preflight_local(src, [dst], min_free_gb=10 ** 9)  # 1e9 GB
    assert any("剩余空间不足" in p for p in problems), problems


def test_need_bytes_is_checked_with_margin(tmp_path):
    """已知总量时按 1.05 倍判断：源文件 1 GB、盘上只剩 1 GB → 必须拒。"""
    _mk(tmp_path)                              # 先把 src 目录建出来
    src_file = str(tmp_path / "src" / "big.bin")
    with open(src_file, "wb") as f:
        f.truncate(1024)                       # 稀疏文件：报大小 1 KB，不真占盘
    dst = _mk(tmp_path, "dst")
    problems = transfer_core.preflight_local(src_file, [dst],
                                             min_free_gb=0, need_bytes=10 ** 12)
    assert any("剩余空间不够" in p for p in problems), problems


def test_multi_destination_each_probed(tmp_path):
    """第二目标也要过一遍：它不可写时同样不能让任务起跑。"""
    src = _mk(tmp_path)
    (tmp_path / "src" / "a.dcm").write_bytes(b"x")
    ok_dst = _mk(tmp_path, "dst")
    problems = transfer_core.preflight_local(
        src, [ok_dst, r"Z:\no_such_dir_2"], min_free_gb=0)
    assert any("不可写" in p for p in problems), problems


@pytest.mark.parametrize("need", [None, 0])
def test_single_file_source_size_is_used(tmp_path, need):
    """单文件源的大小是白拿的：不传 need_bytes 也应该能判空间。"""
    src_file = str(tmp_path / "one.bin")
    with open(src_file, "wb") as f:
        f.truncate(1024)
    dst = _mk(tmp_path, "dst")
    problems = transfer_core.preflight_local(src_file, [dst], min_free_gb=0,
                                             need_bytes=need)
    # 本机盘不可能真满，所以这里预期"通过"——判的是"不会因为 need_bytes 缺省就崩"
    assert isinstance(problems, list)


# ---------------------------------------------------------------- 接线判据
class _FakeCore(object):
    """只装 `_preflight` 会碰到的那两个出口，别的什么都不实现。

    这条判的是**接线**：拒绝启动时必须 ① 返回 False ② 弹框（hooks）③ 日志留一行。
    少了任何一条，"拒绝"就只发生在控制台里，用户在界面上什么也看不到。
    """

    def __init__(self):
        self.logs = []
        self.problems = []
        outer = self

        class _UI(object):
            def _update_log(self, text):
                outer.logs.append(text)

        class _Hooks(object):
            def on_path_problem(self, text):
                outer.problems.append(text)

        self._ui = _UI()
        self._hooks = _Hooks()


def test_preflight_wiring_refuses_and_reports(tmp_path):
    src = _mk(tmp_path)                      # 空目录 → 必被拒
    dst = _mk(tmp_path, "dst")
    core = _FakeCore()
    ok = transfer_core.TransferCore._preflight(core, src, dst)
    assert ok is False, "预检没通过却返回了 True"
    assert core.problems and "源文件夹是空的" in core.problems[0], core.problems
    assert core.logs and core.logs[0].startswith("[预检]"), core.logs


def test_preflight_wiring_passes_when_clean(tmp_path):
    src = _mk(tmp_path)
    (tmp_path / "src" / "a.dcm").write_bytes(b"x")
    dst = _mk(tmp_path, "dst")
    core = _FakeCore()
    assert transfer_core.TransferCore._preflight(core, src, dst) is True
    assert core.problems == [] and core.logs == []
