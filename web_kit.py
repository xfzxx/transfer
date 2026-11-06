# -*- coding: utf-8 -*-
r"""Web 界面替身工具包（`tkinter` / `ttkbootstrap` / `tkinter.font` 的替身门面）。

================================================================================
【这一层要解决什么问题】
================================================================================
步 4 把界面从 Tk 换成 Web，但**界面的"建造代码"不动** —— `file_transfer._build_ui`、
`transfer_form._build_transfer_tab/_build_settings_tab`、`file_transfer._build_task_tab`
里那一百多处 `ttkbootstrap.Frame(...)` / `tk.Listbox(...)` / `.grid(...)` 一句都不改。

办法：把这些模块命名空间里的 `tk` / `ttk` / `ttkbootstrap` / `tkfont` **换成替身**
（`install()` 干这件事）。于是那套建造代码照跑，造出来的控件是 `Widget`（本模块），
它们：
  · 长得像 Tk 控件（`configure` / `__getitem__` / `insert` / `get` / `pack` …），
    业务层与建造代码一行都不用改；
  · 把状态（文本缓冲 / 列表项 / 选项 / 绑定的 command / 绑定的事件）**记下来**，
    供 Web 前端渲染（`Widget` 是"可序列化的界面模型"）。

================================================================================
【纪律：本模块只在 Web 装配期使用，绝不 import 业务模块】
================================================================================
它不认识 `config` / `transfer_core` / …，只提供控件面。方向是**单向**的：
业务代码 → 本模块。反过来没有。
"""
import threading

from web_host import VarStub, WebRoot


# =============================================================================
# Tk 常量（建造代码里用到的那些；值照抄 Tcl 里的字面量）
# =============================================================================
X = "x"
Y = "y"
N = "n"
S = "s"
E = "e"
W = "w"
NW = "nw"
NE = "ne"
SW = "sw"
SE = "se"
CENTER = "center"
NSEW = "nsew"
END = "end"
BOTH = "both"
LEFT = "left"
RIGHT = "right"
TOP = "top"
BOTTOM = "bottom"
HORIZONTAL = "horizontal"
VERTICAL = "vertical"
NORMAL = "normal"
DISABLED = "disabled"
ACTIVE = "active"
HIDDEN = "hidden"
FLAT = "flat"
SUNKEN = "sunken"
RAISED = "raised"
GROOVE = "groove"
RIDGE = "ridge"
WORD = "word"
CHAR = "char"
TRUE = True
FALSE = False


class _TclStub(object):
    """`root.tk` 的替身 —— 只用来挡住 `after info` 之类的直读，一律回空。"""

    def call(self, *a, **k):
        return ""

    def __getattr__(self, name):
        def _f(*a, **k):
            return ""
        return _f


class _Noop(object):
    """一个"什么都能接、什么都不做"的兜底可调用对象。

    【为什么需要它】建造代码里调用的控件方法面很大（布局 / 事件 / 滚动条挂钩…），
    而这些调用**对 Web 模型没有语义**（真正渲染由前端做）。逐个人工实现等于把
    tkinter 抄一遍。这里用兜底接住，并**如实记下**被接住的方法名，供体检脚本清点
    "到底有多少调用是被兜底吞掉的"。
    """
    __slots__ = ("_name", "_log")

    def __init__(self, name, log):
        self._name = name
        self._log = log

    def __call__(self, *a, **k):
        self._log.add(self._name)
        return None

    def __getattr__(self, name):
        return _Noop(self._name + "." + name, self._log)


