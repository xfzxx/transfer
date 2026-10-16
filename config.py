# -*- coding: utf-8 -*-
"""全局常量与纯工具函数 —— 从 file_transfer.py 抽出的第一刀（见 PROJECT_STATE §7.4）。

**本模块不含任何状态**：只有常量定义、无副作用的小函数，以及两个只在**调用时**
才做文件 I/O 的配置读写函数（`load_last_config` / `save_config`）—— 导入本模块仍然零副作用。
`file_transfer.py` 通过 `import config` 使用它们，**不做 re-export**
（`from config import X` 会让调用方拿到一份独立副本，改 config 里的值对副本无效 ——
那正是测试侧注入守护专门要挡的"静默绿"）。

搬走的原则：**它被引用，但它自己不引用任何有状态的东西**。
依赖方向只有一个：file_transfer → config，**永不反向**。
"""
import calendar
import datetime
import errno
import json
import os
import re
import time


# ===================== 配置持久化 =====================
CONFIG_FILE = os.path.join(os.path.expanduser("~"), ".ftp_transfer_config.json")

MAX_LOG_LINES = 1000

# ===================== 阶段1：线程安全 UI 通道参数 =====================
# 【回滚开关】置 False 即整体退回"后台线程直接 root.after"的旧行为。
# 保留至阶段3压测通过；阶段6摘除 False 分支时，本常量定义仍保留（历史回滚开关，已废弃）。
_UI_QUEUE_ENABLED = True

UI_DRAIN_INTERVAL_MS = 100      # 常规 drain 轮询间隔（毫秒）

UI_DRAIN_MAX_BATCH = 500        # 单次 drain 最多处理的消息条数，防止一次处理过多导致 UI 冻结

UI_QUEUE_WARN_SIZE = 20000      # 队列积压告警阈值

LOG_BATCH_SIZE = 50             # 日志聚合：条数阈值

LOG_FLUSH_INTERVAL_S = 0.3      # 日志聚合：时间阈值（秒），先到者触发

# ===================== 阶段2a：传输状态库 =====================
# 【阶段2a-修改原因】状态库回滚开关与参数。
# 两个开关存在强制耦合：_STATE_DB_ENABLED=True 时 _ATOMIC_WRITE_ENABLED 必须为 True，
# 否则会出现"库说 done、磁盘上是半截文件"的静默数据丢失（见 __main__ 的启动检查）。
# 回滚方式：_STATE_DB_ENABLED=False → 行为退回阶段1（不建库、不启 db-writer、不埋点）。
_STATE_DB_ENABLED = True

_ATOMIC_WRITE_ENABLED = True          # 【2a 仅声明，2b 生效】开关原子落盘 .part → os.replace

STATE_DB_FILE    = os.path.join(os.path.expanduser("~"), ".ftp_transfer_state.db")

# 【D-1-修改原因 1 → 2】`files` 的主键从 `(filename)` 改成 `(filename, src_root)`。
# 为什么非改不可（实测，见 PROJECT_STATE §7.9）：单列主键意味着**全库每个裸文件名只允许一行**，
# 于是"源 A\IM_0001.dcm 和 源 B\IM_0001.dcm"在**存档时就互相覆盖**了 —— 第二个源的任务跑起来
# 查到自己那行是 done，直接跳过，界面报「复制 0 个，跳过 1 个」**看起来像成功**，
# 目标盘上永远只有第一个源的数据，覆盖审计也是空的。**是静默漏传，不是报错。**
# 加一列 src_root 之后，"同名"不再等于"同一份文件"，两个源各占一行，各自独立判重。
#
# 【任务1-修改原因 2 → 3】主键再加宽一次，加到底：`(filename, src_root)` → `(src_path)`。
# 与 D-1 **同形态**，只是层级更细：上一次漏的是"同名在不同源"，这一次漏的是
# "同名在**同一个源的不同子目录**"——
#     D:\photos\2024\1.jpg 与 D:\photos\2025\1.jpg，key 都是 ('1.jpg', 'D:\photos')
# 第二份查到的是第一份写下的 done 行，size/mtime/dst_path 全对得上就命中跳过，
# 界面报「跳过 1 个」看着像成功，目标盘上**根本没有这个文件**。同样是静默漏传。
# 为什么是 `src_path` 而不是 `(src_root, rel_path)`：rel_path 要现场算、要跟扫描侧
# 逐字对齐，多一处"两边算法不一致就静默失效"的面；而 `src_path` 本来就是库里那一列
# 的真实内容（改前就在存全路径），判重与写库用的是**同一个字符串**，没有换算。
# 到此为止不再加宽：路径已经是"这个文件是谁"的最小完备答案。
DB_SCHEMA_VERSION      = 3

DB_BATCH_SIZE          = 200     # 攒够这么多条记录就提交一次

DB_COMMIT_INTERVAL_S   = 1.0     # 或者距上次提交超过这么久

DB_HEARTBEAT_INTERVAL_S = 10     # db-writer 阻塞等待的超时；空转时用它刷新心跳

DB_MAX_CONSEC_ERRORS   = 20      # 连续写库失败达到此数 → 降级为"续传能力失效"

DB_STALE_AFTER_S       = 30      # 心跳超过这么久视为陈旧，任务级锁可被抢占

# 【连接复用-新增】每线程复用读连接的开关。**这是纯性能开关，不是正确性开关。**
#
# 背景（M4 实测，`_checks\_nightly\26_M4_scan_cost.md`）：`get_file_state` 原本
# **每文件开/关一条短连接**，4000 文件量到 642.9ms —— 而整个扫描只有 838.9ms，
# 也就是说 **96.6% 的扫描成本花在"开连接"这件与数据无关的事上**。
# 同一条 SQL、同一份数据，复用一条连接只要 52.9ms（8.2%）。
#
# 置 False 即**逐字退回**老路径（每文件一条短连接，state_db._read 里那个分支）。
# 两条路径的 SQL、绑定参数、返回结构**完全相同** —— 差别只有耗时。
# 所以验收里有一条是"两条路径的查询结果**逐位比对**"，而不是"开关打开后更快就行"。
DB_REUSE_READ_CONN     = True

# 复用读连接的**空闲**存活上限（秒）。超过这么久没用过就主动关掉重开。
# 理由：一条长期挂着的连接会一直占着 WAL 的读标记，把库维护那边的
# passive checkpoint 挡住（WAL 只涨不缩）。**"不做长期持有"是本设计的既定要求**，
# 这个数就是它的落点。0 = 不限时（不推荐，仅供对照实验）。
DB_READ_CONN_MAX_IDLE_S = 120.0

# ===================== P1：判重记录的**内存预取** =====================
# 【P1-新增】`_should_skip` 判重时**真正读的那六个字段** —— 全项目**唯一**一份来源。
#
# 它为什么必须是一份共享常量（而不是各写各的）：
#   `state_db.load_skip_states()` 用它拼 SELECT，`file_transfer._should_skip`
#   用它做六条闸门。**两处一旦不同步，症状是"少读一个字段"** —— 而少读的那条
#   在 Python 里是 `None != 值` ⇒ 判定"不跳过" ⇒ 重传。方向是安全的（多传），
#   但用户会看到"明明传过的文件每次都重传"，且**没有任何一条现有判据会红**。
#   ⇒ 所以两边都从这一个 tuple 派生，另加一条静态判据
#     （`_checks\_verify_p1_prefetch.py` 的 A 组）去生产源码里把字段名读回来比。
#
# 【为什么是这六个】逐条对着 `_should_skip` 的闸门数：①②③④⑤ 各要一个字段，
# ⑤ 之后 move 分支要 `src_deleted`。`src_path` 不在里面 —— 它是**键**。
# `filename` / `task_id` / `dst_size` / `attempts` / `last_error` 也不在里面：
# `_should_skip` 一个字都没读过它们（读回来就是白占内存，20 万行 × 每个字段都算钱）。
SKIP_STATE_FIELDS = ("status", "src_size", "mtime_precision", "src_mtime",
                     "dst_path", "src_deleted")

# 字段名 → 下标。预取回来的记录是 **tuple**（不是 dict：20 万行的 dict 比 tuple
# 每行多约 200 B，实测就是"过不过 200 MB 门槛"的差别），所以闸门代码要用它取字段。
SKIP_STATE_IDX = {name: i for i, name in enumerate(SKIP_STATE_FIELDS)}

# ===================== 阶段2d：重试策略与原子上传 =====================
# 【阶段2d-新增】重试退避曲线（方案 §8）。由 0.5*(i+1)=0.5s/1.0s 改为 1/3/9。
# 1+3+9=13 秒即"单文件失败的最长耗时"，Q8 的成本核算就是按这个数做的。
# 注意：要真的用满三级退避，attempts 必须是 4（3 次等待 = 4 次尝试）；
# 沿用原来的 attempts=3 只会产生 1s/3s 两次等待，第三级 9s 永远走不到，
# 与 §8 写定的"1s/3s/9s"不符。两者一并改，否则曲线名不符实。
RETRY_BACKOFF_S = (1, 3, 9)

# 【阶段2d-新增】连续失败中止阈值（方案 Q8）。要挡的是"目标盘掉线/只读/写满"
# 这类**持续性**错误——在它上面逐个文件耗满 13 秒，20 万文件能把一整晚耗光。
# 成功一次即清零，所以"散落的坏文件"不会被误判成持续性错误（见 _note_transfer_result）。
MAX_CONSEC_FAILURES = 50

# 【阶段2d-新增】原子上传的中间后缀。与本地原子落盘同名同义，故意保持一致：
# 看到 .part 就知道"这是一份没走完的中间产物"，不需要记两套约定。
# 下次上传同一个文件时会直接 STOR 覆盖掉残留的 .part，不会累积。
UPLOAD_PART_SUFFIX = ".part"

# ===================== 阶段3：FTP 连接池与并发 =====================
# 【阶段3-新增】总回滚开关：置 False 即整体退回阶段2 的"单连接顺序传输"实现
# （_ftp_download_worker_single / _ftp_upload_worker_single，一字未动）。
# 与 _UI_QUEUE_ENABLED 同性质：保留至压测通过，阶段6 摘除。
_FTP_POOL_ENABLED = True

# 连接池默认连接数。**默认值只在调用方不传 size 时生效**；UI 路径一律传
# 界面上 thread_count 的值（见 _ftp_pool_workers），所以界面上设 1 就是 1 条连接，
# 不会因为这里写了 4 就偷偷开 4 条。
FTP_POOL_DEFAULT_SIZE = 4

# 任务队列上限（方案 §5.1）。生产者（扫描线程）在队列满时阻塞，
# 于是"已扫描但未处理"的待办最多这么多条——这是并发化之后内存占用的唯一上界。
FTP_TASK_QUEUE_MAXSIZE = 1000

# 【阶段3-新增】断线重连的退避曲线。与 2d 的文件级重试曲线**数值相同、变量独立**：
# 同一套运维直觉（1+3+9=13 秒后放弃，让上层记一次失败），但两条曲线将来可以各自调整，
# 共用一个变量会让"改文件重试"顺手改掉"重连节奏"。
FTP_RECONNECT_BACKOFF_S = (1, 3, 9)

FTP_RECONNECT_ATTEMPTS = 4          # 3 次等待 = 4 次尝试（同 2d 的算法，见 RETRY_BACKOFF_S 注释）

# 空闲多久才值得发一次 NOOP 探活。**不是"每次操作都探"**：一条 0.1 秒前刚回完 226 的
# 连接，再发 NOOP 问一遍纯属多一次往返，而本路径的每文件开销正是要压的东西。
# 真正断掉的连接由操作本身报错 → _with_reconnect 兜住，NOOP 只是"久未使用后"的提前量。
FTP_HEALTHCHECK_IDLE_S = 5.0

# 数据连接空闲看门狗（原审计 P1-7）：30 秒没收到任何字节即判定卡死。
# 应用在**数据**socket 上（不是控制socket）：拔网线时 recv 会无限期阻塞，
# 有了它才会抛 TimeoutError → 走 _with_reconnect 重连重放。
FTP_DATA_IDLE_TIMEOUT_S = 30

