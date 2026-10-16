import os
import sys
import shutil
import json
import errno
import threading
import re
import sqlite3                                                     # 【阶段2a-新增】传输状态库
import queue                                                       # 【阶段1-新增】主线程 UI 消息通道
import time
import calendar                                                      # 【阶段2c-新增】FTP mtime 规范化
import datetime
from concurrent.futures import ThreadPoolExecutor, as_completed
# 【功能C1】`ttk` 从这一行里拿掉了：全文件最后一处 `ttk.X`（进度条）已换成
#   `ttkbootstrap.Progressbar`，剩下的 ttk 控件全部由 ttkbootstrap 提供。
#   留着它是个**死 import** —— 而"死 import 清理"是另一件排在裁定队列里的事，
#   这一处**不是**：它是本次改动**刚刚**造成的，不顺手拿掉就等于我亲手留了一条。
from tkinter import messagebox, filedialog, simpledialog, StringVar, BooleanVar, IntVar
from tkinter import font as tkfont        # 【功能B】按像素量速度标签的宽度（只用 stdlib）
import tkinter as tk
import ttkbootstrap                    # 【功能C1】界面主题。**本项目的唯一第三方依赖**
import ftplib
import socket                                                        # 【阶段3-新增】看门狗/断线分类
from pathlib import Path
import locale                                                        # 【阶段四-新增】解码 schtasks 的控制台输出
import subprocess                                                    # 【阶段四-新增】调 schtasks（唯一会起子进程的地方）
import tempfile                        # 【§三-B】`/Create /XML` 要一个临时文件路径



# 【第一刀】常量与纯工具已迁出到 config（见 PROJECT_STATE §7.4）。
# **故意不做 re-export**：`from config import X` 会让本模块拿到一份
# 独立副本，改 config 里的值对副本无效 —— 那正是测试侧注入守护要挡的静默失效。
import config
import ui_dialogs                    # 【第二刀】FTP 对话框已抽出，本模块不再定义它们
import ftp_client                    # 【第三刀】FTP 客户端（看门狗/连接池/断线重放）已抽出，本模块不再定义它们
import state_db                      # 【第四刀】传输状态库（任务锁/心跳/续传记录）已抽出，本模块不再定义它
import ui_channel                    # 【第五刀=第 1 步】UI 通道（消息泵/进度/速度/日志）已抽出，本模块不再定义它
import ftp_transfer
import maintenance_worker
import transfer_form
import sched_tab
import app_bootstrapper        # 【第七刀=第 6 步】应用装配器（装配序列/无头族/UX/恢复）已抽出，本模块不再定义它们
import transfer_core              # 【第六刀=第 2 步】传输内核（逐源编排/判重/终态/库写线程）已抽出，本模块不再定义它


# ===================== 【阶段三】共享文件夹（UNC） =====================
# 【为什么共享要并进"本地这一支"，而不是新开一条路线】
#   一个 `\\服务器\共享名` 在**传输层**上和一个本地目录没有任何区别：都是
#   `os.walk` + `open` + `shutil.copy2`（Windows 把 SMB 挂进同一个命名空间，
#   `os` 模块不区分）。所以这里**不新增 worker、不新增路线** —— 只新增
#   "用户是从菜单里选的哪种、这条路径是不是 UNC"这一层**界面语义**。
#
# 【凭据为什么不进本工具】SMB 会话是**登录会话级**的，由 Windows 自己维护；
#   工具去 `net use` 一次反而会造出第二份凭据状态（谁赢？什么时候过期？）。
#   用户先在资源管理器里连一次，之后本工具与任何别的程序看到的是**同一个**已连上的盘。
_LOCAL_LIKE_SRC = ("local", "share")

# ===================== 阶段四：无头模式 =====================
# 【当前这一次进程是不是"无头跑一条定时任务"】None = 正常的 GUI 启动；
#   非 None = 要跑的那条定时任务的名字（`--run-task <名>` 传进来的）。
#
# 【为什么是**模块级**变量，而不是 `FileTransferApp` 的实例属性】
#   因为 `FileTransferApp.__init__` 里就有几个会弹模态框的地方
#   （`_prompt_resume_task` 的"要不要继续上次的任务"、状态库不可用的警告、
#   `_start_db_maintenance`），而实例属性要等到构造完才有值 —— 那时候框已经弹出来了。
#   模块级在**构造之前**就已确定，所以那些地方问得出正确答案。
#
# 【为什么不是环境变量】计划任务命令行走的是命令行参数（`/TR` 里那一串），
#   环境变量在这条路上更绕、更难在日志里说清"这次到底是不是无头"。
_HEADLESS_TASK = None





























# ==================== 阶段四：schtasks 封装 ====================




def _launch_argv_prefix():
    """返回 `(可执行文件, 可选的脚本)` —— 计划任务该用什么把本程序拉起来。

    【两种形态，判据是 `sys.frozen`（PyInstaller 打的包会把它设成 True）】
      * **打包好的 exe**：`(那个 exe, None)` —— 直接就是它。
      * **从源码跑**（开发、自检脚本）：`(pythonw.exe, 本文件的绝对路径)`。
        ⚠️ **必须是 `pythonw.exe` 而不是 `python.exe`**：后者每跑一次任务就
        在用户屏幕上闪一个黑框。计划任务是无人值守的，闪黑框是最糟的一种打扰
        —— 而且它一天闪 N 次，用户只会以为中了病毒。
        找不到 `pythonw.exe` 才回落到 `sys.executable`（宁可闪框，不能不跑）。

    【为什么不用 `sys.argv[0]`】它可能是相对路径、可能是 `-c`。
    这里要的是"任务计划程序在**任意工作目录**下执行也能找到的那个路径"，
    所以一律 `os.path.abspath`。
    """
    if getattr(sys, "frozen", False):
        return sys.executable, None
    exe_dir = os.path.dirname(sys.executable)
    pyw = os.path.join(exe_dir, "pythonw.exe")
    if not os.path.exists(pyw):
        pyw = sys.executable
    return pyw, os.path.abspath(__file__)
