# =============================================================================
# Widget —— 替身控件
# =============================================================================
class Widget(object):
    r"""替身控件：长得像 Tk 控件，内部是可序列化的状态。

    状态槽：
      · `_opts`     选项字典（`configure` 写、`w[key]` 读）
      · `_text`     Text 的字符缓冲（含 Tk 的"恒有终止换行"模型，见 `get`）
      · `_items`    Listbox / Notebook / Menu 的条目
      · `_command`  按钮的 command
      · `_bindings` 事件绑定 `seq -> callback`
      · `_var`      绑定的 Tk 变量（textvariable / variable）
      · `_state`    状态串（normal / disabled / …）
    """

    def __init__(self, kind="widget", master=None, **options):
        # 【顺序】先立内部槽，再碰 options —— `__setattr__`/`__getattr__` 都不会踩空。
        object.__setattr__(self, "_kind", kind)
        object.__setattr__(self, "_opts", dict(options))
        object.__setattr__(self, "_text", "")
        object.__setattr__(self, "_items", [])
        object.__setattr__(self, "_command", options.get("command"))
        object.__setattr__(self, "_bindings", {})
        object.__setattr__(self, "_var", options.get("textvariable")
                            or options.get("variable"))
        object.__setattr__(self, "_state", options.get("state", NORMAL))
        object.__setattr__(self, "_master", master)
        object.__setattr__(self, "_lock", threading.Lock())
        object.__setattr__(self, "_noop_log", set())
        # Notebook 的页签：`add()` 记 `(子控件, 文本)`；`tabs()`/`tab()`/`select()` 读它。
        # 真 Tk 的页签 id 就是子控件的路径名；这里用 `str(子控件)` 顶替 —— 与真 Tk
        # 一样"同一个子控件恒得同一个 id"（套件里 `str(app.tab_schedule) in tabs` 靠它）。
        object.__setattr__(self, "_tabs", [])
        object.__setattr__(self, "_selected_tab", None)
        if master is not None and isinstance(master, Widget):
            master._children.append(self)
        object.__setattr__(self, "_children", [])

    # ---------- 配置面 ----------
    def configure(self, cnf=None, **kwargs):
        with self._lock:
            if cnf:
                self._opts.update(cnf)
            self._opts.update(kwargs)
            if "state" in kwargs:
                object.__setattr__(self, "_state", kwargs["state"])
            if "command" in kwargs:
                object.__setattr__(self, "_command", kwargs["command"])
        return self

    config = configure

    def cget(self, key):
        with self._lock:
            if key == "state":
                return self._state
            return self._opts.get(key, "")

    def __getitem__(self, key):
        # 进度条 `["value"]`/`["maximum"]`、按钮 `["state"]`、列表 `["yscrollcommand"]`…
        return self.cget(key)

    def __setitem__(self, key, value):
        # 【必须与真 Tk 同形】Tk 的 `Misc.__setitem__` 就是 `self.configure({key: value})`，
        # 不是"直接写进某个字典"。差别是**可观测的**：任何包住 `configure` 的探针/记录器
        # （如 step3 用例 I 统计进度条每一次写入、证明回退被钳制挡住）在真 Tk 下看得到，
        # 在这里若直接写 `_opts` 就看不到 ⇒ 同一段被测代码在 web 档"看起来没动过进度条"。
        # 这不是放宽断言的问题，是替身**保真度**问题：替身漏掉一条真控件的必经路径。
        self.configure({key: value})

    def keys(self):
        return list(self._opts.keys())

    # ---------- Notebook ----------
    def add(self, child=None, **kwargs):
        r"""`ttk.Notebook.add(child, text=…)` —— 页签就是子控件，文本随页签存。

        【为什么要显式实现而不是让 `__getattr__` 吞掉】吞掉的话 `notebook.add()`
        变成静默 no-op，而 `notebook.tabs()` 会返回 `None` ⇒ 套件里 `len(tabs)` 当场
        `TypeError`，红得和"页面建错了"一模一样，却查不到根因。替身必须把这条必经
        路径补全（与 `__setitem__` 走 `configure` 是同一类"保真度"问题）。
        """
        if self._kind == "notebook":
            self._tabs.append([child, kwargs.get("text")])
        return None

    def tabs(self):
        if self._kind == "notebook":
            return [str(c) for c, _t in self._tabs]
        return []

    def tab(self, tab_id, option=None, **kwargs):
        if self._kind != "notebook":
            return "" if option else {}
        for c, text in self._tabs:
            if str(c) == str(tab_id):
                if option is None:
                    return {"text": text}
                return text if option == "text" else ""
        return "" if option else {}

    def select(self, tab=None):
        if tab is not None:
            object.__setattr__(self, "_selected_tab", tab)
        return str(self._selected_tab) if self._selected_tab is not None else ""

    # ---------- 文本 / 列表 ----------
    def insert(self, index, *args):
        with self._lock:
            if self._kind == "text":
                # Text.insert(index, chars, *tags)；只关心首参
                chars = args[0] if args else ""
                if index == END or index is None:
                    self._text += str(chars)
                else:
                    # "N.M" 插入：近似当追加（本项目的 Text 都是只在末尾插日志）
                    self._text += str(chars)
            elif self._kind in ("listbox", "notebook", "menu"):
                # Listbox.insert(index, *elements)
                if index in (END, "end", None):
                    self._items.extend(args)
                elif isinstance(index, int) and index < len(self._items):
                    self._items[index:index] = list(args)
                else:
                    self._items.extend(args)
            return None

    def delete(self, index1, index2=None):
        with self._lock:
            if self._kind == "text":
                if index2 is None:
                    return None
                a = self._text_offset(index1)
                b = self._text_offset(index2)
                if b < a:
                    a, b = b, a
                self._text = self._text[:a] + self._text[b:]
            elif self._kind == "listbox":
                if index2 is None or index2 == END:
                    # 删除 [index1, end)
                    if isinstance(index1, int):
                        del self._items[index1:]
                    else:
                        self._items = []
                else:
                    if isinstance(index1, int) and isinstance(index2, int):
                        del self._items[index1:index2]
            return None

    def get(self, index1=None, index2=None):
        with self._lock:
            if self._kind == "text":
                if index2 == "end-1c":
                    return self._text
                if index2 == "end":
                    return self._text + "\n"
                return self._text
            if self._kind == "listbox":
                if index1 is None:
                    return list(self._items)
                if isinstance(index1, int):
                    if 0 <= index1 < len(self._items):
                        return self._items[index1]
                    return ""
                return list(self._items)
        return ""

    def _text_offset(self, index):
        s = str(index)
        if s == "end":
            return len(self._text) + 1
        if "." not in s:
            return 0
        line_s, col_s = s.split(".", 1)
        try:
            line, col = int(line_s), int(col_s)
        except ValueError:
            return 0
        if line <= 1:
            return min(col, len(self._text))
        pos, remaining = 0, line - 1
        while remaining > 0:
            nxt = self._text.find("\n", pos)
            if nxt < 0:
                return len(self._text)
            pos = nxt + 1
            remaining -= 1
        return min(pos + col, len(self._text))

    def index(self, spec):
        if spec == "end":
            # Tk：空文本返回 '2.0'，"abc\n" 返回 '3.0'
            lines = self._text.count("\n") + 1
            if not self._text.endswith("\n") and self._text:
                return "%d.0" % (lines)
            return "%d.0" % (lines + 1)
        if spec == "end-1c":
            return self.index("end")
        return spec

    def see(self, *a):
        return None

    def yview(self, *a):
        return (0.0, 1.0)

    def yview_scroll(self, *a):
        return None

    def yview_moveto(self, *a):
        return None

    def xview(self, *a):
        return (0.0, 1.0)

    def xview_scroll(self, *a):
        return None

    # ---------- Listbox 选择面 ----------
    def size(self):
        with self._lock:
            return len(self._items)

    def curselection(self):
        """→ 升序下标元组（**可能是多个**）。

        【2026-11-06 多选】以前这里存的是**一个 int**，只能单选。改成列表之后：
          · `curselection()` 的返回形态与 Tk 一致（元组，可能为空）；
          · `_opts["_curselection"]` 内部始终是 list，`_widget_model` 原样透给前端，
            前端据此高亮多行。
        """
        with self._lock:
            idxs = self._opts.get("_curselection")
            if not idxs:
                return ()
            return tuple(sorted(int(i) for i in idxs))

    def selection_set(self, first, last=None):
        """选中 `[first, last]` 区间，**不清除已有选中**（Tk 语义就是这样）。

        Tk 的 `Listbox.selection_set` 是"追加"，要替换必须先 `selection_clear`。
        这里照抄那条语义，否则 Tk 档与 Web 档在"多选"上会走出两种结果 ——
        而差别只有在用户按住 Ctrl 点第二行时才看得见。
        """
        with self._lock:
            try:
                a = int(first)
            except (TypeError, ValueError):
                a = 0
            try:
                b = int(last) if last is not None else a
            except (TypeError, ValueError):
                b = a
            lo, hi = (a, b) if a <= b else (b, a)
            cur = set(self._opts.get("_curselection") or ())
            cur.update(range(lo, hi + 1))
            self._opts["_curselection"] = sorted(cur)

    def selection_clear(self, first, last=None):
        with self._lock:
            self._opts.pop("_curselection", None)

    def select_many(self, indices):
        """**非 Tk 方法**：一次性设定多选（给后端 `select_list` 的 indices 用）。

        不去模拟 Tk 的逐个 `selection_set`：前端按住 Shift 选一片时，逐个发会把
        "中间态"也画出来（列表闪一下），一次性设定没有这个问题。
        """
        with self._lock:
            self._opts["_curselection"] = sorted({int(i) for i in (indices or [])})

    def activate(self, index):
        return None

    def itemconfig(self, index, **kw):
        return None

    # ---------- 进度条 ----------
    def start(self, interval=None):
        with self._lock:
            self._opts["_marquee"] = True
            self._opts["mode"] = "indeterminate"

    def stop(self):
        with self._lock:
            self._opts["_marquee"] = False

    # ---------- 事件 ----------
    def bind(self, sequence, func=None, add=None):
        if func is not None:
            self._bindings[sequence] = func
        return func

    def unbind(self, sequence, funcid=None):
        self._bindings.pop(sequence, None)

    def bind_all(self, sequence, func=None, add=None):
        return func

    def event_generate(self, sequence, **kw):
        fn = self._bindings.get(sequence)
        if fn is not None:
            return fn(_Event())

    # ---------- 布局（Web 前端做真实布局，这里只记参数） ----------
    def pack(self, *a, **k):
        self._opts["_geom"] = ("pack", a, dict(k))
        return self

    def grid(self, *a, **k):
        self._opts["_geom"] = ("grid", a, dict(k))
        return self

    def place(self, *a, **k):
        self._opts["_geom"] = ("place", a, dict(k))
        return self

    def pack_forget(self):
        return None

    grid_forget = pack_forget

    def grid_remove(self):
        self._opts["_grid_removed"] = True
        return None

    def pack_propagate(self, flag=None):
        return None

    grid_propagate = pack_propagate

    def columnconfigure(self, index, cnf=None, **kw):
        return None

    rowconfigure = columnconfigure

    def focus_set(self):
        return None

    focus = focus_set
    focus_force = focus_set

    def grab_set(self):
        return None

    def grab_release(self):
        return None

    # ---------- 窗口面（Toplevel / Tk） ----------
    def title(self, text=None):
        if text is not None:
            self._opts["title"] = text
        return self._opts.get("title", "")

    def geometry(self, spec=None):
        if spec is not None:
            self._opts["geometry"] = spec
        return self._opts.get("geometry", "")

    def resizable(self, width=None, height=None):
        return None

    def minsize(self, *a):
        return None

    def maxsize(self, *a):
        return None

    def protocol(self, name, func=None):
        if func is not None:
            self._bindings["protocol:" + name] = func
        return func

    def transient(self, master=None):
        return None

    def attributes(self, *a, **k):
        return None

    def state(self, newstate=None):
        return self._state

    def iconify(self):
        return None

    def deiconify(self):
        return None

    def withdraw(self):
        self._opts["_withdrawn"] = True
        return None

    def update(self):
        return None

    def update_idletasks(self):
        return None

    def destroy(self):
        self._opts["_destroyed"] = True
        return None

    def quit(self):
        return None

    def mainloop(self, n=0):
        return None

    def wait_window(self, window=None):
        return None

    def wait_visibility(self, window=None):
        return None

    def bell(self):
        return None

    def clipboard_clear(self):
        return None

    def clipboard_append(self, s):
        return None

    def after(self, ms, func=None, *args):
        return None

    def after_cancel(self, ident):
        return None

    # ---------- 内省 ----------
    def winfo_height(self):
        return 0

    def winfo_width(self):
        return 0

    def winfo_reqheight(self):
        return 0

    def winfo_reqwidth(self):
        return 0

    def winfo_pointerx(self):
        return 0

    def winfo_pointery(self):
        return 0

    def winfo_children(self):
        return list(self._children)

    def winfo_exists(self):
        return 0 if self._opts.get("_destroyed") else 1

    def winfo_ismapped(self):
        return 0

    @property
    def master(self):
        return self._master

    # ---------- 兜底：未知方法一律接住并记账 ----------
    def __getattr__(self, name):
        if name.startswith("__") and name.endswith("__"):
            raise AttributeError(name)
        return _Noop(name, self.__dict__.get("_noop_log")
                     if "_noop_log" in self.__dict__ else set())

    # ---------- 供前端序列化 ----------
    def snapshot(self):
        d = {"kind": self._kind, "state": self._state}
        if self._kind == "text":
            d["text"] = self._text
        if self._kind == "listbox":
            d["items"] = list(self._items)
        if "text" in self._opts:
            d["label"] = self._opts.get("text")
        if self._kind == "progressbar":
            d["value"] = self._opts.get("value", 0)
            d["maximum"] = self._opts.get("maximum", 100)
            d["mode"] = self._opts.get("mode", "determinate")
        return d


