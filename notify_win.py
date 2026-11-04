# -*- coding: utf-8 -*-
r"""Windows 托盘通知气泡（**零第三方依赖**，只用 ctypes + 标准库）。

================================================================================
【为什么自己写，不用 win10toast / winrt】
================================================================================
那两个都要装包，而这个项目对依赖的态度是"能不加就不加"（requirements.txt 里只有
4 个直接依赖，每一个都有交代）。Shell_NotifyIcon 是 Win32 的老 API，
`ctypes` 就能调，Windows 10 上气泡行为稳定 —— 不值得为一句"任务完成了"引一个依赖。

================================================================================
【它必须是"哑"的：通知失败绝不能影响传输】
================================================================================
所有异常在这里吞掉，只返回 `bool`。理由：这是**锦上添花**的一条通道，
而它跑在任务收尾的路径上 —— 一次 `struct` 布局错误不该让"任务已完成"变成"任务崩溃"。

================================================================================
【线程】在**独立 daemon 线程**里跑：气泡要挂 `timeout_ms` 毫秒，
在调用方（主线程收尾）上 sleep 会让界面僵住。
"""
import ctypes
import sys
import threading
import time
from ctypes import wintypes

_IS_WIN = sys.platform == "win32"

# ---- Shell_NotifyIcon 常量（winuser.h / shellapi.h）----
NIM_ADD, NIM_MODIFY, NIM_DELETE = 0, 1, 2
NIF_MESSAGE, NIF_ICON, NIF_TIP, NIF_INFO = 0x01, 0x02, 0x04, 0x10
NIIF_INFO, NIIF_WARNING, NIIF_ERROR = 0x01, 0x02, 0x03
IDI_APPLICATION = 32512
HWND_MESSAGE = wintypes.HWND(-3)


class _GUID(ctypes.Structure):
    _fields_ = [("Data1", wintypes.DWORD), ("Data2", wintypes.WORD),
                ("Data3", wintypes.WORD), ("Data4", ctypes.c_ubyte * 8)]


class _NOTIFYICONDATAW(ctypes.Structure):
    """必须与 Win32 的布局逐字节对齐 —— 错一个字段后面全错，且不报错。"""

    _fields_ = [
        ("cbSize", wintypes.DWORD),
        ("hWnd", wintypes.HWND),
        ("uID", wintypes.UINT),
        ("uFlags", wintypes.UINT),
        ("uCallbackMessage", wintypes.UINT),
        ("hIcon", wintypes.HICON),
        ("szTip", wintypes.WCHAR * 128),
        ("dwState", wintypes.DWORD),
        ("dwStateMask", wintypes.DWORD),
        ("szInfo", wintypes.WCHAR * 256),
        ("uVersion", wintypes.UINT),        # union: uTimeout / uVersion
        ("szInfoTitle", wintypes.WCHAR * 64),
        ("dwInfoFlags", wintypes.DWORD),
        ("guidItem", _GUID),
        ("hBalloonIcon", wintypes.HICON),
    ]


_WNDPROC = ctypes.WINFUNCTYPE(ctypes.c_ssize_t, wintypes.HWND, wintypes.UINT,
                              wintypes.WPARAM, wintypes.LPARAM)
_class_registered = False
_class_lock = threading.Lock()