# 【哪些路径算"共享"】只认 UNC 前缀，两种写法都收：
#   `\\服务器\共享名`（Windows 原生）与 `//服务器/共享名`（正斜杠写法，同样可用）。
#   **不在这里做规范化**：`config.py:302-304` 已经把"`D:\a` / `D:/a` / `D:\a\`
#   算三个源"列为**已接受**，UNC 面对同一问题一并接受，不单开一刀。
def _is_unc_path(p):
    return p.startswith("\\\\") or p.startswith("//")


# ===================== 功能C1：界面主题（ttkbootstrap） =====================




























class _HostFormSink(object):
    r"""`TransferForm` 的**可写句柄**（宿主侧视图）。

    【它是什么】一张**白名单**：正好列出 L5 允许触碰的那些宿主属性，每个都是
      `@property` 代理回宿主**同一个**属性槽（读 `self._app.<名>`，写 `self._app.<名> = v`）。

    【为什么不让新模块直接拿宿主对象】不是为了功能（鸭子类型本来就够跑），而是为了
      **可审计**：L5 里写了 `self.<X>`，机械就能判它有没有越界（`X` 必须 ∈ 这张表）。

    【`__slots__` 是干什么的】钉死"只有 `_app` 一个槽" ⇒ 谁也别想往句柄上长出第二个同名属性
      （那就是第 4 步"观测槽两份"的形态）。只读面没有 setter：`sink.<只读名> = v` 当场报错。
    """

    __slots__ = ("_app",)

    def __init__(self, app):
        self._app = app

    # ---------- 只读 16 个：L5 方法体里 **0 处** `self.<名> = ` ----------
    #   故意**不给 setter**：读出去的就是宿主那**一个**对象 —— `self.X.set(...)` /
    #   `self.X.append(...)` / 控件方法 都是"对**返回对象**做变异"，与 setter 无关；
    #   而万一将来有人在 L5 里写了 `self.X = ...`（重绑），**当场 AttributeError**，
    #   比"悄悄把宿主那个对象换成新的"（L7 侧挂在旧对象上的引用全失效）好得多。
    #   —— 与第 4 步 6b4 同一条纪律：「响亮失败」优先于「静默兼容」。
    @property
    def db_keep_days(self):
        return self._app.db_keep_days

    @property
    def dest_path(self):
        return self._app.dest_path

    @property
    def dest_type(self):
        return self._app.dest_type

    @property
    def file_filter(self):
        return self._app.file_filter

    @property
    def flatten(self):
        return self._app.flatten

    @property
    def ftp_timeout(self):
        return self._app.ftp_timeout

    @property
    def log_export_dir(self):
        return self._app.log_export_dir

    @property
    def log_level(self):
        return self._app.log_level

    @property
    def purge_overwrites(self):
        return self._app.purge_overwrites

    @property
    def reconnect_count(self):
        return self._app.reconnect_count

    @property
    def retry_count(self):
        return self._app.retry_count

    @property
    def source_path(self):
        return self._app.source_path

    @property
    def source_type(self):
        return self._app.source_type

    @property
    def thread_count(self):
        return self._app.thread_count

    @property
    def transfer_mode(self):
        return self._app.transfer_mode

    @property
    def source_list(self):
        return self._app.source_list

    # ---------- 可写 17 个：L5 方法体里 **有** `self.<名> = ` ----------
    #   setter **写回宿主同一个槽**（不是新建、不是自己存一份）⇒ 全局只有一份。
    @property
    def dst_settings_btn(self):
        return self._app.dst_settings_btn

    @dst_settings_btn.setter
    def dst_settings_btn(self, value):
        self._app.dst_settings_btn = value

    @property
    def export_log_btn(self):
        return self._app.export_log_btn

    @export_log_btn.setter
    def export_log_btn(self, value):
        self._app.export_log_btn = value

    @property
    def pause_btn(self):
        return self._app.pause_btn

    @pause_btn.setter
    def pause_btn(self, value):
        self._app.pause_btn = value

    @property
    def profile_listbox(self):
        return self._app.profile_listbox

    @profile_listbox.setter
    def profile_listbox(self, value):
        self._app.profile_listbox = value

    @property
    def profile_summary(self):
        return self._app.profile_summary

    @profile_summary.setter
    def profile_summary(self, value):
        self._app.profile_summary = value

    @property
    def purge_btn(self):
        return self._app.purge_btn

    @purge_btn.setter
    def purge_btn(self, value):
        self._app.purge_btn = value

    @property
    def purge_overwrites_check(self):
        return self._app.purge_overwrites_check

    @purge_overwrites_check.setter
    def purge_overwrites_check(self, value):
        self._app.purge_overwrites_check = value

    @property
    def retry_btn(self):
        return self._app.retry_btn

    @retry_btn.setter
    def retry_btn(self, value):
        self._app.retry_btn = value

    @property
    def source_listbox(self):
        return self._app.source_listbox

    @source_listbox.setter
    def source_listbox(self, value):
        self._app.source_listbox = value

    @property
    def src_scrollbar(self):
        return self._app.src_scrollbar

    @src_scrollbar.setter
    def src_scrollbar(self, value):
        self._app.src_scrollbar = value

    @property
    def src_settings_btn(self):
        return self._app.src_settings_btn

    @src_settings_btn.setter
    def src_settings_btn(self, value):
        self._app.src_settings_btn = value

    @property
    def start_btn(self):
        return self._app.start_btn

    @start_btn.setter
    def start_btn(self, value):
        self._app.start_btn = value

    @property
    def stop_btn(self):
        return self._app.stop_btn

    @stop_btn.setter
    def stop_btn(self, value):
        self._app.stop_btn = value

    @property
    def ftp_active(self):
        return self._app.ftp_active

    @ftp_active.setter
    def ftp_active(self, value):
        self._app.ftp_active = value

    @property
    def ftp_source_config(self):
        return self._app.ftp_source_config

    @ftp_source_config.setter
    def ftp_source_config(self, value):
        self._app.ftp_source_config = value

    @property
    def ftp_dest_config(self):
        return self._app.ftp_dest_config

    @ftp_dest_config.setter
    def ftp_dest_config(self, value):
        self._app.ftp_dest_config = value

    @property
    def ftp_profiles(self):
        return self._app.ftp_profiles

    @ftp_profiles.setter
    def ftp_profiles(self, value):
        self._app.ftp_profiles = value



