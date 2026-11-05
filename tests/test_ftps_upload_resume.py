# -*- coding: utf-8 -*-
"""判据：上传路径的**续传决策**与降级文案（Mock 服务器，不连网络）。

【这块为什么值得单独判】它决定"重传一整个文件"还是"从 300 MB 处接着传"。
判错的代价不对称：
  · 该续却从头 → 只是慢；
  · **不该续却续** → 远端出现一个"大小对、内容错"的文件，没有任何报错，
    下游 PACS 会把它当完整片子读走。所以所有拿不准的分支都必须回 0（从头）。
"""
import ftp_client


class FakeServer(object):
    def __init__(self, part_len=None, caps=None, size_raises=False):
        self.part_len = part_len
        self.caps = caps if caps is not None else {"size": True, "mdtm": True, "rest": True}
        self.size_raises = size_raises
        self.calls = []

    def size(self, path):
        self.calls.append(("size", path))
        if self.size_raises:
            raise OSError("boom")
        return self.part_len

    def storbinary(self, cmd, fp, rest=0):
        self.calls.append(("stor", cmd, rest, fp.tell()))
        return "226 ok"


def _helper(server, tmp_path, local_bytes=1000):
    h = ftp_client.FTPHelper("h", "u", "p", 21, timeout=5)
    h.ftp = server
    h.resume_caps = server.caps
    f = tmp_path / "x.dcm"
    f.write_bytes(b"x" * local_bytes)
    return h, str(f)


def test_resumes_from_partial_part(tmp_path):
    h, local = _helper(FakeServer(part_len=400), tmp_path)
    off, why = h._plan_upload_resume("/r/x.dcm.part", local)
    assert off == 400 and "续传" in why


def test_restarts_when_remote_part_longer_than_source(tmp_path):
    """**最关键的一条**：远端 .part 比源文件还长 ⇒ 必然是别人的/传错的 ⇒ 从头。"""
    h, local = _helper(FakeServer(part_len=5000), tmp_path)
    off, why = h._plan_upload_resume("/r/x.dcm.part", local)
    assert off == 0 and "不一致" in why and "从头" in why


def test_restarts_when_size_unsupported(tmp_path):
    h, local = _helper(FakeServer(part_len=400, caps={"size": False, "rest": True}), tmp_path)
    off, why = h._plan_upload_resume("/r/x.dcm.part", local)
    assert off == 0 and "SIZE" in why


def test_restarts_when_rest_refused(tmp_path):
    """服务器有 SIZE 但拒 REST：知道能续，却没法续 —— 也必须说明白。"""
    h, local = _helper(FakeServer(part_len=400, caps={"size": True, "rest": False}), tmp_path)
    off, why = h._plan_upload_resume("/r/x.dcm.part", local)
    assert off == 0 and "REST" in why


def test_restarts_when_part_missing(tmp_path):
    h, local = _helper(FakeServer(part_len=None), tmp_path)
    off, why = h._plan_upload_resume("/r/x.dcm.part", local)
    assert off == 0 and "从头" in why


def test_restarts_when_size_call_raises(tmp_path):
    h, local = _helper(FakeServer(size_raises=True), tmp_path)
    off, _why = h._plan_upload_resume("/r/x.dcm.part", local)
    assert off == 0


def test_upload_direct_seeks_and_passes_rest(tmp_path):
    """`rest>0` 时既要 seek 文件头，也要把 rest 传给 storbinary（否则 STOR 从 0 覆盖）。"""
    srv = FakeServer()
    h, local = _helper(srv, tmp_path)
    h._upload_direct(local, "/r/x.dcm.part", rest=400)
    kind, _cmd, rest, told = srv.calls[-1]
    assert kind == "stor" and rest == 400 and told == 400


def test_upload_direct_without_rest_starts_at_zero(tmp_path):
    srv = FakeServer()
    h, _local = _helper(srv, tmp_path)
    h._upload_direct(_local, "/r/x.dcm.part")
    _kind, _cmd, rest, told = srv.calls[-1]
    assert rest == 0 and told == 0


def test_empty_part_restarts(tmp_path):
    h, local = _helper(FakeServer(part_len=0), tmp_path)
    off, why = h._plan_upload_resume("/r/x.dcm.part", local)
    assert off == 0 and "空文件" in why


def test_equal_length_part_means_nothing_to_send(tmp_path):
    """等长 = 上次其实传完了（只是没来得及改名）→ 偏移 0，但原因要说清楚。"""
    h, local = _helper(FakeServer(part_len=1000), tmp_path)
    off, why = h._plan_upload_resume("/r/x.dcm.part", local)
    assert off == 0 and "等长" in why
