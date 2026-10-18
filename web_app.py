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
                             与双模主循环：
                               · C3（pywebview）：宿主事件循环（主线程）；
                               · C4（本地 HTTP + 系统浏览器）：HTTP 服务在后台线程，
                                 主线程泵 `SchedulerStub.run_pending()`。
                             两模共用**同一份**前端与同一份桥（`WebBridge`）。

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


    @staticmethod
    def _jsonable(v):
        if isinstance(v, bool) or isinstance(v, (int, float)) or v is None:
            return v
        if isinstance(v, str):
            return v
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
            "messages": msgs,
        }

    def snapshot(self):
        return self.call_on_main(self._snapshot)

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
    def fs_list(path):
        r"""列出一个目录下的**子目录**（给源/目标选择器用）。纯本机、只读。

        返回 `{"path": <绝对路径>, "parent": <上一级或 None>, "dirs": [名字…]}`。
        盘符根（`C:\`）的上一级是 `None`（再往上没有意义）。
        """
        path = os.path.abspath(path or os.path.expanduser("~"))
        if not os.path.isdir(path):
            raise ValueError("不是目录：%s" % path)
        dirs = []
        try:
            with os.scandir(path) as it:
                for e in it:
                    try:
                        if e.is_dir() and not e.name.startswith("$"):
                            dirs.append(e.name)
                    except OSError:
                        continue
        except OSError as e:
            raise ValueError("打不开目录：%s" % e)
        dirs.sort(key=str.lower)
        parent = os.path.dirname(path.rstrip("\\/"))
        if not parent or parent == path:
            parent = None
        return {"path": path, "parent": parent, "dirs": dirs}

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

        def _read_json(self):
            n = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(n) if n else b""
            if not raw:
                return {}
            return json.loads(raw.decode("utf-8"))

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
                        bridge.fs_list((qs.get("path") or [""])[0]),
                        ensure_ascii=False))
                except Exception as e:                  # noqa: BLE001
                    return self._send(200, json.dumps({"ok": False,
                                                       "error": str(e)}))
            if path == "/api/fs/drives":
                return self._send(200, json.dumps(bridge.fs_drives(),
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

        def _serve_static(self, rel):
            rel = rel.replace("\\", "/").lstrip("/")
            full = os.path.normpath(os.path.join(WEB_DIR, rel))
            if not full.startswith(os.path.normpath(WEB_DIR)) or not os.path.isfile(full):
                return self._send(404, json.dumps({"error": "not found"}))
            ctype = {
                ".html": "text/html; charset=utf-8",
                ".js": "text/javascript; charset=utf-8",
                ".css": "text/css; charset=utf-8",
            }.get(os.path.splitext(full)[1], "application/octet-stream")
            with open(full, "rb") as f:
                return self._send(200, f.read(), ctype)

    return Handler


def run_c4(bridge, port=DEFAULT_PORT, open_browser=True):
    """C4：标准库 HTTP 服务（后台线程）+ 主线程泵 `SchedulerStub`。**只听 127.0.0.1**。"""
    from http.server import ThreadingHTTPServer
    import webbrowser

    srv = ThreadingHTTPServer(("127.0.0.1", port), _make_handler(bridge))
    t = threading.Thread(target=srv.serve_forever, daemon=True,
                         name="web-http-c4")
    t.start()
    url = "http://127.0.0.1:%d/" % port
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
            root.run_pending()
            d = root.next_due_in()
            time.sleep(0.01 if d is None else min(max(d, 0.0), 0.02))
    finally:
        srv.shutdown()
        srv.server_close()
    return 0


# =============================================================================
# C3：pywebview 窗口（宿主事件循环 = pywebview 的循环，主线程）
# =============================================================================
def run_c3(bridge, port=DEFAULT_PORT, title=None):
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
    from http.server import ThreadingHTTPServer

    import ui_channel

    old_assert = getattr(ui_channel, "_DEBUG_UI_THREAD_ASSERT", True)
    ui_channel._DEBUG_UI_THREAD_ASSERT = False

    srv = ThreadingHTTPServer(("127.0.0.1", port), _make_handler(bridge))
    threading.Thread(target=srv.serve_forever, daemon=True,
                     name="web-http-c3").start()
    url = "http://127.0.0.1:%d/" % port
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

    webview.create_window(title or root._title or "文件传输工具 v2.4",
                          url=url)
    try:
        webview.start()
    finally:
        stop.set()
        ui_channel._DEBUG_UI_THREAD_ASSERT = old_assert
        srv.shutdown()
        srv.server_close()
    return 0


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    mode = None                       # None ⇒ 交给 `host_probe` 开机自检
    port = DEFAULT_PORT
    no_browser = False
    i = 0
    while i < len(argv):
        a = argv[i]
        if a in ("--c3",):
            mode = "c3"
        elif a in ("--c4",):
            mode = "c4"
        elif a == "--port":
            i += 1
            port = int(argv[i])
        elif a in ("--no-browser",):
            no_browser = True
        i += 1

    bridge = build()
    if mode is None:
        # 开机自检：.NET FW + WebView2 都满足 ⇒ C3；否则 C4。`detect()` 任何异常
        # 都回 C4（详见 host_probe 的判据）。
        import host_probe
        mode = host_probe.detect()
    if mode == "c3":
        return run_c3(bridge, port=port)
    return run_c4(bridge, port=port, open_browser=not no_browser)


if __name__ == "__main__":
    sys.exit(main())
