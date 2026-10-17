# -*- coding: utf-8 -*-
r"""Web 双模宿主的**替身层**（步 1：只做三个 Stub，不含前端、不含 HTTP 服务）。

================================================================================
【这一层是什么】
================================================================================
乙重构把上帝对象拆成了四层（见项目记忆的依赖分层）：

    纯引擎（零界面依赖）    config · state_db · ftp_transfer · ftp_client
    逻辑带弹框              transfer_core · maintenance_worker
    接缝                    ui_channel          ← 引擎 → 界面的唯一通道
    界面 + 装配             app_bootstrapper · file_transfer · transfer_form · …

**关键结构事实（本文件成立的唯一理由）**：`transfer_core.TransferCore.__init__`
按 22 个关键字参数把 Tk 变量**当普通对象收下来**（`self.source_path = source_path`
…），运行期只对它调 `.get()` / `.set()`。`ui_channel.UIChannel` 同理：它持有一个
"root"和三个控件，只对它们调 `after` / `after_cancel` / `configure` / 下标。

⇒ **只要这三个位置的对象长得像 Tk 的那几个东西，业务层一行都不用改。**
本文件就是那三个"长得像"的替身。它们**不是** Tk 的包装（不 import tkinter），
也**不是**前端（不认识 HTML）。它们是引擎与前端之间那段可替换的垫层。

================================================================================
【三个 Stub 的分工 —— 由机械枚举得出，不是猜的】
================================================================================
用法：`python _probe_stub_contract.py` 复现下表（枚举脚本随本文件一起交付）。

    谁在调                               对象            实际用到的方法
    ─────────────────────────────────────────────────────────────────────
    transfer_core（12 个变量）           VarStub         get / set
    ui_channel（log_level/log_export_dir）VarStub        get / set
    transfer_core（4 个按钮）            WidgetStub      configure(state=, text=)
    maintenance_worker（retry_btn）      WidgetStub      configure(state=)
    ui_channel（progress）               WidgetStub      configure / start / stop / ["value"] / ["maximum"]
    ui_channel（log_text）               WidgetStub      configure / insert / delete / see / get
    ui_channel（speed_label）            WidgetStub      configure(text=)
    ui_channel（root）                   SchedulerStub   after / after_cancel

**枚举面之外一个都不实现**（"响亮失败"优先于"静默兼容"）：
将来谁多调了一个方法，直接 AttributeError，而不是悄悄什么都不做。

================================================================================
【"逐字节一致"的依据 —— Tk 语义是实测的，不是查文档猜的】
================================================================================
判据要求"Stub 与 Tk 逐字节相同"。差一个字符（`end-1c` 到底去掉几个字符、
空 Text 的 `get` 返回什么、`BooleanVar.set("no")` 得到什么）就会让
日志导出 / 行数截断产生肉眼看不见的偏差。所以下面每条语义都在真 Tk 上跑过
（探针 `probe_tk_semantics.py`，输出留档）：

  A. Text：内部模型 = "用户插入内容的拼接" + **恒有的一个终止换行**
       · 空 Text            get("1.0","end-1c") == ""         get("1.0","end") == "\n"
       · insert "abc\n"     get("1.0","end-1c") == "abc\n"
       · insert "xyz"（无尾换行） get("1.0","end-1c") == "xyz"
       · delete("1.0","3.0") on "l1\nl2\nl3\n" ⇒ "l3\n"
       · index("end")：空='2.0' / "abc\n"='3.0' / "xyz"='2.0' / 三行='5.0'
  B. 变量：StringVar.set(4)→'4'；StringVar.set(False)→**'0'**（不是 'False'）
       IntVar.set(True)→1；BooleanVar.set('no')→False
       新变量初值：StringVar='' / IntVar=0 / BooleanVar=False
  C. Progressbar：configure(value=3) 后 ["value"]==3；["value"]=50 后 ==50
       ["value"] / ["maximum"] 都是 **int**
  D. 按钮：configure(state="disabled") 后 ["state"]=="disabled"（str）

================================================================================
【SchedulerStub 为什么是"主线程泵"而不是"另起一条线程跑定时器"】
================================================================================
`ui_channel` 里有 8 处线程断言，形态统一是
`assert threading.current_thread() is threading.main_thread()`。
⇒ **UI 回调必须在真正的 main thread 上执行。**

所以 SchedulerStub **不能**用 `threading.Timer`（那会在定时器线程里跑回调，
断言当场挂）。它只是一个**到期队列**：
  · `after(ms, fn, *args)`  —— 任何线程可调（含 worker 线程序列化投递），只**入队**；
  · `run_pending()`         —— **由宿主主循环在主线程调**，执行所有到期回调。

这正是双模要的形状：
  · C3（pywebview）：宿主主循环 = pywebview 的事件循环（主线程）；
  · C4（本地 HTTP + 系统浏览器）：宿主主循环 = HTTP 服务的等待循环（主线程）。
两者都在主线程，各自按自己的节奏调 `run_pending()` 即可 —— **前端代码逐字节相同**。
"""
import heapq
import itertools
import threading
import time