# 【阶段3-修复｜体检发现】QUIT/221 握手的等待预算。见 FTPHelper.disconnect()：
# 对端卡死时 ftp.quit() 会等满一个完整的 socket 超时（默认 10 秒），而它恰好发生在
# "看门狗判死 → 重连重放"的关键路径上。实测：数据连接 1 秒判死，disconnect 又等 10.02 秒。
FTP_QUIT_TIMEOUT_S = 2.0

# _known_dirs 缓存上限（原审计 P1-6 的兜底）：目录数超出预期时宁可清空退回全量 MKD，
# 也不让一个 set 无限增长。
FTP_KNOWN_DIRS_MAX = 20000

# 进度消息条数上限：worker 每完成一个文件都投递会让 20 万文件产生 20 万条 UI 消息，
# 把消息泵和内存都压在无谓的进度刷新上。按总量算出步长，每条任务最多这么多条
# （末条必投，且 _poll_progress 仍是兜底心跳，所以不会丢进度）。
FTP_PROGRESS_MAX_MSGS = 1000

# ===================== 阶段5：上传续传 / 重传护栏 / .part 回收 / 库维护 =====================
# 【阶段5-新增｜上传路径的可信续传】2c 只覆盖了 FTP→本地，上传路径一直"每次都全量传"。
# 补法是给 _should_skip 的第 ⑥ 条一个**惰性探针**：前五条都过了才花一次 SIZE 往返
# 去问"远端那份还在不在、大小对不对"。惰性很重要——首次上传时库里压根没有记录，
# 绝大多数文件在第 ① 条就返回了，不该为它们白付一次往返。
# 回滚：把 _should_skip 的调用点去掉 dst_probe 参数即退回"上传全量重传"（2c 行为）。
UPLOAD_RESUME_ENABLED = True

# 【阶段5-新增】远端半截 .part 的回收。场景：进程被强杀/断电时 STOR 停下来，
# 服务器上留下 <名字>.part（正常失败路径会自己删，见 FTPHelper.upload_file 的
# except BaseException 分支），长期累积白占空间。
# 三条安全线，缺一不可：
#   ① 只删"本工具命名规则"能认出来的：必须以 .part 结尾，且 .part 之前那截
#      还得是个**带扩展名**的文件名（本工具产出的恒是 <完整目标名>.part）；
#   ② 设了扩展名过滤时，基名的扩展名还必须在过滤集里；
#   ③ 必须能从列举结果里读出 mtime，且**比现在旧 N 天以上**——读不出 mtime 一律不删。
# 年龄阈值取得远大于"一次任务可能持续的时间"，所以不会误删另一个正在跑的窗口
# 刚写出来的 .part。只在本任务的目标目录树内（remote_base 往下）生效。
UPLOAD_PART_RECLAIM_ENABLED = True

UPLOAD_PART_RECLAIM_DAYS = 7

# 回收是后台线程，不能让它在大树上无限跑下去；两道上限到了就停下并如实记录。
UPLOAD_PART_RECLAIM_MAX_DIRS = 2000

UPLOAD_PART_RECLAIM_MAX_DEL = 5000

def _part_reclaim_candidate(name, extensions):
    """【阶段5-3-新增】这条远端文件名够不够格被回收？**只判名字**，年龄另判。

    对应上面三条安全线里的 ①②：

    ① 必须以 .part 结尾，且去掉 .part 之后**还带扩展名**。
       本工具写出的中间文件恒为 <完整目标名>.part，而目标名来自源文件；源文件
       没有扩展名的情形在上传路径上根本不会产生 .part（见 _ftp_upload_one 的
       UPLOAD_PART_SUFFIX 用法）。所以 "foo.part" 这种基名没有扩展名的，
       不是本工具产生的 —— 服务器上别人的东西，一律不碰。
    ② 本次任务设了扩展名过滤时，基名的扩展名必须在过滤集里。
       过滤 .dcm 的任务去删 .txt.part，删的就不是它自己产生的东西。

    两条都不满足的，留给人工判断：多留一个文件只是占点空间，
    误删一个不是本工具产生的文件则可能是在毁别人的数据。
    """
    if not name.endswith(UPLOAD_PART_SUFFIX):
        return False
    stem = name[:-len(UPLOAD_PART_SUFFIX)]
    ext = os.path.splitext(stem)[1].lower()
    if not ext:
        return False
    if extensions and ext not in extensions:
        return False
    return True

def _part_reclaim_summary(rec):
    """【阶段5-3-新增】把一次回收的结果说成一行话。没什么可说的就返回 None。

    为什么要**克制**：这是开机顺手做的事，绝大多数任务的服务器上根本没有陈年 .part。
    每次传输都刷一行"清理 0 个"，用户很快就会学会无视整个日志区 ——
    真正有事发生的那次也就跟着被无视了。所以只在"删了、跳过了、删不动了、
    或者被上限截断了"的时候才出声。
    """
    if rec.get("error"):
        return f"[回收] 远端 .part 清理中断：{rec['error']}（不影响本次传输）"
    if not (rec.get("deleted") or rec.get("no_mtime") or rec.get("failed")
            or rec.get("capped")):
        return None
    bits = [f"扫描 {rec.get('scanned_dirs', 0)} 个目录",
            f"清理 {rec.get('deleted', 0)} 个过期 .part"]
    if rec.get("no_mtime"):
        bits.append(f"{rec['no_mtime']} 个读不到修改时间已跳过")
    if rec.get("failed"):
        bits.append(f"{len(rec['failed'])} 个删不掉")
    if rec.get("capped"):
        bits.append("已达本次上限，剩下的留到下次")
    return "[回收] 远端残留 " + "，".join(bits)

# 【阶段5-新增】状态库的长期维护。files 表只增不减，TB 级长期使用必然涨到几百万行。
# 触发条件与动作：
#   * 行数 > DB_MAINT_MIN_ROWS 才动手（不做无谓的 VACUUM，它要重写整个库文件）；
#   * VACUUM 回收 DELETE 留下的空洞；
#   * 清理"已完成且久远"的行——但**必须先确认目标文件还在**：目标没了，这一行就是
#     "这里曾经有一份备份、现在不见了"的唯一证据，删掉等于把数据事故的线索抹了。
#     远端目标（上传任务的 dst_path）在启动时刻无法核实，一律保留（见 _db_maintenance_worker）。
DB_MAINT_ENABLED = True

DB_MAINT_MIN_ROWS = 1000000

DB_PRUNE_AFTER_DAYS = 730        # 2 年

# VACUUM 要被 sqlite 授予"独占重写整个库文件"的权利，**只要还有另一条连接抱着
# 未提交的事务就会被顶回来（database is locked）**。这不是猜想，是 _probe_maint_sqlite
# 的 P3 实测：写入方闲着时 VACUUM 一次就过，写入方持事务时必失败。
# 本程序的 db-writer 是攒批提交的，任何时刻最多只持有 DB_COMMIT_INTERVAL_S 那么久，
# 所以"退一步、等一小会儿、再试"就够了，不需要也不该去抢锁或关掉写入方
# （那是拿本次传输的数据可靠性去换一次启动时的空间回收，本末倒置）。
# 试满还不成，就如实记进报告、跳过 VACUUM —— 清理那部分该做的照做。
DB_MAINT_VACUUM_ATTEMPTS = 3

DB_MAINT_VACUUM_RETRY_S = 2.0

# 删除分批提交。百万行级要删的可能是几十万条，一条一条自动提交会把启动拖成分钟级；
# 但也不能攒成一个大事务 —— 那个事务一开，别的写方（以及紧随其后的 VACUUM）全都被挡在外面。
DB_MAINT_DELETE_CHUNK = 500

# 【阶段4-新增】流式扫描期间的"我还在动"心跳日志：每扫到这么多文件打一行。
# 为什么要这个：扫描与传输重叠之后，"扫描完成，找到 N 个文件"那行**要等下完才出现**
# （总数扫完才知道），20 万文件的第一分钟里日志区会一片安静——而"安静"正是用户判断
# "卡死了没有"的直觉依据。所以按"已扫描到 N 个文件…"给一条粗粒度的进度线。
# 取 5000 而不是 1000：这行日志是给人看的，不是给程序看的，20 万文件 40 行刚刚好。
FTP_SCAN_LOG_EVERY = 5000

# 【阶段3-新增】断线判定用的 errno / winerror 白名单。见 FTPPooledConn._is_transport_error。
_TRANSPORT_ERRNOS = frozenset(x for x in (
    errno.ECONNRESET, errno.EPIPE, errno.ETIMEDOUT, errno.ECONNABORTED,
    errno.ECONNREFUSED, errno.EHOSTUNREACH, errno.ENETUNREACH, errno.ENOTCONN,
    getattr(errno, 'ESHUTDOWN', -1), getattr(errno, 'ENETRESET', -1),
    getattr(errno, 'ENETDOWN', -1),
) if x is not None and x >= 0)

_TRANSPORT_WINERRORS = frozenset((10050, 10051, 10052, 10053, 10054, 10057,
                                  10058, 10060, 10061, 10065))

# 【阶段2a-修改原因】完整表结构。集中成一个常量，配合 ensure_schema 的 schema_version
# 检查，使未来的结构变更有一个明确的迁移锚点。
_SCHEMA_DDL = """
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS tasks (
    task_id        INTEGER PRIMARY KEY AUTOINCREMENT,
    src_type       TEXT    NOT NULL,
    src_root       TEXT    NOT NULL,
    dst_type       TEXT    NOT NULL,
    dst_root       TEXT    NOT NULL,
    mode           TEXT    NOT NULL,
    extensions     TEXT,
    flatten        INTEGER NOT NULL DEFAULT 0,
    status         TEXT    NOT NULL,
    heartbeat_at   INTEGER NOT NULL,
    created_at     TEXT    NOT NULL,
    finished_at    TEXT,
    total_seen     INTEGER NOT NULL DEFAULT 0,
    done_count     INTEGER NOT NULL DEFAULT 0,
    skipped_count  INTEGER NOT NULL DEFAULT 0,
    failed_count   INTEGER NOT NULL DEFAULT 0,
    end_reason     TEXT
);

CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks(status, heartbeat_at);

-- 【D-1】主键 = (filename, src_root)。src_root 必须 NOT NULL —— 它现在是身份的一部分，
-- 允许它为空等于允许"这一行属于谁"说不清，而**判重正是靠它**。空串是合法值（见迁移的
-- 回填）：空串永远等于不了任何真实源根（_start_transfer 显式拒绝空源路径），
-- 所以那些老行查不到自己 → 宁可重传，不会漏传。
-- 没做路径规范化（"D:\\a" / "D:/a" / "D:\\a\\" 会算三个源），这是**接受**的：
-- 源根是用户当场选的那一个字符串，规范化要处理大小写、短名、junction、UNC，
-- 而那恰恰是"看起来在判重其实判错"的高发区。
--
-- 【任务1】主键 = (src_path)。`src_path` 必须显式写 NOT NULL —— 这是 SQLite 的怪癖，
-- 不是洁癖：非 INTEGER 的 PRIMARY KEY 列**默认允许 NULL**（它只是个 UNIQUE 索引，
-- 而 UNIQUE 索引不拦 NULL）。不写 NOT NULL，`PRIMARY KEY (src_path)` 就允许多行
-- `src_path IS NULL` 并存，等于给"这一行是谁"留了个能无限复制的空洞。
-- 同一套"不规范化"的理由在这里继续成立，而且更硬：判重与写库用的是**同一个字符串**
-- （file_transfer 两侧传的是同一个局部变量），任何一侧加规范化都会当场把续传打断。
-- filename / src_root 两列**保留**：它们不再是身份，但仍被 find_dst_owner（按 src_root
-- 排除自己）、iter_failed（按 filename 排序）、报告（pruned 列文件名）读着。
CREATE TABLE IF NOT EXISTS files (
    filename         TEXT    NOT NULL,
    src_root         TEXT    NOT NULL,
    task_id          INTEGER,
    src_path         TEXT    NOT NULL,
    src_size         INTEGER,
    src_mtime        INTEGER,
    mtime_precision  TEXT,
    dst_path         TEXT,
    dst_size         INTEGER,
    status           TEXT    NOT NULL,
    src_deleted      INTEGER NOT NULL DEFAULT 0,
    attempts         INTEGER NOT NULL DEFAULT 0,
    last_error       TEXT,
    updated_at       TEXT,
    PRIMARY KEY (src_path)
);

CREATE INDEX IF NOT EXISTS idx_files_task   ON files(task_id);
CREATE INDEX IF NOT EXISTS idx_files_status ON files(status);
-- 【D-1 新增】给 find_dst_owner 用：按 dst_path 反查"这个目标路径是谁写下的"。
-- 不加这条索引，跨源覆盖审计会退化成全表扫描 —— 而它在"目标已存在且大小/mtime 全同"
-- 那一档每次都要跑一次（见 file_transfer._audit_overwrite）。
CREATE INDEX IF NOT EXISTS idx_files_dst    ON files(dst_path);

-- 【D-1】加两列：谁覆盖的 / 被覆盖的那份原来属于谁。
-- 列名跟本表既有的 old_/new_ 前缀（old_size/old_mtime/new_size/new_mtime）**保持一致** ——
-- 这张表的读者已经习惯了那套命名，再引入 prev_/cur_ 只会让同一张表出现两套词汇。
-- 不加这两列，多源场景下用户看到「IM_0001.dcm 被覆盖」只能一头雾水：谁覆盖了谁？
CREATE TABLE IF NOT EXISTS overwrites (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    filename       TEXT    NOT NULL,
    dst_path       TEXT    NOT NULL,
    old_size       INTEGER,
    old_mtime      INTEGER,
    new_size       INTEGER,
    new_mtime      INTEGER,
    old_src_root   TEXT,
    new_src_root   TEXT,
    task_id        INTEGER,
    overwritten_at TEXT    NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_overwrites_task ON overwrites(task_id);
"""