class _HostSchedSink(object):
    r"""`SchedTab` 的**可写句柄**（宿主侧视图）。

    【它是什么】一张**白名单**：正好列出 L6 允许触碰的那些宿主属性，每个都是
      `@property` 代理回宿主**同一个**属性槽（读 `self._app.<名>`，写 `self._app.<名> = v`）。

    【为什么不让新模块直接拿宿主对象】不是为了功能（鸭子类型本来就够跑），而是为了
      **可审计**：L6 里写了 `self.<X>`，机械就能判它有没有越界（`X` 必须 ∈ 这张表）。

    【`__slots__` 是干什么的】钉死"只有 `_app` 一个槽" ⇒ 谁也别想往句柄上长出第二个同名属性
      （那就是第 4 步"观测槽两份"的形态）。只读面没有 setter：`sink.<只读名> = v` 当场报错。

    【与 5a 的 `_HostFormSink` 方向不同】5a 的 13 个控件由 L5 自建（宿主不许自持）；
      本模块的 6 个 `sched_*` 控件由**宿主** `_build_task_tab` 建、L6 **只读**
      ⇒ 它们落在**只读面**，宿主**必须**已经建好（否则代理一读就 AttributeError）。
    """

    __slots__ = ("_app",)

    def __init__(self, app):
        self._app = app

    # ---------- 只读 20 个：L6 方法体里 **0 处** `self.<名> = ` ----------
    #   故意**不给 setter**：读出去的就是宿主那**一个**对象 —— `self.X.get()` /
    #   `self.X.configure(...)` 都是"对**返回对象**做操作"，与 setter 无关；
    #   而万一将来有人在 L6 里写了 `self.X = ...`（重绑），**当场 AttributeError**，
    #   比"悄悄把宿主那个对象换成新的"（L7 侧挂在旧对象上的引用全失效）好得多。
    #   —— 与第 4 步 6b4 同一条纪律：「响亮失败」优先于「静默兼容」。
    @property
    def date_template(self):
        return self._app.date_template

    @property
    def db_keep_days(self):
        return self._app.db_keep_days

    @property
    def dest_path(self):
        return self._app.dest_path

    @property
    def file_filter(self):
        return self._app.file_filter

    @property
    def flatten(self):
        return self._app.flatten

    @property
    def ftp_timeout(self):
        return self._app.ftp_timeout

    @property
    def log_export_dir(self):
        return self._app.log_export_dir

    @property
    def log_level(self):
        return self._app.log_level

    @property
    def reconnect_count(self):
        return self._app.reconnect_count

    @property
    def retry_count(self):
        return self._app.retry_count

    @property
    def source_path(self):
        return self._app.source_path

    @property
    def thread_count(self):
        return self._app.thread_count

    @property
    def transfer_mode(self):
        return self._app.transfer_mode

    @property
    def source_list(self):
        return self._app.source_list

    @property
    def sched_banner(self):
        return self._app.sched_banner

    @property
    def sched_banner_label(self):
        return self._app.sched_banner_label

    @property
    def sched_cmd_text(self):
        return self._app.sched_cmd_text

    @property
    def sched_detail(self):
        return self._app.sched_detail

    @property
    def sched_listbox(self):
        return self._app.sched_listbox

    @property
    def sched_toggle_btn(self):
        return self._app.sched_toggle_btn

    # ---------- 可写 4 个：L6 方法体里 **有** `self.<名> = ` ----------
    #   setter **写回宿主同一个槽**（不是新建、不是自己存一份）⇒ 全局只有一份。
    @property
    def _sync_refresh_id(self):
        return self._app._sync_refresh_id

    @_sync_refresh_id.setter
    def _sync_refresh_id(self, value):
        self._app._sync_refresh_id = value

    @property
    def sched_checked_at(self):
        return self._app.sched_checked_at

    @sched_checked_at.setter
    def sched_checked_at(self, value):
        self._app.sched_checked_at = value

    @property
    def sched_status(self):
        return self._app.sched_status

    @sched_status.setter
    def sched_status(self, value):
        self._app.sched_status = value

    @property
    def sched_tasks(self):
        return self._app.sched_tasks

    @sched_tasks.setter
    def sched_tasks(self, value):
        self._app.sched_tasks = value



