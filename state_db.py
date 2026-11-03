# -*- coding: utf-8 -*-
"""传输状态库：任务锁 / 心跳 / 续传记录 / 库维护。

【第四刀】整段从 file_transfer.py 第 51–511 行**逐字**搬来（见 PROJECT_STATE §7.4.10）。
搬家判据是 **AST 等价**，不是"跑通测试" —— 被搬的两个类一行都没有改动
（搬完由 `_verify_split.py` 逐节点比对、`_check_anchor_shift.py` 逐行保真来证）。

依赖方向：`file_transfer -> state_db -> config`（单向，无环）。
本模块**不引用主模块的任何顶层名**（侦察实证 0 处），所以不需要反向 import ——
这也是它能被搬出来的前提：一旦这里出现 `file_transfer.X`，就是循环依赖。

**没有任何 UI 调用**：本模块不 import tkinter，一个 `root.after` 都没有
（`check_stage1_ast_whitelist.py` / `check_stage1_after_audit.py` 把本文件纳入了断言范围）。

**Schema 一个字节都没动**：`config._SCHEMA_DDL` 与 `config.DB_SCHEMA_VERSION` 仍是唯一来源，
本模块只是**读**它们（25 处 config.X 访问里包括 4 处），不定义、不复制。

线程模型（本类最重要的一条约定，破坏了就会踩 sqlite3 的跨线程限制）：
    写连接 self._conn    —— 由 _db_writer_loop 线程独占，外部一律不准碰；
    读连接 _read()       —— **每线程一条**，存在 threading.local 里，调用方不准 close；
    短连接 _short_conn() —— 自开自关，任意线程可调。**现在是三处用途**：
                            ① 建库/迁移（ensure_schema）② 带写事务的短操作
                            （claim_task / abort_task / maintenance / purge）
                            ③ 开关 `config.DB_REUSE_READ_CONN=False` 时，
                            读路径**逐字退回**到它（见 `_read`）。

【连接复用-新增】读路径原来一律走短连接，M4 实测那是扫描成本的 **96.6%**
（每文件一次 connect+close，与"这个文件传过没有"毫无关系）。现在默认走读连接。
**写路径一个字没动** —— db-writer 独占写连接这条约定不因本刀而变。

为什么不 re-export config 里的东西：与主模块头部同款理由 ——
`from config import X` 会抄出一份**独立副本**，改 config 里的值对副本无效。
"""
import contextlib
import gc
import os
import shutil
import sqlite3
import threading
import time

import config

# 【2026-11-03 加固】**判重记录的列序只写一次**。
#
# 原来 `get_file_state` 里 SELECT 的列名和 `keys` 元组是**两份手写清单**，
# 靠"看着一样"保持一致。这正是本模块最怕的那类静默错位：把 SELECT 里两项换个位置，
# 或者加一列忘了同步 keys，症状是"拿 dst_path 去比 src_size"⇒ 全部不跳过 ⇒ 每次
# 全量重传，而库、SQL、返回值看上去全都正常，没有任何地方报错。
# 现在两处都从这一个元组派生，且 `zip(..., strict=True)` 兜住长度不一致。
_FILE_STATE_COLS = ("filename", "task_id", "src_path", "src_size", "src_mtime",
                    "mtime_precision", "dst_path", "dst_size", "status",
                    "src_deleted", "attempts", "last_error")
_FILE_STATE_COLS_SQL = ", ".join(_FILE_STATE_COLS)

# 【连接复用-新增】每线程一条**读**连接。用 threading.local 而不是全局字典，是因为
# `threading.local()` 的语义就是"每个线程看到自己那一份" —— 这一步**正是本类第一条
# 约定所要求的**（见文件头"线程模型"）：sqlite3 连接**跨线程用会踩限制**，不能共享。
#
# ⚠️【实测更正，2026-09-20】原文接着写"线程结束自动回收"。**那句话不准确**：
#   线程退出时，它那份小字典与里面的连接**成环**，不会立刻析构 ——
#   要等**一次垃圾回收**才真正 close()。在那之前，库文件在 Windows 上
#   仍然被占着（`os.replace` 报 WinError 32，见 `release_db_file()`）。
#   实测：3 个工作线程查过并退出后，`gc.collect()` 一跑，库文件才挪得动。
#
# 为什么放在模块级而不是 self 上：同一个进程里可能有多个 TransferStateDB 实例
# （测试夹具就会造好几个），但**同一线程、同一个 db_path** 才该复用。
# 所以真正的键是 (db_path)，存在下面每个线程自己的小字典里。
_read_tls = threading.local()


def _like_escape(s):
    """把 LIKE 的元字符转义成字面量（配合 SQL 里的 `ESCAPE '\\'`）。

    只做转义、不负责拼 `%`：调用方自己决定通配符加在前面还是后面。
    反斜杠必须**第一个**换，否则 `%` 换成 `\\%` 之后那个新加的反斜杠
    会被后一步再换一次，变成 `\\\\%` —— 转义两次，语义就反了。
    """
    return str(s).replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def release_all_read_conns():
    """关掉**本线程**所有库的复用读连接，再跑一次 `gc.collect()`。

    给"我手上还没有 `TransferStateDB` 实例，但马上就要对库文件动手"的场合用 ——
    回归夹具的 `fresh_db()` 就是这种：它要挪库，而那一刻它是模块级函数，
    手里没有实例。

    与 `TransferStateDB.release_db_file()` 的分工：
      * `release_db_file()` —— 有实例，只处理**这一个** db_path；
      * 本函数          —— 模块级，处理本线程持有的**全部** db_path。
    两者都**只清调用线程**；别的线程（尤其已经退出、还没被 GC 的）够不到。
    所以本函数末尾那次 `gc.collect()` 不是可选的 —— 它才是真正让
    "已退出线程占着的文件"松手的那个动作，理由见文件头。
    """
    ent = getattr(_read_tls, "ent", None)
    if ent:
        for k in list(ent.keys()):
            d = ent.pop(k, None)
            if d is not None:
                try:
                    d[0].close()
                except Exception:
                    pass
    gc.collect()

# 【阶段2a-修改原因】任务级锁冲突的专用异常。
# 与普通 Exception 区分开，使 _start_transfer 能精确捕获"被别人占了"而不是"库坏了"。
class TaskLockedError(Exception):
    """已有任务在运行（心跳新鲜），本次抢占失败。"""

    def __init__(self, task_row=None):
        self.task_row = task_row or {}
        super().__init__("已有任务在运行")


# 【D-1 新增】v1 → v2 的一次性迁移：`files` 的主键 (filename) → (filename, src_root)。
#
# 为什么不是"让用户删库重来"：README 写明过重传代价可能是"几 TB 全库"，
# 真实场景是 DICOM 归档 —— 为省这一段迁移而让用户重跑几小时到几天，方向错了。
# 它是**一次性代码**，写完就固定，不会持续膨胀（判据是"是否引入新的维护负担"，
# 行数只是代理指标）。
#
# 三条硬要求：
#   ① 全在一个事务里 —— SQLite 的 DDL 是事务性的，中途任何一步失败整体回滚，
#      版本号保持 '1'，下次启动原样重试，**不会留下改了一半的库**。
#   ② 备份先行 —— 落 <db>.v1.bak。备份先于事务：前者防代码出错，后者防判断出错。
#   ③ 老行的 src_root 从 tasks 表回填 —— 判重靠 src_root，回填错就会漏传。
#      回填不上（任务行已被 maintenance 清掉）的填空串：空串永远等于不了任何真实
#      源根（_start_transfer 显式拒绝空源路径），那些行查不到自己 → 宁可重传，不会漏传。
def _migrate_v1_to_v2(conn, db_path):
    conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")   # 先把 WAL 折回主库，否则下面
    shutil.copy2(db_path, db_path + ".v1.bak")        # 的 copy2 只拷主文件会漏掉近期提交
    conn.execute("BEGIN IMMEDIATE")
    try:
        conn.execute("DROP TABLE IF EXISTS files_v2")   # 上一次的残留（理论上不会有）
        conn.execute("ALTER TABLE overwrites ADD COLUMN old_src_root TEXT")
        conn.execute("ALTER TABLE overwrites ADD COLUMN new_src_root TEXT")
        conn.execute(
            "CREATE TABLE files_v2 ("
            " filename TEXT NOT NULL, src_root TEXT NOT NULL,"
            " task_id INTEGER, src_path TEXT, src_size INTEGER, src_mtime INTEGER,"
            " mtime_precision TEXT, dst_path TEXT, dst_size INTEGER,"
            " status TEXT NOT NULL, src_deleted INTEGER NOT NULL DEFAULT 0,"
            " attempts INTEGER NOT NULL DEFAULT 0, last_error TEXT, updated_at TEXT,"
            " PRIMARY KEY (filename, src_root))")
        conn.execute(
            "INSERT INTO files_v2 SELECT filename,"
            " COALESCE((SELECT t.src_root FROM tasks t WHERE t.task_id = files.task_id), ''),"
            " task_id, src_path, src_size, src_mtime, mtime_precision,"
            " dst_path, dst_size, status, src_deleted, attempts, last_error, updated_at"
            " FROM files")
        conn.execute("DROP TABLE files")                # 索引随表一起没了，下面重建
        conn.execute("ALTER TABLE files_v2 RENAME TO files")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_files_task   ON files(task_id)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_files_status ON files(status)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_files_dst    ON files(dst_path)")
        # 【任务1-修改原因】这里原本写的是 `str(config.DB_SCHEMA_VERSION)`。当前版本升到 3
        # 之后，那句话会让一个 **v1 老库**升完 v2 结构却盖着 '3' 的章 —— 下次启动
        # ensure_schema 认为它已是当前版本，**v2→v3 那一步永远不会跑**，而它手里是一张
        # 主键还是 (filename, src_root) 的表。这正是"静默写歪"的标准形态。
        # 迁移函数盖**自己那一级的版本号**，是这条链能逐级往上的前提。
        conn.execute("UPDATE meta SET value='2' WHERE key='schema_version'")
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise


