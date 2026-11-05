# -*- coding: utf-8 -*-
"""判据：FTP 下载续传的三条生死线（Mock，不连网络）。

用户点名的三个场景，每条都对应一种"静默出错"：

    ① 断网重试 → 必须 **REST n**（n = 上次已落盘字节数），否则白下
    ② 断网期间远端被改（mtime 变）→ 必须**丢弃 .part 从 0 重下**，
       否则拼出"两代内容拼接"的文件：大小对、内容错
    ③ 老配置没有旁挂记录 → 安全降级为从 0 重下（不许瞎猜偏移）

外加一条：续传完成后必须做**全文件**校验，不能只校验追加的那一截。
"""
import os

import ftp_client


def _part(tmp_path, size):
    p = tmp_path / "movie.dcm.part"
    p.write_bytes(b"x" * size)
    return str(p)


# ---------------------------------------------------------------- ① 正常续传
def test_resumes_from_part_length(tmp_path):
    part = _part(tmp_path, 400)
    ctx = {"remote": "/pub/a.dcm", "remote_mtime": 1000, "size": 1000}
    offset, why, drop = ftp_client.plan_download_resume(ctx, part, 1000, 1000)
    assert offset == 400 and not drop and "续传" in why


# ---------------------------------------------------------------- ② 远端被改
def test_remote_mtime_changed_discards_part(tmp_path):
    """**最关键的一条**：mtime 变了就必须丢 .part，即使字节数看起来能续。"""
    part = _part(tmp_path, 400)
    ctx = {"remote": "/pub/a.dcm", "remote_mtime": 1000, "size": 1000}
    offset, why, drop = ftp_client.plan_download_resume(ctx, part, 1000, 2000)
    assert offset == 0 and drop is True and "变更" in why


# ---------------------------------------------------------------- ③ 老配置
def test_missing_ctx_starts_from_zero(tmp_path):
    part = _part(tmp_path, 400)
    offset, why, drop = ftp_client.plan_download_resume(None, part, 1000, 1000)
    assert offset == 0 and not drop and "从 0" in why


def test_part_larger_than_remote_discards(tmp_path):
    """本地 .part 比远端还大 → 必然不是同一份，丢弃。"""
    part = _part(tmp_path, 1200)
    ctx = {"remote_mtime": 1000}
    offset, _why, drop = ftp_client.plan_download_resume(ctx, part, 1000, 1000)
    assert offset == 0 and drop is True


def test_equal_length_means_nothing_to_fetch(tmp_path):
    part = _part(tmp_path, 1000)
    ctx = {"remote_mtime": 1000}
    offset, why, drop = ftp_client.plan_download_resume(ctx, part, 1000, 1000)
    assert offset == 0 and not drop and "等长" in why


def test_missing_part_starts_from_zero(tmp_path):
    offset, _why, drop = ftp_client.plan_download_resume({}, str(tmp_path / "none.part"), 1000, 1)
    assert offset == 0 and not drop


def test_unknown_remote_size_still_resumes(tmp_path):
    """服务器不给 SIZE 时不阻断续传（只知道 mtime 一致就够）—— 但绝不丢弃。"""
    part = _part(tmp_path, 400)
    ctx = {"remote_mtime": 1000}
    offset, _why, drop = ftp_client.plan_download_resume(ctx, part, None, 1000)
    assert offset == 400 and not drop


# ---------------------------------------------------------------- 旁挂记录读写
def test_ctx_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setattr(ftp_client.config, "LOCAL_FTP_DOWNLOAD_CTX_DIR", str(tmp_path / "ctx"))
    local = str(tmp_path / "a.dcm.part")
    ftp_client.save_dl_ctx(local, "/pub/a.dcm", 1000, 1234, 400)
    got = ftp_client.load_dl_ctx(local)
    assert got["remote"] == "/pub/a.dcm" and got["transferred_bytes"] == 400
    assert got["remote_mtime"] == 1234


def test_ctx_load_survives_broken_json(tmp_path, monkeypatch):
    monkeypatch.setattr(ftp_client.config, "LOCAL_FTP_DOWNLOAD_CTX_DIR", str(tmp_path / "ctx"))
    local = str(tmp_path / "b.dcm.part")
    path = ftp_client._dl_ctx_path(local)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write("{坏了")
    assert ftp_client.load_dl_ctx(local) is None


# ---------------------------------------------------------------- 全文件校验
def test_full_size_check_catches_truncated_result(tmp_path, monkeypatch):
    """续传后整份大小不符 → 抛错。只校验"追加的那截"是抓不到这种错的。"""
    monkeypatch.setattr(ftp_client.config, "LOCAL_FTP_DOWNLOAD_CTX_DIR", str(tmp_path / "ctx"))

    class Fake(object):
        def __init__(self):
            self.rest = None

        def size(self, p):
            return 1000

        def retrbinary(self, cmd, cb, rest=0):
            self.rest = rest
            cb(b"y" * 100)                  # 只给了 100 字节（模拟被截断）

    h = ftp_client.FTPHelper("h", "u", "p", 21, timeout=5)
    h.ftp = Fake()
    h.resume_caps = {"size": True, "mdtm": True, "rest": True}
    local = str(tmp_path / "c.dcm.part")
    try:
        h.download_file("/pub/c.dcm", local)
        raise AssertionError("字节数不符时必须抛错")
    except OSError as e:
        assert "不符" in str(e)
