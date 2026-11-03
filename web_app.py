# -*- coding: utf-8 -*-
r"""Web 宿主的**运行时**：装配（替身工具包 + 装配器）+ 双模主循环（C3 / C4）。

================================================================================
【这一层在整体里的位置】
================================================================================
    web_kit.py   替身工具包  —— 让**未改动的** Tk 建造代码在无 Tk 下跑通，
                             造出可序列化的界面模型（`web_kit.Widget`）。
    web_host.py  替身层      —— `VarStub` / `WidgetStub` / `SchedulerStub`，以及
                             本运行时用的 `WebRoot`（`tk.Tk()` 的替身）。
    web_app.py   本文件      —— 装配序列（`file_transfer.FileTransferApp(root)`）
                             与三档壳：
                               · `--tk`（真 Tk 窗口）：不装替身，直接 `tk.Tk()`，
                                 见 `run_tk()`；
                               · C3（pywebview）：宿主事件循环（主线程）；
                               · C4（本地 HTTP + 系统浏览器）：HTTP 服务在后台线程，
                                 主线程泵 `SchedulerStub.run_pending()`。
                             C3/C4 共用**同一份**前端与同一份桥（`WebBridge`）；
                             三档共用**同一份**传输内核与同一份装配序列。

================================================================================
【线程纪律（照抄 `web_host.SchedulerStub` 的约定，不是新造的）】
================================================================================
`ui_channel` 里有 8 处 `assert current_thread() is main_thread()`。
⇒ **一切触碰界面模型 / 业务对象的动作都在主线程做**。HTTP 线程（C4）与
pywebview 的 JS 回调线程都只做一件事：把请求**投递**给主线程（`WebBridge.call_on_main`），
然后等结果。`SchedulerStub.run_pending()` 只在主线程被调（它自己断言）。
"""
import json
import os
import sys
import threading
import time
import urllib.parse
import urllib.request

import config
import web_host
import web_kit

# 前端静态文件目录（纯 HTML/CSS/JS，无框架、无构建器 —— 见项目纪律）。
# 【打包用的一手】：单文件 exe 下 `.spec` 把 `web\` 作为 datas 打进来，运行时被
#   bootloader 解包到 `sys._MEIPASS\web\`。所以冻结态必须从 `_MEIPASS` 起算，
#   否则会去找 exe 旁边的 `web\`（那里什么都没有）⇒ 白屏。
if getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"):
    WEB_DIR = os.path.join(sys._MEIPASS, "web")
else:
    WEB_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "web")

# 默认端口：`host_probe` 自检通过后由 C4 使用。**只听 127.0.0.1**（钉死的纪律）。
DEFAULT_PORT = 8765

# 数值变量的**合法区间**：Tk 档是 Spinbox 的 from_/to，Web 档由 `/api/state` 发给前端，
# 保存设置时后端也拿这一份校验。**只有这一份** —— 以前它内联在 `_snapshot` 里，
# 保存接口要是另写一份，两边迟早对不上（改了一边，另一边还在按老区间放行/拒绝）。
_VAR_RANGES = {
    "thread_count": [1, 16],
    "ftp_timeout": list(config.TIMEOUT_RANGE_S),
    "retry_count": list(config.RETRY_COUNT_RANGE),
    "reconnect_count": list(config.RECONNECT_COUNT_RANGE),
    "db_keep_days": list(config.DB_KEEP_DAYS_RANGE),
}

# 设置页「保存默认参数」允许写的键。**白名单，不是黑名单**：前端送上来什么就写什么
# 等于把"任意变量都能改"这个口子交给一次 HTTP 请求。来源路径 / 传输模式这类
# 「传输」页的即时参数不在此列 —— 它们本来就该即时生效，且不属于"默认参数"。
_EDITABLE_SETTINGS = tuple(_VAR_RANGES) + ("log_level", "log_export_dir")

# 端口落点：正常启动（没有显式 `--port`）时写在这里，第二次双击 exe 靠它找到
# 已有实例并直接把浏览器/窗口指过去，而不是再起一个抢不到端口的进程。
_PORT_FILE = os.path.join(config.DATA_DIR, ".ftp_transfer_web_port")

# 原生对话框的标题 —— **逐字照抄 Tk 档那四处调用点**（改一个字符就是"和原来不一样"）：
#   transfer_form._pick_source_file   → 选择文件
#   transfer_form._pick_source_dir    → 选择文件夹
#   transfer_form._browse_dest        → 选择目标文件夹
#   ui_channel._pick_log_export_dir   → 选择日志默认导出目录
# 键是前端 `openPicker(mode)` 的 mode，与 `PICKER_TITLES` 那边一一对应。
_PICK_TITLES = {
    "source-file": "选择文件",
    "source-dir": "选择文件夹",
    "dest": "选择目标文件夹",
    "dest2": "选择第二个目标文件夹",
    "logdir": "选择日志默认导出目录",
}

# 同一时刻只让一个原生对话框存在（用户连点两下「浏览」时，第二条请求排队等第一条）。
# 没有它 = 两个 Tk root 同时创建，"第二套解释器"的 Tcl 状态会互相踩。
_PICK_LOCK = threading.Lock()


