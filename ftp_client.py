# -*- coding: utf-8 -*-
"""FTP 客户端：看门狗 / 连接池 / 断线重连重放。

【第三刀】整段从 file_transfer.py 第 522–1354 行**逐字**搬来（见 PROJECT_STATE §7.4.9）。
搬家判据是 **AST 等价**，不是"跑通测试" —— 被搬的 4 个类一行都没有改动
（搬完由 `_verify_split.py` 逐节点比对、`_check_anchor_shift.py` 逐行保真来证）。

依赖方向：`file_transfer -> ftp_client -> config`（单向，无环）。
本模块**不引用主模块的任何顶层名**（侦察实证 0 处），所以不需要反向 import ——
这也是它能被搬出来的前提：一旦这里出现 `file_transfer.X`，就是循环依赖。

**没有任何 UI 调用**：进度/日志一律走回调（`set_log_callback`），
本模块不 import tkinter。

为什么不 re-export config 里的东西：与主模块头部同款理由 ——
`from config import X` 会抄出一份**独立副本**，改 config 里的值对副本无效。
"""
import os
import queue
import socket
import ssl
import time
import ftplib

import config

# ===================== 阶段3：带看门狗的 FTP 客户端 =====================
# 【阶段3-新增】只改数据连接的 socket 超时，**命令序列一个字节都没动**
# （依然是 PASV → RETR/STOR → 226）。
# 为什么必须落在 ntransfercmd 上：数据连接是 ftplib 在 ntransfercmd 内部建的，
# 从外面拿不到那个 socket 对象，唯一的切入点就是在这里给它 settimeout。
# 语义是"每次 recv/send 最多等这么久"——即"连续 30 秒没字节"才算卡死，
# 而不是"整个文件必须在 30 秒内传完"（后者会让大文件在慢链路上必死）。
# ===================== 【2026-11-05】加密通道下的续传能力探测 =====================
# 【为什么必须探测】`SIZE` / `MDTM` / `REST` 这三条在所有 FTP 服务器上都是"可选"的，
#   在 FTPS（PROT P）下更是参差不齐：有的服务器数据通道加密了、但 `REST` 直接回 502。
#   不探的后果不是报错，是**静默整文件重传** —— 20 万文件的夜里备份，慢一倍没人知道。
# 【为什么用 `REST 0` 探而不是真续传】`REST 0` 是合法的空偏移请求：支持的服务器回 350，
#   不支持的立刻回 5xx，且**不改变任何状态**（偏移 0 = 从头）。用它探最安全。
_RESUME_PROBE_PATH = "/.ftp_transfer_resume_probe"
_RESUME_REJECT_CODES = ("500", "501", "502", "504")


def _base_name(path):
    """日志里只写文件名（完整远端路径太长，会把日志挤成一团）。"""
    return (path or "").replace("\\", "/").rstrip("/").split("/")[-1]


def _probe_command(ftp, cmd):
    """→ True/False（服务器接不接受这条命令）。出任何异常都当"不支持"。"""
    try:
        ftp.sendcmd(cmd)
        return True
    except ftplib.error_perm as e:
        return str(e)[:3] not in _RESUME_REJECT_CODES
    except Exception:                                  # noqa: BLE001
        return False


def probe_resume_caps(ftp, log=None):
    r"""探 SIZE / MDTM / REST 三条能力。→ `{"size": bool, "mdtm": bool, "rest": bool}`

    【`REST 不被接受时必须喊一句】需求原文："服务器拒绝加密通道下的断点续传，
    将从头开始传" —— 不许静默。所以这里直接回调 `log`，而不是只把结果塞进返回值：
    返回值会被上层忘掉，日志不会。
    """
    caps = {
        "size": _probe_command(ftp, "SIZE %s" % _RESUME_PROBE_PATH),
        "mdtm": _probe_command(ftp, "MDTM %s" % _RESUME_PROBE_PATH),
        "rest": _probe_command(ftp, "REST 0"),
    }
    if not caps["rest"] and log:
        log("[警告] 服务器拒绝加密通道下的断点续传（REST 不受支持），"
            "该任务的传输将**从头开始**，不做续传")
    if not caps["size"] and log:
        log("[警告] 服务器不支持 SIZE（无法核对远端大小）—— 上传校验会退化成只看字节数")
    return caps


def verify_resume_point(remote_size, part_size):
    r"""能不能从 `part_size` 接着传？→ `(bool, 原因文本)`。

    【为什么必须"正好等于"】`remote_size > part_size` 是正常续传；
    `remote_size < part_size` 说明远端的半截文件比本地还短（另一边重传过 / 被换过），
    这时**只能整文件重来** —— 拿一个更短的远端当基线接着写，写出来的文件是错的，
    而且错得很安静（大小对得上、内容对不上）。
    """
    try:
        rs = int(remote_size)
        ps = int(part_size)
    except (TypeError, ValueError):
        return False, "远端大小未知（SIZE 不可用或返回非法值），从头开始"
    if rs < 0 or ps < 0:
        return False, "大小出现负数（服务器返回异常），从头开始"
    if rs < ps:
        return False, "远端文件比本地 .part 还短（%.0f < %.0f），判为不一致，从头开始" % (rs, ps)
    if rs == ps:
        return False, "远端已完整（%d 字节），无需续传" % rs
    return True, "从 %d 字节处续传（远端 %d）" % (ps, rs)


# ===================== 【2026-11-05】FTPS（显式 / 隐式 TLS） =====================
def build_tls_context(allow_selfsigned):
    r"""建 TLS 上下文。

    【自签名这件事必须说清楚】`allow_selfsigned=True` 会把证书校验整个关掉
    （`CERT_NONE` + `check_hostname=False`）。这是**医院内网自建 FTP 的常见现状**，
    但它在密码学上等于"不验身份、只求加密信道" —— 能防 Passive Sniffing，
    防不住中间人。所以界面上它是**默认不勾**的勾选项，而不是默认行为。
    """
    ctx = ssl.create_default_context()
    if allow_selfsigned:
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
    return ctx


class ImplicitFTP_TLS(ftplib.FTP_TLS):
    r"""隐式 TLS（FTPS，通常 990）：**连上立刻握手**，不发 AUTH TLS。

    与 `FTP_TLS` 的区别只有一步：`connect()` 之后马上把控制连接包起来。
    之后的 `login()` / `prot_p()` 与显式模式完全相同 —— 所以握手函数可以共用。
    """

    def connect(self, host="", port=0, timeout=-999, source_address=None):
        super().connect(host, port, timeout, source_address)
        self.sock = self.context.wrap_socket(self.sock, server_hostname=host)
        self.af = self.sock.family
        return self.welcome