# 【阶段2a-修改原因】可读时间统一走这一个出口，避免各处格式化不一致。
def _now_text():
    """ISO8601 本地时间，仅用于人读（不参与任何比较）。"""
    return datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")

# 【阶段2a-修改原因】回读落盘文件的 size / mtime / 精度层级。
# 用目标文件而非源文件：move 模式下源已被删除；且 copy2 与 move 都会保留源 mtime，
# 因此目标文件的属性等价于源的属性。在 db-writer 线程执行，不占传输关键路径。
def _stat_for_state(path):
    """返回 (precision, size, mtime)；stat 失败返回 ("ns", None, None)。"""
    try:
        st = os.stat(path)
    except OSError:
        return ("ns", None, None)
    try:
        mtime = int(st.st_mtime_ns)
        precision = "ns"
    except AttributeError:
        mtime = int(st.st_mtime)
        precision = "s"
    return (precision, int(st.st_size), mtime)

# 【阶段5-4-新增】这条 done 记录的**目标文件**现在还确实在吗？只有答"是"才敢删这一行。
#
# 为什么要单独一个函数：清理的判据不是"这行旧了"，而是"这行旧了**且它的目标还在**"。
# 目标没了而记录还在，这一行就是"这里曾经有一份备份、现在不见了"的唯一证据；
# 把它当垃圾清掉，等于把数据事故的线索一起抹了。所以是**确认制**：
# 只有正面确认"文件在、且大小与当时记录的一致"，才返回 True（= 可以清理）；
# 其余一律 False（= 保留），包括：
#   * dst_path 为空 —— 没有可核对的坐标；
#   * 远端 FTP 路径 —— 启动时刻无法核实（不为了清理去连服务器，也不该为了清理
#     去连：那会把一次启动维护变成一次远程 IO，还得处理连不上、超时）；
#   * stat 失败（文件被删/被移走/盘没挂上）；
#   * 大小对不上 —— 那里现在是**别的东西**，原备份的去向依然成谜，留证。
#
# 【为什么只比大小、不比 mtime】mtime 是"什么时候落的盘"，大小才是"是不是同一份内容"
# 的最低成本近似；而这里要防的是"目标没了"，大小不等已经足够说明"不是原来那份"。
# 逐个文件读内容做散列是不现实的（这是 TB 级、百万行的场景）。
def _backup_still_there(dst_path, dst_size):
    if not dst_path or _is_remote_path(dst_path):
        return False
    if dst_size is None:
        return False
    try:
        return os.path.getsize(dst_path) == int(dst_size)
    except (OSError, TypeError, ValueError):
        return False

# 【阶段5-4-新增】把一次库维护说成一行话。没什么可说的就返回 None。
#
# 分寸与 _part_reclaim_summary 一致：这是开机顺手做的事，绝大多数启动都够不着阈值，
# 每次都刷一行"检查了，没到阈值"只会把日志区变成噪音，真正有事的那次反而被淹掉。
# 所以只在"真动手了（清理或 VACUUM）""出错"这两种情况下出声。
def _db_maint_summary(rep):
    if rep.get("error"):
        return f"[库维护] 未完成：{rep['error']}（不影响使用）"
    if rep.get("skipped_reason"):
        return None
    n_kept = len(rep.get("kept") or [])
    if not ((rep.get("pruned")) or rep.get("vacuumed") or rep.get("vacuum_error")):
        return None
    bits = [f"files 表 {rep.get('rows', 0)} 行（阈值 {rep.get('threshold', 0)}）",
            f"清理 {len(rep.get('pruned') or [])} 条过期记录"]
    if n_kept:
        # 这句不能省：kept 是"目标已不在、特意留着"的条数。用户看到才知道
        # 库为什么没像预期那样瘦下来，也才知道这里面藏着待查的线索。
        bits.append(f"{n_kept} 条的目标已不在，一律保留")
    if rep.get("vacuumed"):
        bits.append("VACUUM 完成")
    elif rep.get("vacuum_error"):
        # 不吞：清理已经做了，但空间没还回来，用户得知道"下次再说"是为什么。
        bits.append(f"VACUUM 未成功（{rep['vacuum_error']}），空间下次再收")
    else:
        bits.append("VACUUM 未执行")
    bits.append(f"耗时 {rep.get('elapsed_s', 0)}s")
    return "[库维护] " + "，".join(bits)

# ===================== FTP 辅助类（增强版） =====================
# ===================== 阶段2c：FTP mtime 采集 =====================
# 【阶段2c-关键约束（方案 §4.3）】FTP mtime **一律不做时区转换**。
#
# FTP 服务器返回的是服务器自己的时间：LIST 是服务器本地时间，MLSD 的 modify= 按
# RFC 3659 是 UTC。客户端无法可靠得知服务器时区，任何"修正"都是猜。
# 因此本模块只把服务器给的**原始字符串**规范化成 epoch，不叠加任何本地时区偏移，
# 且该值**仅用于"同一服务器、同一文件的自比较"**（判断"这个文件与我上次传的是不是同一个"），
# 绝不用于跨源比对（本地 mtime vs 远端 mtime 比大小毫无意义）。
#
# 为什么这样是安全的：比对双方都来自同一服务器的同一格式，时区/格式偏移是**恒定值**，
# 相减后抵消，不影响"是否变化"的判定。
#
# 【警告·写给后人】不要在这里加"时区修正"。加了之后，同一文件在同一服务器上会因为
# 客户端所在时区的变化（或 DST 切换）算出不同的 epoch → 判定为"源变了" → 整盘重传。
# 那是纯粹的伤害，没有任何收益。
_LIST_MONTHS = {'jan': 1, 'feb': 2, 'mar': 3, 'apr': 4, 'may': 5, 'jun': 6,
                'jul': 7, 'aug': 8, 'sep': 9, 'oct': 10, 'nov': 11, 'dec': 12}

_YEAR_AMBIGUOUS_BASE = 70    # 方案 §4.2：两位年份 YY >= 70 → 19YY，否则 20YY（覆盖 1945~2070）

# 【阶段4-新增】LIST 行的三个格式正则。原先它们编译在 _recursive_list 函数体里，
# 阶段4 的流式遍历需要**同一套**格式判定，于是提到模块级共用。
# 两处各写一份正则的危险从来不是多几行，而是将来只改了一处：老路径认得出的文件、
# 新路径认不出（或反过来），而两边都能"正常跑完"——静默漏文件是这里最坏的失败形态。
# 共用之后，格式判定只有一份，用例 N 再拿两种实现逐字段比对钉住"结果一致"。
# 注意模式仍**结束于文件名之前**，下面靠 match.end() 定位文件名（文件名里可以有空格）。
_LIST_UNIX_RE = re.compile(
    r'^([\-d])([rwx\-]{9})\s+\d+\s+\w+\s+\w+\s+(\d+)\s+(\w+)\s+(\d+)\s+([\d:]+)')

_LIST_WIN_DIR_RE = re.compile(r'^(\d{2}-\d{2}-\d{2})\s+(\d{2}:\d{2}[AP]M)\s+<DIR>\s+(.+)$')

_LIST_WIN_FILE_RE = re.compile(r'^(\d{2}-\d{2}-\d{2})\s+(\d{2}:\d{2}[AP]M)\s+(\d+)\s+(.+)$')

def _parse_mlsd_mtime(raw):
    """MLSD 的 modify= 值 → (epoch, 's')。RFC 3659 规定该值为 UTC。
    解析失败返回 (None, None)：调用方据此判为"mtime 不可用"，进而**不跳过**（安全方向）。"""
    if not raw:
        return None, None
    s = raw.strip().split('.')[0]          # RFC 3659 允许小数秒，本工具用不到
    if len(s) < 14 or not s[:14].isdigit():
        return None, None
    try:
        return calendar.timegm(time.strptime(s[:14], '%Y%m%d%H%M%S')), 's'
    except ValueError:
        return None, None

def _parse_list_mtime(raw, now=None):
    """LIST 行的"月 日 时间/年"部分 → (epoch, 'min'|'s')。
    支持三种真实服务器的输出形式：
        Unix 带年份  "Jan 12 2024"           → 'min'（LIST 从不给秒）
        Unix 省年份  "Jan 12 10:30"          → 'min'（ls 对近半年的文件省略年份）
        含秒变体     "Jan 12 10:30:45"       → 's'
        DOS/Windows  "01-12-24 10:30AM"      → 'min'
    now 仅用于"省年份"形式的年份推断，可注入以便测试；缺省取当前时间。"""
    if not raw:
        return None, None
    s = ' '.join(str(raw).split())

    # ---- 形式1：Unix 带年份 "Jan 12 2024" ----
    m = re.match(r'^([A-Za-z]{3})\s+(\d{1,2})\s+(\d{4})$', s)
    if m:
        mon = _LIST_MONTHS.get(m.group(1).lower())
        if not mon:
            return None, None
        try:
            t = time.strptime("%04d-%02d-%02d" % (int(m.group(3)), mon, int(m.group(2))),
                              "%Y-%m-%d")
            return calendar.timegm(t), 'min'
        except ValueError:
            return None, None

    # ---- 形式2：Unix 省年份 "Jan 12 10:30" / "Jan 12 10:30:45" ----
    m = re.match(r'^([A-Za-z]{3})\s+(\d{1,2})\s+(\d{1,2}):(\d{2})(?::(\d{2}))?$', s)
    if m:
        mon = _LIST_MONTHS.get(m.group(1).lower())
        if not mon:
            return None, None
        day, hh, mi = int(m.group(2)), int(m.group(3)), int(m.group(4))
        ss = int(m.group(5) or 0)
        # 年份推断用 gmtime 而非 localtime：本模块全程在 UTC 框架里算（见文件头约束）
        year = time.gmtime(time.time() if now is None else now).tm_year
        ep = _mk_epoch(year, mon, day, hh, mi, ss)
        if ep is None:
            return None, None
        # ls 的惯例：省略年份的都是"过去半年内"的文件。若按今年算落在未来超过一天，
        # 说明它其实属于上一年。判断失误的后果只是多传一次（安全方向），不会漏传。
        if ep - (time.time() if now is None else now) > 86400:
            ep = _mk_epoch(year - 1, mon, day, hh, mi, ss)
            if ep is None:
                return None, None
        return ep, ('s' if m.group(5) else 'min')

    # ---- 形式3：DOS/Windows "01-12-24 10:30AM" ----
    m = re.match(r'^(\d{2})-(\d{2})-(\d{2})\s+(\d{1,2}):(\d{2})\s*([AP])M$', s, re.I)
    if m:
        mo, da, yy = int(m.group(1)), int(m.group(2)), int(m.group(3))
        yy = (1900 + yy) if yy >= _YEAR_AMBIGUOUS_BASE else (2000 + yy)
        hh, mi = int(m.group(4)) % 12, int(m.group(5))
        if m.group(6).upper() == 'P':
            hh += 12
        ep = _mk_epoch(yy, mo, da, hh, mi, 0)
        return (ep, 'min') if ep is not None else (None, None)

    return None, None