# =============================================================================
# 桥：界面模型 ↔ 前端
# =============================================================================
class WebBridge(object):
    r"""界面模型与前端的桥。

    【它负责三件事】
      1. **索引**：装配完成后，把散落在宿主与 `_ui` 上的替身控件 / 变量按**名字**
         收集起来（`start_btn` / `source_listbox` / `progress` / `log_text` …）——
         名字就是建造代码里 `self.<名>` 那个名字，前端据此寻址。
      2. **快照**（`snapshot`）：把当前界面状态序列化成 JSON 给前端渲染。
      3. **动作**（`invoke`）：把前端的动作翻译成对替身控件的调用（点按钮 =
         调它的 `command`，选列表项 = 改选中并触发绑定）。

    【为什么是"扫属性"而不是"手写一张名字表"】手写表会随建造代码漂移（加了控件
    忘了登记 ⇒ 前端点不动，且**不报错**）。扫属性则自动跟上：建造代码往 `self.*`
    放什么，桥就认什么。这与 `_HostFormSink` 的白名单正好互补 —— 那边管"L5 能碰哪些
    宿主属性"，这边管"前端能看见哪些控件"。
    """

    # 需要用户在**前端**点一下才算完的弹框（模态）；其余按"通知"处理。
    _ASK_KINDS = ("askyesno", "askokcancel", "askretrycancel", "askquestion")
    _ASK_DEFAULTS = {"askyesno": False, "askokcancel": False,
                     "askretrycancel": False, "askquestion": "no"}
    # 前端模态框的最长等待（秒）。到点回退保守默认（否/取消）—— 与"无人看管"时
    # Tk 也无从应答同义，且**绝不永久卡死主循环**。
    DIALOG_TIMEOUT_S = 300.0

    def __init__(self, app, root):
        self.app = app
        self.root = root
        self.widgets = {}          # 名字 → web_kit.Widget
        self.vars = {}             # 名字 → web_host.VarStub
        # 弹框流水（通知 + 待答），前端轮询取走渲染。
        self._msgs = []
        self._msg_seq = 0
        self._msg_lock = threading.Lock()
        self._msg_events = {}      # id → threading.Event（等前端答案）
        # C4（系统浏览器）没有"窗口已关闭"这种事件可用，只能靠前端报到/告别：
        # 每开一个页面发一次 hello（带 sessionStorage 里的随机 id），关页面时
        # `pagehide` 发一次 bye。最后一个页面 bye 之后开始计时，超时还没人回来
        # ⇒ 进程自行退出。没有这一步，关掉标签页后进程会一直挂在后台。
        self._clients = set()
        self._clients_lock = threading.Lock()
        self._empty_since = None
        self._seen_client = False          # 没见过 hello 就收到 bye？那是外来的，不算数
        if app is not None:
            self.attach(app)

    # ---- 索引 ----
    def attach(self, app):
        r"""把装配好的 `FileTransferApp` 挂上来并建索引。

        **为什么要分两步（`__init__` 时 app 允许为 None）**：弹框记录器必须在
        `FileTransferApp(root)` **之前**装好 —— `__init__` 里可能就有弹框（比如配置
        告警），那会儿桥还没建。⇒ 先建一个 `app=None` 的桥、装记录器、再构造 app、
        再 `attach`。见 `build()`。
        """
        self.app = app
        self.widgets = {}
        self.vars = {}
        self._collect()

    def _collect(self):
        sources = [("", self.app), ("", getattr(self.app, "_ui", None))]
        for prefix, obj in sources:
            if obj is None:
                continue
            for name, val in list(vars(obj).items()):
                if isinstance(val, web_kit.Widget):
                    self.widgets.setdefault(prefix + name, val)
        for name, val in list(vars(self.app).items()):
            if isinstance(val, web_host.VarStub):
                self.vars[name] = val

    # ---- 主线程投递 ----
    def call_on_main(self, fn, *args, **kwargs):
        r"""在**主线程**执行 `fn(*args)` 并等它返回。

        · 已经在主线程 ⇒ 直接调（否则会自己把自己锁死在等结果上）；
        · 否则 ⇒ `root.after(0, ...)` 投递，阻塞等结果（带超时）。
        超时抛 `TimeoutError` —— **响亮失败**（主循环没在跑 = 宿主坏了，
        静默返回空值会让前端显示一个"看起来正常"的假界面）。
        """
        if threading.current_thread() is threading.main_thread():
            return fn(*args, **kwargs)
        box = {}
        done = threading.Event()

        def _run():
            try:
                box["v"] = fn(*args, **kwargs)
            except Exception as e:                    # noqa: BLE001 — 回传给调用方
                box["e"] = e
            finally:
                done.set()

        self.root.after(0, _run)
        if not done.wait(5.0):
            raise TimeoutError("主循环没有在跑（WebBridge.call_on_main 超时）")
        if "e" in box:
            raise box["e"]
        return box["v"]

    # ---- 弹框通道（⑤ 剥离的落点：**接缝在替身层，不动 67 处生产代码**） ----
    def _dialog(self, name, title, message):
        r"""`web_kit.messagebox` 的记录器：把每一次弹框搬进前端。

        【为什么接在这里、而不是把 `transfer_core` 里 25 处 `messagebox.xxx` 逐处
        改成回调】全项目 67 处弹框（8 个模块）在 Web 档**都**经 `web_kit.messagebox`
        这一道咽喉（`web_kit.install()` 把各模块命名空间里的 `messagebox` 换成本对象）。
        在这里接一次 = 67 处全覆盖；逐处改写则是 67 处"改坏了不能自证"的改动，且会
        破坏 tk 档"输出逐字节相同"的判据（那 67 处的 monkeypatch 语义会变）。⇒ 这是
        一次**有据的工程取舍**，记入收官报告的"已知差异/决策"。

        通知类（info/warning/error）：只入流水，前端当 toast/通知显示，**不阻塞**。
        应答类（ask*）：入流水 + 挂一个 `threading.Event` 等前端点按钮。**逐字复刻
        Tk 的模态语义** —— Tk 的 `askyesno` 也是"阻塞并跑嵌套事件循环"，这里主线程
        阻塞时改为泵 `SchedulerStub.run_pending()`（同构），非主线程则 `Event.wait`。
        """
        with self._msg_lock:
            self._msg_seq += 1
            mid = self._msg_seq
            rec = {"id": mid, "kind": name, "title": title, "message": message,
                   "needs_answer": name in self._ASK_KINDS, "answer": None,
                   "ts": time.time()}
            self._msgs.append(rec)
            if len(self._msgs) > 200:                       # 只留最近 200 条
                del self._msgs[:len(self._msgs) - 200]
        if name not in self._ASK_KINDS:
            return None                                     # 通知：不拦，照默认
        ev = threading.Event()
        with self._msg_lock:
            self._msg_events[mid] = ev
        return self._await_dialog(mid, ev, name)

    def _await_dialog(self, mid, ev, name):
        default = self._ASK_DEFAULTS.get(name, False)
        deadline = time.monotonic() + self.DIALOG_TIMEOUT_S
        if threading.current_thread() is threading.main_thread():
            # 主线程：不能 `Event.wait`（那会停掉主循环，HTTP 线程再投递也没人跑）。
            # 泵调度器 = Tk 模态框的嵌套事件循环在 Web 档的等价物。
            while (not ev.is_set() and time.monotonic() < deadline
                   and not (self.root._closed or self.root._destroyed)):
                self.root.run_pending()
                time.sleep(0.01)
        else:
            ev.wait(self.DIALOG_TIMEOUT_S)
        with self._msg_lock:
            rec = next((r for r in self._msgs if r["id"] == mid), None)
            ans = rec["answer"] if rec and rec["answer"] is not None else default
            self._msg_events.pop(mid, None)
        if name == "askquestion":
            return "yes" if ans in (True, "yes", 1) else "no"
        return bool(ans)

    def answer_dialog(self, mid, answer):
        r"""前端点了应答类弹框的按钮 → 记下答案并叫醒等待方（可由 HTTP 线程调）。"""
        with self._msg_lock:
            rec = next((r for r in self._msgs if r["id"] == mid), None)
            if rec is None:
                raise KeyError("没有 id=%r 的弹框" % (mid,))
            rec["answer"] = answer
            ev = self._msg_events.get(mid)
        if ev is not None:
            ev.set()
        return True

    # ---- FTP 远程目录（前端"浏览"用；连真服务器、同步、只读） ----
    #
    # 【与 `fs_list` 一对】`fs_list` 管**本机**，这里管**远端**。Tk 档这两条路也是分开的：
    #   本机走 `filedialog.askdirectory`，远端走 `ui_dialogs.FTPBrowserDialog`
    #   （`transfer_form._handle_ftp_browse`）。Web 档没有系统对话框可开，
    #   所以远端这一半**必须**由后端真连服务器列目录 —— 否则 `dest_type=ftp`
    #   时点「浏览…」只能选到本机目录，那正是"选了 FTP 却选不了 FTP 文件夹"。
    #
    # 【为什么解析不另写一份】列目录的 MLSD/LIST 解析（含中文、DOS/Unix 两种格式）
    #   已经住在 `ftp_client.FTPHelper.list_dir` 里，而且**被生产传输路径天天用**。
    #   这里再抄一份正则 = 第二份真相（迟早漂移，症状是"浏览看得见、传的时候找不到"）。
    def _ftp_cfg(self):
        r"""取「当前激活的那一套」FTP 配置；一份都没有时给 None。

        【为什么不是"列表里选中的那一套"】Tk 档的 ⚙/浏览读的就是 `ftp_source_config` /
        `ftp_dest_config`，它们由 `_apply_active_profile` 从**当前激活**那一套填。Web 档
        没有那两个内存槽位，所以按同一条口径取激活那套（取不到再退第一套）。
        【本方法必须跑在主线程】它读业务对象 `ftp_profiles` / `ftp_active`（与别处同规矩）。
        """
        rows = getattr(self.app, "ftp_profiles", []) or []
        active = getattr(self.app, "ftp_active", "") or ""
        for p in rows:
            if isinstance(p, dict) and p.get("name") == active:
                return dict(p)
        for p in rows:
            if isinstance(p, dict):
                return dict(p)
        return None

    @staticmethod
    def _norm_ftp_dir(path):
        path = (path or "").strip()
        if not path:
            return "/"
        if not path.startswith("/"):
            path = "/" + path
        if len(path) > 1:
            path = path.rstrip("/") or "/"
        return path

    def ftp_list(self, path=""):
        r"""列出一个**远端**目录：`{"path", "parent", "dirs", "files"}`。

        返回形状与 `fs_list` 对齐（前端同一个选择器画两遍），只多一个 `files`
        （远端能给每个文件报大小，本机那边不列文件）。
        """
        cfg = self.call_on_main(self._ftp_cfg)
        if not cfg:
            raise ValueError("还没有 FTP 配置。请先到「设置」页建一套。")
        host = (cfg.get("host") or "").strip()
        if not host:
            raise ValueError("这套 FTP 配置里没有服务器地址。")
        path = WebBridge._norm_ftp_dir(path or cfg.get("remote_dir") or "/")

        import ftp_client
        try:
            port = int(cfg.get("port") or 21)
        except (TypeError, ValueError):
            port = 21
        with ftp_client.FTPHelper(host, cfg.get("user") or "",
                                 cfg.get("password") or "", port, 10) as ftp:
            files, subdirs = ftp.list_dir(path)
        base = path.rstrip("/") or "/"
        parent = None if base == "/" else (base[:base.rfind("/")] or "/")
        return {
            "path": base,
            "parent": parent,
            "dirs": sorted([d for d in subdirs], key=str.lower),
            "files": sorted([{"name": f[0], "size": f[1]} for f in files],
                            key=lambda x: (x["name"] or "").lower()),
        }

    @staticmethod
    def ftp_test(fields):
        r"""「测试连接」。**逐字复刻** `ui_dialogs.FTPSettingsDialog._test_connection`
        的那套动作与那两句文案（用户看到的字必须与 Tk 档一致）：
        连接 → 登录 → 开 UTF-8 → `cwd(remote_dir)` → `nlst()`。
        【纯网络、不碰业务对象】⇒ 由 HTTP 线程直接调，不占主循环（连不上要等 10 秒）。
        """
        fields = fields or {}
        host = (fields.get("host") or "").strip()
        if not host:
            return {"ok": False, "message": "服务器地址不能为空"}
        raw_port = fields.get("port")
        if raw_port in (None, ""):
            raw_port = 21
        try:
            port = int(raw_port)
        except (TypeError, ValueError):
            return {"ok": False, "message": "端口必须是数字"}
        user = (fields.get("user") or "").strip()
        password = fields.get("password") or ""
        remote_dir = WebBridge._norm_ftp_dir(fields.get("remote_dir"))

        import ftp_client
        try:
            with ftp_client.FTPHelper(host, user, password, port, 10) as ftp:
                ftp.ftp.cwd(remote_dir)
                ftp.ftp.nlst()
        except Exception as e:                          # noqa: BLE001 — 逐字给用户看
            return {"ok": False,
                    "message": "无法连接到 FTP 服务器：\n%s" % e}
        return {"ok": True, "message": "FTP 连接测试成功！"}

    # ---- 源列表的文本入口（Tk 里是 `simpledialog.askstring` 那两项） ----
    def _add_source_text(self, payload):
        r"""把"手打/贴进来的一条来源"加进列表。

        与 `transfer_form._pick_source_manual` / `_pick_share_source` 逐字同源：
        FTP URL 原样收，其余 `os.path.normpath`；`share=True` 时若不是 UNC 只记一行提示
        **不拒绝**（用户拿它当"手工添加"用是合法的，见那两处的注释）。
        """
        path = (payload.get("path") or "").strip()
        if not path:
            return None
        if payload.get("share"):
            if not self.app._form._is_unc_path(path):
                self.app._ui._update_log(
                    "[提示] 这不是共享路径（应以 \\\\ 或 // 开头），将按**本地路径**处理：%s"
                    % path)
        return self.app._form._add_source(
            path if path.startswith("ftp://") else os.path.normpath(path))

    def _export_log_text(self):
        r"""Web 档的「导出日志」：把日志正文交给浏览器去下载。

        【为什么不走 `ui._export_log`】那一位的最后一跳是 `filedialog.asksaveasfilename`
        —— Web 档下 `filedialog` 是替身（`web_kit.WebFileDialog`），而它的记录器
        **从未被装上**（只有 `set_messagebox_recorder`，没有 `set_filedialog_recorder`）
        ⇒ 永远返回空串 ⇒ 用户点「导出日志」**什么都不发生、也不报错**。
        浏览器里对应的东西是「下载」（`<a download>` + Blob，会弹浏览器自己的保存框），
        所以正文由这里取、文件由前端落盘。

        【空日志那句为什么抄一份】同一句话在 `ui_channel._export_log` 里也有一份，
        但那一份的前面站着 `filedialog`（见上，走不通）。这里给出**同一句原文**，
        由前端当 toast 显示。探针 `_probe_web_parity.py` 会同时核对两边字面量一致，
        所以这份拷贝不会悄悄漂移。
        """
        body = self.app._ui.log_text.get("1.0", "end-1c")
        if not body.strip():
            return {"ok": False, "empty": True,
                    "error": "日志区是空的，没有可导出的内容。"}
        return {"ok": True, "text": body}

    # ---- 定时任务：新建 / 编辑 / 删除（Tab3 那三个按钮的 Web 侧落点）----
    def _sched_form(self, payload):
        r"""给前端那个"定时任务表单"取初值：原记录 + 参数预览。

        `initial` 是**整份原记录**（不只是 name/trigger）—— 因为 `config._clean_schedule`
        会把认不出的键丢掉，其中就有 `last_run` / `last_status` / `last_count` 那一组运行
        痕迹；保存时必须从原记录整份出发，否则"只改个时间"会顺手抹掉"上次运行：部分失败"
        （`ui_dialogs.ScheduleDialog._on_save` 的注释把这条写死了）。
        """
        sched = self.app._sched
        initial = {}
        if (payload.get("mode") or "") == "edit":
            rec = sched._sched_selected()
            if rec is None:
                raise ValueError("请先在列表里选一条任务。")
            initial = dict(rec)
        try:
            params = sched._collect_task_params()
            preview = sched._params_preview(params)
        except Exception:                               # noqa: BLE001 — 取不到预览不该拦住建表单
            params, preview = {}, ""
        return {"initial": self._jsonable(initial),
                "params": self._jsonable(params),
                "preview": preview,
                "types": [["daily", "每天"], ["weekly", "每周"],
                          ["hourly", "每隔 N 小时"], ["once", "单次"]],
                "day_codes": list(config.SCHEDULE_DAY_CODES)}

    def _sched_save_fields(self, payload):
        r"""把表单字段攒成一条记录并存盘。

        校验**不另写**：交给 `config._clean_schedule`（"什么样的任务是合法的"唯一一份答案，
        与 `ScheduleDialog._on_save` 同源）。不合法把那段提示**原样**回给前端显示在表单里
        （关掉框让用户重填是 Tk 档刻意不做的事）。
        """
        sched = self.app._sched
        t = payload.get("type") or "daily"
        trig = {"type": t, "time": (payload.get("time") or "").strip()}
        if t == "weekly":
            have = set(payload.get("days") or [])
            trig["days"] = [c for c in config.SCHEDULE_DAY_CODES if c in have]
        elif t == "hourly":
            try:
                trig["every_hours"] = int(payload.get("every_hours"))
            except (TypeError, ValueError):
                trig["every_hours"] = None
        elif t == "once":
            trig["date"] = (payload.get("date") or "").strip()

        rec = dict(payload.get("initial") or {})
        rec["name"] = (payload.get("name") or "").strip()
        rec["trigger"] = trig
        clean = config._clean_schedule(rec)
        if clean is None:
            return {"ok": False, "error":
                    "请检查：\n"
                    "  · 任务名不能为空、不能含 \\ / : * ? \" < > | 这些字符，不超过 60 字；\n"
                    "  · 时间要写成 24 小时制的 HH:MM（如 02:30，不能写 2:30）；\n"
                    "  · 每周至少要勾一天；间隔是 1~23 的整数；\n"
                    "  · 单次的日期要写成 MM/DD/YYYY（如 09/20/2026）。"}
        # 参数以调用方带进来的那份为准（它是"打开表单那一刻的传输页"）。
        clean["params"] = payload.get("params") or sched._collect_task_params()
        sched._sched_save(clean)
        return {"ok": True, "name": clean.get("name")}

    @staticmethod
    def _jsonable(v):
        r"""把界面上的值折成 JSON 能装下的东西。

        【dict 也要递归下去】这里的兜底是 `str(v)` —— 那是留给"意外混进来的东西"的
        最后一道。但 `dict` **不是**意外：定时任务表单要的 `initial`（整条原记录）和
        `params`（传输设置）都是 dict，走兜底会变成 `"{'name': '夜跑', ...}"` 这样一段
        Python repr 字符串：前端填不了表单，回传时 `dict("...")` 直接抛错。
        （口令不在这条路上 —— FTP 配置有自己的字段级快照 `_profile_rows`。）
        """
        if isinstance(v, bool) or isinstance(v, (int, float)) or v is None:
            return v
        if isinstance(v, str):
            return v
        if isinstance(v, dict):
            return {str(k): WebBridge._jsonable(x) for k, x in v.items()}
        if isinstance(v, (list, tuple)):
            return [WebBridge._jsonable(x) for x in v]
        return str(v)

    def _var_value(self, v):
        try:
            return self._jsonable(v.get())
        except Exception:
            return None

    def _profile_rows(self):
        r"""FTP 配置的**字段级**快照（前端「编辑…」拿它当表单初值）。

        【为什么不能像别处那样"把对象丢给 `_jsonable` 了事"】`_jsonable` 对不认识的类型
        兜底是 `str(v)` —— 那是给"意外混进来的东西"准备的最后一道。一套配置是个 dict，
        丢进去会变成 `"{'name': 'x', 'host': ...}"` 这样一段 **Python repr 字符串**：
        前端既填不了表单，那段字符串里还**带着口令**。所以这里显式逐字段取。

        【为什么把口令也发过去】Tk 档的编辑对话框就是这么干的（`FTPSettingsDialog` 用
        `initial_config` 预填口令输入框，`show="*"` 遮住）—— 不这么做，用户每次改地址
        都得**重打一遍口令**。两档的差别只在于：Tk 那份一直住在进程内存里，Web 这份要
        过一次 `127.0.0.1` 的本地 HTTP、并落在 DOM 的 `<input type="password">` 里。
        **相同的口径照旧**：列表与摘要里一个字符都不显示（见 `_show_profile_summary`）。
        """
        rows = []
        for p in getattr(self.app, "ftp_profiles", []) or []:
            if not isinstance(p, dict):
                continue
            rows.append({k: self._jsonable(p.get(k))
                         for k in ("name", "host", "port", "user",
                                   "password", "remote_dir")})
        return rows

    def _widget_model(self, w):
        m = {"kind": w._kind, "state": w._state}
        opts = {}
        for k, val in w._opts.items():
            if k.startswith("_"):
                continue
            if isinstance(val, (web_host.VarStub, web_kit.Widget)):
                continue                              # 变量/子控件另走 vars/widgets
            opts[k] = self._jsonable(val)
        m["opts"] = opts
        if w._kind == "text":
            m["text"] = w._text
        if w._kind in ("listbox", "notebook", "menu", "optionmenu", "treeview"):
            m["items"] = [self._jsonable(x) for x in w._items]
        if w._kind == "listbox":
            sel = w._opts.get("_curselection")
            m["selection"] = sel
        m["has_command"] = w._command is not None
        m["bindings"] = sorted(w._bindings.keys())
        return m

    def _snapshot(self):
        with self._msg_lock:
            msgs = [dict(r) for r in self._msgs]
        return {
            "title": self.root._title,
            "geometry": self.root._geometry,
            "vars": {k: self._var_value(v) for k, v in self.vars.items()},
            "widgets": {k: self._widget_model(w) for k, w in self.widgets.items()},
            "source_list": self._jsonable(getattr(self.app, "source_list", [])),
            "ftp_profiles": self._profile_rows(),
            "ftp_active": self._jsonable(getattr(self.app, "ftp_active", "")),
            # 数值变量的**合法区间**（Tk 档是 Spinbox 的 from_/to，写死在建造代码里读 config）。
            # 【为什么要发过来而不是抄进 HTML】抄一份 = 第二份真相：`config.TIMEOUT_RANGE_S`
            # 改了之后，HTML 里那份 min/max 还在按老区间卡人，而**没有任何地方会报错**。
            "ranges": _VAR_RANGES,
            # 版本 / 运行壳：顶栏与「关于」都读这一份。以前标题里的版本号是
            # 建造代码里手写的字符串，与 `config.APP_VERSION` 各说各话。
            "version": config.APP_VERSION,
            "runtime": dict(getattr(self, "runtime", None) or {}),
            "data_dir": config.DATA_DIR,
            "messages": msgs,
        }

    def snapshot(self):
        return self.call_on_main(self._snapshot)

    # ---- 设置页：草稿式保存 ----
    def save_settings(self, values):
        r"""校验 → 下发 → 落盘。返回 `{ok, values|errors, persisted}`。

        【为什么是批量、而不是前端逐项 `set_var`】逐项写的话，第 3 项非法时前 2 项
        已经生效、后 2 项没写，而落盘会把这份**半套状态**当成用户的最终选择。
        批量接口先验全部、再一次性写，失败时任何一项都不动。

        【为什么 `set_var` 仍然不校验】传输页的线程数是"本次运行参数"，改一下就该
        立即生效；设置页才是"默认参数"，它必须能拒绝非法值。两条语义不同，入口分开。
        """
        return self.call_on_main(self._save_settings, values)

    def _validate_settings(self, values):
        """→ (要写入的 {key: value}, 错误 {key: 人话})。任何一项有错就整批不写。"""
        out, errors = {}, {}
        if not isinstance(values, dict):
            return {}, {"_": "请求里没有设置内容"}
        for key, raw in values.items():
            if key not in _EDITABLE_SETTINGS:
                errors[key] = "不是可保存的设置项"
                continue
            if key in _VAR_RANGES:
                s = str(raw).strip()
                body = s[1:] if s[:1] in "+-" else s
                if not body.isdigit():
                    errors[key] = "必须是整数"
                    continue
                n = int(s)
                lo, hi = _VAR_RANGES[key]
                if not (lo <= n <= hi):
                    errors[key] = "需在 %d–%d 之间" % (lo, hi)
                    continue
                out[key] = n
            elif key == "log_level":
                if raw not in config.LOG_LEVELS:
                    errors[key] = "只能是「详细」或「常规」"
                    continue
                out[key] = raw
            elif key == "log_export_dir":
                if not isinstance(raw, str):
                    errors[key] = "必须是路径文本"
                    continue
                out[key] = raw.strip()
        return out, errors

    def _save_settings(self, values):
        out, errors = self._validate_settings(values)
        if errors:
            return {"ok": False, "errors": errors}
        if not out:
            return {"ok": False, "errors": {"_": "没有需要保存的项"}}
        for key, value in out.items():
            self.vars[key].set(value)
        # 唯一落点：`_HostHooks.on_ux_changed`（关闭窗口 / 开始传输也走它）。
        # 不另写一份 `save_ux_state(self._collect_ux_state())` —— 那是第二份"存什么"。
        persisted = bool(self.app._core._hooks.on_ux_changed())
        applied = {k: self._var_value(self.vars[k]) for k in out}
        return {"ok": True, "values": applied, "persisted": persisted}

    # ---- 退出语义（C3 关窗口 / C4 退出按钮与页面关闭） ----
    def _task_active(self):
        return bool(getattr(getattr(self.app, "_core", None), "_task_active", False))

    def request_close(self):
        r"""执行与 Tk 档 `_on_close` **同一份**退出流程。→ 是否可以立刻拆壳。

        · 没有任务在跑：直接在主线程走 `_on_close`（它只 `destroy()`），返回 True；
        · 传输中：`_on_close` 会弹"确定退出吗"（Web 档经替身层流到前端模态框）。
          它会阻塞到用户作答，**不能**占用 pywebview 的关闭回调/HTTP 请求线程，
          所以放到后台线程；关闭回调先返回 False（窗口留着），确认后由
          `_on_close` 里同一个 `root.destroy()` 收尾。
        """
        if not self._task_active():
            self.call_on_main(self.app._on_close)
            return True
        threading.Thread(target=self.app._on_close, daemon=True,
                         name="web-close-confirm").start()
        return False

    # ---- C4 页面存活跟踪 ----
    def client_hello(self, cid):
        with self._clients_lock:
            self._clients.add(str(cid or "?"))
            self._empty_since = None
            self._seen_client = True

    def client_bye(self, cid):
        with self._clients_lock:
            self._clients.discard(str(cid or "?"))
            if not self._clients and self._seen_client:
                self._empty_since = time.monotonic()

    def clients_gone_for(self, seconds):
        """最后一个页面离开已超过 `seconds` 秒 ⇒ True（run_c4 据此自行退出）。"""
        with self._clients_lock:
            since = self._empty_since
        return since is not None and (time.monotonic() - since) >= seconds

    # ---- 单实例探测 / 诊断包 / 打开目录 ----
    def ping(self):
        r"""`GET /api/ping` 的应答。**故意不走 `call_on_main`**：

        第二个实例（见 `_find_running_instance`）用它判断"端口那头是不是我们"，
        这一刻如果主线程正卡在某个对话框上，走投递就会超时 ⇒ 误判成"没人"，
        于是又起一个抢端口的进程。这里只读几个构造后就不再变的字段，没有竞态。
        """
        rt = dict(getattr(self, "runtime", None) or {})
        rt.update({"ok": True, "app": "transfer", "version": config.APP_VERSION})
        return rt

    def open_folder(self, which):
        """「打开数据目录 / 日志目录」。白名单，不接受任意路径。"""
        return self.call_on_main(self._open_folder, which)

    def _open_folder(self, which):
        target = {"data": config.DATA_DIR,
                  "log": config.SCHEDULE_LOG_DIR}.get(which)
        if not target:
            return {"ok": False, "error": "未知目录：%r" % (which,)}
        try:
            os.makedirs(target, exist_ok=True)
        except OSError:
            pass
        if not os.path.isdir(target):
            return {"ok": False, "error": "目录不存在：%s" % target}
        try:
            os.startfile(target)                 # noqa: S606 — 本机、固定白名单路径
        except Exception as e:                   # noqa: BLE001
            return {"ok": False, "error": str(e)}
        return {"ok": True, "path": target}

    def diagnostics(self):
        """→ (文件名, zip 字节)。**不含任何口令**。"""
        return self.call_on_main(self._build_diagnostics)

    def _build_diagnostics(self):
        r"""一键诊断包：出问题时用户下一个 zip 发回来，不用来回问环境。

        【纪律：口令绝不进包】`profiles` 里的 `password` 一律替换成占位符；
        状态库、配置原文都不直接打 —— 那道口子一旦开了，以后谁加个字段都可能带出去。
        """
        import io
        import platform
        import zipfile

        stamp = time.strftime("%Y%m%d_%H%M%S")
        ui = getattr(self.app, "_ui", None)
        try:
            log_body = ui.log_text.get("1.0", "end-1c")
        except Exception:                        # noqa: BLE001
            log_body = ""
        try:
            ux = config.load_ux_state()
        except Exception:                        # noqa: BLE001
            ux = {}
        try:
            profiles, active = config.load_profiles()
        except Exception:                        # noqa: BLE001
            profiles, active = [], ""
        try:
            schedules = config.load_schedules()
        except Exception:                        # noqa: BLE001
            schedules = []
        safe_profiles = []
        for p in profiles:
            row = dict(p)
            row["password"] = "***" if row.get("password") else ""
            safe_profiles.append(row)

        rt = dict(getattr(self, "runtime", None) or {})
        env_lines = [
            "文件传输工具 v%s" % config.APP_VERSION,
            "生成时间：%s" % time.strftime("%Y-%m-%d %H:%M:%S"),
            "运行壳：%s" % rt.get("mode", "(测试装配)"),
            "端口：%s" % rt.get("port", "-"),
            "Python：%s" % sys.version.replace("\n", " "),
            "系统：%s" % platform.platform(),
            "解释器：%s" % sys.executable,
            "冻结态：%s" % bool(getattr(sys, "frozen", False)),
            "数据目录：%s" % config.DATA_DIR,
            "日志目录：%s" % config.SCHEDULE_LOG_DIR,
            "任务在跑：%s" % self._task_active(),
            "日志目录变量：%r" % (
                self.vars["log_export_dir"].get()
                if "log_export_dir" in self.vars else ""),
        ]
        try:
            import host_probe
            env_lines.append("")
            env_lines.append("—— 宿主自检 ——")
            env_lines.extend(host_probe.report())
        except Exception as e:                   # noqa: BLE001
            env_lines.append("宿主自检不可用：%s" % e)

        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
            z.writestr("诊断.txt", "\n".join(env_lines) + "\n")
            z.writestr("日志.txt", log_body or "(日志区为空)\n")
            z.writestr("界面状态.json", json.dumps(ux, ensure_ascii=False, indent=2))
            z.writestr("FTP配置.json", json.dumps(
                {"active": active, "profiles": safe_profiles},
                ensure_ascii=False, indent=2))
            z.writestr("定时任务.json", json.dumps(
                schedules, ensure_ascii=False, indent=2))
        return "transfer_diagnostics_%s.zip" % stamp, buf.getvalue()

    # ---- 动作 ----
    def _resolve(self, name):
        if name in self.widgets:
            return "widget", self.widgets[name]
        if name in self.vars:
            return "var", self.vars[name]
        raise KeyError("未知的界面名字：%r" % (name,))

    def _semantic(self, action, payload):
        r"""语义动作：把前端的意图翻成对**业务对象**的调用（不是对控件）。

        【为什么需要一个语义层，而不是让前端"点按钮"了事】有些 UI 操作在 Tk 里
        是"菜单项 / 对话框"触发的（`_add_source` / `_reset_syncpoint` /
        `_profile_*` / `_sched_*`），它们**没有**对应的 `self.<按钮>`（要么是
        `tk.Menu` 的项、要么根本没按钮）。Web 前端点不到它们 ⇒ 这里给名字。
        判据：**凡前端要触发的意图，都必须有一个能按名字调到的落点**。
        """
        app = self.app
        if action == "add_source":
            return app._form._add_source(payload.get("path") or "")
        if action == "remove_source":
            return app._form._remove_selected_source()
        # 源列表排序 / 去重：与 Tk 档共用同一份实现（顺序 = 串行执行顺序）。
        if action == "source_move_up":
            return app._form._move_selected_source(-1)
        if action == "source_move_down":
            return app._form._move_selected_source(+1)
        if action == "source_dedupe":
            return app._form._dedupe_sources()
        if action == "clear_log":
            # 清的是**界面日志区**，与"开新任务时自动清空"走同一个落点
            # （计数一并归位，否则下一次截断会从旧行数起算）。
            return app._ui._reset_log_area()
        if action == "reset_syncpoint":
            return app._core._reset_syncpoint()
        if action == "export_log":
            return app._ui._export_log()
        if action == "refresh_sched":
            return app._sched._sched_refresh_status()
        if action == "sched_reload":
            return app._sched._sched_reload(select=None)
        # ---- FTP 配置（Tab2「FTP 配置」那一组按钮的 Web 侧落点）----
        # 【为什么"删 / 设为当前"能整段复用，"新建 / 编辑"不能】前两个只依赖"列表里
        #   选中的那一套"：`_profile_delete` / `_profile_set_active` 自己去读
        #   `profile_listbox` 的选中项，而那个选中项**也是**前端 `select_list` 在同一个
        #   替身控件上设进去的 ⇒ 同一份状态、同一份实现；连删除前的二次确认
        #   （`askyesno`）都自动走前端模态框（见 `_dialog` 的应答类分支）。
        #   后两个的中段是 `ui_dialogs.FTPSettingsDialog`（`tk.Toplevel`）—— Web 档没有
        #   窗口可画 ⇒ 只能把"表单里的那几个字段"直接送进 `_profile_save_fields`；
        #   而"存 + 刷新列表 + 回填 Tab1 + 记日志"那半截仍与 Tk 档**共用同一份**。
        if action in ("profile_new", "profile_edit"):
            return app._form._profile_save_fields(payload,
                                                  is_new=(action == "profile_new"))
        if action == "profile_delete":
            return app._form._profile_delete()
        if action == "profile_set_active":
            return app._form._profile_set_active()
        # ---- 补回与老版（Tk）对齐的三处入口（见 141 收官报告"老版有、Web 缺"清单）----
        #   ① 源列表的文本入口：Tk 里是「浏览」菜单的"手工添加…"与共享那条 `askstring`；
        #   ② 定时任务表单：Tk 里是 `ScheduleDialog` 那个 `tk.Toplevel`（Web 没有窗口可画
        #      ⇒ 表单搬到前端，攒记录/校验/存盘仍调后端同一份 `_sched_save`）；
        #   ③ 定时任务删除：整段逻辑与界面无关（含二次确认，弹框自动走前端模态）。
        if action == "add_source_text":
            return self._add_source_text(payload)
        if action == "export_log_text":
            return self._export_log_text()
        if action == "sched_form":
            return self._sched_form(payload)
        if action == "sched_save":
            return self._sched_save_fields(payload)
        if action == "sched_delete":
            return app._sched._sched_delete()
        if action == "sched_copy_cmd":
            # 前端已经真把命令写进浏览器剪贴板；这里走同一份后端方法只为补那行日志
            # （文本取自同一个 `sched_cmd_text` 替身 ⇒ 两档说法一致）。
            return app._sched._sched_copy_cmd()
        raise ValueError("未知语义动作：%r" % (action,))

    def _invoke(self, req):
        action = req.get("action")
        target = req.get("target")
        payload = req.get("payload") or req
        if action in ("click", "invoke"):
            kind, obj = self._resolve(target)
            if kind != "widget" or obj._command is None:
                raise ValueError("%r 上没有可调用的 command" % (target,))
            return obj._command()
        if action == "set_var":
            kind, obj = self._resolve(target)
            if kind != "var":
                raise ValueError("%r 不是变量，不能 set" % (target,))
            obj.set(payload.get("value"))
            return obj.get()
        if action == "select_list":
            kind, obj = self._resolve(target)
            if kind != "widget" or obj._kind != "listbox":
                raise ValueError("%r 不是列表，不能 select" % (target,))
            idx = int(payload["index"])
            obj.selection_clear(0, "end")
            obj.selection_set(idx)
            obj.see(idx)
            fn = obj._bindings.get("<<ListboxSelect>>")
            if fn is not None:
                fn(web_kit._Event(widget=obj))
            return idx
        if action == "semantic":
            # ⚠️ 语义动作名必须从**信封**（req）里读，不能从 payload 里读：
            #    payload 里也可能有一个 `name` —— 那正是 FTP 配置的名字
            #    （`{"action":"semantic","name":"profile_new","payload":{"name":"生产",…}}`）。
            #    从 payload 里读的后果实测过：`_semantic("生产", …)` ⇒
            #    "未知语义动作：'生产'"。前端报这一句时，谁也想不到问题出在
            #    "信封的字段和载荷的字段撞了名"。
            return self._semantic(req.get("name"), req.get("payload") or req)
        raise ValueError("未知动作：%r" % (action,))

    def invoke(self, req):
        return self.call_on_main(self._invoke, req)

    # ---- 目录列举（前端"浏览"用；本机、同步、只读） ----
    @staticmethod
    def fs_list(path, with_files=False):
        r"""列出一个目录下的**子目录**（纯本机、只读）。

        【现在的主路是原生对话框（`/api/pick`）】本地路径默认由系统对话框选，
        本方法只服务**退路**：原生框起不来时前端退回自绘框（`openPickerFallback`），
        那个框靠这里的 `/api/fs` 列目录。留着它不是死代码 —— Tk 起不来时它是唯一的路。

        返回 `{"path": <绝对路径>, "parent": <上一级或 None>, "dirs": [名字…]}`。
        盘符根（`C:\`）的上一级是 `None`（再往上没有意义）。

        `with_files=True` 时多列一份 `files` —— Tk 档源侧那个「浏览」菜单第一项是
        **选择文件**（`transfer_form._pick_source_file`），Web 要照做就得能列文件。
        """
        path = os.path.abspath(path or os.path.expanduser("~"))
        if not os.path.isdir(path):
            raise ValueError("不是目录：%s" % path)
        dirs, files = [], []
        try:
            with os.scandir(path) as it:
                for e in it:
                    try:
                        if e.is_dir():
                            if not e.name.startswith("$"):
                                dirs.append(e.name)
                        elif with_files:
                            files.append(e.name)
                    except OSError:
                        continue
        except OSError as e:
            raise ValueError("打不开目录：%s" % e) from e
        dirs.sort(key=str.lower)
        files.sort(key=str.lower)
        parent = os.path.dirname(path.rstrip("\\/"))
        if not parent or parent == path:
            parent = None
        out = {"path": path, "parent": parent, "dirs": dirs}
        if with_files:
            out["files"] = files
        return out

    @staticmethod
    def fs_drives():
        r"""Windows 上的盘符列表（源/目标选择器的起点）。"""
        if os.name != "nt":
            return ["/"]
        out = []
        for letter in "ABCDEFGHIJKLMNOPQRSTUVWXYZ":
            root = "%s:\\" % letter
            if os.path.exists(root):
                out.append(root)
        return out

    # ---- 原生对话框（本地路径的四处「浏览」） ----
    #
    # 【为什么请系统对话框回来】Tk 档这四处走的就是 `filedialog`，用户看到的是 Windows
    #   自己的对话框（目录树、盘符、图标、新建文件夹、键盘导航）。Web 档把 `filedialog`
    #   换成无头替身之后，前端只能自绘一个列表 —— 能选，但"比原来差远了"。
    #   系统对话框的决定性优势是它**跑在用户自己的机器上**：后端本就在 127.0.0.1，
    #   在这一侧调 Tk，与 Tk 档调的是同一个函数、弹出的是同一个框。
    #
    # 【线程：为什么**不**投给 UI 线程】Web 档没有 Tk 主循环，但 `tkinter` 本身还在
    #   （`file_transfer` 就 import 它）。实测（`_tmp/_tk_thread_probe.py`）：在普通工作
    #   线程里 `tk.Tk()` + `askdirectory` 能正常弹框、也能被 WM_CLOSE 关掉。
    #   而对话框**不碰本应用的界面模型**（不读 `Widget`、不读 `VarStub`），所以那条
    #   "一切界面动作走主线程"的纪律在这里**不适用** —— 它守的是 Tk 变量的跨线程读。
    #   ⇒ 直接在当前 HTTP 请求线程里开框，用户关框之前这条请求一直等（模态）。这样：
    #     · C4 的主线程、C3 的泵线程都不被阻塞 ⇒ 传输进度与状态照常刷新；
    #     · 不需要 `call_on_main` 那个 5 秒超时（原生框开一分钟是常事）。
    #   `_PICK_LOCK` 保证同一时刻只有一个 Tk root。
    #
    # 【失败怎么办】不猜、不静默：返回 `{"ok": False, "error": ...}`，前端退回原来那个
    #   自绘框。"系统对话框起不来"不该等于"点浏览没反应"。
    def pick_native(self, kind, initial=""):
        title = _PICK_TITLES.get(kind)
        if title is None:
            raise ValueError("未知的选择类型：%r" % (kind,))
        with _PICK_LOCK:
            return self._pick_native(kind, initial, title)

    @staticmethod
    def _pick_native(kind, initial, title):
        r"""在**当前线程**上弹系统原生对话框，返回 `{"ok", "path", "cancelled"}`。

        【为什么可以不在主线程】见 `pick_native` 上方那段：Tk 的对话框不碰本应用的
        界面模型，只用一个自己创建的、随即销毁的 root。实测在普通工作线程里可用。

        【空串 = 取消，不是失败】`filedialog` 取消时回 `''`；这与 Tk 档
        `if path:` 的判据完全一致（取消不提示任何东西）。
        """
        import tkinter as _tk
        from tkinter import filedialog as _fd

        initial = (initial or "").strip()
        # 初值可能是"文件"（源路径既可能是文件也可能是目录），而 initialdir 只认目录。
        if initial and not os.path.isdir(initial):
            parent = os.path.dirname(initial)
            initial = parent if parent and os.path.isdir(parent) else ""
        kw = {"title": title}
        if initial:
            kw["initialdir"] = initial

        root = _tk.Tk()
        root.withdraw()
        try:
            # 无可见主窗口时，对话框可能被别的窗口压在后面 —— 置顶一下。
            # （Tk 档的 root 就是可见主窗口，不存在这个问题；这里不置顶就会"点了没反应"。）
            try:
                root.attributes("-topmost", True)
            except Exception:                       # noqa: BLE001 — 非 Windows 忽略
                pass
            if kind == "source-file":
                picked = _fd.askopenfilename(parent=root, **kw)
            else:
                picked = _fd.askdirectory(parent=root, **kw)
        finally:
            try:
                root.destroy()
            except Exception:                       # noqa: BLE001
                pass
        picked = os.path.normpath(picked) if picked else ""
        return {"ok": True, "path": picked, "cancelled": not picked}