__all__ = ["VarStub", "WidgetStub", "SchedulerStub"]


# =============================================================================
# Tk 值语义转换（照抄实测结果，见模块 docstring 的 B/C/D）
# =============================================================================
def _to_str(v):
    """Tk StringVar 的 `set` 语义：任何值先变字符串。

    实测：`set(4)` → `'4'`，`set(False)` → `'0'`（**不是** `'False'`）。
    bool 必须先走 int —— 否则 `str(False)` 会给出 Tcl 里不存在的 `'False'`。
    """
    if isinstance(v, bool):
        return "1" if v else "0"
    return str(v)


def _to_int(v):
    """Tk IntVar 的 `set` 语义。

    实测：`set(True)` → `1`（bool 当 0/1）；`set('7')` → `7`。
    非数字串在 Tk 里抛 `TclError` —— 这里抛 `ValueError`（同型：响亮失败）。
    """
    if isinstance(v, bool):
        return 1 if v else 0
    if isinstance(v, int):
        return v
    if isinstance(v, float):
        return int(v)
    s = str(v).strip()
    if s == "":
        raise ValueError("IntVar 不接受空串（Tk 同样报错）")
    return int(float(s))


_BOOL_FALSE = ("", "0", "false", "no", "off")
_BOOL_TRUE = ("1", "true", "yes", "on")


def _to_bool(v):
    """Tk BooleanVar 的 `set` 语义（Tcl 的 boolean 规则）。

    实测：`set(False)`→False，`set(0)`→False，`set('false')`→False，
          `set('no')`→False，`set(1)`→True。
    既非真值词也非假值词 ⇒ `ValueError`（Tk 抛 `TclError`，同型）。
    """
    if isinstance(v, bool):
        return v
    if isinstance(v, (int, float)):
        return v != 0
    s = str(v).strip().lower()
    if s in _BOOL_FALSE:
        return False
    if s in _BOOL_TRUE:
        return True
    raise ValueError("BooleanVar 不接受 %r（Tk 同样报错）" % (v,))