# ===================== 主程序 =====================
class FileTransferApp:
    def __init__(self, root):
        # 【第 6 步】装配序列（564 行）已整体搬进 `app_bootstrapper.AppBootstrapper`（L7）。
        #   本方法**只留一行委托** —— 用户 2026-10-14 裁定②「钉死形态」：
        #   委托之后**不许再有任何装配代码**（否则"委托了又没委托"＝两份槽，
        #   第 4 步 `_part_reclaim` 那条真红就是这个形态）。
        #   `self._boot` 是"宿主 → 装配器"的持有者，**恰好一份** ——
        #   与既有 `self._core` / `_ui` / `_ftp` / `_maint` / `_form` / `_sched`
        #   同一套（硬自检 6b8 的枚举面由 6 → 7）。
        #   `host_ns=` 注入的是**本模块对象**：装配序列要造那 6 个句柄类、要用
        #   `_launch_argv_prefix` / `_is_unc_path` / `_LOCAL_LIKE_SRC` / `_HEADLESS_TASK`、
        #   无头入口还要造 `FileTransferApp` 自己 —— 而 `app_bootstrapper` **不许**
        #   import 本模块（它是唯一不产生反向边的模块）。
        #   `sys.modules[__name__]` 在"直接跑脚本"（`__name__ == '__main__'`）与
        #   "被 import"（`__name__ == 'file_transfer'`）两种情形下都指本模块。
        self._boot = app_bootstrapper.AppBootstrapper(
            self, root, sys.modules[__name__])


    # ==================== 阶段5-4：状态库长期维护（Q1 第 4 项） ====================



    # ==================== 阶段二：清空状态库（Tab2） ====================





    # ==================== 阶段2d：恢复上次任务（方案 §7） ====================



    # ==================== 功能A：记住上次路径 ====================






    # 【阶段2a-修改原因】关闭窗口前把未完成任务显式标为 aborted（reason='user_abort'），
    # 避免任务级锁被一条心跳停止的 running 行占住，直到 30 秒超时才被清掉。
    def _on_close(self):
        # 传输中（开始按钮被禁用）关闭窗口会中断任务并留下不完整文件，需确认
        if str(self.start_btn["state"]) == "disabled":
            if messagebox.askokcancel("退出", "传输正在进行，关闭窗口将中断任务并留下不完整文件。\n确定退出吗？"):
                self._core._abort_current_task("user_abort")
                self.root.destroy()
        else:
            self.root.destroy()

    # ==================== 功能C1：控件换肤（ttkbootstrap） ====================
    # 【这一节改了什么、没改什么 —— 判据是硬约束 9】
    #   改的：**控件类**（`tk.X` → `ttkbootstrap.X`）与**配色**。用户看到的东西变了。
    #   没改：14 行 grid 的**行号/列号/columnspan/sticky/pady 一个字符没动**，
    #         pack 的 side/fill/expand/padx 一个字符没动，控件的**创建顺序、名称、
    #         文字、绑定的事件**一个字符没动。用户的操作方式没变。
    #   → 按硬约束 9 的判据（"看到的东西变了 = 缺陷/美化修复；操作方式变了 = 布局变更"），
    #     这属于前者，是你 C1 裁定里明确授权的范围。
    #
    # 【为什么必须把 tk.* 全换掉，而不是"装个库换个主题就行"】
    #   这个界面原本 37 个控件里只有 1 个是 ttk（进度条），其余全是 tk 原生。
    #
    #   ⚠️ **先更正一句我自己写错的话**（侦察阶段写进 `05_prep_C.md` §3.1 的）：
    #      那里说"ttkbootstrap 的主题只作用于 ttk 控件，裸 `tk.Button` 不会跟着变"。
    #      **这句是错的。** 实测（同一进程，挂 `Style(theme="cosmo")` 前后各量一次）：
    #          tk.Label    bg  SystemButtonFace → #ffffff
    #          tk.Button   bg  SystemButtonFace → #2780e3（主色）
    #          tk.Entry    bg  SystemWindow     → #fdfdfe
    #      原因是 ttkbootstrap 除了建样式，还会往 **Tk 的 option database** 里写默认值
    #      （`*Menu*background` / `*Button.background` 那一类），而 option database
    #      对**之后创建的所有** tk 控件生效。所以"颜色"其实会跟着变。
    #
    #   **但换控件类仍然是对的，理由换成实测出来的这两条**：
    #     ① **几何不会跟着变**。tk 原生控件拿不到主题的 `padding` / `relief` / 边框，
    #        它还是 Windows 那套"凸起立体边"。实测同一批控件的声明高：
    #          Label 23→21  Button 26→33  Entry 23→31  Spinbox 23→31
    #          Radiobutton 27→17  Checkbutton 27→17
    #        ——**有增有减，幅度最大 10px**。14 行 grid 的高度是这些数加出来的，
    #        所以"换不换控件类"直接决定那个 640 的盒子成不成立，不是审美问题。
    #     ② **状态色不会跟着变**。hover / pressed / disabled / focus 这四态
    #        是 ttk **样式映射**（`style.map`）的事，option database 只给静态默认值。
    #        裸 `tk.Button` 的禁用态还是系统灰，跟旁边 ttk 控件的禁用态对不上 ——
    #        而本界面**开局就有一排禁用按钮**（暂停/停止/重传失败），
    #        那正是用户第一眼看到的东西。
    #   → 结论没变（全换），但依据从"颜色不会变"改成"几何与状态色不会变"。
    #
    # 【两处不是机械替换的地方，都写在各自旁边】
    #   * `main` 的 `padx=20, pady=20` → `padding=(20, 20)`：ttk.Frame 没有
    #     `padx/pady` 这两个选项（那是 tk.Frame 的），它有 `padding`。
    #     两个元素的 `padding` 按 Tk 的规则是 (左右, 上下)，与原来等价（实测 reqheight 一致）。
    #   * 两个 `OptionMenu` 的**参数多了一个** —— 见下面那段注释，这是个会静默吃掉
    #     一个选项的坑。
    # 【阶段二：标签页重构】把上面那 14 行 grid 装进两个 Tab。
    #
    # 【这一节改了什么、没改什么 —— 判据是用户给的阶段二边界】
    #   改的：**排布**。原来 14 行平铺在一个 `main` 里，现在分成
    #         「源 / 目标 / 选项 / 进度 / 日志」五个分区 + 一个全新的「设置」页。
    #   没改的：**控件本身**。Tab1 里每一个控件的类、文字、绑定的事件、
    #         以及**在 self 上的属性名**一个字符没动 —— `self.source_listbox` /
    #         `self.pause_btn` / `self._ui.log_text` 还是原来那些对象。
    #         这不是审美上的保守：auto_test_stage1~5 里几十处 `app.xxx` 直接取用它们，
    #         改名字等于把整套阶段验收脚本一起改，而"改测试以适配生产"正是要防的事。
    #   新增的：Tab2 一整个页面（阶段二立项内容），以及分区外壳。
    #
    # 【Tab 切换为什么不重新初始化 —— 这里没有"实现"，只有"不做"】
    #   两个 Tab 的 Frame 在**本函数里一次建好，此后永不销毁**；
    #   不绑 `<<NotebookTabChanged>>`、不做懒加载、不在切换时重建任何控件。
    #   于是"状态保留"是**结构的必然**：Tk 变量与控件都还活着，没有代码能去重置它们。
    #   反过来，任何"切过去再重建"的写法都会让这个保证变成一句承诺 ——
    #   而承诺会在某次"顺手清理一下"时被删掉，结构不会。
    def _build_ui(self):
        outer = ttkbootstrap.Frame(self.root, padding=(20, 20))
        outer.pack(fill="both", expand=True)
        self.notebook = ttkbootstrap.Notebook(outer)
        self.notebook.pack(fill="both", expand=True)
        self.tab_transfer = ttkbootstrap.Frame(self.notebook)
        self.tab_settings = ttkbootstrap.Frame(self.notebook)
        # 【阶段四】第 3 个 Tab。**铁律照旧**：与本函数里另外两页一样 ——
        #   一次建好、此后永不销毁、不绑 `<<NotebookTabChanged>>`、不做懒加载。
        #   于是"这一页上的东西一直在"是结构的必然，不是一句承诺：
        #   任务的勾选状态、命令行框里显示的那条命令、顶部那条"工具已移动"的提示，
        #   切走再切回来一个字都不会变，因为没有任何代码有本事去重置它们。
        self.tab_schedule = ttkbootstrap.Frame(self.notebook)
        self.notebook.add(self.tab_transfer, text="传输")
        self.notebook.add(self.tab_settings, text="设置")
        self.notebook.add(self.tab_schedule, text="定时任务")
        self._form._build_transfer_tab(self.tab_transfer)
        self._form._build_settings_tab(self.tab_settings)
        self._build_task_tab(self.tab_schedule)

    # ==================== 阶段四：Tab3「定时任务」 ====================
    def _build_task_tab(self, main):
        """Tab3「定时任务」。**全是新增控件**，前两页一个控件都没动。

        【这一页为什么可以"只在打开时读一次、之后用手动刷新"】
          它显示的是**别处的状态**：配置文件里的任务列表，与系统任务计划程序里
          的注册情况。这两样都可能在本程序之外被改（用户直接用 schtasks，
          或另一份配置同步过来）。所以页上必须有一个「刷新」，
          而且**不假装自己是实时的** —— 标着"上次检查：<时间>"，
          比一个悄悄过期的列表诚实。
        """
        main.columnconfigure(0, weight=1)
        main.rowconfigure(1, weight=1)

        # ---- ① 顶部提示条（默认不显示）----
        # 【为什么先建好再 `grid_remove()`】铁律要求这一页的控件一次建好永不销毁。
        #   用 `grid_remove()`（不是 `grid_forget()`）是为了**记住原来的位置参数**，
        #   再 `grid()` 回来时不用重写一遍坐标 —— 重写就会有两个地方描述它的位置。
        self.sched_banner = ttkbootstrap.Frame(main)
        self.sched_banner.grid(row=0, column=0, sticky="ew", pady=(0, 8))
        self.sched_banner.columnconfigure(0, weight=1)
        self.sched_banner_label = ttkbootstrap.Label(
            self.sched_banner, text="", bootstyle="warning", anchor="w",
            justify="left", wraplength=640)
        self.sched_banner_label.grid(row=0, column=0, sticky="ew")
        self.sched_banner_btn = ttkbootstrap.Button(
            self.sched_banner, text="全部重新注册", width=14,
            command=self._sched._sched_register_all, bootstyle="warning")
        self.sched_banner_btn.grid(row=0, column=1, sticky="e", padx=(8, 0))
        self.sched_banner.grid_remove()

        # ---- ② 任务列表 ----
        list_group = ttkbootstrap.Labelframe(main, text="定时任务（一句话一条；括号里是它当前的状态）")
        list_group.grid(row=1, column=0, sticky="nsew", pady=(0, 8))
        list_group.columnconfigure(0, weight=1)
        list_group.rowconfigure(0, weight=1)

        list_row = ttkbootstrap.Frame(list_group)
        list_row.grid(row=0, column=0, sticky="nsew")
        self.sched_listbox = tk.Listbox(
            list_row, height=7, exportselection=False,
            bg=self._entry_style("background"), fg=self._entry_style("foreground"),
            selectbackground=ttkbootstrap.Style.instance.colors.primary,
            selectforeground="#ffffff", relief="flat", borderwidth=0,
            highlightthickness=1,
            highlightbackground=self._entry_style("darkcolor"),
            highlightcolor=ttkbootstrap.Style.instance.colors.primary,
            activestyle="none")
        self.sched_listbox.pack(side="left", fill="both", expand=True, padx=(0, 4))
        # 【绑定写在 `_build_ui` 的这一条路上，与 profile_listbox 同一手法】
        #   只是"选中哪一条 → 下面那行详情跟着变"，没有任何副作用。
        self.sched_listbox.bind("<<ListboxSelect>>", self._sched._sched_on_select)
        sched_sb = ttkbootstrap.Scrollbar(list_row, orient="vertical",
                                          command=self.sched_listbox.yview)
        sched_sb.pack(side="left", fill="y")
        self.sched_listbox.configure(yscrollcommand=sched_sb.set)

        btns = ttkbootstrap.Frame(list_group)
        btns.grid(row=0, column=1, sticky="nw", padx=(8, 0))
        ttkbootstrap.Button(btns, text="新建…", width=10, command=self._sched._sched_new,
                            bootstyle="primary").pack(fill="x", pady=(0, 4))
        # 「编辑…」「删除」「启用/停用」都作用于**选中的那一条** —— 与设置页
        # 那三个按钮同一条约定：选着 B 却改到 A，是静默改错地方。
        ttkbootstrap.Button(btns, text="编辑…", width=10, command=self._sched._sched_edit,
                            bootstyle="secondary-outline").pack(fill="x", pady=(0, 4))
        self.sched_toggle_btn = ttkbootstrap.Button(
            btns, text="停用", width=10, command=self._sched._sched_toggle,
            bootstyle="secondary-outline")
        self.sched_toggle_btn.pack(fill="x", pady=(0, 4))
        ttkbootstrap.Button(btns, text="删除", width=10, command=self._sched._sched_delete,
                            bootstyle="secondary-outline").pack(fill="x")

        # ---- ③ 选中那条的详情 ----
        self.sched_detail = ttkbootstrap.Label(list_group, text="", bootstyle="secondary",
                                               anchor="w", justify="left", wraplength=620)
        self.sched_detail.grid(row=1, column=0, columnspan=2, sticky="ew", pady=(8, 0))

        # ---- ④ 日期分层增量（阶段四·P0-1）----
        # 【为什么这一组在「定时任务」页，而不在「设置」页】
        #   实测（`_checks\_probe_tab2_note.py`）：传输页余 106px、**设置页只剩 32px**、
        #   本页余 223px，而窗口 `geometry("888x717")` 是写死的 —— 内容变高**不会**
        #   把窗户撑大，只会把底部裁掉（被裁的正好是那一页最要紧的说明文字）。
        #   本页既有余量又紧挨着"任务参数就是「传输」页那一套"的语境。
        # 【为什么这组控件是"三件"而不是"四件"】模板输入框 / 同步点显示 / 「重置同步点」。
        #   没有「启用」勾选框：模板非空即启用（理由见 `config.parse_date_template`）。
        dl_group = ttkbootstrap.Labelframe(
            main, text="日期分层增量（可选：源目录按日期分层时，只扫上次之后的部分）")
        dl_group.grid(row=2, column=0, sticky="ew", pady=(0, 8))
        dl_group.columnconfigure(1, weight=1)
        ttkbootstrap.Label(dl_group, text="日期模板：", anchor="w").grid(
            row=0, column=0, sticky="w")
        ttkbootstrap.Entry(dl_group, textvariable=self.date_template).grid(
            row=0, column=1, sticky="ew", padx=(0, 8))
        ttkbootstrap.Button(dl_group, text="重置同步点", width=12,
                            command=self._core._reset_syncpoint,
                            bootstyle="secondary-outline").grid(row=0, column=2, sticky="e")
        ttkbootstrap.Label(
            dl_group, bootstyle="secondary", anchor="w", justify="left",
            text="填 YYYY/MM/DD 这样的层（大写、用 / 隔开）；**留空 = 关闭**。"
            ).grid(row=1, column=0, columnspan=3, sticky="w", pady=(4, 0))
        # 同步点只读显示。**它不是实时刷新的一面镜子** —— 它按「传输」页当前的
        # 源/目标 + 上面的模板去查，改了源或目标之后靠 `_schedule_syncpoint_refresh`
        # 的防抖回调跟上；真的没跟上时，它显示的是"上一次查到的值"而不是一个错值
        # （查不到的一律写「（无）」）。
        self.sync_label = ttkbootstrap.Label(
            dl_group, text="", bootstyle="secondary", anchor="w", justify="left",
            wraplength=620)
        self.sync_label.grid(row=2, column=0, columnspan=3, sticky="ew", pady=(4, 0))

        # ---- ⑤ 命令行（可复制）----
        # ⚠️ 行号是从这里往下的**全部**：本组插在 row=2 之后，原来占 2/3 的两块各让一格。
        cmd_group = ttkbootstrap.Labelframe(main, text="这条任务对应的系统命令（只读，可复制）")
        cmd_group.grid(row=3, column=0, sticky="ew", pady=(0, 8))
        cmd_group.columnconfigure(0, weight=1)
        self.sched_cmd_text = tk.Text(cmd_group, height=2, wrap="char",
                                      relief="flat", borderwidth=0,
                                      highlightthickness=1, state="disabled",
                                      bg=self._entry_style("background"),
                                      fg=self._entry_style("foreground"),
                                      highlightbackground=self._entry_style("darkcolor"))
        self.sched_cmd_text.grid(row=0, column=0, sticky="ew", padx=(0, 4))
        cmd_btns = ttkbootstrap.Frame(cmd_group)
        cmd_btns.grid(row=0, column=1, sticky="n")
        ttkbootstrap.Button(cmd_btns, text="复制", width=8, command=self._sched._sched_copy_cmd,
                            bootstyle="secondary-outline").pack(fill="x", pady=(0, 4))
        ttkbootstrap.Button(cmd_btns, text="刷新状态", width=8,
                            command=self._sched._sched_refresh_status,
                            bootstyle="secondary-outline").pack(fill="x")

        # ---- ⑥ 说明 ----
        # 【这一段不是装饰】"注册要管理员吗""为什么只在登录时跑""保存为什么立即生效"
        #   是这一页最容易被问到的三件事，答案都必须写在这里 ——
        #   写在文档里的答案，用户在这一页上是看不见的。
        # 【日期分层那三行同理】它是本页唯一"跑起来可能少看见一批目录"的功能，
        #   而它默认关闭、失败方向又不对称（剪错了不报错），所以把"怎么用、什么时候
        #   不该用、怎么退回全扫"写在同一页上。
        self.sched_hint = ttkbootstrap.Label(
            main, bootstyle="secondary", anchor="w", justify="left", wraplength=680,
            text=("· 任务参数 = 你点「保存」那一刻「传输」页上的设置（源/目标/模式/过滤/线程数…）。\n"
                  "· 保存后**立即**写入系统计划任务；停用会把它从系统里删掉。\n"
                  "· 任务只在**你登录时**运行（共享文件夹的凭据是登录会话级的，不登录跑不了）。\n"
                  "· 到点时机器没开机 / 在睡眠 ⇒ **下次可用时补跑一次**（不会把错过的每一天\n"
                  "  都补回来）；**用电池时不会启动**（这是 Windows 的默认值，本工具没改）。\n"
                  "· 注册不需要管理员。若被拒绝访问，配置照样会保存，把上面那条命令\n"
                  "  复制到**管理员 PowerShell** 里粘一次即可。\n"
                  "· 日期分层：源根下面必须**正好**按模板那几层分日期（例如 源/2026/07/31/…），\n"
                  "  且日期段要用**定宽数字**（2026 而不是 26）。只比字符串大小、不解析日期，\n"
                  "  所以不跨世纪、不认时区、也不认非公历。**同步点只在这一趟正常跑完后推进**\n"
                  "  （被停止 / 出错都不推进）；想让它下次全量重扫，点「重置同步点」。"))
        self.sched_hint.grid(row=4, column=0, sticky="ew")

        self.sched_tasks = []
        self.sched_status = {}
        self.sched_checked_at = ""
        self._sched._sched_reload(select=None)

        # 【P0-1】同步点标签的四个输入源，都是**低频**改动（模板手打、两个下拉框选、
        #   源/目标路径手打或粘贴）。走 `_schedule_syncpoint_refresh` 的 400ms 防抖，
        #   免得在源框里每敲一个字符就开一次 SQLite 连接。
        # 【为什么不用轮询】这一页的控件一次建好永不销毁，轮询会变成一根永久空转的链。
        self.date_template.trace_add("write", self._sched._schedule_syncpoint_refresh)
        for _v in (self.source_path, self.dest_path, self.source_type, self.dest_type):
            _v.trace_add("write", self._sched._schedule_syncpoint_refresh)
        self._core._refresh_syncpoint_label()

    # ==================== 阶段四·P0-1：日期分层（同步点） ====================
    #
    # 【本节的四件事，按"什么时候发生"排】
    #   1. 查：`_read_syncpoint` —— 读 `meta` 并校验，任何不对劲 ⇒ None ⇒ 全扫；
    #   2. 建：`_date_layer_begin` —— 主线程，把模板 + 同步点做成一个 `DateLayerPruner`；
    #   3. 收：`_date_layer_finish` —— 终态，出声 + **只在正常跑完时**推进同步点；
    #   4. 面：`_refresh_syncpoint_label` / `_reset_syncpoint` —— Tab3 上那两件控件。
    #
    # 【贯穿全节的一条判据】"这次到底有没有启用日期分层"**只有一个答案**：
    #   `self._date_pruner is not None`。四处 `os.walk`、FTP 调用点、收尾，都只看它。
    #   没有任何一处去读 `self.date_template` —— 那是个 Tk 变量，后台线程碰不得
    #   （`_start_db_maintenance` 那条注释里记着这个教训：读它会撞
    #    `RuntimeError: main thread is not in main loop`，而异常会被兜底吞掉，
    #    结果是"功能静默失效、界面上只多一行字"）。







































    # 【Listbox 的皮肤为什么要手配 —— 与 Tab1 的 source_listbox 同一个理由】
    #   原生 `tk.Listbox` 没有主题概念，默认是"凹陷边框 + 系统配色"，插在扁平白底的
    #   cosmo 里很扎眼。这里取的是**周边 `Entry` 正在用的那几个颜色**（不是挑的），
    #   两个 Listbox 因此长得一样 —— 顺手把它抽成一个方法，免得第二处的取值和第一处
    #   哪天对不上（对不上的症状是"两个列表长得不一样"，很难说清哪个才是对的）。
    @staticmethod
    def _entry_style(key):
        return ttkbootstrap.Style.instance.lookup("TEntry", key) \
            or getattr(ttkbootstrap.Style.instance.colors,
                       {"background": "bg", "foreground": "fg",
                        "darkcolor": "border"}.get(key, "bg"))


    # ==================== 阶段二：多套 FTP 配置（Tab2） ====================

























    # ==================== 传输逻辑 ====================








    # ==================== 阶段2b：原子落盘 + 可信续传 ====================

















    # ==================== 阶段3：FTP 并发（1 扫描 + N worker + 连接池） ====================





















    # ==================== 阶段2a：状态库通道 ====================

    # 【阶段2a-修改原因】标题栏改动的唯一出口，只在主线程执行（Tk 不是线程安全的）。
    # 用 root.title 直接设，不引入新的 Tk 变量（Q11）。
    def _set_title(self, text):
        assert threading.current_thread() is threading.main_thread(), \
            "_set_title 必须在主线程调用"
        try:
            self.root.title(text)
        except Exception:
            pass



























