def ftp_class_for(mode):
    """→ 该用哪个类建连接对象。`none` 返回 `None`（调用方按原样用 `ftplib.FTP`）。"""
    if mode == "explicit":
        return ftplib.FTP_TLS
    if mode == "implicit":
        return ImplicitFTP_TLS
    return None


def secure_session(ftp, mode, host, user, password):
    r"""在**已 connect** 的对象上完成握手 → 登录 → 数据通道加密。

    【为什么抽成独立函数】这三步的顺序是安全语义的核心（先握手再送密码），
    而它没法用"真连一台 FTPS"来验（测试环境没有那台服务器）。
    抽出来之后就能用一个记录调用的假对象把**顺序**钉死 —— 顺序错了就是明文送密码，
    那种错误不会有任何报错，只会在抓包里看得见。

    【显式模式的顺序】connect → auth() → login() → prot_p()
      先 auth 再 login，密码才是走加密信道的；`prot_p()` 让**数据通道**也加密
      （没有这一步，文件内容是明文传的，控制通道加密等于白做）。
    """
    if not isinstance(ftp.sock, ssl.SSLSocket):
        if mode == "implicit":
            # 隐式模式理论上已在 connect 里包过；走到这里说明调用方拿错了类
            raise ValueError("隐式 TLS 应在 connect() 时完成握手")
        ftp.auth()                      # AUTH TLS
    ftp.login(user, password)
    ftp.prot_p()                        # PBSZ 0 + PROT P
    return ftp


class _WatchdogFTP(ftplib.FTP):
    def __init__(self, *args, **kwargs):
        self.data_idle_timeout = kwargs.pop('data_idle_timeout', None)
        ftplib.FTP.__init__(self, *args, **kwargs)

    def ntransfercmd(self, cmd, rest=None):
        conn, size = ftplib.FTP.ntransfercmd(self, cmd, rest)
        if conn is not None and self.data_idle_timeout:
            try:
                conn.settimeout(self.data_idle_timeout)
            except OSError:
                pass                    # 设不上超时不该让这次传输失败，退回 ftplib 原行为
        return conn, size