# 【任务1 新增】v2 → v3 的一次性迁移：`files` 的主键 (filename, src_root) → (src_path)。
#
# 范式与 `_migrate_v1_to_v2` **逐字相同**（WAL 折回 → 备份先行 → 单个 BEGIN IMMEDIATE
# 事务里 DDL+搬行+换名+重建索引+盖版本号 → 任何一步失败整体 ROLLBACK、版本号保持 '2'、
# 下次启动原样重试），三条硬要求的理由也照旧，不重复。这里只写**本刀独有**的两件事：
#
# ① 老行的 src_path 从哪来 —— 不是无条件拼接，是
#        COALESCE(NULLIF(src_path, ''), src_root || '\' || filename)
#    用户当时的要求是"老记录 src_path = 拼接(src_root, filename)"，**照做会毁数据**：
#    `filename` 存的是 `os.path.basename(src_path)`，是**裸文件名**；对嵌套文件
#    （D:\photos\2024\1.jpg，src_root=D:\photos）拼出来是 D:\photos\1.jpg ——
#    一个不存在的路径。而 v2 起 `src_path` 列本来就存着真实全路径，无条件覆盖等于把
#    库里唯一正确的信息擦掉，换来一个错的。
#    所以：**有真值就用真值**，只有 NULL / 空串（v1 遗留、早期写入路径没带上）才退回拼接。
#    拼接那半段对**顶层文件**恰好是对的（那正是"没有 src_path 的老行"的形态），
#    对嵌套文件只能错着 —— 那些行的真实路径本来就丢了，编不出来。错的 src_path 后果是
#    "查不到自己 → 重传"，方向安全（宁可重传，不可漏传）。
#
# ② 回填会撞车 —— 加宽主键本身不会撞（(a,b) 不同则路径必不同），**COALESCE 会**：
#    一行 src_path 为 NULL 的旧行回填出来的路径，可能与另一行**已有的**真实 src_path
#    相同。这不是理论组合：v1→v2 那次迁移把回填不上 src_root 的行留成了空串，
#    之后的任务再按真实路径写下同一份文件，两行就分属不同的 (filename, src_root) 而
#    src_path 相同。
#    撞车时按 `updated_at DESC, rowid DESC` 取**最新**的一行，其余丢弃 —— 与 UPSERT
#    "后写覆盖先写"是同一套语义。丢弃 = 少一条续传记录 = 重传，方向安全。
#    丢了几条先数出来，交给 ensure_schema 回流到启动日志（见那里的说明）。
def _migrate_v2_to_v3(conn, db_path):
    path_expr = "COALESCE(NULLIF({c}.src_path,''), {c}.src_root || '\\' || {c}.filename)"
    conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")   # 先把 WAL 折回主库，否则下面
    shutil.copy2(db_path, db_path + ".v2.bak")        # 的 copy2 只拷主文件会漏掉近期提交
    conn.execute("BEGIN IMMEDIATE")
    try:
        dropped = 0
        total, uniq = conn.execute(
            "SELECT COUNT(*), COUNT(DISTINCT " + path_expr.format(c="files") + ") "
            "FROM files").fetchone()
        dropped = int(total) - int(uniq)
        conn.execute("DROP TABLE IF EXISTS files_v3")   # 上一次的残留（理论上不会有）
        conn.execute(
            "CREATE TABLE files_v3 ("
            " filename TEXT NOT NULL, src_root TEXT NOT NULL,"
            " task_id INTEGER, src_path TEXT NOT NULL, src_size INTEGER, src_mtime INTEGER,"
            " mtime_precision TEXT, dst_path TEXT, dst_size INTEGER,"
            " status TEXT NOT NULL, src_deleted INTEGER NOT NULL DEFAULT 0,"
            " attempts INTEGER NOT NULL DEFAULT 0, last_error TEXT, updated_at TEXT,"
            " PRIMARY KEY (src_path))")
        # 列顺序与 v2 逐字一致（有探针按位置 INSERT），只是 src_path 那一位现在是
        # 算出来的、且 NOT NULL。
        # 【为什么用窗口函数而不是相关子查询】撞车时取"每组最新的一行"最直白的写法是
        # `WHERE rowid = (SELECT ... WHERE 路径相等 ORDER BY ... LIMIT 1)`，但那个子查询
        # 用不上索引（比较的是算出来的表达式），每行一次全表扫描 = O(n²)。真实库里
        # 候选项是 20 万量级（DB_MAINT_MIN_ROWS 就是按这个量级定的），那会跑到天亮。
        # ROW_NUMBER() 一次分区排序搞定，O(n log n)。需要 SQLite ≥ 3.25（2018-09），
        # 本机与打包环境都是 3.45；真撞上更老的库，这里抛错 → ensure_schema 抛错 →
        # 主程序走既有的"状态库不可用"降级路径（丢续传能力，但一条数据不动），
        # 比"静默按 (filename, src_root) 写下去"安全得多。
        conn.execute(
            "INSERT INTO files_v3 SELECT filename, src_root, task_id, p, src_size,"
            " src_mtime, mtime_precision, dst_path, dst_size, status, src_deleted,"
            " attempts, last_error, updated_at FROM ("
            " SELECT f.*, " + path_expr.format(c="f") + " AS p,"
            " ROW_NUMBER() OVER (PARTITION BY " + path_expr.format(c="f") +
            "   ORDER BY f.updated_at DESC, f.rowid DESC) AS rn FROM files f"
            ") WHERE rn = 1")
        conn.execute("DROP TABLE files")                # 索引随表一起没了，下面重建
        conn.execute("ALTER TABLE files_v3 RENAME TO files")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_files_task   ON files(task_id)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_files_status ON files(status)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_files_dst    ON files(dst_path)")
        conn.execute("UPDATE meta SET value='3' WHERE key='schema_version'")
        conn.execute("COMMIT")
        return dropped
    except Exception:
        conn.execute("ROLLBACK")
        raise


