# -*- coding: utf-8 -*-
r"""C3 / C4 宿主自检 —— 「这台机器能不能跑 pywebview（C3）」的唯一判据。

================================================================================
【它回答什么问题】
================================================================================
`113 §2` 的双模裁定：开机自检「.NET FW ≥ 4.6.2 **且** WebView2 Runtime ≥ 86.0.622.0」
⇒ 满足 = **C3**（pywebview，无地址栏）；任一不满足 = **C4**（本地 HTTP + 系统浏览器）。
**自动切换，不写死任一档。** 判据来源不是猜的：`pywebview/platforms/winforms.py::
_is_chromium()` 的源码（步 0 实测，见 `107` §八）。

为什么要这么严：C3 的界面由 WebView2 承载；一旦 WebView2 不在，pywebview 会**静默
退回 mshtml（IE11 内核）**，而 mshtml **读不了本地文件** ⇒ 界面直接废掉。
`root.withdraw()` 之类的兜底都救不了 —— 所以宁可在开机时判成 C4。

================================================================================
【三条坑 —— 每一条都有实测来源。改这个文件之前先读完这一段。】
================================================================================
1. **`pv` 是字符串，必须按 `.` 切成数字逐段比。**
   WebView2 的版本在注册表里是 `REG_SZ`，形如 `"155.0.4283.45"`。
   直接做**字符串比较**会给出**错答案**：

       "99.0.0" > "155.0.4283.45"        ← 字典序下为**真**，版本语义上是**假**

   ⇒ 一台「WebView2 只有 99」的机器会被误判成 C3，然后在运行期以
     「界面废掉」的形态爆炸。**本模块任何地方都不许对 `pv` 做字符串比较**，
     一律走 `_ver_satisfies()`。

2. **64 位 Python 在 64 位系统上读 `HKLM\SOFTWARE\Microsoft\...` 会被重定向。**
   ⇒ `WOW6432Node` 那一支**必须先试**（本机实测命中的正是它）。
   反过来，读不到**不等于**没装 ⇒ 是"三支依次试、取第一个读到的"，
   **不是**"第一支没有就判 C4"。

3. **版本串段数可能不齐**（厂商可能给 `"86.0"`）。
   ⇒ 短段补零，**不许抛异常**。
   抛异常的后果最坏：`detect()` 抛出去 ⇒ 宿主起不来 ⇒ 用户看到的是"崩"，
   而不是"退回 C4"。**"检测失败"与"环境不支持"必须走同一条路，都返回 `"c4"`。**

================================================================================
【本模块的纪律】
================================================================================
* **纯标准库**（`winreg` + `sys`）。**不 import** `config` / `state_db` /
  `transfer_core` / 任何业务模块 —— 宿主层**不认识业务**。
* **只读注册表**：不写、不装、不联网。
* **不读 `config` 里的任何业务常量**（`113 §3.2.3` 已裁定 N3 的空闲超时常数
  **必须**与业务心跳无关；同一条纪律适用于本模块）。
* 装配进启动流程是**步 4** 的事 —— 现在它与 `web_host.py` 同状态：
  **在盘上、被单测覆盖、但没有任何生产代码 import 它** ⇒ 生产行为零变化。

================================================================================
【怎么验 —— 不许只靠"本机跑一遍"】
================================================================================
本机（Win11 + `WOW6432Node` 命中）恰好是**能过**的那一侧。拿它当验据的话，
把上面第 2 条坑（`WOW6432Node` 那一支）整支删掉，本机仍然全绿。
⇒ 判据的正确性由**注入式单测**证明：`_checks/host_probe_selftest.py`（S1~S8 + 两条突变对照），
  把"读注册表"换成喂合成值的可调用对象。这是唯一能证伪上表的办法。

命令行自检（人工看一眼用）：
    python host_probe.py
"""
import sys

__all__ = ["detect", "report"]

# ---------------------------------------------------------------------------
# 注册表读取点（两个条件、四条 key_path）
# ---------------------------------------------------------------------------
# 条件 1：.NET Framework 4.x 的发布号（DWORD）。394802 = .NET FW 4.6.2。
NET_FW_KEY_PATH = r"HKLM\SOFTWARE\Microsoft\NET Framework Setup\NDP\v4\Full"
NET_FW_VALUE = "Release"
NET_FW_MIN_RELEASE = 394802