class FTPHelper:
    def __init__(self, host, user='', password='', port=21, timeout=10,
                 data_idle_timeout=None, tls_mode="none", allow_selfsigned=False):
        self.host = host
        self.user = user
        self.password = password
        self.port = port
        self.timeout = timeout
        # 【阶段3-新增】None = 用 ftplib.FTP（阶段1/2 的原有行为，回滚路径逐字不变）；
        # 传秒数 = 用 _WatchdogFTP（连接池路径）。默认 None 是刻意的：
        # 回滚开关 _FTP_POOL_ENABLED=False 时，被跑起来的必须是**当年验过的那套**。
        self.data_idle_timeout = data_idle_timeout
        # 【2026-11-05 FTPS】默认 "none" = 明文，与升级前逐字节一致；
        #   调用方（ftp_transfer / 维护任务）从配置里把这两项传进来。
        self.tls_mode = tls_mode or "none"
        self.allow_selfsigned = bool(allow_selfsigned)
        # 【2026-11-05】续传能力（connect() 里探一次就固定）。`None` = 还没探过；
        #   上层据此判断"能不能续"，而不是假设服务器一定支持 REST。
        self.resume_caps = None
        self.ftp = None
        self.log_callback = None
        # 【阶段2d-新增】RNFR/RNTO 能力缓存：None=未探测，True/False=已探测结果。
        # 每条连接的探测结果固定，一次连接只探一次（见 supports_rename）。
        self._rename_ok = None

    def set_log_callback(self, callback):
        self.log_callback = callback

    def _log(self, msg):
        if self.log_callback:
            self.log_callback(msg)

    def connect(self):
        # 【2026-11-05 FTPS】按配置的加密模式选连接类。
        #   `tls_mode` 缺省（老配置 / 未接线）一律当 `none` —— 与升级前逐字节一致。
        mode = getattr(self, "tls_mode", None) or "none"
        cls = ftp_class_for(mode)
        if cls is not None:
            self.ftp = cls(context=build_tls_context(getattr(self, "allow_selfsigned", False)))
            self.ftp.connect(self.host, self.port, self.timeout)
            secure_session(self.ftp, mode, self.host, self.user, self.password)
        else:
            if self.data_idle_timeout:
                self.ftp = _WatchdogFTP(data_idle_timeout=self.data_idle_timeout)
            else:
                self.ftp = ftplib.FTP()
            self.ftp.connect(self.host, self.port, self.timeout)
            self.ftp.login(self.user, self.password)
        # 启用 UTF-8 支持以处理中文文件名
        try:
            self.ftp.voidcmd('OPTS UTF8 ON')
        except Exception:  # 可选能力：服务器不认 OPTS UTF8 属正常
            pass
        if hasattr(self.ftp, 'encoding'):
            self.ftp.encoding = 'utf-8'
        self.ftp.voidcmd('TYPE I')
        # 【2026-11-05】续传能力探测。放在 TYPE I 之后：这是连接就绪的最后一个动作，
        #   探测失败也只影响"能不能续"，不影响连接本身能不能用。
        self.resume_caps = probe_resume_caps(self.ftp, log=self._log)

    # 【阶段3-修改原因】改为**幂等**（原审计 P0-5）。连接池里同一条连接的收尾路径有
    # 三条且可能叠加：worker 正常退出、worker 异常退出、池的 close_all()。
    # 旧实现在"已经关过"时：quit() 抛 AttributeError（sock 为 None）→ 裸 except 吞掉
    # → 再 close()（此时 sock 已是 None，是空操作）。行为上勉强没崩，但一是靠裸 except
    # 兜着、二是 close() 之后再进来还会再 try 一遍。
    # 现在显式置 self.ftp = None：第二遍直接短路返回，且调用方也能靠 `ftp.ftp is None`
    # 判断"这条连接已经关了"。注意 ftplib.FTP.quit() 失败时自身不保证关 socket，
    # 所以 except 分支仍要 close()。
    def disconnect(self):
        """幂等：关掉当前连接；已经关过（self.ftp 已是 None）就直接返回。"""
        ftp, self.ftp = self.ftp, None
        if ftp is None:
            return
        try:
            # 【阶段3-修复｜体检发现】quit() = 发 QUIT + **等 221 应答**。
            # 对端卡死时（数据连接刚被 watchdog 判死的那条连接正是这种状态）服务器
            # 根本没在读命令，这一等就是一个完整的 socket 超时：默认 10 秒。
            # 实测：数据连接 1 秒判死，disconnect 又白等 10.02 秒——而调用方
            # （_with_reconnect 每次链路故障、pool.close_all 每任务收尾）都在关键路径上。
            # 把等 221 的预算压到 FTP_QUIT_TIMEOUT_S：服务器正常时 221 立刻回，
            # 不回就 close() 走人——QUIT 的语义只是"礼貌告别"，本端的正确性不依赖它
            # （数据完整性由 .part + SIZE 自检保证，与告别礼数无关）。
            try:
                ftp.sock.settimeout(config.FTP_QUIT_TIMEOUT_S)
            except Exception:
                pass            # sock 已经没了（连接早断）：quit() 自己会立刻失败
            ftp.quit()
        except Exception:
            try:
                ftp.close()
            except Exception:
                pass

    # 【阶段3-新增】原审计 P0-5：让 FTPHelper 可直接用 with 包裹。
    # 池里的长连接不用它（连接的存活跨越多次操作，见 FTPPooledConn），
    # 但"借用一条连接做完事就还"的写法应当是 `with FTPHelper(...) as ftp:`，
    # 而不是靠调用方记得在 finally 里 disconnect——那就是 P0-5 记的那个漏。
    def __enter__(self):
        self.connect()
        return self

    def __exit__(self, exc_type, exc, tb):
        self.disconnect()           # 幂等，异常路径上也必然执行
        return False

    def list_files_recursive(self, remote_dir):
        """列出远程目录下所有文件，remote_dir 应为以 / 开头的绝对路径。

        【阶段2c-返回值变更】由 `(path, size)` 二元素改为**五元组**：
            (path, size, mtime_raw, mtime_norm, precision)
            mtime_raw   服务器给的原始字符串（仅供日志/排错）
            mtime_norm  规范化后的 epoch；解析失败为 None
            precision   's' / 'min'；解析失败为 None
        解析失败（旧服务器、LIST 格式不识别）不是错误：调用方看到 None 就判为
        "该文件 mtime 不可信"→ 不跳过 → 重传。宁可重传，不可漏传（§4.3）。"""
        files = []
        # 标准化为绝对路径
        remote_dir = '/' + remote_dir.lstrip('/')
        try:
            self._log(f"尝试进入目录: {remote_dir}")
            self.ftp.cwd('/')  # 先回到根目录
            self.ftp.cwd(remote_dir)
            work_dir = remote_dir
        except ftplib.error_perm as e:
            self._log(f"进入失败: {e}，尝试去掉开头斜杠")
            alt = remote_dir.lstrip('/')
            if alt:
                try:
                    self._log(f"尝试进入去掉斜杠后的目录: {alt}")
                    self.ftp.cwd(alt)
                    work_dir = alt
                except Exception as e2:
                    self._log(f"再次失败: {e2}")
                    return files
            else:
                self.ftp.cwd('/')
                work_dir = '/'
        self._recursive_list('.', files, work_dir)
        self.ftp.cwd('/')
        return files

    def _recursive_list(self, path, files, base_remote_dir, acc_path=None):
        if acc_path is None:
            acc_path = path
        try:
            self.ftp.cwd(path)
        except ftplib.error_perm:
            return
        lines = []
        try:
            self.ftp.retrlines('MLSD', lines.append)
        except ftplib.error_perm:
            # 回退到 LIST，使用增强解析
            lines = []
            self.ftp.retrlines('LIST', lines.append)
            # 【阶段2c-修改原因】多捕 3 组（月/日/时间或年份）供 _parse_list_mtime 使用。
            # 模式仍**结束于文件名之前**，因为下面用的是 match.end() 定位文件名。
            # 注意：FTPBrowserDialog._load_children 里有一份同源的 LIST 解析（方案 §4.2 明确不动它）。
            # 【阶段4-修改原因】三个正则提到模块级 _LIST_*_RE，与流式遍历共用一份
            # （理由见常量处的注释）。本函数的行为一字未变，纯属"定义位置搬家"。
            unix_pattern = config._LIST_UNIX_RE
            win_dir_pattern = config._LIST_WIN_DIR_RE
            win_file_pattern = config._LIST_WIN_FILE_RE
            try:
                for line in lines:
                    line = line.rstrip()
                    if not line:
                        continue
                    unix_match = unix_pattern.match(line)
                    if unix_match:
                        filetype = unix_match.group(1)
                        name_start = unix_match.end()
                        name = line[name_start:].strip()
                        if name in ('.', '..'):
                            continue
                        full_path = f"{base_remote_dir}/{acc_path}/{name}" if acc_path != '.' else f"{base_remote_dir}/{name}"
                        full_path = full_path.replace('//', '/')
                        if filetype == 'd':
                            new_acc = f"{acc_path}/{name}" if acc_path != '.' else name
                            self._recursive_list(name, files, base_remote_dir, new_acc)
                        elif filetype == '-':
                            size = int(unix_match.group(3))
                            # 【阶段2c】"月 日 时间/年" → (epoch, precision)
                            mraw = "%s %s %s" % (unix_match.group(4), unix_match.group(5),
                                                 unix_match.group(6))
                            mnorm, mprec = config._parse_list_mtime(mraw)
                            files.append((full_path, size, mraw, mnorm, mprec))
                        continue
                    win_dir_match = win_dir_pattern.match(line)
                    if win_dir_match:
                        name = win_dir_match.group(3).strip()
                        if name in ('.', '..'):
                            continue
                        full_path = f"{base_remote_dir}/{acc_path}/{name}" if acc_path != '.' else f"{base_remote_dir}/{name}"
                        full_path = full_path.replace('//', '/')
                        new_acc = f"{acc_path}/{name}" if acc_path != '.' else name
                        self._recursive_list(name, files, base_remote_dir, new_acc)
                        continue
                    win_file_match = win_file_pattern.match(line)
                    if win_file_match:
                        name = win_file_match.group(4).strip()
                        size = int(win_file_match.group(3))
                        full_path = f"{base_remote_dir}/{acc_path}/{name}" if acc_path != '.' else f"{base_remote_dir}/{name}"
                        full_path = full_path.replace('//', '/')
                        # 【阶段2c】DOS 形式："01-12-24 10:30AM" → (epoch, 'min')
                        mraw = "%s %s" % (win_file_match.group(1), win_file_match.group(2))
                        mnorm, mprec = config._parse_list_mtime(mraw)
                        files.append((full_path, size, mraw, mnorm, mprec))
                        continue
                    # 最后的回退简单解析
                    parts = line.split()
                    if len(parts) < 9:
                        continue
                    name = ' '.join(parts[8:])
                    if name in ('.', '..'):
                        continue
                    full_path = f"{base_remote_dir}/{acc_path}/{name}" if acc_path != '.' else f"{base_remote_dir}/{name}"
                    full_path = full_path.replace('//', '/')
                    if parts[0].startswith('d'):
                        new_acc = f"{acc_path}/{name}" if acc_path != '.' else name
                        self._recursive_list(name, files, base_remote_dir, new_acc)
                    elif parts[0].startswith('-'):
                        size = int(parts[4])
                        # 【阶段2c】同上：parts[5..7] = 月、日、时间或年份
                        mraw = "%s %s %s" % (parts[5], parts[6], parts[7])
                        mnorm, mprec = config._parse_list_mtime(mraw)
                        files.append((full_path, size, mraw, mnorm, mprec))
            finally:
                self.ftp.cwd('..')
            return

        # MLSD 解析
        for line in lines:
            parts = line.split(';')
            name = parts[-1].strip()
            if name in ('.', '..'):
                continue
            attrs = {}
            for part in parts[:-1]:
                if '=' in part:
                    k, v = part.strip().split('=', 1)
                    attrs[k.lower()] = v
            item_type = attrs.get('type', '')
            full_path = f"{base_remote_dir}/{acc_path}/{name}" if acc_path != '.' else f"{base_remote_dir}/{name}"
            full_path = full_path.replace('//', '/')
            if item_type == 'dir':
                new_acc = f"{acc_path}/{name}" if acc_path != '.' else name
                self._recursive_list(name, files, base_remote_dir, new_acc)
            elif item_type == 'file':
                size = int(attrs.get('size', 0))
                # 【阶段2c】MLSD 的 modify= 是 UTC（RFC 3659），解析为 (epoch, 's')。
                # 服务器没给 modify= 时得到 (None, None) → 该文件不参与可信续传（每次都传）。
                mraw = attrs.get('modify', '')
                mnorm, mprec = config._parse_mlsd_mtime(mraw)
                files.append((full_path, size, mraw, mnorm, mprec))
        self.ftp.cwd('..')

    # ---- 阶段4：流式遍历（逐目录推进） ----
    def list_dir(self, path):
        """【阶段4-新增】列出**单个**目录（不递归），返回 (files, subdirs)。

        files   = [(name, size, mraw, mnorm, mprec), ...]   名字是条目名，不含路径
        subdirs = [name, ...]

        为什么是"单目录"而不是"直接给个递归生成器"：与 `_recursive_list` 不同，
        本函数是流式遍历的**推进单位**，它必须能被独立地重连重放（连接断了就重列
        这一个目录，而不是把整棵树重扫一遍——后者会把已经交出去的文件再交一遍）。
        也正因为要能重连重放，这里一律按**传入路径** cwd：`_recursive_list` 那种
        `cwd('..')` 的相对回退在重连之后毫无意义（新连接的当前目录是登录目录）。
        解析不出来（权限不足、列到一半目录消失）返回空表：与 `_recursive_list`
        "进不去就跳过"的分支保持一致，一个坏目录不该让整次扫描失败。
        """
        try:
            self.ftp.cwd(path)
        except ftplib.error_perm:
            return [], []
        try:
            lines = []
            self.ftp.retrlines('MLSD', lines.append)
        except ftplib.error_perm:
            try:
                lines = []
                self.ftp.retrlines('LIST', lines.append)
            except ftplib.error_perm:
                return [], []
            return self._parse_list_lines(lines)
        return self._parse_mlsd_lines(lines)

    def _parse_mlsd_lines(self, lines):
        """MLSD 输出 → (files, subdirs)。字段语义与 `_recursive_list` 的 MLSD 分支一致。"""
        files, subdirs = [], []
        for line in lines:
            parts = line.split(';')
            name = parts[-1].strip()
            if name in ('.', '..'):
                continue
            attrs = {}
            for part in parts[:-1]:
                if '=' in part:
                    k, v = part.strip().split('=', 1)
                    attrs[k.lower()] = v
            item_type = attrs.get('type', '')
            if item_type == 'dir':
                subdirs.append(name)
            elif item_type == 'file':
                size = int(attrs.get('size', 0))
                # 【阶段2c】MLSD 的 modify= 是 UTC（RFC 3659），解析为 (epoch, 's')。
                # 服务器没给 modify= 时得到 (None, None) → 该文件不参与可信续传（每次都传）。
                mraw = attrs.get('modify', '')
                mnorm, mprec = config._parse_mlsd_mtime(mraw)
                files.append((name, size, mraw, mnorm, mprec))
        return files, subdirs

    def _parse_list_lines(self, lines):
        """LIST 输出 → (files, subdirs)。四个分支与 `_recursive_list` 的 LIST 分支一一对应
        （Unix / DOS 目录 / DOS 文件 / 兜底简单解析），正则共用模块级 _LIST_*_RE。"""
        files, subdirs = [], []
        for line in lines:
            line = line.rstrip()
            if not line:
                continue
            unix_match = config._LIST_UNIX_RE.match(line)
            if unix_match:
                filetype = unix_match.group(1)
                name = line[unix_match.end():].strip()
                if name in ('.', '..'):
                    continue
                if filetype == 'd':
                    subdirs.append(name)
                elif filetype == '-':
                    mraw = "%s %s %s" % (unix_match.group(4), unix_match.group(5),
                                         unix_match.group(6))
                    mnorm, mprec = config._parse_list_mtime(mraw)
                    files.append((name, int(unix_match.group(3)), mraw, mnorm, mprec))
                continue
            win_dir_match = config._LIST_WIN_DIR_RE.match(line)
            if win_dir_match:
                name = win_dir_match.group(3).strip()
                if name in ('.', '..'):
                    continue
                subdirs.append(name)
                continue
            win_file_match = config._LIST_WIN_FILE_RE.match(line)
            if win_file_match:
                name = win_file_match.group(4).strip()
                mraw = "%s %s" % (win_file_match.group(1), win_file_match.group(2))
                mnorm, mprec = config._parse_list_mtime(mraw)
                files.append((name, int(win_file_match.group(3)), mraw, mnorm, mprec))
                continue
            # 最后的回退简单解析
            parts = line.split()
            if len(parts) < 9:
                continue
            name = ' '.join(parts[8:])
            if name in ('.', '..'):
                continue
            if parts[0].startswith('d'):
                subdirs.append(name)
            elif parts[0].startswith('-'):
                mraw = "%s %s %s" % (parts[5], parts[6], parts[7])
                mnorm, mprec = config._parse_list_mtime(mraw)
                files.append((name, int(parts[4]), mraw, mnorm, mprec))
        return files, subdirs

    def iter_files_recursive(self, remote_dir, dir_counter=None,
                             prune=None, on_visit=None):
        """【阶段4-新增】流式版 list_files_recursive：逐目录 yield 五元组。

        【阶段一 Bug2】`dir_counter`（可选）：原样转交给 config._walk_remote_files，
        用来数"已列举过多少个目录"。默认 None = 不计数，行为与本阶段之前完全相同。

        【P0-1 新增 `prune` / `on_visit`（均可选）】**原样透传**，本类一个字都不判。
        日期分层的判据在 `config.DateLayerPruner` 里，遍历器不认识"日期"——
        与 `_match_filter` 同一层。默认 None = 逐字节不变。

        五元组与 list_files_recursive **逐字段相同**（用例 N 拿两种实现比对钉住）。
        内存从 O(全部文件) 降到 O(单个目录)，这是"20 万文件不再先扫完再开传"的关键。
        与 list 版的两点差异，都是流式化的必然，都不影响"找到哪些文件、每个文件什么属性"：
          ① 下钻顺序：list 版按目录列举顺序深度优先，本版同层子目录**倒序**下钻
             （用栈而不是递归）。文件之间彼此独立，顺序不参与任何判定。
          ② 路径：list 版在"绝对路径进不去、退回相对路径"时可能产出相对路径，
             本版沿用同一套回落逻辑（见下），因此也一致。
        """
        remote_dir = '/' + str(remote_dir).lstrip('/')
        try:
            self._log(f"尝试进入目录: {remote_dir}")
            self.ftp.cwd('/')  # 先回到根目录
            self.ftp.cwd(remote_dir)
            work_dir = remote_dir
        except ftplib.error_perm as e:
            # 与 list_files_recursive 的首段逐字同源：进不去就试"去掉开头斜杠"，
            # 再不行就退回根目录（此时遍历根，等于"把能看到的都传走"）。
            self._log(f"进入失败: {e}，尝试去掉开头斜杠")
            alt = remote_dir.lstrip('/')
            if alt:
                try:
                    self._log(f"尝试进入去掉斜杠后的目录: {alt}")
                    self.ftp.cwd(alt)
                    work_dir = alt
                except Exception as e2:
                    self._log(f"再次失败: {e2}")
                    return
            else:
                self.ftp.cwd('/')
                work_dir = '/'
        try:
            for item in config._walk_remote_files(self.list_dir, work_dir, dir_counter,
                                                  prune=prune, on_visit=on_visit):
                yield item
        finally:
            # 遍历可能被中途放弃（用户停止 → 生成器被关闭），此时 cwd 停在中途某个目录。
            # 与 list_files_recursive 一样复位回根：这条连接后面还要被复用。
            # 这里**不能**走重连重放（本类是裸的 FTPHelper），复位失败就算了——
            # 它是卫生动作，不是正确性动作（池里的连接每次操作都用绝对路径）。
            try:
                self.ftp.cwd('/')
            except Exception:
                pass

    def download_file(self, remote_path, local_path, mtime=None):
        """直接使用完整的远程绝对路径下载，不依赖当前工作目录。

        【阶段2c-新增 mtime 参数】下载完成后用 os.utime 把本地文件的 mtime 对齐到远端值。
        两个用途：
          1) 原子落盘时，.part → os.replace 之后目标文件带着**远端 mtime**，
             与 files 表里记的 src_mtime 一致，续传判定才有可比的基准；
          2) 不传 mtime（缺省）时行为与阶段1 逐字一致：文件 mtime = 当前时间。
        失败不抛：改不动 mtime 只是让该文件下次重传，不该让整个下载失败。"""
        with open(local_path, 'wb') as f:
            self.ftp.retrbinary(f'RETR {remote_path}', f.write)
        if mtime is not None:
            try:
                os.utime(local_path, (mtime, mtime))
            except OSError as e:
                self._log(f"[警告] 无法对齐 mtime（该文件下次将重传）: {local_path} - {e}")

    # 【阶段2d-新增】探测服务器是否支持 RNFR/RNTO（改名）。一次连接只探一次。
    # 判据来自 RFC 959 的应答约定：**命令未实现**回 500/501/502/504；
    # **命令可用但文件不存在**回 550。所以拿一个必然不存在的路径去 RNFR：
    #   回 500/501/502/504 → 不支持改名
    #   其余（550 / 450 / 553 / 350）→ 支持改名
    # 探测本身出错（网络异常）一律保守判为"不支持"，退回直传 + 校验：
    # 绝不能让"探测"这个动作本身把上传搞失败。
    def supports_rename(self):
        if self._rename_ok is None:
            code = '500'
            try:
                self.ftp.sendcmd("RNFR /.ftp_transfer_rename_probe")
            except ftplib.error_perm as e:
                code = str(e)[:3]
            except Exception:
                self._rename_ok = False
                return False
            self._rename_ok = code not in ('500', '501', '502', '504')
        return self._rename_ok

    def _upload_direct(self, local_path, remote_path, rest=0):
        """把数据 STOR 到目标路径。`rest>0` 时从该偏移续传（阶段1 行为 + 续传）。

        【为什么用 `storbinary(rest=)` 而不是自己先 `REST`】`storbinary` 内部会先发
        `REST n` 再发 `STOR`，两者是**同一条命令序列**；分开写就多一个"REST 成功了、
        STOR 还没发就断线"的中间态，恢复起来还要判断偏移是否已生效。
        """
        with open(local_path, 'rb') as f:
            if rest:
                f.seek(rest)
            self.ftp.storbinary(f'STOR {remote_path}', f, rest=rest)

    def _plan_upload_resume(self, part_remote, local_path):
        r"""→ `(偏移, 原因)`。**任何不确定都返回 0（从头传）** —— 慢一点可以接受，
        拼出"大小对、内容错"的文件不行。

        判据（缺一不可）：
          · 服务器支持 SIZE（否则不知道远端 .part 到底有多少字节）；
          · 远端 .part 存在且 `0 < len < 本地大小`；
          · `len > 本地大小` ⇒ 远端那份比源还长，必然是别人的/上次传错文件 ⇒ 从头。
        """
        caps = self.resume_caps or {}
        if not caps.get("size"):
            return 0, "服务器不支持 SIZE，无法核对续传点，从头开始"
        try:
            remote_len = self.ftp.size(part_remote)
        except Exception:                              # noqa: BLE001
            return 0, "取远端 .part 大小失败，从头开始"
        if remote_len is None:
            return 0, "远端 .part 不存在，从头开始"
        try:
            local_len = os.path.getsize(local_path)
        except OSError:
            return 0, "读不到本地文件大小，从头开始"
        if remote_len <= 0:
            return 0, "远端 .part 是空文件，从头开始"
        if remote_len > local_len:
            return 0, ("远端 .part（%d B）比本地文件（%d B）还长，判为不一致，从头开始"
                       % (remote_len, local_len))
        if remote_len == local_len:
            return 0, "远端 .part 已与本地等长，直接改名即可"
        if not caps.get("rest"):
            return 0, "服务器拒绝 REST，从头开始"
        return remote_len, "从 %d / %d 字节处续传" % (remote_len, local_len)

    def upload_file(self, local_path, remote_path):
        """直接使用完整的远程绝对路径上传。

        【阶段2d-新增 原子上传】默认走"传 .part → SIZE 校验 → RNFR/RNTO 改名"：
        只有**全部字节到达且校验通过**，正式文件名才会出现。这是铁律一在 FTP 侧的
        对偶——半截文件永远停在 .part 上，下游（PACS/阅片端）不会把残缺文件当成
        完整文件读走。直传做不到这一点：崩溃或断网时，残缺内容就停在正式文件名下，
        而库里根本没有对应的 done 记录，除了"下次再传一遍"没有任何线索。

        两条降级路径，都不牺牲"不产生假完整文件"：
          * _ATOMIC_WRITE_ENABLED=False（§11 回滚开关）→ 逐字退回阶段1 的直传；
          * 服务器不支持 RNFR/RNTO → 直传 + SIZE 校验，并**明确告警**。
            直传没有原子性，但 SIZE 校验仍能挡住"服务器谎报 226 却少收了字节"
            这一类静默截断（这类截断在 DICOM 里等于一张读不出来的片子）。
        """
        if not config._ATOMIC_WRITE_ENABLED or not self.supports_rename():
            if config._ATOMIC_WRITE_ENABLED:
                self._log("[警告] 服务器不支持 RNFR/RNTO，本次上传降级为非原子"
                          "（已加 SIZE 校验兜住字节数）")
            self._upload_direct(local_path, remote_path)
            self._verify_size(local_path, remote_path)
            return

        part = remote_path + config.UPLOAD_PART_SUFFIX
        # 【2026-11-05】续传：先问"能不能从 .part 接着传"，再动手。
        _offset, _why = self._plan_upload_resume(part, local_path)
        if _offset:
            self._log("[续传] %s：%s" % (_base_name(remote_path), _why))
        elif "从头" in _why:
            # 降级一律出声（不许静默整文件重传 —— 那是这台机器最常见的性能黑洞）
            self._log("[续传] %s：%s" % (_base_name(remote_path), _why))
        try:
            self._upload_direct(local_path, part, rest=_offset)
            self._verify_size(local_path, part)
            try:
                self.ftp.rename(part, remote_path)
            except ftplib.error_perm:
                # 少数服务器拒绝 RNTO 覆盖已存在的文件（回 550）。此时才删旧文件再改名。
                # 这一段窗口是"旧文件已删、新文件未就位"，但 .part 已是**校验通过的
                # 完整副本**，最坏情况也只是目标名短暂缺失，绝不会出现半截内容。
                self.ftp.delete(remote_path)
                self.ftp.rename(part, remote_path)
        except BaseException:
            # 【2026-11-05】可续的服务器上**留着 .part** —— 那正是下次续传的起点。
            #   不可续（无 SIZE / 无 REST）时才删：留着也接不上，只会在服务器上堆垃圾。
            caps = self.resume_caps or {}
            if caps.get("size") and caps.get("rest"):
                try:
                    _n = self.ftp.size(part)
                    self._log("[续传] 已保留远端 .part（%s 字节），下次从这里接着传"
                              % (_n if _n is not None else "?"))
                except Exception:            # noqa: BLE001
                    pass
            else:
                try:
                    self.ftp.delete(part)    # 不可续：不完整的那份无保留价值
                except Exception:
                    pass
            raise

    # 【阶段2d-新增】上传后的字节数自检。远端 size 未知一律按不通过处理：
    # "量不出来"和"量出来不对"在数据安全上是同一件事——都不能确认这份文件完整。
    def _verify_size(self, local_path, remote_path):
        local_size = os.path.getsize(local_path)
        got = self.size(remote_path)
        if got is None:
            raise IOError(f"无法取得远端文件大小（服务器不支持 SIZE 或路径不可达），"
                          f"不能确认上传完整性: {remote_path}")
        if got != local_size:
            raise IOError(f"上传字节数不符：本地 {local_size}，远端 {got}（{remote_path}）")

    def size(self, remote_path):
        """SIZE 命令取远端文件大小；服务器不支持或路径不存在时返回 None。

        【阶段2c-新增】用途有二（方案 §2 / §9）：
          1) 上传路径的跳过判定：远端目标文件的 size 必须等于本地源 size，否则重传；
          2) 上传完成后的自检（2d 的原子上传 STOR .part → SIZE → RNFR/RNTO 会用到）。
        返回 None 一律按"远端状态不可信"处理 → 调用方**不跳过**。"""
        try:
            return self.ftp.size(remote_path)
        except Exception:
            return None

    def ensure_remote_dir(self, remote_dir):
        """递归创建远程目录，忽略已存在的错误"""
        parts = remote_dir.strip('/').split('/')
        current = '/'
        for part in parts:
            if part:
                current = current + '/' + part if current != '/' else '/' + part
                try:
                    self.ftp.mkd(current)
                except ftplib.error_perm:
                    pass