# =============================================================================
# 装配
# =============================================================================
def build():
    r"""装配一个无 Tk 的 `FileTransferApp`，返回 `WebBridge`。

    【必须的次序】先 `import file_transfer`（把全部界面模块拉进 `sys.modules`），
    再 `web_kit.install()`（改这些模块命名空间里的 `tk` / `ttkbootstrap` / 三扇门），
    最后才 `FileTransferApp(root)`。顺序反了 ⇒ 建造代码拿到的还是真 tkinter。
    """
    import file_transfer                                # noqa: F401 — 副作用：拉起全部界面模块

    web_kit.install()
    root = web_host.WebRoot()
    # 【顺序：先桥、后 app】弹框记录器要在 `FileTransferApp(root)` **之前**装好 ——
    #   构造期就可能弹框，那会儿前端桥得已经在位（见 `WebBridge.attach` 的说明）。
    bridge = WebBridge(None, root)
    # `only_if_none=True`：套件（`patch_messagebox`）若已先装了记录器，**不许盖掉它**
    #   —— 那是套件断言用的那份；只有"没人装过"（生产运行）时才接前端对话框。
    web_kit.set_messagebox_recorder(bridge._dialog, only_if_none=True)
    app = file_transfer.FileTransferApp(root)
    bridge.attach(app)
    root.bridge = bridge
    return bridge