# 条件 2：WebView2 Runtime 的版本（String，三段数字）。
# 这个 GUID 是 Edge/WebView2 官方的**固定**客户端 id，不是随机值。
WEBVIEW2_CLIENT_ID = "{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}"
WEBVIEW2_VALUE = "pv"
WEBVIEW2_MIN_VERSION = "86.0.622.0"

# ⚠️ **顺序即语义**：WOW6432Node 那一支**必须在最前**（坑 2）。
#    `_probe` 式的"只试一支"会让本机以外的机器大面积错判。
WEBVIEW2_KEY_PATHS = (
    r"HKLM\SOFTWARE\WOW6432Node\Microsoft\EdgeUpdate\Clients\%s" % WEBVIEW2_CLIENT_ID,
    r"HKLM\SOFTWARE\Microsoft\EdgeUpdate\Clients\%s" % WEBVIEW2_CLIENT_ID,
    r"HKCU\SOFTWARE\Microsoft\EdgeUpdate\Clients\%s" % WEBVIEW2_CLIENT_ID,
)

# 版本比较只取前 N 段（见坑 1：三段比）。
VER_SEGMENTS = 3

try:                                       # pragma: no cover - 平台分支
    import winreg
except ImportError:                        # 非 Windows：本模块会被判成"检测失败"
    winreg = None

_HIVES = None
if winreg is not None:
    _HIVES = {"HKLM": winreg.HKEY_LOCAL_MACHINE,
              "HKCU": winreg.HKEY_CURRENT_USER}


# ---------------------------------------------------------------------------
# 默认读取器（可注入的同一形态：reader(key_path, value_name) -> 值 或 None）
# ---------------------------------------------------------------------------
def _default_reader(key_path, value_name):
    r"""真读注册表。**读不到一律返回 `None`，绝不抛异常。**

    接口形态与单测注入的假读取器**完全一致**（`(key_path, value_name) -> 值 | None`），
    所以 `detect(reader=...)` 换的只是这一个函数，其余判据逐字不变。

    【为什么把 hive 前缀写在 key_path 里而不是另开一个参数】
      单测要按"哪个 key 有值"造夹具；写成一整条路径 ⇒ 夹具就是一张
      `{(key_path, value_name): 值}` 的字典，**不引入第二套寻址语法**。
    """
    if winreg is None:
        return None
    hive_name, sep, sub = key_path.partition("\\")
    hive = _HIVES.get(hive_name) if _HIVES else None
    if not sep or hive is None:
        return None
    try:
        with winreg.OpenKey(hive, sub, 0, winreg.KEY_READ) as k:
            value, _kind = winreg.QueryValueEx(k, value_name)
            return value
    except Exception:                      # noqa: BLE001 - 读不到就是"读不到"
        return None


# ---------------------------------------------------------------------------
# 判据（两个纯函数，各自可单独证伪）
# ---------------------------------------------------------------------------
def _segments(value, count=VER_SEGMENTS):
    r"""把一个版本串切成 `count` 个整数段；切不出来返回 `None`（**不抛**）。

    坑 1（逐段数字比）与坑 3（段数不齐）都落在这里：
      · `"155.0.4283.45"` → `(155, 0, 4283)`（第 4 段被截掉，与 pywebview 的三段比一致）
      · `"86.0"`          → `(86, 0, 0)`   —— **补零**，不是报错
      · `"86.0.622a"`     → `None`         —— 段里混了非数字 ⇒ 判"不可比较"
    """
    try:
        raw = str(value).strip()
    except Exception:                      # noqa: BLE001
        return None
    if not raw:
        return None
    parts = raw.split(".")
    out = []
    for i in range(count):
        if i < len(parts):
            seg = parts[i].strip()
            if not seg.isdigit():
                return None
            out.append(int(seg))
        else:
            out.append(0)
    return tuple(out)


