# -*- coding: utf-8 -*-
r"""定时任务页（`SchedTab`）—— 从 `FileTransferApp` 里切出来的第 6 个真模块（**5b**）。

【它管什么】**第三个页签「定时任务」的全部界面与交互**（`_nightly\55` 的分层：L6）：
  · 任务列表（一句话一条：名字 / 触发规则 / 启停 / 注册状态）、选中详情、命令行预览与复制
  · 新建 / 编辑 / 删除 / 启用·停用 / 全部重新注册 / 刷新注册状态
  · 启动自检（把"上次跑得怎么样"打进日志、后台查一遍注册状态并在挪位时挂提示条）
  · 同步点标签的 400ms 防抖刷新（日期分层增量）
  · `schtasks` 封装的**全部基础设施**（跑 / 拼 argv / 查注册 / 补设置 / 解码控制台输出）

【层级】**L6** —— 只 import L0（`config`）+ `ui_dialogs`；不认识 `FileTransferApp`
  （它只拿到一个**抽象可写句柄** `_HostSchedSink`）⇒ 不存在 L6→L7 的反向依赖。

【装配】宿主把 4 个跨层对象**同名同对象**注入（`root` / `_core` / `_launch_argv_prefix` /
  `_headless_task`）；`_ui` **延迟补挂**（`attach_ui`）—— 因为宿主 `_ui` 的 handlers 表里
  有一项 `"sched_check": self._sched._sched_check_done`（构造期就要取到）⇒ 与"本类要用 `_ui`"
  构成**循环依赖**（真缺陷，见 `__init__` 的注释）。24 个界面名字走 property 代理
  （**只读 20 / 可写 4**），读写两向都落回宿主**同一个**属性槽。

【⚠️ `_launch_argv_prefix` 为什么是**注入**而不是随函数搬走（雷A）】
  它返回 `os.path.abspath(__file__)` —— 那个值是"**本程序**该被怎么拉起来"的入口脚本，
  计划任务的 `/TR` 就靠它。函数若搬进本模块，`__file__` 会变成 `sched_tab.py` ⇒
  计划任务去跑 `sched_tab.py` ⇒ **到点静默不干活**（界面上看不出任何异常）。
  它是**宿主知识**（`sys.frozen` 判定也是宿主的）⇒ 留在宿主，这里只收一个 callable。
"""
import datetime
import locale
import os
import re
import subprocess
import tempfile
import threading
import tkinter as tk
from tkinter import messagebox

import config
import ui_dialogs


# `last_status` → 一句人话。**取值域由 config.SCHEDULE_STATUSES 定**，
# 这里只是给每一档一个中文说法；多出来的档位在界面上显示原始英文，不猜。
_SCHED_STATUS_CN = {
    "ok": "成功",
    "partial": "部分失败",
    "failed": "完全失败",
    "aborted": "被中止（不算成功）",
    "skipped": "让路（撞上另一个正在跑的任务）",
    "error": "起不来（配置或路径错）",
}


def _format_trigger(trig):
    """触发规则 → 一句人话（日志头用）。

    【为什么开头要挡一道非 dict】唯一调用点写的是 `task.get("trigger") or {}`
      —— 作者当时就知道这里可能拿到 `None`。把 `or {}` 留在调用点、而不是收进
      本函数，意味着**下一个调用点必须自己想起来**加它，忘了就是日志头这一行
      把整个任务带崩（而这行日志恰恰是"任务为什么没跑"的唯一线索）。
      收进来之后这个函数是**全函数**（任何输入都有一句话），调用点可以裸调。
    """
    if not isinstance(trig, dict):
        return "未知触发规则"
    t = trig.get("type")
    if t == "daily":
        return "每天 %s" % trig.get("time", "?")
    if t == "weekly":
        return "每周 %s 的 %s" % (",".join(trig.get("days") or []), trig.get("time", "?"))
    if t == "hourly":
        return "每 %s 小时（起点 %s）" % (trig.get("every_hours", "?"), trig.get("time", "?"))
    if t == "once":
        return "单次 %s %s" % (trig.get("date", "?"), trig.get("time", "?"))
    return "未知触发规则"

def _parse_run_task_arg(argv):
    """从命令行里认出 `--run-task <名字>`。认不出返回 None。

    【为什么手写解析，不引 `argparse`】本项目至今**零第三方依赖**
    （除了 ttkbootstrap），而 `argparse` 是标准库、引它不算破例 —— 真正的原因是
    这个入口只需要认**一个**参数，而 `argparse` 会把"不认识的参数"变成
    `SystemExit(2)` **并往 stderr 打一段用法**（打包 `console=False` 时那段没人看得见）。
    更实际的一条：这个 exe 平时是双击启动的（`argv` 为空），
    手写解析能明确地做到"**没有 `--run-task` 就一切照旧**"，一眼看得出。
    形态与 `run_all.py` 里那段命令行解析同源。

    【同时写两种写法】`--run-task=名字` 与 `--run-task 名字` 都收。
    第一种是给"自己手敲一次试试"用的（少一层引号），第二种是 schtasks 写进去的形态。
    """
    args = list(argv)
    for i, a in enumerate(args):
        if a == "--run-task":
            if i + 1 < len(args) and args[i + 1].strip():
                return args[i + 1].strip()
            return ""            # 给了开关没给值：也是"要无头"，只是名字空 → 找不到任务
        if a.startswith("--run-task="):
            return a.split("=", 1)[1].strip()
    return None