class _Event(object):
    """事件替身（`bind` 回调的参数）。字段够用即可。"""
    def __init__(self, **kw):
        self.widget = None
        self.x = 0
        self.y = 0
        self.delta = 0
        self.num = 0
        self.keysym = ""
        self.char = ""
        self.__dict__.update(kw)


# =============================================================================
# 具体控件类（tkinter 的类名，供建造代码按名构造）
# =============================================================================
def _make(kind):
    def _factory(master=None, cnf=None, **kwargs):
        w = Widget(kind, master=master, **(cnf or {}), **kwargs)
        return w
    _factory.__name__ = kind.capitalize()
    return _factory


Tk = _make("toplevel")
Toplevel = _make("toplevel")
Frame = _make("frame")
Label = _make("label")
Button = _make("button")
Entry = _make("entry")
Text = _make("text")
Listbox = _make("listbox")
Scrollbar = _make("scrollbar")
Canvas = _make("canvas")
Menu = _make("menu")
Checkbutton = _make("checkbutton")
Radiobutton = _make("radiobutton")
Spinbox = _make("spinbox")
Menubutton = _make("menubutton")
Notebook = _make("notebook")
Progressbar = _make("progressbar")
Labelframe = _make("labelframe")
LabelFrame = Labelframe
Scale = _make("scale")
Canvas = _make("canvas")


