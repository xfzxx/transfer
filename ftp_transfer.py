# -*- coding: utf-8 -*-
r"""FTP 传输层（`FtpTransfer`）—— 从 `FileTransferApp` 里切出来的第 3 个真模块。

【它管什么】FTP 那一路的**全部**：worker 池 / 生产者 / 每文件搬一个 / 单线程版 /
远端路径解析 / 本地文件枚举 / 进度投递。

【层级】**L3** —— 只依赖 `transfer_core`(L2) / `ui_channel`(L1) / L0（config·state_db·ftp_client）。
      与 `maintenance_worker`(L4) **互不依赖**。

【装配】宿主把 `_core` / `_lock` / `_pause_event` / `_stop_event` / `_scan_dirs` /
      任务参数（`source_path` / `dest_path` / `flatten` / `thread_count`）与 `live` 视图
      **同对象**注入；`_ui` 在构造时**还不存在**（它的 handlers 表指向本类的方法）
      ⇒ 由宿主 `attach_host_widgets()` 在 `_build_ui()` 之后补挂（逐个断言、只许挂一次）。

【`_total` 为什么归这里】它是 FTP 那一路扫描出来的总数，写它的是本类的 worker
      （`_scan_finished` / `_ftp_download_worker` / ...）。第 2 步时它暂时留在宿主
      （见 `_UiStatsView` 的说明），**第 3 步随本类搬过来**。

【`ftp_source_config` / `ftp_dest_config` 为什么是 property】它们会被宿主的
      `_fill_ui_from_task`（恢复上次任务）/ `_apply_active_profile`（套用配置档）**在构造之后重写**
      ⇒ 按值快照会让 FTP 上传/下载**用旧配置**（第 2 步实测的两个真回归就是这个形态，见 `63`）
      ⇒ 必须走 `live` 视图**每次现读**。

【谁在对齐】`_checks\_verify_split_ast.py --spec _split_ast_spec_step3.json`。
"""
import ftplib
import os
import queue
import threading
from pathlib import Path

import config
import ftp_client