def _mk_epoch(year, month, day, hour, minute, second):
    """构造 epoch；非法日期（如平年 2-29）返回 None 而不是抛异常。"""
    try:
        return calendar.timegm(time.strptime(
            "%04d-%02d-%02d %02d:%02d:%02d" % (year, month, day, hour, minute, second),
            "%Y-%m-%d %H:%M:%S"))
    except (ValueError, OverflowError):
        return None

# ===================== 阶段四·P0-1：日期分层增量 =====================
# 【P0-1 新增】这一节解决的是 FTP 上最贵的那件事：一次目录列举 = 一次网络往返。
# 源根下按 `YYYY/MM/DD` 分层的目录树，**只下探到同步点那一层**，
# 比同步点旧的分支整棵剪掉，连 LIST 都不发。
#
# 【核心设计：**不解析日期**】（`45_plan_incremental.md` §A.1 —— 整节的支点）
# `2026` / `07` / `31` 是**定宽纯数字**，而定宽数字的**字典序 == 时间序**。
# ⇒ 「只扫新的日期目录」不需要知道"这是几月几号"，只需要**字符串比大小**。
# 一旦把它当日期，平年 2-29、两位年、时区、`07` vs `7`、非公历 就全都要处理 ——
# `_YEAR_AMBIGUOUS_BASE` 那个常量正是当年解析 FTP LIST 两位年份踩出来的坑。
# 不解析 ⇒ 那些坑**一个都碰不到**。与本项目"不猜"的一贯风格一致。
#
# 【模板只许写 YYYY / MM / DD，不许夹字面量，也不给 YY】
#   · 判据是"字典序 == 时间序"，它**要求各层定宽**：`2026/7/31` 不补零就不成立
#     （`"7" > "12"`）。所以模板的每一层都必须点明宽度，"自动探测第几层是日期"
#     是典型的"看起来在判断、其实判错"，明确不做。
#   · **不给 `YY`**：两位年在字典序里跨不过世纪（`"99" > "26"`），
#     而它出错的形态是**静默漏传**。宁可让用户写四位年。
#   · **不夹字面量**（例如 `dcm/YYYY/MM/DD`）：一旦允许，校验就从"每段宽度对不对"
#     变成"这一段是字面量还是记号、位置对不对"，多一个维度就多一处能判错的地方。
#     需要固定前缀目录的用户，**把源根往下设一级**即可 —— 语义相同，少一个判错面。
#
# ⚠️ 本节的函数**全是纯函数**（无 I/O、无全局状态），带状态的那点东西在
#    `DateLayerPruner` 里，且只存"记录用"的量。这样 9 类验收里最难写的那几条
#    （前缀比较的各分支）可以直接对着一张表逐格打，不必起进程。

# 允许的记号 → 定宽位数。**这张表是模板校验与剪枝判定**唯一**的宽度来源。**
_DATE_TOKENS = {"YYYY": 4, "MM": 2, "DD": 2}

# 判定结果。`WARN` 的含义是"**不剪**，但要说话" —— 它**永远不等于剪**，
# 这是故意的：名字不像日期时判"留着"，宁可多扫一棵树，也不赌它是旧的。
PRUNE = "prune"
KEEP = "keep"
WARN = "warn"


def parse_date_template(tpl):
    """模板串 → `((记号, ...), 错误)`。**空模板 = 未启用**，返回 `((), None)`。

    为什么空 = 未启用（而不是"另加一个开关勾选框"）：
    方案 §A.5 规则 1 要求"默认关闭"，规则 2 要求"缺失一律退回最安全的动作"——
    而"用户没填"和"用户填了又清掉"在这套语义下是同一件事，
    多一个勾选框就多一处"勾了但没填模板 / 填了但没勾"要判的组合。
    """
    if tpl is None:
        return (), None
    s = str(tpl).strip()
    if not s:
        return (), None
    # 首尾的斜杠先剥掉再切：`YYYY/MM/DD/` 是**同一个模板的另一种拼法**，
    # 不是"有空层"。不剥的话它会撞进下面那条判据、被报成"模板里有空的层"——
    # 用户看着一个明明写对了的模板被拒，而真正的原因（多打了个斜杠）不显示。
    # 层**之间**的空层（`YYYY//DD`）仍然照拒：那才是真的少写了一层。
    parts = [p.strip() for p in s.replace("\\", "/").strip("/").split("/")]
    if any(not p for p in parts):
        return None, "模板里有空的层（`%s`）：每一层都要写一个记号，例如 YYYY/MM/DD" % tpl
    for i, p in enumerate(parts):
        if p not in _DATE_TOKENS:
            return None, ("模板第 %d 层 `%s` 不是合法记号：只允许 %s（**大写**），"
                          "层与层之间用 `/` 隔开"
                          % (i + 1, p, " / ".join(sorted(_DATE_TOKENS))))
    return tuple(parts), None


def format_date_template(segments):
    """记号元组 → 模板串。用在同步点的 meta 键里。空模板转成空串。"""
    return "/".join(segments or ())


def _segment_ok(name, token):
    """这一层的目录名像不像模板要的那一层。

    `isascii()` 不是多余的：`str.isdigit()` 对全角数字和阿拉伯-印度数字
    （`'٣'.isdigit()` 为真）也返回 True，而那些字符的字典序**不是**数字序。
    少这一句，判据就从"定宽数字"悄悄变成"看着像数字"。"""
    w = _DATE_TOKENS[token]
    return len(name) == w and name.isdigit() and name.isascii()


def parse_syncpoint(text, segments):
    """meta 里存的同步点串 → `(层元组, 原因)`。**任何不对劲都当作"没有同步点"**。

    返回的第一个元素为 `None` 时，调用方必须走**全扫**（规则 2：缺失一律退回最安全的动作）。
    校验这么严是因为同步点是**唯一**能决定"哪些目录不被看见"的输入 ——
    它写歪一格，对应的那批文件就永远传不过来，而且**界面上全绿**。
    """
    if text is None or str(text).strip() == "":
        return None, "没有同步点 ⇒ 全扫"
    if not segments:
        return None, "没启用模板 ⇒ 全扫"
    parts = tuple(str(text).split("/"))
    if len(parts) > len(segments):
        return None, ("同步点有 %d 层，模板只有 %d 层 ⇒ 对不上，全扫"
                      % (len(parts), len(segments)))
    for i, p in enumerate(parts):
        if not _segment_ok(p, segments[i]):
            return None, ("同步点第 %d 层 `%s` 不像模板 `%s`（应为 %d 位数字）⇒ 全扫"
                          % (i + 1, p, segments[i], _DATE_TOKENS[segments[i]]))
    return parts, None


def date_layer_decision(segments, syncpoint_parts, parent_parts, name):
    """逐层前缀范围比较（方案 §A.2）。返回 `(动作, 理由)`。

    `parent_parts` = 从**源根**到"装着 `name` 的那个目录"的各层目录名（元组）。
    动作 ∈ `{"prune", "keep", "warn"}`；**`warn` 同时意味着不剪**。

    | 情况 | 动作 |
    |---|---|
    | 前 k 段都等于同步点，且第 k 段 > 同步点第 k 段 | keep（**整棵保留**，后面的段一次都不用比）|
    | 第 k 段 == 同步点第 k 段 | keep（继续下探）|
    | 第 k 段 < 同步点第 k 段 | **prune** |
    | 前 k 段**不是**同步点的前缀 | keep（已经岔开 ⇒ 剪枝根本不适用）|

    最后那一格是最容易漏的一格：同步点在 `2026/07/31`，走到 `2026/08` 时，
    它下面的 `01` **不能**拿去和同步点的第 3 段 `31` 比 —— `"01" < "31"`，
    一剪就把 8 月 1 号剪掉了，而它是**新**的。判据必须先问"我还在这条路上吗"。
    """
    if not segments:
        return KEEP, "没启用模板 ⇒ 不剪"
    if not syncpoint_parts:
        return KEEP, "没有同步点 ⇒ 全扫"
    k = len(parent_parts)
    if k >= len(segments):
        return KEEP, "已经在日期层以下 ⇒ 不剪"
    if tuple(parent_parts) != tuple(syncpoint_parts[:k]):
        return KEEP, "已经岔开同步点这条路 ⇒ 剪枝不适用"
    if k >= len(syncpoint_parts):
        # 同步点比模板短（例如只到 `2026/07`）：这一层没有可比的段，一律不剪。
        return KEEP, "同步点没有第 %d 层可比 ⇒ 不剪" % (k + 1)
    if not _segment_ok(name, segments[k]):
        return WARN, ("`%s` 不像模板第 %d 层（`%s` = %d 位数字）⇒ **不剪**，"
                      "只报一声" % (name, k + 1, segments[k], _DATE_TOKENS[segments[k]]))
    sp = syncpoint_parts[k]
    if name > sp:
        return KEEP, "`%s` > 同步点第 %d 层 `%s` ⇒ 整棵保留" % (name, k + 1, sp)
    if name == sp:
        return KEEP, "`%s` == 同步点第 %d 层 ⇒ 继续下探" % (name, k + 1)
    return PRUNE, "`%s` < 同步点第 %d 层 `%s` ⇒ 整棵剪掉" % (name, k + 1, sp)


def _rel_parts(path, base):
    """`path` 相对 `base` 的各层目录名。

    两处必须做的归一化，都是**实测会撞上**的：
      · 分隔符 —— `os.path.join("D:/data", "2026")` 在 Windows 上给出
        `D:/data\\2026`（**混的**）。不归一，`split("/")` 会把最后两层粘成一段。
      · 盘符大小写 —— 用户在源根里敲的是 `d:/data`、而 `os.walk` 吐的是 `D:/data`。
        不理会大小写，前缀就匹配不上，于是 rel 变成整条路径、层级全错位；
        **那种错位的失效方向恰好是"判成不剪"**（安全），但会静默地让整个功能不生效。
    """
    p = str(path).replace("\\", "/").rstrip("/")
    b = str(base).replace("\\", "/").rstrip("/")
    if b:
        if p == b:
            p = ""
        elif p.startswith(b + "/"):
            p = p[len(b) + 1:]
        elif os.name == "nt" and p.lower().startswith(b.lower() + "/"):
            p = p[len(b) + 1:]
    return tuple(x for x in p.split("/") if x)