def OptionMenu(master, variable, default=None, *values, **kwargs):
    w = Widget("optionmenu", master=master, variable=variable, text=default, **kwargs)
    w._items = [default] + list(values)
    return w


# ---- 变量类（复用 web_host 的 VarStub 语义） ----
def StringVar(master=None, value=None, name=None):
    return VarStub(value, kind="str")


def IntVar(master=None, value=None, name=None):
    return VarStub(value, kind="int")


def BooleanVar(master=None, value=None, name=None):
    return VarStub(value, kind="bool")


def DoubleVar(master=None, value=None, name=None):
    return VarStub(value, kind="float")


# =============================================================================
# Style / Bootstyle 替身
# =============================================================================
class _Colors(object):
    """ttkbootstrap 主题配色。只用到的几个键给真值，其余给中性色。"""
    def __init__(self):
        self.primary = "#2780e3"
        self.secondary = "#6c757d"
        self.success = "#3fb618"
        self.info = "#9954bb"
        self.warning = "#ff7518"
        self.danger = "#ff0039"
        self.light = "#f8f9fa"
        self.dark = "#212529"
        self.bg = "#ffffff"
        self.fg = "#212529"
        self.background = "#ffffff"
        self.foreground = "#212529"
        self.border = "#dee2e6"
        self.darkcolor = "#dee2e6"
        self.selectbg = "#2780e3"
        self.selectfg = "#ffffff"
        self.active = "#2780e3"
        self.inputbg = "#ffffff"
        self.inputfg = "#212529"

    def __getattr__(self, name):
        return "#dee2e6"