# =============================================================================
# ① VarStub —— 替代 StringVar / IntVar / BooleanVar
# =============================================================================
class VarStub(object):
    """Tk 变量的替身：`get()` / `set()` / `trace_add()`。

    【kind 是干什么的】Tk 的三种变量各有自己的「存进去 → 取出来」转换规则
    （见模块 docstring B 段实测）。`kind` 就是复刻这条规则：
        kind="str"   ⇔ StringVar
        kind="int"   ⇔ IntVar
        kind="bool"  ⇔ BooleanVar
    不传 `kind`（None）时按 **raw** 处理：原样存、原样取。raw 只给"确定不会
    被当 Tk 变量读类型"的位置用；凡是原来在 Tk 里是三种变量之一的，一律
    显式给 `kind` —— 这样"转换规则"在装配处一眼可见，不靠读代码猜。

    【初值】与 Tk 对齐：str → `""`；int → `0`；bool → `False`；
    raw 且未给 `init` → `None`。Tk 的变量在没 set 过时就是这个值，
    业务层有代码靠这个初值分支（例：`flatten.get()` 为假才不走展平）。
    """

    __slots__ = ("_kind", "_value", "_traces", "_lock")

    def __init__(self, value=None, kind=None):
        if kind not in (None, "str", "int", "bool"):
            raise ValueError("VarStub: 未知的 kind=%r" % (kind,))
        self._kind = kind
        self._traces = []
        self._lock = threading.Lock()
        if value is None:
            self._value = {"str": "", "int": 0, "bool": False}.get(kind)
        else:
            self._value = self._coerce(value)

    # ---- 语义转换 ----
    def _coerce(self, v):
        if self._kind == "str":
            return _to_str(v)
        if self._kind == "int":
            return _to_int(v)
        if self._kind == "bool":
            return _to_bool(v)
        return v

    # ---- Tk 变量接口（机械枚举面：get / set） ----
    def get(self):
        with self._lock:
            return self._value

    def set(self, value):
        new = self._coerce(value)
        with self._lock:
            old = self._value
            self._value = new
            traces = list(self._traces)
        # trace 回调在**锁外**触发：Tk 里回调函数体不能反过来再拿这把锁时死锁。
        # 与 Tk 一致：回调收到的是"变量名/下标/操作"三个串，这里给三个占位串。
        for fn in traces:
            fn("", "", "write")

    # ---- 装配层预留：Tk 的 write trace（业务层**不用**，见模块 docstring 枚举面） ----
    def trace_add(self, mode, callback):
        r"""注册"值被写"的回调。Tk 里签名是 `trace_add(mode, callback)`。

        【为什么实现一个"枚举面之外"的方法】机械枚举显示 `trace_add` 只出现在
        `file_transfer.py`（装配层）—— 它随换前端一起被重写，不是业务层需求。
        但"变量变了要推给前端"这件事在 Web 装配里一定会用到（推 delta 给
        HTML），所以这里预留一个**行为等价**的实现，省得装配层自己再包一层。
        仅支持 `mode="write"`（Tk 还有 read/unset，本项目一处没用 ⇒ 不实现，
        用了就 ValueError —— 响亮失败）。
        """
        if mode != "write":
            raise ValueError("VarStub.trace_add 只支持 mode='write'（实测项目只用这一种）")
        with self._lock:
            self._traces.append(callback)
        return callback


