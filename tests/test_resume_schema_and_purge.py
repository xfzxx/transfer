# -*- coding: utf-8 -*-
"""判据：续传旁挂 JSON 的**老记录降级** + 远端 .part 清理。

【两条都很容易静默出错】
① JSON 加了 `transferred_bytes`，但如果读侧从此**只认字段**，那么升级前留下的
   老记录（没有这个键）会被当成"0 字节已传" ⇒ 每次都从头传，用户看到的是
   "升级后变慢了"，没有任何报错。
② 清理动作跑在别人的生产服务器上：判错年龄就删错文件。所以它必须"判不出年龄就不动"。
"""
import time

import ftp_client
import transfer_core


# ---------------------------------------------------------------- ① 旁挂 JSON
def _part(tmp_path, size):
    p = tmp_path / "x.dcm.part"
    p.write_bytes(b"x" * size)
    return str(p)


def test_old_record_without_field_falls_back_to_part_length(tmp_path):
    part = _part(tmp_path, 400)
    # 升级前写下的记录：没有 transferred_bytes
    assert transfer_core.resume_offset_from_ctx(
        {"src": "a", "dst": "b", "size": 1000}, part) == 400


def test_field_smaller_than_part_resends_the_tail(tmp_path):
    """字段记 300、磁盘上有 400 —— 取 300 续传：**多传 100 字节是安全的，跳过才是危险的**。

    这条的方向性很重要：两边冲突时，我们永远选择"可能重传一点"，而不是"可能跳过一段"。
    """
    part = _part(tmp_path, 400)
    assert transfer_core.resume_offset_from_ctx({"transferred_bytes": 300}, part) == 300


def test_field_larger_than_part_is_capped_down(tmp_path):
    """字段记 900、磁盘只有 400 —— 必须按 400 续，否则会跳过 500 字节没写的数据。"""
    part = _part(tmp_path, 400)
    assert transfer_core.resume_offset_from_ctx({"transferred_bytes": 900}, part) == 400


def test_bad_field_values_fall_back(tmp_path):
    part = _part(tmp_path, 400)
    for bad in (None, "abc", -1, {}, []):
        assert transfer_core.resume_offset_from_ctx({"transferred_bytes": bad}, part) == 400


def test_missing_part_means_zero(tmp_path):
    assert transfer_core.resume_offset_from_ctx({"transferred_bytes": 400},
                                                str(tmp_path / "nope.part")) == 0


def test_empty_part_means_zero(tmp_path):
    assert transfer_core.resume_offset_from_ctx({"transferred_bytes": 400},
                                                _part(tmp_path, 0)) == 0


# ---------------------------------------------------------------- ② .part 清理
class FakeFTPServer(object):
    def __init__(self, files):
        # files: {name: age_days}
        self.files = dict(files)
        self.deleted = []

    def nlst(self, d):
        return ["/pub/" + n for n in self.files]

    def sendcmd(self, cmd):
        name = cmd.split(" ", 1)[1].split("/")[-1]
        age = self.files[name]
        ts = time.gmtime(time.time() - age * 86400)
        return "213 " + time.strftime("%Y%m%d%H%M%S", ts)

    def delete(self, path):
        self.deleted.append(path)
        self.files.pop(path.split("/")[-1], None)


def test_purges_parts_older_than_7_days():
    srv = FakeFTPServer({"a.dcm.part": 8, "b.dcm.part": 9, "c.dcm": 30, "d.dcm.part": 1})
    n = ftp_client.purge_stale_parts(srv, "/pub")
    assert n == 2, srv.deleted
    assert sorted(x.split("/")[-1] for x in srv.deleted) == ["a.dcm.part", "b.dcm.part"]
    assert "c.dcm" not in srv.deleted, "非 .part 的文件绝不能碰"
    assert "d.dcm.part" not in srv.deleted, "1 天的不该被删"


class NoMdtm(FakeFTPServer):
    def sendcmd(self, cmd):
        raise Exception("502 MDTM not implemented")


def test_no_mdtm_means_delete_nothing():
    """判不出年龄就一条都不删 —— 宁可留垃圾，也不要凭猜删别人的文件。"""
    srv = NoMdtm({"a.dcm.part": 99})
    assert ftp_client.purge_stale_parts(srv, "/pub") == 0
    assert srv.deleted == []


def test_nlst_failure_is_silent():
    class Boom(object):
        def nlst(self, d):
            raise OSError("connection reset")

    assert ftp_client.purge_stale_parts(Boom(), "/pub") == 0
