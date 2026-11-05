# -*- coding: utf-8 -*-
"""判据：`last_run.json` 的**容错读取**与前端可渲染性。

【为什么容错是重点】这份文件是给**显示**用的，而它有三种"不正常"的常态：
    ① 从来不存在（没跑过无头任务，绝大多数用户就是这样）；
    ② 写到一半断电 → JSON 截断；
    ③ 被手改坏 / 编码不对。
三种都只该让那行字消失，**不该让界面打不开**。所以这里的判据不是"能读出来"，
而是"读不出来时安静返回 None，且不抛"。
"""
import json
import os

import pytest

import config
import web_app


def setup_function(_fn):
    # 每个用例自己摆布这个文件
    if os.path.exists(config.LAST_RUN_FILE):
        os.remove(config.LAST_RUN_FILE)


def teardown_function(_fn):
    if os.path.exists(config.LAST_RUN_FILE):
        os.remove(config.LAST_RUN_FILE)


def _write(text):
    os.makedirs(os.path.dirname(config.LAST_RUN_FILE), exist_ok=True)
    with open(config.LAST_RUN_FILE, "w", encoding="utf-8") as f:
        f.write(text)


def test_missing_file_is_none():
    assert config.load_last_run() is None
    assert config.format_last_run(None) == ""


def test_broken_json_is_none_not_crash():
    _write('{"finished_at": "2026-11-05T02:00:00", "status": "ok"')   # 截断
    assert config.load_last_run() is None


def test_non_dict_json_is_none():
    _write('["不是字典"]')
    assert config.load_last_run() is None


def test_good_record_formats_into_one_line():
    _write(json.dumps({"finished_at": "2026-11-05T02:00:00", "status": "ok",
                       "copied": 500, "failed": 0}))
    d = config.load_last_run()
    text = config.format_last_run(d)
    assert "2026-11-05 02:00:00" in text and "成功" in text and "复制 500 个" in text


def test_failed_record_mentions_failures():
    _write(json.dumps({"finished_at": "2026-11-05T02:00:00", "status": "partial",
                       "copied": 12, "failed": 3}))
    text = config.format_last_run(config.load_last_run())
    assert "有失败" in text and "失败 3 个" in text


def test_unknown_status_yields_empty_text():
    """状态认不出来 → 空串 → 界面隐藏。不要显示半句话。"""
    assert config.format_last_run({"finished_at": "x", "status": "weird"}) == ""
    assert config.format_last_run({"status": "ok"}) == ""


def test_snapshot_carries_both_fields():
    _write(json.dumps({"finished_at": "2026-11-05T02:00:00", "status": "ok",
                       "copied": 7, "failed": 0}))
    f = web_app._last_run_fields()
    assert f["last_run"]["copied"] == 7
    assert "复制 7 个" in f["last_run_text"]


def test_snapshot_fields_degrade_on_broken_file():
    _write("{坏掉了")
    f = web_app._last_run_fields()
    assert f["last_run"] is None and f["last_run_text"] == ""


@pytest.mark.parametrize("payload", [None, {}, {"copied": "abc", "failed": None}])
def test_format_survives_odd_values(payload):
    """字段类型不对（手改过的文件）也不许抛 —— 最多少说一句。"""
    assert isinstance(config.format_last_run(payload), str)