# =============================================================================
# ② WidgetStub —— 替代 Button / Listbox / Text / Label / ttk.Progressbar
# =============================================================================
class WidgetStub(object):
    r"""控件替身：一个通用壳，两种内部状态。

    【为什么不用分五个类】机械枚举出来的**方法面**很小（configure / 下标 /
    start / stop / insert / delete / see / get），而且互不冲突 —— 一个类就能
    全部承载。分成 ButtonStub/TextStub/… 只会让装配处多五个名字，却不多任何判据。

    【两个内部槽】
      · `_options`   —— 通用配置项。`configure(**kw)` 写进来，`widget[key]`
                        读出去。**这正是 Progressbar 的行为**（实测 C 段：
                        `configure(value=3)` 之后 `["value"]==3`），
                        也是按钮的行为（实测 D 段：`["state"]=="disabled"`）。
      · `_text`      —— Text 的字符缓冲。见模块 docstring A 段模型：
                        **内部 = 插入内容拼接 + 恒有一个终止换行**。

    【marquee】`start(ms)` / `stop()` 是 Progressbar 的跑马灯。在替身里它们
    只记一个布尔 —— 因为"转不转"是**前端的表现**，后端只需要知道"
    现在是不是未知总数模式"。业务层从不读它（枚举面里没有），前端才读。
    """

    __slots__ = ("_options", "_text", "_marquee", "_lock")

    def __init__(self, **options):
        self._options = dict(options)
        self._text = ""
        self._marquee = False
        self._lock = threading.Lock()

    # ---- 通用配置（Tk 的 configure / config / cget / 下标） ----
    def configure(self, **kwargs):
        with self._lock:
            self._options.update(kwargs)

    # Tk 里 `widget.config(...)` 是 `configure` 的别名，本项目未用但同名同义。
    config = configure

    def cget(self, key):
        with self._lock:
            if key not in self._options:
                raise KeyError("WidgetStub 没有配置项 %r" % (key,))
            return self._options[key]

    def __getitem__(self, key):
        return self.cget(key)

    def __setitem__(self, key, value):
        with self._lock:
            self._options[key] = value

    # ---- Progressbar 的跑马灯（枚举面：start / stop） ----
    def start(self, interval=None):
        with self._lock:
            self._marquee = True
            self._options["mode"] = "indeterminate"

    def stop(self):
        with self._lock:
            self._marquee = False

    # ---- Text 的字符缓冲（枚举面：insert / delete / see / get） ----
    def insert(self, index, chars):
        if index != "end":
            raise NotImplementedError("WidgetStub.insert 只支持 index='end'（实测项目只用这一种）")
        with self._lock:
            self._text += chars

    def delete(self, index1, index2=None):
        r"""删除 `[index1, index2)` 区间的字符。

        实测项目只出现两种调用，两种都实现：
          · `delete("1.0", "end")`      —— 清空（"end" = 终止换行之后）
          · `delete("1.0", f"{n}.0")`   —— 删掉前 n-1 行（日志区超行数时的截断）
        """
        if index2 is None:
            raise NotImplementedError("WidgetStub.delete 需要两个索引（Tk 的默认形态项目未用）")
        with self._lock:
            a = self._offset(index1)
            b = self._offset(index2)
            if b < a:
                a, b = b, a
            self._text = self._text[:a] + self._text[b:]

    def see(self, index):
        """把视图滚到某位置 —— **纯表现**，替身里只验证索引合法后丢弃。"""
        self._offset(index)

    def get(self, index1, index2=None):
        r"""取区间文本。只实现实测到的两种调用：

          · `get("1.0", "end-1c")` —— 导出日志用，取全部内容（**不含**末尾那个
                                      Tk 内部补的终止换行）。实测：空 Text 返回 `''`。
          · `get("1.0", "end")`    —— 取全部内容 + 终止换行。
        """
        with self._lock:
            if index2 == "end-1c":
                return self._text
            if index2 == "end":
                return self._text + "\n"
            if index2 is None:
                return self._text
            a = self._offset(index1)
            b = self._offset(index2)
            return self._text[a:b]

    # ---- 索引 → 字符偏移（Tk 的 "行.列" 记法） ----
    def _offset(self, index):
        """把 Tk 索引串换算成 `_text` 里的字符偏移。

        模型（见模块 docstring A 段）：内部文本 = `_text` + `"\\n"`。
          · `"end"`   = 终止换行之后 = `len(_text) + 1`
          · `"1.0"`   = 0
          · `"N.0"`   = 第 N 行的起始（空文本时 N=1 也在 0）
        """
        if index == "end":
            return len(self._text) + 1
        s = str(index)
        if "." not in s:
            raise NotImplementedError("WidgetStub 索引只支持 'end' 与 'N.M'：收到 %r" % (index,))
        line_s, col_s = s.split(".", 1)
        line = int(line_s)
        col = int(col_s)
        if line <= 1:
            return min(col, len(self._text))
        # 找第 (line-1) 个换行符之后的位置
        pos = 0
        remaining = line - 1
        while remaining > 0:
            nxt = self._text.find("\n", pos)
            if nxt < 0:
                return len(self._text)          # 超出末尾 → 停在末尾（Tk 会钳到末行）
            pos = nxt + 1
            remaining -= 1
        return min(pos + col, len(self._text))

    # ---- 只读内省（给探针/前端读，不是 Tk 接口） ----
    @property
    def text(self):
        """当前文本缓冲（= `get("1.0","end-1c")` 的结果）。"""
        with self._lock:
            return self._text

    @property
    def marquee(self):
        """跑马灯是否在转（`start()` 后 True，`stop()` 后 False）。"""
        with self._lock:
            return self._marquee