# =============================================================================
# C4：本地 HTTP 服务（后台线程）+ 主线程泵
# =============================================================================
class _HandlerMarker(object):
    """占位，避免误用（真正的 Handler 在 `_make_handler` 里按桥生成）。"""


# SSE 推送节奏（见 Handler._serve_events）
_SSE_INTERVAL = 0.3      # 后端自查快照的间隔；有变化才真的发帧
_SSE_PING_S = 15.0       # 没变化时的注释心跳，防止中间层判死
_SSE_DEAD_ROUNDS = 30    # 连续这么多轮投递不上主线程 ⇒ 认为宿主已停，结束连接


def _make_handler(bridge):
    from http.server import BaseHTTPRequestHandler

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *a):                      # 静音（前端轮询很吵）
            return

        def _send(self, code, body, ctype="application/json; charset=utf-8"):
            if isinstance(body, str):
                body = body.encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        _MAX_BODY = 8 * 1024 * 1024

        def _read_json(self):
            try:
                n = int(self.headers.get("Content-Length") or 0)
            except (TypeError, ValueError):
                n = 0
            # 负数会让 rfile.read 一直读到对端关连接（keep-alive 下等于挂死这条请求线程）；
            # 超大值则是一次性分配。两种都按坏请求处理，并让本条连接到此为止。
            if n < 0 or n > self._MAX_BODY:
                self.close_connection = True
                raise ValueError("请求体大小不合法：%r" % (n,))
            raw = self.rfile.read(n) if n else b""
            if not raw:
                return {}
            text = raw.decode("utf-8", "replace").strip()
            try:
                return json.loads(text)
            except ValueError:
                # `navigator.sendBeacon` 发的是字符串（`text/plain`），不是 JSON ——
                # 关页面时的 bye 走这条路。当裸文本收下，别让告别消息 500。
                return {"_text": text}

        def _send_binary(self, body, ctype, filename):
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header(
                "Content-Disposition",
                'attachment; filename="%s"' % filename.replace('"', ""))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            path, _, query = self.path.partition("?")
            if path in ("/", "/index.html"):
                return self._serve_static("index.html")
            if path == "/api/state":
                try:
                    return self._send(200, json.dumps(bridge.snapshot(),
                                                      ensure_ascii=False))
                except Exception as e:                  # noqa: BLE001
                    return self._send(500, json.dumps({"error": str(e)}))
            if path == "/api/fs":
                qs = urllib.parse.parse_qs(query)
                try:
                    return self._send(200, json.dumps(
                        bridge.fs_list((qs.get("path") or [""])[0],
                                       with_files=((qs.get("files") or [""])[0] == "1")),
                        ensure_ascii=False))
                except Exception as e:                  # noqa: BLE001
                    return self._send(200, json.dumps({"ok": False,
                                                       "error": str(e)}))
            if path == "/api/fs/drives":
                return self._send(200, json.dumps(bridge.fs_drives(),
                                                  ensure_ascii=False))
            if path == "/api/ftp":
                # 【为什么单开一个端点、不走 /api/invoke】列远端目录要真连服务器
                #   （连不上时按 ftplib 的 timeout 等满 10 秒）。走 invoke = 在主线程里
                #   等这 10 秒 ⇒ 整个界面冻住。这里在**请求线程**里做，主线程只被借去
                #   读一次"当前激活那套配置"（瞬时）。
                qs = urllib.parse.parse_qs(query)
                try:
                    return self._send(200, json.dumps(
                        bridge.ftp_list((qs.get("path") or [""])[0]),
                        ensure_ascii=False))
                except Exception as e:                  # noqa: BLE001
                    return self._send(200, json.dumps({"ok": False,
                                                       "error": str(e)},
                                                      ensure_ascii=False))
            if path == "/api/ping":
                # 第二个实例 / 探活脚本用；**不走主线程**（见 `WebBridge.ping`）。
                return self._send(200, json.dumps(bridge.ping(), ensure_ascii=False))
            if path == "/api/events":
                return self._serve_events()
            if path == "/api/diagnostics.zip":
                try:
                    name, blob = bridge.diagnostics()
                    return self._send_binary(blob, "application/zip", name)
                except Exception as e:                  # noqa: BLE001
                    return self._send(200, json.dumps({"ok": False,
                                                       "error": str(e)},
                                                      ensure_ascii=False))
            if path.startswith("/static/"):
                return self._serve_static(path[len("/static/"):])
            return self._send(404, json.dumps({"error": "not found"}))

        def do_POST(self):
            path = self.path.split("?", 1)[0]
            if path == "/api/dialog":
                try:
                    req = self._read_json()
                    bridge.answer_dialog(req.get("id"), req.get("answer"))
                    return self._send(200, json.dumps({"ok": True}))
                except Exception as e:                  # noqa: BLE001
                    return self._send(200, json.dumps({"ok": False, "error": str(e)},
                                                      ensure_ascii=False))
            if path == "/api/pick":
                # 原生对话框：在 UI 线程上**阻塞**到用户关框，所以这条请求会挂很久。
                # 前端在等待期间暂停轮询（见 app.js `pickBusy`），否则快照请求会排队。
                try:
                    req = self._read_json()
                    result = bridge.pick_native(req.get("kind") or "",
                                                req.get("initial") or "")
                    return self._send(200, json.dumps(result, ensure_ascii=False))
                except Exception as e:                  # noqa: BLE001
                    return self._send(200, json.dumps({"ok": False, "error": str(e)},
                                                      ensure_ascii=False))
            if path == "/api/ftp/test":
                # 纯网络、不碰业务对象 ⇒ 就在请求线程里跑（理由同 GET /api/ftp）。
                try:
                    req = self._read_json()
                    return self._send(200, json.dumps(bridge.ftp_test(req),
                                                      ensure_ascii=False))
                except Exception as e:                  # noqa: BLE001
                    return self._send(200, json.dumps({"ok": False,
                                                       "message": "无法连接到 FTP 服务器：\n%s"
                                                       % e}, ensure_ascii=False))
            if path == "/api/settings/save":
                try:
                    req = self._read_json()
                    return self._send(200, json.dumps(
                        bridge.save_settings(req.get("values") or {}),
                        ensure_ascii=False))
                except Exception as e:                  # noqa: BLE001
                    return self._send(200, json.dumps(
                        {"ok": False, "errors": {"_": str(e)}},
                        ensure_ascii=False))
            if path == "/api/hello" or path == "/api/bye":
                try:
                    req = self._read_json()
                    cid = req.get("client") or req.get("_text") or "?"
                    if path == "/api/hello":
                        bridge.client_hello(cid)
                    else:
                        bridge.client_bye(cid)
                    return self._send(200, json.dumps({"ok": True}))
                except Exception as e:                  # noqa: BLE001
                    return self._send(200, json.dumps({"ok": False,
                                                       "error": str(e)},
                                                      ensure_ascii=False))
            if path == "/api/quit":
                # 立刻回执、后台收尾：`_on_close` 可能弹"确定退出吗"（阻塞到用户作答），
                # 也可能直接 destroy 掉主循环 —— 都不能让这条 HTTP 响应陪着一起没。
                threading.Thread(target=bridge.request_close, daemon=True,
                                 name="web-quit").start()
                return self._send(200, json.dumps({"ok": True}))
            if path == "/api/open_folder":
                try:
                    req = self._read_json()
                    return self._send(200, json.dumps(
                        bridge.open_folder(req.get("which") or ""),
                        ensure_ascii=False))
                except Exception as e:                  # noqa: BLE001
                    return self._send(200, json.dumps({"ok": False,
                                                       "error": str(e)},
                                                      ensure_ascii=False))
            if path != "/api/invoke":
                return self._send(404, json.dumps({"error": "not found"}))
            try:
                req = self._read_json()
                result = bridge.invoke(req)
                return self._send(200, json.dumps({"ok": True, "result": result},
                                                  ensure_ascii=False))
            except Exception as e:                      # noqa: BLE001
                return self._send(200, json.dumps({"ok": False, "error": str(e)},
                                                  ensure_ascii=False))

        def _serve_events(self):
            r"""SSE 推送（`GET /api/events`）：一条连接，快照变了就推一帧。

            【为什么要有这条】原来是前端每 500 ms 发一次 `GET /api/state`，有两个
            治不好的毛病：

              ① **请求会叠加**。`setInterval` 不管上一轮回没回来。主线程忙的时候
                 （扫大目录 / 传大文件 / 原生对话框开着）一轮超过 500 ms，请求就排队；
              ② **响应会乱序**。排队的请求各自返回，旧的快照可能后到，
                 把新的那份盖掉 —— 界面上表现为**进度条倒退、按钮状态闪回**。

            改成 SSE 之后，一条连接、一个方向、严格有序，上面两条同时消失；
            顺带把「一次轮询一个 TCP 连接」变成「一次 SSE 一个长连接」。

            【线程纪律】本方法跑在**请求线程**里（每个连接一条）。`bridge.snapshot()`
            自己会 `call_on_main` 投递到主线程，所以这里不碰任何界面对象。
            主循环没在跑时它抛 `TimeoutError` —— 连续几次就收工，不空转。

            【心跳】没变化时每 `_SSE_PING_S` 秒发一个注释行（`: ping`）。不发的话
            中间层（代理 / 浏览器）会以为连接死了；而每帧都发又会把带宽吃满。
            """
            self.close_connection = True
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Connection", "keep-alive")
            self.end_headers()

            last = None
            last_ping = time.time()
            dead_rounds = 0
            try:
                while True:
                    body = None
                    try:
                        body = json.dumps(bridge.snapshot(), ensure_ascii=False)
                        dead_rounds = 0
                    except TimeoutError:
                        # 主线程正忙（比如原生文件对话框开着）：这一轮跳过，
                        # 但连续多轮都忙说明宿主没了，收工。
                        dead_rounds += 1
                        if dead_rounds >= _SSE_DEAD_ROUNDS:
                            break
                    except Exception:                    # noqa: BLE001
                        break
                    if body is not None and body != last:
                        last = body
                        self.wfile.write(("data: %s\n\n" % body).encode("utf-8"))
                        self.wfile.flush()
                        last_ping = time.time()
                    elif time.time() - last_ping >= _SSE_PING_S:
                        self.wfile.write(b": ping\n\n")
                        self.wfile.flush()
                        last_ping = time.time()
                    time.sleep(_SSE_INTERVAL)
            except (BrokenPipeError, ConnectionResetError):
                return                                   # 页面关了 / 刷新了，正常
            except OSError:
                return

        def _serve_static(self, rel):
            rel = rel.replace("\\", "/").lstrip("/")
            root = os.path.normpath(WEB_DIR)
            full = os.path.normpath(os.path.join(root, rel))
            # 前缀比较必须带分隔符：`...\transfer\web` 也会匹配 `...\transfer\web_app.py`，
            # 于是 `/static/../web_app.py` 能把后端源码读出去。
            root_cmp = os.path.normcase(root) + os.sep
            if not os.path.normcase(full).startswith(root_cmp) or not os.path.isfile(full):
                return self._send(404, json.dumps({"error": "not found"}))
            ctype = {
                ".html": "text/html; charset=utf-8",
                ".js": "text/javascript; charset=utf-8",
                ".css": "text/css; charset=utf-8",
            }.get(os.path.splitext(full)[1], "application/octet-stream")
            with open(full, "rb") as f:
                return self._send(200, f.read(), ctype)

    return Handler


