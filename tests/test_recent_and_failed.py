# -*- coding: utf-8 -*-
"""判据：最近路径的去重/截断，以及"最近失败"查询的只读与降级。

【为什么专门判这两条】
· 最近路径是**用户可见的顺序**：写错一个 `insert` 位置，界面上"最近用过的"
  就变成"最早用过的"，而这种错不会报错、只会让人觉得"这功能没用"。
· 最近失败是**只读查询**：它跑在请求线程里，一旦误用了写连接就会与 db-writer
  抢锁，症状是偶发的 `database is locked` —— 又慢又难复现。所以判据要钉住
  "库不在时安静返回空、且不去碰读连接"。
"""
import config
import state_db


def setup_function(_fn):
    config.RECENT_PATHS["source"] = []
    config.RECENT_PATHS["dest"] = []


def test_recent_path_moves_to_front_without_duplicating():
    config.record_recent_path("source", r"D:\data\a")
    config.record_recent_path("source", r"D:\data\b")
    config.record_recent_path("source", r"D:\data\a")     # 旧的重提一次
    assert config.RECENT_PATHS["source"] == [r"D:\data\a", r"D:\data\b"]


def test_recent_path_dedupes_case_insensitively():
    config.record_recent_path("source", r"D:\Data\A")
    config.record_recent_path("source", r"d:\data\a")     # Windows 上同一处
    assert len(config.RECENT_PATHS["source"]) == 1


def test_recent_path_caps_at_five():
    for i in range(8):
        config.record_recent_path("dest", r"D:\dst%d" % i)
    lst = config.RECENT_PATHS["dest"]
    assert len(lst) == config.RECENT_PATHS_MAX == 5
    assert lst[0] == r"D:\dst7", "最近用的必须在最前"


def test_recent_path_ignores_blank_and_unknown_kind():
    config.record_recent_path("source", "   ")
    config.record_recent_path("source", None)
    config.record_recent_path("nope", r"D:\x")
    assert config.RECENT_PATHS["source"] == [] and config.RECENT_PATHS["dest"] == []


class _FakeDB(object):
    """库文件不存在 —— 这时**连读连接都不该开**。"""

    db_path = "Z:/definitely_not_exists/preflight.db"

    def _read(self):
        raise AssertionError("库不存在时不该去开读连接")


def test_recent_failed_returns_empty_when_db_missing():
    assert state_db.TransferStateDB.recent_failed(_FakeDB()) == []


def test_recent_failed_clamps_limit():
    """limit 是外部（HTTP 查询串）给进来的，越界要被夹住而不是拼进 SQL 报错。"""
    for bad in (0, -5, 10 ** 9, "abc", None):
        try:
            state_db.TransferStateDB.recent_failed(_FakeDB(), bad)
        except AssertionError:
            raise                                   # 真去开连接了才算失败
        except Exception as e:                      # noqa: BLE001
            raise AssertionError("limit=%r 不该抛异常：%s" % (bad, e)) from e