class FtpTransfer(object):
    """FTP 那一路的 worker / 生产者 / 远端路径解析。装配见模块 docstring。"""

    def __init__(self, core, live, lock, pause_event, stop_event, scan_dirs,
                 source_path, dest_path, flatten, thread_count, on_part_reclaim):
        self._core = core
        self._live = live
        self._lock = lock
        self._pause_event = pause_event
        self._stop_event = stop_event
        self._scan_dirs = scan_dirs
        self.source_path = source_path
        self.dest_path = dest_path
        self.flatten = flatten
        self.thread_count = thread_count
        # 【消 `ftp → maint` 那条反向边】`.part` 回收**不再由本类自己发起**，而是调宿主
        #   传进来的**回调**（宿主的方法）⇒ 本类**不 import 上层**。
        #   **时机不变**：仍是 worker 启动时那一下（原 `_start_part_reclaim(...)` 那一行的位置）。
        self._on_part_reclaim = on_part_reclaim
        # 归本类自己（宿主不再持有 —— 见模块 docstring 的 `_total` 说明）
        self._total = 0
        # 进度投递步长：总数为 N 时最多投 `config.FTP_PROGRESS_MAX_MSGS` 条；由 `_scan_finished`
        #   在 `_total` 确定后写一次、之后只读（多线程读 int 安全）。
        #   **初值 1 与原宿主的 `self._progress_stride = 1` 逐字相同** ⇒ 行为等价。
        self._progress_stride = 1
        # `_ui` 由宿主 `attach_host_widgets()` 补挂（构造时它还不存在）
        self._ui = None
        self._ui_attached = False

    # -------- 走 live 视图的两个（**每次现读**，不做快照）--------
    # ⚠️ `_HostLiveView` 暴露的属性名是 **`ftp_src` / `ftp_dest`**，不是 `ftp_source_config`。
    #    2026-10-10 实测踩过：按 `ftp_source_config` 写 ⇒ `AttributeError`（AttributeError 在
    #    属性读取时才炸，静态 AST 折叠看不出来 ⇒ 只有"真构造"才拦得住）。
    @property
    def ftp_source_config(self):
        return self._live.ftp_src

    @property
    def ftp_dest_config(self):
        return self._live.ftp_dest

    def attach_ui(self, ui):
        """宿主在 `_build_ui()` 之后补挂 `_ui` —— **只许挂一次**（照抄 core 的做法）。"""
        assert not self._ui_attached, "FtpTransfer._ui 已经挂过了（重复挂 = 两份引用）"
        self._ui = ui
        self._ui_attached = True


    # ---- FTP 下载（修复 550 错误的核心） ----
    def _start_ftp_to_local(self, extensions):
        cfg = self.ftp_source_config
        dst = self.dest_path.get().strip()
        remote_url = self.source_path.get().strip()   # Tk 变量在主线程读取
        flatten = self.flatten.get()
        # 【阶段3】并发度同样在主线程读：worker 线程碰 Tk 变量是数据竞争
        # （与上面 remote_url 的处理同理）。
        workers = self._ftp_pool_workers()
        # 【开跑回执】实际并发：池开关关掉时是 1（与 `_ftp_download_worker` 的分支一致），
        #   不能照着 `workers` 报，否则日志会说"4 个并发"而实际单连接在跑。
        self._ui._update_log("[参数] 本次并发 = %d"
                             % (workers if config._FTP_POOL_ENABLED else 1))
        threading.Thread(target=self._ftp_download_worker, args=(cfg, dst, extensions, remote_url, flatten, workers), daemon=True).start()

    # 【阶段3-新增】FTP 并发度的唯一出口。
    # 取界面上 thread_count 的值（与本地→本地并发复用同一个控件，不新增控件）。
    # 为什么不是"恒为 4"：FTP_POOL_DEFAULT_SIZE=4 是**池的默认构造参数**，
    # 而界面上明明摆着一个线程数控件；两处默认值打架时以用户可见的那个为准——
    # 对着医院 PACS 悄悄开 4 条并发连接，不是"默认值"该有的分量。
    # 想改成恒为 4，把这里换成 return FTP_POOL_DEFAULT_SIZE 即可（一行）。
    def _ftp_pool_workers(self):
        try:
            n = int(self.thread_count.get())
        except Exception:
            n = 1
        return max(1, n)

    # 【阶段3-新增】FTP→本地 的分发口。_FTP_POOL_ENABLED=False 时逐字回滚到单连接实现。
    def _ftp_download_worker(self, ftp_cfg, local_dst, extensions, remote_url, flatten,
                             workers=None):
        if not config._FTP_POOL_ENABLED:
            return self._ftp_download_worker_single(ftp_cfg, local_dst, extensions,
                                                    remote_url, flatten)
        if workers is None:
            workers = config.FTP_POOL_DEFAULT_SIZE    # 非 UI 调用方（测试/脚本）的兜底
        pool = None
        scan_conn = None
        try:
            pool = ftp_client.FTPConnectionPool(ftp_cfg['host'], ftp_cfg['user'],
                                     ftp_cfg['password'], ftp_cfg['port'],
                                     size=workers, log_callback=self._ui._threadsafe_log,
                                     timeout=self._core._task_ftp_timeout,
                                     reconnect_attempts=self._core._task_reconnect_attempts)
            remote_dir = self._ftp_remote_dir_from_url(remote_url, ftp_cfg)
            self._ui._threadsafe_log(f"使用的远程目录: {remote_dir}")

            # ---- 阶段4：扫描与传输**重叠**（原来是"先扫完再开传"）----
            # 扫描连接独立于池，池里的 N 条全部留给 N 个 worker。为什么不能从池里借：
            #   * 借一条整个扫描期不还 → 并发立刻掉到 N-1（worker 是借到就一直持有到
            #     线程退出，见 _ftp_worker_loop，中途不会还）；
            #   * 边扫边还（每个目录借一次） → 更糟：扫描要等某条连接空出来，而连接
            #     只有 worker 退出才空；worker 退出要等队列空且生产者结束；生产者就是
            #     在等连接的那个扫描线程 —— 死锁。
            # 独立一条不改变"界面上设 N 就是 N 条并发传输"这个语义。
            scan_conn = ftp_client.FTPPooledConn(ftp_cfg['host'], ftp_cfg['user'],
                                      ftp_cfg['password'], ftp_cfg['port'],
                                      log_callback=self._ui._threadsafe_log,
                                      timeout=self._core._task_ftp_timeout,
                                      reconnect_attempts=self._core._task_reconnect_attempts)
            # 总数此刻还不知道（要扫完才知道），所以先转跑马灯；
            # 扫描一结束由 _ftp_run_workers 里的生产者投 progress_range 切回精确。
            # 同时把 _total 清零：它是"上一条命令"的残留值，而 _post_progress 用它当
            # "总数已知"的判据（不清零的话跑马灯期间会按旧总数狂发进度消息）。
            self._total = 0
            # 【阶段一 Bug2/Bug3】两个指示器按**源**归零（多源是串行的，一个源跑完
            # 才起下一个），位置紧挨着跑马灯 —— 它们与跑马灯是同一条命的量：
            # 跑马灯一亮，界面上说的就是"这个源正在扫"。
            # 原地改盒子，不重新绑定（生成器可能还握着旧引用）。
            self._scan_dirs[0] = 0
            self._ui._reset_transfer_started()      # 【第 1 步】按源归零（FTP 两处）
            self._ui._post_ui("progress_marquee")

            actual_remote_dir = self._ftp_probe_remote_dir(scan_conn, remote_dir)
            if actual_remote_dir is None:
                self._ui._threadsafe_log("无法进入目录，请检查 FTP 设置中的远程目录")
                # 本分支尚未扫描，self._total 可能残留上一个任务的值，故显式传 0
                self._core._db_end_task("done", "finished", total_seen=0)
                return
            self._ui._threadsafe_log(f"开始流式扫描: {actual_remote_dir}（边扫边传）")

            # 【阶段4】把"整棵树的 list"换成"逐目录推进的生成器"。
            # 过滤也在流上做（原来是对 all_files 做一次列表推导），语义完全相同。
            # 【阶段一 Bug2】把目录计数盒子递给遍历器，由它每列举完一个目录自增。
            # 【P0-1】没启用时**调用点逐字节不变**（一个关键字都不多传）——
            #   这既是最强的"默认关闭"保证，也让测试里那两个同签名的替身原样可用
            #   （`_verify_exe_d1.py` / `auto_test_stage4.py` 各有一处）。
            # `on_visit` 传的是"这个远端目录真的被列举过了"，不是"它存在"——
            #   同步点只推进到真的进去过的最大日期层，理由见 `_walk_remote_files`。
            _pruner = self._core._date_pruner
            if _pruner is None:
                stream = scan_conn.iter_files_recursive(actual_remote_dir, self._scan_dirs)
            else:
                stream = scan_conn.iter_files_recursive(
                    actual_remote_dir, self._scan_dirs,
                    prune=_pruner.make_prune_callback(actual_remote_dir),
                    on_visit=lambda cur: _pruner.note_visit_remote(actual_remote_dir, cur))
            if extensions:
                stream = self._ftp_filter_stream(stream, extensions)

            stats = self._ftp_run_workers(
                pool, workers,
                self._ftp_download_producer(stream, actual_remote_dir, local_dst, flatten),
                self._ftp_download_one)

            if stats["scan_error"]:
                # 【阶段4-必须做】扫描异常绝不能落成 done。它发生在生产者线程里，
                # 若只是让那根线程默默死掉，worker 会照常把队列排空并正常收尾，
                # 用户看到的是"任务完成"，而磁盘上少了一整片文件——静默漏传。
                self._core._db_end_task("error", "error",
                                  msg=f"扫描中断：{stats['scan_error']}")
            elif stats["scan_done"] and self._total == 0:
                # 【阶段4】原来是"扫到 0 个就直接返回、不开 worker"；流式之后
                # "一个都没有"同样是扫完才知道，所以挪到这里判（结论与日志一字不差）。
                #
                # 判据必须带上 scan_done（用例 M 逼出来的）：_total 为 0 现在有两种来路——
                #   ① 扫完了，确实一个都没有（本分支，日志与终态跟阶段3 一字不差）；
                #   ② 扫描跑到一半被用户按了停止（_scan_finished 根本没被调用），
                #      此刻**可能已经传完一批文件了**。只看 _total==0 会把 ② 也认成 ①，
                #      于是任务被记成 finished、日志说"没有符合过滤条件的文件"——
                #      按了停止却弹出"完成"，正是阶段2d 修过的那类缺陷
                #      （中止了却报"完成"，用户会以为数据都过去了）。
                self._ui._threadsafe_log("没有符合过滤条件的文件")
                self._core._db_end_task("done", "finished", total_seen=0)
            else:
                # 方案 §10 的 2c 验收项要求日志能直接看到命中跳过数。
                # 扫描被中途停止时 _total 还是 0，这条算式会算出负数，故加 _total 闸
                # （那种情形下"共扫到多少个"本来就未知，不打这条更诚实）。
                if stats["skip"] and self._total:
                    self._ui._threadsafe_log(
                        f"状态库命中跳过 {stats['skip']} 个，实际下载 {self._total - stats['skip']} 个")
                # total_seen：扫完了就是扫到的总数；被停止时用"已经派发出去的数量"，
                # 它等于"本次已经找到多少个文件"（停止只拦"还没派发的"，不丢已找到的）。
                # 不写 0：0 与"真的一无所有"分不开，而 done_count 又明明不是 0。
                self._core._db_end_task("done", self._core._end_reason(),
                                  total_seen=self._total or stats["dispatched"],
                                  failed_count=stats["fail"])
        except Exception as e:
            self._core._db_end_task("error", "error", msg=str(e))
        finally:
            # 扫描连接不一定被借到过（建池就失败时是 None），关闭是幂等的
            if scan_conn is not None:
                try:
                    scan_conn.close()
                except Exception:
                    pass
            if pool is not None:
                pool.close_all()

    # 【阶段3-新增】本地→FTP 的分发口。回滚同上。
    def _ftp_upload_worker(self, local_src, ftp_cfg, extensions, remote_url, flatten,
                           workers=None):
        if not config._FTP_POOL_ENABLED:
            return self._ftp_upload_worker_single(local_src, ftp_cfg, extensions,
                                                  remote_url, flatten)
        if workers is None:
            workers = config.FTP_POOL_DEFAULT_SIZE
        pool = None
        try:
            pool = ftp_client.FTPConnectionPool(ftp_cfg['host'], ftp_cfg['user'],
                                     ftp_cfg['password'], ftp_cfg['port'],
                                     size=workers, log_callback=self._ui._threadsafe_log,
                                     timeout=self._core._task_ftp_timeout,
                                     reconnect_attempts=self._core._task_reconnect_attempts)
            remote_base = self._ftp_remote_base_from_url(remote_url)
            self._ui._threadsafe_log(f"确保远程目录存在: {remote_base}")
            conn = pool.acquire()
            try:
                conn.ensure_remote_dir(remote_base)
            finally:
                pool.release(conn)

            # 【阶段4】os.walk 本来就是惰性的，原来把它整个落成一个 list 只是为了
            # 先数出总数（进度条上限）。改成流式后总数要扫完才知道，于是与下载路径
            # 同构：入口先转跑马灯，扫描结束时由生产者投 progress_range 切回精确。
            # 【阶段5-3-新增】远端 .part 回收：起一根后台线程，用它自己的一条连接。
            # 为什么不在池里借连接：与阶段4 的扫描连接同一条理由 —— 借走一条就少一条
            # 并发（worker 借到就一直持有到线程退出），而"等连接空出来"会与生产者
            # 互相等待成死锁。独立一条不改变"界面上设 N 就是 N 条并发传输"的语义。
            # 为什么不 join：它可能要在几千个目录上走一遍，把任务终态压在它后面
            # 是本末倒置（用户关心的是文件传完没有）。它删的是 7 天前的陈年残留，
            # 与本次在飞的文件互不相干，脱开跑是安全的。
            self._on_part_reclaim(ftp_cfg, remote_base, extensions)

            self._total = 0
            # 【阶段一 Bug2/Bug3】与下载路径逐字同源：按源归零，紧挨着跑马灯。
            self._scan_dirs[0] = 0
            self._ui._reset_transfer_started()      # 【第 1 步】按源归零（FTP 两处）
            self._ui._post_ui("progress_marquee")
            if os.path.isfile(local_src):
                stream = iter([(local_src, "", os.path.basename(local_src))])
            else:
                stream = self._iter_local_files(local_src, extensions)

            stats = self._ftp_run_workers(
                pool, workers,
                self._ftp_upload_producer(stream, remote_base, flatten),
                self._ftp_upload_one)

            if stats["scan_error"]:
                # 【阶段4-必须做】与下载路径同理：本地扫描异常也不能落成 done。
                self._core._db_end_task("error", "error",
                                  msg=f"扫描中断：{stats['scan_error']}")
            elif stats["scan_done"] and self._total == 0:
                # 【阶段4】"一个都没有"同样是扫完才知道，结论与日志一字不差。
                # scan_done 这一半判据的理由与下载路径完全相同（见那里的长注释）。
                self._ui._threadsafe_log("没有符合过滤条件的文件")
                self._core._db_end_task("done", "finished", total_seen=0,
                                  folder_count=0, matched=0, stopped=False)
            elif stats["fail"]:
                # 【阶段3】与 _ftp_upload_worker_single 的语义保持一致：上传路径
                # "任一文件失败即中止整个任务"（阶段1 起如此，2c 只补记录没改它）。
                # 第一个失败的 worker 已经置了 _stop_event，其余 worker 会在下一个
                # 文件边界停下，所以这里不能按"完成数"写终态，必须按"有过失败"。
                self._core._db_end_task("error", "error",
                                  msg=f"本批次有 {stats['fail']} 个文件上传失败"
                                      f"（上传路径的既有语义：任一失败即中止整个任务）")
            else:
                # total_seen 的取法与下载路径同理（扫描被停止时 _total 还是 0）。
                self._core._db_end_task("done", self._core._end_reason(),
                                  total_seen=self._total or stats["dispatched"])
        except Exception as e:
            self._core._db_end_task("error", "error", msg=str(e))
        finally:
            if pool is not None:
                pool.close_all()

    # 【阶段3-新增】执行骨架：生产者灌任务、N 个 worker 消费、结果回流。
    # 返回 {"ok": n, "skip": n, "fail": n, "scan_error": str|None,
    #       "scan_done": bool, "dispatched": n}。
    # scan_done / dispatched 是【阶段4】补的：流式之后"扫完了没有"与"扫到多少个"
    # 只有生产者知道，而调用方判终态要靠它们（见 _ftp_download_worker 的注释）。
    #
    # 【为什么用 producer_done 事件而不是哨兵对象】哨兵的收尾方式是"生产结束给每个
    # worker 投一个 None"，而这一步本身可能阻塞在满队列的 put 上——若此刻 worker 已
    # 因收到停止信号而退出，生产者就永远等在那里（死锁）。
    # 改成"队列空 **且** 生产者已结束"才退，双方谁都不需要等对方腾出手。
    #
    # 【阶段4-新增】生产者还兼职两件事，因为只有它知道"扫描什么时候结束"：
    #   1) 扫描一结束就把总数投给界面（跑马灯 → 精确进度条，见 _set_progress_range）；
    #   2) 兜住扫描异常。它跑在自己的线程里，异常若只让这根线程默默死掉，
    #      worker 会照常把队列排空、任务照常"完成"——磁盘上却少了一整片文件。
    def _ftp_run_workers(self, pool, workers, producer, handler):
        task_queue = queue.Queue(maxsize=config.FTP_TASK_QUEUE_MAXSIZE)
        result_queue = queue.Queue()
        producer_done = threading.Event()
        stats = {"ok": 0, "skip": 0, "fail": 0, "scan_error": None,
                 "scan_done": False, "dispatched": 0}

        def _producer():
            dispatched = 0
            scan_done = False
            try:
                for item in producer:
                    # 【停止/暂停都必须在**生产者**侧判】worker 一旦取走文件就会做完
                    # （半途停下会留下 .part 残渣且没有任何收益）。在派发口拦住，
                    # 语义就是"已经在传的传完，剩下的不再开始"——这正是按钮该有的含义。
                    if self._stop_event.is_set():
                        break
                    self._pause_event.wait()
                    if self._stop_event.is_set():
                        break
                    placed = False
                    while not placed:
                        # 【阶段一 Bug1】停止与暂停都提到循环头判。
                        # 原先只在 put 抛 Full 之后判停止，于是"队列一直满"时这一圈
                        # 是个 0.2 秒一拍的空转；暂停时更糟 —— 它压根不看暂停，
                        # 会在队列满的整段时间里持续空转（虽然不派发，但没意义）。
                        if self._stop_event.is_set():
                            break
                        self._pause_event.wait()
                        if self._stop_event.is_set():
                            break
                        try:
                            task_queue.put(item, timeout=0.2)
                            placed = True
                        except queue.Full:
                            pass            # 队列满：回到循环头，重新判停止/暂停
                    if not placed:
                        break               # 停止：剩下的不再开始
                    dispatched += 1
                    if dispatched % config.FTP_SCAN_LOG_EVERY == 0:
                        self._ui._threadsafe_log(f"已扫描到 {dispatched} 个文件…")
                else:
                    scan_done = True        # for-else：只有正常耗尽（非 break）才置位
            except Exception as e:
                # 扫描（远端遍历 / 本地 os.walk）中途出错。已派发的任务照常做完，
                # 但终态必须是 error —— 见本函数的注释。
                stats["scan_error"] = str(e)
                self._ui._threadsafe_log(f"[错误] 扫描中断：{e}")
            finally:
                stats["scan_done"] = scan_done
                stats["dispatched"] = dispatched
                if scan_done:
                    self._scan_finished(dispatched)
                producer_done.set()

        threads = [threading.Thread(target=self._ftp_worker_loop,
                                    args=(i, pool, task_queue, result_queue,
                                          handler, producer_done),
                                    daemon=True)
                   for i in range(max(1, int(workers)))]
        pt = threading.Thread(target=_producer, daemon=True)
        pt.start()
        for t in threads:
            t.start()
        pt.join()
        for t in threads:
            t.join()

        while True:
            try:
                status = result_queue.get_nowait()[0]
            except queue.Empty:
                break
            if status in stats:
                stats[status] += 1
        return stats

    def _scan_finished(self, dispatched):
        """【阶段4-新增】"扫描结束、总数确定"这一刻的收尾动作。

        先落 self._total 再投消息：worker 是拿 self._total 当"总数已知"的判据的
        （见 _post_progress），顺序反了会有一瞬间"消息说总数 N、判据还是 0"——
        那几条进度消息会被当成"总数未知"丢掉。丢掉不致命（心跳会补），但没必要。

        dispatched == 0 时不投：这条路径的结论是"没有任何文件"，终态由调用方
        按 self._total == 0 去写（与阶段3 之前"扫到 0 个就返回"完全一致）。
        停在跑马灯上也正确——终态复位会把它停下并转回 determinate。
        """
        if dispatched <= 0:
            return
        self._total = dispatched
        self._progress_stride = max(1, dispatched // config.FTP_PROGRESS_MAX_MSGS)
        self._ui._threadsafe_log(f"扫描完成，共 {dispatched} 个文件")
        self._ui._post_ui("progress_range", dispatched)
        self._ui._post_ui("progress_start")

    # 【阶段3-新增】worker 主循环。每个 worker **独占**一条连接（ftplib.FTP 不是
    # 线程安全的，且一套 cwd/RETR/STOR 序列在一条连接上才有意义），借到就一直持有，
    # 直到本 worker 退出才归还。
    # 每 worker 独立埋点、独立重连：一个文件失败不会带走这条线程，更不会带走别的 worker。
    def _ftp_worker_loop(self, worker_id, pool, task_queue, result_queue, handler,
                         producer_done):
        conn = None
        try:
            conn = pool.acquire()
            while True:
                if self._stop_event.is_set():
                    break
                # 【阶段一 Bug1-必须做】暂停**必须在这里也判一次**。
                # 原先只有生产者派发口（_producer 里那处 wait）判暂停，而队列上限是
                # FTP_TASK_QUEUE_MAXSIZE=1000、生产者又是抢着灌满的（见那里的 put 重试），
                # 于是按"暂停"的真实效果是：派发停了，**队列里最多 1000 个已排好的任务
                # 照跑**，4 个 worker 会一路把它们全部传完。小文件场景下那就是几分钟的
                # 持续滚动 —— 用户看到的是"暂停没反应"，点"继续"更看不出区别（worker
                # 从头到尾就没停过）。
                #
                # 等在 get **之前**，语义正好是按钮该有的含义"在飞的那个传完、剩下的
                # 不再开始"：worker 一旦取走就会做完（半途停下只会留 .part 残渣且零收益），
                # 所以判据只能放在取任务这一侧，不能放在 handler 里。
                # 停止路径不受影响：_toggle_stop / _abort_on_consec_failures 都会
                # 顺带 set 本事件，wait 立刻返回，下面那次 stop 判据接住。
                self._pause_event.wait()
                if self._stop_event.is_set():
                    break
                try:
                    task = task_queue.get(timeout=0.1)
                except queue.Empty:
                    # 队列空：可能是生产者还没派完（继续等），也可能是派完了（收工）。
                    # 生产者结束标志是唯一的判据，不能靠"等够久了"来猜。
                    if producer_done.is_set():
                        break
                    continue
                try:
                    status = handler(conn, task)
                except Exception as e:
                    # handler 自己负责埋点与记日志；这里只兜住"连埋点都崩了"的极端情况，
                    # 绝不能让一个文件的异常把整个 worker 线程带走（那是并发版的"一损俱损"）。
                    self._ui._threadsafe_log(f"[错误] worker{worker_id} 处理文件时异常：{e}")
                    status = "fail"
                result_queue.put((status, task[0]))
        finally:
            pool.release(conn)

    # 【阶段3-新增】进度投递口。按 FTP_PROGRESS_MAX_MSGS 算出的步长投递，
    # 避免 20 万文件产生 20 万条 UI 消息把那点刷新预算全吃掉；
    # 末条必投（done >= total），且 _poll_progress 仍是兜底心跳，所以不会丢进度。
    def _post_progress(self, done):
        # 【阶段4-必须做】总数未知（=0）时**不发**消息。
        # 流式扫描期间进度条是跑马灯，value 这个量根本不被解释；更关键的是
        # 下面的"末条必投"判据是 done >= self._total —— _total 还是 0 的时候
        # 它对**每个**文件都成立，20 万文件会灌 20 万条 UI 消息进来，
        # 正好把阶段3 用步长省下来的那点预算全部倒回去。
        if self._total <= 0:
            return
        stride = self._progress_stride or 1
        if done % stride == 0 or done >= self._total:
            self._ui._post_ui("progress", done, self._total)

    # ---- 远端目录解析/探测：与 _ftp_download_worker_single 里的逻辑同源 ----
    @staticmethod
    def _ftp_remote_dir_from_url(remote_url, ftp_cfg):
        if remote_url.startswith('ftp://'):
            parts = remote_url.split('/', 3)
            remote_dir = ('/' + parts[3]) if len(parts) >= 4 else '/'
        elif remote_url:
            remote_dir = remote_url if remote_url.startswith('/') else '/' + remote_url
        else:
            remote_dir = ftp_cfg.get('remote_dir', '/')
        return '/' + remote_dir.lstrip('/')

    @staticmethod
    def _ftp_remote_base_from_url(remote_url):
        if remote_url.startswith('ftp://'):
            parts = remote_url.split('/', 3)
            base = ('/' + parts[3]) if len(parts) >= 4 else '/'
        else:
            base = remote_url if remote_url.startswith('/') else '/' + remote_url
        return '/' + base.lstrip('/')

    def _ftp_probe_remote_dir(self, conn, remote_dir):
        """试进入候选目录，返回实际可用的绝对路径；都进不去返回 None。
        原目录与"去掉开头斜杠"两个候选，与单连接实现逐字一致。"""
        for candidate in (remote_dir, remote_dir.lstrip('/')):
            if candidate == '':
                candidate = '/'
            self._ui._threadsafe_log(f"尝试进入: {candidate}")
            try:
                conn.cwd(candidate)
                actual = candidate if candidate.startswith('/') else '/' + candidate
                self._ui._threadsafe_log(f"成功进入: {actual}")
                return actual
            except ftplib.error_perm as e:
                self._ui._threadsafe_log(f"进入失败: {e}")
                continue
        return None

    # ---- 阶段4：两条流式源 ----
    def _ftp_filter_stream(self, stream, extensions):
        """【阶段4】按扩展名过滤远端流。

        与阶段2c 那次列表推导（`[f for f in all_files if Path(f[0]).suffix.lower()
        in extensions]`）语义完全相同，只是从"对整份清单滤一遍"改成"边扫边滤"——
        否则流式扫描省下来的内存又被一份过滤后的清单吃回去了。
        """
        for item in stream:
            if Path(item[0]).suffix.lower() in extensions:
                yield item

    def _iter_local_files(self, root, extensions):
        """【阶段4】本地源的流式清单（上传路径用）。

        os.walk 本来就是惰性的，原来落成 list 只为先数出总数。元组形状
        (完整路径, 相对目录, 文件名) 与阶段1 起的那份清单逐字段相同。

        【阶段一 Bug2-修改】由 staticmethod 改为实例方法：要往 self._scan_dirs
        里记"已扫目录数"。本地路径与远端路径在这件事上**口径必须一致** ——
        状态栏那句文案是共用的，只有一边计数的话，上传任务会一直显示"已扫 0 个目录"，
        看着像卡死。os.walk 每个目录产出一轮（含空目录），与 _walk_remote_files
        的"每列举完一个目录自增一次"逐条对应。
        """
        pruner = self._core._date_pruner       # 【P0-1】生成器体内只读一次（它不是热路径）
        for dirpath, dirs, filenames in os.walk(root):
            self._core._date_prune_walk(pruner, root, dirpath, dirs)
            self._scan_dirs[0] += 1      # 【阶段一 Bug2】先记数，再产出本目录的文件
            for fname in filenames:
                full = os.path.join(dirpath, fname)
                if config._match_filter(full, extensions):
                    yield (full, os.path.relpath(dirpath, root), fname)

    # ---- 任务生产者 ----
    def _ftp_download_producer(self, files_stream, actual_remote_dir, local_dst, flatten):
        """把扫描结果展开成任务元组：(序号, 远端路径, 大小, mtime, 精度, 本地目标路径)。

        路径推算留在**协调线程**里做（不放进 N 个 worker）：它是纯本地计算，
        每个 worker 各算一遍只是重复劳动，还会把"目录还没建好"的竞态引进来。

        【阶段4】入参从"整棵树的 list"换成"逐目录推进的生成器"（惰性求值）。
        本函数体除参数名外一行未改——它本来就是 for 循环，流的形态对它透明。
        """
        handed_out = set()          # 见下方扁平化重名说明
        actual_stripped = (actual_remote_dir or '/').lstrip('/')
        for i, (remote_path, r_size, _r_raw, r_mtime, r_prec) in enumerate(files_stream, 1):
            remote_stripped = remote_path.lstrip('/')
            if remote_stripped.startswith(actual_stripped):
                rel = remote_stripped[len(actual_stripped):].lstrip('/')
            else:
                rel = os.path.basename(remote_path)

            if flatten:
                local_path = self._core._unique_path(local_dst, os.path.basename(remote_path))
                # 【阶段3-必须做｜并发引入的新竞态】_unique_path 是**看磁盘**判重的
                # （"目标已存在就加 _1"）。顺序传输时上一个同名文件已经落盘，所以
                # 第二个能拿到 _1；并发后生产者会提前上千个文件规划路径，那时前面的
                # 同名文件还没落盘，两次规划会得到**同一个目标路径**——
                # 后传的静默覆盖先传的，两边都记为 done。
                # 所以在"看磁盘"之外再叠一层"本次已规划"的集合。
                base, ext = os.path.splitext(local_path)
                n = 1
                while local_path in handed_out:
                    local_path = f"{base}_{n}{ext}"
                    n += 1
                handed_out.add(local_path)
            else:
                local_path = os.path.join(local_dst, rel)
                os.makedirs(os.path.dirname(local_path), exist_ok=True)
            yield (i, remote_path, r_size, r_mtime, r_prec, local_path)

    def _ftp_upload_producer(self, files_stream, remote_base, flatten):
        """(序号, 本地源, 远端目录, 文件名)。远端目录的建立放在 worker 里做，
        由 FTPPooledConn._known_dirs 保证每个目录整条任务只 MKD 一次。
        【阶段4】入参同样是流（见 _ftp_download_producer）。"""
        for i, (full_path, rel_dir, fname) in enumerate(files_stream, 1):
            if flatten or rel_dir == ".":
                remote_dir = remote_base
            else:
                remote_dir = remote_base.rstrip('/') + '/' + rel_dir.replace('\\', '/')
            yield (i, full_path, remote_dir, fname)

    # ---- 单文件处理（worker 侧，每个 worker 独立执行） ----
    def _ftp_download_one(self, conn, task):
        seq, remote_path, r_size, r_mtime, r_prec, local_path = task
        name = os.path.basename(remote_path)

        # 【阶段2c】可信续传判定（方案 §3.1 六条件）。源属性全部来自 FTP 侧
        # 的五元组：远端 stat 拿不到，本地 stat 拿到的也不是同一回事。
        # r_mtime / r_prec 为 None（服务器没给 modify= 或 LIST 格式不识别）时
        # 条件 ④ 必然不成立 → 不跳过 → 重传。这是设计上的安全降级。
        if self._core._should_skip(remote_path, local_path, "copy", r_prec, r_size, r_mtime):
            with self._lock:
                self._core._skipped += 1
                self._core._completed += 1
                done = self._core._completed
            self._post_progress(done)
            # 【阶段4】总数未知时不打这条（否则是"(10/0)"这种没意义的行）。
            # 扫描期间有"已扫描到 N 个文件…"那条粗粒度线兜着，日志不会静默。
            if self._total and (done % 10 == 0 or done == self._total):
                self._ui._threadsafe_log(f"({done}/{self._total}) 跳过已传: {name}")
            return "skip"

        # 【阶段一 Bug3】"第一个文件开始传输"事件。位置在**跳过判定之后、真正搬
        # 字节之前** —— 跳过的文件一个字节都没搬，它不该让状态栏从"扫描中"切到速度。
        self._ui._note_transfer_start()
        self._ui._threadsafe_log(f"正在下载: {remote_path} -> {local_path}")
        try:
            # 【阶段2c】原子落盘：.part → os.replace，并在其中用 os.utime 把本地
            # mtime 对齐到远端值（否则续传判定失去可比基准）。
            # 【阶段3】入参由 FTPHelper 换成 FTPPooledConn：同名同签名，本函数一行未改。
            self._core._atomic_write_download(conn, remote_path, local_path, r_mtime)
        except Exception as e:
            self._ui._threadsafe_log(f"下载失败: {remote_path} - {e}")
            # 【阶段2a】失败埋点：src 用远端路径，filename 取其 basename
            # 【阶段2c】附 FTP 侧源属性，让 _db_dispatch 不必从本地目标回推精度
            self._core._db_enqueue(("failed", remote_path, local_path, str(e),
                              {"size": r_size, "mtime": r_mtime, "precision": r_prec}))
            # 【阶段2d】连续失败阈值（Q8）：埋点先于中止，计数不漏。
            # 并发下 _note_transfer_result 走 self._lock，N 个 worker 数的是同一个计数器。
            if self._core._note_transfer_result(False):
                self._core._abort_on_consec_failures()
            return "fail"

        with self._lock:
            self._core._completed += 1
            done = self._core._completed
        self._core._db_enqueue(("done", remote_path, local_path,
                          {"size": r_size, "mtime": r_mtime, "precision": r_prec}))
        self._core._note_transfer_result(True)      # 【阶段2d】成功即清零
        # 【功能B】2/5 记账点：FTP→本地 的并发 worker。字节数用**远端 stat 给的
        # `r_size`**（扫描阶段就拿到了），不是本地回读 —— 回读要多一次 I/O。
        self._ui._note_bytes(r_size)
        self._post_progress(done)
        if self._total and (done % 10 == 0 or done == self._total):   # 【阶段4】同上
            self._ui._threadsafe_log(f"({done}/{self._total}) {name}")
        return "ok"

    def _ftp_upload_one(self, conn, task):
        seq, full_path, remote_dir, fname = task
        remote_path = remote_dir.rstrip('/') + '/' + fname

        # 【阶段5-新增 上传路径续传】2c 留下的缺口：上传从来不判跳过，断了就得从头全传。
        # 判据与下载路径**同一套** _should_skip，唯一区别是第 ⑥ 条的目标完整性由
        # conn.size() 去远端问（本地 os.path.getsize 够不着远端路径）。
        # 前五条（库里有记录 / 上次是 done / 源 size 没变 / mtime+精度没变 / 目标路径没变）
        # 用的都是本地 stat 与库里的值，对上传路径同样成立。
        # 注意 mode 传 "copy"：上传**不会**删本地源，所以 ⑥' 那条 move 专用判据不适用。
        sp, ss, sm = config._stat_for_state(full_path)
        if config.UPLOAD_RESUME_ENABLED and self._core._should_skip(
                full_path, remote_path, "copy", sp, ss, sm,
                dst_probe=lambda: conn.size(remote_path)):
            with self._lock:
                self._core._skipped += 1
                self._core._completed += 1
                done = self._core._completed
            self._post_progress(done)
            # 与下载路径同构：总数未知（还在扫）时不打这条，否则是 "(10/0)" 这种没意义的行。
            if self._total and (done % 10 == 0 or done == self._total):
                self._ui._threadsafe_log(f"({done}/{self._total}) 跳过已传: {fname}")
            return "skip"

        # 【阶段一 Bug3】同下载路径：跳过判定之后、真正上传之前（见那里的说明）。
        self._ui._note_transfer_start()
        try:
            conn.ensure_remote_dir(remote_dir)      # 【阶段3】带 _known_dirs 缓存
            conn.upload_file(full_path, remote_path)
        except Exception as e:
            self._ui._threadsafe_log(f"上传失败: {full_path} - {e}")
            sp, ss, sm = config._stat_for_state(full_path)
            self._core._db_enqueue(("failed", full_path, remote_path, str(e),
                              {"size": ss, "mtime": sm, "precision": sp}))
            # 【阶段3】保留"任一文件失败即中止整个任务"的既有语义：
            # 置停止让其余 worker 在下一个文件边界停下，协调线程按 fail>0 走 error 终态。
            # 同时解开暂停阻塞，否则暂停中点失败会一直卡在 wait 上。
            self._stop_event.set()
            self._pause_event.set()
            return "fail"

        with self._lock:
            self._core._completed += 1
            done = self._core._completed
        # 【阶段2c】成功埋点。源属性取本地 stat（准）；目标属性取 FTP 侧回读。
        sp, ss, sm = config._stat_for_state(full_path)
        self._core._db_enqueue(("done", full_path, remote_path,
                          {"size": ss, "mtime": sm, "precision": sp,
                           "dst_size": conn.size(remote_path)}))
        # 【功能B】3/5 记账点：本地→FTP 的并发 worker。`ss` 就是上面刚取的本地 stat 大小。
        self._ui._note_bytes(ss)
        self._post_progress(done)
        if self._total and (done % 10 == 0 or done == self._total):   # 【阶段4】同上
            self._ui._threadsafe_log(f"({done}/{self._total}) {fname}")
        return "ok"

    # 【阶段1-修改原因】共 7 处 root.after 落地：
    #   4 处终态（进目录失败 / 过滤后为空 / 正常结束 / 异常）→ flush + _post_ui；
    #   2 处进度（progress_range + progress_start）→ _post_ui；
    #   1 处日志（每 10 个文件）→ _threadsafe_log。
    # 顺带把 _completed 的读取移到 self._lock 内取快照后再投递，
    # 与 _transfer_one 保持一致（原来是在锁外读、再在锁外投递）。
    #
    # 【阶段3-修改原因】本函数改名并**整体下沉为回滚路径**：新实现是
    # "1 个扫描线程 + N 个 worker + 连接池"（_ftp_download_worker 现在是分发口）。
    # 这里的代码一行未动——_FTP_POOL_ENABLED=False 时跑的必须是当年验过的那套。
    def _ftp_download_worker_single(self, ftp_cfg, local_dst, extensions, remote_url, flatten):
        try:
            # 【阶段二】回滚路径同样带上超时快照。**位置参数一个没动**，只追加关键字。
            #
            # 【阶段二-修正 2026-09-19】这里原先还追加了 `reconnect_attempts=` —— **那是错的**。
            #   本行调的是 `ftp_client.FTPHelper`，签名是
            #       `__init__(self, host, user='', password='', port=21, timeout=10, data_idle_timeout=None)`
            #   **没有** `reconnect_attempts`；有这个参数的是 `FTPPooledConn` / `FTPConnectionPool`
            #   （所以池路径那两处是对的）。写成这样之后，`_FTP_POOL_ENABLED=False` 这条回滚路径
            #   在 connect 之前就 TypeError，任务以 'error' 中止、0 个文件传出去。
            #   实测由 stage3 的 D4/D5（上传）与 H2/H3（下载）抓到，两分钟可复现。
            # 单连接实现本来就没有重连逻辑（`_reconnect_attempts` 只活在 FTPPooledConn 里），
            # 所以这里**不需要**它：删掉即回到基线行为，一个参数都没少。
            ftp = ftp_client.FTPHelper(ftp_cfg['host'], ftp_cfg['user'], ftp_cfg['password'],
                                       ftp_cfg['port'], timeout=self._core._task_ftp_timeout)
            ftp.set_log_callback(self._ui._threadsafe_log)
            ftp.connect()

            if remote_url.startswith('ftp://'):
                parts = remote_url.split('/', 3)
                if len(parts) >= 4:
                    remote_dir = '/' + parts[3]
                else:
                    remote_dir = '/'
            elif remote_url:
                remote_dir = remote_url if remote_url.startswith('/') else '/' + remote_url
            else:
                remote_dir = ftp_cfg.get('remote_dir', '/')

            # 标准化为绝对路径
            remote_dir = '/' + remote_dir.lstrip('/')
            self._ui._threadsafe_log(f"使用的远程目录: {remote_dir}")

            # 尝试进入目录，确定实际可用的绝对路径
            actual_remote_dir = None
            for candidate in (remote_dir, remote_dir.lstrip('/')):
                if candidate == '':
                    candidate = '/'
                self._ui._threadsafe_log(f"尝试进入: {candidate}")
                try:
                    ftp.ftp.cwd(candidate)
                    actual_remote_dir = candidate if candidate.startswith('/') else '/' + candidate
                    self._ui._threadsafe_log(f"成功进入: {actual_remote_dir}")
                    break
                except ftplib.error_perm as e:
                    self._ui._threadsafe_log(f"进入失败: {e}")
                    continue

            if actual_remote_dir is None:
                self._ui._threadsafe_log("无法进入目录，请检查 FTP 设置中的远程目录")
                try:
                    ftp.disconnect()
                except Exception:
                    pass
                # 本分支尚未扫描，self._total 可能残留上一个任务的值，故显式传 0
                self._core._db_end_task("done", "finished", total_seen=0)
                return

            # 递归获取所有文件（路径均以 / 开头）
            all_files = ftp.list_files_recursive(actual_remote_dir)
            self._ui._threadsafe_log(f"扫描完成，找到 {len(all_files)} 个文件")

            if extensions:
                # 【阶段2c】五元组过滤：保留 size/mtime_raw/mtime_norm/precision
                all_files = [f for f in all_files if Path(f[0]).suffix.lower() in extensions]
                self._ui._threadsafe_log(f"过滤后剩余 {len(all_files)} 个文件")

            self._total = len(all_files)
            if self._total == 0:
                self._ui._threadsafe_log("没有符合过滤条件的文件")
                try:
                    ftp.disconnect()
                except Exception:
                    pass
                self._core._db_end_task("done", "finished", total_seen=self._total)   # 此时必为 0
                return

            self._ui._post_ui("progress_range", self._total)
            self._ui._post_ui("progress_start")

            fail_count = 0
            skip_count = 0
            for i, (remote_path, r_size, _m_raw, r_mtime, r_prec) in enumerate(all_files, 1):
                self._pause_event.wait()
                # 【阶段2d-修复】本循环原先**只等 pause_event、不看 stop_event**，
                # 于是"停止"按钮对 FTP→本地 完全无效：_toggle_stop 置了 stop_event，
                # 循环却一路把剩余文件下完（实测：只会停在整批结束）。
                # 连续失败中止（Q8）也依赖这个判定才停得下来，两条一起补。
                if self._stop_event.is_set():
                    break
                # remote_path 已经是完整的绝对路径，如 /data/file.txt
                # 计算相对路径用于构建本地路径
                remote_stripped = remote_path.lstrip('/')
                actual_stripped = actual_remote_dir.lstrip('/')
                if remote_stripped.startswith(actual_stripped):
                    rel = remote_stripped[len(actual_stripped):].lstrip('/')
                else:
                    # 如果路径不匹配，直接使用文件名
                    rel = os.path.basename(remote_path)

                if flatten:
                    local_path = self._core._unique_path(local_dst, os.path.basename(remote_path))
                else:
                    local_path = os.path.join(local_dst, rel)
                    os.makedirs(os.path.dirname(local_path), exist_ok=True)

                # 【阶段2c】可信续传判定（方案 §3.1 六条件）。源属性全部来自 FTP 侧
                # 的五元组：远端 stat 拿不到，本地 stat 拿到的也不是同一回事。
                # r_mtime / r_prec 为 None（服务器没给 modify= 或 LIST 格式不识别）时
                # 条件 ④ 必然不成立 → 不跳过 → 重传。这是设计上的安全降级。
                if self._core._should_skip(remote_path, local_path,
                                     "copy", r_prec, r_size, r_mtime):
                    with self._lock:
                        self._core._skipped += 1
                        self._core._completed += 1
                        skip_count += 1
                        done = self._core._completed
                    if done % 10 == 0 or done == self._total:
                        self._ui._threadsafe_log(
                            f"({done}/{self._total}) 跳过已传: {os.path.basename(remote_path)}")
                    continue

                # 直接使用完整的远程绝对路径下载，不再切换目录
                self._ui._threadsafe_log(f"正在下载: {remote_path} -> {local_path}")
                try:
                    # 【阶段2c】改走原子落盘：.part → os.replace，并在其中用 os.utime
                    # 把本地 mtime 对齐到远端值（否则续传判定失去可比基准）。
                    self._core._atomic_write_download(ftp, remote_path, local_path, r_mtime)
                except Exception as e:
                    self._ui._threadsafe_log(f"下载失败: {remote_path} - {e}")
                    fail_count += 1
                    # 【阶段2a】失败埋点：src 用远端路径，filename 取其 basename，
                    # 与 2b 按"文件名唯一"建索引的模型一致
                    # 【阶段2c】附 FTP 侧源属性，让 _db_dispatch 不必从本地目标回推精度
                    # （回推会把 'ns' 写进库，续传判定永远不成立）
                    self._core._db_enqueue(("failed", remote_path, local_path, str(e),
                                      {"size": r_size, "mtime": r_mtime,
                                       "precision": r_prec}))
                    # 【阶段2d】连续失败阈值（Q8）：断网/服务器拒写时不该把
                    # 剩余几万个文件逐个耗完再退出。埋点先于中止，计数不漏。
                    if self._core._note_transfer_result(False):
                        self._core._abort_on_consec_failures()
                    continue

                with self._lock:
                    self._core._completed += 1
                    done = self._core._completed
                # 【阶段2c】同上：附 FTP 侧源属性
                self._core._db_enqueue(("done", remote_path, local_path,
                                  {"size": r_size, "mtime": r_mtime,
                                   "precision": r_prec}))
                self._core._note_transfer_result(True)      # 【阶段2d】成功即清零
                # 【功能B】5/5 记账点：FTP→本地 的单线程顺序分支（并发不可用时的回退路径）。
                self._ui._note_bytes(r_size)
                if done % 10 == 0 or done == self._total:
                    self._ui._threadsafe_log(f"({done}/{self._total}) {os.path.basename(remote_path)}")

            ftp.disconnect()
            # 方案 §10 的 2c 验收项要求日志能直接看到命中跳过数
            if skip_count:
                self._ui._threadsafe_log(
                    f"状态库命中跳过 {skip_count} 个，实际下载 {self._total - skip_count} 个")
            # 【阶段2d】end_reason 改用 _end_reason()：原先是硬编码的 "finished"，
            # 停止/连续失败中止时库里也会记成正常完成，事后追查看不出区别。
            self._core._db_end_task("done", self._core._end_reason(), total_seen=self._total,
                              failed_count=fail_count)
        except Exception as e:
            self._core._db_end_task("error", "error", msg=str(e))

    # ---- FTP 上传 ----
    def _start_local_to_ftp(self, extensions):
        src = self.source_path.get().strip()
        cfg = self.ftp_dest_config
        remote_url = self.dest_path.get().strip()   # Tk 变量在主线程读取
        flatten = self.flatten.get()
        workers = self._ftp_pool_workers()          # 【阶段3】同上，主线程读 Tk
        self._ui._update_log("[参数] 本次并发 = %d"
                             % (workers if config._FTP_POOL_ENABLED else 1))
        threading.Thread(target=self._ftp_upload_worker, args=(src, cfg, extensions, remote_url, flatten, workers), daemon=True).start()

    # 【阶段2c-已补】本函数原先**刻意不加状态库埋点**（2a 阶段唯一的传输路径豁免），
    # 因为上传的"目标文件属性"必须来自 FTP 侧，本地 stat 拿不到。
    # 2c 已接上：每文件 done/failed 埋点，src 属性取本地 stat，
    # dst_size 取 FTPHelper.size() 回读（远端自检值）。
    #
    # 【2c 已知边界】上传路径**不使用**跳过判定：_should_skip 的第 ⑥ 条要
    # os.path.getsize(dst_path)，而上传的目标是远端路径，本地根本 stat 不到。
    # 因此 local→ftp 目前仍是"每次都全量上传"（与阶段1 行为一致，不倒退）。
    # 让上传也能续传需要一个走 ftp.size() 的远端判定分支——不在 2c 验收范围内
    # （§10 的 2c 验收只要求 ftp→local 不重传），留待后续阶段。
    #
    # 【阶段2a-必须偏离设计文档】3 处终态仍**必须**改走 _db_end_task（已改）。
    # 设计文档"未改：_ftp_upload_worker"的理由只覆盖"每文件 dst_* 埋点"，
    # 漏了一个致命点：_start_transfer 对 local→ftp 同样会 claim 任务并启动 db-writer，
    # 而 db-writer 只在收到 __task_end__ 时才 return。若终态仍走 _post_ui，
    # 该线程会永远空转刷心跳 → tasks 行永远 running → 任务级锁被永久占住，
    # 此后任何窗口的任何传输都会被"已有任务在运行"拒绝（实测复现，见 2a-E）。
    # 【阶段1-修改原因】与 _ftp_download_worker 同构：3 处终态 + 2 处进度 + 1 处日志。
    # 同样把 _completed 的读取收进 self._lock 取快照。
    #
    # 【阶段3-修改原因】与 _ftp_download_worker 同构：改名并下沉为回滚路径，
    # 新实现见 _ftp_upload_worker（分发口）。本函数体一行未动。
    def _ftp_upload_worker_single(self, local_src, ftp_cfg, extensions, remote_url, flatten):
        try:
            # 【阶段二】回滚路径同样带上超时快照。**位置参数一个没动**，只追加关键字。
            #
            # 【阶段二-修正 2026-09-19】与 `_ftp_download_worker_single` 同一处错误：
            #   原先追加的 `reconnect_attempts=` 在 `ftp_client.FTPHelper` 上不存在
            #   （有它的是 `FTPPooledConn` / `FTPConnectionPool`），回滚路径直接 TypeError。
            #   删除理由与下载侧逐字相同：单连接实现没有重连逻辑，这个参数本来就用不上。
            ftp = ftp_client.FTPHelper(ftp_cfg['host'], ftp_cfg['user'], ftp_cfg['password'],
                                       ftp_cfg['port'], timeout=self._core._task_ftp_timeout)
            ftp.set_log_callback(self._ui._threadsafe_log)
            ftp.connect()

            if remote_url.startswith('ftp://'):
                parts = remote_url.split('/', 3)
                if len(parts) >= 4:
                    remote_base = '/' + parts[3]
                else:
                    remote_base = '/'
            else:
                remote_base = remote_url if remote_url.startswith('/') else '/' + remote_url

            # 标准化为绝对路径
            remote_base = '/' + remote_base.lstrip('/')
            self._ui._threadsafe_log(f"确保远程目录存在: {remote_base}")
            ftp.ensure_remote_dir(remote_base)

            if os.path.isfile(local_src):
                files = [(local_src, "", os.path.basename(local_src))]
            else:
                files = []
                # 【P0-1】回滚路径（`_FTP_POOL_ENABLED=False`）也要剪 —— 它就是
                #   当初的正式实现，剪枝是**功能**不是并发特性：漏了这里，用户
                #   一旦切回单连接，日期分层会**静默地不生效**（只是每次都全量慢扫）。
                pruner = self._core._date_pruner
                for dirpath, dirs, filenames in os.walk(local_src):
                    self._core._date_prune_walk(pruner, local_src, dirpath, dirs)
                    for fname in filenames:
                        full = os.path.join(dirpath, fname)
                        if config._match_filter(full, extensions):
                            rel = os.path.relpath(dirpath, local_src)
                            files.append((full, rel, fname))

            self._total = len(files)
            if self._total == 0:
                self._ui._threadsafe_log("没有符合过滤条件的文件")
                self._core._db_end_task("done", "finished", total_seen=self._total,
                                  folder_count=0, matched=0, stopped=False)
                return

            self._ui._post_ui("progress_range", self._total)
            self._ui._post_ui("progress_start")

            for i, (full_path, rel_dir, fname) in enumerate(files, 1):
                self._pause_event.wait()
                if flatten:
                    remote_path = remote_base.rstrip('/') + '/' + fname
                else:
                    if rel_dir == ".":
                        remote_dir = remote_base
                    else:
                        remote_dir = remote_base.rstrip('/') + '/' + rel_dir.replace('\\', '/')
                    ftp.ensure_remote_dir(remote_dir)
                    remote_path = remote_dir.rstrip('/') + '/' + fname
                # 上传时也使用绝对路径
                try:
                    ftp.upload_file(full_path, remote_path)
                except Exception as e:
                    # 【阶段2c】失败埋点后**照旧向上抛**：上传路径的既有语义是
                    # "任一文件失败即中止整个任务"（阶段1 行为，外层的 except 会投 error 终态）。
                    # 本阶段只补记录，不改这个语义——改成"跳过继续"是行为变更，不在 2c 范围。
                    self._ui._threadsafe_log(f"上传失败: {full_path} - {e}")
                    sp, ss, sm = config._stat_for_state(full_path)
                    self._core._db_enqueue(("failed", full_path, remote_path, str(e),
                                      {"size": ss, "mtime": sm, "precision": sp}))
                    raise
                with self._lock:
                    self._core._completed += 1
                    done = self._core._completed
                # 【阶段2c】成功埋点。源属性取本地 stat（准）；目标属性取 FTP 侧回读：
                # 本地 stat 拿不到远端，硬填 NULL 会让续传判定永远"不一致"（见本函数头部注释）。
                sp, ss, sm = config._stat_for_state(full_path)
                self._core._db_enqueue(("done", full_path, remote_path,
                                  {"size": ss, "mtime": sm, "precision": sp,
                                   "dst_size": ftp.size(remote_path)}))
                # 【功能B】4/5 记账点：上传的单线程顺序分支（并发不可用时的回退路径）。
                self._ui._note_bytes(ss)
                if done % 10 == 0 or done == self._total:
                    self._ui._threadsafe_log(f"({done}/{self._total}) {fname}")
            ftp.disconnect()
            self._core._db_end_task("done", "finished", total_seen=self._total)
        except Exception as e:
            self._core._db_end_task("error", "error", msg=str(e))
