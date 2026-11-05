# -*- coding: utf-8 -*-
"""判据：FTPS 的**握手顺序**与**自签名策略**。

【为什么只能靠单测】测试环境没有真的 FTPS 服务器。但真正会出事的地方不是"能不能连上"，
而是两件**不会有任何报错**的事：

    ① 顺序错：先 `login()` 再 `auth()` ⇒ 用户名口令走了**明文**，之后一切正常；
    ② 漏 `prot_p()`：控制通道加密了、**文件内容还是明文**传的，用户以为"已加密"。

这两种错都不会抛异常、日志里也看不出来，只有抓包能发现。所以用一个记录调用的假对象
把顺序钉死 —— 这是本项目里"判据替代真机"的典型用法。

自签名那条守的是策略：勾了 = 主动关掉证书校验（`CERT_NONE`），不勾 = 必须验。
"""
import ssl

import pytest

import config
import ftp_client


class FakeSock(object):
    """冒充 `ssl.SSLSocket` 之外的东西：`secure_session` 用它判"要不要 auth"。"""

    family = 2


class FakeFTP(object):
    """只记录调用顺序，不连任何东西。"""

    def __init__(self, already_tls=False):
        self.calls = []
        self.sock = object() if already_tls else FakeSock()

    def auth(self):
        self.calls.append("auth")

    def login(self, user, password):
        self.calls.append("login")

    def prot_p(self):
        self.calls.append("prot_p")


def test_explicit_order_is_auth_then_login_then_prot_p():
    """顺序即安全语义：密码必须在 auth() **之后**才发出去。"""
    ftp = FakeFTP()
    ftp_client.secure_session(ftp, "explicit", "h", "u", "p")
    assert ftp.calls == ["auth", "login", "prot_p"]


def test_explicit_skips_auth_when_already_tls():
    """已经握过手的不重复 AUTH（服务器会回 503，然后整条连接废掉）。"""
    ftp = FakeFTP(already_tls=True)
    ftp.sock = _FakeSSLSocket()
    ftp_client.secure_session(ftp, "explicit", "h", "u", "p")
    assert ftp.calls == ["login", "prot_p"]


class _FakeSSLSocket(ssl.SSLSocket):
    """只用于 `isinstance(x, ssl.SSLSocket)` 判定 —— 不构造真连接。"""

    def __init__(self):                     # noqa: D107 —— 不调父类构造
        pass


def test_implicit_without_tls_socket_is_refused():
    """隐式模式却没握手 = 调用方拿错了类，必须**响亮失败**而不是静默明文登录。"""
    with pytest.raises(ValueError):
        ftp_client.secure_session(FakeFTP(), "implicit", "h", "u", "p")


def test_class_selection():
    assert ftp_client.ftp_class_for("explicit") is __import__("ftplib").FTP_TLS
    assert ftp_client.ftp_class_for("implicit") is ftp_client.ImplicitFTP_TLS
    assert ftp_client.ftp_class_for("none") is None
    assert ftp_client.ftp_class_for(None) is None
    assert ftp_client.ftp_class_for("bogus") is None       # 认不出 ⇒ 退回明文（保守）


def test_tls_context_verifies_by_default():
    ctx = ftp_client.build_tls_context(allow_selfsigned=False)
    assert ctx.verify_mode == ssl.CERT_REQUIRED
    assert ctx.check_hostname is True


def test_tls_context_accepts_selfsigned_when_asked():
    ctx = ftp_client.build_tls_context(allow_selfsigned=True)
    assert ctx.verify_mode == ssl.CERT_NONE
    assert ctx.check_hostname is False


def test_implicit_class_wraps_control_socket():
    """隐式模式的关键动作：connect 之后控制连接被包起来（否则后续全是明文）。"""
    assert issubclass(ftp_client.ImplicitFTP_TLS, __import__("ftplib").FTP_TLS)
    assert "wrap_socket" in __import__("inspect").getsource(
        ftp_client.ImplicitFTP_TLS.connect)


# ---------------------------------------------------------------- 配置层
def test_profile_defaults_to_plaintext():
    """老配置（没有这两个键）必须**逐字节**还是明文 —— 升级不能偷偷改行为。"""
    p = config._clean_profile({"name": "a", "host": "h"})
    assert p["tls_mode"] == "none"
    assert p["allow_selfsigned"] is False


def test_profile_roundtrip_tls_fields():
    p = config._clean_profile({"name": "b", "host": "h", "port": 21,
                               "tls_mode": "explicit", "allow_selfsigned": True})
    assert p["tls_mode"] == "explicit" and p["allow_selfsigned"] is True


def test_profile_rejects_unknown_tls_mode():
    """拼错的值必须回落到明文**而不是**抛异常 —— 但回落是显式的，记在这里。"""
    for bad in ("tls", "Explicit", "", None, 123):
        assert config._clean_profile({"name": "c", "host": "h", "tls_mode": bad}
                                     )["tls_mode"] == "none"


def test_helper_passes_tls_settings_through():
    ftp = ftp_client.FTPHelper("h", "u", "p", 21, timeout=5,
                               tls_mode="implicit", allow_selfsigned=True)
    assert ftp.tls_mode == "implicit" and ftp.allow_selfsigned is True
    # 缺省（老调用点）仍是明文
    assert ftp_client.FTPHelper("h").tls_mode == "none"