# ===================== 阶段3：连接池 =====================
# 【阶段3-设计说明｜与方案的差异，已记入简报】方案把 ensure_connected() /
# _with_reconnect() 和"连接池"写在同一条里。本实现把它们落在**单条连接**
# （FTPPooledConn）上，池（FTPConnectionPool）只管"谁用哪条"。
# 理由：健康检查与重连是**连接级**语义，写成池级就只能靠 thread-local 反查
# "我这条线程当前借的是哪条连接"，那是为了对齐命名而生的死代码。
# 名字都保留了，只是各归其位。
class FTPPooledConn:
    """池中的一条连接：健康检查 + 断线重连 + 目录缓存 + 数据连接看门狗。

    对 worker 而言它就是 FTPHelper（download_file / upload_file / size /
    ensure_remote_dir / list_files_recursive / cwd 同名同签名），
    所以 _atomic_write_download 这类"拿一个 ftp 对象就用"的既有代码**一行都不用改**。
    """

    def __init__(self, host, user, password, port=21, timeout=10, log_callback=None,
                 reconnect_attempts=None):
        self._args = (host, user, password, port, timeout)
        self._log_cb = log_callback
        self._helper = None
        self._known_dirs = set()
        self._last_ok_at = 0.0
        self.reconnect_count = 0        # 供测试断言"确实重连过"
        # 【阶段二-新增】断线重连的**尝试**次数。`None` = 回退到常量
        # FTP_RECONNECT_ATTEMPTS —— 与 `timeout` 的既有默认参数是同一条规矩：
        # 常量仍是"没人传的时候用什么"，调用方传了才覆盖。
        # ⚠️ 不进 `self._args`：那个元组是喂给 `FTPHelper(*self._args)` 的位置参数，
        #    多塞一个会让 FTPHelper 收到一个它不认识的参数、当场 TypeError。
        self._reconnect_attempts = reconnect_attempts

    # ---------------------------------------------------------- 日志
    def _log(self, msg):
        if self._log_cb:
            self._log_cb(msg)

    # ---------------------------------------------------------- 生命周期
    def connect(self):
        """建一条**全新**的连接（旧的先关掉）。"""
        self.close()
        helper = FTPHelper(*self._args, data_idle_timeout=config.FTP_DATA_IDLE_TIMEOUT_S)
        helper.set_log_callback(self._log_cb)
        helper.connect()
        self._helper = helper
        # 【阶段3 / 原审计 P1-6】连接重建时清空目录缓存。
        # 缓存记的是"这条连接上建过哪些目录"，依据的是这条连接的历史；
        # 换了连接就没有依据了（何况这中间管理员可能把目录删了）。
        # 清空的代价是重连后每条目录多做一次 MKD（服务器对已存在目录回 550，
        # ensure_remote_dir 本来就忽略它），换来的是"绝不因缓存而漏建目录"——
        # 漏建的后果是该目录下**所有**文件上传失败。
        self._known_dirs.clear()
        self._last_ok_at = time.monotonic()

    def close(self):
        helper, self._helper = self._helper, None
        self._known_dirs.clear()
        if helper is not None:
            helper.disconnect()         # 幂等

    def __enter__(self):
        self.connect()
        return self

    def __exit__(self, exc_type, exc, tb):
        self.close()
        return False

    @property
    def connected(self):
        helper = self._helper
        return (helper is not None and helper.ftp is not None
                and helper.ftp.sock is not None)

    def ensure_connected(self):
        """每次操作前的健康检查：断了就重连。"""
        if not self.connected:
            # 【阶段3-修正】首次连接与"断了重连"必须分开写日志。
            # 池是按需连接的（acquire 时还不连），于是每个 worker 的第一次操作
            # 都会走到这里；若不加区分，N 个 worker 一开跑就刷 N 条"[重连]"，
            # 事后查日志根本分不清"真的断过"与"这是它第一次连"——
            # 而"这条链路稳不稳"恰恰是阶段3 最需要从日志里看出来的东西。
            if self._helper is None:
                self.connect()
            else:
                self._log("[重连] 连接不可用，正在重建…")
                self.close()
                self.connect()
                self.reconnect_count += 1
            return
        if time.monotonic() - self._last_ok_at > config.FTP_HEALTHCHECK_IDLE_S:
            try:
                self._helper.ftp.voidcmd('NOOP')
            except Exception as e:
                self._log(f"[重连] NOOP 无应答（{e}），判定连接已断，正在重建…")
                self.close()
                self.connect()
                self.reconnect_count += 1

    # ---------------------------------------------------------- 断线分类
    # 【阶段3-新增｜重要】为什么不能照单全收地捕 OSError：
    # 本地文件错误抛的**也是** OSError（源文件被删→FileNotFoundError、
    # 权限不足→PermissionError、磁盘满→ENOSPC）。它们跟"FTP 连接断了"毫无关系。
    # 一起吃进去会出两个后果：
    #   ① 一个坏文件要白等 1+3+9=13 秒（还重放 4 次）；
    #   ② 破坏"上传路径任一文件失败即中止整个任务"这条既有语义——
    #      2c 的用例 I 正是靠注入 IOError 来验证它，重连重放会把注入的那次失败
    #      悄悄吃掉（第二次调用就成功了），用例会从"应中止"变成"应成功"。
    # 所以判定分两层：先排掉**业务性**应答（error_perm = 服务器明确拒绝，连接是好的），
    # 再要求剩下的 OSError 必须带"链路指征"（ConnectionError/TimeoutError/
    # 白名单 errno/winerror）才当断线。
    @staticmethod
    def _is_transport_error(e):
        if isinstance(e, ftplib.error_perm):
            return False                    # 5xx：服务器听懂了并拒绝了，连接没问题
        if isinstance(e, (ftplib.error_temp, ftplib.error_proto,
                          ftplib.error_reply, EOFError, ConnectionError,
                          TimeoutError, socket.timeout)):
            return True
        if isinstance(e, OSError):
            if e.errno in config._TRANSPORT_ERRNOS:
                return True
            return getattr(e, 'winerror', None) in config._TRANSPORT_WINERRORS
        return False

    # ---------------------------------------------------------- 重连重放
    def _with_reconnect(self, op, *args, cleanup_remote=None, **kw):
        """包装单个 FTP 操作：链路故障 → 断开 → 退避 → 重连 → 清理残留 → 重放。

        op 必须是**每次调用时才去取 self._helper** 的方法（见下面的 _op_*），
        不能在调用前先把绑定方法取出来——那样重连之后重放的还是那条已经关掉的连接。

        cleanup_remote：重放前必须删掉的远端残留路径（上传的半截 .part）。
        """
        last = None
        # 【阶段二】次数从"写死读常量"改成"调用方传了就用调用方的，没传就是常量"。
        # `or` 而不是三元判断：0 与 None 都要落到常量上 —— 界面上"重试 0 次"的语义是
        # **不重试**（试一次就放弃），它由调用方传成 1 来实现（见 file_transfer 的
        # _retry_attempts_value），而不是在这里传 0 把循环变成"一次都不试"。
        attempts = self._reconnect_attempts or config.FTP_RECONNECT_ATTEMPTS
        for i in range(attempts):
            if i:
                time.sleep(config.FTP_RECONNECT_BACKOFF_S[
                    min(i - 1, len(config.FTP_RECONNECT_BACKOFF_S) - 1)])
            try:
                self.ensure_connected()
                if i and cleanup_remote:
                    # 【阶段3-必须做】重放 STOR 之前先删掉上一条连接留下的半截 .part。
                    # 原因：部分服务器的 STOR 对已存在路径是**续写/覆盖写**而不是截断，
                    # 不清掉就会得到"前半截旧内容 + 后半截新内容"的拼接文件；
                    # 而 SIZE 校验查的是长度——长度对得上，内容是错的。
                    # 这种文件比"缺文件"更危险：阅片端会把它当成一张完整的片子读。
                    self._delete_quietly(cleanup_remote)
                result = op(*args, **kw)
                self._last_ok_at = time.monotonic()
                if i:
                    self._log(f"[重连] 已重连并重放成功（第 {i} 次重试）")
                return result
            except Exception as e:
                if not self._is_transport_error(e):
                    raise               # 业务错误原样上抛，不重连、不退避
                last = e
                self._log(f"[重连] 操作因链路故障失败：{e}")
                self.close()
        raise last

    def _delete_quietly(self, remote_path):
        try:
            self._helper.ftp.delete(remote_path)
        except Exception:
            pass                        # 删不掉（本来就不存在/服务器不支持）都不该中断重放

    # ---------------------------------------------------------- 操作（迟绑定）
    def _op_download(self, remote_path, local_path, mtime=None):
        self._helper.download_file(remote_path, local_path, mtime)

    def _op_upload(self, local_path, remote_path):
        self._helper.upload_file(local_path, remote_path)

    def _op_size(self, remote_path):
        try:
            return self._helper.ftp.size(remote_path)
        except ftplib.error_perm:
            return None                 # 550：服务器说没有这个文件，合法应答，不是断线
        except Exception:
            return None                 # 其余一律"量不出来"→ 上层按不可信处理

    def _op_mkd(self, remote_dir):
        self._helper.ensure_remote_dir(remote_dir)

    def _op_list(self, remote_dir):
        return self._helper.list_files_recursive(remote_dir)

    def _op_cwd(self, path):
        self._helper.ftp.cwd(path)

    def _op_list_dir(self, path):
        return self._helper.list_dir(path)

    def _op_delete(self, remote_path):
        self._helper.ftp.delete(remote_path)

    # ---------------------------------------------------------- 对外接口
    def download_file(self, remote_path, local_path, mtime=None):
        self._with_reconnect(self._op_download, remote_path, local_path, mtime)

    def upload_file(self, local_path, remote_path):
        self._with_reconnect(self._op_upload, local_path, remote_path,
                             cleanup_remote=remote_path + config.UPLOAD_PART_SUFFIX)

    def size(self, remote_path):
        return self._with_reconnect(self._op_size, remote_path)

    def cwd(self, path):
        return self._with_reconnect(self._op_cwd, path)

    def list_files_recursive(self, remote_dir):
        return self._with_reconnect(self._op_list, remote_dir)

    def list_dir(self, path):
        """【阶段4-新增】单目录列举 + 重连重放。

        可以安全重放的前提由 list_dir 的返回时机保证：**一次列举在吐任何东西之前完成**，
        所以重放不会产生重复条目。整棵树的 list_files_recursive 就没法这样——
        它断了只能从头重扫，而重扫出来的文件与已经交出去的那些无法区分。
        """
        return self._with_reconnect(self._op_list_dir, path)

    def delete_file(self, remote_path):
        """【阶段5-3-新增】删一个远端文件 + 重连重放。目前只有 .part 回收用它。

        走与其他操作同一套 _with_reconnect。文件不存在时服务器回 550 →
        `_is_transport_error` 对 error_perm 判 False → **原样上抛不重连**
        （链路是好的，是文件没了），由调用方当作"这一个删不掉"记下来继续。
        """
        return self._with_reconnect(self._op_delete, remote_path)

    def iter_files_recursive(self, remote_dir, dir_counter=None,
                             prune=None, on_visit=None):
        """【阶段4-新增】带重连重放的流式遍历：逐目录 yield 五元组。

        【阶段一 Bug2】`dir_counter` 同 FTPHelper.iter_files_recursive：可选、透传。

        【P0-1】`prune` / `on_visit` 同 FTPHelper：可选、透传，本类不判。
        重连重放会让**同一个目录**被列举两次（断线后重列当前目录），于是一次重连
        会让计数多 1。这是可接受的：它是个"死活指示器"，不是对账用的数字；
        宁可多报一个目录，也不要为了精确去和重放逻辑纠缠。

        与 list_files_recursive 的关键差别是**重连粒度**：那个把整棵树塞给一次
        _with_reconnect（断了重扫整棵），这个把粒度压到单个目录（断了只重列当前目录）。
        传进来的 remote_dir 由 _ftp_probe_remote_dir 给过，已经是探测成功的绝对路径，
        所以这里不再回退"去斜杠"那一套（那是 list_files_recursive 试探入口时的事）。
        """
        base = '/' + str(remote_dir or '/').strip().replace('\\', '/').lstrip('/')
        for item in config._walk_remote_files(self.list_dir, base, dir_counter,
                                              prune=prune, on_visit=on_visit):
            yield item

    def ensure_remote_dir(self, remote_dir):
        """递归建远端目录，带 _known_dirs 缓存（原审计 P1-6）。

        上传路径原本是**每个文件**都把整条目录链 MKD 一遍：20 万文件、每个 3 层目录
        = 60 万次往返，全是明知已存在的重复动作。缓存后同一目录整条任务只做一次。
        缓存不参与正确性判定：命中就直接返回；未命中就照做一遍再记上（MKD 对已存在
        目录报 550，被 ensure_remote_dir 忽略，所以"多建一次"永远不会出错）。
        """
        key = '/' + str(remote_dir).strip().replace('\\', '/').lstrip('/')
        if key in self._known_dirs:
            return
        self._with_reconnect(self._op_mkd, remote_dir)
        self._known_dirs.add(key)
        if len(self._known_dirs) > config.FTP_KNOWN_DIRS_MAX:
            self._known_dirs.clear()
            self._known_dirs.add(key)