# =============================================================================
# 单实例 / 端口落点 / 探活
# =============================================================================
# 【为什么需要这一节】双击 exe 两次会起两个进程：两个都在跑启动自检、都想写
# 同一份 `.ftp_transfer_config.json`，且第二个大概率绑不上 8765。后端原子写
# 保证"不会写坏"，但"谁写了最后一份"仍然说不清。所以：
#   ① 正常启动（没给 `--c3/--c4/--port`）先争一个命名互斥量；
#   ② 争不到 ⇒ 读端口落点文件 → `/api/ping` 认人 → 把浏览器指过去，自己退出；
#   ③ 显式参数（脚本 / 探针 / 并排调试）**不参与**单实例，照旧想开几个开几个。
_MUTEX_NAME = "Local\\FTPTransferTool.WebHost"
_mutex_handle = None            # 进程生命期内必须一直持有（放掉 = 别的实例能进来）


def _write_port_file(port):
    try:
        os.makedirs(config.DATA_DIR, exist_ok=True)
        with open(_PORT_FILE, "w", encoding="utf-8") as f:
            f.write(str(int(port)))
    except OSError:
        pass                    # 落点文件只是"方便找到我"，写不了不影响服务本身


def _read_port_file():
    try:
        with open(_PORT_FILE, "r", encoding="utf-8") as f:
            return int((f.read() or "").strip())
    except Exception:                           # noqa: BLE001
        return None


