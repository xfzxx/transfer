# -*- coding: utf-8 -*-
r"""后台三件套（`MaintenanceWorker`）—— 从 `FileTransferApp` 里切出来的第 4 个真模块。

【它管什么】**维护 / 清理 / 回收** 三件套（`_nightly\55 §三` 第 4 步：
"只做回收/维护/清理三件套；db-writer 已在第 2 步归 core"）：
  · 库存维护  —— 启动 1 秒后跑一次，清过期行 + 空间回收（`state_db.maintenance`）
  · 清空状态库 —— Tab2 那个 danger 按钮（二次确认 + 后台 VACUUM + 回主线程收尾）
  · `.part` 回收 —— 上传任务启动时扫远端目标树、删过期半截文件

【层级】**L4** —— 只依赖 `transfer_core`(L2) / `ui_channel`(L1) / L0（config·state_db·ftp_client）。
      与 `ftp_transfer`(L3) **互不依赖**。

【装配】宿主把 `_core` / `_db` / `_stop_event` **同对象**注入（所以新类里 `self._core.X`
      这些行**一字不用改**，而且读的是**同一个对象** ⇒ 天然实时，不存在"按值快照不更新"）；
      `_ui` 与两个控件（`purge_btn` / `retry_btn`）在构造时还不存在 ⇒ 由宿主在 `_build_ui()`
      之后 `attach(ui, purge_btn, retry_btn)` **一次性**补挂（只许挂一次）。
      ⚠️ 入口名是**唯一的 `attach`**，**不是** core/ftp 那样的 `attach_host_widgets` / `attach_ui`
      —— 理由见 `attach()` 的 docstring（手段 5 的「同名两份」判据会误伤，实测有据）。

【Tk 变量为什么是"零参可调用"而不是值】`db_keep_days` / `purge_overwrites` / `date_template`
      是三个 Tk 变量，宿主里读它们的位置有**明确次序**（在 `DB_MAINT_ENABLED` /
      `_STATE_DB_ENABLED` / `_task_active` 三道守卫**之后**）。收下"值"会把这个次序改掉，
      收下"可调用"则一切照旧 —— 这是本模块**不持有 Tk 变量**又**不改行为**的唯一办法。
"""
import threading
import time

import tkinter.messagebox as messagebox      # ｜无头模式下宿主会改绑本模块的这个名字
                                             # ｜（`_install_headless_dialogs()`，见宿主 @330 附近）

import config
import ftp_client