class DateLayerPruner:
    """一次扫描期间的剪枝器。**只做三件事**：判、记、说话。

    它是本功能里唯一带状态的东西，而状态**全部只用于记录**（剪了几个、报过什么话、
    见过的最新日期层是哪个）—— 判定本身仍然是 `date_layer_decision` 那个纯函数。
    这样"少数几条真正决定'什么会被看见'的代码"是可枚举、可逐格打的。
    """

    # 日志里最多列几个被剪掉的具体路径。**只限"列几个"，不限"剪几个"**——
    # 计数永远是全量。刷屏会把"每次剪枝都出声"这条规则本身变成噪声，
    # 而噪声判据比没有判据更糟（`run_all.py` §7.4.6 的教训）。
    MAX_LOGGED_SAMPLES = 8

    def __init__(self, segments, syncpoint_parts=None):
        self.segments = tuple(segments or ())
        self.syncpoint = tuple(syncpoint_parts or ())
        self.n_pruned = 0
        self.n_kept = 0
        self.samples = []          # [(相对路径串, 理由)]，最多 MAX_LOGGED_SAMPLES 条
        self.warnings = []         # 去重后的告警（"不像日期"那种）
        self.max_seen = None       # 见过的**最大**日期层路径（元组），同步点就推进到它

    # ---------- 判定 ----------
    def _record(self, rel_parts, name, action, why):
        if action == PRUNE:
            self.n_pruned += 1
            if len(self.samples) < self.MAX_LOGGED_SAMPLES:
                self.samples.append(("/".join(tuple(rel_parts) + (name,)), why))
        else:
            self.n_kept += 1
            if action == WARN and why not in self.warnings:
                self.warnings.append(why)
        return action

    def decide(self, parent_parts, name):
        """剪不剪（`True` = 剪）。给还没接线到 `os.walk` 的调用方用。"""
        action, why = date_layer_decision(
            self.segments, self.syncpoint, parent_parts, name)
        self._record(parent_parts, name, action, why)
        return action == PRUNE

    def filter_walk_dirs(self, src_root, dirpath, dirs):
        r"""`os.walk` 的剪枝点：**原地**改 `dirs`。

        ⚠️ 必须是 `dirs[:] = ...`。写成 `dirs = [...]` 只是**重新绑定一个局部名**，
        `os.walk` 手里拿的还是原来那个列表 —— **一行日志都不会少，一个目录也不会少扫**，
        功能静默失效。这是本条最容易写错的地方，验收里专门钉了一条
        （`_verify_date_layer.py` 的 F 组：把这里换成重新绑定，那条必须红）。
        """
        rel = _rel_parts(dirpath, src_root)
        dirs[:] = [n for n in dirs
                   if not self.decide(rel, n)]
        return dirs

    def make_prune_callback(self, base):
        """给 `config._walk_remote_files(prune=...)` 用的回调。

        签名按方案 §A.3 定为 `prune(child_abs, name) -> bool`（`True` = 整棵剪掉）——
        遍历器那边只多两行，日期逻辑一行都不进那个函数。
        """
        def _prune(child_abs, name):
            return self.decide(_rel_parts(child_abs, base)[:-1], name)
        return _prune

    # ---------- 记录 ----------
    def note_visit(self, rel_parts):
        """记下"这个日期层目录**被真的进去过**"。同步点只推进到见过的最大者。

        为什么记的是"进过的"而不是"列表里有的"：剪枝的依据是"上次**跑完了**"，
        而"跑完了"只对**真的走进去扫过**的那些日期成立。
        """
        if len(rel_parts) < len(self.segments):
            return
        head = tuple(rel_parts[:len(self.segments)])
        for i, p in enumerate(head):
            if not _segment_ok(p, self.segments[i]):
                return                      # 树不是模板说的那个样子 ⇒ 不拿它当同步点
        if self.max_seen is None or head > self.max_seen:
            self.max_seen = head

    def note_visit_local(self, src_root, dirpath):
        self.note_visit(_rel_parts(dirpath, src_root))

    def note_visit_remote(self, base, cur):
        self.note_visit(_rel_parts(cur, base))

    # ---------- 说话（规则 3：每次剪枝都出声）----------
    def summary(self):
        """一行摘要 + 至多几条样本 + 至多几条告警。**剪了 0 个也说话** ——
        "开了但一个都没剪"本身就是要看见的事实（模板写错了就长这样）。"""
        if not self.segments:
            return ""
        head = "[日期分层] 同步点 %s，剪掉 %d 个日期目录、保留 %d 个" % (
            "/".join(self.syncpoint) if self.syncpoint else "(无，本次全扫)",
            self.n_pruned, self.n_kept)
        for rel, why in self.samples:
            head += "\n    · 剪 %s —— %s" % (rel, why)
        if self.n_pruned > len(self.samples):
            head += "\n    · …另有 %d 个同样被剪（仅计数）" % (self.n_pruned - len(self.samples))
        for w in self.warnings:
            head += "\n    ⚠️ %s" % w
        return head


# ===================== 阶段4：流式遍历骨架 =====================
# 【阶段4-新增】遍历骨架只有这一份，两个调用方唯一的区别是"一次目录列举失败时怎么办"：
#   FTPHelper.iter_files_recursive     传裸的 list_dir      → 出错就上抛
#   FTPPooledConn.iter_files_recursive 传重连重放版 list_dir → 断链自动重放当前目录
# 参数 list_dir(路径) 必须返回 (files, subdirs)：
#   files   = [(name, size, mraw, mnorm, mprec), ...]
#   subdirs = [name, ...]
#
# 为什么推进单位是"一个目录"而不是"一个文件"：一次目录列举 = 一次网络往返。
# 粒度定在目录上，重放就发生在"这一层还没吐出去任何东西"之前，
# 于是重放永远不会把已经交给下游的文件重复交一遍（重复 == 同盘异构的同名撞车）。
def _walk_remote_files(list_dir, base_remote_dir, dir_counter=None,
                       prune=None, on_visit=None):
    """逐目录推进的远端遍历，yield 文件五元组（绝对路径, size, mraw, mnorm, mprec）。

    【阶段一 Bug2 新增 dir_counter（可选）】
      "已扫 N 个目录"这个量**只有这里知道** —— 文件流的长度是"找到了多少个文件"，
      与"列举过多少个目录"是两回事（空目录一个文件都不产出，但它确实被扫过了）。
      所以计数器必须由遍历器自己在**每列举完一个目录**之后自增。

      为什么是"调用方传一个可变容器进来"而不是"本函数返回目录数"：
      本函数是**生成器**（惰性、逐目录推进），返回值要等耗尽才拿得到，而界面要的是
      **过程量**（10~30 分钟的扫描期里随时可读）。传 `[0]` 进来、由调用方在别的线程里
      读它，是唯一能同时保住"惰性"和"可观察"的形态。
      默认 `None` = 完全不计数，**所有既有调用方逐字节行为不变**（含 list 版与流式版）。

      容器约定：`dir_counter` 是**长度 ≥1 的列表**，本函数只做 `dir_counter[0] += 1`；
      调用方保证它有初值。不引入锁 —— 扫描线程是唯一写者，主线程只读一个 int，
      GIL 下不会撕裂，最坏读到 500ms 前的值（对"死活指示器"完全够用，与 _total 同理）。

    【P0-1 新增两个可选回调，沿用同一条"默认 None = 逐字节不变"的约定】
      · `prune(child_abs, name) -> bool` —— **True = 整棵剪掉**，不列举、不压栈。
        日期分层的判据在 `DateLayerPruner` 里，**本函数一个字都不知道"日期"是什么**
        （与 `_match_filter` 同一层：那个也不知道"过滤"是给谁用的）。
        这是有意为之：本函数是 FTP 两条路（裸连接 / 连接池）共用的唯一遍历骨架，
        把业务判据焊进来，等于让两条路一起承担它的 bug。
      · `on_visit(cur) -> None` —— 每列举完一个目录回调一次。
        给"同步点只推进到真的进去过的最大日期层"这条规则用（`note_visit_remote`）。
        为什么不让调用方从 `prune` 里自己推：`prune` 只在**有子目录**时才会被调到，
        而最深的那个日期层目录往往一个子目录都没有 —— 从它推会**永远漏掉最深一层**，
        于是同步点每次都比实际新日期旧一格，表现为"每天多扫一天"。**不报错，只是慢。**
    """
    base = (str(base_remote_dir).strip().replace('\\', '/') if base_remote_dir else '/')
    base = base.rstrip('/') or '/'
    stack = [base]
    while stack:
        cur = stack.pop()
        entries, subdirs = list_dir(cur)
        if dir_counter is not None:
            dir_counter[0] += 1          # 【阶段一 Bug2】本目录已列举完毕，先记数再产出
        if on_visit is not None:
            on_visit(cur)                # 【P0-1】"这个目录真的被进去过了"
        prefix = cur.rstrip('/')          # base='/' 时 rstrip 成 ''，正好拼出 '/name'
        for name, size, mraw, mnorm, mprec in entries:
            yield (prefix + '/' + name, size, mraw, mnorm, mprec)
        for name in subdirs:
            # 【P0-1】剪枝发生在**压栈之前** —— 被剪掉的连 `list_dir` 都不会被调到，
            #   这正是本功能省下的那笔网络往返。（判据见 `DateLayerPruner`。）
            if prune is not None and prune(prefix + '/' + name, name):
                continue
            stack.append(prefix + '/' + name)


def _match_filter(filepath, extensions):
    if extensions is None: return True
    _, ext = os.path.splitext(filepath)
    return ext.lower() in extensions

# 【阶段2c-新增】远端路径判据：以 / 开头且不是 Windows 盘符（D:\... / D:/...）。
# 用途见 _retry_failed_worker：files 表里 FTP 任务的 src_path（下载）或
# target_path（上传）是远端路径，而重传实现只会本地 shutil.copy2/move。
# 为什么不给 files 表加 src_type 列：那要改表结构；本工具的实际路径形态只有
# "本地 Windows 路径"与"FTP 绝对路径"两种，判据当前完全可靠。
# 放长线（若将来支持 Linux 本地路径）再加列也不迟。
def _is_remote_path(path):
    if not path:
        return False
    if len(path) >= 2 and path[1] == ':':   # Windows 盘符：C: / D: ...
        return False
    return path.startswith('/')


# ===================== 第二刀：配置读写（从 file_transfer.py 搬来） =====================
# 【为什么它们住这里，而不是留在主模块、也不搬进 ui_dialogs】
#   * 它们做的事就是"读写上面那个 CONFIG_FILE"，与那个常量同层同理；
#   * 需要它们的两个 FTP 对话框住 ui_dialogs.py，而那里**不能反向 import 主模块**
#     （file_transfer → ui_dialogs → file_transfer 是循环依赖）。
#     依赖方向的下游就是本模块，所以这里是唯一的家。
#
# 【顺带修掉的一个 P0 bug —— 详见 PROJECT_STATE 第二刀小节】
# 第一刀把这两个函数体里的 `CONFIG_FILE` 机械改写成 `config.CONFIG_FILE`，
# 而 `save_config` 的**形参就叫 `config`** —— 于是那句成了"对 dict 取属性"
# → AttributeError → 被下面的裸 `except:` 吞掉 → **FTP 设置从此一次都没落过盘**
# （README_FINAL.md 第六节写的"保存过一次就会记住"因此一直是假的）。
#
# 修法是**结构性**的，不是"改一行"：函数搬回自己名字的家乡模块之后，
# 裸名 `CONFIG_FILE` 就是同模块全局 —— 遮蔽的前提（作用域里有个同名 import）
# 在本文件里根本不存在。`_checks/_shadow_audit.py` 的命中数从 1 变 0。
#
# 【两个"不做"】
#   * 形参不改名（config → cfg）：锚点校验器要求搬走的行**逐字保真**，
#     改名会让 520 号锚点永远对不上，只能靠加"有意变更豁免"来绕过 ——
#     那等于放宽断言。而这里没有需要防的威胁：本文件没有 `import config`。
#   * 裸 `except:` 不动：改它属于"改逻辑"，不属本刀。
#     **但它是这个 bug 藏了两轮的放大器**（AttributeError 被静默吞掉），
#     已记入 PROJECT_STATE 技术债。
def load_last_config():
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, 'r', encoding='utf-8') as f:
                return json.load(f)
        except:
            return None
    return None


def save_config(config):
    try:
        with open(CONFIG_FILE, 'w', encoding='utf-8') as f:
            json.dump(config, f, indent=2)
    except:
        pass


# ===================== 功能A：界面状态（上次用的路径 / 模式 / 过滤 / 线程数） =====================
# 【为什么是**另一个文件**，不是往 CONFIG_FILE 里加一个键】
#   CONFIG_FILE 那份 JSON 是 **FTP 连接配置**，而 `save_config(cfg)` 是**整份覆盖**写出去的
#   （见上面那一节）。往里塞一个 `_ux` 键，用户下次点一次 FTP 目录浏览就会被整体冲掉 ——
#   "有时候记得住、有时候记不住"比"从来不记"难查得多。
#   两个用途、两份文件、两套读写，互不干扰。这也让"清掉记忆"退化成删一个文件。
#
# 【两个函数都不抛异常】
#   记住路径是**锦上添花**：它失败绝不该影响一次真实传输，所以读写一律静默。
#   口径与上面的 `load_last_config` / `save_config` 一致（同样是裸 `except:`）。
#   **代价要知道**：裸 except 会吞掉真错误 —— 这个形态正是 §7.4.7 那个 P0 bug
#   藏了两轮的原因，已记在技术债里。沿用它是为了与既有代码同形，不是因为它更好。
#
# 【本模块不认识这些键的**含义**】
#   这里只负责"把 dict 存进一个文件 / 从文件读回一个 dict"。
#   哪个键对应界面上哪个控件、怎么验类型，全在 file_transfer.py 的 `_restore_ux_state` ——
#   理由是那份白名单回答的是"这份文件可以被谁相信"，而它要防的是**用户手改这个文件**，
#   属界面层的判断，塞进常量模块只会让两边各存一份、迟早对不上。
UX_STATE_FILE = os.path.join(os.path.expanduser("~"), ".ftp_transfer_ux.json")