#
# 【这一节是"改用户系统"的唯一出口】整个项目里只有这里会去动任务计划程序。
#   所以三条规矩写死在这里，别处不许自己拼 subprocess：
#     ① **一律 argv 列表 + `shell=False`** —— 命令行的拼装全在 `config.schtasks_*`
#        那几个函数里，它们产出的是列表。没有 shell 就没有注入面。
#     ② **一律 `CREATE_NO_WINDOW`** —— 本程序平时是 GUI 进程，
#        跑子进程时若不加这个标志，用户每次点「注册」都会**闪一次黑框**。
#     ③ **一律带超时** —— schtasks 卡住（服务没起来、凭据管理器弹窗）时，
#        界面会连带卡死；有超时至少能报一句"它没在 30 秒内回来"。
#
# 【为什么不放在 config.py】那个模块的定位是"常量 + 纯函数 + 读写文件"，
#   它不许 import `subprocess`（那会让"读一个常量"都可能起进程）。
#   命令行**怎么拼**是纯字符串问题，住 config；**怎么跑**是进程问题，住这里。
def _decode_console(b):
    """解码 Windows 控制台输出。

    【为什么不写死 utf-8】`schtasks` 是按**控制台代码页**输出的 —— 简体中文
    Windows 上是 GBK。「本工具全是 UTF-8」这条直觉在这里不成立：
    写死 utf-8 会让所有中文任务名变成一串 `锟斤拷`，而错误信息里最要紧的
    恰恰就是那个任务名。
    """
    if not b:
        return ""
    for enc in (locale.getpreferredencoding(False), "gbk", "utf-8"):
        if not enc:
            continue
        try:
            return b.decode(enc)
        except (UnicodeDecodeError, LookupError):
            continue
    return b.decode("utf-8", "replace")