# ===================== 【第 2 步】宿主侧的胶水与只读视图 =====================
#
# 这三个类是"第 2 步"新增的**装配件**：内核靠它们去够上层 / 去读统计，
# 但**内核一个字都不知道宿主是谁**（它只拿到两个 duck-typed 对象 + 一个只读视图）。


class _HostHooks(object):
    """宿主实现的 `hooks`（旧代码里那些**直接调用**，一行不改地挪到这里来）。

    ⚠️ 每个方法的实现体就是原来 core 直接调的那几行 —— **参数、时机、文案都不动**，
       只是"谁调它"从 core 内联变成了 core → hooks。
    """

    def __init__(self, app):
        self._app = app

    def on_fatal(self, code, msg):
        return self._app._boot._headless_bail(code, msg)

    def on_task_end(self, spec, reason, copied, skipped, failed, stopped):
        return self._app._boot._headless_finish(spec, reason, copied, skipped, failed, stopped)

    def on_ux_changed(self):
        return config.save_ux_state(self._app._boot._collect_ux_state())

    def on_purge_state_changed(self):
        return self._app._maint._sync_purge_btn()

    def on_path_problem(self, text):
        # 与 `_check_local_path` 原来那三处调用**同标题、同正文**（标题恒为"错误"）。
        return messagebox.showerror("错误", text)