def _remove_port_file():
    try:
        os.unlink(_PORT_FILE)
    except OSError:
        pass


def _query_instance(port, timeout=0.6):
    """端口那头**是不是我们**？是 → 返回 `/api/ping` 的字典，否则 None。

    只看 `app == "transfer"`：那个端口上可能是别的开发服务，光"能连上"不够。
    """
    try:
        with urllib.request.urlopen(
                "http://127.0.0.1:%d/api/ping" % int(port),
                timeout=timeout) as r:
            data = json.loads(r.read().decode("utf-8"))
    except Exception:                           # noqa: BLE001
        return None
    if isinstance(data, dict) and data.get("app") == "transfer":
        return data
    return None


def _find_running_instance():
    """按端口落点文件找已有实例；文件缺失 / 过期 / 被别的程序占着 ⇒ None。"""
    port = _read_port_file()
    if not port:
        return None
    return _query_instance(port)


def _acquire_instance_mutex():
    """争命名互斥量。→ True 表示"我是唯一实例"（或本平台上拿不到互斥量）。

    【失败方向要想清楚】`ctypes` 调用出任何问题都返回 True：宁可多开一个，
    也不能因为一段探测代码把正常启动拦死。真正隔离并发写的是原子写。
    """
    global _mutex_handle
    if os.name != "nt":
        return True
    try:
        import ctypes
        from ctypes import wintypes

        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.CreateMutexW.argtypes = (wintypes.LPVOID, wintypes.BOOL,
                                     wintypes.LPCWSTR)
        k32.CreateMutexW.restype = wintypes.HANDLE
        h = k32.CreateMutexW(None, False, _MUTEX_NAME)
        if not h:
            return True
        if ctypes.get_last_error() == 183:      # ERROR_ALREADY_EXISTS
            k32.CloseHandle(h)
            return False
        _mutex_handle = h
        return True
    except Exception:                           # noqa: BLE001 — 见 docstring
        return True