class MaintenanceWorker(object):
    """后台三件套。装配见模块 docstring。"""

    def __init__(self, core, db, stop_event, keep_days_of,
                 purge_overwrites_of, date_template_of):
        self._core = core
        self._db = db
        self._stop_event = stop_event
        # 【雷⑥】Tk 变量的**惰性取值器** —— 本模块只持"可调用"，不持 IntVar/BooleanVar/StringVar；
        #   求值发生在**原有那一行**（依次序在守卫之后），且仍在主线程。
        self._keep_days_of = keep_days_of
        self._purge_overwrites_of = purge_overwrites_of
        self._date_template_of = date_template_of
        # 归本类自己：`.part` 回收的观测槽（宿主不再持有）
        self._part_reclaim = None
        # `_ui` 与两个控件由宿主在 `_build_ui()` 之后一次性补挂（构造时它们还不存在）
        self._ui = None
        self._attached = False
        self.purge_btn = None
        self.retry_btn = None

    def attach(self, ui, purge_btn, retry_btn):
        r"""宿主在 `_build_ui()` 之后**一次性**补挂 `_ui` 与两个控件 —— **只许挂一次**。

        【为什么入口名是唯一的 `attach`，不照抄 core 的 `attach_host_widgets` / ftp 的
          `attach_ui`】手段 5（`_verify_split_ast`）有一条判据：
          「参照版里**唯一**的裸名，在当前版变成**两份** ⇒ 判红」
          （它抓的是"搬过去了、旧的没删 ⇒ 两份锁 / 两份计数器"，本工具 docstring 有写）。
          可那条判据只看**裸名计数**：本类这两个入口是**新增**方法，但 `attach_ui` /
          `attach_host_widgets` 在参照版里**各已有一份**（`FtpTransfer.attach_ui` /
          `TransferCore.attach_host_widgets`）⇒ 沿用旧名会被**误伤成 ambiguous**。
          实测证据（只读探针，喂合成限定名表、不碰真树）：`_tmp\_step4_amb_probe.py`
          —— A 稿（沿用 `attach_ui` + `attach_host_widgets`）**命中 2 条 ambiguous**；
          B 稿（唯一名 `attach`）**0 条**。
          另外，代码库既有约定本来就是"**每个模块一个互不相同的 attach 名**"
          （core = `attach_host_widgets`、ftp = `attach_ui`）⇒ 取唯一名也是**跟着约定**，
          不是绕着判据走。
        """
        assert not self._attached, "MaintenanceWorker 已经挂过了（重复挂 = 两份引用）"
        self._ui = ui
        self.purge_btn = purge_btn
        self.retry_btn = retry_btn
        self._attached = True


    # 【阶段5-4-新增】起一根后台线程跑一次库维护。
    #
    # 【为什么不在主线程直接跑】VACUUM 要整库重写，百万行的库上这不是瞬间的事，
    # 放在主线程就是启动时界面定格；而这次维护本就不产出任何用户要等的结果。
    # 后台跑，日志区里出现一行「[库维护] ...」就完事了。
    #
    # 【为什么不 join】同上：没有任何人需要等它的结果。它用短连接自开自关，
    # 与 db-writer / worker 之间只有 sqlite 自己的锁在协调，不需要额外的握手。
    #
    # 【为什么正在传输时直接跳过】本方法只在启动 1 秒后跑一次，而此刻 `_task_active`
    # 为真的唯一途径是用户在 1 秒内点了开始（或恢复弹窗选了继续）—— 那是极小概率，
    # 但真发生了就是"一边大规模传输一边整库重写"，VACUUM 会反复撞锁、白耗三次重试，
    # 而这次维护完全可以留到下次启动。跳过是纯赚。
    def _start_db_maintenance(self):
        if not config.DB_MAINT_ENABLED:
            return
        if not config._STATE_DB_ENABLED or self._core._state_db_degraded:
            return
        if self._core._task_active:
            return
        # 【阶段二-修正 2026-09-19】取快照这一步必须在**主线程**做。
        #   本方法由 `root.after` 调起（主线程），而 `_db_maintenance_worker` 是后台线程 ——
        #   在那里读 `self.db_keep_days`（Tk IntVar）会得到
        #       `RuntimeError: main thread is not in main loop`，
        #   被 worker 的兜底 except 接住、写一行「[库维护] 未完成」就完了：
        #   **整段维护一次都没跑成，过期行一条都不清，而界面上只多一行字。**
        #   实测由 stage5 的 D12 抓到（D1~D11 全绿 —— 维护函数本身没问题，坏的只有启动路径）。
        # 与 `_task_retry_attempts` / `_task_ftp_timeout` 同一条规矩：
        #   **Tk 变量只在主线程读，后台线程读主线程取好的快照。**
        # 顺带说明：基线这里压根不传这个实参（走 `config.DB_PRUNE_AFTER_DAYS`=730 兜底），
        # 阶段二把它提到 Tab2 界面上来、从此用户填的天数真正生效 —— 这一条是**新增能力**，
        # 不是修 bug；要修的只是"在哪儿读它"。
        keep_days = self._keep_days_of()
        t = threading.Thread(target=self._db_maintenance_worker, args=(keep_days,),
                             name="db-maintenance", daemon=True)
        t.start()

    def _db_maintenance_worker(self, keep_days=None):
        """后台线程体。**整段都得兜住**：这是一根纯后台线程，异常没人接。

        `keep_days` 由主线程取好传进来，本函数**不许**碰任何 Tk 变量（见调用处的说明）。
        缺省时退回 `config.DB_PRUNE_AFTER_DAYS` —— 与 `state_db.maintenance` 自己的
        `prune_after_days=None` 兜底是同一条口径。"""
        if keep_days is None:
            keep_days = config.DB_PRUNE_AFTER_DAYS
        try:
            self._db.maintenance(log=self._ui._threadsafe_log,
                                 prune_after_days=int(keep_days))
        except Exception as e:
            try:
                self._ui._threadsafe_log(f"[库维护] 未完成：{type(e).__name__}: {e}（不影响使用）")
            except Exception:
                pass

    # 【这个按钮为什么是整页里唯一"红了"的那个控件】
    #   它删的是**跨任务累积的历史**：`files` 里是"哪些文件已传完"（清掉=下次全都重传），
    #   `tasks` 里是任务台账。这两样丢了还能重来；而 `overwrites` 是**覆盖事故的唯一
    #   线索**，丢了就再也查不出"谁把谁盖了"。所以按钮是 danger 色、默认不带上它、
    #   并且在确认框里把"这一次会删掉什么"逐条念出来。
    #
    # 【为什么确认框要逐条写表名与条数，而不是一句"确定清空吗"】
    #   "确定吗"这种框用户是闭着眼点"是"的。写成"files 12345 条 / tasks 8 条 /
    #   overwrites 3 条"之后，用户**能看出来那个勾选框到底带没带上 overwrites** ——
    #   这正是本按钮唯一的真风险点。数字是点按钮那一刻现查的（见下）。
    def _purge_state_db(self):
        if self._core._task_active:
            # 与 `_sync_purge_btn` 是两道保险：按钮的 enabled 状态是"看起来"，
            # 这一句是"按下去的一瞬间确实没有任务在跑"。少了它，某些终态路径
            # 一旦漏调 `_sync_purge_btn`，就会变成"一边传一边删记录"。
            messagebox.showwarning("清空状态库", "正在传输，不能清空状态库。\n\n"
                                   "请等这次传输结束（或先点「停止」）再试。")
            return
        if not config._STATE_DB_ENABLED:
            messagebox.showinfo("清空状态库", "状态库功能当前是关闭的（config._STATE_DB_ENABLED），没有可清空的内容。")
            return
        with_over = self._purge_overwrites_of()
        tables = ("files", "overwrites", "tasks") if with_over else ("files", "tasks")
        try:
            counts = {t: self._db.count_rows(t) for t in tables}
        except Exception:
            counts = {t: 0 for t in tables}
        # ⚠️ 【2026-09-21 修】`% counts[...]` 原来挂在**每对里的第二行**上，
        #   而那两行**自己不带格式符**（`%6d` 在上一行）——
        #   于是这一句当场抛 `TypeError: not all arguments converted during
        #   string formatting`，**确认框根本弹不出来**。
        #   症状：点「清空状态库」**什么都不发生**，异常只在 stderr 上留一行，
        #   界面没有任何提示（Tk 回调里抛的异常不会自己弹框）。
        #   罪魁是每对第一行末尾那个**逗号** —— 有它，两个字面量就是**两个列表元素**，
        #   不会隐式拼接，`%` 自然落到了没有格式符的那一个上。
        #   这个缺陷是写 E 组（裁定 B 的文案对照）时才被**真的执行到**的：
        #   在此之前，全项目没有任何一条测试点过这个按钮。
        #   ⇒ 教训：**凡是"只在用户点了才跑"的分支，写文案断言的价值不只是文案。**
        lines = [
            "即将清空传输状态库里的记录：",
            "",
            "  files      %6d 条  —— 已传完文件的记录。清掉之后，" % counts["files"],
            "                      下次遇到同样的文件会**重新传一遍**（不是删除文件本身）。",
            "  tasks      %6d 条  —— 任务台账（含上次未完成任务）。清掉之后，" % counts["tasks"],
            "                      启动时不会再询问「是否恢复上次任务」。",
        ]
        if with_over:
            lines += [
                "  overwrites %6d 条  ← 「同时清空覆盖记录」已勾选！" % counts["overwrites"],
                "                      这些是「哪个文件覆盖了哪个文件」的唯一记录，",
                "                      清掉之后再也查不回来。",
            ]
        else:
            lines += [
                "  overwrites 会原样保留（未勾选），覆盖事故的线索不受影响。",
            ]
        # 【P0-1·裁定 B（2026-09-21）】日期分层的**同步点**是唯一一处
        #   "清空之后仍然生效"的东西：它住在 `meta` 表里，而 `meta` 不在这几张表的
        #   名单里（同一个表里还住着 `schema_version`，那个**绝对不能**跟着清）。
        #   ⇒ 清了库之后，日期分层**照样**会剪掉同步点之前的目录：用户对"清空"的
        #   直觉是"下次全都重传"，实际是"下次只重传同步点之后的"。
        #   **失效方向是漏传**，所以哪怕只是文案也值得单独占一段。
        #   裁定是**不改行为** —— 连带清同步点会把"只删缓存、删了顶多重传"这个动作，
        #   变成一个会改变**后续传输范围**的动作，而范围变更正是本阶段所有数据安全
        #   事故的同一个形状。这里只把这件事念给用户听，选择权留给他。
        #   为什么只在模板非空时才显示：没启用日期分层的人看到这句纯属噪音，
        #   而确认框一旦变长，**真正要看的那两行条数反而更没人看**。
        if self._date_template_of().strip():
            lines += [
                "",
                "注意：日期分层的「同步点」不会被清掉（它不在上面这几张表里）。",
                "      同步点之前的目录，下次仍然不会被重扫 ——",
                "      要重置它，请到「定时任务」页点「重置同步点」。",
            ]
        lines += [
            "",
            "不会动的东西：磁盘上的文件、FTP 上的文件、传输日志、FTP 连接配置。",
            "清空之后无法撤销。确定继续吗？",
        ]
        if not messagebox.askyesno("清空状态库", "\n".join(lines), icon="warning", default="no"):
            return
        self.purge_btn.configure(state="disabled")
        threading.Thread(target=self._purge_worker, args=(tables,), daemon=True).start()

    def _purge_worker(self, tables):
        """后台线程体。**整段兜住** —— 与库维护同一根规矩：异常没人接。

        为什么不在主线程直接跑：`purge` 结尾要 VACUUM（整库重写）。几十万行的库上
        这不是瞬间的事，放主线程就是界面定格几秒，用户以为程序死了。
        """
        try:
            rep = self._db.purge(tables=tables, log=self._ui._threadsafe_log)
        except Exception as e:
            rep = {"ok": False, "error": "%s: %s" % (type(e).__name__, e)}
        self._ui._post_ui(lambda: self._purge_done(rep))

    def _purge_done(self, rep):
        self.purge_btn.configure(state="normal")
        self._sync_purge_btn()
        if not rep.get("ok"):
            return                      # 失败的那行已经由 _purge_summary 写进日志了
        # 清空之后内存里的失败清单**必须一起清**：`_retry_failed` 在状态库可用时
        # 读的是库（`iter_failed`），清完就是空的；而按钮此刻若还是 enabled，
        # 用户点下去会得到一次"没有可重传的文件"的空操作。
        self._core._last_failed = []
        self.retry_btn.configure(state="disabled")

    def _sync_purge_btn(self):
        """按"此刻有没有任务在跑"同步清空按钮的可用性。**唯一的状态来源是 `_task_active`**。

        为什么不逐个 `configure` 写在任务起止的每一处：那些地方已经有 6 处以上
        （开始 / 重传 / 4 条终态路径），漏掉任何一处，症状都是"传输中这个按钮还亮着"
        —— 而它删的正是运行中的任务正在写的那张表。宁可让调用方少写一行。
        """
        try:
            self.purge_btn.configure(state="disabled" if self._core._task_active else "normal")
        except Exception:
            pass                    # 建 Tab2 之前（或窗口已销毁）调用它不该炸

    # ---- 【阶段5-3-新增】远端 .part 回收 ----
    #
    # 场景（Q1 第 3 项）：上传中断会在服务器上留下 <目标名>.part。本工具的命名是
    # 可控的（恒为"完整目标名 + .part"），所以可以只清自己产生的那一类，
    # 长期累积下来的这几个 G 不至于白占空间。
    #
    # 三条安全线（缺一不可，逐条的理由见 UPLOAD_PART_RECLAIM_* 常量的注释）：
    #   ① 名字符合本工具的命名规则；② 扩展名在本次任务的过滤集里；
    #   ③ 列举结果里读得出 mtime，且比现在旧 N 天以上 —— 读不出 mtime 一律不删。
    # 第 ③ 条是防"误删另一个窗口正在写的 .part"：正在写的必然是新的，
    # 而 N 天这个阈值远大于一次任务可能持续的时间。
    #
    # 只在本任务的目标目录树内（remote_base 往下）生效，跑在自己的线程 + 自己的连接上；
    # 它出的任何问题都只写进日志与观测槽，绝不影响任务本身的终态。
    def _start_part_reclaim(self, ftp_cfg, remote_base, extensions):
        rec = {"removed": [], "scanned_dirs": 0, "deleted": 0, "capped": False,
               "no_mtime": 0, "failed": [], "error": None, "thread": None}
        self._part_reclaim = rec
        if not config.UPLOAD_PART_RECLAIM_ENABLED:
            return                      # 回滚开关：关掉即完全不碰服务器上的任何文件
        t = threading.Thread(target=self._part_reclaim_worker,
                             args=(ftp_cfg, remote_base, extensions),
                             name="part-reclaim", daemon=True)
        rec["thread"] = t
        t.start()

    def _part_reclaim_worker(self, ftp_cfg, remote_base, extensions):
        rec = self._part_reclaim
        conn = None
        try:
            conn = ftp_client.FTPPooledConn(ftp_cfg['host'], ftp_cfg['user'],
                                 ftp_cfg['password'], ftp_cfg['port'],
                                 log_callback=self._ui._threadsafe_log,
                                 timeout=self._core._task_ftp_timeout,
                                 reconnect_attempts=self._core._task_reconnect_attempts)
            cutoff = time.time() - config.UPLOAD_PART_RECLAIM_DAYS * 86400.0
            base = '/' + str(remote_base or '/').strip().replace('\\', '/').lstrip('/')
            base = base.rstrip('/') or '/'
            stack = [base]
            stop = False
            # 遍历形态与 _walk_remote_files 一致（逐目录推进、复用 list_dir 的重连重放），
            # 但没有直接复用它：回收要**边遍历边数目录数**，用现成的生成器就看不见
            # "已经列了几个目录"，也就没法在 MAX_DIRS 上刹车。
            while stack and not stop:
                if self._stop_event.is_set():
                    break                # 用户按了停止：收拾收拾就走，别赖着
                if rec["scanned_dirs"] >= config.UPLOAD_PART_RECLAIM_MAX_DIRS:
                    rec["capped"] = True
                    break
                cur = stack.pop()
                entries, subdirs = conn.list_dir(cur)
                rec["scanned_dirs"] += 1
                prefix = cur.rstrip('/')          # base='/' 时 rstrip 成 ''，正好拼出 '/name'
                for name, _size, _mraw, mnorm, _mprec in entries:
                    if not config._part_reclaim_candidate(name, extensions):
                        continue
                    if mnorm is None:
                        # 服务器没给 modify=（或只认 LIST 而那个格式解析不出时间）。
                        # 年龄判不了就不能动手：删掉的可能是别人正在写的那一份。
                        rec["no_mtime"] += 1
                        continue
                    if mnorm >= cutoff:
                        continue                  # 还不够老
                    if rec["deleted"] >= config.UPLOAD_PART_RECLAIM_MAX_DEL:
                        rec["capped"] = True
                        stop = True
                        break
                    full = prefix + '/' + name
                    try:
                        conn.delete_file(full)
                    except Exception as e:
                        # 单个删不掉（权限、别人正拿着）不该中断整轮回收
                        rec["failed"].append(f"{full}({e})")
                        continue
                    rec["deleted"] += 1
                    rec["removed"].append(full)
                if not stop:
                    for name in subdirs:
                        stack.append(prefix + '/' + name)
        except Exception as e:
            rec["error"] = str(e)
        finally:
            if conn is not None:
                try:
                    conn.close()
                except Exception:
                    pass
            line = config._part_reclaim_summary(rec)
            if line:
                self._ui._threadsafe_log(line)