class _HostLauncher(object):
    """宿主实现的 `launcher`（"这批走 FTP 还是本地"由它决定 —— 于是 core 不 import ftp）。

    ⚠️ 分派判据（`src_type` / `dst_type` 那些 `if`）**逐行原样留在 core**，
       这里只是"调用谁"的落点。
    """

    def __init__(self, app):
        self._app = app

    def start_ftp_to_local(self, extensions):
        return self._app._ftp._start_ftp_to_local(extensions)

    def start_local_to_ftp(self, extensions):
        return self._app._ftp._start_local_to_ftp(extensions)


class _UiStatsView(object):
    """`ui_channel` 要的**只读三件套**（`_completed` / `_total` / `_task_active`）适配器。

    【为什么需要一个适配器，而不是把 `_core` 直接传过去】
      第 2 步之后这三个数**不在同一个对象上**：`_completed` / `_task_active` 归
      `transfer_core`（内核自己写），`_total` **仍归宿主**（它由 FTP 那一路的扫描器写，
      第 3 步才随 `ftp_transfer` 走）。传任何一个都拿不齐三个。
      ⇒ 这里把三处读**穿透**到真正的持有者，**不是副本**（没有任何一份被拷贝的值，
        所以不可能出现"两份计数器"）。
    【为什么 `ui_channel` 不直接 import `transfer_core`】
      分层里 `ui_channel`(L1) 在 `transfer_core`(L2) **下面**，向上 import 就是反向边。
      这里是宿主（L7）把视图对象**注入**下去 —— 与 `pause_event` / `scan_dirs` 同一个模式。
    """

    def __init__(self, app):
        self._app = app

    @property
    def _completed(self):
        return self._app._core._completed

    @property
    def _total(self):
        # 【第 3 步】`_total` 随 `ftp_transfer` 搬走 ⇒ 读 ftp 那一份（不是宿主）。
        return self._app._ftp._total

    @property
    def _task_active(self):
        return self._app._core._task_active


