# -*- coding: utf-8 -*-
r"""传输内核（`TransferCore`）—— 从 `FileTransferApp` 里切出来的第 2 个真模块。

【它管什么】本次传输的**编排与状态**：逐源推进、判重跳过、原子落盘、覆盖审计、重传、
终态与退出原因、日期分层剪枝、状态库写线程（含 `_db_queue` / `_db_writer_loop`）。
它是**被宿主装配**的一方：宿主把锁/事件/队列/状态库/控件等**对象引用**注入进来
（**同对象，不是副本** —— 所以锁、事件、队列永远只有一份），并把 `hooks` / `launcher`
两个回调对象传进来，用它去够**上层**（这就是那 13 条反向边的解法）。

【为什么 `hooks` / `launcher` 是必填参数、没有默认值】
  回调没挂上＝静默失效。把它设成必填 ⇒ 漏挂一个在 **App 构造期**就 `TypeError`，
  而不是跑到某一步才悄悄少做一件事。见 `_checks\_nightly\60_step2_glue_equivalence.md`。

【为什么 `_ui` 与 4 个按钮不在构造参数里】
  它们在本类**构造的那一刻还不存在**（`_ui` 的 handlers 表与本类的按钮 `command=`
  都指向本类的方法 ⇒ 本类必须先存在）。这 5 个由宿主在 `_build_ui()` 之后
  `attach_host_widgets()` 补挂 —— 逐个断言、只许挂一次。

【谁在对齐】`_checks\_verify_split_ast.py --spec _split_ast_spec_step2.json`。
"""
import datetime
import errno
import os
import queue
import shutil
import sys
import threading
import time
import tkinter.messagebox as messagebox
from concurrent.futures import ThreadPoolExecutor, as_completed

import config
import state_db
class DiskFullError(Exception):
    """目标磁盘空间不足：立即中止整个传输任务，不再逐文件重试。"""


# 【P1】判重记录的**统一读取器**。`_should_skip` 的六条闸门只从它取字段。
#
# 【为什么必须有它】P1 之后，同一份记录有**两种承载形态**：
#   · 逐文件查库（`state_db.get_file_state`）回的是一个 dict（12 个键）；
#   · 内存预取（`state_db.load_skip_states`）回的是 tuple（只有闸门要的 6 个字段）。
# 让六条闸门各写两遍（一遍 `st.get("x")`、一遍 `st[config.SKIP_STATE_IDX["x"]]`）
# 是这批改动里最坏的一种写法：**两份实现必然漂移**，而漂移的方向是漏判一个字段
# ⇒ 该跳过的变成重传（安全方向），但用户会看到"每次都在重传"且没有判据会红。
# 所以"取字段"这件事收成这一个函数：调用方拿到的都是 `k -> 值`，
# **闸门代码一个字不改**（`_should_skip` 里只把 `st.get` 换成 `get`）。
#
# 【返回 None 表示"没有这条记录"】调用方必须先判 `st is None` —— 那条闸门
# （① 库里没有）不能省，它正是"首次传"的落点。
def _state_getter(st):
    if isinstance(st, tuple):
        idx = config.SKIP_STATE_IDX
        return lambda k: st[idx[k]]
    return st.get