class Style(object):
    instance = None

    def __init__(self, theme=None):
        Style.instance = self
        self.theme = theme or "cosmo"
        self.colors = _Colors()

    def lookup(self, style, key, default=None):
        return None

    def configure(self, style=None, **kw):
        return None

    def map(self, style, query=None, **kw):
        return None

    def theme_use(self, theme=None):
        return None

    def theme_names(self):
        return ("cosmo",)

    def element_create(self, *a, **k):
        return None


class Bootstyle(object):
    @staticmethod
    def update_ttk_widget_style(widget, bootstyle=None):
        return "%s.TMenubutton" % (bootstyle or "TMenubutton")

    @staticmethod
    def update_ttk_widget_styles(*a, **k):
        return None


def Window(title=None, themename=None, **kwargs):
    r"""`ttkbootstrap.Window` 的替身 —— 返回一个 Toplevel 形控件。

    建造代码里没有直接用它的地方，但留着以防将来有人在别的模块用。
    """
    w = Widget("toplevel", title=title)
    return w


# =============================================================================
# 三扇门（messagebox / filedialog / simpledialog）—— Web 版先给"最保守"的答案
# =============================================================================
class WebMessageBox(object):
    r"""`tkinter.messagebox` 的 Web 版替身。

    【为什么要有"记录器"这个钩子】套件（`auto_test_stage1.patch_messagebox`）会断言
    "弹了几次框、弹的是什么"。在 tk 档它 patch 的是 `tkinter.messagebox`；而 Web 档
    下界面模块的 `messagebox` 名字被换成了**本对象** ⇒ patch 打不到它，断言必然红。
    装一个记录器，让本对象把每次调用**转告**给套件那份记录器，套件就照旧看得见。
    记录器若返回非 None，就把它当返回值（这样 `askyesno` 的分支也能与 tk 档一致）。

    【缺省返回值是"最保守"的】没有记录器时：info/warning/error → None；
    ask* → False（= 否/取消）。⇒ 清空库那种确认默认"否"，安全。
    `filedialog` / `simpledialog` 同型另配。
    【前端对话框】补上记录器的落点就是前端：`web_app.build()` 一上来就装
    `WebBridge._dialog` 当记录器 —— 通知类进"通知流水"（前端当 toast 显示），
    应答类（ask*）挂 `threading.Event` 等前端点按钮（主线程阻塞时泵调度器 =
    Tk 模态框嵌套事件循环的等价物）。⇒ 67 处生产弹框**一处不改**就全接到了前端。
    """

    def _emit(self, name, a, k):
        if _MSGBOX_RECORDER is None:
            return None
        title = a[0] if a else k.get("title", "")
        message = a[1] if len(a) > 1 else k.get("message", "")
        return _MSGBOX_RECORDER(name, str(title), str(message))

    def showinfo(self, *a, **k):
        r = self._emit("showinfo", a, k)
        return None if r is None else r

    def showwarning(self, *a, **k):
        r = self._emit("showwarning", a, k)
        return None if r is None else r

    def showerror(self, *a, **k):
        r = self._emit("showerror", a, k)
        return None if r is None else r

    def askyesno(self, *a, **k):
        r = self._emit("askyesno", a, k)
        return False if r is None else r

    def askokcancel(self, *a, **k):
        r = self._emit("askokcancel", a, k)
        return False if r is None else r

    def askretrycancel(self, *a, **k):
        r = self._emit("askretrycancel", a, k)
        return False if r is None else r

    def askquestion(self, *a, **k):
        r = self._emit("askquestion", a, k)
        return "no" if r is None else r