class _HostLiveView(object):
    r"""把宿主**会在运行期重新赋值**的几个值，按引用暴露给内核（core 不 import 上层）。

    【为什么需要它 —— 第 2 步实测到的两个**真回归**（缺陷形态⑤：构造快照不穿透）】
      内核构造时若把这些值**按值快照**收进自身，宿主之后任何赋值都只落在宿主属性上，
      内核永远读构造那一刻那一份：
        * `ftp_dest_config` / `ftp_source_config` 恒 `None`（构造时还是初值）⇒
          `_launch_source` 的两道 FTP 校验恒真 ⇒ **FTP 上传与 FTP 源任务全被
          「请先设置 FTP 目标」拒掉**（界面配好了也起不来）；
        * `source_list` 恒为构造时的空列表 ⇒ `_start_transfer` 建的 `_src_queue` 恒空
          ⇒ **多源串行驱动器静默失效**（只剩下当前 `source_path` 一个源在跑）。
      ⇒ 每读一次都**回宿主取当前值**，宿主怎么改都立刻可见。
        （与 `_UiStatsView` 同一个模式；宿主侧注入，内核不含任何 `import` 上层的语句。）

    【什么该放进来 —— 两个条件缺一不可】
      ① 宿主会在构造**之后**重新赋值这个属性（不是 `StringVar.set()` 那种"改内部状态"）；
      ② 内核在运行期要读它。
      ⇒ Tk 变量（内核走 `.get()`，对象身份不变）、锁 / 事件 / `db`（对象身份不变、
        只换内部状态）、模块级常量（`_HEADLESS_TASK` / `_LOCAL_LIKE_SRC` / `_is_unc_path`）
        都**不需要**视图。判据与实测清单见 `_checks\_nightly\60`~`63` 与项目记忆。
    """

    __slots__ = ("_app",)

    def __init__(self, app):
        self._app = app

    @property
    def ftp_dest(self):
        return self._app.ftp_dest_config

    @property
    def ftp_src(self):
        return self._app.ftp_source_config

    @property
    def source_list(self):
        return self._app.source_list


