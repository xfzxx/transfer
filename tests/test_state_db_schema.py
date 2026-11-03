# -*- coding: utf-8 -*-
"""状态库 schema 与列序判据。

这两个判据守的都是**静默**缺陷：

· 索引没了 → 只是慢（20 万行的库每个源全表扫一遍），没人会报错；
· 列序错了 → `dict(zip(keys, row))` 会把 dst_path 当 src_size 比，
  症状是"每次全量重传"，而库、SQL、返回值看着全都正常。
"""
import sqlite3

import pytest

import config
import state_db


@pytest.fixture()
def conn(tmp_path):
    c = sqlite3.connect(str(tmp_path / "probe.db"))
    c.executescript(config._SCHEMA_DDL)
    yield c
    c.close()


def test_src_root_index_exists_and_is_used(conn):
    """P1 预取的取数是 WHERE src_root=?，必须是索引搜索而不是全表扫。"""
    names = [r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='files'")]
    assert "idx_files_src_root" in names

    plan = conn.execute(
        "EXPLAIN QUERY PLAN SELECT src_path FROM files WHERE src_root=?",
        ("X",)).fetchall()
    assert any("idx_files_src_root" in str(row) for row in plan), plan


def test_schema_is_idempotent_on_existing_db(conn):
    """老库升级路径：同一份 DDL 再跑一遍不许报错（IF NOT EXISTS 全覆盖）。"""
    conn.executescript(config._SCHEMA_DDL)
    conn.executescript(config._SCHEMA_DDL)


def test_file_state_columns_are_real_columns(conn):
    """`_FILE_STATE_COLS` 里每个名字都必须是 files 表真有的列。

    这是「SELECT 清单与 keys 元组各写一份」那个坑的守卫：现在两者都从这一个元组
    派生，但如果有人给元组加了个拼错的列名，SQL 会直接报 no such column ——
    真报错，算好的；怕的是两边各写一份时**不报错**。
    """
    actual = {r[1] for r in conn.execute("PRAGMA table_info(files)")}
    missing = set(state_db._FILE_STATE_COLS) - actual
    assert not missing, "这些列名在 files 表里不存在：%s" % sorted(missing)


def test_file_state_query_roundtrip(conn):
    """按 `_FILE_STATE_COLS` 取出来的每一列，都落在它自己那个键上。"""
    row = {name: None for name in state_db._FILE_STATE_COLS}
    row.update({
        "filename": "IM_0001.dcm",
        "task_id": 7,
        "src_path": r"D:\src\IM_0001.dcm",
        "src_size": 1234,
        "src_mtime": 1699999999,
        "mtime_precision": "ns",
        "dst_path": r"D:\dst\IM_0001.dcm",
        "dst_size": 1234,
        "status": "done",
        "src_deleted": 0,
        "attempts": 1,
        "last_error": None,
    })
    conn.execute(
        "INSERT INTO files (%s, src_root) VALUES (%s, ?)"
        % (state_db._FILE_STATE_COLS_SQL,
           ", ".join("?" * len(state_db._FILE_STATE_COLS))),
        [row[n] for n in state_db._FILE_STATE_COLS] + [r"D:\src"])
    conn.commit()

    cur = conn.execute(
        "SELECT " + state_db._FILE_STATE_COLS_SQL + " FROM files WHERE src_path=?",
        (row["src_path"],))
    got = dict(zip(state_db._FILE_STATE_COLS, cur.fetchone(), strict=True))
    assert got == row

    # src_size / dst_size 故意设成同值会掩盖错位，这里再用不同值判一次
    conn.execute("UPDATE files SET dst_size=999 WHERE src_path=?", (row["src_path"],))
    conn.commit()
    cur = conn.execute(
        "SELECT " + state_db._FILE_STATE_COLS_SQL + " FROM files WHERE src_path=?",
        (row["src_path"],))
    got = dict(zip(state_db._FILE_STATE_COLS, cur.fetchone(), strict=True))
    assert got["src_size"] == 1234 and got["dst_size"] == 999