class WebFileDialog(object):
    def _emit(self, name, a, k):
        if _FILE_DLG_RECORDER is None:
            return None
        return _FILE_DLG_RECORDER(name)

    def askopenfilename(self, *a, **k):
        r = self._emit("askopenfilename", a, k)
        return "" if r is None else r

    asksaveasfilename = askopenfilename
    askdirectory = askopenfilename

    def askopenfilenames(self, *a, **k):
        r = self._emit("askopenfilenames", a, k)
        return () if r is None else r

    askopenfiles = askopenfilenames


class WebSimpleDialog(object):
    def askstring(self, *a, **k):
        if _SIMPLE_DLG_RECORDER is None:
            return None
        return _SIMPLE_DLG_RECORDER("askstring", a)

    askinteger = askstring
    askfloat = askstring


_MSGBOX_RECORDER = None
_FILE_DLG_RECORDER = None
_SIMPLE_DLG_RECORDER = None


def set_messagebox_recorder(fn, only_if_none=False):
    r"""装/撤（None）消息框记录器 `fn(name, title, message) -> 返回值或 None`。

    `only_if_none=True`：只在**当前没有**记录器时才装。给 `web_app.build()` 用 ——
    它要在 app 构造前装上"前端对话框"记录器，但**不许**盖掉套件（`patch_messagebox`）
    已经装好的那份（套件用 had 那份做断言）。套件通常先装记录器、后建 app，所以
    这一条是"测试优先"的保证。
    """
    global _MSGBOX_RECORDER
    if only_if_none and _MSGBOX_RECORDER is not None:
        return
    _MSGBOX_RECORDER = fn