if __name__ == "__main__":
    # 【阶段四】无头模式的入口。**必须排在下面那道耦合检查之前**：
    #   耦合检查走的是"建一个 Tk + 弹 messagebox"那条路，而无头模式下
    #   那条路上的 messagebox **没有人点** —— 进程会永远挂在框上，
    #   而 schtasks 那边看到的是一个"正在运行"的任务。同样的理由，
    #   下面那道检查本身也要在无头模式下改走"写日志 + 退出码 4"。
    _headless_name = sched_tab._parse_run_task_arg(sys.argv[1:])

    # 【阶段2a-修改原因】强制耦合检查（方案 §6.2）。
    # _STATE_DB_ENABLED=True 而 _ATOMIC_WRITE_ENABLED=False 意味着：
    # 状态库记录文件已完成，但磁盘上可能是写入中断的半截文件，续传会静默跳过它们
    # ——正是整个阶段 2 要消灭的静默数据丢失。这是开发者配置错误，
    # 必须立即、可见地失败，而不是静默降级（静默降级会让人以为开关生效了）。
    # 反向组合（DB 关、原子开）是安全的，允许。
    if config._STATE_DB_ENABLED and not config._ATOMIC_WRITE_ENABLED:
        _msg = ("_STATE_DB_ENABLED = True 时，_ATOMIC_WRITE_ENABLED 必须为 True。\n\n"
                "该组合会导致：状态库记录文件已完成，但磁盘上可能是写入中断的半截文件，\n"
                "续传时会静默跳过这些损坏文件。\n\n"
                "请把 _ATOMIC_WRITE_ENABLED 改回 True 后重启。")
        if _headless_name is not None:
            # 无头模式：这个框没有人点（见上面入口处的注释）。写进这条任务的日志文件
            # 再退 4 —— 用户翻日志时看到的是**完整那句话**，不是一句转述。
            sys.stderr.write("[无头] 配置错误：%s\n" % _msg)
            try:
                os.makedirs(config.SCHEDULE_LOG_DIR, exist_ok=True)
                with open(config.schedule_log_path(_headless_name or "unknown"),
                          'a', encoding='utf-8') as _f:
                    _f.write("=" * 78 + "\n[无头] **配置错误，任务没有跑**\n%s\n%s\n"
                             % (_msg, "=" * 78))
            except Exception:
                pass
            sys.exit(config.SCHEDULE_EXIT_CANNOT_START)
        _r = tk.Tk()
        _r.withdraw()
        messagebox.showerror("配置错误", _msg)
        _r.destroy()
        sys.exit(1)

    # 【阶段四】无头：藏起窗口跑完 `--run-task` 指定的那条任务，用它的返回值当退出码。
    #   **这一行之后不再有 GUI 路径** —— `sys.exit` 的码就是 schtasks 看到的全部信号，
    #   所以 `_run_headless` 每一条出口都必须给一个码（见 `config.SCHEDULE_EXIT_*`）。
    if _headless_name is not None:
        # `__main__` 这一段本身就是模块作用域，直接赋值即可（不需要 `global`）。
        # **必须在 `FileTransferApp(root)` 之前**：构造函数里那几个弹框点要读它。
        _HEADLESS_TASK = _headless_name
        # 【总闸：从此本模块再没有"会卡住的模态框"】
        #   必须在 `FileTransferApp(root)` **之前** —— 构造函数里就有框（状态库那条路）。
        #   见 `_install_headless_dialogs` 的 docstring：这里换的是**名字**，
        #   44 个调用点一个都不动，以后新加的也自动被挡。
        #   ⚠️ 它挡的是**三扇门**（messagebox / simpledialog / filedialog），
        #   不只是 messagebox 一扇 —— 名字原来叫 `_install_headless_messagebox`，
        #   改名就是因为那个名字会让人以为另外两扇已经有人管了。
        app_bootstrapper._install_headless_dialogs()
        # 【第 6 步·雷⑨ 第 6 次复发】装闸函数搬进 `app_bootstrapper` 之后，
        #   它改绑的是**它自己那个模块**的 `messagebox`（`app_bootstrapper.messagebox`）。
        #   而宿主这边**还有 3 处** `messagebox` 用法（`_on_close` / `_HostHooks.on_path_problem`
        #   / 上面那道配置错误检查）⇒ 宿主命名空间**必须一起关**，否则无头模式下
        #   那三处会弹**真**模态框 ⇒ 没人点 ⇒ 进程占着任务锁不放
        #   ⇒「从某天起再也不传文件了」（本项目最坏的一种失败形态）。
        #   6b7 会从树里枚举"引用过 messagebox 的生产模块"，本模块自动进名单。
        messagebox = app_bootstrapper.messagebox
        sys.exit(app_bootstrapper._run_headless(_headless_name, sys.modules[__name__]))

    root = tk.Tk()
    app = FileTransferApp(root)
    root.mainloop()