class FTPConnectionPool:
    """FTP 连接池：预建 size 条连接，谁用谁借，借完还回。

    【阶段3-不做的事】不做空闲回收、不做超时踢人、不做按需扩容。
    本工具的连接生命周期就是**一次任务**：任务开始建池，任务结束 close_all()。
    加上这三样只会引入"连接被池回收而 worker 还在用"这一整类新故障面，
    而它们要解决的问题（长驻服务的连接老化）在这里根本不存在。
    借不到就阻塞而不是新建：新建会让实际并发超过用户在界面上设的线程数。
    """

    def __init__(self, host, user, password, port=21, size=config.FTP_POOL_DEFAULT_SIZE,
                 timeout=10, log_callback=None, reconnect_attempts=None):
        self.size = max(1, int(size))
        self._log_cb = log_callback
        self._free = queue.LifoQueue()
        for _ in range(self.size):
            # 【阶段二】reconnect_attempts 是**关键字**传的：上面的位置参数顺序
            # （host, user, password, port, timeout, log_callback）是阶段3 定下的，
            # 池里每一条连接都从这同一个入口出去，顺序一动就是全线错位。
            self._free.put(FTPPooledConn(host, user, password, port, timeout,
                                         log_callback,
                                         reconnect_attempts=reconnect_attempts))
        self._closed = False

    def acquire(self):
        return self._free.get()

    def release(self, conn):
        if conn is None:
            return
        if self._closed:
            try:
                conn.close()
            except Exception:
                pass
            return
        self._free.put(conn)

    def close_all(self):
        """收尾。只关当前空闲的那些；还借在外面的由各自的 release() 关掉。"""
        self._closed = True
        while True:
            try:
                conn = self._free.get_nowait()
            except queue.Empty:
                break
            try:
                conn.close()
            except Exception:
                pass