class TransferCore(object):
    r"""传输内核。方法与字段的归属见 `_checks\_nightly\59_step2_reverse_edges.md`。
    
    【它不 import 任何上层模块】够上层只走 `self._hooks` / `self._launcher`；
    统计值由宿主注入只读视图。
    """

    # 【键的形状为什么带这么多段】见 `_sync_key` 的注释。
    _SYNC_KEY_PREFIX = "syncpoint|"

    # 【D-1 裁定③-方案C】跨源覆盖日志的**去洪**阈值。
    #   前 50 条逐条打；从第 51 条起只打累计，之后每 500 次再追一条。
    _OVERWRITE_LOG_HEAD = 50

    _OVERWRITE_LOG_EVERY = 500


    def __init__(self, *, hooks, launcher, live, headless_task, local_like_src, is_unc_path, db, lock, pause_event, stop_event, date_template, dest_path, dest_type, file_filter, flatten, ftp_timeout, reconnect_count, retry_count, source_path, source_type, thread_count, transfer_mode):
        """构造。**全部按关键字传**（22 个参数，位置传太容易错）。
        
        `hooks` / `launcher` 是**必填**且逐个断言 callable：漏挂一个立刻 `TypeError`，
        而不是跑到某一步才静默少做一件事。
        
        【形参名与属性名的关系】注入项里带前导下划线的，规律是「**形参去掉前导下划线、属性带**」：
        `db → self._db`、`lock → self._lock`、`headless_task → self._headless_task`。
        照这个规律写，`self.X` 的读点（全类 16 处读 `self._headless_task` 这一族）才对得上
        —— 第 2 步刚落地时这 5 个属性名漏了下划线（`self.headless_task`），
        是**运行期构造探针**抓出来的（静态 S4 自由名检查看不见属性名，只会看到"名字绑定了"）。
        
        【`live` 是什么 —— 缺陷形态⑤（构造快照不穿透）的修法】
        `ftp_dest_config` / `ftp_source_config` / `source_list` 这三个**宿主会在构造之后
        重新赋值**的值，不再按值收进来（按值 = 快照，宿主后写看不见），改收一个只回指
        宿主的**只读视图**（宿主侧 `_HostLiveView`，与 `_UiStatsView` 同模式）。
        分层不破：内核不 import 上层，只拿注入进来的对象；视图每次读都回宿主取当前值。
        为什么必须这样：按值收快照时，`ftp_dest_config` 恒 `None` ⇒ FTP 上传/下载全被
        「请先设置 FTP 目标」拒掉；`source_list` 恒空 ⇒ 多源串行驱动器静默失效。
        
        `_ui` 与 4 个按钮**不在这里**（构造时还不存在）⇒ 见 `attach_host_widgets`。
        """
        for _n in ("on_fatal", "on_task_end", "on_ux_changed",
                   "on_purge_state_changed", "on_path_problem"):
            if not callable(getattr(hooks, _n, None)):
                raise TypeError("TransferCore: hooks.%s 不是可调用的（漏挂？）" % _n)
        for _n in ("start_ftp_to_local", "start_local_to_ftp"):
            if not callable(getattr(launcher, _n, None)):
                raise TypeError("TransferCore: launcher.%s 不是可调用的（漏挂？）" % _n)
        self._hooks = hooks
        self._launcher = launcher
        self._headless_task = headless_task
        self._local_like_src = local_like_src
        self._is_unc_path = is_unc_path
        self._db = db
        self._lock = lock
        self._pause_event = pause_event
        self._stop_event = stop_event
        self.date_template = date_template
        self.dest_path = dest_path
        self.dest_type = dest_type
        self.file_filter = file_filter
        self.flatten = flatten
        # 【缺陷形态⑤】`ftp_dest_config` / `ftp_source_config` / `source_list` 三个值的
        #   **真身都在宿主上**，且宿主会在构造之后重新赋值 ⇒ 不按值收快照，改经 `live`
        #   视图每次现读（视图只回指宿主）。三个都收快照时实测的后果见类 docstring。
        self._live = live
        self.ftp_timeout = ftp_timeout
        self.reconnect_count = reconnect_count
        self.retry_count = retry_count
        self.source_path = source_path
        self.source_type = source_type
        self.thread_count = thread_count
        self.transfer_mode = transfer_mode

        # ---- 以下 26 条是 `FileTransferApp.__init__` 里**原样搬来**的字段初始化 ----
        self._date_pruner = None
        self._date_sync_key = None
        self._skip_states = None
        self._skip_states_n = 0
        self._src_queue = []            # 本次串行驱动器的待跑源（`_start_transfer` 时建立）
        self._src_index = 0             # 正在跑第几个（1-based 显示，0 表示还没开始）
        self._log_opened = False
        self._src_ran = 0
        self._src_totals = {"copied": 0, "skipped": 0, "failed": 0}
        self._overwrite_logged = 0
        self._completed = 0
        self._skipped = 0
        self._last_failed = []
        self._consec_fail = 0
        self._stop_reason = None              # 非 None 时覆盖终态的 stopped/finished
        self._task_active = False               # 终态处理幂等标志
        self._db_queue = None
        self._db_writer_thread = None
        self._current_task_id = None
        self._db_task_src_root = ""
        self._task_extensions = None          # 本次任务的过滤器，重传时要复用
        self._task_retry_attempts = config.DEFAULT_RETRY_COUNT + 1
        self._task_reconnect_attempts = config.DEFAULT_RECONNECT_COUNT + 1
        self._task_ftp_timeout = config.DEFAULT_TIMEOUT_S
        self._state_db_degraded = False       # 降级后不再尝试写库，且终态仍由 db-writer 投递
        self._db_title_changed = False        # 标题栏只改一次（Q11）


    def attach_host_widgets(self, ui, start_btn, pause_btn, stop_btn, retry_btn):
        """把构造时**还不存在**的 5 个宿主对象挂进来（各一次，逐个断言）。

        为什么不在 `__init__` 里收：`_ui`（它的 `handlers=` 指向本类的 5 个方法）与
        `_build_transfer_tab` 里那 4 个按钮的 `command=` 都指向本类 ⇒ 本类必须先存在；
        而这两样东西又都要在 `_build_ui()` 之内/之前就位。见 `file_transfer.py` 里
        构造点那段注释。**漏挂**：`TypeError`；**重复挂**：`RuntimeError`。
        """
        if getattr(self, "_ui", None) is not None:
            raise RuntimeError("attach_host_widgets 被调了第二次")
        for _n, _o in (("ui", ui), ("start_btn", start_btn), ("pause_btn", pause_btn),
                       ("stop_btn", stop_btn), ("retry_btn", retry_btn)):
            if _o is None:
                raise TypeError("attach_host_widgets: %s 是 None" % _n)
        self._ui = ui
        self.start_btn = start_btn
        self.pause_btn = pause_btn
        self.stop_btn = stop_btn
        self.retry_btn = retry_btn


    def _sync_key(self, segments=None):
        """同步点在 `meta` 表里的键。**空模板返回 None**（没有可读写的同步点）。

        形状取自 `45_plan_incremental.md` §A.4：
            `syncpoint|<src_type>|<src_root>|<dst_root>|<模板>`

        四段都带的理由，逐条都是"少了它会怎样"：
          · `src_root` —— 少了它，两个不同来源会互相顶掉对方的同步点。症状有两种，
            一种只是慢（某个源莫名其妙全量重扫），另一种**是漏传**：另一个源的
            同步点被顶到很新，它下次就从那儿往后扫，中间那批再也不看。
          · `src_type` —— 同一个路径串在本地与 FTP 下含义不同（`ftp://h/d` 是 URL）。
          · `dst_root` —— 同一批源文件传到两个地方，进度是两回事。
          · `模板` —— 用户把 `YYYY/MM` 改成 `YYYY/MM/DD` 时层数就变了，旧同步点
            在新模板下**层数对不上**（`parse_syncpoint` 会拒）。带上模板就是让
            "换了模板"天然从一个干净的同步点开始，不必去删旧的、也不会把旧的
            硬套到新模板上（那才是会漏传的那条路）。

        非法模板返回 None：调用方一律当归零处理（全扫）。
        """
        if segments is None:
            segments, err = config.parse_date_template(self.date_template.get())
            if err is not None:
                return None
        if not segments:
            return None
        return "%s%s|%s|%s|%s" % (
            self._SYNC_KEY_PREFIX,
            self.source_type.get(), self.source_path.get().strip(),
            self.dest_path.get().strip(), config.format_date_template(segments))


    def _read_syncpoint(self, key, segments):
        """读回并校验同步点。返回 `(层元组 或 None, 不可用的原因 或 None)`。

        **任何一处不对劲都返回 None**（= 全扫）—— 规则 2：缺失一律退回最安全的动作。
        三种来路分开说，因为用户的**动作**完全不同：
          · 键为 None / 库不可用 → 原因给一句，让用户知道"这次没在用增量"；
          · 值被手改坏了 / 是旧模板写的 → `parse_syncpoint` 给出具体哪一层不像。
        注意 `get_meta` 自己已经吞掉 `sqlite3.Error` 并返回 None（首次运行时
        `syncpoint|...` 本来就不存在，那不是错误），所以这里不再包一层。
        """
        if not key:
            return None, "没有可用的同步点键"
        if not config._STATE_DB_ENABLED:
            return None, "状态库功能已关闭（config._STATE_DB_ENABLED）"
        if self._state_db_degraded:
            return None, "状态库已降级"
        try:
            raw = self._db.get_meta(key)
        except Exception as e:
            return None, "读同步点出错：%s: %s" % (type(e).__name__, e)
        if raw is None:
            return None, None                 # 就是没有 —— 第一次跑，正常
        return config.parse_syncpoint(raw, segments)


    def _date_layer_begin(self):
        """在**主线程**建本次源的剪枝器，写进 `self._date_pruner`。返回它。

        必须在主线程：它要读 `self.date_template`（Tk 变量）。与 `_task_ftp_timeout`
        那一批快照同一条规矩 —— Tk 变量只在主线程读，后台线程读主线程取好的快照。

        空模板 / 模板非法 ⇒ `self._date_pruner` 保持 None ⇒ 四处 `os.walk` 与
        FTP 遍历**逐字节不变**，与本次改动之前完全一致。这是"默认关闭"的落点。
        """
        self._date_pruner = None
        self._date_sync_key = None
        segments, err = config.parse_date_template(self.date_template.get())
        if err is not None:
            # 不合法就不启用 —— **不抛、不阻止任务**。规则 2：宁可全扫。
            # 界面上填的是坏的，用户得知道，所以说一行。
            self._ui._update_log("[日期分层] 模板不合法，本次按全扫跑：%s" % err)
            return None
        if not segments:
            return None
        tpl_text = config.format_date_template(segments)
        key = self._sync_key(segments)
        pts, why = self._read_syncpoint(key, segments)
        if pts is None:
            self._ui._update_log("[日期分层] 启用（模板 %s），本次全扫%s"
                             % (tpl_text, ("：" + why) if why else ""))
        else:
            self._ui._update_log("[日期分层] 启用（模板 %s），同步点 %s"
                             % (tpl_text, "/".join(pts)))
        self._date_sync_key = key
        self._date_pruner = config.DateLayerPruner(segments, pts)
        return self._date_pruner


    def _date_prune_walk(self, pruner, src_root, dirpath, dirs):
        r"""四处 `os.walk` 共用的剪枝点：判 + 记"这个目录真的进去过了"。

        **`pruner is None` 时一行都不做** —— 这就是"没填模板 = 与改动前逐字节相同"
        在四个调用点的落点。四处共用这一份，是为了让"`dirs[:]` 才是原地改"这条
        唯一容易写错的事只有一处实现（写在 `DateLayerPruner.filter_walk_dirs` 里，
        那里有长注释；验收 `_verify_date_layer.py` 的 F 组专门钉它）。

        两个动作的**顺序不能反**：先把 `dirs` 里该剪的剪掉，再记"本目录进去过了"。
        反过来的话，被剪掉的子目录会不会被记进 `max_seen` 就取决于实现细节了。
        """
        if pruner is None:
            return
        pruner.filter_walk_dirs(src_root, dirpath, dirs)
        pruner.note_visit_local(src_root, dirpath)


    def _skip_states_begin(self):
        r"""【P1】本次源起跑前，把该 `src_root` 的判重记录**一次性读进内存**。

        返回 dict 或 None。**任何一步不顺都退回 None** —— 退回之后 `_should_skip`
        逐文件查库，行为与 P1 之前逐字节相同。这是本功能唯一的安全出口，所以它
        挂在最外层，而不是散在里面的 try 里。

        【为什么在这里建】与 `_date_layer_begin` 同一个位置、同一条理由：必须在
        **主线程**（要读 `self._db_task_src_root`，而它由主线程 `_claim_current_task`
        落的）、必须**在派发之前**（三个 `_start_*` 一起线程就有 worker 去判重了）。
        晚一行 = 头几个文件走逐文件查库，跑得对但白付一次连接开销。

        【为什么按 `src_root` 取、而不是整张表】20 万行是"一个源"的量级；
        整张表在多源场景下会被放大到"源数 × 20 万"。实测多源形态（库 20 万行 /
        8 个源）按源取只留 2.5 万行 ⇒ 内存按 1/8 摊。
        ⚠️ 代价如实记：`files` 表**没有 `src_root` 索引**，
        `SELECT ... WHERE src_root=?` 是全表扫（SQLite 自己说 `SCAN files`）——
        只影响建表墙钟（实测 200 万行量级仍在毫秒级），不影响内存。
        要不要为它加索引是**另一件事**：那动 schema，本次不做。

        【为什么"读不到"要出声】预取失败时用户看到的是"变慢了"，而不是"坏了" ——
        没有这一行，将来有人拿着日志问"为什么这次没走预取"时，翻遍日志都找不到答案。
        """
        self._skip_states = None
        self._skip_states_n = 0
        if not config._STATE_DB_ENABLED or self._state_db_degraded:
            return None
        root = self._db_task_src_root
        if not root:
            return None
        try:
            d = self._db.load_skip_states(root)
        except Exception as e:
            # 【方向】失败 = 退回逐文件查库 = 与 P1 之前完全一样。
            #   **绝不允许**"预取失败 ⇒ 当成本源没有记录"（那样会变成全量重传）。
            self._ui._update_log("[预取] 读判重记录失败，本次退回逐文件查库（不影响传输）：%s: %s"
                             % (type(e).__name__, e))
            return None
        self._skip_states = d
        self._skip_states_n = len(d)
        self._ui._update_log("[预取] 读入 %d 条判重记录（源 %s），本次逐文件查内存"
                         % (len(d), root))
        return d


    def _skip_states_release(self):
        r"""【P1】一趟源跑完就**释放**（与 `_date_layer_finish` 同一道终态闸）。

        【为什么必须释放，而不是留着给下一次用】
          20 万行 ≈ 85 MB 常驻。多源串行时留着不释放，内存会按源数**线性叠加**
          —— 而门槛（200 MB）的实测余量只有 13.5 MB。这一条是 P1 落地时的**硬约束**，
          `_checks\_verify_p1_prefetch.py` 的 D 组专门钉它。
        【为什么不"下次覆盖就好了"】覆盖发生在**下一个源建 dict 的时候**，而两个
          源之间存在一段"两个 dict 同时活着"的窗口（新 dict 正在长、旧的还没被覆盖完）。
          显式释放把那个窗口关掉。
        """
        self._skip_states = None
        self._skip_states_n = 0


    def _date_layer_finish(self, normal_finish):
        """一趟源跑完的收尾：**出声**（永远）+ **推进同步点**（只在正常跑完时）。

        【为什么"推进"必须挂在终态上，而不是 `os.walk` 一跑完就推】
          遍历跑完 ≠ 这趟成功了：磁盘满、连续失败中止、用户点停止，都可能发生在
          遍历结束之后（还在读、还在传）。拿一次**没跑成**的扫描去推进同步点，
          下次就那批**没传成功**的日期之后就再也不看了 —— 而且界面上全绿。
          这是本功能唯一会造成**漏传**的路径，所以闸门设在这里，判据与
          `_dispatch_task_end` 里"多源串行要不要接着跑"用的是**同一条**。
        """
        pruner = self._date_pruner
        # 先摘掉再干活：一条迟到的重复终态（`_dispatch_ui_message` 的兜底路径）
        # 走到这里时 `self._date_pruner` 已经是 None，不会再推一次。
        self._date_pruner = None
        if pruner is None:
            return
        # 【剪了 0 个也说话】—— "开了但一个都没剪"本身就是要看见的事实
        # （模板写错了、同步点还是空的、树本来就不分层，都长这样）。
        # 但"一个目录都没判过"（进目录就失败了）不值得说，那行日志另有其人来打。
        if pruner.n_pruned or pruner.n_kept or pruner.warnings:
            for line in pruner.summary().split("\n"):
                self._ui._update_log(line)
        if not normal_finish:
            return
        if pruner.max_seen is None:
            # 这趟一个日期层目录都没进去过（源根底下就不是日期层）。
            # **不推进**：没有依据。硬推成"今天"会把整棵树跳过。
            return
        key = self._date_sync_key
        if not key:
            return
        if not config._STATE_DB_ENABLED or self._state_db_degraded:
            # 库不可用 ⇒ 本来就读不到同步点 ⇒ 本次是全扫。这一行是说给
            # "以为自己在用增量"的用户听的：不写出来，他会以为已经在增量了。
            self._ui._update_log("[日期分层] 状态库不可用，本次没能记下同步点 ⇒ 下次仍是全扫")
            return
        text = "/".join(pruner.max_seen)
        try:
            # `set_meta` 自己吞异常并返回 True/False（为一次记录失败把已经跑完的
            # 传输报成"失败"是把账算错了地方），所以这里只需要转述结果。
            ok = self._db.set_meta(key, text)
        except Exception as e:
            ok = False
            self._ui._update_log("[日期分层] 同步点写入失败（不影响本次传输）：%s: %s"
                             % (type(e).__name__, e))
        if ok:
            self._ui._update_log("[日期分层] 同步点推进到 %s" % text)
        self._refresh_syncpoint_label()


    def _refresh_syncpoint_label(self):
        """把"这一对源/目标 + 当前模板"的同步点写到标签上。**只读，绝不写库。**

        【三种显示各自的意思，必须分开说】
          · 没填模板   → 未启用            —— 功能关着，每次全扫；
          · 填了但查不到 → 无 ⇒ 下次全扫     —— 第一次跑 / 刚重置过；
          · 查到了     → 2026/07/31        —— 下次只扫它**之后**的部分。
        把前两种合并成一句话，第一次用的人会以为没设上。

        【查不到为什么写"无"而不是猜一个】同步点唯一的合法来源是"上一次真的
        跑完过"。推断出来的（比如取今天）会让某一批日期被整棵剪掉，而界面上全绿。
        """
        label = getattr(self, "sync_label", None)
        if label is None:
            return                      # 还没建到那一步（或测试里造的对象）
        segments, err = config.parse_date_template(self.date_template.get())
        if err is not None:
            label.configure(text="⚠️ 模板不合法（功能不会启用）：%s" % err)
            return
        if not segments:
            label.configure(text="同步点：（未启用 —— 每次全扫，一个目录都不剪）")
            return
        key = self._sync_key(segments)
        pts, why = self._read_syncpoint(key, segments)
        if pts is not None:
            label.configure(text="同步点：%s ⇒ 下次只扫它**之后**的日期目录"
                                 % "/".join(pts))
        elif why:
            label.configure(text="同步点：（不可用 ⇒ 下次全扫）\n    %s" % why)
        else:
            label.configure(text="同步点：（无 ⇒ 下次全扫）")


    def _reset_syncpoint(self):
        """「重置同步点」：删掉**上面那一行显示的那一个**键 ⇒ 下次全扫。

        【作用域为什么是"当前这一个"而不是全部】
          标签上明明白白写着是哪一对源/目标，按钮就该只动那一个 —— 删掉别的源的
          同步点，用户在这块界面上**看不出**发生了什么（症状是那些源下次全量重扫，
          只是慢）。要"全清"用「设置」页的清空状态库那条路更醒目。
        【为什么不用确认框】它让下次**多扫**、不会让任何文件不被传 ——
          失效方向是安全的那一侧，不值得为它拦一道。改成 info 说清后果即可。
        """
        segments, err = config.parse_date_template(self.date_template.get())
        if err is not None:
            messagebox.showwarning("重置同步点",
                                   "日期模板不合法，先改好模板再重置：\n\n%s" % err)
            return
        if not segments:
            messagebox.showinfo("重置同步点",
                                "还没填日期模板（日期分层未启用），没有同步点可重置。")
            return
        key = self._sync_key(segments)
        if key is None:
            return
        if not config._STATE_DB_ENABLED or self._state_db_degraded:
            messagebox.showinfo("重置同步点",
                                "状态库当前不可用，同步点本来就不存在 ——\n"
                                "下次扫描本来就是全扫，不用重置。")
            return
        try:
            self._db.del_meta(key)
        except Exception as e:
            messagebox.showerror("重置同步点",
                                 "删除同步点失败：%s: %s" % (type(e).__name__, e))
            return
        self._refresh_syncpoint_label()
        messagebox.showinfo("重置同步点",
                            "已重置。\n\n下次扫描会从源根**全量重扫一遍**"
                            "（慢一些，但不会漏）。\n第一次跑完之后会重新记下同步点。")


    def _probe_local_path(self, path, is_source):
        """检查本地源/目标路径是否可用 —— **纯检查**（无界面副作用）。
        可用返回 `True`；不可用返回**要显示的文案**（`str`），
        由 `_check_local_path` 交给 `hooks.on_path_problem(text)`。
        区分「路径真不存在」与「磁盘访问出错」（如移动盘 I/O 错误），并对瞬时闪断重试。"""
        label = "源路径" if is_source else "目标文件夹"
        # 【阶段三】UNC 的"不存在"与本地目录的"不存在"**不是一回事**：
        #   本地那个是"路径真没了"，UNC 这个九成是"这次登录会话还没连上这台共享"
        #   （最常见的三种：资源管理器里没连过、口令变了、VPN 断了）。
        #   所以这一支的报错要多给一句**能直接照做**的话，否则用户看到
        #   "源路径不存在：\\srv\share" 只会去想"路径我明明打对了"。
        #   ⚠️ 只加"怎么办"，不改判据本身 —— 判据仍是 `os.stat` 抛什么。
        unc = self._is_unc_path(path)
        what = "共享文件夹" if unc else label
        #   ⚠️ 文案里**不能用 Markdown 的 `**`**：messagebox 不渲染它，会原样显示。
        tip = ("\n\n这看起来是一个共享文件夹。如果这个路径确实存在，\n"
               "多半是这台机器当前还没连上它 —— 请先在「资源管理器」里\n"
               "打开一次该共享（口令由 Windows 记住，本工具不代填也不代存）。"
               ) if unc else ""
        for attempt in range(3):
            try:
                os.stat(path)
                return True
            except FileNotFoundError:
                break   # 路径真不存在，重试无意义
            except OSError as e:
                if attempt < 2:
                    time.sleep(0.5 * (attempt + 1))   # 移动盘闪断时给盘一点恢复时间
                    continue
                return (f"无法访问{what}：{path}\n\n原因：{e}\n\n（已重试3次仍失败，通常是源盘 I/O 错误或已断开，"
                    f"请检查盘是否插好/是否在报硬件错误）{tip}")
            except Exception as e:
                return (f"无法访问{what}：{path}\n原因：{e}{tip}")
        return (f"{what}不存在：{path}{tip}")


    def _check_local_path(self, path, is_source):
        """路径检查的**界面出口**：判据在 `_probe_local_path`，这里只管"要不要弹框"。

        【为什么拆两半（§59 C1/C2）】判据与三段文案下沉进内核（纯检查、无界面副作用），
        弹框走 `hooks.on_path_problem(text)` —— 于是内核不必知道"错误是怎么显示的"
        （宿主用 `messagebox.showerror("错误", text)`，**标题与正文一字不差**）。
        **返回语义不变**：`True` = 继续开跑（调用点拿它决定 `return False` 与否）。
        """
        text = self._probe_local_path(path, is_source)
        if text is True:
            return True
        self._hooks.on_path_problem(text)
        return False


    # 【D-1 重构】`_start_transfer` 原来的整块函数体**一行没删、一行没改**，
    # 整体搬进了 `_launch_source` —— 因为多源串行要对**每个源**各做一遍，
    # 而它们本来就都是逐源成立的（校验、抢占、起跑都是"当前源"的事）。
    #
    # 【为什么拆成三个函数而不是在一个里面加分支】
    #   ① `_start_transfer`：定下"这次跑哪些源"。它现在是按钮的回调，只做这一件事。
    #   ② `_start_next_source`：推进到下一个源。**只负责"下一个是谁"**，
    #      不负责它能不能跑（那是 ③ 的事）—— 于是"跳过坏源"和"校验失败整体放弃"
    #      两条出口在代码里是分开的两行，读的人不会把它们看成一回事。
    #   ③ `_launch_source`：就当前 `source_path` 起跑（原函数体）。
    def _start_transfer(self):
        # 【D-1】≥2 项才走串行驱动器；≤1 项时 `_src_queue` 留空，源就是 `source_path`，
        # 与 D-1 之前**逐字相同** —— 这是回归能否绿的硬条件（10_prep_D1 §7.7）：
        # 9 个测试文件直接 `app.source_path.set(...)`，它们永远不知道有这张列表。
        # 这不是为测试开的后门：手工填过 `source_path` 的旧配置走的是同一条路，
        # **单源是多源的真子集**。
        # 【缺陷形态⑤】源列表同样经 `live` 视图现读 —— 宿主在 `_apply_ux_state_dict`
        #   里会**重新绑定** `self.source_list`（恢复上次状态 / 套用任务参数），
        #   按值收的那份会一直是构造时的空列表 ⇒ 多源串行驱动器静默失效。
        _live_sources = self._live.source_list
        self._src_queue = list(_live_sources) if len(_live_sources) >= 2 else []
        self._src_index = 0
        # 【本轮修复】本轮日志的"开过没有"旗标。**必须在这里归零**：
        #   `_begin_run_log` 是幂等的，而这个归零点就是"新一轮"的唯一定义处。
        self._log_opened = False
        self._src_ran = 0
        self._src_totals = {"copied": 0, "skipped": 0, "failed": 0}
        # 【D-1 裁定③-方案C】覆盖日志的去洪计数**按整条序列**归零 —— 日志跨源是
        #   同一份（只有第一枪清），所以"前 50 条"也是整条序列共用的 50 条。
        #   放进 `_launch_source` 会让每个源各打 50 条，"前 50 条"就名不副实了。
        self._overwrite_logged = 0
        # ⚠️ "启动 N 个源传输任务" **不能在这里打** —— 本轮第一枪起跑前会
        #   **清日志**（单源时代的既有行为），这一行会被当场擦掉。
        #   所以它跟着清日志一起、挪进了 `_begin_run_log()`。第一版就栽在这里：
        #   4 条验收用例同时红，全是因为日志里根本没有这一行。
        self._start_next_source()


    def _begin_run_log(self):
        r"""本轮**第一次**要往日志里写东西之前调用：清空日志区 + 打「启动 N 个源传输任务」。

        【幂等】整条源序列只开一次，靠 `self._log_opened` 这个**一次性旗标**。

        【为什么不是一个位置判断 —— 这正是本轮修掉的缺陷】
          这两件事原来挂在 `_launch_source` 里，判据是 `if self._src_index <= 1:`。
          而 `_src_index` 是在 `_start_next_source` 的 while 循环里**先自增**的：
          "第 1 个源被预检跳过"时 `continue`，走到 `_launch_source` 的已经是
          **第 2 个源**（`_src_index == 2`）—— 条件**永不成立**。后果有两条，
          而且两条都是**静默**的：

            1. 这一轮日志**从头到尾没被清过** —— 上一轮的
               `[源 1/2] 完成：复制 N 个…` 直接挂在新一轮日志的开头，
               读起来像是"这一轮先跑了一个源"。实测证据见
               `_checks\_nightly\23_step_stage3_share.md` §5.1 贴的 C 组日志。
            2. 本轮自己的 `启动 N 个源传输任务` **一次都没打过**（它跟清日志同生共死）。

          根因不是条件写错了，是**判据用错了**：
          "是不是本轮第一枪"是一个**与位置无关**的事实，而 `_src_index` 是一个
          **会被跳过动作推进的循环变量**。拿一个会被 `continue` 推走的量去表达
          "第一次"，只要中间有任何一条路径跳过它，判断就塌了。
          所以改成显式旗标 —— 它不会被任何 `continue` 推走。

        【为什么「启动 N 个源传输任务」必须跟进来】
          它必须落在清日志**之后**（否则当场被上面三行擦掉），所以两件事只能同进同出。
          跟着旗标走之后，"被跳过的源"那几行会排在它**后面** —— 这才是本轮的真实经过。

        【调用点只有两处，且都在"本轮确实要产出日志"的出口上】
          * `_start_next_source` 的**跳过分支**（写 `不可用，跳过` 那一行之前）；
          * `_launch_source` 的起跑处（校验过完、任务锁拿到之后）。
          这样"校验没过 → 日志不动"这条既有行为**一个字没变**：
          单源模式下唯一会开日志的点仍然是 `_launch_source` 那一处。
          「所有源都被跳过」不需要额外的调用点 —— 每跳一个源都会经过上面第一处。
        """
        if self._log_opened:
            return
        self._log_opened = True
        self._ui._reset_log_area()      # 【第 1 步】日志区清空 + 两个计数归位（逐字搬）
        # 【D-1 裁定①】条件用 `>= 2`：单源模式一个字都不多打。
        if len(self._src_queue) >= 2:
            self._ui._update_log("启动 %d 个源传输任务" % len(self._src_queue))


    def _start_next_source(self):
        """推进到下一个源并起跑；没有可跑的就收尾。

        【为什么用 while 而不是递归】源不可用时（盘拔了、目录被删）要**跳过它看下一个**
        —— 每个坏源压一层栈，30 个源就是 30 层；而且递归会把"跳过"和"起跑"两种出口
        混在同一条调用链里，读的人分不清哪一层是真的起跑了。
        """
        while True:
            self._src_index += 1
            total = len(self._src_queue)
            if not self._src_queue:
                break                     # 单源模式：源就是 source_path，不推进也不改写
            if self._src_index > total:
                # 走到这里 = 队列**跑完了**，而且最后一个真跑的源**拿不到 `is_last`**
                #   （它后面还排着源，`_dispatch_task_end` 里 `is_last = _src_index >= total`
                #   算出来是 False）—— 于是 `_finish_one_source` 那行
                #   `全部完成：N 个源，共复制 X 个…` **一次都没打**。
                #   【本轮修复·第二处】那行汇总补在这里打，按 `_src_ran` 分成两种收尾。
                #
                # 判据用 `_src_ran`（真起跑过几个）而**不是** `_src_totals` 是不是全 0：
                #   一个"跑完了但一个文件都没传"的源同样会把 `_src_totals` 留成全 0，
                #   那时打「没有可执行的来源」就是与事实相反 —— 它明明跑过。
                if self._src_ran:
                    t = self._src_totals
                    self._ui._update_log("全部完成：%d 个源，共复制 %d 个，跳过 %d 个，失败 %d 个"
                                     % (total, t["copied"], t["skipped"], t["failed"]))
                else:
                    # 剩下的源全被跳过了（都在 "_start_next_source" 上面那道预检里），
                    # 而且**一个都没起跑过** —— 这句才是实话。
                    self._ui._update_log("[任务] 没有可执行的来源（其余来源都不可用）")
                self._src_queue = []
                self._src_index = 0
                # 【阶段四】无头模式：走到这里 = **一个源都没跑起来**，
                #   而这条路径**不经过终态网关**（没有任务、没有 db-writer、没有
                #   `_dispatch_task_end`），所以没有任何人会去结束主循环 ——
                #   无头进程会永远挂在 `mainloop()` 里。必须在这里自己收口。
                #
                #   退出码给 4（起不来）而不是别的：这个分支的两种走法
                #   （源全不可用 / `_launch_source` 校验没过）都是"配置或路径错"，
                #   正是 4 的定义。注意它**不是** 3（完全失败）—— 3 的前提是
                #   "跑起来了但一个都没成功"，而这里根本没跑。
                if self._headless_task is not None:
                    self._hooks.on_fatal(
                        config.SCHEDULE_EXIT_CANNOT_START,
                        "[无头] 没有可执行的来源（%d 个源全部不可用或校验没过），任务未起跑。"
                        % total)
                return
            cur = self._src_queue[self._src_index - 1]
            self.source_path.set(cur)     # ★ 核心：把当前源塞回原变量
            # 【D-1】每一项**自带类型**（§7.4）：按前缀判定，写回 `source_type`。
            # 不这么做的话，"两类混排"只是句空话 —— 下面那三个 `_start_*` 是按
            # `source_type` 分派的，一个 ftp:// 项会被当成本地路径去 os.walk。
            # 这里改的是**同一个菜单的值**（不是加控件、不是挪位置），
            # 语义上也更对：菜单显示的就是"当前这个源是什么类型"。
            # 【阶段三】多认一种：UNC 前缀 → `"share"`。**这一层与上面的 `ftp://`
            #   完全同构**，都是"按路径的前缀推出它是什么类型"。
            #   好处是"共享"这个类型**不依赖用户选得对不对** —— 用户把 UNC 加进来时
            #   菜单选的是哪一项其实无所谓，路径自己会说话。
            self.source_type.set(
                "ftp" if cur.startswith("ftp://")
                else ("share" if self._is_unc_path(cur) else "local"))
            # 【D-1 裁定①】"每个源开始：不记" —— 原来这里有一行 `[任务 X/N] 源：…`，
            #   已删。理由（用户裁定）：序号前缀 `[源 X/N]` 本身就能推出当前是第几个，
            #   再单独报一行等于把同一件事说两遍。**唯一保留的是"跳过"那一行**
            #   （它是异常，不是常规），见下。
            # 源不存在 → **不弹框**，记一行跳下一个。
            # 弹框会把串行驱动器整个卡住（模态框不点就不返回），而"第 3 个源的移动盘
            # 没插"不该拦下第 4、5 个源 —— 那是体验问题，不是正确性问题。
            # 预检只做本地源的 `exists`（够用了）：它盖住的是"盘没插/目录被删"这一类，
            # 而真正的 I/O 故障留给 `_check_local_path` 去报（那种情况本来就该停下来看）。
            # 【阶段三】共享**照做这道预检**（`_LOCAL_LIKE_SRC`）。
            #   ⚠️ 这里曾经有一条相反的设计：让共享跳过预检，理由是"打不通的 UNC
            #   会卡到 SMB 超时、把驱动器卡死"。**实测把它推翻了**
            #   （`_probe_unc_stat_first.py`）：代价是**有界的 21 秒、且一次性的**
            #   —— Windows 按**主机**记负缓存，同一台主机第二次开始全是 0.00s；
            #   而换来的是**静默**：不预检 ⇒ 传输照跑 ⇒ `os.walk` 扫出 0 个文件 ⇒
            #   报「全部完成：复制 0 个」。用一个 21 秒的停顿换一个假的成功，不划算。
            #   完整数据见 `_checks\_nightly\21_plan_stage3.md` §2.2b。
            if self.source_type.get() in self._local_like_src and not os.path.exists(cur):
                # 【本轮修复】"这一轮要产出日志了"的第一处出口。必须排在下面那行**之前**：
                #   否则"跳过"会写进上一轮的日志里 —— 那正是这次要修的形态。
                self._begin_run_log()
                self._ui._update_log("[源 %d/%d] 不可用，跳过：%s%s"
                                 % (self._src_index, total, cur,
                                    "（共享：请先在资源管理器里连一次它）"
                                    if self._is_unc_path(cur) else ""))
                continue
            break
        if not self._launch_source():
            # 本源的校验没过 → **整条序列就此放弃**（不是跳到下一个）。
            # 校验报的是"目标没了 / 目标在源内部 / 抢不到任务锁 / FTP 没配"，
            # 这些没有一条是"只跟本源有关"的，接着跑下一个只会把同一个错再说一遍。
            self._src_queue = []
            self._src_index = 0
            # 【阶段四】无头模式：同上面那个分支 —— 这里也没有任何人会去收口。
            #   ⚠️ 这一处**可能已经被收过口了**：抢不到任务锁时
            #   `_claim_current_task` 自己就 `_headless_bail(2)` 过了，而
            #   `root.quit()` **不会中断当前的调用栈**（它只是让 `mainloop` 返回），
            #   所以代码会继续跑到这一行。`_headless_bail` 自己是幂等的
            #   （第一次收口说了算），这里直接调即可 —— 2 不会被这里的 4 盖掉。
            if self._headless_task is not None:
                self._hooks.on_fatal(
                    config.SCHEDULE_EXIT_CANNOT_START,
                    "[无头] 源的校验没过（源：%s），任务未起跑。" % self.source_path.get().strip())


    # 【阶段2a-修改原因】三处改动：
    #   1) FTP→FTP 的"暂不支持"守卫提前到抢占之前（同一条提示，行为等价，
    #      避免先 claim 再发现不支持而留下一行无人收尾的 running 任务）；
    #   2) 参数校验通过后立即 claim_task 抢占（Q6 任务级锁），抢不到就整体返回、
    #      不动任何按钮状态；
    #   3) claim 成功后启动本任务的 db-writer，此后所有终态都由它投递。
    # 【阶段1-修改原因】清日志后行数缓存归 1（空 Text 也占 1 行）；
    # 置 _task_active=True 开启本次任务的终态幂等窗口。
    # 【D-1】返回值：True = 真的起跑了；False = 校验没过，源序列该放弃。
    def _launch_source(self):
        src_type = self.source_type.get()
        dst_type = self.dest_type.get()

        if src_type in self._local_like_src and not self.source_path.get().strip():
            messagebox.showwarning("警告", "请填写源路径")
            return False
        if dst_type == "local" and not self.dest_path.get().strip():
            messagebox.showwarning("警告", "请填写目标路径")
            return False
        # 【缺陷形态⑤】这里读的是 `live` 视图（宿主**当前**值），不是构造时的快照 ——
        #   否则 UI / 恢复状态里配好的 FTP 配置永远看不见，下面两道校验恒真。
        if src_type == "ftp" and not self._live.ftp_src:
            messagebox.showwarning("警告", "请先设置 FTP 源")
            return False
        if dst_type == "ftp" and not self._live.ftp_dest:
            messagebox.showwarning("警告", "请先设置 FTP 目标")
            return False
        if src_type == "ftp" and dst_type == "ftp":
            messagebox.showwarning("暂不支持", "FTP 到 FTP 的传输暂未实现，请先下载到本地再上传。")
            return False

        if src_type in self._local_like_src:
            src = self.source_path.get().strip()
            if not self._check_local_path(src, is_source=True):
                return False
        if dst_type == "local":
            dst = self.dest_path.get().strip()
            if not self._check_local_path(dst, is_source=False):
                return False

        # 防护：目标位于源内部或与源相同 → 无限复制/自覆盖
        # 【阶段三】共享也要过这一条：`\\srv\a` → `\\srv\a\sub` 是**同一台共享**
        #   上的自嵌套，`commonpath` 对 UNC 一样算得对（它只做前缀比较）。
        if src_type in self._local_like_src and dst_type == "local":
            try:
                if os.path.commonpath([os.path.abspath(src), os.path.abspath(dst)]) == os.path.abspath(src):
                    messagebox.showerror("错误", "目标文件夹不能位于源文件夹内部或与之相同，否则会无限复制。")
                    return False
            except ValueError:
                pass   # 不同盘符时 commonpath 抛 ValueError，视为安全

        raw_filter = self.file_filter.get().strip()
        extensions = None
        if raw_filter:
            extensions = set()
            for ext in raw_filter.split(","):
                ext = ext.strip().lower()
                if not ext:
                    continue
                if not ext.startswith("."):
                    ext = "." + ext   # 自动补点，如输入 "dcm" 也按 ".dcm" 处理
                extensions.add(ext)

        # ---- 任务级锁：抢占成功之前不动任何 UI（Q6） ----
        task_id = self._claim_current_task(extensions)
        if task_id is None:
            return False
        self._current_task_id = task_id
        self._task_extensions = extensions
        # 【阶段二-新增】三个运行参数的主线程快照。**必须在这里、也就是抢到任务锁之后、
        # 起任何 worker 之前**取 —— 理由与 `_task_extensions` 逐字相同：
        #   * worker 线程不许读 Tk 变量（本项目最硬的一条线程规矩），
        #     而 `self.ftp_timeout` 那几个都是 Tk 变量；
        #   * 快照取在**开跑前**，于是"传输途中去设置页改超时"不会影响这次任务 ——
        #     它只影响下一次。这正是快照机制想要的性质：一次任务用一套参数跑到底，
        #     中途改参数不会让前半段和后半段用不同的超时。
        #   * `+1` 是把界面上的"重试次数 N"换算成代码里的"尝试次数 N+1"，
        #     换算**只在这一处发生** —— 散在多处的话迟早有一处忘了加或加了两次。
        self._task_retry_attempts = int(self.retry_count.get()) + 1
        self._task_reconnect_attempts = int(self.reconnect_count.get()) + 1
        self._task_ftp_timeout = int(self.ftp_timeout.get())
        self._hooks.on_purge_state_changed()
        self._start_db_writer(task_id, self._db_task_src_root)

        # 【D-1】清日志**只在本条序列的第一枪**做。后面每个源都往同一份日志里追加
        # —— `[任务 2/3] 源：…` 这一行本身就只有"不清日志"才读得通。
        #
        # 【本轮修复】判据从 `if self._src_index <= 1:` 换成这个幂等调用 ——
        #   旧判据在"第 1 个源被预检跳过"时**永不成立**（那时 `_src_index` 已是 2），
        #   于是本轮日志从不清、`启动 N 个源传输任务` 也一次都不打。
        #   完整根因与证据见 `_begin_run_log` 的注释。
        #   放在**这个位置**（校验过完、任务锁拿到之后）没有变：
        #   "校验没过 → 日志不动"这条既有行为保持原样。
        self._begin_run_log()

        # 【本轮修复·第二处】走到这里 = 本源**真的起跑了**（校验过完、任务锁拿到）。
        #   只记"起跑过几个"，不记"打算跑几个"—— 判据见 `_start_next_source` 里
        #   `if self._src_index > total:` 那个分支的注释。
        #   ⚠️ 只能加在**这一处出口**：上面 6 个 `return False` 都不算起跑。
        self._src_ran += 1

        self._completed = 0
        self._skipped = 0
        self._pause_event.set()
        self._stop_event.clear()
        self._last_failed = []
        # 【阶段2d】中止状态必须随任务归零，否则上一次的"连续失败中止"会串到这一次：
        # _stop_reason 残留会让本次的 end_reason 写成 consec_fail，_consec_fail 残留
        # 会让本次开局就离阈值只差一个失败。
        self._consec_fail = 0
        self._stop_reason = None
        # 【功能B】速度状态随任务归零。**必须在这里**，理由与上面 _consec_fail 一样：
        # 残留的滑窗会让新任务开局就"继承"上一次的速度（可能是一小时前那次），
        # _speed_ema 残留更糟 —— EMA 是"新值 30%"，一个陈旧的旧值会把开局的头
        # 几秒拖住，看起来就是"刚开始传就很慢"。
        self._ui._speed_reset()
        self._task_active = True            # 【阶段1】开启本次任务的终态幂等窗口
        self.retry_btn.configure(state="disabled")
        self.start_btn.configure(state="disabled")
        self.pause_btn.configure(state="normal", text="暂停")
        self.stop_btn.configure(state="normal")

        # 【功能B】起跳。放在**按钮切到运行态之后**：本函数一开头那批校验
        # （空源 / 路径不存在 / 目标在源内部 / 抢不到任务锁）都会提前 return，
        # 定时器要是排在它们前面，就会出现"没任务在跑、但有个 500ms 链在空转"。
        # 【功能B】起跳。**必须在上面那批校验之后**：本函数开头那些提前 return
        # （空源 / 路径不存在 / 目标在源内部 / 抢不到任务锁）要是走掉了，
        # 定时器就已经在空转了 —— 没任务在跑，却每 500ms 刷一次一个空标签。
        self._ui._note_task_start_time()      # 【第 1 步】ETA 的计时零点
        self._ui._start_speed_poll()

        # 【功能A-新增】落盘"这次用了什么参数"。
        #
        # 【为什么在这个位置 —— 这是本功能唯一重要的判断】
        #   它在**全部校验通过、任务锁已抢占、UI 已切到运行态之后**，
        #   也就是"这次传输真的开始了"。放在函数开头（点了就写）是错的：
        #   验收里有一条"传一个空源 → 路径不应该被记住"，
        #   而空源/路径不存在/目标在源内部这几种情况都会在**上面**被挡下并 return，
        #   根本走不到这一行。填错一次就把上次真正跑成功的参数冲掉，是净损失。
        #
        # 【为什么调 config 的静态函数，而不是自己开文件】
        #   与 `save_config` 同一层：常量与它的读写在一起，且主模块不 re-export（R5 守着）。
        #   本行**不夹带任何异常处理**：`save_ux_state` 内部已经吞掉所有异常
        #   （理由见 config.py 该函数上方），这里再包一层 try 只会让"它到底抛不抛"变得含糊。
        self._hooks.on_ux_changed()

        # 【P0-1】建本次源的剪枝器。**必须在主线程、且必须在 `_begin_run_log()` 之后**
        #   —— 那一位在本函数更靠上处清日志区，它打的那两行要是排在清屏前面，
        #   用户就一条都看不见（"启用了没有"这件事实恰恰只能从这两行读出来）。
        #   放在派发之前：三个 `_start_*` 都会立刻起后台线程，那些线程张口就读
        #   `self._date_pruner` —— 晚一行就是"第一个目录没被剪"。
        #   它一次性同时管本地与 FTP：`_date_pruner` 是一个属性，四条 `os.walk` 与
        #   FTP 遍历点都按"它是不是 None"决定要不要剪 —— 这就是 A-④
        #   "本地与 FTP 用同一模板"在代码里的样子（只有一份，不可能不一致）。
        self._date_layer_begin()

        # 【P1】建本次源的判重记录快照。**紧挨着剪枝器**，两条理由与它逐字相同：
        #   主线程（要读 `_db_task_src_root`）、派发之前（三个 `_start_*` 立刻起线程，
        #   那些线程张口就 `_should_skip`）。晚一行 = 头几个文件走逐文件查库。
        #   它建在剪枝器**之后**：日志顺序是"日期分层启用…"在前、"读入 N 条判重记录"
        #   在后，读起来就是"先决定扫哪些、再把要判重的都装进内存"。
        self._skip_states_begin()

        # 【阶段三】共享与本地**走同一个 worker**（`_start_local_to_local`）：
        #   UNC 在 `os` 眼里就是个路径，`os.walk` / `open` / `shutil.copy2` 全都
        #   一视同仁。**不新开 `_start_share_to_*`** —— 那会造出第二套传输逻辑，
        #   而两份实现必然漂移（本项目记过这个教训）。
        if src_type in self._local_like_src and dst_type == "local":
            self._start_local_to_local(extensions)
        elif src_type == "ftp" and dst_type == "local":
            self._launcher.start_ftp_to_local(extensions)
        elif src_type == "local" and dst_type == "ftp":
            self._launcher.start_local_to_ftp(extensions)
        return True


    def _start_local_to_local(self, extensions):
        src = self.source_path.get().strip()
        dst = self.dest_path.get().strip()
        # Tk 变量一律在主线程读好再传给后台线程（Tk 变量跨线程读取不安全）
        flatten = self.flatten.get()
        mode = self.transfer_mode.get()
        # 不再在主线程全盘计数（那是"一开始就卡死"的根源）：
        # 直接切跑马灯进度条，后台线程用 os.walk 惰性逐文件夹扫描并传输
        self._ui.progress.configure(mode="indeterminate", maximum=100, value=0)
        self._ui.progress.start(50)
        num_threads = self.thread_count.get()
        if num_threads == 1:
            t = threading.Thread(target=self._stream_transfer, args=(src, dst, mode, extensions, flatten), daemon=True)
        else:
            t = threading.Thread(target=self._stream_transfer_concurrent, args=(src, dst, mode, extensions, num_threads, flatten), daemon=True)
        t.start()


    @staticmethod
    def _unique_path(dst_dir, basename):
        """FTP 扁平化下载用：避免与目标盘已有文件重名。"""
        target = os.path.join(dst_dir, basename)
        if not os.path.exists(target):
            return target
        name, ext = os.path.splitext(basename)
        counter = 1
        while True:
            target = os.path.join(dst_dir, f"{name}_{counter}{ext}")
            if not os.path.exists(target):
                return target
            counter += 1


    @staticmethod
    def _plan_target(base_target, fname, occupied):
        """规划目标路径：本次运行内同名冲突时加 _N 后缀，避免扁平化模式下数据丢失。"""
        target = base_target
        if target in occupied:
            base_dir = os.path.dirname(base_target)
            name, ext = os.path.splitext(fname)
            counter = 1
            while target in occupied:
                target = os.path.join(base_dir, f"{name}_{counter}{ext}")
                counter += 1
        occupied.add(target)
        return target


    # 【阶段2b-新增】可信续传判定（方案 §3.1）。六条**全部**满足才跳过；
    # 任意一条不满足都返回 False（= 重传）。每一条都是"宁可重传，不可漏传"的方向。
    # 回滚（_STATE_DB_ENABLED=False）或状态库降级时，退回阶段1 的 size-only 判定，
    # 使关掉开关后的行为与阶段1 逐字一致。
    # 本函数只读库、不写库（Q5：跳过不写库，否则重跑 20 万文件要写 20 万条无意义记录）。
    # 【阶段5-新增 dst_probe】第 ⑥ 条目标完整性的**远端版本**。上传路径给一个返回
    # "远端目标当前大小"的可调用对象（通常是 conn.size 的闭包），本地路径不给（None）。
    # 惰性调用是刻意的：探针 = 一次 SIZE 往返，只有前五条都过了的文件才值得花这一次——
    # 首次上传时库里没有记录，绝大多数文件在第 ① 条就返回，不该为它们白付一次往返。
    # 远端探针返回 None（SIZE 失败 / 文件不存在 / 服务器不支持 SIZE）一律**不跳过**：
    # "量不出来"和"量出来不对"在数据安全上是同一件事。
    # 【任务1 修改原因】首形参 `filename` 删掉：它在这函数里**只有一个用处** —— 那次查库。
    # 判重口径改成全路径 `src_path` 之后它就没用了，留着只会让人以为"库里按文件名查"。
    # ⚠️ 这里传下去的 `src_path` 必须与 `_db_dispatch` 里 `mark_done/mark_failed` 收到的
    # **逐字节相同**，否则续传会静默失效（症状：不报错，但每次都全量重传）。
    # 保证方式是结构性的：四个调用点传的都是**同一个局部变量**，而那个变量随后又被原样
    # 塞进 `_db_enqueue`（本地 2476/2481/2492、下载 3255/3244、上传 3312/3298、
    # FTP 单文件 3480/3467 —— 逐一核过）。**中间不许对这个字符串做任何加工。**
    def _should_skip(self, src_path, dst_path, mode, precision, src_size, src_mtime,
                     dst_probe=None):
        if not config._STATE_DB_ENABLED or self._state_db_degraded or self._current_task_id is None:
            return self._should_skip_by_size(src_path, dst_path)
        # 【阶段2c-修复】源 mtime 或精度**缺失**时，源状态不可信 → 一律不跳过。
        # 这条必须显式写出来：两个 None 是**相等**的，若只靠下面那条 != 比较，
        # "服务器没给 modify=（MLSD 无 modify 属性）"或"源文件 stat 失败"的文件，
        # 第二次运行会被判定为"mtime 没变"而全部命中跳过 —— 判据退化成 size-only，
        # 正是阶段2 要消灭的 P0-1 风险面（DICOM 序列内同大小文件极常见，
        # 一旦目标不是同一个文件就会永久丢数据）。宁可重传，不可漏传。
        if precision is None or src_mtime is None:
            return False
        # 【P1】取数改走内存快照（有的话）。**判据一个字没动** —— 六条闸门原样，
        #   换的只是"这份记录从哪来"：`self._skip_states` 里是预取回来的 tuple，
        #   没有（回滚 / 库降级 / 预取读失败 / 重传路径）就逐文件查库，拿回一个 dict。
        #   两种形态由 `_state_getter` 收成同一个读取器，闸门代码不写两遍。
        if self._skip_states is not None:
            st = self._skip_states.get(src_path)
        else:
            try:
                st = self._db.get_file_state(src_path)
            except Exception as e:
                # 查询失败按"未传过"处理：重传是安全的，跳过才危险
                self._ui._threadsafe_log(f"[警告] 状态库查询失败，该文件按未传处理：{e}")
                return False
        if st is None:
            return False                       # ① 库里没有（这个路径没传过）
        get = _state_getter(st)
        if get("status") != "done":
            return False                                   # ② 上次是 failed
        if get("src_size") != src_size:
            return False                                   # ③ 源大小变了
        if get("mtime_precision") != precision or get("src_mtime") != src_mtime:
            # ④ 源 mtime 变了，或两侧精度不同不可比（方案 §3.3）
            return False
        if get("dst_path") != dst_path:
            # ⑤ 目标变了。缺这条会导致"改传另一个目标盘时全部命中跳过、而新目标一个文件都没有"
            return False
        if mode == "move" and not get("src_deleted") and os.path.exists(src_path):
            # ⑥' move 模式：库里说传完了，但源还在 → 重做（见 mark_src_deleted 的口径）
            return False
        if dst_probe is not None:
            # 【阶段5】远端目标：问服务器要当前大小。None（不可信）一律不跳过。
            try:
                if dst_probe() != src_size:
                    return False                           # ⑥ 远端目标不在/不完整/量不出来
            except Exception:
                return False                               # 探针本身出错也按"不可信"处理
            return True
        try:
            if os.path.getsize(dst_path) != src_size:
                return False                               # ⑥ 磁盘上的目标不完整
        except OSError:
            return False                                   # 目标不在/不可读
        return True


    # 【阶段2b-新增】阶段1 的原判定，原样保留作为回滚路径。
    @staticmethod
    def _should_skip_by_size(src_path, dst_path):
        if os.path.exists(dst_path):
            try:
                if os.path.getsize(dst_path) == os.path.getsize(src_path):
                    return True
            except OSError:
                pass
        return False


    # 【阶段2b-新增】原子落盘（方案 §2、§1.3 铁律一）。
    # 写入 .part → os.replace(.part, 目标)，使"目标文件存在"在物理上等价于"目标文件完整"。
    # 铁律：os.replace 成功之前绝不写 DB 的 done。本函数返回后调用方才埋点，
    #       因此崩溃只可能留下"目标完整但库里没记录"（下次重传，不丢数据）。
    # 覆盖审计（Q1 选项C）：仅当"覆盖前后的 size 或 mtime 至少一项不同"才记一条，
    #       size 与 mtime 都相同视为"同一文件的重复投递"，静默处理（避免几十万条废记录）。
    def _atomic_write_local(self, transfer_func, src, dst, mode):
        # 补充 B：目标目录必须提前存在（原子落盘不再依赖 transfer_func 自行处理）
        d = os.path.dirname(dst)
        if d:
            os.makedirs(d, exist_ok=True)

        if not config._ATOMIC_WRITE_ENABLED:
            transfer_func(src, dst)          # 回滚：退回阶段1 的直接写目标路径
            return

        # 覆盖前记下旧文件属性（仅在目标已存在时多花一次 stat）
        old = None
        if os.path.exists(dst):
            _precision, old_size, old_mtime = config._stat_for_state(dst)
            old = (old_size, old_mtime)
        _, new_size, new_mtime = config._stat_for_state(src)

        part = dst + ".part"
        if mode == "move":
            try:
                os.replace(src, dst)         # 同盘：原子直移，不拷数据（方案 §2 / Q4）
            except OSError:
                pass                         # 跨盘（EXDEV）或目标被占用 → 退回下面的拷贝路径
            else:
                self._audit_overwrite(os.path.basename(src), dst, old, new_size, new_mtime)
                return
        try:
            shutil.copy2(src, part)
            os.replace(part, dst)
        except BaseException:
            try:
                os.remove(part)              # 失败不留 .part 残渣（写失败的那份不完整，无保留价值）
            except OSError:
                pass
            raise
        self._audit_overwrite(os.path.basename(src), dst, old, new_size, new_mtime)
        if mode == "move":
            # 铁律二：dst 已完整 → 才删源。删源失败会让本文件记 failed（下次重做，不丢数据）
            os.remove(src)


    # 【阶段2c-新增】FTP → 本地的原子落盘（方案 §2）。铁律一同样适用：
    # os.replace 成功之前绝不写 DB 的 done。
    # 与 _atomic_write_local 是**刻意的兄弟实现，不是重复**。三处硬差异使它们无法合并：
    #   ① 远端 src 无法 os.stat → 覆盖审计的"新属性"只能写完回读本地目标；
    #   ② 没有"同盘 os.replace 直移"可用（源在 FTP 服务器上），恒走 .part + os.replace；
    #   ③ mode 恒为 copy：远端源的删除语义本工具尚未实现，套用 move 分支是错的。
    # 合并需要引入 remote_source 开关 + 三处条件分支，省下约 10 行，
    # 代价是让已经过 2b 全套用例验证的本地路径重新承担风险。不划算。
    def _atomic_write_download(self, ftp, remote_path, local_path, remote_mtime):
        d = os.path.dirname(local_path)
        if d:
            os.makedirs(d, exist_ok=True)

        if not config._ATOMIC_WRITE_ENABLED:
            ftp.download_file(remote_path, local_path, remote_mtime)   # 回滚：直接写目标
            return

        old = None
        if os.path.exists(local_path):
            _precision, old_size, old_mtime = config._stat_for_state(local_path)
            old = (old_size, old_mtime)

        part = local_path + ".part"
        try:
            ftp.download_file(remote_path, part, remote_mtime)
            os.replace(part, local_path)
        except BaseException:
            try:
                os.remove(part)              # 失败不留 .part 残渣（不完整的那份无保留价值）
            except OSError:
                pass
            raise
        # 写完回读本地目标：远端 mtime 已在 download_file 内用 os.utime 对齐到 .part 上，
        # os.replace 不改 mtime，所以这里拿到的就是"这份新内容"的真实属性。
        _precision, new_size, new_mtime = config._stat_for_state(local_path)
        self._audit_overwrite(os.path.basename(remote_path), local_path,
                              old, new_size, new_mtime)


    # 【阶段2b-新增】覆盖审计的唯一出口（Q1 选项C 的写入条件判定在这里）。
    # 【D-1 修改原因】"大小 + mtime 全同"那一档**不再一律静默**。
    # 单源时代它确实只能是"同一份的重复投递"，静默是对的；多源之后它多了一种可能：
    # 目标上那一份是**别的源**写下的 —— 本源判重按 (filename, src_root) 各算各的，
    # 所以本源仍然真复制了一次、目标上那一份确实被换掉了，只是新旧 size/mtime 恰好相同。
    # **不查这一下，这种覆盖在审计里一点痕迹都没有** —— 而它恰恰是多源场景最常见的一种。
    # 探针 S1 就是这一档（同名 + 同大小 + 同 mtime），按改前的代码那条日志不会出现。
    def _audit_overwrite(self, filename, dst, old, new_size, new_mtime):
        if old is None or not config._STATE_DB_ENABLED or self._state_db_degraded:
            return
        old_size, old_mtime = old
        old_src_root = None
        if old_size == new_size and old_mtime == new_mtime:
            # 查"这个目标路径是谁写下的"，并把自己排除掉（自己覆盖自己不算跨源）
            try:
                prev = self._db.find_dst_owner(dst, self._db_task_src_root)
            except Exception as e:
                # 查不出来就按"查不到"处理：少一条审计可以接受，
                # 让一次审计查询失败反过来影响传输本身不可以接受。
                self._ui._threadsafe_log(f"[警告] 跨源覆盖审计查询失败，本条不记：{e}")
                return
            if prev is None:
                return               # 真是同一份的重复投递 → 仍静默（不制造几十万条废记录）
            old_src_root = prev["src_root"]
            self._log_overwrite(filename, dst, old_src_root)
        # 第 8 项（下标 7）= 被覆盖的那份原来属于谁；_db_dispatch 用 len>7 兼容读取。
        self._db_enqueue(("overwrite", filename, dst, old_size, old_mtime,
                          new_size, new_mtime, old_src_root))


    def _log_overwrite(self, filename, dst, old_src_root):
        """记一条跨源覆盖日志；超过阈值后**降级成累计行**（方案 C）。

        【为什么必须做】2000 个同名文件跨源覆盖时，逐条版本打 2000 行，
        而日志区上限是 `config.MAX_LOG_LINES = 1000` 行 ——
        `启动 N 个源传输任务`、`[源 X/N] 完成：…` 这些**用户真正要看的关键行**
        会被覆盖日志整批顶出视野。而故障现场恰恰是用户最需要看这些行的时候，
        等于把日志功能废掉了。实测（`_verify_feature_d.py` 用例 T）：
        2000 次覆盖下本函数只打 **55 行**（50 + 1 条转聚合 + 4 条累计）。

        【为什么不是全聚合（方案 A）】排查时要看**具体哪个文件**被覆盖。
        只报"发生了 2000 次"没法知道是哪 2000 个，等于换了一种没法用。

        【为什么不是只写表（方案 B）】`overwrites` 表里逐条都在，但用户不会为了
        看一眼去开 SQLite；界面上一个字不显示，观感上就是"程序在悄悄覆盖"。

        【为什么是 50 不是 100】50 条已经够看出模式（是不是整目录同名、哪两个源在打架）；
        整条序列的日志预算就 1000 行，给覆盖日志的份额要留得体面。
        """
        # 计数：
        #   * 计数必须在 `self._lock` 里自增 —— 并发线程数 >1 时本函数是**多个
        #     worker 同时进**的（`_audit_overwrite` 在拷贝线程里调）。
        #   * 打日志必须在**出锁之后** —— `_threadsafe_log` 内部还要加 `_log_lock`
        #     并 queue.put，持锁调用会把临界区拉长到包含一次队列操作
        #     （同 `_transfer_one` 里那条既有纪律）。
        with self._lock:
            self._overwrite_logged += 1
            n = self._overwrite_logged
        head, every = self._OVERWRITE_LOG_HEAD, self._OVERWRITE_LOG_EVERY
        if n <= head:
            self._ui._threadsafe_log(
                "⚠ 源「%s」的 %s 覆盖了源「%s」的同名文件（目标：%s）"
                % (self._db_task_src_root, filename, old_src_root, dst))
        elif n == head + 1:
            # 转聚合的**那一枪**要交代清楚：逐条到此为止，后面只在累计行里露面。
            self._ui._threadsafe_log(
                "⚠ 跨源同名覆盖已多，以下改为只记累计：已发生 %d 次跨源覆盖"
                "（详见 overwrites 表）" % n)
        elif n % every == 0:
            self._ui._threadsafe_log(
                "⚠ 跨源同名覆盖累计：已发生 %d 次跨源覆盖（详见 overwrites 表）" % n)


    # 【阶段2b-修改原因】原子写下沉到 _atomic_write_local（方案 §2）：
    # 本函数只保留"重试 + DiskFullError 识别"两项职责，不再直接调用 transfer_func。
    # 因此由 staticmethod 改为实例方法（_atomic_write_local 需要 self 投递覆盖审计）。
    # 【阶段2d-修改原因】退避曲线按方案 §8 改为 1s/3s/9s（回滚：见 RETRY_BACKOFF_S）。
    # attempts 由 3 提到 4 —— §8/Q8 的耗时上限 13 秒 = 1+3+9 是三次等待、四次尝试，
    # 沿用 attempts=3 只会等 1s/3s 两次，第三级形同虚设。
    # 独立可回滚：把 RETRY_BACKOFF_S 换回 (0.5, 1.0) 并把 attempts 还原为 3 即可。
    # 【阶段二-修改：attempts 的默认值从字面量 4 改成"读任务快照"】
    #   口径：界面上填的是**重试次数 N**，这里用的是**尝试次数 N+1**（= 用户填 3）。
    #   默认值一字未变 —— `_task_retry_attempts` 的初值就是 `DEFAULT_RETRY_COUNT + 1` = 4，
    #   与改动前那个 `attempts=4` 逐字相同。
    #   ⚠️ 为什么读 `self._task_*` 而不是直接读 `self.retry_count.get()`：
    #      后者是 **Tk 变量**，而本函数跑在 worker 线程上 —— 跨线程读 Tk 变量在这个
    #      项目里是不允许的（整个 `_post_ui` / `_task_*` 快照机制就是为它建的）。
    #      快照在 `_start_transfer` / `_retry_failed` 里由主线程取好。
    def _copy_with_retry(self, transfer_func, src, dst, mode, attempts=None):
        """复制/移动单个文件；OSError（如硬件 I/O 错误）自动重试，仍失败返回错误信息；
        磁盘空间不足则立即抛 DiskFullError 中止整个任务（重试无意义）。"""
        if attempts is None:
            attempts = self._task_retry_attempts
        for i in range(attempts):
            try:
                self._atomic_write_local(transfer_func, src, dst, mode)
                return True, None
            except OSError as e:
                # Windows ERROR_DISK_FULL(112) 映射为 ENOSPC
                if e.errno == errno.ENOSPC or getattr(e, 'winerror', None) == 112:
                    raise DiskFullError(f"目标磁盘空间不足：{dst}")
                if i < attempts - 1:
                    time.sleep(config.RETRY_BACKOFF_S[min(i, len(config.RETRY_BACKOFF_S) - 1)])
                    continue
                return False, str(e)
            except Exception as e:
                return False, str(e)
        return False, "unknown error"


    # 【阶段2d-新增】连续失败计数（方案 Q8）。返回 True 表示"已触到中止阈值"。
    # 成功一次即清零，这是本判据能被信任的关键：要挡的是"目标盘掉线/只读/写满"
    # 这类**持续**错误；而"散落的坏文件"之间夹着成功，计数会被清零，任务不会被误中止。
    def _note_transfer_result(self, ok):
        with self._lock:
            if ok:
                self._consec_fail = 0
                return False
            self._consec_fail += 1
            return self._consec_fail >= config.MAX_CONSEC_FAILURES


    # 【阶段2d-新增】触发中止。幂等：已经停过就直接返回，避免 N 个 worker 抢着刷日志。
    def _abort_on_consec_failures(self):
        if self._stop_reason == "consec_fail":
            return
        self._stop_reason = "consec_fail"
        self._stop_event.set()
        self._pause_event.set()      # 解除暂停阻塞，让 worker 在下一个文件边界停下
        self._ui._threadsafe_log(
            f"[中止] 连续失败 {config.MAX_CONSEC_FAILURES} 个文件，目标可能已不可写/已断开，"
            f"已停止本次传输（已完成的文件都已落库，重新开始会从断点续传）")


    # 【阶段2d-新增】终态原因。显式中止原因优先于 stopped/finished——
    # 两种都会置 _stop_event，只按事件判断的话，"连续失败中止"在库里会被记成
    # 用户点了停止，事后追查分不清是人为还是故障。
    def _end_reason(self):
        return self._stop_reason or ("stopped" if self._stop_event.is_set() else "finished")


    # 【阶段2a-修改原因】在三个出口各追加一次 _db_enqueue 埋点。
    # 判定分支（跳过条件 / 重试 / 失败归类）一行未动——本阶段只记录"发生过什么"。
    # 跳过分支**不入队**：按 Q5，跳过的文件不写 files 表，
    # 它的计数只存在于内存里的 self._skipped，由 finish_task 的 skipped_count 落库。
    # 【阶段1-修改原因】§2.5：原代码在 with self._lock 块内调用 root.after（跨线程碰 Tk）。
    # 改为先在锁内取走计数快照，出锁后再投递——_threadsafe_log 内部会加 _log_lock
    # 并 queue.put，持锁调用会把临界区拉长到包含一次队列操作。
    # 【阶段2b-修改原因】判定分支从"目标存在且 size 相同"升级为 _should_skip 的六条全条件；
    # 写入分支从直接调 transfer_func 升级为 _atomic_write_local（经 _copy_with_retry）。
    # 新增 mode 入参：原子写要按 copy/move 分流（同盘 move 走 os.replace 直移）。
    # 跳过分支仍然不写库（Q5）。
    def _transfer_one(self, src_path, target_path, transfer_func, failed, mode):
        """单个文件：可信续传判定命中则跳过；否则原子落盘，
        重试后仍失败则记入失败清单继续，绝不中断整个任务。"""
        if self._stop_event.is_set():
            return
        self._pause_event.wait()
        if self._stop_event.is_set():
            return
        # 断点续传：_should_skip 六条全满足才跳过（方案 §3.1）
        precision, src_size, src_mtime = config._stat_for_state(src_path)
        if self._should_skip(src_path, target_path,
                             mode, precision, src_size, src_mtime):
            with self._lock:
                self._skipped += 1
                self._completed += 1
            return
        # 【阶段一 Bug3】同 FTP 两条路径：跳过判定之后、真正复制之前。
        # 本地→本地路径不设跑马灯，本标志在这里暂时用不上 —— 置它不是为了今天，
        # 而是让"第一个文件开始传输"这件事在**三条搬运路径上口径一致**，
        # 免得下一次再有人把某个判据挂到"扫描结束"上去。
        self._ui._note_transfer_start()
        ok, err = self._copy_with_retry(transfer_func, src_path, target_path, mode)
        if ok:
            with self._lock:
                self._completed += 1
                done = self._completed
            # 投递必须在锁外：_threadsafe_log 内部会加 _log_lock 并 queue.put
            if done % 50 == 0:
                self._ui._threadsafe_log(f"已处理 {done} 个文件")
            self._db_enqueue(("done", src_path, target_path))     # 【阶段2a】成功埋点
            if mode == "move":
                # 【阶段2b】铁律二（方案 §1.3）：dst 已完整 → 源已删除 → 标记 src_deleted=1。
                # 能走到这里说明 _atomic_write_local 的 os.remove(源) 已成功；
                # 该标记是下次运行判定"源残留 → 重做"的依据（2a 建好接口，本阶段接上调用点）。
                self._db_enqueue(("src_deleted", src_path))
            # 【阶段2d】成功即清零连续失败计数
            self._note_transfer_result(True)
            # 【功能B】1/5 记账点：本地单文件。`src_size` 是上面 `_stat_for_state`
            # 顺手拿到的（第 ⑥ 条续传判定本来就要它），**不额外付一次 stat**。
            # 它为 None（stat 失败）时不记 —— 宁可少记一个文件，也不编一个字节数。
            self._ui._note_bytes(src_size)
        else:
            with self._lock:
                failed.append((src_path, target_path, err))
            self._ui._threadsafe_log(f"失败跳过: {src_path} - {err}")
            self._db_enqueue(("failed", src_path, target_path, err))   # 【阶段2a】失败埋点
            # 【阶段2d】连续失败阈值（Q8）。注意埋点必须**先于**中止：中止只是不再
            # 派发新文件，这一条失败记录仍要落库，否则界面上的失败数会比实际少一个。
            if self._note_transfer_result(False):
                self._abort_on_consec_failures()


    # 【阶段1-修改原因】3 处终态投递（单文件分支 / 正常结束 / 异常）全部改为
    # 先 _flush_log_buffer(force=True) 再 _post_ui(...)。
    # flush 必须先做：否则缓冲区里不满一批的日志会被终态消息抢先，
    # 而终态处理完会 messagebox + 复位按钮，用户再也看不到那几条日志。
    def _stream_transfer(self, src, dst, mode, extensions, flatten):
        """本地单线程流式传输：os.walk 惰性逐文件夹扫描并传输，不预扫描全盘、不占大内存。"""
        try:
            transfer_func = shutil.copy2 if mode == "copy" else shutil.move
            failed = []
            folder_count = 0
            matched = 0
            occupied = set()

            if os.path.isfile(src):
                if config._match_filter(src, extensions):
                    matched += 1
                    # 目标固定为 目录/文件名，使断点续传的大小判断基于真实目标文件
                    base = os.path.join(dst, os.path.basename(src))
                    self._transfer_one(src, base, transfer_func, failed, mode)
                self._db_end_task(
                    "done_stream",
                    self._end_reason(),
                    folder_count=folder_count, matched=matched, total_seen=matched,
                    stopped=self._stop_event.is_set(), failed_count=len(failed))
                return

            # 【P0-1】剪枝器在后台线程里**只读一次**，不在循环里反复读 `self.` ——
            #   它是主线程在起跑前置好的快照，循环里再读只会多一个"读到 None"的窗口。
            pruner = self._date_pruner
            for dirpath, dirs, filenames in os.walk(src):
                if self._stop_event.is_set():
                    break
                self._date_prune_walk(pruner, src, dirpath, dirs)
                folder_count += 1
                self._ui._threadsafe_log(f"[{folder_count}] {dirpath}（{len(filenames)} 个文件）")
                if not flatten:
                    rel = os.path.relpath(dirpath, src)
                    target_dir = dst if rel == "." else os.path.join(dst, rel)
                    os.makedirs(target_dir, exist_ok=True)
                for fname in filenames:
                    if self._stop_event.is_set():
                        break
                    full_path = os.path.join(dirpath, fname)
                    if not config._match_filter(full_path, extensions):
                        continue
                    matched += 1
                    base = os.path.join(dst, fname) if flatten else os.path.join(target_dir, fname)
                    target = self._plan_target(base, fname, occupied)
                    self._transfer_one(full_path, target, transfer_func, failed, mode)

            self._db_end_task(
                "done_stream",
                self._end_reason(),
                folder_count=folder_count, matched=matched, total_seen=matched,
                stopped=self._stop_event.is_set(), failed_count=len(failed))
        except DiskFullError as e:
            self._db_end_task("error", "disk_full", msg=str(e))
        except Exception as e:
            self._db_end_task("error", "error", msg=str(e))


    # 【阶段1-修改原因】同 _stream_transfer：2 处终态改为 flush + _post_ui。
    # 并发路径的终态是在 ThreadPoolExecutor 的 with 块退出后才投递的，
    # 此刻所有 worker 已结束，不存在"投递时还有 worker 在写日志"的竞争。
    def _stream_transfer_concurrent(self, src, dst, mode, extensions, num_threads, flatten):
        """本地多线程流式传输：逐文件夹规划并派发，内存只占一个文件夹的量，保留并发。"""
        try:
            transfer_func = shutil.copy2 if mode == "copy" else shutil.move
            failed = []
            folder_count = 0
            matched = 0
            occupied = set()

            pruner = self._date_pruner       # 【P0-1】同上：循环外读一次
            with ThreadPoolExecutor(max_workers=num_threads) as executor:
                for dirpath, dirs, filenames in os.walk(src):
                    if self._stop_event.is_set():
                        break
                    self._date_prune_walk(pruner, src, dirpath, dirs)
                    folder_count += 1
                    self._ui._threadsafe_log(f"[{folder_count}] {dirpath}（{len(filenames)} 个文件）")
                    if not flatten:
                        rel = os.path.relpath(dirpath, src)
                        target_dir = dst if rel == "." else os.path.join(dst, rel)
                        os.makedirs(target_dir, exist_ok=True)
                    jobs = []
                    for fname in filenames:
                        if self._stop_event.is_set():
                            break
                        full_path = os.path.join(dirpath, fname)
                        if not config._match_filter(full_path, extensions):
                            continue
                        matched += 1
                        # 目标路径在本线程内顺序规划，避免 _plan_target 重名竞争
                        base = os.path.join(dst, fname) if flatten else os.path.join(target_dir, fname)
                        target = self._plan_target(base, fname, occupied)
                        jobs.append((full_path, target))
                    if not jobs:
                        continue
                    # 等本文件夹全部完成再进下一个，保证重名判断基于真实磁盘状态
                    futures = {executor.submit(self._transfer_one, s, t, transfer_func, failed, mode): s for s, t in jobs}
                    for future in as_completed(futures):
                        try:
                            future.result()
                        except DiskFullError:
                            raise   # 磁盘满 → 中止整个任务
                        except Exception as e:
                            with self._lock:
                                failed.append((futures[future], "", str(e)))
                            self._ui._threadsafe_log(f"失败跳过: {futures[future]} - {e}")
                            # 【阶段2a】兜底失败埋点：target 未知，用空串占位，
                            # 使状态库的失败计数与内存计数保持一致
                            self._db_enqueue(("failed", futures[future], "", str(e)))

            self._db_end_task(
                "done_stream",
                self._end_reason(),
                folder_count=folder_count, matched=matched, total_seen=matched,
                stopped=self._stop_event.is_set(), failed_count=len(failed))
        except DiskFullError as e:
            self._db_end_task("error", "disk_full", msg=str(e))
        except Exception as e:
            self._db_end_task("error", "error", msg=str(e))


    # 【阶段2a-修改原因】状态库连续写入失败后的降级（方案 §5.4）。
    # 只关掉"持久化能力"，绝不动本次运行的内存统计，也绝不中止传输：
    #   mark_done 失败 → 文件已正确落盘，下次重传是浪费但不丢
    #   mark_src_deleted 失败 → 源残留，下次自愈
    #   mark_failed / record_overwrite 失败 → 少几条记录，不影响传输
    # 降级是单向的（本任务内不恢复），下次启动自然恢复。
    # 本方法可能由 db-writer 线程调用，因此改标题栏走 _post_ui，不直接碰 Tk。
    def _degrade_state_db(self):
        if self._state_db_degraded:
            return
        self._state_db_degraded = True
        if not self._db_title_changed:
            self._db_title_changed = True
            self._ui._post_ui("title", "文件传输工具 v2.4 [续传已失效]")


    # 【阶段2a-修改原因】用户关闭窗口时显式收尾，不留 running 行。
    # 用短连接 + 短超时：db-writer 可能正持有写锁，但进程马上要退出，
    # 抢不到锁也无所谓——那条行会在 30 秒后被下一个任务的 claim_task 判为陈旧并清掉。
    def _abort_current_task(self, reason):
        if not config._STATE_DB_ENABLED or self._state_db_degraded:
            return
        if self._current_task_id is None:
            return
        try:
            # 【阶段2d】改调 TransferStateDB.abort_task：恢复弹窗的 [放弃] 需要同一条
            # UPDATE，两处各写一份 SQL 迟早会改岔（一处改了一处漏）。
            self._db.abort_task(self._current_task_id, reason)
        except Exception:
            pass   # 收尾失败不影响退出；陈旧心跳会兜底


    # 【阶段2a-修改原因】按当前 UI 参数抢占一个任务（Q6 任务级锁）。
    # 所有 Tk 变量在这里、在主线程读好，再交给状态库。
    # 返回 task_id；失败返回 None 且已弹窗提示，调用方直接 return、不改按钮状态。
    def _claim_current_task(self, extensions):
        if not config._STATE_DB_ENABLED:
            return -1          # 回滚模式：用一个占位 id，不参与状态库写入
        # 【D-1 修改原因】源根在这里一次取好，认领**成功之后**才落进 _db_task_src_root。
        # 取在这里是硬要求：self.source_path 是 Tk 控件，只有主线程能读，而判重发生在
        # 工作线程里。落"成功之后"也是硬要求：认领失败时不许留下一个"看着像某个源"的
        # 半成品 —— 那会让下一次判重拿着上一个源的路径去查。
        src_root = self.source_path.get().strip()
        try:
            task_id = self._db.claim_task(
                src_type=self.source_type.get(),
                src_root=src_root,
                dst_type=self.dest_type.get(),
                dst_root=self.dest_path.get().strip(),
                mode=self.transfer_mode.get(),
                extensions=extensions,
                flatten=bool(self.flatten.get()))
        except state_db.TaskLockedError as e:
            info = e.task_row
            # 【阶段四】无头模式：**不弹框**，退出码 2（让路），**不重试**。
            #
            # 【为什么"让路"是正确语义，而不是"排队等它"】
            #   排队得有一个常驻进程在那儿等着前一个跑完 —— 而本项目明确不做常驻
            #   （见 `22_plan_stage4.md` §①）。没有常驻进程，时间一到两个进程各自启动，
            #   谁也管不了谁。并行同样不行：任务锁是全局单锁，而且两个任务会抢
            #   同一个目标盘与带宽。于是只剩一个说得通的语义：
            #   **同一时刻只跑一个，撞上的那个这次算了**。下一个周期照常。
            #   （不重试也是用户定死的：明写"不做任务重试"。）
            if self._headless_task is not None:
                self._hooks.on_fatal(
                    config.SCHEDULE_EXIT_LOCKED,
                    "[无头] 已有任务在运行（源：%s → %s，开始于 %s，已完成 %s 个），"
                    "本次让路，不重试。"
                    % (info.get('src_root', ''), info.get('dst_root', ''),
                       info.get('created_at', ''), info.get('done_count', 0)))
                return None
            messagebox.showwarning(
                "已有任务在运行",
                "另一个窗口正在执行传输：\n\n"
                f"  源：{info.get('src_root', '')}\n"
                f"  目标：{info.get('dst_root', '')}\n"
                f"  开始于：{info.get('created_at', '')}\n"
                f"  已完成：{info.get('done_count', 0)} 个\n\n"
                "请等待其完成后再开始新任务。")
            return None
        except Exception as e:
            if self._headless_task is not None:
                self._hooks.on_fatal(config.SCHEDULE_EXIT_CANNOT_START,
                                    "[无头] 无法启动状态库任务：%s" % e)
                return None
            messagebox.showerror("错误", f"无法启动状态库任务：{e}")
            return None
        self._db_task_src_root = src_root
        return task_id


    # 【阶段2a-修改原因】启动本任务的 db-writer。每个任务一个线程、一个队列，
    # 任务结束时线程自行 return，不跨任务复用（避免终态消息投递到错的任务上）。
    # 【D-1 修改原因】源根改为**随线程参数传下去**，不再让 writer 线程去读
    # `self._db_task_src_root`：
    #   `_db_dispatch` 会在**任务名义结束之后**继续排空队列里剩下的条目（队列 FIFO，
    #   `__task_end__` 由 worker 最后入队）。若它读的是成员变量，正确性就依赖
    #   "新任务绝不能在旧 writer 排空前启动"这条隔着好几步的不变量（`:2638` 不变量 2）。
    #   传成参数之后，本任务的源根由**本线程自己持有整个生命周期**，那件事**结构上
    #   不可能发生**，不再依赖任何远端推理。
    #   （拷贝工作线程里的 `_should_skip` / `_audit_overwrite` **不在此列**：它们都在
    #   任务存续期内执行，仍读成员变量，见 `_claim_current_task`。）
    def _start_db_writer(self, task_id, src_root):
        if not config._STATE_DB_ENABLED:
            return
        self._db_queue = queue.Queue()
        self._db_writer_thread = threading.Thread(
            target=self._db_writer_loop, args=(task_id, src_root), daemon=True,
            name=f"db-writer-{task_id}")
        self._db_writer_thread.start()


    # 【阶段2a-修改原因】投递一条状态库消息。纯 queue.put，绝不阻塞、绝不碰 Tk。
    # 降级后丢弃普通消息，但 **终态消息必须放行**——否则 db-writer 永远等不到
    # __task_end__ 退出，它会一直空转刷心跳，任务级锁就永远解不开了。
    def _db_enqueue(self, item):
        if self._db_queue is None:
            return
        if self._state_db_degraded and item[0] != "__task_end__":
            return
        self._db_queue.put(item)


    # 【阶段2a-修改原因】把队列消息翻译成 TransferStateDB 调用。仅 db-writer 线程执行，
    # 因此这里做的 os.stat 不占传输关键路径（见 _stat_for_state 的说明）。
    # 【D-1】src_root 由 `_db_writer_loop` 作为**参数**传进来（不是读成员变量）——
    # 理由见 `_start_db_writer` 上方那段。
    def _db_dispatch(self, item, task_id, src_root):
        kind = item[0]
        if kind == "overwrite":
            # 【阶段2b】覆盖审计：filename 由调用方给出（_plan_target 可能给目标加过 _N 后缀，
            # 此处必须用**源**文件名，才能与 files 表的主键口径一致）
            # 【D-1】old_src_root 由**调用方**随消息带来（item[7]，跨源审计查出来的"被覆盖
            # 的那份属于谁"）；new_src_root 就是本任务的源根。用 len 判断而不是下标直取，
            # 是为了让还没带第 8 项的旧入队点仍然能跑（此时 old_src_root 记 NULL）。
            self._db.record_overwrite(
                item[1], item[2], item[3], item[4], item[5], item[6], task_id,
                old_src_root=(item[7] if len(item) > 7 else None),
                new_src_root=src_root)
            return
        if kind == "src_deleted":
            # 【阶段2b】move 模式：源已删除的标记。与 ("done", ...) 同队列同批提交，
            # 顺序由入队顺序保证（先 done 后 src_deleted）。
            # 【D-1】必须带上源根：只按 filename 会连**别的源**的同名行一起标记。
            # 【任务1】改成按全路径定位：`item[1]` 本来就是那个全路径字符串，
            # 不再取 basename —— 取 basename 正是"同源内同名互相误伤"的来源。
            self._db.mark_src_deleted(item[1])
            return
        src_path = item[1]
        target_path = item[2]
        # 【阶段2c-新增】FTP 侧的源属性由调用方以 **info 字典**显式附加：
        #   ("done",   src, dst,       info)      info = {"size","mtime","precision"[,"dst_size"]}
        #   ("failed", src, dst, err,  info)
        # 为什么不能像本地那样从目标回推：FTP 源的 mtime 精度是 's'/'min'，
        # 而本地目标的精度恒为 'ns'；回推会把 'ns' 写进 mtime_precision，
        # 于是续传判定 ④ 拿 FTP 侧的 's' 去比库里的 'ns' 永远不等 → 续传彻底失效。
        # 这些值直接来自 list_files_recursive 的五元组 / FTPHelper.size()，
        # 远端 stat 反正也拿不到别的。
        info = None
        if kind == "done" and len(item) >= 4 and isinstance(item[3], dict):
            info = item[3]
        elif kind == "failed" and len(item) >= 5 and isinstance(item[4], dict):
            info = item[4]
        if info is not None:
            size = info.get("size")
            mtime = info.get("mtime")
            precision = info.get("precision")
            dst_size = info.get("dst_size", size)     # 上传路径给的是 FTP 侧回读值
        else:
            # 本地源：由目标回推。成立的理由是"源与目标是同一类路径"——
            # copy2 / os.replace 都保留 mtime 与 size，且两侧精度同为 'ns'。
            # 这条不占传输关键路径（本函数只跑在 db-writer 线程里）。
            precision, size, mtime = config._stat_for_state(target_path)
            dst_size = size
        # 【D-1】两处都要带 src_root（= 本任务的源根）。它与 task_id 一样都是"标识"，
        # 漏传/传错不会报错，只会静默写歪。
        # 【任务1】`filename` 不再由这里传 —— 它现在纯粹是冗余列，由 state_db 从
        # src_path 推（`os.path.basename`），省掉一个"必须与 src_path 自洽"的形参。
        # 传下去的 `src_path` 与 `_should_skip` 收到的是**同一个局部变量**（见那里的说明）。
        if kind == "done":
            self._db.mark_done(src_path, src_root, task_id,
                               size, mtime,
                               precision, target_path, dst_size)
        elif kind == "failed":
            self._db.mark_failed(src_path, src_root, task_id,
                                 size, mtime,
                                 precision, target_path, 3, item[3])


    # 【阶段2a-修改原因】终态统一入口。所有 worker 的终态都改为走这里，
    # 由 db-writer 保证"记录先全部落库、UI 再恢复可用"（方案 §5.3）。
    # 回滚分支（_STATE_DB_ENABLED=False）没有 db-writer，退回直接投递。
    # 【阶段2a-补漏项】total_seen 由调用方通过 spec 显式传入，db-writer **不再读 self._total**。
    # 原因：self._total 只在两个 FTP worker 里赋值，本地遍历路径从不设，
    # 于是本地任务的 total_seen 恒为 0，且 FTP 早期返回分支会读到**上一个任务的残留值**。
    # 解耦后 self._total 保持原语义（只服务 FTP 进度条），阶段1 的进度条行为一行不变。
    def _db_end_task(self, ui_kind, end_reason, total_seen=0, **spec):
        spec["ui_kind"] = ui_kind
        spec["end_reason"] = end_reason
        spec.setdefault("failed_count", 0)
        spec["total_seen"] = total_seen
        self._ui._flush_log_buffer(force=True)   # 终态不能抢在缓冲日志前面（阶段1 的既有约束）
        if not config._STATE_DB_ENABLED:
            self._ui._post_ui("task_end", spec)
            return
        self._db_enqueue(("__task_end__", spec))


    # ---- 【D-1】多源串行的两个支点 ----
    def _has_next_source(self):
        """序列里还有没有下一个源。**空队列恒为 False** —— 单源模式走的就是这一条。"""
        return bool(self._src_queue) and self._src_index < len(self._src_queue)


    def _finish_one_source(self, spec, is_last):
        """本源跑完：记**一行**、累计、最后一个源再多记一行"全部完成"。

        【为什么不直接调 `_transfer_done_stream`】
        它 6 条分支里有 4 条会弹 `messagebox`，而多源串行下**每源一个模态框 =
        用户不点就不往下走** —— 串行驱动器等于被自己的完成提示卡死。给它加一个
        `silent=True` 参数要把那 4 条分支逐个改一遍，而它是全项目回归面最大的函数之一；
        这里只挑"不记就会丢数据"的那几项重写一遍，**逐条对应**它的同名分支。

        【只做记账，不做决定】"要不要接着跑"由调用方（`_dispatch_task_end`）判过了。
        但**"是不是最后一个"必须由调用方传进来**（`is_last`）：本函数跑的时候队列
        还没清，自己看队列是看不出"最后一个"的。

        ⚠️ 参数是 `is_last`（**事实**：本源的序号已到队列末尾），不是"驱动器还要不要
        往下跑"（**决定**）。第一版传的是后者，两者在正常路径上恒等，所以看不出来；
        是 P2 阳性对照（把驱动器的判据掐死成"永远没有下一个"）把它照出来的 ——
        那一次日志里赫然写着"全部完成：3 个源，共复制 2 个"，而**另外两个源根本没跑**。
        "最后一个源跑完了"和"没有下一个了"是两句话，前者才配得上"全部完成"。

        【日志格式（D-1 裁定①，方案 B —— 源边界聚合）】
          * 每源**开始**不记（序号前缀已能推出是第几个）；
          * 每源**完成**记 1 行 `[源 X/N] 完成：复制 A 个，跳过 B 个，失败 C 个`；
          * 最后一个源再多记 1 行 `全部完成：N 个源，共复制 X 个，跳过 Y 个，失败 Z 个`。
        5 个源：原来 10 行（每源"源：…"+"结束：…"两条）→ 现在 1+5+1 = **7 行**。
        方案 D（日志降级到文件）**被否**：那样用户在界面上看不出哪个源慢/失败。
        """
        failed_count = spec.get("failed_count", 0)
        failed = self._fetch_failed_details(failed_count)   # 必须在本任务 id 还生效时查
        copied = self._completed - self._skipped
        total = len(self._src_queue)

        # 跨源累计：`_completed`/`_skipped` 每个源都会被 `_launch_source` 重置，
        # 所以要另存一份才能给"全部完成"那一行。**失败数**同理（`failed_count` 是本任务的）。
        self._src_totals["copied"] += copied
        self._src_totals["skipped"] += self._skipped
        self._src_totals["failed"] += failed_count

        if failed and not is_last:
            # 失败清单照样逐源落盘、照样进 `_last_failed` —— 与单源时一字不差。
            # 后一个源跑起来会在 `_launch_source` 里把 `_last_failed` 清掉，
            # 所以"最后一次"的重传目标天然是**最后一个源**的失败文件，语义与今天一致。
            #
            # ⚠️ **`not is_last` 那一半是必须的**：最后一个源**不走这里** —— 它接着走
            # `_transfer_done_stream`，那个函数自己就会落盘、自己就会置 `_last_failed`
            # 与 retry 按钮。在这里再做一遍的后果是**存两份清单、打两行"已保存"**。
            log_path = self._save_failed_list(failed)
            self._last_failed = failed
            self._ui._update_log("[源 %d/%d] 失败清单已保存：%s"
                             % (self._src_index, total, log_path))
        elif not is_last:
            self._last_failed = []

        self._ui._update_log("[源 %d/%d] 完成：复制 %d 个，跳过 %d 个，失败 %d 个"
                         % (self._src_index, total, copied, self._skipped, failed_count))

        if is_last:
            t = self._src_totals
            self._ui._update_log("全部完成：%d 个源，共复制 %d 个，跳过 %d 个，失败 %d 个"
                             % (total, t["copied"], t["skipped"], t["failed"]))


    # 【阶段2a-修改原因】主线程：把终态信封还原成阶段 1 的终态调用。
    # 这里做的事只有一件——把"计数"换回"明细调用"，明细由被调函数自己查库（补充 A）。
    def _dispatch_task_end(self, spec):
        kind = spec.get("ui_kind")
        # 【阶段2d-修改原因】把 end_reason 一并转给终态函数（原本只转计数）：
        # "连续失败中止"与"用户点停止"都是 stopped=True，不给原因就分不出该弹哪句话。
        # failed_count 此刻已是 db-writer 用 count_failed_writer 查出的**本任务**失败数。
        reason = spec.get("end_reason")

        # 【P0-1】日期分层的收尾。**位置是本函数的第一件事**，两条理由：
        #   ① 它要在下面那条多源分支**之前**跑 —— 那一支会 `_start_next_source()`，
        #      也就是换上下一个源的剪枝器；晚一行就是"拿 B 源的同步点去推 A 源的键"。
        #   ② 判据与多源那条**逐字相同**：`reason == "finished" and not stopped`。
        #      "这一趟跑成了"只有一个定义，两处各写一份迟早对不上，
        #      而对不上的那一侧是**漏传**（见 `_date_layer_finish` 的注释）。
        # `_task_active` 这一项防的是**迟到/重复的终态**：单源模式跑到这里时它必为
        #   True（`_launch_source` 里置的），而重复终态到达时上一次的
        #   `_transfer_done_*` 已经把它关掉了。与多源分支用的是同一道闸。
        if self._task_active:
            self._date_layer_finish(reason == "finished" and not spec.get("stopped"))
            # 【P1】与剪枝器**同一道终态闸、同一行位置**：都必须在下面那条多源分支
            #   （它会 `_start_next_source()`，也就是换上下一个源）**之前**收掉。
            #   晚一行就是"B 源的 dict 已经建好、A 源的还活着" —— 两个 85 MB 同时占着。
            #   它不看 `reason`：**任何**终态都要放（停止、出错、连续失败中止都算），
            #   因为"记录还有没有用"与"这一趟成不成"无关 —— 下一趟要的是新记录。
            self._skip_states_release()

        # ---- 【D-1】多源串行：本源正常跑完、且后面还有源 → 接着跑，**不弹完成框** ----
        # 判据只有一条：`reason == "finished"`。**其余一律收尾整条序列**，逐条都有理由：
        #   * `stopped`（用户点了停止）→ §7.6 明写"要停掉整个序列"；
        #   * `consec_fail`（连续失败中止）→ 目标多半已不可写，下一个源只会再撞一次；
        #   * `error`（任务级异常）→ 同上，而且连弹 N 个错误框会把第一个的原文淹掉。
        # 挂在这里而不是挂在某个 `_transfer_done_*` 里面：这里是**终态的唯一网关**
        # （`_db_writer_loop` 的终态消息经 `_post_ui` 到 `_drain_ui_queue`，最后都落到这），
        # 挂在下面任何一个具体函数上都会漏掉另外几个。
        # `_task_active` 那一项是**防重复终态**的，不是防抖：终态可能重复到达
        # （`_dispatch_ui_message` 的兜底、先点停止再收到 done_stream），
        # 而 `_transfer_finished` 用 `_task_active` 吸收重复。这里要是绕过它，
        # 一条迟到的重复终态就会**再掀一个源** —— 同一个源跑两遍。
        # 真终态到达时 `_task_active` 必为 True（`_launch_source` 里置的）。
        # 【D-1 裁定①】`multi_last` = "这是序列的最后一个源，收尾照旧走下面那条正常路"。
        #   它决定下面要不要**压掉每个任务自己那一行总结日志** —— 最后一个源的
        #   `[源 N/N] 完成：…` 与 `全部完成：…` 已经把同一件事说完了，
        #   再打一行 `传输完成：复制 X 个…` 就是第三遍（5 个源会变成 8 行，不是 7 行）。
        multi_last = False
        # 【阶段四】"本源那一份有没有已经进过 `_src_totals`"。只有走
        #   `_finish_one_source` 那一支才会进；其余终结方式（被停止 / 出错 /
        #   单源模式）都还没进 —— 无头模式算退出码时要在下面补上。
        counted = False
        if (self._src_queue and self._task_active and reason == "finished"
                and not spec.get("stopped")):
            total = len(self._src_queue)
            is_last = self._src_index >= total     # 事实：本源已是队列末尾
            has_next = self._has_next_source()     # 决定：驱动器还要不要接着跑
            self._finish_one_source(spec, is_last)
            counted = True
            if has_next:
                # 复位本次任务的运行态（进度条 / 按钮 / 速度链 / `_task_active`）。
                # **必须在 `_start_next_source` 之前**：这个函数负责把 `_task_active`
                # 关掉 —— 终态幂等窗口不关，下一个源的终态会被当成"重复到达"而静默丢掉。
                self._transfer_finished()
                self._start_next_source()
                return
            # 正常路径上 `is_last` 与 `not has_next` 恒等，所以这一行和第一版的
            # `multi_last = True` 一模一样；只有驱动器被掐死（P2 对照）时才分岔 ——
            # 那一岔必须落到 False：序列没跑完，就不该压掉总结行冒充"跑完了"。
            multi_last = is_last
        # 【D-1】任何**没有**接着往下跑的终态，都在这里把序列清掉。
        # 这是"序列"唯一的收口，也顺带把上面那个判据变成结构性安全的：
        # 清掉之后，一条迟到的重复终态（`_dispatch_ui_message` 的兜底路径）
        # 不可能再掀起一个源 —— 它看到的是空队列。
        self._src_queue = []
        self._src_index = 0

        if kind == "done_stream":
            self._transfer_done_stream(
                spec.get("failed_count", 0), spec.get("folder_count", 0),
                spec.get("matched", 0), spec.get("stopped", False),
                end_reason=reason, log_summary=not multi_last)
        elif kind == "done":
            self._transfer_done(end_reason=reason, log_summary=not multi_last)
        elif kind == "retry_done":
            # 【阶段5-2-修改原因】把 end_reason 也转过去：连续失败中止在重传路径上
            # 同样是 stopped=True，不给原因就只能弹"已停止"——用户会以为是自己按的，
            # 以为剩下的文件只是"还没轮到"，再点一次重传，还是同样的结果。
            self._retry_failed_done(
                spec.get("failed_count", 0), spec.get("retried_ok", 0),
                spec.get("stopped", False), end_reason=reason)
        elif kind == "error":
            self._transfer_error(spec.get("msg", "未知错误"))
        else:
            self._transfer_finished()

        # ---- 【阶段四】无头模式的收口 ----
        # 【为什么挂在这里，而不是挂进 `_transfer_finished`】
        #   `_transfer_finished` 在**多源串行中途**也会被调（上面 `has_next` 那一支，
        #   为了把 `_task_active` 关掉好让下一个源的终态能被收下）。挂在那里的后果是
        #   **第 1 个源跑完就退出去了**，后面 2..N 个源一个都不跑，
        #   而日志上写着"成功" —— 这正是无头模式最不能出的那种错。
        #   而这一行是**整条序列唯一的收口**：上面 4404-4405 已经把队列清空了，
        #   能走到这里就说明"不会再有下一个源了"。
        #
        # 【为什么计数要在这里补而不是直接用 `_completed`】
        #   多源模式下 `_completed` / `_skipped` 每个源都会被 `_launch_source` 重置，
        #   序列跑完时它们只剩**最后一个源**的数 —— 拿它当总计会少报前面几个源。
        #   所以基准是 `_src_totals`（每源累加），只在"本源的还没进过"时补本源这一份。
        if self._headless_task is not None:
            if not counted:
                self._src_totals["copied"] += self._completed - self._skipped
                self._src_totals["skipped"] += self._skipped
                self._src_totals["failed"] += spec.get("failed_count", 0)
            t = self._src_totals
            self._hooks.on_task_end(spec, reason, t["copied"], t["skipped"], t["failed"],
                                  bool(spec.get("stopped")))


    # 【阶段2a-修改原因】失败明细的唯一取数口。回滚模式或降级时退回内存里的
    # _last_failed，保证关闭状态库后 UI 行为与阶段 1 完全一致。
    def _fetch_failed_details(self, failed_count):
        if not failed_count:
            return []
        if config._STATE_DB_ENABLED and not self._state_db_degraded and self._current_task_id is not None:
            try:
                return self._db.iter_failed(self._current_task_id)
            except Exception:
                pass
        return list(self._last_failed)


    # 【阶段2a-修改原因】状态库单写线程的消费循环（方案 §5.2）。
    # 三条不变量：
    #   1) get() 必须带 timeout —— 空转时（如 FTP 深度扫描）也要刷心跳，
    #      否则 30 秒后本任务会被其他窗口判为陈旧并抢占；
    #   2) 终态消息由本线程投递 —— 保证"记录落库"先于"UI 恢复可用"；
    #   3) 写库连续失败只降级、不中止 —— 没有任何一条 DB 写入失败会导致数据丢失。
    def _db_writer_loop(self, task_id, src_root):
        db = self._db
        opened = False
        try:
            db.open()
            opened = True
        except Exception as e:
            self._ui._threadsafe_log(f"[警告] 状态库打开失败，续传能力已失效：{e}")
            self._degrade_state_db()

        pending = 0
        last_commit = time.monotonic()
        consec_err = 0

        while True:
            # ---- 取消息：超时即空转分支 ----
            try:
                item = self._db_queue.get(timeout=config.DB_HEARTBEAT_INTERVAL_S)
            except queue.Empty:
                if opened and not self._state_db_degraded:
                    try:
                        db.heartbeat(task_id)
                        db.batch_commit()
                    except Exception as e:
                        self._ui._threadsafe_log(f"[警告] 状态库心跳失败：{e}")
                last_commit = time.monotonic()
                continue

            # ---- 终态分支 ----
            if item[0] == "__task_end__":
                spec = item[1]
                # 【阶段2c-修复】重传任务的计数与主动传输**分离**。
                # 原因：_start_transfer 会在开局把 _completed/_skipped 清零，_retry_failed
                # 不清零，于是重传任务收尾时读到的是**上一个任务**留下的终值——
                # done_count 会写成上一次的传输总数、skipped_count 会写成上一次的跳过数，
                # 与本次真正重传的文件数毫无关系（tasks 表里的统计因此完全不可信）。
                # 重传的口径本就不同：没有"扫描到的候选集"，只有"待重传的失败行"，
                # 所以 skipped 恒为 0、done 取 worker 自算的 retried_ok、total_seen 取失败行数。
                if spec.get("ui_kind") == "retry_done":
                    skipped = 0
                    done_count = spec.get("retried_ok", 0)
                else:
                    with self._lock:
                        completed = self._completed
                        skipped = self._skipped
                    done_count = max(0, completed - skipped)
                reason = spec.get("end_reason", "finished")
                if opened and not self._state_db_degraded:
                    try:
                        db.batch_commit()
                        # 必须用写连接计数：此刻事务刚提交，短连接能看到同样的数据，
                        # 但走同一条连接可以避免任何可见性歧义。
                        spec["failed_count"] = db.count_failed_writer(task_id)
                        db.finish_task(
                            task_id,
                            status=("finished" if reason == "finished" else "aborted"),
                            total_seen=spec.get("total_seen", 0),
                            done_count=done_count,
                            skipped_count=skipped,
                            failed_count=spec["failed_count"],
                            end_reason=reason)
                    except Exception as e:
                        self._ui._threadsafe_log(f"[警告] 状态库收尾写入失败：{e}")
                if opened:
                    db.close()
                # 终态前先冲掉日志缓冲，避免最后几条日志被终态消息抢先（阶段1 的既有约束）
                self._ui._flush_log_buffer(force=True)
                self._ui._post_ui("task_end", spec)
                return

            # ---- 正常记录分支 ----
            if self._state_db_degraded or not opened:
                continue
            try:
                self._db_dispatch(item, task_id, src_root)
                consec_err = 0
                pending += 1
            except Exception as e:
                consec_err += 1
                if consec_err == 1 or consec_err % 5 == 0:
                    self._ui._threadsafe_log(f"[警告] 状态库写入失败（第 {consec_err} 次）：{e}")
                if consec_err >= config.DB_MAX_CONSEC_ERRORS:
                    self._ui._threadsafe_log(
                        "[警告] 状态库连续写入失败，续传能力已失效，但传输继续进行")
                    self._degrade_state_db()

            # ---- 攒批提交 ----
            now = time.monotonic()
            if pending >= config.DB_BATCH_SIZE or (now - last_commit) >= config.DB_COMMIT_INTERVAL_S:
                try:
                    db.heartbeat(task_id)
                    db.batch_commit()
                except Exception as e:
                    self._ui._threadsafe_log(f"[警告] 状态库提交失败：{e}")
                last_commit = now
                pending = 0


    def _toggle_pause(self):
        if self._pause_event.is_set():
            self._pause_event.clear()
            self.pause_btn.configure(text="继续")
            self._ui._update_log("已暂停")
        else:
            self._pause_event.set()
            self.pause_btn.configure(text="暂停")
            self._ui._update_log("已继续")


    def _toggle_stop(self):
        self._stop_event.set()
        self._pause_event.set()   # 解除暂停阻塞，让 worker 在下一个文件边界停下
        # 【D-1】"停止"停的是**整条序列**，不是当前这一个源（§7.6 硬要求）。
        # 在**按下的这一刻**就清掉，而不是等终态到达再清：从按下到终态之间有
        # 最长约 45 秒（在飞文件 + 看门狗 + 退避，见下面那段注释），
        # 这段时间里队列要是还在，任何一条异常路径的终态都可能把下一个源掀起来
        # —— 用户按的是"停"，看到的是"又跑起来了"，那是最坏的一种反馈。
        self._src_queue = []
        self._src_index = 0
        self.stop_btn.configure(state="disabled")
        self._ui._update_log("正在停止，将在当前文件处理完后停止…")
        # 【体检-K5｜按拍板"保持现状，只改提示文案"】停止的语义是"已经在传的传完、
        # 剩下的不再开始"，所以按下去之后界面**本来就可能**还要等一会儿才收尾：
        # 在飞的那个文件若正卡在看门狗窗口里（数据连接不通），它要先被判死（30 秒），
        # 再按退避曲线重连重放（1+3+9=13 秒）才结束。合计四十来秒里按钮是灰的、
        # 界面像没反应——那不是卡死，是语义如此。不改成"立刻放弃在飞文件"（那会把
        # 半截 .part 和"记成什么终态"这两个新问题引进来），只把这段等待说出来。
        if self._task_active:
            self._ui._update_log("（在飞的文件会传完再收工；若链路正卡着，"
                             "最长约 45 秒——看门狗 30 秒判死 + 重连退避 13 秒）")


    # 【阶段2d-新增】连续失败中止的提示语（唯一出口，两个终态函数共用）。
    # 这不是"改 UI 设计"，是修功能缺陷：中止了却弹"完成"，用户会以为数据都过去了。
    # 控件、布局、按钮一律没动，改的只是同一控件按状态显示的文字。
    # N 取阈值 MAX_CONSEC_FAILURES 而**不是** spec 里的 failed_count：口号是"连续 N 个"，
    # 而 failed_count 是本任务累计失败数（= 连续失败数 + 更早那些夹着成功的失败），
    # 拿它填"连续 N 个"会把话说大。两个数在只有连续失败的场景下相等，有事例为证。
    @staticmethod
    def _consec_abort_note():
        return (f"连续失败 {config.MAX_CONSEC_FAILURES} 个文件，目标可能已不可写"
                f"（磁盘满 / 掉线 / 只读），已在第 {config.MAX_CONSEC_FAILURES} 个失败处中止。")


    def _transfer_done(self, end_reason=None, log_summary=True):
        # 【阶段2d】end_reason='consec_fail' 时改标题与正文（其余取值一字不动）
        # 【D-1 裁定①】`log_summary` 同 `_transfer_done_stream`：多源最后一个源压掉
        # 那句 `传输完成，共处理 N 个文件`（弹窗照旧）。默认 True ⇒ 单源一字不变。
        if end_reason == "consec_fail":
            self._ui._update_log(self._consec_abort_note())
            messagebox.showinfo(
                "已中止",
                f"{self._consec_abort_note()}\n\n"
                f"已处理 {self._completed} 个文件。已完成的都已落库，"
                f"重新开始会从断点续传。")
            self._transfer_finished()
            return
        if log_summary:          # 多源序列的最后一个源压掉这一句（弹窗照旧）
            self._ui._update_log(f"传输完成，共处理 {self._completed} 个文件")
        messagebox.showinfo("完成", f"传输完成，共处理 {self._completed} 个文件")
        self._transfer_finished()


    def _save_failed_list(self, failed):
        # PyInstaller 打包后 __file__ 指向临时目录，改写到 exe 所在目录，避免清单丢失
        if getattr(sys, 'frozen', False):
            base_dir = os.path.dirname(sys.executable)
        else:
            try:
                base_dir = os.path.dirname(os.path.abspath(__file__))
            except NameError:
                base_dir = os.getcwd()
        log_path = os.path.join(base_dir, f"transfer_failed_{datetime.datetime.now().strftime('%Y%m%d_%H%M%S')}.log")
        with open(log_path, 'w', encoding='utf-8') as f:
            for src_path, target_path, err in failed:
                f.write(f"{src_path}\t{target_path}\t{err}\n")
        return log_path


    # 【阶段2a-修改原因】签名从"传失败清单"改为"传失败数量"（补充 A）：
    # 几十万条失败记录不再经 UI 消息通道搬运，明细在主线程按需查库。
    # 其余展示逻辑、按钮置位、messagebox 文案一字未动。
    # 【阶段2d-修改原因】新增 end_reason 入参：stopped 这个分支同时承载"用户点停止"
    # 与"连续失败中止"，两者都置 _stop_event，只看 stopped 分不开。文案按 end_reason 分：
    # consec_fail 弹"已中止"并说明原因，其余取值与阶段1 逐字一致。
    # 【D-1 裁定①】`log_summary`：多源序列的**最后一个**源传 False，
    # 压掉那句 `传输完成：复制 X 个，跳过 Y 个（遍历 M 个文件夹）` —— 已由
    # `[源 N/N] 完成：…` + `全部完成：…` 说过。**只压这一句**（`else` 分支里那行）：
    # 其余分支（停止 / 有失败 / 无匹配）一个字不动 —— 那些是异常路径，
    # `失败清单已保存` 这类信息本来就没有别的地方会说。
    # 默认 True ⇒ 单源（唯一入参为默认值的调用方）行为与今天逐字相同。
    def _transfer_done_stream(self, failed_count, folder_count, matched, stopped=False,
                              end_reason=None, log_summary=True):
        failed = self._fetch_failed_details(failed_count)
        consec = (end_reason == "consec_fail")

        if stopped:
            self._ui._update_log(f"已停止：成功复制 {self._completed - self._skipped} 个，跳过 {self._skipped} 个，失败 {failed_count} 个（遍历 {folder_count} 个文件夹）")
            if failed:
                log_path = self._save_failed_list(failed)
                self._last_failed = failed
                self._ui._update_log(f"失败清单已保存: {log_path}")
                self.retry_btn.configure(state="normal")
            else:
                self._last_failed = []
                self.retry_btn.configure(state="disabled")
            if consec:
                self._ui._update_log(self._consec_abort_note())
                messagebox.showinfo(
                    "已中止",
                    f"{self._consec_abort_note()}\n\n"
                    f"已复制 {self._completed - self._skipped} 个，跳过 {self._skipped} 个，"
                    f"失败 {failed_count} 个（遍历 {folder_count} 个文件夹）。\n"
                    f"已完成的都已落库，重新开始会从断点续传。")
            else:
                messagebox.showinfo("已停止", f"传输已停止。\n已复制 {self._completed - self._skipped} 个，跳过 {self._skipped} 个，失败 {failed_count} 个。")
            self._transfer_finished()
            return
        if failed:
            log_path = self._save_failed_list(failed)
            self._last_failed = failed
            self._ui._update_log(f"传输结束：成功复制 {self._completed - self._skipped} 个，跳过 {self._skipped} 个，失败 {failed_count} 个（遍历 {folder_count} 个文件夹）")
            self._ui._update_log(f"失败清单已保存: {log_path}")
            self.retry_btn.configure(state="normal")
            messagebox.showwarning("部分完成",
                f"复制完成：成功 {self._completed - self._skipped} 个，跳过 {self._skipped} 个，失败 {failed_count} 个。\n"
                f"失败清单已保存到:\n{log_path}\n\n可点\"重传失败\"按钮重新尝试。")
        elif matched == 0:
            self._ui._update_log("没有符合过滤条件的文件")
            self._last_failed = []
            self.retry_btn.configure(state="disabled")
            messagebox.showinfo("提示", "没有符合过滤条件的文件")
        else:
            if log_summary:      # 多源序列的最后一个源压掉这一句（弹窗照旧）
                self._ui._update_log(f"传输完成：复制 {self._completed - self._skipped} 个，跳过 {self._skipped} 个（遍历 {folder_count} 个文件夹）")
            self._last_failed = []
            self.retry_btn.configure(state="disabled")
            messagebox.showinfo("完成", f"传输完成：复制 {self._completed - self._skipped} 个，跳过 {self._skipped} 个文件")
        self._transfer_finished()


    # 【阶段2a-修改原因】三条改动：
    #   1) 失败清单改由状态库提供（补充 A）——_last_failed 不再是权威来源；
    #   2) 重传自身也是一次任务，同样要 claim（Q6）+ 启 db-writer，
    #      否则重传的记录无处落库、终态无人投递；
    #   3) 判空改用 iter_failed 查库，避免依赖可能已过期的内存清单。
    # 【阶段1-修改原因】置 _task_active=True，与 _start_transfer 对称。
    # 重传也是一次完整的任务周期，同样的终态幂等窗口必须打开。
    def _retry_failed(self):
        if not config._STATE_DB_ENABLED:
            if not self._last_failed:
                return
        else:
            failed = self._db.iter_failed(self._current_task_id)
            if not failed:
                self.retry_btn.configure(state="disabled")
                return

        # 【阶段2b-修复】失败行挂在**上一个任务**的 id 下，而重传要 claim 一个**新** id。
        # 上面那次判空查的是旧 id（所以能过），worker 若照新 id 再查一次必然是空的：
        # 结果是重传静默空转、一个文件都不传，却回报"重传完成，0 个文件全部成功"，
        # 并把重传按钮置灰 —— 失败清单从此在界面上消失（数据还留在库里，用户已无法重传）。
        # 修法：把"失败行所属任务"显式传给 worker。逐行重传时 mark_done/mark_failed
        # 会把行自然改挂到新任务下，因此收尾时 count_failed_writer(新 id) 的口径自动正确。
        failed_task_id = self._current_task_id
        task_id = self._claim_current_task(self._task_extensions)
        if task_id is None:
            return
        self._current_task_id = task_id
        self._start_db_writer(task_id, self._db_task_src_root)
        # 【P1】重传**不经过 `_launch_source`**（它是另一条启动路径），所以这里显式
        #   清空预取快照：宁可不预取（逐文件查库，逐字节回到 P1 之前），
        #   也**不许**拿着上一个源的记录去判重 —— 那是"用 A 的记录判 B 的文件"，
        #   命中的格子会**跳过**，而正确性完全靠巧合。
        #   （正常路径走到这里时它本来就是 None；这一行是防"有人将来改了释放点"。）
        self._skip_states_release()

        self._ui.progress.configure(mode="indeterminate", maximum=100, value=0)
        self._ui.progress.start(50)
        self._stop_event.clear()
        # 【阶段2d】与 _start_transfer 同理：重传不经过 _start_transfer，中止状态得自己清，
        # 否则上一次任务的 consec_fail 会被写进这次重传的终态（end_reason 张冠李戴）。
        self._consec_fail = 0
        self._stop_reason = None
        self._task_active = True            # 【阶段1】开启重传任务的终态幂等窗口
        # 【阶段二】重传**不经过 `_start_transfer`**，所以那三个快照得在这里再取一次。
        #   与那边同一条理由（worker 不许读 Tk 变量），也同一条规矩：重传用的必须是
        #   **点「重传失败」这一刻**设置页里的值，而不是上次任务开跑时的那一套。
        self._task_retry_attempts = int(self.retry_count.get()) + 1
        self._task_reconnect_attempts = int(self.reconnect_count.get()) + 1
        self._task_ftp_timeout = int(self.ftp_timeout.get())
        self._hooks.on_purge_state_changed()
        self.start_btn.configure(state="disabled")
        self.pause_btn.configure(state="disabled")
        self.stop_btn.configure(state="normal")
        self.retry_btn.configure(state="disabled")
        mode = self.transfer_mode.get()   # Tk 变量在主线程读取
        threading.Thread(target=self._retry_failed_worker,
                         args=(mode, task_id, failed_task_id), daemon=True).start()


    # 【阶段2a-修改原因】失败清单改从状态库取（补充 A），不再从 self._last_failed 复制。
    # 成功/失败各加一次埋点，使 _retry_failed_done 能用 count_failed_writer 算出
    # 与内存计数一致的 still_failed 数量。
    # 终态改 _db_end_task；停止时的 retried_ok / stopped 随终态信封传回。
    # 【阶段1-修改原因】2 处终态改为 flush + _post_ui；
    # 并新增一个 except Exception 兜底：原代码只捕 DiskFullError，
    # 其他异常（如 _copy_with_retry 之外的 OSError）会让线程静默死掉，
    # 界面永远停在"传输中"、按钮永远 disabled。属可选加固，删除不影响正确性。
    # 【阶段2b-修改原因】新增 failed_task_id：待重传的失败行属于**发起重传的那个任务**，
    # 而不是本次重传 claim 到的新 id（见 _retry_failed 里的说明）。缺省 None 时退回 task_id，
    # 与旧行为一致，避免影响任何按位置传参的调用方。
    def _retry_failed_worker(self, mode, task_id, failed_task_id=None):
        try:
            src_task = task_id if failed_task_id is None else failed_task_id
            failed = self._db.iter_failed(src_task) if config._STATE_DB_ENABLED else list(self._last_failed)
            # 【阶段2c-已修】本重传实现只认**本地**路径：它直接拿 files 表里的
            # src_path/target_path 喂给 shutil.copy2/move，而 FTP 任务的 src_path（下载）
            # 或 target_path（上传）是**远端路径**，本地 stat 不到。修复前的后果：
            #   * FTP→本地：逐行本地 stat 失败，用户看到一堆无意义的报错；
            #   * 本地→FTP：copy 会在本地靶盘上凭空造一个同名文件（数据污染），
            #     move 还会把本地源挪到那个怪路径上。
            # 修法：用 _is_remote_path 识别并**明确拒绝**，一行提示代替一行乱象。
            # 走本地通道走不通，走 FTP 通道要新增分支/改表结构，两者都超出本阶段范围——
            # 而"拒绝并说清楚"已经拿到了全部收益：数据不被污染，用户知道该重新发起传输。
            transfer_func = shutil.copy2 if mode == "copy" else shutil.move
            retried_ok = 0
            remote_skipped = 0
            for src_path, target_path, _ in failed:
                if self._stop_event.is_set():
                    break
                self._pause_event.wait()
                if self._stop_event.is_set():
                    break
                if config._is_remote_path(src_path) or config._is_remote_path(target_path):
                    # 【阶段2c】必须**照旧投一条 failed 埋点**，不能只是跳过：
                    # mark_failed 是 UPSERT 且会把 task_id 改写到本次重传任务下，
                    # 这样 count_failed_writer(新 id) 才数得到它、界面才会如实显示
                    # "仍失败 N 个"并保留重传按钮。只写日志不入库的话，这批行会
                    # 停留在**旧任务 id** 下 → 收尾时数出 0 条失败 → 界面谎报
                    # "全部成功"并把重传按钮置灰 —— 正是 2b 修过的那类谎报。
                    remote_skipped += 1
                    msg = "远端路径重传未支持，请重新发起传输"
                    self._ui._threadsafe_log(f"[跳过] {msg}: {src_path} -> {target_path}")
                    self._db_enqueue(("failed", src_path, target_path, msg))
                    continue
                ok, err = self._copy_with_retry(transfer_func, src_path, target_path, mode)
                if ok:
                    retried_ok += 1
                    self._note_transfer_result(True)      # 【阶段5-2】成功即清零连续失败计数
                    self._db_enqueue(("done", src_path, target_path))          # 【阶段2a】成功埋点
                    if mode == "move":
                        # 【阶段2b】同上：重传成功也是"源已删"，铁律二一视同仁
                        self._db_enqueue(("src_deleted", src_path))
                else:
                    self._ui._threadsafe_log(f"重传失败: {src_path} - {err}")
                    self._db_enqueue(("failed", src_path, target_path, err))   # 【阶段2a】失败埋点
                    # 【阶段5-2-新增】重传路径原本**没有**连续失败护栏（Q1 第 2 项）。
                    # 那些失败行是上一个任务留下来的，目标盘此刻若已经掉线/写满/只读，
                    # 一百行会一行一行试过去：每行都要走完 4 次重试 + 退避（起手十几秒），
                    # 用户干等十几分钟，最后拿到的还是同一句话——时间全耗在已知不可能成功的事上。
                    # 判据与主传输路径**共用同一个计数器与同一个阈值**（Q8 的口径，不另立一套）：
                    # 中间成功一次即清零，所以"散落的坏文件"不会被误中止，
                    # 被挡住的只会是"目标整体不可写"这一类持续故障。
                    # 上面那条 remote_skipped 分支不计入：它压根没发起传输，不算失败。
                    if self._note_transfer_result(False):
                        self._abort_on_consec_failures()
                        break
            if remote_skipped:
                self._ui._threadsafe_log(
                    f"其中 {remote_skipped} 个属于 FTP 任务（远端路径），"
                    f"重传不支持，请对它们重新发起一次传输")
            self._db_end_task(
                "retry_done",
                self._end_reason(),
                # 【阶段2c-修复】total_seen 显式给"本次待重传的失败行数"（用户拍板二选一，
                # 取 len(failed_list) 而非 0）：这样 tasks 表能读出"这次重传面对多少行"，
                # 与 done_count=retried_ok 对得上，事后追查不用靠猜。
                total_seen=len(failed),
                retried_ok=retried_ok, stopped=self._stop_event.is_set())
        except DiskFullError:
            self._db_end_task("error", "disk_full", msg="目标磁盘空间不足，重传中止")
        except Exception as e:
            self._db_end_task("error", "error", msg=str(e))


    # 【阶段2a-修改原因】签名改为接收 still_failed_count（补充 A），
    # 明细同样通过 _fetch_failed_details 查库。按钮置位与文案逻辑一字未动。
    def _retry_failed_done(self, still_failed_count, retried_ok, stopped=False,
                           end_reason=None):
        still_failed = self._fetch_failed_details(still_failed_count)
        self._last_failed = still_failed
        # 【阶段5-2-新增】连续失败中止：与 _transfer_done 同一口径、同一句提示语
        # （_consec_abort_note 是两个终态函数共用的唯一出口）。
        # 为什么必须与"用户点了停止"分开：两者都是 stopped=True，但一个是故障、
        # 一个是人工；中止了却弹"已停止"，用户会以为是自己按的、以为剩下的只是没轮到，
        # 于是再点一次重传 —— 结果一样，而且又耗掉十几分钟。
        # 失败清单原样保留、重传按钮保持可用：目标修好之后这一批还能接着重传。
        if end_reason == "consec_fail":
            self._ui._update_log(self._consec_abort_note())
            self._ui._update_log(f"重传中止：成功 {retried_ok} 个，仍失败 {still_failed_count} 个")
            if still_failed_count:
                self.retry_btn.configure(state="normal")
            else:
                self.retry_btn.configure(state="disabled")
            messagebox.showinfo(
                "已中止",
                f"{self._consec_abort_note()}\n\n"
                f"重传：成功 {retried_ok} 个，仍失败 {still_failed_count} 个。\n"
                f"失败清单仍保留在库里，目标恢复后可以直接再点「重传失败」。")
            self._transfer_finished()
            return
        if stopped:
            self._ui._update_log(f"重传已停止：成功 {retried_ok} 个，仍失败 {still_failed_count} 个")
            if still_failed_count:
                self.retry_btn.configure(state="normal")
            else:
                self.retry_btn.configure(state="disabled")
            messagebox.showinfo("已停止", f"重传已停止：成功 {retried_ok} 个，仍失败 {still_failed_count} 个。")
            self._transfer_finished()
            return
        if still_failed_count:
            log_path = self._save_failed_list(still_failed)
            self._ui._update_log(f"重传结束：成功 {retried_ok} 个，仍失败 {still_failed_count} 个，清单已更新: {log_path}")
            self.retry_btn.configure(state="normal")
            messagebox.showwarning("部分完成",
                f"重传：成功 {retried_ok} 个，仍失败 {still_failed_count} 个。\n更新后的失败清单:\n{log_path}")
        else:
            self._ui._update_log(f"重传完成，{retried_ok} 个文件全部成功")
            self.retry_btn.configure(state="disabled")
            messagebox.showinfo("完成", f"重传完成，{retried_ok} 个文件全部成功")
        self._transfer_finished()


    def _transfer_error(self, msg):
        self._ui._update_log(f"错误：{msg}")
        messagebox.showerror("错误", f"传输失败：{msg}")
        self._transfer_finished()


    # 【阶段1-修改原因】增加 _task_active 幂等守卫。
    # 重复到达的路径有三条：_dispatch_ui_message 对终态的兜底、
    # 用户先点"停止"再收到 done_stream、以及阶段2 起 db-writer 与旧路径并存。
    # 没有守卫时第二次会让刚复位的进度条再 stop 一次、按钮状态被覆盖。
    # _progress_timer_id 判空统一为 is not None，与 _start_progress_poll 一致。
    def _transfer_finished(self):
        if not self._task_active:
            return
        self._task_active = False
        self._ui._cancel_timers()      # 【第 1 步】进度 + 速度两条 after 链（顺序不变）
        # 【功能B】速度链收口。**必须排在 `_task_active = False` 之后**：
        # `_poll_speed` 的自终止条件读的就是 _task_active。顺序反过来（先 cancel 再置
        # False）的话，万一此刻恰好有一条已经排队的 _poll_speed 回调在跑，它会看到
        # _task_active 仍为 True 而**再给自己排一条** —— 刚 cancel 掉的链又活了。
        # 先置 False 则两道保险同向：cancel 掉了就没了，万一 cancel 漏了它也自己停。
        # 【第 1 步】速度链的 cancel 已并入上面的 `self._ui._cancel_timers()`（顺序不变）。
        # 【功能B-修复】终态**必须补一次取走**，否则最后不到 500ms 的那批字节
        #   会永远留在 `_speed_pending_bytes` 里 —— 没人再取它了：定时器刚被 cancel，
        #   而 `_refresh_speed` 除定时器外没有别的调用方。
        #   是冒烟脚本 B3/B4 把它抓出来的：2000 个文件真传完，计数器里只有 1942 个、
        #   1942000 字节，差额恰好是"最后一个 500ms 刻度到任务结束"之间搬的那批。
        #   （单元验收脚本抓不到它 —— 那边是我自己往窗口里塞样本，从来不经过累加器
        #     这一环。"验收管算得对不对，冒烟管接没接上"，这一条是后者的活。）
        #   位置：在 `_set_speed_final_text()` **之前**。`_refresh_speed` 此刻读到
        #   `_task_active` 已经是 False，会把标签写成空串，紧接着被终态文字盖掉；
        #   反过来的话这一拍写的空串会**盖掉刚写好的"完成"**。
        self._ui._refresh_speed()
        self._ui._set_speed_final_text()
        self._ui._reset_progress_bar()      # 【第 1 步】进度条复位 + 清跑马灯标记
        # 【阶段4】终态把进度条复位成精确模式，跑马灯标记必须跟着清掉——
        # 留着 True 会让下一次任务的 _leave_marquee 以为"还在跑马灯"而多 stop 一次
        # （无害），真正的问题是反过来：本次任务停在跑马灯上结束（扫描都没跑完就
        # 被停止），标记若不清，下一次任务开局又会以为自己不在跑马灯。
        # 【第 1 步】`_progress_marquee = False` 已并入 `self._ui._reset_progress_bar()`。
        self._stop_event.clear()
        self._pause_event.set()
        self.start_btn.configure(state="normal")
        self.pause_btn.configure(state="disabled", text="暂停")
        self.stop_btn.configure(state="disabled")
        # 【阶段二】任务终态把「清空状态库」放回可用。**排在 `_task_active = False`
        #   之后是硬要求**（本函数更上面那一行），否则这一句读到的还是 True，
        #   按钮就永远亮不回来了 —— 症状是"传过一次之后再也清不了库"。
        self._hooks.on_purge_state_changed()


class _Hooks(object):
    """`hooks` 的**形状说明**（只读，用来给 IDE / 人看；真正的实现在宿主那一侧）。

    5 个回调，一个都不许少：`TransferCore.__init__` 要求它是必填参数，
    并且**逐个断言 callable** —— 漏挂一个就在那里当场炸，不会静默。
    """

    def on_fatal(self, code, msg):              # ← 旧 `_headless_bail(code, msg)`
        raise NotImplementedError

    def on_task_end(self, spec, reason, copied, skipped, failed, stopped):
        raise NotImplementedError               # ← 旧 `_headless_finish(...)`（**6 个实参**）

    def on_ux_changed(self):                    # ← 旧 `config.save_ux_state(self._collect_ux_state())`
        raise NotImplementedError

    def on_purge_state_changed(self):           # ← 旧 `_sync_purge_btn()`
        raise NotImplementedError

    def on_path_problem(self, text):            # ← 旧 `_check_local_path` 里那三处 messagebox
        raise NotImplementedError