# =============================================================================
# ③ SchedulerStub —— 替代 Tk 的 `root.after` / `root.after_cancel`
# =============================================================================
class SchedulerStub(object):
    r"""到期队列（**不是**独立定时器线程）。设计理由见模块 docstring 末段。

    【为什么是"队列 + 主循环泵"】
      `ui_channel` 有 8 处 `assert current_thread() is main_thread()`。
      用 `threading.Timer` 会让回调跑在定时器线程 ⇒ 断言当场挂。
      所以这里只把回调**排期**，由宿主的**主线程主循环**调 `run_pending()` 执行。

    【线程安全】`after` 会被 worker 线程调用（`_post_ui` 的跨线程投递形态：
      `root.after(0, handler, *payload)`）⇒ 入队/撤单都在锁内。
      `run_pending()` 只由主线程调。

    【同刻顺序】Tk 保证同一毫秒注册的回调按注册顺序执行 ⇒ 这里用单调递增的
      `seq` 做堆的第二关键字，`after(0, a)` 与 `after(0, b)` 严格按 a、b 顺序。
    """

    __slots__ = ("_heap", "_seq", "_cancelled", "_lock", "_closed")

    def __init__(self):
        self._heap = []            # (due_seconds, seq, tid, fn, args)
        self._seq = itertools.count()
        self._cancelled = set()
        self._lock = threading.Lock()
        self._closed = False

    # ---- 机械枚举面：after / after_cancel ----
    def after(self, ms, fn, *args):
        r"""`ms` 毫秒后执行 `fn(*args)`，返回一个可传给 `after_cancel` 的句柄。

        返回的是**单调递增整数**（Tk 返回的是一串数字 id，形态等价）。
        """
        tid = next(self._seq)
        due = time.monotonic() + max(0, float(ms)) / 1000.0
        with self._lock:
            if self._closed:
                raise RuntimeError("SchedulerStub 已关闭")
            heapq.heappush(self._heap, (due, tid, tid, fn, args))
        return tid

    def after_cancel(self, tid):
        """撤销一个尚未执行的定时器。已执行/已撤销的 tid 是 **no-op**（与 Tk 一致）。"""
        with self._lock:
            self._cancelled.add(tid)

    # ---- 宿主主循环接口 ----
    def run_pending(self, max_callbacks=None, now=None):
        r"""**在主线程调用**：执行所有已到期的回调，返回执行条数。

        三个必须的保护，逐条对应它在替身"消息泵"里的角色：
          1. **每次取堆顶前重新取锁**：回调自己会再 `after(...)`（进度/速度两条
             链都这么接），也可能被别的线程插队 —— 所以不能在持锁状态下跑回调。
          2. **执行时不再持锁**：回调体里会调业务层，不能占着调度锁。
          3. **`max_callbacks` 上限**：防止"回调又注册 0ms 回调"的链把主循环
             饿死（与 `ui_channel._drain_ui_queue` 的批上限同一动机）。
        `now` 只为探针注入假时钟用；正常调用不传。
        """
        if threading.current_thread() is not threading.main_thread():
            raise AssertionError("SchedulerStub.run_pending 必须在主线程调用"
                                 "（ui_channel 的线程断言依赖这一条）")
        if now is None:
            now = time.monotonic()
        ran = 0
        while max_callbacks is None or ran < max_callbacks:
            with self._lock:
                while self._heap and (self._heap[0][2] in self._cancelled):
                    _, _, tid, _, _ = heapq.heappop(self._heap)
                    self._cancelled.discard(tid)
                if not self._heap or self._heap[0][0] > now:
                    break
                _, _, tid, fn, args = heapq.heappop(self._heap)
            fn(*args)
            ran += 1
        return ran

    def next_due_in(self):
        """距下一个到期回调还有多少秒；队列空返回 `None`（宿主主循环用来决定睡多久）。"""
        with self._lock:
            while self._heap and (self._heap[0][2] in self._cancelled):
                _, _, tid, _, _ = heapq.heappop(self._heap)
                self._cancelled.discard(tid)
            if not self._heap:
                return None
            return max(0.0, self._heap[0][0] - time.monotonic())

    def pending_count(self):
        """未到期且未撤销的回调数（探针断言用）。"""
        with self._lock:
            return sum(1 for e in self._heap if e[2] not in self._cancelled)

    def close(self):
        """关停：清空队列并让后续 `after` 抛错。"""
        with self._lock:
            self._closed = True
            self._heap = []
            self._cancelled = set()