def _ensure_class():
    """注册一个消息专用窗口类（进程内只需一次）。"""
    global _class_registered
    with _class_lock:
        if _class_registered:
            return True
        user32 = ctypes.windll.user32
        kernel32 = ctypes.windll.kernel32

        @_WNDPROC
        def _wndproc(hwnd, msg, wparam, lparam):      # noqa: ARG001
            return user32.DefWindowProcW(hwnd, msg, wparam, lparam)

        # 【必须显式声明形参类型，否则回调里必炸】不声明时 ctypes 默认把每个参数
        #   当 `c_int`，而 lparam 是 64 位 —— 实测抛
        #   `ArgumentError: argument 4: OverflowError: int too long to convert`，
        #   而且它是从**回调里**抛的：外层 try/except 抓不到，只在 stderr 留一行
        #   "Exception ignored on calling ctypes callback function"。窗口因此拿不到
        #   正常的消息处理，气泡也就不出现 —— 症状是"什么都没发生"。
        user32.DefWindowProcW.argtypes = [wintypes.HWND, wintypes.UINT,
                                          wintypes.WPARAM, wintypes.LPARAM]
        user32.DefWindowProcW.restype = ctypes.c_ssize_t
        user32.CreateWindowExW.restype = wintypes.HWND
        user32.CreateWindowExW.argtypes = [
            wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD,
            ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
            wintypes.HWND, wintypes.HMENU, wintypes.HINSTANCE, wintypes.LPVOID]
        user32.DestroyWindow.argtypes = [wintypes.HWND]
        ctypes.windll.shell32.Shell_NotifyIconW.argtypes = [wintypes.DWORD,
                                                           ctypes.c_void_p]

        class _WNDCLASSW(ctypes.Structure):
            _fields_ = [
                ("style", wintypes.UINT), ("lpfnWndProc", _WNDPROC),
                ("cbClsExtra", ctypes.c_int), ("cbWndExtra", ctypes.c_int),
                ("hInstance", wintypes.HINSTANCE), ("hIcon", wintypes.HICON),
                ("hCursor", wintypes.HANDLE), ("hbrBackground", wintypes.HBRUSH),
                ("lpszMenuName", wintypes.LPCWSTR), ("lpszClassName", wintypes.LPCWSTR),
            ]

        wc = _WNDCLASSW()
        wc.lpfnWndProc = _wndproc
        wc.hInstance = kernel32.GetModuleHandleW(None)
        wc.lpszClassName = "TransferToolNotifyWnd"
        if not user32.RegisterClassW(ctypes.byref(wc)):
            err = ctypes.get_last_error() if hasattr(ctypes, "get_last_error") else 0
            if err not in (0, 1410):                    # 1410 = 类已注册
                return False
        _class_registered = True
        # 保留引用：局部变量被回收会让 WndProc 变成野指针
        _ensure_class._keep = _wndproc
        return True


def _show(title, message, timeout_ms, level):
    if not _ensure_class():
        return False
    user32 = ctypes.windll.user32
    kernel32 = ctypes.windll.kernel32
    shell32 = ctypes.windll.shell32

    hwnd = user32.CreateWindowExW(0, "TransferToolNotifyWnd", "", 0,
                                  0, 0, 0, 0, HWND_MESSAGE, None,
                                  kernel32.GetModuleHandleW(None), None)
    if not hwnd:
        return False
    try:
        nid = _NOTIFYICONDATAW()
        nid.cbSize = ctypes.sizeof(_NOTIFYICONDATAW)
        nid.hWnd = hwnd
        nid.uID = 1
        nid.uFlags = NIF_ICON | NIF_TIP | NIF_INFO
        # 【MAKEINTRESOURCE 的 ctypes 写法】`LoadIconW(None, 32512)` 直接把 int 交给
        # LPCWSTR 形参会抛 `OverflowError: int too long to convert`（实测）。
        # 关掉参数的自动转换、显式给个 `c_void_p` 才对得上 Win32 那个"整数当指针用"的约定。
        try:
            user32.LoadIconW.argtypes = [wintypes.HINSTANCE, ctypes.c_void_p]
            user32.LoadIconW.restype = wintypes.HICON
            nid.hIcon = user32.LoadIconW(None, ctypes.c_void_p(IDI_APPLICATION))
        except Exception:                              # noqa: BLE001 —— 没图标也要能弹
            nid.uFlags = NIF_TIP | NIF_INFO
        nid.szTip = "文件传输工具"
        nid.szInfoTitle = (title or "")[:63]
        nid.szInfo = (message or "")[:255]
        nid.uVersion = timeout_ms
        nid.dwInfoFlags = level
        if not shell32.Shell_NotifyIconW(NIM_ADD, ctypes.byref(nid)):
            return False
        time.sleep(max(1.0, timeout_ms / 1000.0))
        shell32.Shell_NotifyIconW(NIM_DELETE, ctypes.byref(nid))
        return True
    finally:
        user32.DestroyWindow(hwnd)


def notify(title, message, *, timeout_ms=6000, error=False):
    """弹一条托盘气泡。**永不抛异常**；真的弹不出来就返回 False。

    `timeout_ms` 是气泡停留时间（Windows 会取自己的最小值，通常 6~10 秒）。
    `error=True` 用红色错误图标。
    """
    if not _IS_WIN:
        return False
    level = NIIF_ERROR if error else NIIF_INFO

    def _run():
        # 线程体再包一层：异常发生在子线程里时，默认会把 traceback 打到 stderr ——
        # 对一个"锦上添花"的通道来说，那条 stderr 会让用户以为传输出问题了。
        try:
            _show(title, message, timeout_ms, level)
        except Exception:                              # noqa: BLE001
            pass

    try:
        t = threading.Thread(target=_run, name="tray-notify", daemon=True)
        t.start()
        return True
    except Exception:                                  # noqa: BLE001 —— 通知永不影响主流程
        return False