# 【阶段2a-修改原因】传输状态库。
# 线程模型（这是本类最重要的一条约定，破坏了就会踩 sqlite3 的跨线程限制）：
#   写连接 self._conn  —— 由 _db_writer_loop 线程独占，外部一律不准碰；
#   短连接 _short_conn() —— 其余方法各自开、各自关，任意线程可调。
# 只有心跳/文件记录这类"每文件一次"的写入才走写连接；
# claim_task / 各种查询是"每任务一次"，走短连接，代价可忽略。
class TransferStateDB:
    def __init__(self, db_path):
        self.db_path = db_path
        self._conn = None          # 写连接：仅 _db_writer_loop 线程使用

    # ---------- A. 连接与建表 ----------

    # 【阶段2a-修改原因】写连接只在 db-writer 线程内建立，杜绝跨线程共享 sqlite3 连接。
    # isolation_level=None 关掉 sqlite3 的隐式事务，事务边界完全由我们显式控制
    # （batch_commit 攒批、claim_task 的 BEGIN IMMEDIATE），行为可预测。
    def open(self):
        if self._conn is not None:
            return
        self._conn = sqlite3.connect(self.db_path, timeout=5, isolation_level=None)
        self._conn.execute("PRAGMA journal_mode=WAL")     # 持久属性，只需设一次
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.execute("PRAGMA busy_timeout=5000")    # 多进程必需
        self._conn.execute("PRAGMA foreign_keys=ON")

    # 【阶段2a-修改原因】短连接工厂。journal_mode 是持久属性不需重设，
    # 但 busy_timeout 是每连接属性，必须重设，否则并发抢占时会立刻抛 database is locked。
    def _short_conn(self):
        conn = sqlite3.connect(self.db_path, timeout=5, isolation_level=None)
        conn.execute("PRAGMA busy_timeout=5000")
        return conn

    # ---------- A2. 读连接：复用（新） / 短连接（回退） ----------
    #
    # 【连接复用-新增】这一节解决的是 M4 量出来的那 96.6%：
    # 原来每个文件都要 `connect()` + `close()`，而这两步与"这个文件传过没有"毫无关系。
    #
    # 三条不可让步的性质，逐条说清楚它是靠什么成立的：
    #
    # ① **绝不跨线程共享** —— 每条连接只存在调用它的那个线程的 `threading.local()` 里。
    #    这是 sqlite3 的硬要求，也是本模块第一条约定。
    # ② **绝不长期持有** —— 见 `_read_conn_alive` 的空闲上限；空闲到点即关。
    #    一条挂着的连接会占住 WAL 读标记，把库维护的 checkpoint 挡在外面。
    # ③ **库被换掉必须发现** —— 见 `_db_identity`。这是最阴的一条：SQLite 在 Windows 上
    #    以 FILE_SHARE_DELETE 打开文件，所以"库被整个挪走"**会成功**，
    #    而手里的连接还连着**原来那份**。不检测的症状是"接着读旧库"——
    #    静默，且表现为续传记录凭空少一批。（测试夹具 `fresh_db()` 干的正是这件事，
    #    见 `_checks\_nightly\31_中途 kill 的代价.md`。）

    def _db_identity(self):
        """`(st_dev, st_ino)` —— "此刻 db_path 上那个文件是谁"。

        用 inode 而不是 mtime/size：后两者**每次提交都会变**，拿它们做身份会把
        "库被正常写入"误判成"库被换掉了"，于是每次调用都重开一条连接 ——
        功能还是对的，但性能回到改造前，而且**不报任何错**（最难发现的那种退化）。
        """
        try:
            st = os.stat(self.db_path)
        except OSError:
            return None
        return (st.st_dev, st.st_ino)

    def _read_conn_alive(self, conn, sig, last_used):
        """手头这条复用连接还能不能用。**任一条件不满足就换一条**，不做修补。"""
        if self._db_identity() != sig:
            return False                      # 库被挪走/删除/重建了 —— 手里这条连着旧文件
        if config.DB_READ_CONN_MAX_IDLE_S > 0:
            if time.time() - last_used > config.DB_READ_CONN_MAX_IDLE_S:
                return False                  # 闲置太久，按"不长期持有"关掉重开
        try:
            conn.execute("SELECT 1").fetchone()
        except sqlite3.Error:
            return False                      # 连接本身坏了（文件被占用/损坏/被换格式）
        return True

    def _read_conn(self):
        """取**本线程**的读连接。⚠️ 调用方**绝不 close 它** —— 它归本线程所有。"""
        ent = getattr(_read_tls, "ent", None)
        if ent is not None:
            d = ent.get(self.db_path)
            if d is not None and self._read_conn_alive(d[0], d[1], d[2]):
                ent[self.db_path] = (d[0], d[1], time.time())   # 刷新"最后使用"
                return d[0]
            if d is not None:                 # 失效：关掉，下面重开
                try:
                    d[0].close()
                except Exception:
                    pass
                del ent[self.db_path]
        conn = self._short_conn()
        if ent is None:
            ent = {}
            _read_tls.ent = ent
        # 先连（可能顺带把库建出来）再取身份，否则全新机器上 sig 恒为 None，
        # "库刚被建出来"会被当成"库被换掉了"。
        ent[self.db_path] = (conn, self._db_identity(), time.time())
        return conn

    def close_read_conn(self):
        """关掉**本线程**的复用读连接（幂等）。

        给"我知道这个库马上就要被动刀子"的场合用 —— 例如测试夹具要挪走库文件之前。

        ⚠️【实测更正，2026-09-20】原文写的是"不调也不会错（身份变了会被自动发现）"。
        **那句话是错的**，探针当场打穿：

            复用连接还开着 ⇒ os.replace(库, 别处) 直接抛
            PermissionError: [WinError 32] 另一个程序正在使用此文件

        原因：Python 的 sqlite3 打开文件时**没有**带 FILE_SHARE_DELETE，
        所以"文件被句柄占着"在文件系统层面就是**硬拦** —— 不是"大概率失败"，
        是"必然失败"。**先调本方法不是优化，是前置条件**：
        任何"先把库挪走、再放一个新的进来"的代码（例如 `fresh_db()`），
        挪之前不调它就会在 `os.replace` 上抛异常。

        ⚠️ 它只清**本线程**的缓存（`_read_tls` 是线程局部的）。工作线程手里那条
        仍然占着文件 —— 那种情况下挪库照样 WinError 32。所以"挪库"和
        "工作线程还在跑"这两件事不该同时发生；真要做，得先让工作线程停下来。
        """
        ent = getattr(_read_tls, "ent", None)
        if not ent:
            return
        d = ent.pop(self.db_path, None)
        if d is not None:
            try:
                d[0].close()
            except Exception:
                pass

    def release_db_file(self):
        """把"**本进程**还占着这个库文件"这件事清干净。挪库/删库/换库之前调它。

        做两件事：
          ① 关掉**本线程**的复用读连接（= `close_read_conn()`）；
          ② 跑一次 `gc.collect()`。

        为什么 ② 是必需的（实测，2026-09-20）：
          一个工作线程退出时，它在 `_read_tls` 里那份小字典与里面的连接**成环**，
          不会立刻析构 —— 要等一次垃圾回收才真正 `close()`。在那之前库文件
          在 Windows 上**仍然挪不动**（`os.replace` 抛 WinError 32）。
          而 `close_read_conn()` 只清**调用线程**，够不到别人 —— 所以只能靠 gc。
          实测对照（`_checks\\_dbg_holder.py`）：
            库里什么都没有、没连接            → 挪得动
            主线程开过一条复用读连接            → **挪不动**
            主线程 close_read_conn() 之后      → 挪得动
            3 个工作线程查过、都已退出之后       → **挪不动**
            再 close_read_conn() 一次          → **还是挪不动**
            gc.collect() 之后                 → 挪得动

        ⚠️ 本方法解决不了的：**还活着的工作线程**手里那条连接。
          那种情况下挪库照样失败 —— 而且这是**对的**，不该在有人正用着的时候
          把库挪走。要挪就先让工作线程停下来。

        ⚠️ 生产代码里**没有任何地方**挪/删状态库（只有回归夹具 `fresh_db()` 会），
          所以本方法目前主要给夹具用。写在这里而不是夹具里，是因为它描述的是
          本模块的连接语义，不是某个夹具的私事。
        """
        self.close_read_conn()
        gc.collect()

    @contextlib.contextmanager
    def _read(self):
        """读连接的**唯一**取法。开关关掉时逐字退回短连接（自开自关）。

        为什么要做成上下文管理器而不是让调用方自己挑：**选哪条路是全局策略，
        不该由每个调用点各自决定** —— 那样将来新增一个读方法就多一处可能忘记的地方，
        而这个开关的全部意义就是"能一起退回去"。
        """
        if not config.DB_REUSE_READ_CONN:
            conn = self._short_conn()
            try:
                yield conn
            finally:
                conn.close()
            return
        yield self._read_conn()

    # 【阶段2a-修改原因】建表 + 版本校验。版本不符直接抛错退出，
    # 而不是带着未知结构继续跑——结构不符会让所有记录静默写错地方。
    # 【D-1 修改原因】多了一档"v1 → v2 就地迁移"。原先是"版本不符一律抛错，
    # 让用户删库"，那对 v1 用户等于**丢掉全部续传记录**（见 _migrate_v1_to_v2 的注释）。
    # 顺序很重要：迁移**必须**排在抛错之前、排在 row is None（全新库）之后。
    # 【任务1 修改原因】分派从"一次 if/elif"改成**逐级向上的循环**：老库可能是 v1 也可能是
    # v2，两级都要能升到 v3。写成 if/elif 时，"v1 库升到 v3"就得在分支里手动链式调用，
    # 多一级就多一处要记得改的地方 —— 而**漏改的后果是静默的**（版本号盖对了、结构没跟上，
    # 见 _migrate_v1_to_v2 结尾那段）。循环里每级的迁移函数负责盖**自己那一级**的版本号，
    # 于是"再加一级"只需要往 MIGRATIONS 里加一条。
    # 返回值 = 被丢弃的重复行条数（见 _migrate_v2_to_v3 决策③），调用方记日志用。
    MIGRATIONS = {"1": _migrate_v1_to_v2, "2": _migrate_v2_to_v3}

    def ensure_schema(self):
        conn = self._short_conn()
        try:
            conn.executescript(config._SCHEMA_DDL)
            row = conn.execute(
                "SELECT value FROM meta WHERE key='schema_version'").fetchone()
            if row is None:
                # 【并发首建】两个无头进程可能同时打开一个全新库：两边都读到
                # `row is None`，后插入的那个撞主键 —— 原先这条会以
                # `IntegrityError` 冒到 `--run-task` 外面，退出码变成"崩溃"，
                # 而实际上库已经由另一个进程建好了。这里只写"初始版本号"，
                # 谁先写都一样 ⇒ `OR IGNORE` 后**重读**，再按真实值决定是否迁移。
                conn.execute(
                    "INSERT OR IGNORE INTO meta (key, value) VALUES ('schema_version', ?)",
                    (str(config.DB_SCHEMA_VERSION),))
                row = conn.execute(
                    "SELECT value FROM meta WHERE key='schema_version'").fetchone()
                if row is None or row[0] == str(config.DB_SCHEMA_VERSION):
                    return 0
            dropped = 0
            # 上界就是版本总数：真有环（某个迁移函数没盖版本号）也会停在这里抛错，
            # 而不是转成死循环把界面冻住。
            for _ in range(len(self.MIGRATIONS) + 1):
                if row[0] == str(config.DB_SCHEMA_VERSION):
                    return dropped
                fn = self.MIGRATIONS.get(row[0])
                if fn is None:
                    raise RuntimeError(
                        f"状态库 schema 版本不匹配：期望 {config.DB_SCHEMA_VERSION}，"
                        f"实际 {row[0]}。请删除 {self.db_path} 后重试"
                        f"（删除只影响续传记录，不影响已传输的文件）")
                dropped += fn(conn, self.db_path) or 0   # 旧库：就地升一级，保住续传记录
                row = conn.execute(
                    "SELECT value FROM meta WHERE key='schema_version'").fetchone()
                if row is None:                          # 理论上不可能；不兜就是 None 下标崩
                    raise RuntimeError("状态库迁移后 schema_version 丢失")
            raise RuntimeError("状态库迁移未收敛（迁移函数没有盖版本号）")
        finally:
            conn.close()

    # ---------- A3. meta 键值（阶段四·P0-1：日期分层增量的同步点）----------
    #
    # 【P0-1 新增】`meta` 表**早就存在**（`schema_version` 就住在里面），
    # 所以这里加的是**方法**、不是表 —— 「不改状态库 schema」这条硬约束原封不动。
    #
    # 为什么同步点要落库而不是记在内存里：它跨进程、跨重启都得住下。
    # 每次开程序都从零扫一遍的话，"日期分层剪枝"省下的正是这一遍。
    #
    # 两条实现约束，都不是风格问题：
    # ① **写走短连接**（与 `purge`/`maintenance` 同一条理由，见 §D2 注释⑤）：
    #    `self._conn` 归 db-writer 线程独占，从界面线程碰它就是跨线程用 sqlite。
    # ② **读走 `_read()`**（与所有其他读方法同一条路）：连接复用开不开由全局策略定，
    #    不该在这里自己挑。

    # 这三个键归 `ensure_schema` 所有，**外部一律不许改**：
    # 把 schema_version 写歪的下场不是"读不到同步点"，而是**下次启动直接抛错**，
    # 用户看到的是"版本不匹配，请删库重试" —— 为一个同步点赔上整个续传记录。
    # 拦在这里是零成本的（同步点的键一律以 syncpoint| 开头，本来就撞不上）。
    META_RESERVED = ("schema_version",)

    def get_meta(self, key, default=None):
        """读一个 meta 值。**库/表不存在返回 default**，不抛 —— 首次运行时
        `syncpoint|...` 本来就不存在，那不是错误，是"还没同步过"。"""
        if key in self.META_RESERVED:
            return default                    # 保留键只由 ensure_schema 读，不外借
        try:
            with self._read() as conn:
                row = conn.execute(
                    "SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        except sqlite3.Error:
            return default
        return row[0] if row is not None else default

    def set_meta(self, key, value):
        """写一个 meta 值（有则覆盖）。返回 True/False —— **不抛**。

        为什么不上抛：同步点推进发生在一整趟传输正常跑完的收尾处，
        为一次记录失败把用户已经完成的传输报成"失败"，是把账算错了地方。
        调用方按返回值决定要不要出声。"""
        if key in self.META_RESERVED or not key:
            return False
        if value is None:
            return False                      # 表上是 value TEXT NOT NULL，先拦住再报 False
        try:
            conn = self._short_conn()
            try:
                conn.execute(
                    "INSERT INTO meta (key, value) VALUES (?,?) "
                    "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                    (key, str(value)))
            finally:
                conn.close()
        except sqlite3.Error:
            return False
        return True

    def del_meta(self, key):
        """删一个 meta 值（「重置同步点」按钮用）。返回 True/False —— 理由同上。"""
        if key in self.META_RESERVED:
            return False
        try:
            conn = self._short_conn()
            try:
                conn.execute("DELETE FROM meta WHERE key=?", (key,))
            finally:
                conn.close()
        except sqlite3.Error:
            return False
        return True

    def del_meta_prefix(self, prefix):
        """按前缀删（例如一次重置清掉某条规则下的所有模板变体）。

        前缀走**参数绑定**（`LIKE ?` + 拼在值里），不拼进 SQL 文本 ——
        与 `purge` 的白名单同一条理由：现在没有注入面不是理由，白名单/参数化是零成本的。
        返回被删的行数；出错返回 -1（与"删了 0 行"区分开，调用方能如实记日志）。"""
        if not prefix:
            return -1                         # 空前缀 = 删全表，拒绝
        try:
            conn = self._short_conn()
            try:
                # ESCAPE 是为了让前缀里的 % 和 _ 当字面量。同步点的键里
                # 有 `|` 和路径，不该出现通配符，但"不该出现"不是"不会出现"。
                cur = conn.execute(
                    "DELETE FROM meta WHERE key LIKE ? ESCAPE '\\'",
                    (_like_escape(prefix) + "%",))
                return cur.rowcount
            finally:
                conn.close()
        except sqlite3.Error:
            return -1

    # ---------- B. 任务生命周期 ----------

    # 【阶段2a-修改原因】任务级锁的核心（Q6 修改版）。
    # 用 BEGIN IMMEDIATE 取写锁，把"查活跃任务 + 插新任务"变成跨进程原子操作，
    # 否则两个窗口可能同时查到"无活跃任务"然后各插一行（TOCTOU）。
    # 同一个事务里顺带把心跳陈旧的僵尸 running 行标为 aborted，
    # 这样 find_unfinished_task 不会一直返回永远无法完成的僵尸任务。
    def claim_task(self, src_type, src_root, dst_type, dst_root, mode,
                   extensions, flatten, stale_after_s=config.DB_STALE_AFTER_S):
        now = int(time.time())
        conn = self._short_conn()
        try:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT task_id, src_root, dst_root, created_at, done_count FROM tasks "
                "WHERE status='running' AND heartbeat_at > ? "
                "ORDER BY task_id DESC LIMIT 1",
                (now - stale_after_s,)).fetchone()
            if row is not None:
                conn.execute("ROLLBACK")
                raise TaskLockedError({
                    "task_id": row[0], "src_root": row[1], "dst_root": row[2],
                    "created_at": row[3], "done_count": row[4],
                })
            conn.execute(
                "UPDATE tasks SET status='aborted', finished_at=?, end_reason='stale' "
                "WHERE status='running' AND heartbeat_at <= ?",
                (config._now_text(), now - stale_after_s))
            cur = conn.execute(
                "INSERT INTO tasks (src_type, src_root, dst_type, dst_root, mode, "
                "extensions, flatten, status, heartbeat_at, created_at) "
                "VALUES (?,?,?,?,?,?,?,'running',?,?)",
                (src_type, src_root, dst_type, dst_root, mode,
                 ",".join(sorted(extensions)) if extensions else None,
                 int(bool(flatten)), now, config._now_text()))
            task_id = cur.lastrowid
            conn.execute("COMMIT")
            return task_id
        except TaskLockedError:
            raise
        except Exception:
            try:
                conn.execute("ROLLBACK")
            except Exception:
                pass
            raise
        finally:
            conn.close()

    # 【阶段2a-修改原因】心跳刷新。空转时也必须调（见 _db_writer_loop 的 queue.Empty 分支），
    # 否则 FTP 深度扫描期间没有文件完成，30 秒后本任务会被其他窗口判为陈旧并抢占。
    def heartbeat(self, task_id):
        self._execute_write("UPDATE tasks SET heartbeat_at=? WHERE task_id=?",
                            (int(time.time()), task_id))

    # 【阶段2a-修改原因】任务收尾。skipped_count 在此一并落库（Q5 补充），
    # 使 UI/排查时能区分"本次真的复制了 N 个"和"本次跳过了 N 个"。
    def finish_task(self, task_id, status, total_seen, done_count,
                    skipped_count, failed_count, end_reason):
        self._execute_write(
            "UPDATE tasks SET status=?, finished_at=?, total_seen=?, done_count=?, "
            "skipped_count=?, failed_count=?, end_reason=? WHERE task_id=?",
            (status, config._now_text(), total_seen, done_count,
             skipped_count, failed_count, end_reason, task_id))

    # 【阶段2d-新增】把一条任务显式收尾为 aborted。**走短连接**是刻意的：
    # 调用它的两个场合都不存在可用的写连接——
    #   ① 启动时的恢复弹窗：本次进程还没开始任何任务，db-writer 没起，self._conn 为 None；
    #   ② 关闭窗口：db-writer 可能正持着写锁，且进程马上要退出。
    # finish_task 走的是写连接 self._conn（只允许 db-writer 线程碰），在这里调不了。
    # 两者语义一致（都是"结束这条任务"），区别只在连接归属。
    # 抢不到锁也不抛：那条行的陈旧心跳会在 30 秒后被下一个任务的 claim_task 清掉。
    def abort_task(self, task_id, reason):
        conn = self._short_conn()
        try:
            conn.execute(
                "UPDATE tasks SET status='aborted', finished_at=?, end_reason=? "
                "WHERE task_id=? AND status='running'",
                (config._now_text(), reason, task_id))
        finally:
            conn.close()

    # 【阶段2a-修改原因】查"心跳新鲜"的活跃任务。供提示信息展示，
    # 不承担锁职责（锁职责在 claim_task 的原子事务里）。
    def find_active_task(self, stale_after_s=config.DB_STALE_AFTER_S):
        now = int(time.time())
        with self._read() as conn:
            row = conn.execute(
                "SELECT task_id, src_root, dst_root, created_at, done_count FROM tasks "
                "WHERE status='running' AND heartbeat_at > ? "
                "ORDER BY task_id DESC LIMIT 1",
                (now - stale_after_s,)).fetchone()
            if row is None:
                return None
            return {"task_id": row[0], "src_root": row[1], "dst_root": row[2],
                    "created_at": row[3], "done_count": row[4]}

    # 【阶段2a-修改原因】查未完成任务，**不论心跳新旧**——启动时的恢复弹窗要的正是
    # "上次异常退出留下的 running 行"。与 find_active_task 的区别就在这里，不能合并。
    def find_unfinished_task(self):
        with self._read() as conn:
            row = conn.execute(
                # 【阶段2d-修改原因】多带一列 heartbeat_at：它是恢复弹窗的**判定依据**（§7）。
                # 只有心跳早于本次启动时刻的 running 行才是"上次异常退出留下的"，
                # 心跳新鲜的属于另一个正在跑的窗口。不外传这一列，弹窗就只能靠
                # "存在 running 行"来判断，会把别人正在传的任务提示成可放弃——那是数据事故。
                "SELECT task_id, src_type, src_root, dst_type, dst_root, mode, "
                "extensions, flatten, created_at, done_count, skipped_count, failed_count, "
                "heartbeat_at "
                "FROM tasks WHERE status='running' ORDER BY task_id DESC LIMIT 1").fetchone()
            if row is None:
                return None
            return {"task_id": row[0], "src_type": row[1], "src_root": row[2],
                    "dst_type": row[3], "dst_root": row[4], "mode": row[5],
                    "extensions": row[6], "flatten": bool(row[7]),
                    "created_at": row[8], "done_count": row[9],
                    "skipped_count": row[10], "failed_count": row[11],
                    "heartbeat_at": row[12]}

    # ---------- C. 单文件写入（仅 _db_writer_loop 线程调用） ----------

    # 【阶段2a-修改原因】成功记录。ON CONFLICT 只更新内容字段，不动 src_deleted
    # ——否则重传一个已删源的文件会把"源已删"标记抹掉，破坏 move 模式的自愈判断。
    # 【D-1 修改原因】多一个 src_root 参数，排在 filename 之后 —— 两者合起来才是
    # 这一行的**身份**，放在一起读才不容易传错。冲突目标随之改成 (filename, src_root)：
    # 同一个裸文件名在**每个源**各占一行，互不覆盖，各自独立判重。
    # ⚠️ 调用方必须传**当前任务的源根**（`file_transfer._db_task_src_root`），
    # 不能传 task_id —— 两者都是"数字或路径"，传错了 SQLite 不会报错，只会静默写歪。
    # 【任务1 修改原因】身份变成 `src_path`（全路径），冲突目标随之改 `ON CONFLICT(src_path)`；
    # `filename` 形参**删掉**，由本方法从 src_path 推（`os.path.basename`）——
    # 它现在纯粹是个冗余列（报告/排序用），让调用方多传一个"必须与 src_path 自洽"的
    # 参数，只是白送一个能传歪的地方。src_root 仍要传：它是 find_dst_owner 的排除依据。
    def mark_done(self, src_path, src_root, task_id, src_size, src_mtime,
                  precision, dst_path, dst_size):
        filename = os.path.basename(src_path)
        self._execute_write(
            "INSERT INTO files (filename, src_root, task_id, src_path, src_size, "
            "src_mtime, mtime_precision, dst_path, dst_size, status, src_deleted, "
            "attempts, last_error, updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,'done',0,0,NULL,?) "
            "ON CONFLICT(src_path) DO UPDATE SET "
            "filename=excluded.filename, src_root=excluded.src_root, "
            "task_id=excluded.task_id, "
            "src_size=excluded.src_size, src_mtime=excluded.src_mtime, "
            "mtime_precision=excluded.mtime_precision, dst_path=excluded.dst_path, "
            "dst_size=excluded.dst_size, status='done', attempts=0, last_error=NULL, "
            "updated_at=excluded.updated_at",
            (filename, src_root, task_id, src_path, src_size, src_mtime, precision,
             dst_path, dst_size, config._now_text()))

    # 【阶段2a-修改原因】失败记录。与 mark_done 同一套 UPSERT，
    # 保留上次的 dst_size（失败时目标通常不存在，不动它比写 NULL 信息量更大）。
    # attempts 采用**累加**而非覆盖：覆盖会让这个值永远是最后一次的 3，
    # 对"这个文件是否反复失败"没有任何诊断价值。
    # 【D-1 修改原因】同 mark_done：加 src_root，冲突目标改 (filename, src_root)。
    # 【任务1 修改原因】同 mark_done：身份改 src_path，filename 形参删掉由内部推。
    def mark_failed(self, src_path, src_root, task_id, src_size, src_mtime,
                    precision, dst_path, attempts, err):
        filename = os.path.basename(src_path)
        self._execute_write(
            "INSERT INTO files (filename, src_root, task_id, src_path, src_size, "
            "src_mtime, mtime_precision, dst_path, dst_size, status, src_deleted, "
            "attempts, last_error, updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,NULL,'failed',0,?,?,?) "
            "ON CONFLICT(src_path) DO UPDATE SET "
            "filename=excluded.filename, src_root=excluded.src_root, "
            "task_id=excluded.task_id, "
            "src_size=excluded.src_size, src_mtime=excluded.src_mtime, "
            "mtime_precision=excluded.mtime_precision, dst_path=excluded.dst_path, "
            "status='failed', attempts=files.attempts + excluded.attempts, "
            "last_error=excluded.last_error, updated_at=excluded.updated_at",
            (filename, src_root, task_id, src_path, src_size, src_mtime, precision,
             dst_path, attempts, err, config._now_text()))

    # 【阶段2a-修改原因】move 模式专用：源已删除的标记，供下次运行自愈
    # （源残留 → 重做；源已删 → 不重复处理）。
    # 【D-1 修改原因】加 src_root 做限定。**这一处不改就是数据事故**：
    # 同名文件在每个源各有一行，只按 filename 更新会把**别的源**的行一起标成
    # "源已删" —— 那个源下次运行就会跳过它根本没有删除过的文件。
    # 【任务1 修改原因】限定从 (filename, src_root) 换成 src_path。这是"限定"的终点形态：
    # src_path 已经把 (filename, src_root) 那点分辨力全包含了，而且更细 ——
    # 加宽之后**不存在**"我想更新的那一行"与"被误伤的别的行"共享 src_path 的情形。
    def mark_src_deleted(self, src_path):
        self._execute_write(
            "UPDATE files SET src_deleted=1, updated_at=? WHERE src_path=?",
            (config._now_text(), src_path))

    # 【阶段2a-修改原因】覆盖审计（Q1 选项 C）。写入条件的判定在调用方
    # （2b 的 _atomic_write_local），本方法只负责写。
    # 【D-1 修改原因】加两列 old_src_root / new_src_root。多源场景下"IM_0001.dcm 被覆盖了"
    # 这句话**不完整** —— 用户要知道的是"谁覆盖了谁"。没有这两列，审计表只能看到
    # 孤立的覆盖事件，追溯不了。旧调用方不传就是 NULL（本方法有默认值，不强制）。
    def record_overwrite(self, filename, dst_path, old_size, old_mtime,
                         new_size, new_mtime, task_id,
                         old_src_root=None, new_src_root=None):
        self._execute_write(
            "INSERT INTO overwrites (filename, dst_path, old_size, old_mtime, "
            "new_size, new_mtime, old_src_root, new_src_root, task_id, "
            "overwritten_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (filename, dst_path, old_size, old_mtime, new_size, new_mtime,
             old_src_root, new_src_root, task_id, config._now_text()))

    # ---------- D. 查询（任意线程；内部各开短连接） ----------

    # 【阶段2a-修改原因】读单文件状态。2a 建好但**不调用**——它是 2b 跳过判定的输入，
    # 现在先把接口和字段定死，2b 只改调用方。
    # 【D-1 修改原因】加 src_root 限定 —— 判重从"这个文件名做过没有"变成
    # "**这个源**的这个文件名做过没有"。少这个限定，第二、第三个源会查到
    # 第一个源写下的 done 行，然后报「跳过 1 个」看起来像成功，**实际是静默漏传**。
    # 【任务1 修改原因】判重口径改 `src_path` 全路径 —— 判重从
    # "**这个源**的这个文件名做过没有"变成"**这个文件**做过没有"。
    # 同一源的 D:\photos\2024\1.jpg 与 D:\photos\2025\1.jpg 这才分得开
    # （改前两者同 key，后者查到前者那行 → 命中跳过 → 静默漏传）。
    # ⚠️ 调用方传的必须是**与 mark_done 侧逐字相同**的那个字符串：两侧一旦有一个字节的
    # 差异（大小写、结尾分隔符、规范化与否），判重就永远查不到自己 —— 症状是
    # **静默退化成全部重传**（不会报错、不会漏传，但续传能力凭空消失）。
    # 生产侧的保证是"两侧传的是同一个局部变量"，见 file_transfer._should_skip 的说明。
    # 【连接复用-修改原因】**这就是 M4 里那 96.6% 的落点** —— 它是全项目唯一的
    # "每文件一次"的读方法（`_should_skip` 的第 ① 步）。SQL 一个字没改，
    # 改的只是"从哪条连接上发这条 SQL"。
    #
    # ⚠️ 连接**不再由本方法关闭** —— 它归线程所有，由 `_read()` 那边管生死。
    # 这是本次改动里唯一一处"看着像忘了关连接"的地方，所以单独写明。
    def get_file_state(self, src_path):
        with self._read() as conn:
            row = conn.execute(
                "SELECT " + _FILE_STATE_COLS_SQL + " FROM files WHERE src_path=?",
                (src_path,)).fetchone()
            if row is None:
                return None
            return dict(zip(_FILE_STATE_COLS, row, strict=True))

    # 【P1 新增】把**该 src_root 的全部判重记录**一次读进内存。
    #
    # 它替换的是"逐文件一次 `get_file_state`"。实测（`_checks\_probe_p1_prefetch.py`，
    # 20 万行合成库，本机热缓存）：
    #     a 现状 每文件查库   87~116 µs/文件  → 20 万文件 17.9~23.7 s
    #     c 预取 建 dict+查内存 0.63~0.77 s + 0.18~0.20 s  → 整轮 0.83~0.97 s
    #     ⇒ 墙钟改善 94.6%~96.5%；进程峰值（界面已起）186.5 MB < 200 MB
    #
    # 【为什么是 tuple 不是 dict】`_should_skip` 只需要那六个字段的**值**。
    #   20 万行的 dict-of-dict 每行多约 200 B（键表 + 哈希），实测就是"过不过门槛"的差别。
    #   代价：调用方取字段要用 `config.SKIP_STATE_IDX`（下标由那份常量决定，
    #   不手写 `row[3]` 这种魔数）—— 取字段那件事收在 `file_transfer._state_getter` 里。
    #
    # 【列名由常量派生，不手写】SELECT 的列序与 `config.SKIP_STATE_FIELDS` **逐字段一致**，
    #   所以 `row[1:]` 的下标与 `SKIP_STATE_IDX` 天然对齐。两处写两份列名的话，
    #   错位的症状是"拿 dst_path 去比 src_size" ⇒ 全部不跳过 ⇒ 每次全量重传，
    #   而库、SQL、返回值看着全都正常 —— 这类静默错位是本项目最贵的一种 bug。
    #
    # 【删掉的行不会有残留】没有任何缓存跨调用存活：这是**一次性的读**，
    #   调用方持有它到本趟源跑完（`file_transfer._launch_source` → `_dispatch_task_end`）。
    #   所以"库里的行被删了、dict 里还有"这种陈旧只在**一趟之内**可能发生，
    #   而那一趟里它本来就该按"上次的记录"判 —— 与逐文件查库的语义一致
    #   （逐文件查也只是"查的那一刻"的库）。
    #
    # 【异常一律往上抛】不在这一层吞：调用方要知道"预取失败了"才能退回逐文件取数。
    #   这里吞掉的话，调用方会以为手里有一个可用的 dict —— 而那个 dict 是 None，
    #   `_should_skip` 会在 `st is None` 那一格判"不跳过" ⇒ 变成**全量重传**，
    #   界面全绿、只是慢。方向安全但事与愿违，所以让它抛。
    def load_skip_states(self, src_root):
        cols = ", ".join(config.SKIP_STATE_FIELDS)
        sql = "SELECT src_path, " + cols + " FROM files WHERE src_root=?"
        d = {}
        with self._read() as conn:
            for row in conn.execute(sql, (src_root,)):
                d[row[0]] = row[1:]
        return d

    # 【D-1 新增】按目标路径反查"这一份是谁写下的" —— 跨源同名覆盖审计用。
    #
    # 场景：源 A 和源 B 各有一个 IM_0001.dcm，都传到同一个目标盘。B 覆盖了 A 的产物时，
    # 光有 overwrites 里那条"IM_0001.dcm 被覆盖"还不够 —— 得说清**被覆盖的那份来自哪个源**。
    # _should_skip 只看得到"目标已存在且大小/mtime 全同"，它不知道那份是谁写的；
    # 本方法就是补这一问。
    #
    # exclude_src_root：把**自己**排除掉。同一个源重传自己的产物不算跨源覆盖，
    # 那是正常的重传/覆盖路径，不该报成"源 B 覆盖了源 A"。
    # status='done' 限定：只认**写成功过**的行；失败行/半截行的 dst_path 指向的东西
    # 不能算"某个源的产物"。idx_files_dst 就是为这个方法建的。
    # 查不到返回 None（库里没有这个目标的记录 —— 目标盘上的文件可能是用户自己放的）。
    # 【连接复用-修改原因】与 get_file_state 同一条理由：它在**发生写入时**每文件一次
    # （`_audit_overwrite` 里），也是"每文件一次"。SQL 一个字没改。
    def find_dst_owner(self, dst_path, exclude_src_root):
        with self._read() as conn:
            row = conn.execute(
                "SELECT filename, src_root FROM files "
                "WHERE dst_path=? AND src_root<>? AND status='done' "
                "ORDER BY updated_at DESC LIMIT 1",
                (dst_path, exclude_src_root)).fetchone()
            if row is None:
                return None
            return {"filename": row[0], "src_root": row[1]}

    # 【阶段2a-修改原因】失败明细改由状态库提供（补充 A），返回的元组形状
    # 与旧的 failed 列表完全一致，使 _save_failed_list 一行不用改。
    # 【连接复用-修改原因】纯读，与上面同一条理由（每任务一次，不是热路径，
    # 但**读连接只有一种取法**这件事比"省这一次开销"重要 —— 见 `_read` 的说明）。
    def iter_failed(self, task_id):
        if task_id is None:
            return []
        with self._read() as conn:
            rows = conn.execute(
                "SELECT src_path, dst_path, COALESCE(last_error, '') FROM files "
                "WHERE status='failed' AND task_id=? ORDER BY filename",
                (task_id,)).fetchall()
            return [(r[0], r[1], r[2]) for r in rows]

    # 【阶段2a-修改原因】失败计数。供 _retry_failed 判空等"非 db-writer 线程"的场景。
    def count_failed(self, task_id):
        if task_id is None:
            return 0
        with self._read() as conn:
            return conn.execute(
                "SELECT COUNT(*) FROM files WHERE status='failed' AND task_id=?",
                (task_id,)).fetchone()[0]

    # 【阶段2a-修改原因】仅供 _db_writer_loop 调用：必须走它自己的写连接。
    # 若这里改用短连接，会读到**本批次尚未提交**前的旧快照，计数与 payload 对不上。
    def count_failed_writer(self, task_id):
        if self._conn is None:
            return 0
        return self._conn.execute(
            "SELECT COUNT(*) FROM files WHERE status='failed' AND task_id=?",
            (task_id,)).fetchone()[0]

    # ---------- F. 长期维护（任意线程；自开自关短连接） ----------

    # 【阶段5-4-新增】状态库的长期维护：清理"已完成且久远"的行 + VACUUM 回收空间。
    #
    # 为什么放在这里而不是 db-writer 线程：它只在启动时跑一次，跑完就结束，
    # 而 db-writer 是**每个任务**一条、生命周期跟着任务走。挂到写连接上会变成
    # "为了维护去借一条不属于任何任务的写连接"，还得处理任务结束时的收尾。
    # 用短连接自开自关最干净：不与任何线程共享 sqlite3 连接，也就绕开了跨线程限制。
    #
    # 【为什么分成 maintenance / _maintenance_body 两层】报告要"无论走哪条路都被记一笔"。
    # 单层写法里那几条提前返回（没开开关、没库、没到阈值）很容易把尾部的记日志那步跳过，
    # 而"跳过的理由"恰恰是最需要在日志里留痕的（否则用户看到库没瘦下来会以为是坏了）。
    # 拆成两层之后，记日志只在一个出口上，提前返回再也绕不开它。
    #
    # 返回一个报告 dict，键固定为：
    #   enabled rows threshold pruned(列表) kept(列表) vacuumed vacuum_attempts
    #   vacuum_error freelist_before freelist_after skipped_reason error elapsed_s
    # pruned/kept 给的是**文件名清单**而不是条数：这两类正是事后要追查的对象，
    # 只留个数字等于把线索又扔了。
    def maintenance(self, min_rows=None, prune_after_days=None, log=None):
        if min_rows is None:
            min_rows = config.DB_MAINT_MIN_ROWS
        if prune_after_days is None:
            prune_after_days = config.DB_PRUNE_AFTER_DAYS
        rep = {
            "enabled": bool(config.DB_MAINT_ENABLED),
            "rows": 0, "threshold": int(min_rows),
            "pruned": [], "kept": [],
            "vacuumed": False, "vacuum_attempts": 0, "vacuum_error": None,
            "freelist_before": None, "freelist_after": None,
            "skipped_reason": None, "error": None, "elapsed_s": 0.0,
        }
        t0 = time.time()
        try:
            self._maintenance_body(rep, min_rows, prune_after_days)
        except Exception as e:
            # 维护失败绝不能让程序起不来：这是一次"顺手把库收拾一下"，
            # 收拾不成最多是库继续大着，而已经完成的传输、还能续传的记录一条都没丢。
            rep["error"] = "%s: %s" % (type(e).__name__, e)
        rep["elapsed_s"] = round(time.time() - t0, 1)
        line = config._db_maint_summary(rep)
        if line and log is not None:
            try:
                log(line)
            except Exception:
                pass        # 记日志本身失败不该反过来影响维护结果
        return rep

    def _maintenance_body(self, rep, min_rows, prune_after_days):
        if not config.DB_MAINT_ENABLED:
            rep["skipped_reason"] = "disabled"
            return
        if not os.path.exists(self.db_path):
            # 首次启动、库还没建出来。没有可维护的对象，也不该顺手建一个出来
            # ——建库是 ensure_schema 的职责，两边都建会让"什么时候有库"变得含糊。
            rep["skipped_reason"] = "no_db"
            return

        conn = self._short_conn()
        try:
            rep["rows"] = int(conn.execute("SELECT COUNT(*) FROM files").fetchone()[0])
            if rep["rows"] <= min_rows:
                # 没到阈值就不 VACUUM：它要整库重写，行数不到时付出的代价
                # 与收回来的空间不成比例（见上面的常量说明）。
                rep["skipped_reason"] = "below_threshold"
                return

            # ---- 1. 选候选：done 且久远 ----
            # 年龄条件放在 SQL 里用 sqlite 自己的日期函数算，不在 Python 里做日期加减：
            # updated_at 存的是 _now_text() 的本地时间文本，让 sqlite 用它自己那套
            # （'now','localtime'）来比，格式天然对齐，也就不会出现"全选/全不选"
            # 这种最难发现的错（_probe_maint_sqlite 的 P2 专门钉了这一条）。
            # status='done' 把 failed / pending 全挡在外面：那些行是**待办**，删掉等于把
            # "还有文件没传成功"这件事抹了。
            # 【D-1 修改原因】候选里**必须**带上 src_root。主键变成 (filename, src_root) 之后
            # 同一个 filename 在不同源各有一行，只按 filename 删会把**别的源**的行一起删掉
            # —— 那是删掉别人的续传记录，用户下次跑那个源会整源重传。
            # 带上它，下面的 DELETE 才能精确落到"这一条候选"上。
            # 【任务1 修改原因】同上，定位键换成 src_path（更细，理由见 mark_src_deleted）。
            # filename 仍然取出来，但**只用于报告** —— rep["pruned"]/["kept"] 给的是一直
            # 以来那份"文件名清单"（形状不能悄悄改，见下面第 3 步的说明）。
            cands = conn.execute(
                "SELECT src_path, filename, dst_path, dst_size FROM files "
                "WHERE status='done' AND updated_at IS NOT NULL "
                "AND updated_at < datetime('now','localtime',?)",
                ("-%d days" % int(prune_after_days),)).fetchall()

            # ---- 2. 逐条确认目标还在，确认不了的进 kept ----
            doomed = []
            for sp, fn, dst_path, dst_size in cands:
                if config._backup_still_there(dst_path, dst_size):
                    doomed.append((sp, fn))
                else:
                    rep["kept"].append(fn)

            # ---- 3. 分块删 ----
            for i in range(0, len(doomed), config.DB_MAINT_DELETE_CHUNK):
                chunk = doomed[i:i + config.DB_MAINT_DELETE_CHUNK]
                conn.execute("BEGIN IMMEDIATE")
                try:
                    # chunk 里是 (src_path, filename) 二元组（filename 只为报告留着），
                    # 而这条 SQL 只有一个占位符 —— 直接把 chunk 递进去会报
                    # "Incorrect number of bindings"，所以要取出第一列。
                    conn.executemany(
                        "DELETE FROM files WHERE src_path=?", [(sp,) for sp, _ in chunk])
                    conn.execute("COMMIT")
                except Exception:
                    # 显式回滚这一块，让后面可能还要做的 VACUUM 拿到一个干净的事务状态；
                    # 回滚本身再失败就随它去（库已经处于异常，把原异常抛出去更有价值）。
                    try:
                        conn.execute("ROLLBACK")
                    except Exception:
                        pass
                    raise
                # 【D-1】报告里仍然只记**文件名**。chunk 现在是 (src_path, filename) 二元组，
                # 直接 extend 会把元组塞进 pruned，把这份报告的形状悄悄改掉
                # （_db_maint_summary 只取 len()，所以它不会报错 —— 正因为不会报错才危险）。
                rep["pruned"].extend(fn for _, fn in chunk)

            # ---- 4. VACUUM（有界重试）----
            rep["freelist_before"] = int(
                conn.execute("PRAGMA freelist_count").fetchone()[0])
            for attempt in range(1, max(1, config.DB_MAINT_VACUUM_ATTEMPTS) + 1):
                rep["vacuum_attempts"] = attempt
                try:
                    conn.execute("VACUUM")
                    rep["vacuumed"] = True
                    break
                except sqlite3.Error as e:
                    # 绝大多数情况是 database is locked：db-writer（或另一个窗口）
                    # 正抱着未提交事务。它最多持 DB_COMMIT_INTERVAL_S，等一下再来就行。
                    if attempt >= max(1, config.DB_MAINT_VACUUM_ATTEMPTS):
                        # **不上抛**：清理已经做完了，为一次空间回收把整次维护记成
                        # "未完成"，会让用户以为记录也没清干净。分开如实记。
                        rep["vacuum_error"] = str(e)
                        break
                    time.sleep(config.DB_MAINT_VACUUM_RETRY_S)
            rep["freelist_after"] = int(
                conn.execute("PRAGMA freelist_count").fetchone()[0])
        finally:
            conn.close()

    # ---------- D2. 清空（阶段二：设置页的「清空状态库」）----------

    # 【阶段二-新增】把指定的表清空。**这是本项目里唯一一个"主动删自己的数据"的动作**，
    # 所以每一条护栏都要写清楚它是防什么的：
    #
    # ① 表名走**白名单**，绝不拼用户/调用方给的字符串。这里没有注入面（调用方是
    #    本进程自己的按钮），但 `DELETE FROM %s` 这种写法一旦被复用就会成为注入面，
    #    而"现在没有注入面"不是理由 —— 白名单是零成本的。
    # ② `overwrites` 表**默认不在列表里**：它是跨源覆盖的唯一记录，删掉之后
    #    "谁把谁的片子覆盖了"再也查不出来。界面上它是一个独立的、默认不勾的勾选框。
    # ③ 只删行，**不动 schema**：meta / schema_version 留着，删完这个库还是 v2 的库，
    #    下次启动走的是同一条路径，不需要重建。
    # ④ VACUUM 有界重试，理由与 maintenance 逐字相同（database is locked 是常态，
    #    不是异常）。VACUUM 失败**不上抛**：行已经删了，为一次空间回收把整次操作
    #    记成"失败"，用户会以为没删干净。
    # ⑤ 连的是**短连接**，不是写连接 self._conn（那条由 db-writer 线程独占，
    #    碰它就是跨线程用 sqlite 连接）。调用方负责保证"此刻没有任务在跑" ——
    #    界面上的做法是传输中禁用这个按钮。
    PURGE_TABLES = ("files", "overwrites", "tasks")

    def purge(self, tables=None, log=None):
        """清空指定表。返回报告 dict：ok / deleted{表:条数} / vacuumed / error / elapsed_s。"""
        if tables is None:
            tables = ("files", "tasks")
        bad = [t for t in tables if t not in self.PURGE_TABLES]
        rep = {"ok": False, "deleted": {}, "vacuumed": False, "vacuum_error": None,
               "error": None, "elapsed_s": 0.0}
        if bad:
            # 白名单之外的表名一律拒绝，**包括"看起来像表名"的**。
            rep["error"] = "拒绝清空未登记的表：%s" % ", ".join(map(str, bad))
            return rep
        if not os.path.exists(self.db_path):
            rep["ok"] = True            # 没有库 == 没有数据可清，不是失败
            return rep

        t0 = time.time()
        conn = self._short_conn()
        try:
            for t in tables:
                conn.execute("BEGIN IMMEDIATE")
                try:
                    cur = conn.execute("DELETE FROM %s" % t)
                    rep["deleted"][t] = int(cur.rowcount if cur.rowcount >= 0 else 0)
                    conn.execute("COMMIT")
                except Exception:
                    try:
                        conn.execute("ROLLBACK")
                    except Exception:
                        pass
                    raise
            for attempt in range(1, max(1, config.DB_MAINT_VACUUM_ATTEMPTS) + 1):
                try:
                    conn.execute("VACUUM")
                    rep["vacuumed"] = True
                    break
                except sqlite3.Error as e:
                    if attempt >= max(1, config.DB_MAINT_VACUUM_ATTEMPTS):
                        rep["vacuum_error"] = str(e)
                        break
                    time.sleep(config.DB_MAINT_VACUUM_RETRY_S)
            rep["ok"] = True
        except Exception as e:
            rep["error"] = "%s: %s" % (type(e).__name__, e)
        finally:
            conn.close()
        rep["elapsed_s"] = round(time.time() - t0, 1)
        if log is not None:
            try:
                log(self._purge_summary(rep))
            except Exception:
                pass
        return rep

    @staticmethod
    def _purge_summary(rep):
        """把一次清空说成一行话。**失败必须出声**（与库维护那边相反）。

        为什么这里不像 `_db_maint_summary` 那样"没什么可说就返回 None"：
        清空是用户**主动按的**，按完一定要有一条回执 —— 哪怕是"清了 0 条"。
        开机顺手做的事没做可以不说，用户刚点的按钮没反应不行。
        """
        if rep.get("error"):
            return "[状态库] 清空失败：%s（原有记录未受影响）" % rep["error"]
        bits = ["%s %d 条" % (t, n) for t, n in sorted(rep.get("deleted", {}).items())]
        line = "[状态库] 已清空：" + ("、".join(bits) if bits else "无")
        if rep.get("vacuumed"):
            line += "，空间已回收"
        elif rep.get("vacuum_error"):
            line += "，空间未回收（%s），下次维护再收" % rep["vacuum_error"]
        return line

    # 【阶段二-新增】设置页要显示"库现在多大"。只读一行 COUNT，走短连接。
    # 库不存在 → 0（不是错误：全新机器上本来就还没建库）。
    def count_rows(self, table="files"):
        if table not in self.PURGE_TABLES:
            return 0
        if not os.path.exists(self.db_path):
            return 0
        with self._read() as conn:
            try:
                return int(conn.execute("SELECT COUNT(*) FROM %s" % table).fetchone()[0])
            except Exception:
                return 0

    # ---------- E. 辅助 ----------

    # 【阶段2a-修改原因】写入口 + 惰性事务。攒批期间不提交，
    # 由 batch_commit 统一 COMMIT，把每文件一次 fsync 降为每 200 个一次。
    def _execute_write(self, sql, params=()):
        if self._conn is None:
            raise sqlite3.ProgrammingError("状态库写连接未打开")
        if not self._conn.in_transaction:
            self._conn.execute("BEGIN")
        self._conn.execute(sql, params)

    # 【阶段2a-修改原因】提交攒批。in_transaction 判断使空提交成为无害的 no-op，
    # 调用方不必自己跟踪事务状态。
    def batch_commit(self):
        if self._conn is not None and self._conn.in_transaction:
            self._conn.execute("COMMIT")

    # 【阶段2a-修改原因】幂等关闭。退出前先尽力提交，避免丢掉最后一批记录。
    def close(self):
        # 【连接复用】顺手把本线程的读连接也收了。收的是**调用者线程自己那条**，
        # 其他线程的归它们自己（threading.local 在那些线程结束时自动回收）。
        self.close_read_conn()
        if self._conn is None:
            return
        try:
            if self._conn.in_transaction:
                self._conn.execute("COMMIT")
        except Exception:
            pass
        try:
            self._conn.close()
        except Exception:
            pass
        self._conn = None
