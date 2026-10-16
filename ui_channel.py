# -*- coding: utf-8 -*-
r"""UI 通道：跨线程消息泵 / 进度 / 实时速度与剩余时间 / 日志与导出。

【它是怎么来的】2026-09-30 **第 1 步**：从 `file_transfer.py` 的
`FileTransferApp`（当时 7530 行的上帝对象）里**整块搬出来**的第一个模块。
分层位置是 **L1**（只比 `config` / `state_db` / `ftp_client` / `ui_dialogs` 高）：
`transfer_core` / `ftp_transfer` / 其余全部依赖它，而它**不 import 任何上层**
（唯一那条反向边走 `_ui_handlers["terminal_fallback"]` 这张回调表）。

【搬运铁律】本文件的 27 个方法、2 个模块级纯函数**逐字搬自原文件**，
只允许一种改动：`self.<别人的状态>` → `self.<模块>.<名字>`。
"没改"由 `_verify_split_ast.py` 折叠比对来证明（折叠掉 `self.<模块>.` 这一层后
必须与参照版逐节点相同），不是靠人看。

【新加的只有 8 个薄封装】它们的每一行都是从宿主某个方法的内部**原样抬出来**的
（抬的原因：不然宿主就得直接写 `self._ui._progress_timer_id = None` 这种
跨模块写私有属性，那正是乙要消除的"纠缠"）。八个也是 `_verify_split_ast.py`
登记在案的允许差异，逐条写了理由。
"""
import os                              # `_open_log_sink` / `_export_log` / `_pick_log_export_dir` 用
import queue
import threading
import time
import tkinter.filedialog as filedialog
import tkinter.messagebox as messagebox

import config
# ==================== 阶段二：日志级别（常规档的行过滤） ====================
# 【为什么是"按前缀丢行"，而不是"生产端少产生"】
#   生产端动的是 **worker 线程**：`_threadsafe_log` 的聚合缓冲、`_post_ui` 的投递、
#   以及十几处"已扫描到 …"的调用点。在那里加判断意味着每个调用点都要先问一句
#   "现在是什么级别" —— 而级别住在 Tk 变量里，**worker 线程不许读 Tk 变量**
#   （这条是整个项目最硬的一条线程规矩）。要绕开就得再加一层"主线程快照 + 锁"，
#   为一个纯显示问题引入跨线程状态，是拿结构换省事。
#   在**消费端**丢行则什么线程问题都没有：`_update_log` 本来就在主线程，
#   Tk 变量随便读；而且丢在最外层，聚合后的多行文本也能逐行处理。
# 【代价说清楚】常规档省的是**界面**，不是 CPU：那几行照样被生产、照样走队列、
#   照样进聚合缓冲，只是不 insert 进 Text。这是明知且接受的一笔 ——
#   换来的是"日志级别"这个功能不碰任何一条线程边界。
def _is_routine_log(line):
    """这一行是不是"常规档要滤掉的那三类"之一。"""
    return line.startswith(config.ROUTINE_LOG_PREFIXES)



def _filter_log(msg):
    """按当前级别过滤一段（可能多行的）日志。返回过滤后的文本，全被滤掉时返回 ""。

    `config.ROUTINE_LOG_PREFIXES` 是那三类的**唯一定义处**（扫描心跳 / 回收 / 库维护），
    本函数只负责比对，不负责决定"哪三类" —— 定义与判断分家，是为了让
    "常规档到底滤了什么"这个问题有一个能一眼看完的答案。
    """
    kept = [ln for ln in msg.split("\n") if not _is_routine_log(ln)]
    return "\n".join(kept)



_DEBUG_UI_THREAD_ASSERT = True  # 【常驻】永不移除：线程断言是本改造的安全网，出现回归时当场暴露