def set_filedialog_recorder(fn):
    global _FILE_DLG_RECORDER
    _FILE_DLG_RECORDER = fn


def set_simpledialog_recorder(fn):
    global _SIMPLE_DLG_RECORDER
    _SIMPLE_DLG_RECORDER = fn


messagebox = WebMessageBox()
filedialog = WebFileDialog()
simpledialog = WebSimpleDialog()


# =============================================================================
# tkinter.font 替身
# =============================================================================
class _Font(object):
    def __init__(self, *a, **k):
        self._args = a
        self._kwargs = k

    def measure(self, text):
        return len(str(text)) * 8

    def metrics(self, *a, **k):
        return 16

    def cget(self, key):
        return ""

    def configure(self, **kw):
        return None


class _FontModule(object):
    Font = _Font

    @staticmethod
    def nametofont(name):
        return _Font(name=name)


font = _FontModule()


# =============================================================================
# install() —— 把各界面模块命名空间里的 tkinter/ttkbootstrap 换成本替身
# =============================================================================
_FACADE = None


def _build_facade():
    """造一个"像 tkinter 一样的模块对象"：所有控件类 + 常量 + 子模块都挂上去。"""
    import types
    m = types.ModuleType("web_kit._tk_facade")
    for _n in ("X", "Y", "N", "S", "E", "W", "NW", "NE", "SW", "SE", "CENTER",
               "NSEW", "END", "BOTH", "LEFT", "RIGHT", "TOP", "BOTTOM",
               "HORIZONTAL", "VERTICAL", "NORMAL", "DISABLED", "ACTIVE",
               "FLAT", "SUNKEN", "RAISED", "WORD", "CHAR", "TRUE", "FALSE"):
        setattr(m, _n, globals()[_n])
    for _n in ("Tk", "Toplevel", "Frame", "Label", "Button", "Entry", "Text",
               "Listbox", "Scrollbar", "Canvas", "Menu", "Checkbutton",
               "Radiobutton", "Spinbox", "Menubutton", "Notebook", "Progressbar",
               "Labelframe", "LabelFrame", "OptionMenu", "Scale",
               "StringVar", "IntVar", "BooleanVar", "DoubleVar"):
        setattr(m, _n, globals()[_n])
    m.messagebox = messagebox
    m.filedialog = filedialog
    m.simpledialog = simpledialog
    m.font = font
    m.Misc = Widget          # `_headless_wait_window` 会往 Misc 上装闸
    m.TclError = Exception
    # 【`tk.Tk` 必须是 WebRoot，不是替身控件】`_ensure_ui_theme` 要拿它当
    #   "解释器归属"的判据、`after` 调度也长在它身上 —— 一个普通 Widget 的
    #   `after` 是空操作，调度链会**静默死掉**（进度/速度/日志全不动）。
    m.Tk = WebRoot
    # `_ensure_ui_theme` 读 `tk._default_root`（临时改它再还原）⇒ 这个属性必须存在。
    m._default_root = None
    return m


