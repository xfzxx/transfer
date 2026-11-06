# -*- coding: utf-8 -*-
r"""构建安装包：读版本号 → 规范化文本编码 → 调 makensis。

【为什么单独一个脚本】版本号只有一个来源（`config.APP_VERSION`）。手写进 .nsi 或
命令行会各写一份，迟早出现"exe 是 2.6.0、安装包写着 2.5.0"这种对不上的事。

用法：
    python installer/build.py            # 正式包（管理员安装，装到 Program Files）
    python installer/build.py --lite     # 轻量版（不含 205MB WebView2 离线安装器）
    python installer/build.py --test     # 测试包（免管理员，装到 %TEMP%，只验布局）

前置：
    · dist\文件传输工具.exe 已打包
    · installer\vendor\MicrosoftEdgeWebView2RuntimeInstallerX64.exe 已就位（--lite 不需要）
    · makensis 可用（默认取 %NSIS_MAKENSIS%，否则在 PATH 里找）
"""
import hashlib
import os
import re
import shutil
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)

WEBVIEW2 = os.path.join(HERE, "vendor", "MicrosoftEdgeWebView2RuntimeInstallerX64.exe")
# 官方 Evergreen 离线独立安装器（x64）的准确字节数 —— 只用来提示，不阻断构建。
WEBVIEW2_EXPECTED = 215017168


def app_version():
    with open(os.path.join(ROOT, "config.py"), "r", encoding="utf-8") as f:
        m = re.search(r'^APP_VERSION\s*=\s*"([^"]+)"', f.read(), re.M)
    if not m:
        raise SystemExit("config.py 里找不到 APP_VERSION")
    return m.group(1)


def to_utf8_bom(path):
    r"""NSIS 的 `Unicode True` 下，许可证与文本文件按 UTF-8 BOM / UTF-16LE 解码。

    没有 BOM 的 UTF-8 中文会被当成 ANSI（本机 936）读 —— 界面上就是一堆乱码，
    而且构建**不会报错**。所以这里统一补 BOM。
    """
    with open(path, "r", encoding="utf-8-sig") as f:
        text = f.read()
    with open(path, "w", encoding="utf-8-sig", newline="\r\n") as f:
        f.write(text)


def find_makensis():
    env = os.environ.get("NSIS_MAKENSIS")
    if env and os.path.exists(env):
        return env
    for name in ("makensis.exe", "makensis"):
        p = shutil.which(name)
        if p:
            return p
    raise SystemExit("找不到 makensis：请设 NSIS_MAKENSIS 或把它加进 PATH")


def _sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def main():
    test = "--test" in sys.argv
    lite = "--lite" in sys.argv

    ver = app_version()

    exe = os.path.join(ROOT, "dist", "文件传输工具.exe")
    if not os.path.exists(exe):
        raise SystemExit("缺 dist\\文件传输工具.exe —— 先跑 PyInstaller")

    if not lite:
        if not os.path.exists(WEBVIEW2):
            raise SystemExit("缺 WebView2 离线安装器：installer\\vendor\\"
                             "MicrosoftEdgeWebView2RuntimeInstallerX64.exe\n"
                             "（只想要小包可以加 --lite）")
        size = os.path.getsize(WEBVIEW2)
        if size != WEBVIEW2_EXPECTED:
            print("[注意] WebView2 安装器大小 %d，与官方 x64 离线包（%d）不一致 ——"
                  "若你换来的是别的架构/版本，忽略此提示" % (size, WEBVIEW2_EXPECTED))

    # 【脚本本身也要 BOM】makensis 默认按 ACP(936) 读 .nsi；`Unicode True` 只是让
    # **产物**是 Unicode，不会把脚本的读取编码改掉。没有 BOM 的 UTF-8 中文注释会被
    # 判成"Bad text encoding"直接中止 —— 这一步踩过一次，别删。
    for name in ("installer.nsi", "license.txt", "部署说明.txt"):
        to_utf8_bom(os.path.join(HERE, name))

    os.makedirs(os.path.join(HERE, "out"), exist_ok=True)
    cmd = [find_makensis(), "/DPRODUCT_VERSION=%s" % ver]
    if test:
        cmd.append("/DTESTMODE")
    if lite:
        cmd.append("/DLITE")
    cmd.append(os.path.join(HERE, "installer.nsi"))

    kind = "测试模式（免管理员）" if test else ("轻量版（不含 WebView2）" if lite else "完整版")
    print("[构建] 版本 %s · %s" % (ver, kind))
    payload = os.path.getsize(exe) / 1048576.0
    if not lite:
        payload += os.path.getsize(WEBVIEW2) / 1048576.0
    print("[构建] 载荷约 %.1f MB —— LZMA 压缩较慢，请耐心" % payload)
    rc = subprocess.call(cmd, cwd=HERE)
    if rc != 0:
        raise SystemExit("makensis 失败，退出码 %d" % rc)

    if test:
        out_name = "_test-安装包.exe"
    elif lite:
        out_name = "文件传输工具-%s-轻量版.exe" % ver
    else:
        out_name = "文件传输工具-%s-安装包.exe" % ver
    out = os.path.join(HERE, "out", out_name)
    if os.path.exists(out):
        digest = _sha256(out)
        print("[完成] %s  %.1f MB" % (out, os.path.getsize(out) / 1048576.0))
        # 【为什么要给校验值】exe 没有代码签名（没有证书），IT 侧唯一的完整性依据
        # 就是 SHA256：下发前对一次，能确认"传过去的和这里打的"是同一个文件。
        print("[SHA256] %s  %s" % (digest, out_name))
        if not test:
            # 【追加而不是覆盖】完整版与轻量版是两个文件，各有一行校验值。
            # 覆盖写会让"最后构建的那个"把另一个的记录挤掉 —— 下发前核对时
            # 找不到对应行，比没有这文件更让人怀疑。
            sums = os.path.join(HERE, "out", "SHA256SUMS.txt")
            lines = []
            if os.path.exists(sums):
                with open(sums, "r", encoding="utf-8") as f:
                    lines = [ln.rstrip("\r\n") for ln in f if ln.strip()]
            lines = [ln for ln in lines if not ln.endswith("  " + out_name)]
            lines.append("%s  %s" % (digest, out_name))
            with open(sums, "w", encoding="utf-8", newline="\r\n") as f:
                f.write("\r\n".join(lines) + "\r\n")
            print("[完成] 校验值已更新 %s（%d 条）" % (sums, len(lines)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