class UIChannel:
    """跨线程 UI 通道：消息泵 / 进度 / 速度与剩余时间 / 日志与导出。

    【它自己管自己的状态】24 个字段（日志缓冲、速度滑窗、定时器 id、
    进度条标记…），宿主与其它模块都不再持有副本。
    【按引用注入的 5 个】root / _pause_event / log_level / log_export_dir /
    _scan_dirs —— 它们天生只有一份，注入的是**同一个对象**（由
    `_verify_shared_instance.py` 的运行时身份判据钉住）。
    【只读的 1 个】core：只读 _completed / _total / _task_active。
    【挂进来的 3 个控件】progress / log_text / speed_label。
    """
    def __init__(self, core, root, pause_event, log_level, log_export_dir,
                 scan_dirs, handlers):
        """构造。**注入的引用必须是宿主那一个实例**，不许各自 new 一份。

        `core` 只用来读三个数（`_completed` / `_total` / `_task_active`）：
        第 2 步 `transfer_core` 拆出来之后，这里会换成真正的统计对象，
        现在传的是宿主替身。**读、不写**。
        """
        self._core = core                     # 只读：_completed / _total / _task_active
        self.root = root                      # 注入：同一个 Tk 根
        self._pause_event = pause_event        # 注入：同一个暂停事件（宿主唯一实例）
        self.log_level = log_level             # 注入：同一个 Tk 变量
        self.log_export_dir = log_export_dir   # 注入：同一个 Tk 变量
        self._scan_dirs = scan_dirs            # 注入：同一个"一格列表"（扫描器握着它）

        # 控件由建造方挂进来（`attach_widgets`）—— 建集合之前先占位，
        # 免得"还没建到那一格就被读到"变成 AttributeError。
        self.progress = None
        self.log_text = None
        self.speed_label = None

        # ---- 阶段1：跨线程 UI 通道 ----
        # 队列刻意无界：有界队列在消费者卡顿时会阻塞 worker，把背压传导到传输本身，
        # 比内存风险严重得多；积压由 _drain_ui_queue 的 qsize() 告警兜住。
        self._ui_queue = queue.Queue()
        self._ui_draining = False               # drain 重入保护（messagebox 会开嵌套事件循环）
        self._log_buf = []                      # 日志聚合缓冲
        self._log_lock = threading.Lock()       # 只保护 _log_buf / _log_last_flush，与 _lock 严格分离
        self._log_last_flush = time.monotonic()
        self._log_line_count = 1                # 日志区当前行数（空 Text 也占 1 行）
        self._log_truncated = False

        # 【阶段4】进度条当前是不是跑马灯（总数未知）。只在主线程读写：
        # 由 _set_progress_marquee 置位，由 _leave_marquee / 终态清除。
        self._progress_timer_id = None
        self._progress_marquee = False

        # 【功能B】实时速度 / ETA 的状态。分两类，**归属线程不同**：
        #   ① worker 侧写入、主线程取走（一把锁护着）：
        #        _speed_pending_bytes / _speed_pending_files
        #   ② 主线程独占（worker 一律不碰）：
        #        _speed_window / _speed_ema / _speed_timer_id /
        #        _speed_task_bytes / _speed_task_files / _speed_task_start
        #
        # 【为什么速度是按字节算的，不是按文件数】
        #   按文件数加权在 DICOM 场景下是**错的**：一个 4KB 的定位像和一个 200MB 的
        #   序列重建各算"一个文件"，同一块盘、同一个网络，报出来的 MB/s 能差几十倍。
        #   文件大小分布一变，速度就跟着跳 —— 那个数字就不代表任何东西了。
        #
        # 【为什么是"worker 累加 + 主线程定时取走"，而不是每传完一个文件投一条 UI 消息】
        #   这是对需求里"每次成功投递 _post_ui"那句写法的**有意偏离**，理由是本项目
        #   自己已经付过学费的那条纪律 —— config.py 里 FTP_PROGRESS_MAX_MSGS 上面写着：
        #     "worker 每完成一个文件都投递会让 20 万文件产生 20 万条 UI 消息，
        #      把消息泵和内存都压在无谓的进度刷新上"。
        #   本功能既然已经按 500ms 刷新界面，字节计数就只需要"主线程来取的时候是最新的"，
        #   一把锁护住两个整数就够了，**一条队列消息都不用发**。
        #   ⚠️ 对外可观察的行为（严格按字节加权 / 500ms 刷新 / 跳过不计）一条没变。
        self._speed_lock = threading.Lock()
        self._speed_pending_bytes = 0
        self._speed_pending_files = 0
        # 滑窗。**用 list 不用 deque**：每 500ms 至多追加一条，10 秒窗口里
        # 最多 20 条，从头部删一个元素的开销可以忽略，不值得为它多引一个模块。
        self._speed_window = []                 # [(monotonic, bytes), ...]
        self._speed_ema = None                  # None = 还没算过（第一条直接用瞬时值）
        self._speed_timer_id = None
        self._speed_task_bytes = 0              # 本任务累计已传字节（算 ETA 的分子）
        self._speed_task_files = 0              # 其中"真的搬了字节"的文件数（算平均大小）
        self._speed_task_start = None
        self._speed_last_text = None            # 挡住无变化的 configure

        # 【阶段一 Bug2/Bug3】本**源**是否已经有文件真的开始搬了。这是"速度该不该上屏"
        #   的判据 —— 原先用的是跑马灯，而跑马灯要等扫描**结束**才关，流式之后那等于
        #   把速度压制整整 10~30 分钟（Bug3）。由 worker/生产者线程写、主线程在 500ms
        #   心跳里读，与 _total 同一套约定：不加锁。按**源**归零。
        self._transfer_started = False

        # 【阶段四】日志文件 sink。**失败不抛**（见 `_open_log_sink`）。
        self._log_sink = None                   # 日志文件句柄；None = 不落文件（GUI 模式）
        self._log_sink_lock = threading.Lock()
        self._log_sink_path = ""

        # ---- 消息表：本类自己的 5 个 kind + 各模块注册进来的 + 终态兜底 ----
        # `_update_log` 依赖 `log_text`（由 `attach_widgets` 挂进来），所以这张表
        # 在**构造末尾**建 —— 与原先"必须在 _build_ui() 之后构建"同一条理由。
        self._ui_handlers = {
            "log": self._update_log,
            "progress_range": self._set_progress_range,
            "progress_start": self._start_progress_poll,
            "progress_marquee": self._set_progress_marquee,   # 【阶段4】总数未知
            "progress": self._on_progress_msg,          # 阶段3启用
        }
        for kind, fn in handlers.items():
            if kind in self._ui_handlers:
                raise ValueError("消息 kind 重复登记：%s" % kind)
            self._ui_handlers[kind] = fn

    # ==================== 功能A：导出日志 ====================

    # 【导出的是**日志区当前内容**，不是完整历史 —— 这一点必须在提示里说清楚】
    #   界面按 config.MAX_LOG_LINES 滚行（见 _update_log），被删掉的行**内存里也没有了**。
    #   所以本方法能给的只有"屏幕上这些"。用 `_log_truncated` 判断这次有没有发生过截断，
    #   发生过就补一句提示 —— 不说的话，导出文件看着像"日志本来就这么长"。
    #
    # 【为什么不做"完整日志落盘"（另写一份到磁盘）】
    #   那要动 `_update_log` / `_threadsafe_log` 这条 **UI 热路径**（每次写日志多一次 I/O），
    #   与本次"最小风险"的定位冲突。真要做，应当是一个**独立的**决定，
    #   连同"日志文件放哪、多大、怎么轮转"一起定。
    # 【阶段二】设置页的「浏览…」。只写 Tk 变量，**不落盘** ——
    #   落盘统一走关闭窗口时的 `_collect_ux_state`（与路径/模式/线程数同一条路）。
    #   这样"设置里有几项、什么时候写盘"只有一个答案，不用逐项记。
    def _pick_log_export_dir(self):
        path = filedialog.askdirectory(title="选择日志默认导出目录")
        if path:
            self.log_export_dir.set(os.path.normpath(path))

    def _export_log(self):
        body = self.log_text.get("1.0", "end-1c")
        if not body.strip():
            # 空日志不是"操作失败"，不写文件、也不报错
            messagebox.showinfo("导出日志", "日志区是空的，没有可导出的内容。")
            return
        # 【阶段二】起始目录由设置页的「默认导出目录」决定。空串 = 不传 `initialdir`，
        #   弹框回到系统默认位置 —— 也就是本行改动之前的行为，所以"没设过"的用户
        #   看到的东西一个像素都没变。
        #   ⚠️ `initialdir` 指向一个**不存在的目录**时，Tk 会静默退回系统默认位置
        #      （实测不报错），所以这里不需要预先判断目录是否存在 —— 判断了反而多一条
        #      只有"目录被删掉"时才走到的分支。
        _export_dir = self.log_export_dir.get().strip()
        path = filedialog.asksaveasfilename(
            title="导出日志",
            defaultextension=".txt",
            initialfile="transfer_log_%s.txt" % time.strftime("%Y%m%d_%H%M%S"),
            initialdir=_export_dir or None,
            filetypes=[("文本文件", "*.txt"), ("所有文件", "*.*")])
        if not path:
            return                      # 用户取消：正常操作，不提示
        try:
            with open(path, "w", encoding="utf-8") as f:
                f.write(body)
                if not body.endswith("\n"):
                    f.write("\n")       # 补一个收尾换行，纯为可读性，不改内容
        except Exception as e:          # 这里**必须**报出来：用户以为存好了才是问题
            messagebox.showerror("导出日志", "写入失败：\n%s" % e)
            return
        tip = "日志已导出到：\n%s" % path
        if self._log_truncated:
            tip += ("\n\n（提示：界面日志区上限 %d 行，更早的行已被丢弃，不在本文件里。）"
                    % config.MAX_LOG_LINES)
        messagebox.showinfo("导出日志", tip)

    # ==================== 阶段1：跨线程 UI 通道 ====================

    # 【阶段1-修改原因】worker → 主线程的唯一出口。纯 queue.put，不阻塞、不碰 Tk。
    # 回滚分支用 root.after 直接调度 handler——但注意只有在 _UI_QUEUE_ENABLED=False 时
    # 才是合法的：此时没有队列，也没有任何跨线程状态的中间层。
    def _post_ui(self, kind, *payload):
        if not config._UI_QUEUE_ENABLED:
            handler = self._ui_handlers.get(kind)
            if handler is not None:
                self.root.after(0, handler, *payload)
            return
        self._ui_queue.put((kind, payload))

    # 【阶段1-修改原因】主线程侧的消息泵。三个必须存在的保护：
    #   1) _ui_draining 重入保护——messagebox 会启动嵌套 Tk 事件循环，
    #      期间已排队的 after 回调仍会触发，导致本函数重入；
    #   2) 开头做一次非强制日志 flush——worker 可能长时间凑不满一批
    #      （如 FTP 深度扫描），光靠条数阈值会让日志卡住好几分钟；
    #   3) try/finally 强制重新调度——任何一条消息处理出错都不能让消息泵停摆。
    # 重入分支直接 return、不自行调度：调度权唯一归外层 finally。
    # 否则弹窗期间每次重入都挂一条 after 链，关闭弹窗后会有多条链并行。
    # 安全性：_ui_draining = True 与 try: 之间无可抛出语句，外层 finally 必然执行。
    def _drain_ui_queue(self):
        if _DEBUG_UI_THREAD_ASSERT:
            assert threading.current_thread() is threading.main_thread(), \
                "_drain_ui_queue 必须在主线程调用"
        if self._ui_draining:
            return          # 让外层的 finally 负责重新调度
        self._ui_draining = True
        try:
            self._flush_log_buffer()

            size = self._ui_queue.qsize()
            if size >= config.UI_QUEUE_WARN_SIZE:
                self._update_log(f"[警告] UI 消息积压 {size} 条，界面可能短暂无响应")

            processed = 0
            while processed < config.UI_DRAIN_MAX_BATCH:
                try:
                    kind, payload = self._ui_queue.get_nowait()
                except queue.Empty:
                    break
                self._dispatch_ui_message(kind, payload)
                processed += 1
        finally:
            self._ui_draining = False
            if not self._ui_queue.empty():
                # 仍有积压：缩短间隔尽快排空；不递归调用，以免饿死 Tk 自身的事件循环
                self.root.after(1, self._drain_ui_queue)
            else:
                self.root.after(config.UI_DRAIN_INTERVAL_MS, self._drain_ui_queue)

    # 【阶段1-修改原因】逐消息分发。每个 kind 独立 try/except：
    # 一条消息处理失败（比如终态里引用了已销毁的控件）不能拖垮整条消息泵。
    # 终态类型全部兜底到 _transfer_finished()，保证按钮永远不会卡在 disabled。
    def _dispatch_ui_message(self, kind, payload):
        handler = self._ui_handlers.get(kind)
        if handler is None:
            self._update_log(f"[警告] 未知的 UI 消息类型: {kind}")
            return
        try:
            handler(*payload)
        except Exception as e:
            self._update_log(f"[错误] 处理 UI 消息 {kind} 失败: {e}")
            if kind in ("done", "done_stream", "retry_done", "error"):
                try:
                    self._ui_handlers["terminal_fallback"]()
                except Exception as e2:
                    self._update_log(f"[错误] 恢复界面状态失败: {e2}")

    # 【阶段1-修改原因】日志缓冲的 flush 出口。force=False 时仅由主线程兜底调用。
    # 改进 1：主线程调用时直接写入日志区，省掉一次队列往返 + 一个 drain 周期的延迟；
    #         worker 线程调用时走 _post_ui。
    # 注意：_update_log / _post_ui 必须在释放 _log_lock 之后调用，
    # 避免持锁期间进入 Tk 或 queue.put，保持锁序单一可读。
    def _flush_log_buffer(self, force=False):
        with self._log_lock:
            if not self._log_buf:
                return
            if not force and (time.monotonic() - self._log_last_flush) < config.LOG_FLUSH_INTERVAL_S:
                return
            text = "\n".join(self._log_buf)
            self._log_buf = []
            self._log_last_flush = time.monotonic()
        if threading.current_thread() is threading.main_thread():
            self._update_log(text)          # 主线程：直接写入
        else:
            self._post_ui("log", text)      # worker：投递

    # 【阶段1-修改原因】进度条上限的跨线程投递口（原本 worker 里直接用 lambda 调 configure）。
    def _set_progress_range(self, total):
        if _DEBUG_UI_THREAD_ASSERT:
            assert threading.current_thread() is threading.main_thread(), \
                "_set_progress_range 必须在主线程调用"
        self._leave_marquee()
        # 【阶段4-修改原因】value 不再硬写 0。
        # 阶段3 之前这条消息总在"扫描刚结束、一个文件都还没传"时到达，写 0 是事实；
        # 阶段4 扫描与传输重叠之后，它到达时通常已经传完若干个了（这正是收益所在），
        # 再写 0 等于把进度条往回拽一大截——既是假消息，也正好撞上 I3 那条
        # "进度条只许前进"的断言。取当前完成数，语义变成"上限确定了，当前走到这儿"。
        # 回滚路径（先扫完再开传）此刻的 _completed 必然是 0，行为与改动前完全一致。
        try:
            done = max(0, int(self._core._completed))
        except (TypeError, ValueError):
            done = 0
        self.progress.configure(maximum=max(1, int(total)), value=min(done, max(1, int(total))))

    # 【阶段4-新增】"总数未知"的外显形态：跑马灯。
    # 流式扫描期间总数是扫完才知道的，此时进度条无法表达比例，能诚实表达的只有
    # "在动"。模式与本地→本地路径用的那套完全相同（configure + start(50)），
    # 不新增控件、不新增常量。
    def _set_progress_marquee(self):
        if _DEBUG_UI_THREAD_ASSERT:
            assert threading.current_thread() is threading.main_thread(), \
                "_set_progress_marquee 必须在主线程调用"
        self._progress_marquee = True
        try:
            self.progress.configure(mode="indeterminate")
            self.progress.start(50)
        except Exception:
            pass            # 界面已销毁（关窗竞态）：终态复位照样会把它收拾干净

    def _leave_marquee(self):
        """【阶段4-新增】跑马灯 → 精确进度条（幂等）。

        两个入口都会调它：progress_range（扫描结束，正常路径）与 _on_progress_msg
        （万一消息通道比 progress_range 先到——worker 拿到总数后立刻就可能在投进度，
        而 progress_range 要走同一条 FIFO 队列，理论上不该超车，但**依赖顺序**是脆的：
        这里多做一次幂等切换，就不必假设队列的投递顺序）。"""
        if not self._progress_marquee:
            return
        self._progress_marquee = False
        try:
            self.progress.stop()
        except Exception:
            pass
        self.progress.configure(mode="determinate")

    # 【阶段1-修改原因】进度轮询计时器的重启口。必须先把已有的取消掉再重建，
    # 否则重复调用会留下多条并行的 after 链，进度条更新频率成倍上升。
    # 判空统一为 is not None（after 的返回 id 理论可为 0）。
    def _start_progress_poll(self):
        if _DEBUG_UI_THREAD_ASSERT:
            assert threading.current_thread() is threading.main_thread(), \
                "_start_progress_poll 必须在主线程调用"
        if self._progress_timer_id is not None:
            try:
                self.root.after_cancel(self._progress_timer_id)
            except Exception:
                pass
            self._progress_timer_id = None
        self._progress_timer_id = self.root.after(150, self._poll_progress)

    # 【阶段1-修改原因】"progress" 通道的占位处理器。
    # 【阶段3-修改原因】启用：FTP 并发 worker 每完成 FTP_PROGRESS_MAX_MSGS 分之一
    # 的文件就投递一次 (done, total)。它与 _poll_progress（现在降级为兜底心跳）
    # **不冲突**：两者写的都是 self._core._completed 这一个事实来源，谁先到都写同一个数。
    # 之所以要这条通道：并发之后 worker 不再经过 _transfer_one，进度刷新只靠
    # 150ms 轮询会在"每文件耗时很短"时报得很难看（进度条一格一格跳而不是随速度走）。
    def _on_progress_msg(self, done, total=None):
        if _DEBUG_UI_THREAD_ASSERT:
            assert threading.current_thread() is threading.main_thread(), \
                "_on_progress_msg 必须在主线程调用"
        # 【阶段4】能收到 progress 消息就说明总数已经确定（_post_progress 在
        # 总数未知时直接返回），所以顺带把跑马灯收掉——理由见 _leave_marquee。
        self._leave_marquee()
        # total 只在真的不一致时才改上限：progress["maximum"] 读回来是 Tcl 的
        # 浮点串，逐条 configure 是无谓开销。progress_range 已设过一次，
        # 这里只是让这条消息自成一体（万一将来有人不投 progress_range 也不会把
        # 上限留在上一次任务的值上）。
        if total is not None:
            try:
                if int(float(self.progress["maximum"])) != int(total):
                    self.progress.configure(maximum=total)
            except (TypeError, ValueError):
                pass
        # 【阶段3-修复｜体检发现】进度条只许前进，不许后退。
        # 消息里的 done 是 worker 在 self._lock 内取的**快照**，而 _post_progress 是在
        # 释放那把锁**之后**、且要走过 _db_enqueue 与 _note_transfer_result 才被调用的。
        # 于是两个 worker 完全可能"后算出来的先投递"：done=7 已入队，done=6 才入队。
        # _ui_queue 是 FIFO，主线程照单全收就会把进度条从 7 拉回 6——用户看到的是抖动。
        # （阶段3 之前不存在这条路径：单连接实现只有一个生产者，天然有序。）
        #
        # 判据取"进度条当前显示值"而不是另记一个计数器：这样消息通道写过的值、
        # 兜底心跳 _poll_progress 直接写进去的 self._core._completed，两者都自动算数，
        # 两条轨不需要互相知道对方存在，也就不会打架。
        try:
            cur = int(float(self.progress["value"]))
        except (TypeError, ValueError):
            cur = -1                    # 读不出来就不拦（宁可抖一下，也不能卡住进度）
        if done < cur:
            return
        self.progress["value"] = done

    # ---- UI 更新 ----

    # 【阶段1-修改原因】只加线程断言。它是 after 回调，理应只在主线程跑；
    # 断言把"意外被 worker 直接调用"这种回归当场暴露出来，而不是变成随机崩溃。
    #
    # 【阶段3-说明】保留为**兜底心跳**，没有摘除。方案给了"降级为兜底心跳或直接摘除"
    # 两个选项，选前者。它负责的事一旦摘掉就没有替代品：
    #   ① FTP 路径上 worker 已经死光、但协调线程还在收尾（还库/建终态）时，
    #      进度条停在最后一格而不会掉队；
    #   ② 它是**消息通道的独立第二来源**：消息通道万一因为异常没投出去
    #      （_dispatch_ui_message 里那条 except 只记日志），进度条仍然自己会走。
    # 【体检-更正】早先这里还写过一条理由"本地→本地并发路径不发 progress 消息"——
    # 查证后不成立：本地→本地走的是 progress.configure(mode="indeterminate") 的
    # **跑马灯**（_start_local_to_local），它压根不用 value 这个量，也不投
    # progress_range/progress_start，所以本心跳在本地路径上根本不会被启动。
    # 也就是说：心跳只对 FTP 任务生效，而 FTP 任务恰好同时有消息通道——
    # 这正是"双轨并存"是常态而不是特例的原因（用例 I 就是钉这一条的）。
    # 代价只是每 150ms 一次 Tk 赋值。
    def _poll_progress(self):
        if _DEBUG_UI_THREAD_ASSERT:
            assert threading.current_thread() is threading.main_thread(), \
                "_poll_progress 必须在主线程调用"
        self.progress["value"] = self._core._completed
        if self._core._completed < self._core._total:
            self._progress_timer_id = self.root.after(150, self._poll_progress)

    # ==================== 功能B：实时速度 / ETA ====================

    # 【worker 侧的唯一入口：只加两个整数】
    #   不碰 Tk、不发消息、不记日志。调用点全部在"文件真的落到目标上了"之后
    #   （`_copy_with_retry` 返回 ok / 下载或上传的原子写返回之后）。
    #
    # 【跳过（_should_skip 命中）为什么不计】
    #   跳过的文件**一个字节都没搬**。把它算进速度里，速度会虚高；
    #   续传场景下更极端 —— 整个任务是"全部跳过"，那会算出"速度无穷大"。
    #   分子分母都只统计真的搬了的，比值才有意义。
    #
    # 【为什么在锁里做加法就够，不需要别的同步】
    #   worker 只写、主线程只"取走并清零"，两步都在同一把锁里，没有中间态。
    #   加法的量级是 int，GIL 下本来也不会撕裂，加锁是为了让"取走"这一步
    #   **不会丢掉恰好同时发生的那一次加法**。
    # 【阶段一 Bug3-新增】"第一个文件开始传输"事件：**有文件真的在搬**的最早信号。
    #   它存在的唯一理由是给状态栏一个比跑马灯更准的判据。跑马灯表达的是"总数还不知道"
    #   （扫描没结束），而阶段4 流式之后这句话与"文件已经在传"**长时间同时成立** ——
    #   FTP 源 12 层目录要扫 10~30 分钟，这期间日志在滚动、进度条在跑马灯，
    #   速度却被 `if self._progress_marquee` 一路压成"扫描中…"，用户看不到速度。
    #   不新增控件的解法就是换判据：把"扫描态"定义成"还在扫**且**还没有文件开始"。
    #
    #   为什么不用 _completed > 0：那是"**传完**了几个"，而这里要的是"开始"。二者的差别
    #   在慢链路上是分钟级 —— 第一个 200MB 的文件还没传完时，速度其实已经在动了，
    #   用 _completed 会让状态栏白等一整个文件。
    #   只写 bool、只由 False 变 True，worker 侧调用、主线程读；不加锁的理由同 _total。
    def _note_transfer_start(self):
        if not self._transfer_started:
            self._transfer_started = True

    def _note_bytes(self, n):
        if not n or n <= 0:
            return
        with self._speed_lock:
            self._speed_pending_bytes += n
            self._speed_pending_files += 1

    # 【主线程侧：把"攒下的字节"变成界面上的一个字串】
    #   与 _poll_progress 是**两条独立的 after 链**，互不影响：
    #   进度条那条 150ms、且本地路径上根本不启动（见 _poll_progress 上方注释），
    #   而速度这条必须在本地路径上也活着 —— 那正是本功能的主要使用场景。
    def _start_speed_poll(self):
        # 重启前先取消：与 _start_progress_poll 同理，重复调用会留下并行 after 链，
        # 于是刷新频率成倍上升（界面看不出，但 CPU 看得出来）。
        if self._speed_timer_id is not None:
            self.root.after_cancel(self._speed_timer_id)
            self._speed_timer_id = None
        self._refresh_speed()          # 开局先刷一次，别让标签空着等 500ms
        self._speed_timer_id = self.root.after(
            config.SPEED_POLL_INTERVAL_MS, self._poll_speed)

    def _poll_speed(self):
        if _DEBUG_UI_THREAD_ASSERT:
            assert threading.current_thread() is threading.main_thread(), \
                "_poll_speed 必须在主线程调用"
        self._refresh_speed()
        # 【自终止条件用 _task_active，不用 _completed < _total】
        #   后者在本项目里对本地路径恒为假（_total 按设计是 0），会让本链**开局就断**。
        #   用 _task_active 则与 _transfer_finished 的幂等窗口完全对齐：
        #   任务在跑就继续刷，任务一结束立刻停 —— 即使 _transfer_finished 因为
        #   某种意外没来得及 after_cancel，这条链也会自己停下，不会漏。
        if self._core._task_active:
            self._speed_timer_id = self.root.after(
                config.SPEED_POLL_INTERVAL_MS, self._poll_speed)
        else:
            self._speed_timer_id = None

    def _set_speed_final_text(self):
        """【功能B】终态那一刻标签上留什么。

        需求写的是"完成后：清空或显示 完成"。这里选**后者**，理由是区分度：
        清空之后，"传完了"和"刚从没跑过"在界面上长得一模一样。
        （停止/出错也走本函数 —— 那两种情况下 `_stop_reason` 非 None，
         但这里的措辞只到"已结束"为止，具体原因在日志区和弹窗里，不在这里重复。）
        """
        try:
            self.speed_label.configure(text="完成")
            self._speed_last_text = "完成"
        except Exception:
            pass        # 界面已销毁（关窗竞态）

    def _speed_reset(self):
        """新任务开局清空。**只清数据，不碰定时器** —— 定时器的生死归
        `_start_speed_poll` / `_poll_speed` / `_transfer_finished` 三处管，
        在数据复位里顺带 cancel 会让"谁负责停表"变成两处，早晚对不上。"""
        with self._speed_lock:
            self._speed_pending_bytes = 0
            self._speed_pending_files = 0
        self._speed_window = []
        self._speed_ema = None
        self._speed_task_bytes = 0
        self._speed_task_files = 0
        self._speed_task_start = None
        # 【阶段一 Bug2/Bug3】两个新指标的权威复位点就在这儿（本函数是"新任务开局
        #   清空"的唯一入口，全局只有一个调用点，所以放这儿能覆盖全部路径：FTP
        #   下载/上传、本地、以及多源模式的每一个源）。
        #   `_scan_dirs` 是**一格列表**，复位必须**原地改** `[0] = 0`，不能写
        #   `self._scan_dirs = [0]` —— 扫描是生成器，它在被创建时就把这个列表对象
        #   抓在手里了；重新绑定只会让主线程看新盒子、生成器往旧盒子里加，计数永远
        #   停在 0。这是本轮唯一一处"看起来等价、实际不等价"的写法。
        #   （FTP worker 入口还有一次同样的复位，那是为了"扫描开始前"这个更紧的
        #   时序；两处重复是刻意的，不是漏删。）
        self._scan_dirs[0] = 0
        self._transfer_started = False
        # 起跳前先把标签清掉：上一次任务留下的"完成"或某个速度值会挂在那儿，
        # 而新任务的头几百毫秒本来就该是空的（还没搬完一个文件）。
        self._speed_last_text = ""
        try:
            self.speed_label.configure(text="")
        except Exception:
            pass

    def _refresh_speed(self):
        """取走 worker 的字节 → 滑窗 → EMA → 写标签。**全程主线程**。"""
        now = time.monotonic()

        # ① 先取走，再判"本周期有没有新数据" —— 顺序反了会把刚到的那批推后 500ms。
        with self._speed_lock:
            add_bytes = self._speed_pending_bytes
            add_files = self._speed_pending_files
            self._speed_pending_bytes = 0
            self._speed_pending_files = 0
        if add_bytes:
            self._speed_window.append((now, add_bytes))
            self._speed_task_bytes += add_bytes
            self._speed_task_files += add_files

        # ② 滑窗右移：只留最近 SPEED_WINDOW_S 秒
        while self._speed_window and (now - self._speed_window[0][0]) > config.SPEED_WINDOW_S:
            self._speed_window.pop(0)

        # ③ 瞬时速度 = 窗口内总字节 / 窗口内**实际时间跨度**
        #    【为什么分母是"跨度"而不是常量 10.0】
        #      任务刚开始时窗口里只有一两秒的样本，除以 10 会把速度**低估**成实际值的
        #      五分之一，用户看到的是"传输刚开始时特别慢"，纯属错觉。
        #      跨度随样本自然长到 10 秒，也就是需求里说的"第一个 10 秒窗口填满前
        #      直接用瞬时值"。
        #    【窗口空了分两种，**必须分开处理** —— 这一处踩过两次坑，都记下来】
        #
        #      ① 曾经有过速度，窗口却空了 = 最近 10 秒一个字节都没搬完。
        #         这是事实，速度**就是** 0，必须让 EMA 往下走。
        #         第一版这里是"算不出来就不更新"，于是十秒钟没动静时界面上
        #         **一直挂着十分钟前那个速度** —— 最坏的一种显示：数字看着正常，
        #         但它描述的是过去，而用户正盯着它判断"还要不要等"。
        #         （验收脚本 S5 就是把它抓出来的：窗口清空后 EMA 原封不动。）
        #         注意是**降到 0**（0.7 倍地衰减），不是"啪"地掉到 0 —— 那正是要 EMA 的原因。
        #
        #      ② 从来没有过速度（`_speed_ema is None`），窗口也是空的 = 任务刚开局，
        #         一个文件都还没传完。此时**必须留白**，不能报 0。
        #         第二版把两种情形合并成"窗口空就是 0"，于是开局第一拍就写下
        #         `_speed_ema = 0.0`，界面从第一毫秒起就显示"0.0 KB/s" ——
        #         而那正是本函数上面明说"我们无法支持"的那个断言：
        #         此刻很可能正有一个 200MB 的文件传到一半。
        #         （T6 就是钉这一条的；它当时红了。）
        #
        #      ③ 窗口里有样本、但跨度小到算不出来（刚塞进去第一条）：`None`，
        #         保留上一次的 EMA。这不是"没数据"，是"数据还不够摊出一个比率"。
        instant = None
        if self._speed_window:
            span = now - self._speed_window[0][0]
            if span > 1e-6:
                instant = sum(b for _, b in self._speed_window) / span
        elif self._speed_ema is not None:
            instant = 0.0

        # ④ EMA 平滑。第一条直接用瞬时值（_speed_ema is None 即"还没算过"），
        #    否则 0.3*instant + 0.7*None 会当场 TypeError。
        if instant is not None:
            if self._speed_ema is None:
                self._speed_ema = instant
            else:
                self._speed_ema = (config.SPEED_EMA_ALPHA * instant
                                   + (1.0 - config.SPEED_EMA_ALPHA) * self._speed_ema)
        speed = self._speed_ema

        # ⑤ 组装文本
        text = self._speed_text(now, speed)
        # 只在真的变了才 configure：Tk 的 configure 会触发一次重绘，
        # 而 500ms 一次的空写在整个传输过程里是纯浪费。
        if text != self._speed_last_text:
            self._speed_last_text = text
            try:
                self.speed_label.configure(text=text)
            except Exception:
                pass        # 界面已销毁（关窗竞态）：终态复位照样会收拾干净

    def _speed_text(self, now, speed):
        """把"现在该显示什么"这件事**集中在一处**。四种状态，互斥且穷尽。

        【阶段一 Bug3-修改】扫描态的判据从"跑马灯开着"改成"跑马灯开着 **且** 还没有
        文件开始传"。理由见 _note_transfer_start 上方那段 —— 一句话：跑马灯说的是
        "总数还不知道"，它要等扫描**结束**才关，而流式扫描期间文件早就在传了。
        把这两件事混为一谈，代价是速度被整整 10~30 分钟的扫描期压住不显示。

        改完之后三种形态（与裁定一致）：
          ① 扫描中、还没文件开始  → `扫描中… 已扫 N 个目录`
          ② 扫描中、已有文件在传  → `1.2 MB/s · 已传 M 个文件`（**不显示 ETA**：
             总数还不知道，ETA 是编的）
          ③ 扫描结束（跑马灯关了）→ 原行为，一字不改：`1.2 MB/s · 剩余 3 分`

        【后续裁定②-补第 ⓪ 档】暂停是**主动状态**，单独一档：
          ⓪ 已暂停                → `已暂停 · 已传 M 个文件`
        它与 ①②③ 也互斥（暂停时不再报速度/扫描进度），恢复后自然落回 ①②③。
        """
        # ⓪ 【裁定②】用户主动暂停：这是一个**主动状态**，压过下面所有档位。
        #    必须排在 ① 之前 —— 扫描期按暂停时，跑马灯还亮着、`_transfer_started` 还是
        #    False，落到 ① 会显示「扫描中… 已扫 N 个目录」，可它**并没有在扫**：
        #    那个数字冻在原地不动，读起来跟卡死没有区别 —— 而"看着像卡死"正是本裁定
        #    要治的那个观感（暂停前的形态是另一头：速度衰减到 0.0 KB/s，同样像卡死）。
        #    `_pause_event` 的语义是 **set = 运行中 / clear = 已暂停**（见 __init__ 与
        #    _toggle_pause），所以判据是 `not is_set()`。
        #    ⚠️ "停止"**不会**被误显示成"已暂停"：_toggle_stop 与 _abort_on_consec_failures
        #    都会 set() 它（那就发生在解除 worker 阻塞的同一处），且终态一到
        #    _task_active 就是 False，⓪ 直接不成立。
        #    `_task_active` 这个前置条件不能省：刚启动、已完成这两种"没任务"的情形
        #    下 _pause_event 也可能是 clear 的，不加就会凭空冒出「已暂停 · 已传 0 个文件」。
        if self._core._task_active and not self._pause_event.is_set():
            return f"已暂停 · 已传 {self._core._completed} 个文件"
        # ① 扫描期且一个文件都还没开始搬：总数、速度、ETA 全是未知，能诚实说的
        #    只有"在动"以及动了多远。N 是**目录数**不是文件数 —— 空目录不产出文件，
        #    而 12 层的树上恰恰是"目录在往下走"最能说明没卡死。
        if self._progress_marquee and not self._transfer_started:
            return f"扫描中… 已扫 {self._scan_dirs[0]} 个目录"
        if not self._core._task_active:
            return ""                   # 没任务在跑（含刚启动、已完成）—— 留白
        if speed is None:
            # 任务在跑、但一个文件都还没搬完。**故意留白而不是显示 0 KB/s**：
            # 此刻"速度是 0"是一个我们**无法支持**的断言 —— 很可能正有一个
            # 200MB 的文件传到一半。留白是诚实的，0 是编的。
            #
            # 【阶段一】注意这里的顺序：上面那条扫描态**必须**排在它前面。②（扫描中、
            # 已在传）开局时速度还算不出来（窗口里只有一条样本、跨度太小），
            # 若把它放在这之后，用户会先看到一片空白、几秒后才蹦出速度。
            return ""
        parts = [self._fmt_speed(speed)]
        if self._progress_marquee:
            # ② 仍在扫描（总数未知）：ETA 的分母 _total 还是 0，_eta_text 本来就返回
            #    None；这里显式换成"已传几个文件"，让用户有第二个增长着的量可看。
            #    【为什么砍掉"已扫 N 个目录"】实测装不下：速度标签是定宽控件
            #    （pack_propagate(False)），预算 209px（见构造处的说明），而
            #    "1023.9 MB/s · 已扫 999999 个目录 · 已传 999999 个文件"量出来 300+px，
            #    超出部分会被**静默裁掉**（不换行、不报错，用户看到半句话）。
            #    按裁定"装不下优先砍已扫 N 个目录"，这里只留文件数。
            parts.append(f"已传 {self._core._completed} 个文件")
        else:
            eta = self._eta_text(now, speed)
            if eta:
                parts.append(eta)
        return " · ".join(parts)

    def _eta_text(self, now, speed):
        """ETA，或者 None（不显示）。

        【它是外推出来的，不是量出来的 —— 这一点必须说清楚】
          本项目**从不统计"待传总字节"**：进度上限 `self._core._total` 是**文件个数**，
          不是字节数（阶段4 起总数还是"扫完才知道"）。所以剩余字节只能估：
              剩余字节 ≈ (总文件数 − 已处理文件数) × 已传文件的平均大小
          文件大小分布悬殊时（DICOM 常见）这个估计会很粗。宁可它粗，也不假装它准 ——
          所以阈值卡得紧（已传够 10 秒、已传够字节、总数已知），并且**不显示信心区间**：
          显示"剩余 2 分 15 秒"然后跳到"剩余 5 分"是可接受的，谁也不会拿它当承诺。
        """
        if self._core._total <= 0:
            return None                  # 总数未知：本地路径恒如此，见 config 里的说明
        if self._speed_task_bytes <= 0 or self._speed_task_files <= 0:
            return None
        if self._speed_task_start is None:
            return None
        elapsed = now - self._speed_task_start
        if elapsed < config.SPEED_ETA_MIN_S:
            return None
        if speed <= 0:
            return None
        remaining_files = self._core._total - self._core._completed
        if remaining_files <= 0:
            return None
        avg_bytes = float(self._speed_task_bytes) / self._speed_task_files
        remain_bytes = remaining_files * avg_bytes
        return "剩余 " + self._fmt_eta(remain_bytes / speed)

    @staticmethod
    def _fmt_speed(bps):
        """< 1MB/s 报 KB/s，否则报 MB/s（1024 进制，与资源管理器的口径一致）。"""
        if bps >= 1024 * 1024:
            return "%.1f MB/s" % (bps / (1024.0 * 1024.0))
        return "%.1f KB/s" % (bps / 1024.0)

    @staticmethod
    def _fmt_eta(seconds):
        """三档：<1 分 → "XX 秒"；<1 小时 → "X 分 XX 秒"；否则 → "X 小时 XX 分"。"""
        s = int(seconds + 0.5)
        if s < 60:
            return "%d 秒" % s
        if s < 3600:
            return "%d 分 %d 秒" % (s // 60, s % 60)
        return "%d 小时 %d 分" % (s // 3600, (s % 3600) // 60)

    # 【阶段1-修改原因】§4 兼容方案：日志写入改为"行数缓存 + 一次 insert"，兼容聚合后的多行文本。
    # 旧实现每写一条就 index("end-1c") 解析一次行号（O(日志长度) 的字符串解析），
    # 在几十万条消息下是纯浪费；改用 self._log_line_count 增量维护。
    # 聚合后的 msg 可能含换行，故按 msg.count("\n") + 1 累加，不能简单 +1。
    # 截断处顺带修正了旧代码的 off-by-one（原 delete("1.0","1.0") 是空操作）。
    def _update_log(self, msg):
        if _DEBUG_UI_THREAD_ASSERT:
            assert threading.current_thread() is threading.main_thread(), \
                "_update_log 必须在主线程调用"
        # 【阶段二-新增】常规档在这里把三类行丢掉。**位置就是本函数第一行有意义的语句**：
        #   过滤必须发生在 `_log_line_count` 累加**之前** —— 丢行不计数，否则被滤掉
        #   的行照样把计数推高，日志区会在明明只显示了几百行的时候触发截断，
        #   把**真正有用**的旧行删掉（截断删的是开头，也就是最老的行）。
        if self.log_level.get() == config.LOG_LEVEL_NORMAL:
            msg = _filter_log(msg)
            if not msg:
                # 整批都被滤掉了：不写、不计数、不滚动。静默返回不是"丢了错误"——
                # 能被滤掉的只有那三类常规行，任何一条 `[警告]`/`[错误]` 都不在其中。
                return
        # 【阶段四-新增】无头模式的文件 sink。
        #
        # 【为什么挂在这里，而不是挂在该文件原方案写的 `_flush_log_buffer`】
        #   方案（`22_plan_stage4.md` §3 第 3 条）当时写的是 `_flush_log_buffer`。
        #   上手改的时候发现**那个点是错的**：`_flush_log_buffer` 只覆盖
        #   `_threadsafe_log` 那一半，而**最要紧的那些行走的不是它** ——
        #   `[源 N/N] 完成：复制 X 个…`、`全部完成：N 个源…`、`传输完成：…`、
        #   `已停止：…` 全是主线程直接调 `_update_log`（`:4081`/`:4086`/`:4958`/`:4971`…）。
        #   挂在 `_flush_log_buffer` 上的话，无头日志文件里**一行总结都不会有** ——
        #   只剩"每个文件传了没"的碎行，恰恰是无人值守时最需要的那几句不见了。
        #   `_update_log` 是**唯一的终端写入点**（谁都绕不过它，且它自己断言在主线程），
        #   所以改挂在这里。I/O 开销也与原方案**相同**：`_flush_log_buffer` 本就是把
        #   一整批 join 成一条再调 `_update_log`，两条路的文件写入次数一样。
        #
        # 【为什么写在 `if not msg: return` 之后、截断逻辑之前】
        #   * 之后：文件里装的与屏幕上显示的**是同一份**（`log_level` 过滤两边同时生效）。
        #     口径与「导出日志」一致 —— 那个导出的也是"屏幕上这些"。
        #   * 之前：`MAX_LOG_LINES` 截断删的是**日志区开头那些行**，而文件是**这一轮
        #     完整的历史**。反过来写的话，一个跑了几十万文件的任务，日志文件会跟着
        #     日志区一起被砍到只剩最后 1000 行 —— 那是无头模式唯一的记录。
        if self._log_sink is not None:
            self._log_sink_write(msg)
        self.log_text.configure(state="normal")
        self.log_text.insert("end", msg + "\n")
        self._log_line_count += msg.count("\n") + 1
        # 限制日志行数，防止几小时的大任务日志膨胀拖慢 UI
        if self._log_line_count > config.MAX_LOG_LINES:
            excess = self._log_line_count - config.MAX_LOG_LINES
            self.log_text.delete("1.0", f"{excess + 1}.0")
            self._log_line_count -= excess
            self._log_truncated = True      # 【功能A】这里删掉的每一行都**真的没了**
        self.log_text.see("end")
        self.log_text.configure(state="disabled")

    # 【阶段1-修改原因】worker 线程写日志的入口。两条路径：
    #   回滚分支：与改造前逐字等价；
    #   正常分支：写入聚合缓冲，够一批（LOG_BATCH_SIZE=50）或够时间（0.3s）就 flush。
    # 只加 _log_lock，绝不在持锁期间进 Tk 或 queue.put。
    # 注：0.3s 是刻意压低的——DICOM 场景下失败信息的可见性比聚合效率更重要。
    def _threadsafe_log(self, msg):
        if not config._UI_QUEUE_ENABLED:
            self.root.after(0, self._update_log, msg)   # 回滚分支：与改造前等价
            return
        should_flush = False
        with self._log_lock:
            self._log_buf.append(msg)
            if len(self._log_buf) >= config.LOG_BATCH_SIZE:
                should_flush = True
            elif (time.monotonic() - self._log_last_flush) >= config.LOG_FLUSH_INTERVAL_S:
                should_flush = True
        if should_flush:
            self._flush_log_buffer(force=True)

    # ==================== 阶段四：无头模式 ====================
    #
    # 【这一节在整个设计里的位置】路线 B（见 `22_plan_stage4.md` §0）：
    #   **不假装"没有 GUI"，而是真的建一个窗口、再把它藏起来。**
    #   于是整套既有传输逻辑（Tk 变量、worker 线程、消息通道、终态网关）
    #   **一行不改地**跑在一条真实（只是看不见）的 Tk 主循环上。
    #   代价是必须满足它的前提：**存在一个可用的窗口站**，也就是"用户处于登录状态"
    #   —— 而这与共享文件夹的前提（SMB 凭据是登录会话级的）**是同一条**，
    #   所以注册计划任务时强制"只在用户登录时运行"（不加 `/RU` `/RP`）。
    #
    # 【`_headless_bail` 与 `_headless_finish` 的分工】两者都"收口并定退出码"，
    #   区别只在**什么时候**：
    #     * `_headless_bail`  —— 任务**根本没跑起来**（配置错 / 抢不到锁 / 状态库挂了）；
    #     * `_headless_finish` —— 任务**跑完了**（正常结束 / 停止 / 报错），
    #       要先把 db-writer 排空再退出，否则任务锁要等到心跳过期（30 秒）才释放。

    def _open_log_sink(self, path):
        """打开无头模式的日志文件（追加）。失败**不抛**，只把 sink 留成 None。

        【为什么失败不抛】日志文件是"给用户看的记录"，不是这次传输能不能跑的判据。
        一个写不进去的日志路径（盘满、目录被 ACL 挡住）不该让备份任务整个不跑 ——
        那才是"为了记录而牺牲被记录的事"。sink 留 None 时 `_update_log` 那条改写
        直接跳过，界面/退出码一切照常。但**要在界面上说一声**：调用方在打开失败后
        会往日志里写一行，用户至少能在 `schtasks` 的历史记录里看到退出码。
        """
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            self._log_sink = open(path, 'a', encoding='utf-8')
            self._log_sink_path = path
            return True
        except Exception:
            self._log_sink = None
            self._log_sink_path = path
            return False

    def _log_sink_write(self, msg):
        """往日志文件写一条并 flush。

        【为什么每条都 flush】无头进程的正常出口是 `mainloop` 返回后 `sys.exit`，
        异常出口是被人 `taskkill` / 机器断电。"攒着最后一起写"在这些出口下
        **就是把最后那一批行丢了** —— 而那批里正好是 `全部完成：…` 与退出码那几行，
        也就是事后唯一想看的几行。一次 flush 的代价相对一次文件传输可以忽略。
        """
        try:
            with self._log_sink_lock:
                self._log_sink.write(msg + "\n")
                self._log_sink.flush()
        except Exception:
            # 写文件失败（盘满 / 设备拔了）**不能**把传输带崩：日志是旁路，
            # 不是主链路。这里静默吞掉，sink 保持打开（下次可能又能写了）。
            pass


    # ==================== 第 1 步-新增：8 个薄封装 ====================
    #
    # 它们不是"新功能"，是把宿主里原本**内联写私有字段**的那几行原样抬到这里。
    # 抬的理由只有一个：宿主不该写 `self._ui._progress_timer_id = None`。
    # 每一条都在 `_verify_split_ast.py` 的 `allowed_diffs` 里登记了理由。

    def attach_widgets(self, **kwargs):
        """把建造期创建出来的控件挂进来（**挂载点紧跟创建它的那一行**）。

        三个控件都在 `_build_transfer_tab` 里面创建，而 `self._ui` 在它之前构造 ——
        所以传不进构造器。挂载点放在创建那一行之后，于是"控件从哪一刻起可用"
        与改动前**完全一致**（原先也是那一刻赋给 `self.X` 的）。
        """
        for name, widget in kwargs.items():
            if name not in ("progress", "log_text", "speed_label"):
                raise ValueError("未登记的控件名：%s" % name)
            setattr(self, name, widget)

    def _cancel_timers(self):
        """终态收口：取消进度与速度两条 after 链。

        顺序与改动前一致（先进度、后速度）—— 原代码就在 `_transfer_finished` 里
        挨着写了这两段。
        """
        if self._progress_timer_id is not None:
            self.root.after_cancel(self._progress_timer_id)
            self._progress_timer_id = None
        if self._speed_timer_id is not None:
            self.root.after_cancel(self._speed_timer_id)
            self._speed_timer_id = None

    def _reset_progress_bar(self):
        """终态把进度条复位成精确模式，并清掉跑马灯标记（原 `_transfer_finished` 里那 7 行）。"""
        try:
            self.progress.stop()
            self.progress.configure(mode="determinate", maximum=100, value=0)
        except Exception:
            pass
        self._progress_marquee = False

    def _reset_log_area(self):
        """新任务开局清空日志区并归位两个计数（原 `_begin_run_log` 里那 5 行）。"""
        self.log_text.configure(state="normal")
        self.log_text.delete("1.0", "end")
        self.log_text.configure(state="disabled")
        self._log_line_count = 1
        self._log_truncated = False

    def _note_task_start_time(self):
        """记下本源的起跳时刻（ETA 的计时零点）。原 `_launch_source` 里那一行。"""
        self._speed_task_start = time.monotonic()

    def _reset_transfer_started(self):
        """按**源**归零"第一个文件是否已开始搬"。原 FTP worker 里那两处赋值。"""
        self._transfer_started = False

    def _close_log_sink(self):
        """取出日志文件句柄并置空，返回取出的那个（幂等）。

        原 `_headless_close_sink` 里写的是 `f, self._log_sink = self._log_sink, None`，
        这里只是把"改自己的字段"这一步收进本类。
        """
        f, self._log_sink = self._log_sink, None
        return f

    def _log_sink_path_value(self):
        """本进程日志文件路径（没打开过时为空串）。"""
        return self._log_sink_path