def _open_existing(url):
    """第二个实例：把已有界面亮出来（C4 用浏览器；C3 已有窗口就在前台）。"""
    import webbrowser

    try:
        webbrowser.open(url)
    except Exception:                           # noqa: BLE001
        pass
    print("已有实例在运行：%s" % url)


def _bind_server(bridge, port, tries=1):
    r"""绑 `127.0.0.1:port`，被占就顺延 `tries` 个端口。返回 `(srv, 实际端口)`。

    【为什么顺延而不是报错退出】双击 exe 是最常见的入口，而 8765 可能被
    "上一次没退干净的进程 / 别的开发工具"占着。用户看到一句"端口被占用"
    等于让他自己去改设置 —— 顺延一格就没有这个问题。显式指定 `--port` 时
    `tries=1`，照旧**响亮失败**（那是脚本/测试要的确定性）。
    """
    from http.server import ThreadingHTTPServer

    handler = _make_handler(bridge)
    last = None
    for i in range(max(1, int(tries))):
        try:
            srv = ThreadingHTTPServer(("127.0.0.1", int(port) + i), handler)
            return srv, srv.server_address[1]
        except OSError as e:
            last = e
    raise last


def run_c4(bridge, port=DEFAULT_PORT, open_browser=True, port_tries=1):
    """C4：标准库 HTTP 服务（后台线程）+ 主线程泵 `SchedulerStub`。**只听 127.0.0.1**。"""
    import webbrowser

    srv, port = _bind_server(bridge, port, port_tries)
    bridge.runtime = {"mode": "c4", "port": port}
    threading.Thread(target=srv.serve_forever, daemon=True,
                     name="web-http-c4").start()
    url = "http://127.0.0.1:%d/" % port
    _write_port_file(port)
    if open_browser:
        try:
            webbrowser.open(url)
        except Exception:
            pass
    if open_browser is None:
        print("C4 前端：%s" % url)
    root = bridge.root
    try:
        while not (root._closed or root._destroyed):
            # 页面全关了（bye 之后超时没回来）⇒ 进程自行退出。
            # 没有这一步，关掉最后一个标签页后进程会一直挂在后台占着端口。
            if bridge.clients_gone_for(8.0):
                break
            root.run_pending()
            d = root.next_due_in()
            time.sleep(0.01 if d is None else min(max(d, 0.0), 0.02))
    finally:
        srv.shutdown()
        srv.server_close()
        _remove_port_file()
    return 0


