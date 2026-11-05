# -*- coding: utf-8 -*-
"""判据：加密通道下的续传能力探测与降级。

【守的是什么】`SIZE` / `MDTM` / `REST` 在 FTP 里都是**可选命令**，FTPS 下更参差。
不支持的时候不会有任何报错 —— 只会**静默整文件重传**。20 万文件的夜里备份，
慢一倍而没人知道，这种缺陷只能靠判据钉住：

    ① 服务器拒绝 REST → 必须留下明确警告（需求原文，不许静默）；
    ② 远端大小与本地 .part 不符 → 必须判"不能续"，宁可重传也不许拼出错误的文件。
"""
import ftplib

import pytest

import ftp_client


class FakeFTP(object):
    """按命令前缀决定"接受还是拒绝"，模拟三种服务器。"""

    def __init__(self, reject=()):
        self.reject = set(reject)
        self.cmds = []

    def sendcmd(self, cmd):
        self.cmds.append(cmd)
        verb = cmd.split(" ", 1)[0].upper()
        if verb in self.reject:
            raise ftplib.error_perm("502 %s not implemented" % verb)
        return "350 ok"


def test_all_supported():
    logs = []
    caps = ftp_client.probe_resume_caps(FakeFTP(), log=logs.append)
    assert caps == {"size": True, "mdtm": True, "rest": True}
    assert logs == []


def test_rest_rejected_warns_explicitly():
    """需求原文那句警告必须真的打出来 —— 这是本条判据的全部意义。"""
    logs = []
    caps = ftp_client.probe_resume_caps(FakeFTP(reject={"REST"}), log=logs.append)
    assert caps["rest"] is False
    assert any("断点续传" in s and "从头开始" in s for s in logs), logs


def test_size_rejected_warns():
    logs = []
    caps = ftp_client.probe_resume_caps(FakeFTP(reject={"SIZE"}), log=logs.append)
    assert caps["size"] is False
    assert any("SIZE" in s for s in logs), logs


def test_probe_survives_connection_errors():
    """连 sendcmd 都炸（连接断了）也不能抛 —— 探测只是"尽量知道"。"""
    class Boom(object):
        def sendcmd(self, cmd):
            raise OSError("connection reset")

    assert ftp_client.probe_resume_caps(Boom()) == {"size": False, "mdtm": False, "rest": False}


@pytest.mark.parametrize("remote,part,ok", [
    (1000, 400, True),      # 远端更长：正常续传
    (400, 400, False),      # 已完整：不需要续
    (300, 400, False),      # **远端更短 = 不一致**：必须重传，否则拼出错误文件
    (None, 400, False),
    ("abc", 400, False),
    (-1, 400, False),
])
def test_verify_resume_point(remote, part, ok):
    got, reason = ftp_client.verify_resume_point(remote, part)
    assert got is ok, reason


def test_verify_explains_why():
    """原因要能直接进日志 —— 只回 False 没有可诊断性。"""
    _ok, reason = ftp_client.verify_resume_point(300, 400)
    assert "从头开始" in reason