def load_ux_state():
    """读上次的界面状态。文件不存在 / 读坏 / 不是 dict → 一律返回 None。

    None 的语义是"**等于首次启动**"，调用方据此什么都不回填 ——
    所以这里绝不能返回 `{}` 或半成品：那会让调用方以为"读到了一份空状态"。
    """
    if os.path.exists(UX_STATE_FILE):
        try:
            with open(UX_STATE_FILE, 'r', encoding='utf-8') as f:
                d = json.load(f)
            if isinstance(d, dict):
                return d
        except:
            return None
    return None


def save_ux_state(state):
    """写界面状态。失败静默（理由见上）。"""
    try:
        with open(UX_STATE_FILE, 'w', encoding='utf-8') as f:
            json.dump(state, f, indent=2)
    except:
        pass


# ===================== 阶段二：多套 FTP 配置 =====================
# 【阶段二-新增】`CONFIG_FILE` 从"一份扁平的 5 键"升级成"多套 + 一个当前项"。
#
# 【为什么升级必须做到"只改内存、不动磁盘"】
#   旧文件是 `{host, port, user, password, remote_dir}`。新版读到一个没有 `profiles`
#   键的 dict 时，**在内存里**把它包成 `[{"name": "默认", …原样 5 键}]`，磁盘上一个字节
#   都不碰 —— 直到用户真的在界面上点了一次保存。这样"新版有问题就退回旧 exe"这条路
#   始终是通的（旧 exe 读到的还是那个旧格式的文件）。主动改写等于把回退路径当场焊死。
#
# 【`load_last_config` / `save_config` 一个字都不改】
#   它们仍在 `ui_dialogs.FTPSettingsDialog` 里被调用（`_knobs.py` 里也登记着）。
#   新增的是**另一组**函数，不是把旧函数改写成新的。旧函数"整份覆盖"的语义因此保留，
#   而"整份覆盖会把别的配置清掉"这个坑由调用方换函数来避（见 `save_profile`）。
CONFIG_VERSION = 2

# 旧扁平格式升级上来时用的名字。写成常量而不是散在代码里：升级提示里要念它，
# 用户改名之后又会出现第二个"默认"，两处各写一份迟早对不上。
DEFAULT_PROFILE_NAME = "默认"

_PROFILE_STR_KEYS = ("user", "password")     # 只做去空白，不做别的
_PROFILE_REQUIRED = ("host",)                # 没有它就等于这一套不可用


def _clean_profile(d):
    """把一份配置字典验成规范形态，不合法返回 None。

    【为什么逐项验类型】与 `load_ux_state` 那边同一条规矩：这个 JSON 用户能直接编辑，
    一个 `{"port": "abc"}` 不该能让 `ftplib` 在连的时候才炸，更不该让**别的**几套
    跟着一起作废。所以是"这一套不合法就丢掉这一套"，不是"有一处坏就整份不要"。
    """
    if not isinstance(d, dict):
        return None
    name = d.get("name")
    if not isinstance(name, str) or not name.strip():
        return None
    host = d.get("host")
    if not isinstance(host, str) or not host.strip():
        return None
    try:
        port = int(d.get("port", 21))
    except (TypeError, ValueError):
        port = 21
    if not (1 <= port <= 65535):
        port = 21
    remote_dir = d.get("remote_dir")
    if not isinstance(remote_dir, str):
        remote_dir = "/"
    out = {"name": name.strip(), "host": host.strip(), "port": port,
           "remote_dir": remote_dir}
    for k in _PROFILE_STR_KEYS:
        v = d.get(k)
        out[k] = v if isinstance(v, str) else ""
    return out


def load_profiles():
    """读多套 FTP 配置。返回 `(profiles, active)`。

    * 文件不存在 / 读坏 / 不是 dict  → `([], "")`，语义是"等于没配过"；
    * 新格式（有 `profiles` 列表）    → 逐项过 `_clean_profile`，坏的那套丢掉；
    * **旧扁平格式（有 host、没有 profiles）→ 在内存里包成一套，磁盘不动**（见上）。
    `active` 指向的那套要是被丢掉了（名字对不上），回落到第一套 ——
    界面必须永远有个"当前配置"，否则 ⚙ 打开时不知道该拿什么当初值。
    """
    if not os.path.exists(CONFIG_FILE):
        return [], ""
    try:
        with open(CONFIG_FILE, 'r', encoding='utf-8') as f:
            d = json.load(f)
    except:
        return [], ""
    if not isinstance(d, dict):
        return [], ""

    if isinstance(d.get("profiles"), list):
        raw = d["profiles"]
    elif any(k in d for k in _PROFILE_REQUIRED):
        # 旧扁平格式 → 内存里升级。**不写回磁盘**，这一步没有副作用。
        raw = [dict(d, name=d.get("name") or DEFAULT_PROFILE_NAME)]
    else:
        return [], ""

    profiles = []
    seen = set()
    for item in raw:
        p = _clean_profile(item)
        if p is None:
            continue
        if p["name"] in seen:        # 重名会让"当前配置"说不清是谁，后来的丢掉
            continue
        seen.add(p["name"])
        profiles.append(p)

    if not profiles:
        return [], ""
    active = d.get("active")
    if not isinstance(active, str) or active not in seen:
        active = profiles[0]["name"]
    return profiles, active


def save_profiles(profiles, active):
    """写多套配置。失败静默（口径与 `save_config` / `save_ux_state` 一致）。"""
    try:
        with open(CONFIG_FILE, 'w', encoding='utf-8') as f:
            json.dump({"version": CONFIG_VERSION,
                       "active": active,
                       "profiles": list(profiles)}, f, indent=2)
    except:
        pass


def save_profile(name, cfg):
    """保存**一套**配置，其余的原样保留。`name=None` 时退化成旧的整份覆盖。

    【`None` 这条分支是硬要求，不是便利】`ui_dialogs.FTPSettingsDialog._on_save`
    在阶段二之前调的就是 `save_config(config)`（整份覆盖）。改造只允许动它那一行，
    所以那个"旧行为"必须由本函数接住 —— 否则旧调用方会拿到一个新语义，
    而"调用方以为改的是旧行为、实际是新行为"正是 §7.4 那个 P0 的形态。
    """
    if name is None:
        return save_config(cfg)
    p = _clean_profile(dict(cfg, name=name))
    if p is None:
        return
    profiles, active = load_profiles()
    for i, old in enumerate(profiles):
        if old["name"] == p["name"]:
            profiles[i] = p
            break
    else:
        profiles.append(p)
    save_profiles(profiles, active or p["name"])


def delete_profile(name):
    """删一套。返回删除后的 `(profiles, active)`。

    **不删最后一套**：删光了界面上就没有"当前配置"，⚙ 打开时拿不到初值，
    而用户多半只是想清掉多余的那几套。要清空这个文件，删文件比删配置更直接。
    """
    profiles, active = load_profiles()
    left = [p for p in profiles if p["name"] != name]
    if not left:
        return profiles, active
    if active == name:
        active = left[0]["name"]
    save_profiles(left, active)
    return left, active


# ===================== 阶段四：定时任务 =====================
# 【为什么不进状态库】定时任务配置是**工具配置**（跟 FTP 那几套同一层），不是传输状态。
#   塞进状态库要改 schema —— 那是 STOP 条件 1，而且语义上也不对：
#   状态库回答"这个文件传过没有"，它不该同时回答"明天两点要不要跑"。
#
# 【这个文件用户能直接编辑，所以每一项都逐条验】口径与 `load_profiles` 那边逐字相同：
#   * 一个坏字段只让**那一条任务**不合法（丢掉它），不牵连其余任务；
#   * 触发时间/星期/日期都要能**原样拼进 schtasks 的命令行**，所以这里用**正则**卡死，
#     不是"大概是个字符串就收" —— 那是把命令行注入的口子留在用户可编辑的 JSON 上。
SCHEDULE_FILE = os.path.join(os.path.expanduser("~"), ".ftp_transfer_schedules.json")

# 无头模式的日志落点。**这是无头模式下唯一的日志终点** ——
# 打包 `console=False` 会把 stdout 整个吞掉，界面又没人看。
# 之所以是"一个目录 + 每天一个文件"而不是"一个文件滚到底"：
# 定时任务跑一年就是一年，单文件会长到打不开；按天切还省了轮转逻辑。
SCHEDULE_LOG_DIR = os.path.join(os.path.expanduser("~"), ".ftp_transfer_logs")

SCHEDULE_VERSION = 1

# schtasks 的任务名统一带这一层"文件夹"前缀。两个用处：
#   ① 用户打开任务计划程序时，我们的任务收在自己的文件夹里，不跟系统的混在一起；
#   ② 自检/批量清理时前缀就是判据 —— 只认**自己建的那些**，绝不碰别人的任务。
SCHEDULE_TOP_FOLDER = "FTP传输工具"

# 星期代号：schtasks 的 `/D` 吃的是这三个字母的大写。顺序即界面上复选框的顺序。
SCHEDULE_DAY_CODES = ("MON", "TUE", "WED", "THU", "FRI", "SAT", "SUN")

SCHEDULE_TRIGGER_TYPES = ("daily", "weekly", "hourly", "once")

# `last_status` 的取值域。**`aborted` 单列**（不是 failed 的一种）：它可能已经有
# 49 个文件成功了，把它显示成"失败"与显示成"成功"一样是假消息 —— 见退出码那段。
SCHEDULE_STATUSES = ("ok", "partial", "failed", "aborted", "skipped", "error")

# 退出码约定（`22_plan_stage4.md` §⑥）。**这五档是"没有邮件渠道"时的全部表达力**，
# schtasks 只看得到进程退出码，所以每一档都必须在日志里有一句人话对应。
SCHEDULE_EXIT_OK = 0            # 全成功（失败数 == 0）
SCHEDULE_EXIT_PARTIAL = 1       # 部分失败（有成功、也有失败）
SCHEDULE_EXIT_LOCKED = 2        # 让路：撞上另一个正在跑的任务（**不重试**）
SCHEDULE_EXIT_FAILED = 3        # 完全失败（一个都没成功、且有失败）
SCHEDULE_EXIT_CANNOT_START = 4  # 起不来：配置/路径/状态库不可用

_RE_HHMM = re.compile(r"^(?:[01]\d|2[0-3]):[0-5]\d$")
_RE_DATE = re.compile(r"^(?:0[1-9]|1[0-2])/(?:0[1-9]|[12]\d|3[01])/(?:19|20)\d\d$")


def _clean_trigger(d):
    """把一条触发规则验成规范形态，不合法返回 None。

    【为什么每种类型必须验到"能直接拼命令行"的程度】这个 dict 是从**用户可编辑的
    JSON** 里读出来的，而它的最终去向是 `schtasks` 的 `/ST` `/D` `/MO` `/SD`。
    "大概是个字符串就收"等于把命令行注入的口子留在那份 JSON 上。
    所以每种类型都卡到最后一位：时间必须是 `HH:MM`、星期必须是那七个代号之一、
    `every_hours` 必须是 1~23 的整数、日期必须是 `MM/DD/YYYY`。
    """
    if not isinstance(d, dict):
        return None
    t = d.get("type")
    if t not in SCHEDULE_TRIGGER_TYPES:
        return None
    tm = d.get("time")
    if not isinstance(tm, str) or not _RE_HHMM.match(tm):
        return None
    out = {"type": t, "time": tm}
    if t == "weekly":
        days = d.get("days")
        if not isinstance(days, list):
            return None
        # 去重 + **按 SCHEDULE_DAY_CODES 的顺序**重排。重排是硬要求：同一个选择
        # 在界面上勾选的先后不同会得到不同的命令行，两相对照时看不出是同一条规则。
        picked = [c for c in SCHEDULE_DAY_CODES if c in days]
        if not picked:
            return None
        out["days"] = picked
    elif t == "hourly":
        n = d.get("every_hours")
        if not isinstance(n, int) or isinstance(n, bool) or not (1 <= n <= 23):
            return None
        out["every_hours"] = n
    elif t == "once":
        dt = d.get("date")
        if not isinstance(dt, str) or not _RE_DATE.match(dt):
            return None
        out["date"] = dt
    return out