# =============================================================================
# C3：pywebview 窗口（宿主事件循环 = pywebview 的循环，主线程）
# =============================================================================
def run_c3(bridge, port=DEFAULT_PORT, title=None, port_tries=1):
    r"""C3：`pywebview` 窗口（WebView2）＋ **本地 HTTP 服务**，前端仍是同一份 `web/`。

    【为什么 C3 也起 HTTP】前端是 `fetch` 轮询的**同一份**代码（无框架、无构建器）。
    不为了 C3 再写一套 `pywebview.api` 传输层 —— 窗口直接指向
    `http://127.0.0.1:port/`，HTTP 面与 C4 **逐字节相同**；C3 与 C4 的差别**只在壳**
    （WebView2 窗口 vs 系统浏览器）。**只听 127.0.0.1**。

    【泵为什么是"专用线程"而不是主线程】pywebview 的 `webview.start()` 必须占住
    主线程（WinForms 消息循环），把 `run_pending()` 塞不进主线程。试过 `js_api`
    当主线程节拍器 —— **实测不通**（`js_bridge_call` 不在主线程，`run_pending`
    的主线程断言当场抛出，泵等于没跑，症状是所有 `/api/*` 都"主循环没在跑"超时）。
    ⇒ 正解：起一个**专用泵线程**；并只在这一档把"UI 线程 = 主线程"这条 **Tk 专有**
    的断言换掉（`web_host` 允许登记 `pump_thread`；`ui_channel` 的
    `_DEBUG_UI_THREAD_ASSERT` 在这一档临时置 False）。替身层本就是**非 Tk 宿主**，
    模型侧一律走锁；这条断言在 C4/tk 档**照旧开着**，安全网不拆。

    ⚠️ 没有 pywebview 的环境抛 ImportError（响亮失败，绝不静默退回 C4）。
    """
    import webview                                       # 没有就 ImportError

    import ui_channel

    old_assert = getattr(ui_channel, "_DEBUG_UI_THREAD_ASSERT", True)
    ui_channel._DEBUG_UI_THREAD_ASSERT = False

    srv, port = _bind_server(bridge, port, port_tries)
    bridge.runtime = {"mode": "c3", "port": port}
    threading.Thread(target=srv.serve_forever, daemon=True,
                     name="web-http-c3").start()
    url = "http://127.0.0.1:%d/" % port
    _write_port_file(port)
    root = bridge.root

    stop = threading.Event()

    def _pump():
        root.set_pump_thread()               # 本线程顶替"UI 线程"角色
        while not (stop.is_set() or root._closed or root._destroyed):
            try:
                root.run_pending()
            except Exception:                            # noqa: BLE001
                pass
            time.sleep(0.01)

    threading.Thread(target=_pump, daemon=True, name="web-pump-c3").start()

    window = webview.create_window(
        title or root._title or ("文件传输工具 v%s" % config.APP_VERSION),
        url=url)

    def _on_closing():
        """用户点窗口 ×/Alt+F4：走与 Tk 档**同一份** `_on_close`。

        返回 False = 先别关（传输中，前端模态框正在问"确定退出吗"）。
        确认后 `_on_close` 自己 `root.destroy()`，下面的看门线程随即销毁窗口。
        """
        return bool(bridge.request_close())

    try:
        window.events.closing += _on_closing
    except Exception:                            # noqa: BLE001 — 老版 pywebview 没有事件
        pass

    def _watch_destroy():
        while not stop.is_set():
            if root._destroyed or root._closed:
                try:
                    window.destroy()
                except Exception:                # noqa: BLE001
                    pass
                return
            time.sleep(0.1)

    threading.Thread(target=_watch_destroy, daemon=True,
                     name="web-watch-c3").start()

    try:
        webview.start()
    finally:
        stop.set()
        ui_channel._DEBUG_UI_THREAD_ASSERT = old_assert
        srv.shutdown()
        srv.server_close()
        _remove_port_file()
    return 0


def run_tk():
    r"""纯 Tk 档：真 `tkinter` 窗口，**一个替身都不装**。

    【为什么要单独一条路，而不是复用 `build()`】
    C3/C4 的 `build()` 第一件事就是 `web_kit.install()` —— 它把各界面模块命名空间里的
    `tk` / `ttkbootstrap` 换成替身。替身造出来的是**界面模型**（可序列化），不是像素；
    像素由 `web/` 那套前端去画。所以要让窗口真的出现在屏幕上，必须**跳过** install()，
    让建造代码照旧拿到真 tkinter —— 这也是 `file_transfer.__main__` 走的同一条路。

    【与 `file_transfer.__main__` 的差别】装配序列一步不差（同一个 `FileTransferApp`），
    只差两处：
      ① 不解析 `--run-task`：无头入口仍归 `file_transfer.__main__`，这里只有 GUI；
      ② 补回开关耦合检查（见下）—— 那条检查原本只在 `__main__` 里，而 web_app 是
         打包后的真实入口，缺了它等于这道保险对所有档都失效。
    两处都是"补回来"，不是新逻辑。

    【为什么不参与单实例互斥】与 `--c3` / `--c4` 同规则：显式参数用于探针与并排调试，
    要能同时开好几个窗口（`main()` 里 `explicit=True` 已经保证）。
    """
    import tkinter as tk

    import file_transfer

    # 【开关耦合检查】`_STATE_DB_ENABLED=True` + `_ATOMIC_WRITE_ENABLED=False` 意味着
    #   状态库记下了"这个文件已完成"，而磁盘上可能是写入中断的半截文件 —— 续传会
    #   静默跳过它们，正是整个阶段 2 要消灭的静默数据丢失。开发者配置错误，必须
    #   响亮失败。反向组合（库关、原子开）是安全的，放行。
    #   文案与 `file_transfer.__main__` 里那道保持一致，两处都改才行。
    if config._STATE_DB_ENABLED and not config._ATOMIC_WRITE_ENABLED:
        msg = ("_STATE_DB_ENABLED = True 时，_ATOMIC_WRITE_ENABLED 必须为 True。\n\n"
               "该组合会导致：状态库记录文件已完成，但磁盘上可能是写入中断的半截文件，\n"
               "续传时会静默跳过这些损坏文件。\n\n"
               "请把 _ATOMIC_WRITE_ENABLED 改回 True 后重启。")
        sys.stderr.write("[tk] 配置错误：%s\n" % msg)
        from tkinter import messagebox
        root = tk.Tk()
        root.withdraw()
        messagebox.showerror("配置错误", msg)
        root.destroy()
        return 1

    root = tk.Tk()
    app = file_transfer.FileTransferApp(root)        # noqa: F841 — 由 root 的控件树持有
    root.mainloop()
    return 0


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    mode = None                       # None ⇒ 交给 `host_probe` 开机自检
    port = DEFAULT_PORT
    no_browser = False
    explicit = False                  # 给了任何显式参数 ⇒ 不参与单实例 / 不顺延端口
    i = 0
    while i < len(argv):
        a = argv[i]
        if a in ("--c3",):
            mode = "c3"
            explicit = True
        elif a in ("--c4",):
            mode = "c4"
            explicit = True
        elif a in ("--tk",):
            # 纯 Tk 档：真窗口。**必须绕开 build()** —— 那条路会装替身，
            # 造出来的只是界面模型，屏幕上不会出现任何窗口。见 `run_tk()`。
            mode = "tk"
            explicit = True
        elif a == "--port":
            if i + 1 >= len(argv):
                print("--port 缺少端口号", file=sys.stderr)
                return 2
            try:
                port = int(argv[i + 1])
            except ValueError:
                print("--port 需要 1-65535 的整数：%r" % (argv[i + 1],), file=sys.stderr)
                return 2
            if not (1 <= port <= 65535):
                print("--port 需要 1-65535 的整数：%r" % (port,), file=sys.stderr)
                return 2
            i += 1
            explicit = True
        elif a in ("--no-browser",):
            no_browser = True
            explicit = True
        i += 1

    if not explicit:
        # 正常双击启动：先看有没有已经跑着的实例（见「单实例 / 端口落点」一节）。
        # 显式参数不参与 —— 探针 / 并排调试要能同时开好几个。
        if not _acquire_instance_mutex():
            info = None
            for _ in range(8):                # 对方可能刚起来，等它把服务绑好
                info = _find_running_instance()
                if info:
                    break
                time.sleep(0.25)
            if info:
                _open_existing("http://127.0.0.1:%s/"
                               % (info.get("port") or port))
                return 0

    # 纯 Tk 档要先分走：`build()` 会 `web_kit.install()` 装替身，
    # 一旦装了，`file_transfer` 里的 `tk.Tk()` 就不再是真窗口了。
    if mode == "tk":
        return run_tk()

    bridge = build()
    if mode is None:
        # 开机自检：.NET FW + WebView2 都满足 ⇒ C3；否则 C4。`detect()` 任何异常
        # 都回 C4（详见 host_probe 的判据）。
        import host_probe
        mode = host_probe.detect()
    tries = 1 if explicit else 10
    if mode == "c3":
        return run_c3(bridge, port=port, port_tries=tries)
    return run_c4(bridge, port=port, open_browser=not no_browser,
                  port_tries=tries)


if __name__ == "__main__":
    sys.exit(main())