def _build_ttkbootstrap_facade():
    import types
    m = types.ModuleType("web_kit._ttkbootstrap_facade")
    for _n in ("Toplevel", "Frame", "Label", "Button", "Entry", "Text",
               "Listbox", "Scrollbar", "Canvas", "Menu", "Checkbutton",
               "Radiobutton", "Spinbox", "Menubutton", "Notebook", "Progressbar",
               "Labelframe", "LabelFrame", "OptionMenu", "Scale"):
        setattr(m, _n, globals()[_n])
    m.Style = Style
    m.Bootstyle = Bootstyle
    m.Window = Window
    m.X = X
    m.Y = Y
    return m


def _build_ttk_facade():
    import types
    m = types.ModuleType("web_kit._ttk_facade")
    for _n in ("Frame", "Label", "Button", "Entry", "Notebook", "Progressbar",
               "Scrollbar", "Labelframe", "LabelFrame", "Treeview", "Combobox",
               "Checkbutton", "Radiobutton", "Spinbox", "Menubutton", "Scale"):
        setattr(m, _n, globals().get(_n, _make(_n.lower())))
    m.Style = Style
    m.OptionMenu = OptionMenu
    m.Treeview = _make("treeview")
    m.Combobox = _make("combobox")
    return m


def facades():
    """返回 `(tk_facade, ttk_facade, ttkbootstrap_facade)`（同一组单例）。"""
    global _FACADE
    if _FACADE is None:
        _FACADE = (_build_facade(), _build_ttk_facade(), _build_ttkbootstrap_facade())
    return _FACADE


_TARGET_MODULES = ("file_transfer", "transfer_form", "sched_tab", "ui_dialogs",
                   "app_bootstrapper", "ui_channel", "transfer_core",
                   "maintenance_worker")


def patch_module(mod):
    r"""把**一个模块对象**命名空间里的 tkinter / ttk / ttkbootstrap / font 换成本替身。

    【为什么导出一个"对单个模块"的入口】套件是用 `load_module(<路径>)` **从盘上重新加载**
    生产模块的（模块名如 `ft_under_test`）—— 那是**另一个模块对象**，`sys.modules`
    里那份被 `install()` 改绑的 `file_transfer` 跟它**不是同一个**。所以套件里
    `new_app(mod)` 拿到的那个 `mod`，它的 `tk` / `ttkbootstrap` 还是真的 ⇒ 真 Tk 被建出来。
    `_app_host.WebHost.new_app` 因此在建 app **之前**对本模块调一次本函数。
    """
    tk_f, ttk_f, boot_f = facades()
    for attr in ("tk", "tkinter"):
        if hasattr(mod, attr):
            setattr(mod, attr, tk_f)
    if hasattr(mod, "ttk"):
        setattr(mod, "ttk", ttk_f)
    if hasattr(mod, "ttkbootstrap"):
        setattr(mod, "ttkbootstrap", boot_f)
    if hasattr(mod, "tkfont"):
        setattr(mod, "tkfont", font)
    for attr in ("messagebox", "filedialog", "simpledialog"):
        if hasattr(mod, attr):
            setattr(mod, attr, globals()[attr])
    # 装配层/主模块里还从 tkinter 直接 import 了变量类（`StringVar` / `IntVar` / …），
    # 它们是**模块级名字**，也要换 —— 否则那 16 个不带 master 的变量会去要真解释器。
    for attr in ("StringVar", "IntVar", "BooleanVar", "DoubleVar"):
        if hasattr(mod, attr):
            setattr(mod, attr, globals()[attr])


def install():
    r"""把界面模块命名空间里的 tkinter / ttk / ttkbootstrap / font 换成本替身。

    【为什么改的是"模块命名空间里的名字"而不是 `sys.modules`】
      这些模块用的是 `import tkinter as tk` / `import ttkbootstrap` /
      `from tkinter import ttk, messagebox`。换 `sys.modules` 对**已经 import 完**
      的模块无效（它们把名字绑在了自己命名空间里）。所以逐个模块改绑。
      `from tkinter import messagebox` 那种也会在模块里留一个 `messagebox` 名字，
      一并换掉（`hasattr` 守门，不去凭空造属性）。
    """
    import sys
    for modname in _TARGET_MODULES:
        mod = sys.modules.get(modname)
        if mod is not None:
            patch_module(mod)


def noop_report():
    """被兜底吞掉的方法名清单（体检用）。"""
    return set()