def _clean_schedule(d):
    """把一条定时任务验成规范形态，不合法返回 None。

    【名字为什么卡得比别处严】它有两个去向，两个都不容忍某些字符：
      * 它要拼成 schtasks 的任务名（`/TN "FTP传输工具\\<名字>"`）—— 一个反斜杠
        就会在任务计划程序里**造出一层子文件夹**，一个引号就能把参数断成两截；
      * 它要拼成日志文件名 —— `\\ / : * ? " < > |` 在 Windows 上根本存不下来。
    所以名字里**禁止** `\\ / : * ? " < > |` 与控制字符。中文、空格、下划线都留着
    —— 用户起的是"每日备份"这种名字，卡成 ASCII 才是本末倒置。
    """
    if not isinstance(d, dict):
        return None
    name = d.get("name")
    if not isinstance(name, str):
        return None
    name = name.strip()
    if not name or len(name) > 60:
        return None
    if any(c in name for c in '\\/:*?"<>|') or any(ord(c) < 32 for c in name):
        return None
    trig = _clean_trigger(d.get("trigger"))
    if trig is None:
        return None
    params = d.get("params")
    if not isinstance(params, dict):
        params = {}
    out = {
        "name": name,
        "enabled": bool(d.get("enabled", True)),
        "trigger": trig,
        # `params` **原样带着**，不在这里验：它的每一项由界面层那个白名单
        # （`_apply_ux_state_dict`）逐项验，两份验证必然漂移 —— 一份就够。
        "params": params,
    }
    # 运行痕迹：读回来只为显示与"上次任务怎么了"的提示，验得浅一点，
    # 但**类型不对就丢掉**（不是留着让界面去炸）。
    for k in ("last_run", "last_log", "last_status"):
        v = d.get(k)
        if isinstance(v, str):
            out[k] = v
    if out.get("last_status") not in SCHEDULE_STATUSES:
        out.pop("last_status", None)
    lc = d.get("last_counts")
    if isinstance(lc, dict):
        out["last_counts"] = {
            k: (v if isinstance(v, int) and not isinstance(v, bool) else 0)
            for k, v in lc.items() if k in ("copied", "skipped", "failed")}
    ec = d.get("exit_code")
    if isinstance(ec, int) and not isinstance(ec, bool):
        out["exit_code"] = ec
    return out


def load_schedules():
    """读全部定时任务。返回一个 list（**永远不是 None**）。

    文件不存在 / 读坏 / 不是 dict / `schedules` 不是 list → 一律 `[]`，
    语义是"一条定时任务都没有" —— 与 `load_profiles` 的 `([], "")` 同一口径。
    **坏的那一条丢掉，其余照收**：用户手改坏了一条不该让另外三条一起消失。

    【重名怎么办】后来的丢掉。判据与 `load_profiles` 那边逐字同源：
    名字是这条任务在本文件里、在界面上、在 schtasks 里的**唯一身份**，
    两条同名会让"删的是哪一条""跑的是哪一条"都说不清。
    """
    if not os.path.exists(SCHEDULE_FILE):
        return []
    try:
        with open(SCHEDULE_FILE, 'r', encoding='utf-8') as f:
            d = json.load(f)
    except:
        return []
    if not isinstance(d, dict):
        return []
    raw = d.get("schedules")
    if not isinstance(raw, list):
        return []
    out = []
    seen = set()
    for item in raw:
        s = _clean_schedule(item)
        if s is None or s["name"] in seen:
            continue
        seen.add(s["name"])
        out.append(s)
    return out


def save_schedules(schedules):
    """整份写回。失败静默（口径与 `save_config` / `save_profiles` 一致）。

    ⚠️ **调用方几乎不该直接用它** —— 它是 `save_schedule` / `delete_schedule` /
    `update_schedule_run` 的底座。直接调它 = 拿一份可能已经过期的内存列表
    盖掉磁盘上的全部任务，那正是 `_probe_profile_clobber.py` 实测过的丢失形态。
    只有"我手里这份就是全部"时才可以直接调（界面上的保存按钮即是）。
    """
    try:
        os.makedirs(os.path.dirname(SCHEDULE_FILE), exist_ok=True)
    except:
        pass
    try:
        with open(SCHEDULE_FILE, 'w', encoding='utf-8') as f:
            json.dump({"version": SCHEDULE_VERSION,
                       "schedules": list(schedules)}, f, indent=2,
                      ensure_ascii=False)
    except:
        pass


def save_schedule(rec):
    """保存**一条**任务（按名字替换或追加），其余的原样保留。

    形态照 `save_profile`，**不照 `save_config`**：无头模式与界面可能同时活着
    （定时任务跑的时候用户正好开着工具），盲写整份会把对方刚存的另一条冲掉。
    所以这里一律"先读、改一条、再整份写回"。
    """
    s = _clean_schedule(rec)
    if s is None:
        return load_schedules()
    cur = load_schedules()
    for i, old in enumerate(cur):
        if old["name"] == s["name"]:
            cur[i] = s
            break
    else:
        cur.append(s)
    save_schedules(cur)
    return cur


def delete_schedule(name):
    """删一条。返回删除后的 list。

    【为什么不在这里碰 schtasks】删除顺序是硬要求「**先删系统、后删配置**」
    （`22_plan_stage4.md` §③）：反过来的话，`schtasks /Delete` 一失败就留下一颗
    **指向已删配置的孤儿任务** —— 它到点还会跑，还会报"任务名不存在"，
    而用户界面上已经看不到它了。所以顺序由调用方（界面层）保证，本函数只管配置。
    """
    left = [s for s in load_schedules() if s.get("name") != name]
    save_schedules(left)
    return left


def update_schedule_run(name, **fields):
    """只更新一条任务的**运行痕迹**（`last_run` / `last_status` / …）。

    【为什么单开一个函数而不是让调用方 `save_schedule(rec)`】
    无头进程手里那份记录是**开跑之前**读的，跑完再整份写回，就会把这段时间里
    用户在界面上改的参数一起盖掉 —— 用户改了明天的目标目录，被一次定时任务的
    收尾悄悄还原成旧的。所以这里**先重读、只改这几个键、再写回**。
    """
    cur = load_schedules()
    for s in cur:
        if s["name"] == name:
            s.update(fields)
            break
    else:
        return cur
    save_schedules(cur)
    return cur


def schedule_log_path(name, day=None):
    """无头模式这次的日志文件路径：`<SCHEDULE_LOG_DIR>\\<任务名>_<YYYYMMDD>.log`。

    【按天切，不按次切】同一个任务一天跑 24 次（每小时一次）就是 24 个文件，
    翻看起来比一个文件还难。按天聚在一起，"今天这个任务跑了什么"是一个文件。
    """
    if day is None:
        day = datetime.date.today()
    safe = "".join(c for c in str(name) if c not in '\\/:*?"<>|').strip() or "task"
    return os.path.join(SCHEDULE_LOG_DIR, "%s_%s.log" % (safe, day.strftime("%Y%m%d")))


# ---------- schtasks 命令行 ----------
# 【为什么参数拼装放在 config.py 而不是界面层】它是**纯函数**（给一条任务记录、
#   返回一个 argv 列表），符合本模块"只有常量与无副作用的小函数"的定位；
#   而且它必须**可被单独验证** —— 一旦真的调了 schtasks，验证的代价就是
#   在用户系统里留下一颗计划任务。纯函数可以直接断言字符串。
#
# 【为什么不拼成一个字符串交给 shell】`shell=True` + 字符串拼接 = 只要任务名里
#   有一个字符没被正确转义，用户可编辑的 JSON 就能变成**任意命令执行**。
#   一律返回 **argv 列表**、由调用方 `subprocess.run(list, shell=False)` 执行，
#   引号由 Python 按 Windows 规则处理，没有 shell 可以注入。

def _win_arg(s):
    """按 Windows 命令行规则给一个参数加引号（只在需要时）。

    规则（`subprocess.list2cmdline` 反向）：反斜杠只有在**紧邻引号**时才需要翻倍。
    这里刻意**不**调用 `subprocess.list2cmdline` —— 那一个函数会把整个 argv 拼成
    一整条命令行，而我们要的只是"`/TR` 的**值**"这**一个** argv 元素。
    """
    s = str(s)
    if s and not any(c in s for c in ' \t"'):
        return s
    out = ['"']
    n_back = 0
    for c in s:
        if c == "\\":
            n_back += 1
            continue
        if c == '"':
            out.append("\\" * (n_back * 2 + 1))
            out.append('"')
        else:
            out.append("\\" * n_back)
            out.append(c)
        n_back = 0
    out.append("\\" * (n_back * 2))
    out.append('"')
    return "".join(out)


def schtasks_task_name(name):
    """界面上的名字 → schtasks 的完整任务名（带顶层文件夹）。"""
    return "%s\\%s" % (SCHEDULE_TOP_FOLDER, name)


def schtasks_run_command(exe_path, name, script=None):
    """`/TR` 的值：`"<exe>" [ "<脚本>" ] --run-task "<名字>"`（**一个** argv 元素）。

    【`script` 是给"从源码跑"用的】打包好的 exe 直接就是 `<exe> --run-task X`
    两段。而**开发/自检时没有 exe** —— 那时程序是 `pythonw.exe file_transfer.py`，
    `/TR` 必须把那个脚本路径也带上，否则 `--run-task` 会被 python 自己吃掉
    （它认不得这个开关，会当文件名找）。
    默认 `None` ⇒ 拼出来与加这个参数之前**逐字相同**，既有调用方与既有验收
    （`_t_sched_args.py` 的往返验证）一行都不用改。
    """
    parts = [_win_arg(exe_path)]
    if script is not None:
        parts.append(_win_arg(script))
    parts += ["--run-task", _win_arg(name)]
    return " ".join(parts)


def schtasks_create_args(exe_path, rec, script=None):
    """把一条任务记录拼成 `/Create` 的 argv。

    ⚠️ **一个 `/RU` / `/RP` / `/RL` 都不加** —— 这不是省略，是本设计的前提：
      * 不加 `/RU` / `/RP` ⇒ 任务**以当前用户身份、且只在用户登录时运行**；
      * 这正是路线 B 的硬前提：无头模式要 `tk.Tk()`，得有一个可用窗口站；
      * 也正好与共享文件夹的前提**是同一条** —— SMB 凭据是登录会话级的，
        配成"不管用户是否登录都运行"的话，共享源会**全部失败**。
      * 不加 `/RL` ⇒ 普通权限，因此**创建任务不需要管理员**。
    加 `/F` 是硬要求：编辑一条已有任务走的是同一条路（"保存即生效"），
    没有 `/F` 会被"任务已存在"顶回来。
    """
    trig = rec["trigger"]
    args = ["schtasks", "/Create", "/TN", schtasks_task_name(rec["name"]),
            "/TR", schtasks_run_command(exe_path, rec["name"], script), "/F"]
    t = trig["type"]
    if t == "daily":
        args += ["/SC", "DAILY", "/ST", trig["time"]]
    elif t == "weekly":
        args += ["/SC", "WEEKLY", "/D", ",".join(trig["days"]), "/ST", trig["time"]]
    elif t == "hourly":
        # `/MO N` 的语义是"每 N 个 <SC 单位>"，`/SC HOURLY /MO 4` = 每 4 小时。
        # `/ST` 在这里给的是**起点**（第一次触发的时刻），不是间隔。
        args += ["/SC", "HOURLY", "/MO", str(trig["every_hours"]), "/ST", trig["time"]]
    elif t == "once":
        args += ["/SC", "ONCE", "/ST", trig["time"], "/SD", trig["date"]]
    else:
        return None
    return args


