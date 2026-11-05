# -*- coding: utf-8 -*-
r"""取 WebView2 离线安装器（Microsoft Edge WebView2 Runtime，Evergreen Standalone x64）。

【为什么用官方离线独立安装器，而不是 1.9MB 的在线引导器】
    引导器（MicrosoftEdgeWebview2Setup.exe）在安装时还要联网下载 200MB —— 医院内网
    恰恰没有外网，那条路等于装不上。离线包一次带全，有网没网都能装。

【为什么不把运行时编进 exe】
    WebView2 是**系统组件**，pywebview 通过它渲染窗口；它不属于 Python 依赖，
    PyInstaller 收不进去。能做的只有"随安装包带一份安装器，装到系统里"。

用法：
    python installer/fetch-webview2.py          # 下载（已存在则跳过）
    python installer/fetch-webview2.py --force  # 强制重下

来源：https://go.microsoft.com/fwlink/p/?LinkId=2124701
      → MicrosoftEdgeWebView2RuntimeInstallerX64.exe
"""
import os
import sys
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
DEST_DIR = os.path.join(HERE, "vendor")
DEST = os.path.join(DEST_DIR, "MicrosoftEdgeWebView2RuntimeInstallerX64.exe")
URL = "https://go.microsoft.com/fwlink/p/?LinkId=2124701"
EXPECTED = 215017168          # 2026-11 时的官方 x64 离线包字节数；变了只提示不阻断


def _download(url, dest):
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=120) as r, open(dest, "wb") as f:
        total = int(r.headers.get("Content-Length") or 0)
        got = 0
        while True:
            chunk = r.read(1 << 20)
            if not chunk:
                break
            f.write(chunk)
            got += len(chunk)
            if total:
                sys.stdout.write("\r  下载中 %5.1f%%  (%.0f / %.0f MB)"
                                 % (got * 100.0 / total, got / 1048576.0, total / 1048576.0))
                sys.stdout.flush()
        sys.stdout.write("\n")


def main():
    force = "--force" in sys.argv
    if os.path.exists(DEST) and not force:
        print("已存在，跳过：%s（%.1f MB）" % (DEST, os.path.getsize(DEST) / 1048576.0))
        return 0
    os.makedirs(DEST_DIR, exist_ok=True)
    print("从 %s 下载 WebView2 离线安装器…" % URL)
    _download(URL, DEST)
    size = os.path.getsize(DEST)
    print("完成：%s（%d 字节）" % (DEST, size))
    if size != EXPECTED:
        print("[注意] 与记录的大小 %d 不一致 —— 微软可能发布了新版本，属正常；"
              "若差得离谱（比如只有 1-2MB），说明拿到的是在线引导器，不能用于内网。" % EXPECTED)
    return 0


if __name__ == "__main__":
    sys.exit(main())