def _schtasks_run(args, timeout=30):
    """跑一次 schtasks。返回 `(rc, 输出文本)`；`rc is None` = 根本没跑起来。

    【为什么 rc=None 要单独一档】"schtasks 说这个任务不存在"（rc=1）与
    "schtasks 这个程序根本没跑起来"（超时 / 被策略拦住）在界面上必须是两种说法：
    前者是"这条任务没注册"，后者是"我没能问出结果"。
    混成一句的话，用户会照着"没注册"去排查一个其实注册得好好的任务。
    """
    try:
        p = subprocess.run(args,
                           stdin=subprocess.DEVNULL,
                           stdout=subprocess.PIPE,
                           stderr=subprocess.STDOUT,
                           timeout=timeout,
                           shell=False,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        return p.returncode, _decode_console(p.stdout)
    except subprocess.TimeoutExpired:
        return None, "schtasks 在 %d 秒内没有返回" % timeout
    except Exception as e:
        return None, "%s: %s" % (type(e).__name__, e)

def _sched_create_args(rec, launch_prefix):
    """一条任务记录 → `/Create` 的 argv（**唯一**的拼装入口）。"""
    exe, script = launch_prefix()
    return config.schtasks_create_args(exe, rec, script)

def _join_argv(argv):
    """把 argv 拼成**给人复制粘贴**的那条命令行。

    【为什么用 `subprocess.list2cmdline`】它实现的正是 Windows 的引号规则
    —— 也就是 `cmd` / PowerShell 粘进去之后会切回**同一组参数**的那套规则。
    于是"界面上显示的命令"与"程序真正执行的那条"是**同一个函数的两种呈现**，
    不可能对不上。自己拼字符串就会：显示的少一个引号，用户复制过去跑出别的结果。
    """
    return subprocess.list2cmdline([str(a) for a in argv])

def _same_exe(target, exe):
    """`<Command>` 里的那个路径，是不是"当前这个程序"。"""
    try:
        return os.path.normcase(os.path.abspath(target)) == \
            os.path.normcase(os.path.abspath(exe))
    except Exception:
        return False

def _sched_last_text(rec):
    """一条任务的"上次运行"说成一句话。从没跑过时返回空串（调用方决定怎么说）。"""
    when = rec.get("last_run")
    if not when:
        return ""
    st = _SCHED_STATUS_CN.get(rec.get("last_status"), rec.get("last_status") or "?")
    c = rec.get("last_counts") or {}
    return "%s %s（成功 %s / 跳过 %s / 失败 %s）" % (
        when, st, c.get("copied", "?"), c.get("skipped", "?"), c.get("failed", "?"))

def _schtasks_query_target(name):
    r"""查一条任务**当前指向哪个可执行文件**。返回 `(state, text)`。

    `state` 三档：
      * `"ok"`      —— 查到了，`text` 是 XML 里的 `<Command>` 值；
      * `"missing"` —— schtasks 明确说没有这条任务；
      * `"error"`   —— 没问出结果（rc=None 或 XML 读不动），`text` 是原因。

    【为什么用 `/XML` 而不是默认那张表】默认输出是给人看的**定宽文本**，
    列宽会随系统语言和任务名长度变，`Task To Run` 那一列里还混着参数
    （`"...\文件传输工具.exe" --run-task 每日备份`）。从定宽文本里切出
    "可执行文件路径"这件事没有稳的写法。XML 有 `<Command>` 与 `<Arguments>`
    两个独立字段，`ElementTree` 是标准库 —— 这是唯一稳的取法。
    """
    rc, out = _schtasks_run(config.schtasks_query_args(name))
    if rc is None:
        return "error", out
    if rc != 0:
        # 【判"不存在"为什么不看返回码的数字】中英文系统上 schtasks 的返回码
        #   都可能是 1，而"拒绝访问"也是 1。判据取**输出里那句话**更稳：
        #   找不到时它会说"找不到" / "cannot find" / "ERROR: The system cannot find"。
        low = out.lower()
        if ("找不到" in out or "cannot find" in low or "不存在" in out
                or "does not exist" in low):
            return "missing", out
        return "error", out
    try:
        import xml.etree.ElementTree as ET
        root = ET.fromstring(out)
    except Exception as e:
        return "error", "读不懂 schtasks 的 XML：%s" % e
    for el in root.iter():
        if el.tag.endswith("Command") and el.text:
            return "ok", el.text.strip()
    return "error", "XML 里没有 <Command> 字段"

def _sched_ensure_settings(name):
    r"""给**已经注册好**的任务补上 `config.SCHEDULE_EXTRA_SETTINGS` 那几条设置。

    返回 `(成功与否, 说明)`。这里的"成功"指的是**"这几条设置确实在系统里生效了"**，
    而不是"我把命令跑出去了" —— 所以最后一定**读回 XML 核对**，
    不看 `/Create` 的返回码（它只说"我收到了"）。

    【为什么必须"注册之后再补一次"，不能一次 `/Create` 到位】
    `schtasks` 的命令行没有这些设置的开关，理由见
    `config.schtasks_create_xml_args` 的 docstring。
    用**系统自己生成的那份** XML 当模板、而不是自己拼一份：XML 里带着本机专有的
    东西（`<Principal>` 的登录方式、`<UserId>` 的 SID），自己拼就得逐个处理；
    读回现成的只改一个元素，其余原样还回去。

    【⚠️ 编码这条坑 —— 实测过，别照直觉写】
    `schtasks /Query /XML` 打到管道里的字节是**控制台代码页**（本机 cp936/GBK），
    而 XML 声明里写的是 `encoding="UTF-16"` —— 这两件事**对不上**。
    更坑的是：**拿一个全 ASCII 的任务名去试，会得出"这是 UTF-8"的错误结论**
    （ASCII 天然也是合法 UTF-8）；**必须拿中文任务名试**才看得见
    （本机实测：严格 utf-8 在 `0xd5` 处就解不开）。
    ⇒ 读一律走 `_decode_console`（它就是按控制台代码页解的那个，顺序**是对的**）；
      写的时候把**声明改写成 UTF-16**、字节也用 UTF-16 —— 让"声明"与"字节"
      由我们自己保证一致，不去猜 schtasks 到底发的是什么。
    全过程见 `_checks\_nightly\47_step_sched_settings.md`。

    【已经有这几条时直接返回 —— 不白跑两次 schtasks】"保存即生效"走的就是这条路。
    """
    rc, out = _schtasks_run(config.schtasks_query_args(name))
    if rc is None:
        return False, out
    if rc != 0:
        return False, (out or "schtasks 返回 %r" % (rc,)).strip()

    def _ok_in(text):
        return [k for k, v in config.SCHEDULE_EXTRA_SETTINGS
                if ("<%s>%s</%s>" % (k, v, k)) not in text]

    missing = _ok_in(out)
    if not missing:
        return True, ""

    text = out
    for k, v in config.SCHEDULE_EXTRA_SETTINGS:
        if ("<%s>%s</%s>" % (k, v, k)) in text:
            continue
        text = config.schtasks_patch_schedule_xml(text, k, v)
        if text is None:
            return False, "读回来的 XML 里没有 </Settings>，改不动"

    text = re.sub(r'encoding="[^"]*"', 'encoding="UTF-16"', text, count=1)

    fd, path = tempfile.mkstemp(prefix="ftp_sched_", suffix=".xml")
    try:
        with os.fdopen(fd, "w", encoding="utf-16", newline="") as f:
            f.write(text)
        rc2, out2 = _schtasks_run(config.schtasks_create_xml_args(name, path))
        if rc2 != 0:
            return False, (out2 or "schtasks /Create /XML 返回 %r" % (rc2,)).strip()
    finally:
        try:
            os.remove(path)
        except OSError:
            pass

    # 【只认"系统收成了什么"】`/Create` 的返回码只说它收到了。
    rc3, out3 = _schtasks_run(config.schtasks_query_args(name))
    if rc3 != 0:
        return False, "改完之后读回来核对失败：%s" % (out3 or "rc=%r" % (rc3,)).strip()
    still = _ok_in(out3)
    if still:
        return False, "改完读回来，这几条**没生效**：%s" % "、".join(still)
    return True, ""


class SchedTab(object):
    r"""定时任务页（L6）。装配与"同一份"的保证见模块 docstring 与 `_HostSchedSink`。"""

    def __init__(self, host, root, core, launch_prefix, headless_task):
        # 【装配】`host` 是**可写句柄**（`_HostSchedSink`，白名单 + 属性代理回宿主），
        #   不是宿主对象本身 ⇒ L6 不认识 `FileTransferApp`。
        self._host = host
        # 【同名同对象注入】下面几个**逐字同名** ⇒ 26 个方法体里对应的行**一字不用改**，
        #   而且读的是**同一个对象**（天然实时，不存在"按值快照不更新"）。
        self.root = root
        self._ui = None
        self._core = core
        self._launch_argv_prefix = launch_prefix
        self._headless_task = headless_task
        # ⚠️ `_ui` **不在这里收**（上面写的是 `None`）：宿主 `_ui` 的 handlers 表里
        #   有一项是 `"sched_check": self._sched._sched_check_done` ⇒ `_sched` 必须
        #   先于 `_ui` 建好；而本类又要用 `_ui`（`_update_log`）⇒ **循环依赖**。
        #   解法与 `_core` / `_ftp` / `_maint` **同一套**：`_ui` 建好之后由宿主调
        #   `attach_ui()` 补挂（不新发明第二种补挂方式）。
        # 【雷A 的落点】`_launch_argv_prefix` **不搬**（`__file__` 是宿主知识）⇒
        #   注入成 callable；L6 里 2 处调用点已改成 `self._launch_argv_prefix()`，
        #   `_sched_create_args` 因此多收一个 `launch_prefix` 形参。
        # 【`_headless_task` 按值注值的**前提**】宿主 `__main__` 里
        #   `_HEADLESS_TASK = _headless_name` 排在 `FileTransferApp(root)` **之前**
        #   （`file_transfer.py:3127` vs `:3136`）⇒ 构造那一刻值已经就位。

    def attach_channel(self, ui):
        r"""补挂 `_ui`（构造时它还不在 —— 循环依赖见 `__init__` 的说明）。

        与 `TransferCore.attach_host_widgets` / `FtpTransfer.attach_ui` /
        `MaintenanceWorker.attach` **同一套**：**只许挂一次**，挂第二次当场断言
        （「响亮失败」优先于「静默覆盖」—— 与全项目同一纪律）。
        """
        assert self._ui is None, "`_ui` 已经挂过了 —— 只许挂一次"
        self._ui = ui

    # ---------- 只读 20 个：L6 方法体里 **0 处** `self.<名> = ` ----------
    #   故意**不给 setter**：读出去的就是宿主那**一个**对象 —— `self.X.get()` /
    #   `self.X.configure(...)` 都是"对**返回对象**做操作"，与 setter 无关；
    #   而万一将来有人在 L6 里写了 `self.X = ...`（重绑），**当场 AttributeError**，
    #   比"悄悄把宿主那个对象换成新的"（L7 侧挂在旧对象上的引用全失效）好得多。
    #   —— 与第 4 步 6b4 同一条纪律：「响亮失败」优先于「静默兼容」。
    @property
    def date_template(self):
        return self._host.date_template

    @property
    def db_keep_days(self):
        return self._host.db_keep_days

    @property
    def dest_path(self):
        return self._host.dest_path

    @property
    def dest_path2(self):
        return self._host.dest_path2

    @property
    def multi_dest(self):
        return self._host.multi_dest

    @property
    def file_filter(self):
        return self._host.file_filter

    @property
    def flatten(self):
        return self._host.flatten

    @property
    def ftp_timeout(self):
        return self._host.ftp_timeout

    @property
    def log_export_dir(self):
        return self._host.log_export_dir

    @property
    def log_level(self):
        return self._host.log_level

    @property
    def reconnect_count(self):
        return self._host.reconnect_count

    @property
    def retry_count(self):
        return self._host.retry_count

    @property
    def source_path(self):
        return self._host.source_path

    @property
    def thread_count(self):
        return self._host.thread_count

    @property
    def transfer_mode(self):
        return self._host.transfer_mode

    @property
    def source_list(self):
        return self._host.source_list

    @property
    def sched_banner(self):
        return self._host.sched_banner

    @property
    def sched_banner_label(self):
        return self._host.sched_banner_label

    @property
    def sched_cmd_text(self):
        return self._host.sched_cmd_text

    @property
    def sched_detail(self):
        return self._host.sched_detail

    @property
    def sched_listbox(self):
        return self._host.sched_listbox

    @property
    def sched_toggle_btn(self):
        return self._host.sched_toggle_btn

    # ---------- 可写 4 个：L6 方法体里 **有** `self.<名> = ` ----------
    #   setter **写回宿主同一个槽**（不是新建、不是自己存一份）⇒ 全局只有一份。
    @property
    def _sync_refresh_id(self):
        return self._host._sync_refresh_id

    @_sync_refresh_id.setter
    def _sync_refresh_id(self, value):
        self._host._sync_refresh_id = value

    @property
    def sched_checked_at(self):
        return self._host.sched_checked_at

    @sched_checked_at.setter
    def sched_checked_at(self, value):
        self._host.sched_checked_at = value

    @property
    def sched_status(self):
        return self._host.sched_status

    @sched_status.setter
    def sched_status(self, value):
        self._host.sched_status = value

    @property
    def sched_tasks(self):
        return self._host.sched_tasks

    @sched_tasks.setter
    def sched_tasks(self, value):
        self._host.sched_tasks = value


    def _sched_startup_report(self):
        """在日志区报一遍"上次定时任务跑得怎么样"。

        【为什么每一条都要报，而不是只报失败的】成功也是一条信息：
        用户打开工具、看到「上次定时任务「每日备份」：成功（成功 412 / 跳过 3 / 失败 0）」，
        就知道昨晚那件事办了。只报失败的话，"什么都没看到"同时意味着
        "跑了且成功"与"根本没跑"—— 这两件事对用户是完全不同的。
        """
        try:
            tasks = config.load_schedules()
        except Exception:
            return
        for rec in tasks:
            txt = _sched_last_text(rec)
            if not txt:
                continue
            self._ui._update_log("[定时任务] 上次「%s」：%s。日志：%s"
                             % (rec["name"], txt, rec.get("last_log") or "（无）"))

    def _schedule_syncpoint_refresh(self, *_a):
        """Tk 变量变了 → 400ms 之后刷一次同步点标签（**合并连打**）。

        为什么非要防抖：这几个变量都绑在用户的**输入**上，源路径敲 30 个字符就是
        30 次回调，而每一次都要开一次 SQLite 连接去查 `meta`。
        `after_cancel` 把上一次还没跑的那一个取消掉 —— 只有"停下来之后"那一次真的会跑。

        【为什么不用轮询】这一页的控件按铁律一次建好永不销毁，轮询会变成一根
        永久空转的 `after` 链，而它要盯的东西一年也变不了几次。
        """
        if self._sync_refresh_id is not None:
            try:
                self.root.after_cancel(self._sync_refresh_id)
            except Exception:
                pass
            self._sync_refresh_id = None
        try:
            self._sync_refresh_id = self.root.after(400, self._core._refresh_syncpoint_label)
        except Exception:
            # 窗口还没建好 / 正在销毁。标签晚一点由别的路径刷，这不是错误。
            self._sync_refresh_id = None

    # ---------- Tab3 的取数 ----------
    def _collect_task_params(self):
        """把**当前「传输」页上的设置**快照成一个 dict，作为任务的 `params`。

        【键名为什么与 `_apply_ux_state_dict` 一一对应】那边是唯一一份
        "哪些键能影响程序行为"的白名单。这里多写一个键，那边就会**静默丢掉**它
        —— 用户在界面上设了、任务跑起来不生效，且没有任何提示。
        所以这份清单是从 `_apply_ux_state_dict` 逐行抄下来的，一个不多一个不少。
        """
        return {
            "source_path": self.source_path.get(),
            "source_list": list(self.source_list),
            "dest_path": self.dest_path.get(),
            # 【功能 3-4】与白名单逐字对应。定时任务大多无人值守，
            #   漏一个键的症状正是"界面上勾了第二目标、夜里跑起来只写了一份"。
            "multi_dest": bool(self.multi_dest.get()),
            "dest_path2": self.dest_path2.get(),
            "transfer_mode": self.transfer_mode.get(),
            "file_filter": self.file_filter.get(),
            "thread_count": int(self.thread_count.get()),
            "flatten": bool(self.flatten.get()),
            "ftp_timeout": int(self.ftp_timeout.get()),
            "retry_count": int(self.retry_count.get()),
            "reconnect_count": int(self.reconnect_count.get()),
            "db_keep_days": int(self.db_keep_days.get()),
            "log_level": self.log_level.get(),
            "log_export_dir": self.log_export_dir.get(),
            # 【P0-1】与 `_apply_ux_state_dict` 的 `date_template` 逐字对应 ——
            # 少了这一个键，定时任务就是"界面上设了日期分层、跑起来全量重扫"，
            # 而这条路**恰恰是无人值守的那条**，没人会看见它没生效。
            "date_template": self.date_template.get(),
        }

    def _params_preview(self, params):
        """把一份 params 说成两三行人话（对话框与详情行共用）。"""
        if not params:
            return "（没有取到任何参数）"
        mode = {"copy": "复制", "move": "移动"}.get(params.get("transfer_mode"), "?")
        src = params.get("source_path") or "（空）"
        n = len(params.get("source_list") or [])
        if n >= 2:
            src = "%s 等 %d 个来源" % (src, n)
        lines = ["源：%s" % src,
                 "目标：%s" % (params.get("dest_path") or "（空）"),
                 "方式：%s    线程：%s    过滤：%s"
                 % (mode, params.get("thread_count", "?"),
                    params.get("file_filter") or "（无）")]
        return "\n".join(lines)

    # ---------- Tab3 的刷新 ----------
    def _sched_reload(self, select=None):
        """从配置文件重读任务列表，重画列表框。`select` = 完成后选中哪个名字。"""
        self.sched_tasks = config.load_schedules()
        self._sched_redraw(select=select)

    def _sched_redraw(self, select=None):
        """按 `self.sched_tasks` + `self.sched_status` 重画列表与详情。"""
        self.sched_listbox.delete(0, tk.END)
        for rec in self.sched_tasks:
            self.sched_listbox.insert(tk.END, self._sched_row_text(rec))
        if select is not None:
            names = [r["name"] for r in self.sched_tasks]
            if select in names:
                i = names.index(select)
                self.sched_listbox.selection_clear(0, tk.END)
                self.sched_listbox.selection_set(i)
                self.sched_listbox.see(i)
        self._sched_update_detail()

    def _sched_row_text(self, rec):
        """一行 = 名字 + 触发规则 + 启停 + 注册状态。"""
        name = rec["name"]
        st = self.sched_status.get(name)
        if st is None:
            reg = "未检查"
        elif st[0] == "ok":
            reg = "已注册"
        elif st[0] == "missing":
            reg = "配置里有、系统里没有"
        else:
            reg = "查不到（%s）" % (st[1] or "").strip().splitlines()[:1]
        return "%-16s %-22s %s　[%s]　%s" % (
            name, _format_trigger(rec.get("trigger") or {}),
            "启用" if rec.get("enabled", True) else "停用",
            reg, _sched_last_text(rec))

    def _sched_on_select(self, _event=None):
        self._sched_update_detail()

    def _sched_selected_name(self):
        sel = self.sched_listbox.curselection()
        if not sel:
            return None
        i = sel[0]
        if i >= len(self.sched_tasks):
            return None
        return self.sched_tasks[i]["name"]

    def _sched_selected(self):
        n = self._sched_selected_name()
        if n is None:
            return None
        for r in self.sched_tasks:
            if r["name"] == n:
                return r
        return None

    def _sched_update_detail(self):
        rec = self._sched_selected()
        if rec is None:
            self.sched_detail.configure(text="（上面选一条任务，这里显示它的细节）")
            self._sched_set_cmd("")
            return
        self.sched_toggle_btn.configure(
            text="停用" if rec.get("enabled", True) else "启用")
        st = self.sched_status.get(rec["name"])
        reg_txt = {"ok": "已注册（指向 %s）" % (st[1] if st else ""),
                   "missing": "**系统里没有这条任务**",
                   "error": "注册状态查不到：%s" % (st[1] if st else ""),
                   None: "注册状态未检查"}[st[0] if st else None]
        self.sched_detail.configure(
            text="%s\n注册：%s\n上次运行：%s\n日志：%s\n%s"
                 % (self._params_preview(rec.get("params")),
                    reg_txt, _sched_last_text(rec) or "从未运行过",
                    rec.get("last_log") or "（无）",
                    "上次检查：%s" % (self.sched_checked_at or "还没检查过")))
        self._sched_set_cmd(self._sched_cmd_text_for(rec))

    def _sched_set_cmd(self, text):
        self.sched_cmd_text.configure(state="normal")
        self.sched_cmd_text.delete("1.0", tk.END)
        self.sched_cmd_text.insert("1.0", text)
        self.sched_cmd_text.configure(state="disabled")

    def _sched_cmd_text_for(self, rec):
        """这条任务"注册"或"删除"时真正会跑的那条命令（照实拼，不美化）。"""
        if rec.get("enabled", True):
            argv = _sched_create_args(rec, self._launch_argv_prefix)
        else:
            argv = config.schtasks_delete_args(rec["name"])
        return _join_argv(argv)

    # ---------- Tab3 的动作 ----------
    def _sched_new(self):
        dlg = ui_dialogs.ScheduleDialog(
            self.root, initial=None, params=self._collect_task_params(),
            params_preview=self._params_preview(self._collect_task_params()))
        self.root.wait_window(dlg)
        if dlg.result is None:
            return
        self._sched_save(dlg.result)

    def _sched_edit(self):
        rec = self._sched_selected()
        if rec is None:
            messagebox.showwarning("编辑定时任务", "请先在列表里选一条任务。")
            return
        params = self._collect_task_params()
        dlg = ui_dialogs.ScheduleDialog(
            self.root, initial=rec, params=params,
            params_preview=self._params_preview(params))
        self.root.wait_window(dlg)
        if dlg.result is None:
            return
        self._sched_save(dlg.result)

    def _sched_save(self, rec):
        """存一条任务 → 按启用状态同步系统计划任务。**保存即生效。**"""
        config.save_schedule(rec)
        self._sched_apply(rec)
        self._sched_reload(select=rec["name"])

    def _sched_toggle(self):
        rec = self._sched_selected()
        if rec is None:
            messagebox.showwarning("启用/停用", "请先在列表里选一条任务。")
            return
        new = dict(rec)
        new["enabled"] = not rec.get("enabled", True)
        config.save_schedule(new)
        self._sched_apply(new)
        self._sched_reload(select=new["name"])

    def _sched_delete(self):
        """删一条。**顺序是硬要求：先删系统、后删配置。**（`22_plan_stage4.md` §1③）

        反过来的话，`schtasks /Delete` 一失败就留下一颗**指向已删配置的孤儿任务**
        —— 它到点还会跑、还会报"任务名不存在"，而用户在界面上已经看不到它了。
        """
        rec = self._sched_selected()
        if rec is None:
            messagebox.showwarning("删除定时任务", "请先在列表里选一条任务。")
            return
        cmd = _join_argv(config.schtasks_delete_args(rec["name"]))
        if not messagebox.askyesno(
                "删除定时任务",
                "确定删除「%s」？\n\n"
                "这会从**系统任务计划程序**里删掉这条任务，并删掉本工具里的配置。\n"
                "已经跑过的日志文件不会被删。\n\n"
                "将执行的命令：\n%s" % (rec["name"], cmd), icon="warning",
                default="no"):
            return
        # ① 先删系统
        ok, why = self._sched_unregister(rec["name"])
        # ② 再删配置 —— **无论系统那一步成没成**都要删。
        #    不删的话用户会陷入"删不掉又跑不了"的死局：界面上那条任务还在，
        #    再点一次删除还是会先卡在同一个系统错误上。
        config.delete_schedule(rec["name"])
        self._sched_reload(select=None)
        if ok:
            self._ui._update_log("[定时任务] 已删除「%s」，系统里的计划任务也一并删掉了。"
                             % rec["name"])
        else:
            # 【失败绝不静默】这是文件头那条规矩的落点：系统里可能还留着一颗
            #   孤儿任务，而用户界面上已经看不到它了 —— 必须把"怎么收拾"写出来。
            messagebox.showwarning(
                "配置已删，但系统里的计划任务没删掉",
                "「%s」已从本工具里删除，但系统任务计划程序里可能还留着它。\n"
                "原因：%s\n\n"
                "请在一个**管理员** PowerShell 里粘这一条：\n\n%s"
                % (rec["name"], why or "（没问出原因）", cmd))
            self._ui._update_log("[警告] 「%s」的配置已删，但系统里的计划任务删不掉（%s）。"
                             "请手动执行：%s" % (rec["name"], why or "原因未知", cmd))

    def _sched_register_all(self):
        """把**当前列表里所有启用的任务**重新注册一遍（覆盖同名）。"""
        n_ok, n_bad, n_warn = 0, [], []
        for rec in self.sched_tasks:
            if not rec.get("enabled", True):
                continue
            ok, why = self._sched_register(rec)
            if not ok:
                n_bad.append((rec["name"], why))
                continue
            n_ok += 1
            if why:                      # 注册上了，但设置没写全（见 `_sched_register`）
                n_warn.append((rec["name"], why))
        self._sched_refresh_status()
        if n_bad:
            messagebox.showwarning(
                "部分任务没注册上",
                "成功 %d 条，失败 %d 条：\n\n%s"
                % (n_ok, len(n_bad),
                   "\n".join("· %s：%s" % (a, b) for a, b in n_bad)))
        if n_warn:
            messagebox.showwarning(
                "有任务注册上了、但有设置没写进去",
                "%d 条任务的「错过时间就补跑」没写进去：\n\n%s\n\n"
                "这些任务**到点会跑**，只是「到点时机器没开机 / 在睡眠」的那些天不补跑。\n"
                "想让它们也生效：用管理员身份重开本工具，再点一次「保存」。"
                % (len(n_warn), "\n".join("· %s：%s" % (a, b) for a, b in n_warn)))
        self._ui._update_log("[定时任务] 重新注册：成功 %d 条，失败 %d 条，设置没写全 %d 条。"
                         % (n_ok, len(n_bad), len(n_warn)))

    def _sched_apply(self, rec):
        """按 `enabled` 决定这条任务在系统里应该是"有"还是"没有"。"""
        if rec.get("enabled", True):
            ok, why = self._sched_register(rec)
            if ok and not why:
                self._ui._update_log("[定时任务] 已注册「%s」：%s"
                                 % (rec["name"], _format_trigger(rec.get("trigger") or {})))
            elif ok:
                # 【"注册上了"和"设置写进去了"是两件事，不能混着报】
                #   这里**不能**走下面那条 `else`：那条会说"还没被系统定时唤起"，
                #   而任务其实已经在系统里了 —— 用户照它去粘命令只会撞上"任务已存在"。
                messagebox.showwarning(
                    "任务已注册，但有一条设置没写进去",
                    "「%s」已经在系统计划任务里了，到点会跑。\n\n"
                    "没生效的是：\n%s\n\n"
                    "受影响的是**「到点时机器没开机 / 在睡眠」的那些天** ——\n"
                    "没有这一条，那些天就**不补跑**，而且不会有任何提示。\n\n"
                    "想让这一条也生效：用管理员身份重开本工具，再点一次「保存」。"
                    % (rec["name"], why))
                self._ui._update_log("[警告] 「%s」已注册，但有一条设置没生效（%s）。"
                                 % (rec["name"], why))
            else:
                # 【降级路径】注册失败**不算保存失败** —— 配置是工具自己的，
                #   不该因为一个系统动作失败而丢。把命令给出来让用户自己粘。
                messagebox.showwarning(
                    "配置已保存，但没能写进系统计划任务",
                    "「%s」的配置存好了，但它**现在还没被系统定时唤起**。\n"
                    "原因：%s\n\n"
                    "解决办法（二选一）：\n"
                    "  1) 用管理员身份重开本工具，再点一次「编辑…」→ 保存；\n"
                    "  2) 在一个**管理员** PowerShell 里粘这一条：\n\n%s"
                    % (rec["name"], why or "（没问出原因）",
                       _join_argv(_sched_create_args(rec, self._launch_argv_prefix))))
                self._ui._update_log("[警告] 「%s」的配置已存，但注册到系统失败（%s）。"
                                 % (rec["name"], why or "原因未知"))
        else:
            ok, why = self._sched_unregister(rec["name"])
            if not ok:
                # 停用时删不掉同理：**不静默**。
                self._ui._update_log("[警告] 「%s」已停用，但系统里的计划任务没删掉（%s）。"
                                 % (rec["name"], why or "原因未知"))

    def _sched_register(self, rec):
        """把这条任务写进系统计划任务。返回 `(成功与否, 说明)`。

        【两步：先建出来，再补设置】
          ① `/Create`     —— 建任务（触发类型、到点要跑的那条命令）；
          ② `/Create /XML` —— 补上「错过时间就补跑」（`config.SCHEDULE_EXTRA_SETTINGS`）。

        【⚠️ 第 ② 步失败时返回的是 `True`，不是 `False` —— 这里最容易写错】
        第 ① 步成功、第 ② 步失败时，**任务是真注册上了**，到点会跑；
        少掉的只是"错过补跑"。
        而调用方对 `False` 的说法是「配置已保存，但**它现在还没被系统定时唤起**」
        —— 那是句**假话**：会把用户指去用管理员重开、手粘命令，
        而他一粘就会撞上"任务已存在"。他真正该知道的是
        「任务在，只是不补跑」。
        ⇒ 所以第 ② 步失败**返回 `True`**，把原因放进第二个返回值。
          调用方见 `ok=True 且 why 非空` 时按**警告**显示，不按失败。

        【第二个返回值放的是**光秃秃的原因**，不套"已经注册上了，但…"】
        包一层的话，两个调用方各自的框里都会变成套娃 ——
        `_sched_apply` 的正文已经写着"没生效的是："，
        `_sched_register_all` 的标题已经写着"有任务注册上了、但有设置没写进去"。
        套上去就是「没生效的是：已经注册上了，但…没写进去：读回来的 XML 里没有
        </Settings>」——三句话在说同一件事。
        （这一条是 S9b 阳性对照的输出顶上来的：那句话以前一次都没被显示过。）
        """
        rc, out = _schtasks_run(_sched_create_args(rec, self._launch_argv_prefix))
        if rc != 0:
            return False, (out or "schtasks 返回 %r" % (rc,)).strip()
        ok, why = _sched_ensure_settings(rec["name"])
        if ok:
            return True, ""
        return True, why

    def _sched_unregister(self, name):
        """跑一次 `/Delete /F`。**任务本来就不存在算成功** —— 目的达到了。"""
        rc, out = _schtasks_run(config.schtasks_delete_args(name))
        if rc == 0:
            return True, ""
        st, _ = _schtasks_query_target(name)
        if st == "missing":
            return True, ""
        return False, (out or "schtasks 返回 %r" % (rc,)).strip()

    def _sched_refresh_status(self):
        """逐条查系统里注册成什么样了，刷新列表与顶部提示条。"""
        self.sched_status = {}
        exe = self._launch_argv_prefix()[0]
        moved = []
        for rec in self.sched_tasks:
            st, txt = _schtasks_query_target(rec["name"])
            self.sched_status[rec["name"]] = (st, txt)
            if st == "ok" and not _same_exe(txt, exe):
                moved.append(rec["name"])
        self.sched_checked_at = datetime.datetime.now().strftime("%H:%M:%S")
        # 【"工具已移动"的判据是**目标对不上**，不是"注册失败"】
        #   工具挪过位置之后，旧任务指向的那个 exe 可能**还在**（用户没删旧目录），
        #   于是 schtasks 那边一切正常、任务也照跑 —— 跑的是**旧版本**。
        #   这是最坏的一种：界面上看不出任何异常。
        if moved:
            self.sched_banner_label.configure(
                text="本工具已移动（或在别的机器上打开过）：有 %d 条任务仍指向旧位置"
                     "——%s。点右边可以一次性改到当前位置。「任务计划程序」里不会自动改，"
                     "必须你点这一下。" % (len(moved), "、".join(moved)))
            self.sched_banner.grid()
        else:
            self.sched_banner.grid_remove()
        self._sched_redraw(select=self._sched_selected_name())
        if self._headless_task is None:
            self._ui._update_log("[定时任务] 已检查 %d 条任务的注册状态%s。"
                             % (len(self.sched_tasks),
                                "，其中 %d 条指向旧位置" % len(moved) if moved else ""))

    def _sched_check_startup(self):
        """启动时在**后台**查一遍注册状态（不阻塞界面）。

        【为什么排到 after(1200)】与 `_start_db_maintenance` 同一个理由：
        启动那几百毫秒里界面正在上屏、状态库正在开、回填正在跑，
        这时候起一个要开子进程的线程只会跟它们抢。晚一点对它没有任何损失 ——
        它要报告的是"任务指向旧位置"这种事，晚 1.2 秒知道不会更糟。
        """
        if self._headless_task is not None:
            return                      # 无头下不起子进程，也无人在看提示条
        if not self.sched_tasks:
            return                      # 一条任务都没有：没必要起线程、开进程

        def _work():
            # 线程里只查（纯 schtasks 调用，不碰任何 Tk 控件），
            # 结果**整包**丢回主线程去上屏 —— 这是本项目唯一的跨线程规矩。
            status = {}
            exe = self._launch_argv_prefix()[0]
            moved = []
            for rec in self.sched_tasks:
                st, txt = _schtasks_query_target(rec["name"])
                status[rec["name"]] = (st, txt)
                if st == "ok" and not _same_exe(txt, exe):
                    moved.append(rec["name"])
            stamp = datetime.datetime.now().strftime("%H:%M:%S")
            self._ui._post_ui("sched_check", status, moved, stamp)

        threading.Thread(target=_work, daemon=True, name="sched-check").start()

    def _sched_check_done(self, status, moved, stamp):
        """`sched_check` 消息的主线程侧。"""
        self.sched_status = dict(status)
        self.sched_checked_at = stamp
        if moved:
            self.sched_banner_label.configure(
                text="本工具已移动（或在别的机器上打开过）：有 %d 条任务仍指向旧位置"
                     "——%s。点右边可以一次性改到当前位置。"
                     % (len(moved), "、".join(moved)))
            self.sched_banner.grid()
        else:
            self.sched_banner.grid_remove()
        self._sched_redraw(select=self._sched_selected_name())

    def _sched_copy_cmd(self):
        txt = self.sched_cmd_text.get("1.0", tk.END).strip()
        if not txt:
            messagebox.showinfo("复制命令", "上面还没有命令可复制 —— 先在列表里选一条任务。")
            return
        self.root.clipboard_clear()
        self.root.clipboard_append(txt)
        self._ui._update_log("[定时任务] 命令已复制到剪贴板。")