def schtasks_delete_args(name):
    """`/Delete` 的 argv。`/F` 是硬要求 —— 没有它 schtasks 会**交互式地问一句
    "确实要删除吗"**，而我们是无人值守地调它，那一问就是卡死。"""
    return ["schtasks", "/Delete", "/TN", schtasks_task_name(name), "/F"]


def schtasks_query_args(name):
    """`/Query` 的 argv（XML 形态）。自检 §④ 用它读回 `Task To Run` 比对路径。"""
    return ["schtasks", "/Query", "/TN", schtasks_task_name(name), "/XML"]


def schtasks_create_xml_args(name, xml_path):
    """`/Create /XML` 的 argv —— 把**改过**的 XML 注册回去时用（见 §三-B）。

    【为什么会有这么一条路】`schtasks` 的命令行**没有** `StartWhenAvailable`
    这类设置的开关（`/Create` 支持 `/RU /RP /RL /IT /Z /F /XML /TN /TR /SC
    /MO /D /M /ST /RI /DU /K /SD /ED /DELAY` —— 就是这个没有）。
    要落这一条只有两条路：
      * 读回系统自己生成的 XML → 改一个元素 → `/Create /XML` 注册回去；← **走这条**
      * 绕道 PowerShell 的 `Set-ScheduledTask` —— **不走**：那等于给程序加一条
        「运行环境里得有 powershell、且 ScheduledTasks 模块可用」的新前提，
        而上面那条**不引入任何新依赖**。
    """
    return ["schtasks", "/Create", "/TN", schtasks_task_name(name),
            "/XML", xml_path, "/F"]


# 【注册之后要补进去的设置】`(元素名, 值)` 列表。
#
# 语义：**错过触发时刻就尽快补跑一次**。
# 为什么非补不可：`schtasks` 建出来的任务默认**没有** `<StartWhenAvailable>`，
# 也就是"到点没开机的那天，那一次就没了，不补"。而用户的原话场景是
# 「**每天凌晨 2 点** NAS → F 盘」—— 凌晨 2 点正是机器最可能睡眠或关机的时候。
# 不补跑的话，那台机器会**看起来设好了、实际上从没跑成过**，
# 而且失败得毫无痕迹（任务计划程序不会告诉你"今天跳过了"）。
#
# 2026-09-20 真机读回 XML 实测：`<Settings>` 段全是系统默认，生产代码一条没覆盖。
# 见 `_checks\_nightly\46_step_daily_selftest.md` §五。
# ⚠️ **不要在这里顺手加 `DisallowStartIfOnBatteries`**：用户明确裁定"只做 B"
#    （只加错过补跑），不要 A（全套改）。电池那条牵动的是"笔记本拔电时到底跑不跑"，
#    是另一个决定，得单独拍。
SCHEDULE_EXTRA_SETTINGS = (("StartWhenAvailable", "true"),)


def schtasks_patch_schedule_xml(text, key, value):
    """把 `key=value` 这条设置补进 `schtasks /Query /XML` 读回来的那段 XML。

    返回**新的文本**；做不了时返回 `None` —— 让调用方**如实报"这一条没生效"**，
    绝不静默。

    【为什么是字符串进出，而不是 ElementTree】
    Task Scheduler 的 XML 带默认命名空间
    （`xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task"`），
    用 ElementTree 回写会把它变成 `ns0:` 前缀，还得一并处理声明、缩进、
    命名空间这些**与本次修改无关**的东西 —— 出问题的面比要改的那一个元素大得多。
    这里只动一个元素，字符串进出**每一处改动都看得见**；
    前提不满足（`<Settings>` 不在）就直接返回 `None`，不会悄悄写出一份坏 XML。
    """
    if not text or "<Settings>" not in text or "</Settings>" not in text:
        return None
    open_tag, close_tag = "<%s>" % key, "</%s>" % key
    want = "%s%s%s" % (open_tag, value, close_tag)
    if open_tag in text:
        # 已经有同名元素 → 只改值。一条任务的 XML 里同一个 key 只出现一次。
        start = text.index(open_tag)
        end = text.find(close_tag, start)
        if end < 0:
            return None
        return text[:start] + want + text[end + len(close_tag):]
    cut = text.index("</Settings>")
    return text[:cut] + "  " + want + "\r\n  " + text[cut:]


# ===================== 阶段二：设置页的默认参数 =====================
# 【口径统一：界面上填的都是"重试次数 N"，代码里用的是"尝试次数 N+1"】
#   一次成功的传输不在"重试"里计数，所以"重试 3 次"= 试 1 次 + 重试 3 次 = 4 次尝试。
#   这与 `_copy_with_retry(attempts=4)` / `FTP_RECONNECT_ATTEMPTS=4` 的既有默认值
#   **逐字对上**：界面上看到 3，代码里就是今天那个 4，默认行为一个字节都不变。
#   ⚠️ 将来谁改这里的默认值，必须同时看一眼上面那两个常量的注释（它们的 4 是算出来的，
#      不是挑出来的），别把"三级退避 1/3/9 秒"改成了两级还留着三级曲线的名。
DEFAULT_TIMEOUT_S = 10           # = FTPHelper/FTPPooledConn 的 timeout 默认值
DEFAULT_RETRY_COUNT = 3          # → attempts = 4，同 _copy_with_retry 的默认
DEFAULT_RECONNECT_COUNT = 3      # → attempts = 4，同 FTP_RECONNECT_ATTEMPTS

# 输入范围。**上界不是审美**：超时 0 秒会让每次连接当场失败，重试 0 次等于关掉重试
# 机制本身（那是"删功能"，不是"调参数"），所以下界都留了 1。
TIMEOUT_RANGE_S = (1, 300)
RETRY_COUNT_RANGE = (0, 10)
RECONNECT_COUNT_RANGE = (0, 10)

# 日志级别。**默认必须是"详细"**：常规档会滤掉扫描心跳 / 回收 / 库维护三类行，
# 默认选常规等于把今天屏幕上有的东西弄没了 —— 那是行为变化，不是新增开关。
LOG_LEVEL_VERBOSE = "verbose"
LOG_LEVEL_NORMAL = "normal"
DEFAULT_LOG_LEVEL = LOG_LEVEL_VERBOSE
LOG_LEVELS = (LOG_LEVEL_VERBOSE, LOG_LEVEL_NORMAL)

# 【常规档滤掉的三类】判据是**行首前缀**，与发这三类日志的地方一一对应：
#   "已扫描到 "  → 流式扫描的心跳（FTP_SCAN_LOG_EVERY 那条，20 万文件 40 行）
#   "[回收] "    → 远端残留 .part 的清理小结
#   "[库维护] "  → 启动时那次库维护小结
# 【为什么只有三类、而不是给每条日志分级】给上百个 `_threadsafe_log` 调用点逐一打标
# 是另一个量级的改动，且极易漏标（漏标的表现是"选了常规还是有那行"，很难发现）。
# 这三类的共同点是"例行公事、且自成一行、且行首稳定"，用前缀能**确定地**认出来。
ROUTINE_LOG_PREFIXES = ("已扫描到 ", "[回收] ", "[库维护] ")

DEFAULT_DB_KEEP_DAYS = DB_PRUNE_AFTER_DAYS       # 730，与启动维护的默认一致

DB_KEEP_DAYS_RANGE = (30, 3650)

DEFAULT_LOG_EXPORT_DIR = ""      # 空 = 沿用系统默认（弹保存框时的起始目录）


# ===================== 功能B：实时速度 / ETA =====================
# 【为什么这几个量放这里，而不写死在方法里】
#   它们是"调起来才知道合不合适"的量。散在 file_transfer.py 里，想试一个别的
#   窗口长度就得改生产代码；集中在这里，与 UI_DRAIN_INTERVAL_MS /
#   LOG_FLUSH_INTERVAL_S 同一层，口径一致。**本模块不认识它们的含义**，
#   与上面 UX_STATE_FILE 一样：这里只存数，怎么用是界面层的事。

SPEED_POLL_INTERVAL_MS = 500
# 界面刷新周期。**与进度条的 150ms 心跳刻意不同频**：进度条是"跟手"的量，
# 速度是"给人看趋势"的量，500ms 足够，再快只是让数字抖动得更厉害。

SPEED_WINDOW_S = 10.0
# 滑动窗口长度："最近 10 秒传了多少字节"。窗口越长越平滑、对突发的反应越慢。
# 10 秒是用户定的：足以跨过"一个大文件传完、下一个刚开始"的空档，
# 又不至于把一分钟前的速度一直拖着。

SPEED_EMA_ALPHA = 0.3
# EMA 平滑系数：new = 0.3 * 瞬时 + 0.7 * 旧值。
# 窗口本身已经平滑过一次，再加一层 EMA 是为了压住"某个 500ms 周期恰好没有
# 文件传完"造成的锯齿（DICOM 大文件常见：一个文件传 30 秒，中间十几个周期
# 一个字节都没计上）。系数越大越跟手、越小越稳。

SPEED_ETA_MIN_S = 10.0
# 已传时长不足这么多秒时**不显示 ETA**。
# 【为什么必须有这一条】ETA = 剩余 / 速度，而任务刚开始时速度那一项是从
# 一两个样本里算出来的，分母极小、噪声极大 —— 会报出"剩余 3 秒"然后一路涨到
# "剩余 2 小时"。宁可不显示，也不显示一个必然不准的数。
#
# 【另一条 ETA 的前提，写在这里因为它是**设计事实**而不是可调参数】
#   ETA 只在**总数已知**（`self._total > 0`）时才可能算得出来。而本项目里
#   本地→本地/上传路径的总数**按设计恒为 0**（阶段4 改成流式扫描，扫完才知道有几个），
#   所以：**本地传输永远不会显示 ETA，FTP 传输在扫描结束后才显示**。
#   这不是本次没做，是数据本身不够 —— 详见 PROJECT_STATE §7.6。

# ===================== 功能C1：界面主题（ttkbootstrap） =====================
# 【这里只放**名字**，不放库】
#   本模块**不 import ttkbootstrap**。依赖方向是 `file_transfer → … → config`，
#   方向是单向的；把 UI 库拖进常量模块会把这条边反过来。
#   与 SPEED_* 一样：**本模块不认识这个字符串的含义**，怎么用是界面层的事。

C_UI_THEME = "cosmo"
# 界面主题名。**当前用的是 ttkbootstrap 1.20.4 的主题名空间。**
#
# 【⚠️ 一条踩过的坑，写在这里免得下次再踩】
#   ttkbootstrap **2.x 把主题名换掉了**。`litera` / `cosmo` / `flatly` / `journal` …
#   这一批是 **1.x**（照着 Bootstrap 4/5 命名）的；2.x 只有
#   bootstrap-light / sandstone / catppuccin / nord / tokyo-night 那一批，
#   **`litera` 和 `cosmo` 在 2.2.3 里根本不存在**（实测 `theme_names()` 里没有）。
#   `pip index versions ttkbootstrap` 同时给得出 1.20.4 和 2.2.3 —— 所以选版本
#   **等于**选主题名空间。本项目钉在 **1.20.4**，理由：
#     ① 用户点名要的就是 litera / cosmo；
#     ② 1.20.4 是纯 Python，在 Python 3.12.4 上实测可用；
#     ③ 换来换去只会让"哪个名字对应哪套配色"更难对。
#   **升级 2.x 之前必须先改这个字符串**，否则启动时 `theme_use` 会抛
#   `TclError: theme "cosmo" doesn't exist`。
#
# 【为什么是 cosmo 而不是 litera】判据是用户给的"更像医疗软件"：
#   cosmo 的主色 #2780e3 是标准的"临床蓝"，界面扁平、白色底、边框 #ced4da 很轻；
#   litera 的主色 #4582ec 偏靛紫，secondary #adb5bd 更淡 —— 淡到那条灰色的
#   线程数提示文字对比度不够。两者的**行高与字体完全相同**（主题不改基础字号，
#   实测 `TkDefaultFont` 在两边都是 Microsoft YaHei UI 9pt），
#   所以这是纯配色取舍，不影响下面那个尺寸盒子。