def _ver_satisfies(value, minimum):
    """`value >= minimum` 按**数字段**判。任一侧切不出来 ⇒ `False`（不是抛异常）。"""
    a = _segments(value)
    b = _segments(minimum)
    if a is None or b is None:
        return False
    return a >= b


def _int_at_least(value, minimum):
    """DWORD 型判据（`.NET FW` 的 `Release`）。读不到/不是整数 ⇒ `False`。"""
    if value is None:
        return False
    try:
        return int(str(value).strip()) >= int(minimum)
    except Exception:                      # noqa: BLE001
        return False


# ---------------------------------------------------------------------------
# 公开接口
# ---------------------------------------------------------------------------
def detect(reader=None):
    r"""返回 `"c3"`（pywebview 可用）或 `"c4"`（走系统浏览器）。

    `reader` 可注入（单测用），形态 `(key_path, value_name) -> 值 | None`；
    不给则用 `_default_reader`（真 `winreg`）。

    **任何异常 ⇒ 返回 `"c4"`** —— "检测失败"与"环境不支持"走同一条路。
    这一条不是防御性编程的习惯动作，是判据的一部分：抛异常会让用户看到"崩"，
    而"崩"与"退回 C4"对用户是两件事（后者是设计好的降级，前者是故障）。
    """
    read = _default_reader if reader is None else reader
    try:
        if not _int_at_least(read(NET_FW_KEY_PATH, NET_FW_VALUE), NET_FW_MIN_RELEASE):
            return "c4"
        # 三支依次试，取第一个**读到**的（坑 2：顺序即语义）
        pv = None
        for key_path in WEBVIEW2_KEY_PATHS:
            pv = read(key_path, WEBVIEW2_VALUE)
            if pv is not None:
                break
        if not _ver_satisfies(pv, WEBVIEW2_MIN_VERSION):
            return "c4"
    except Exception:                      # noqa: BLE001 - 见 docstring 最后一段
        return "c4"
    return "c3"


def report(reader=None):
    r"""给人工看的逐条诊断（`python host_probe.py` 用它）。

    **它不是判据** —— 判据只有一个：`detect()`。这里多写的每一行都只是
    "为什么落到这个结论"，用于医院现场自查（`113 §2.1` 的那张表）。
    """
    read = _default_reader if reader is None else reader
    lines = []

    rel_raw = None
    try:
        rel_raw = read(NET_FW_KEY_PATH, NET_FW_VALUE)
    except Exception:                      # noqa: BLE001
        rel_raw = None
    ok_net = _int_at_least(rel_raw, NET_FW_MIN_RELEASE)
    lines.append("条件1 .NET FW Release = %s   (读点 %s)"
                 % (rel_raw, NET_FW_KEY_PATH))
    lines.append("       判据 ≥ %d ? %s" % (NET_FW_MIN_RELEASE, ok_net))

    pv_raw, hit = None, None
    for key_path in WEBVIEW2_KEY_PATHS:
        try:
            v = read(key_path, WEBVIEW2_VALUE)
        except Exception:                  # noqa: BLE001
            v = None
        if v is not None:
            pv_raw, hit = v, key_path
            break
    ok_wv = _ver_satisfies(pv_raw, WEBVIEW2_MIN_VERSION)
    lines.append("条件2 WebView2  pv = %s   (命中 %s)" % (pv_raw, hit))
    lines.append("       判据 ≥ %s ? %s（三段数字比，**不是**字符串比较）"
                 % (WEBVIEW2_MIN_VERSION, ok_wv))

    verdict = detect(reader)
    lines.append("=> 自检结论：%s（%s）"
                 % ("C3（pywebview）" if verdict == "c3" else "C4（系统浏览器）",
                    "两条都满足" if verdict == "c3" else "至少一条不满足"))
    lines.append("   平台：%s" % sys.platform)
    return "\n".join(lines)


if __name__ == "__main__":
    print(report())
    # 退出码只表达"哪一档"，不表达成败 —— 两档都是**设计好的合法状态**。
    # 判 1 会诱使人把"C4"当故障处理，而它是明确支持的一档（`113 §2.2`）。
    sys.exit(0)
