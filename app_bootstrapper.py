# -*- coding: utf-8 -*-
r"""应用装配器（`AppBootstrapper`）—— 从 `FileTransferApp` 里切出来的第 7 个真模块（**第 6 步**）。

【它管什么】**"怎么把这个程序装起来、怎么无人值守地跑、上次的状态怎么回填"** 三件事：
  · 装配序列：建 `_db` / `_core` / `_maint` / `_ftp` / `_sched` / `_form` / `_ui` ＋ 4 处
    `attach_*` 补挂 ＋ `root.after` 排枪（**顺序就是语义**，一个字不许换）；
  · 无头族：三扇门（`messagebox` / `simpledialog` / `filedialog`）的替身与装闸、
    `_headless_wait_window`、退出码推导、`_run_headless`；
  · UX 三件（`_collect_ux_state` / `_restore_ux_state` / `_apply_ux_state_dict`）与恢复族
    （`_prompt_resume_task` / `_fill_ui_from_task`）；`_ensure_ui_theme`。

【层级】**L7（最顶层）** —— 只 import 它**下面**的层，**从不** import `file_transfer`
  ⇒ 它是全项目**唯一一个不产生反向边的模块**（`file_transfer.__main__` 反向 import 它）。
  ⚠️ 正因如此，宿主里那 6 个句柄类（`_HostFormSink` / `_HostSchedSink` / `_HostHooks` /
  `_HostLauncher` / `_UiStatsView` / `_HostLiveView`）、`_launch_argv_prefix` /
  `_is_unc_path` / `_LOCAL_LIKE_SRC` / `_HEADLESS_TASK`、以及入口类 `FileTransferApp`
  **只能由宿主在构造时注入**（`host_ns=`），不能 import。
  注入面有**白名单**（脚本 `NS_WHITELIST`），机械可审。

【为什么 `__init__` 要一整块搬过来，而不是"挑几行"】
  `__init__` 里的语句**互相有顺序依赖**（最典型：`_sched` 必须先于 `_ui` —— 后者的
  handlers 表构造期就要取 `self._sched._sched_check_done`）。第 5b 步就是在这条上撞了真红
  （静态自检全过、只有 p1 真差分抓到）。**逐行搬、顺序一个不换**，是本模块的第一纪律。

【`self.` 的两义（读本文件时最容易看错的一点）】
  被搬来的 14 个方法**仍然是本类的方法**，所以：
    · `self.<被搬来的方法>` ⇒ 指向 `AppBootstrapper` 自己（**原样保留**）；
    · `self.<其它>` ⇒ `self._app.<其它>`（宿主属性）。
  例：`_headless_bail` 里 `self._headless_write_trace(...)` 是**本类自己的**方法，
  而 `self._app._headless_deferred_bail` 是**宿主**的属性。
"""
import datetime
import os
import sys
import threading
import time

import tkinter as tk
from tkinter import BooleanVar, IntVar, StringVar
from tkinter import filedialog, messagebox, simpledialog

import ttkbootstrap

import config
import ftp_transfer
import maintenance_worker
import sched_tab
import state_db
import transfer_core
import transfer_form
import ui_channel
import ui_dialogs


# 无头模式"开局等待"的上限（毫秒）。超过它还没进入传输态 ⇒ 判"起不来"退出码 4。
# 【为什么需要它】GUI 模式下"卡住了"用户看得见，可以直接关掉窗口；
#   无头模式卡住是**完全静默**的 —— 进程留在后台、任务锁被它占着不放，
#   而下一次到点还会再起一个。有界地失败掉，比无限期挂着强。
#   60 秒这个数：正常路径下从 `mainloop` 到 `_task_active=True` 是毫秒级的
#   （建窗口 + 开库 + claim_task），留三个数量级的余量。
_HEADLESS_START_TIMEOUT_MS = 60000
# 【无头模式"本想弹、但没弹"的那些框】`(kind, title, message)` 三元组，按发生顺序。
#   两个用处：验收脚本据此断言"该弹的弹了、不该弹的没弹"；
#   以及万一某条路径在 sink 还没开时就记了一笔，它至少还在内存里。
_HEADLESS_BOX_LOG = []
# 由 `FileTransferApp._enter_headless` 挂上 `self._ui._log_sink_write`。
# 为什么是一个函数指针而不是"让 shim 去拿 app"：shim 是模块级的，
# 它必须在**任何 app 存在之前**就能工作（构造期也会有框）。
_HEADLESS_BOX_WRITE = None
# 【第四扇门的原件】装闸时把原生的 `tkinter.Misc.wait_window` 存这里。
#   **生产代码一个字都不读它** —— 它只给验收探针做阳性对照用：
#   探针要能证明"没装闸时这个门真的是开着的"，才谈得上"装上了"是有意义的。
#   为 None 表示**还没装过闸**（GUI 模式下就一直是这样）。
_ORIG_WAIT_WINDOW = None


r"""三扇门的无头替身（`messagebox` / `simpledialog` / `filedialog`）。

【为什么它们是类而不是函数】被装到模块全局名上以后，调用点写的是
  `messagebox.askyesno(...)` / `simpledialog.askstring(...)` ⇒ 拿到的必须是一个
  **有那些方法**的对象。三个类各自 `__getattr__` 兜底，把"本该弹但没弹"记进同一本账
  （`_HEADLESS_BOX_LOG`），并返回**最保守**的那个答案（是/否一律取"否"）。

【为什么整块搬进本模块】它们与"装闸"（`_install_headless_dialogs`）、"记账出口"
  （`_headless_emit`）是同一件事的三面，分居两个模块只会让"账本在哪"变成第二个事实来源。
  ⚠️ 它们体内的 `self` 是**自己**（不是宿主）⇒ 搬家时**逐字不动**。
"""
class _HeadlessMessageBox(object):
    r"""无头模式下 `messagebox` 的替身：**把"本该弹出的框"变成日志里的一行**。

    【为什么是"换掉模块里那个名字"，而不是改那 44 个调用点】
      全项目 44 处 `messagebox.xxx`。逐个加 `if _HEADLESS_TASK:` 有四个问题：
        ① 44 次改动 = 44 次改动错的机会，而每一处的正确写法都一样；
        ② 后加的第 45 个调用点**必然忘记加**这个守卫，而且症状是"定时任务卡住"
           —— 最难查的那种（它看起来像网络慢）；
        ③ 有些点是**间接**到达的：`_check_local_path` 里的 3 个框，
           从 `_launch_source` 那条路走上来，改 `_launch_source` 根本挡不住；
        ④ 终态那 5 个框是在 `_drain_ui_queue` 里被调的（主线程消息泵），
           它们前面没有任何"无头判断"的落脚点。
      换成替身之后，**"无头模式下不许有模态框"这件事变成结构性的**：
      不管代码怎么写、以后加多少处，到这个对象上就只会记一行。
      这与 `_LOCAL_LIKE_SRC`、`_apply_ux_state_dict` 是同一种处理 ——
      把一条规矩交给人去遵守，不如把它变成绕不过去的结构。

    【返回值取"最保守的那一个"】`askyesno` 一律返回 False（= 不做那件事）。
      理由：无头模式下没有任何人表达过"同意"，而"默认同意"会让一次无人值守的运行
      **替用户做决定**。本项目在别处的同类取舍永远是"宁可少做，不可静默多做"
      （`_should_skip` 拿不到可信源状态时一律不跳过，也是一样的方向）。
      `show*` 系列本来就没有返回值，给 None。
    """
    _SAFE_FALSE = ("askyesno", "askokcancel", "askretrycancel", "askquestion")
    _SAFE_NONE = ("askstring", "askinteger", "askfloat")

    def _emit(self, kind, args, kwargs, ret):
        # 转调模块级那本共用的账（见 `_headless_emit`）。保留这个方法本身，
        # 是因为 `_sched_tab_inner.YesBox` 继承了本类并按自己的答案改写它。
        return _headless_emit(kind, args, kwargs, ret)

    def showinfo(self, *a, **k):
        return self._emit("showinfo", a, k, None)

    def showwarning(self, *a, **k):
        return self._emit("showwarning", a, k, None)

    def showerror(self, *a, **k):
        return self._emit("showerror", a, k, None)

    def askyesno(self, *a, **k):
        return self._emit("askyesno", a, k, False)

    def askokcancel(self, *a, **k):
        return self._emit("askokcancel", a, k, False)

    def askretrycancel(self, *a, **k):
        return self._emit("askretrycancel", a, k, False)

    def askquestion(self, *a, **k):
        return self._emit("askquestion", a, k, "no")

    def askstring(self, *a, **k):
        return self._emit("askstring", a, k, None)

    def askinteger(self, *a, **k):
        return self._emit("askinteger", a, k, None)

    def askfloat(self, *a, **k):
        return self._emit("askfloat", a, k, None)

class _HeadlessSimpleDialog(object):
    r"""无头模式下 `simpledialog` 的替身：**一样是不弹、只记账**。

    【为什么它和 messagebox 是同一件事，不是两件事】
      `simpledialog` 的三条路（`askstring` / `askinteger` / `askfloat`）最后都落到
      `Dialog.__init__` 结尾那句 `wait_window` —— 和 `messagebox` 一样，
      **它们的终止条件是人**。无头下没有人 ⇒ 进程就停在那儿不动了。
      而这个进程正握着 `state_db` 的任务锁 ⇒ 锁一直不放 ⇒
      **从那以后每一次定时运行都以「让路」退出**。
      用户看到的是"任务从某天起再也不传文件了，界面上也没报错"。

    【上一次为什么漏了】原先只有 `_HeadlessMessageBox` 一个替身。
      它**长得像**已经把这件事办完了 —— 名字叫 messagebox，可它里面
      连 `askstring` 都有。于是一眼扫过去像是"对话框都挡住了"，
      而真正的 `simpledialog.askstring`（`ft:2691/2965/2993` 三处）
      一次都没被挡住。**"覆盖错觉"比明摆着的缺口更危险。**
    """

    def askstring(self, *a, **k):
        return _headless_emit("askstring", a, k, None)

    def askinteger(self, *a, **k):
        return _headless_emit("askinteger", a, k, None)

    def askfloat(self, *a, **k):
        return _headless_emit("askfloat", a, k, None)

class _HeadlessFileDialog(object):
    r"""无头模式下 `filedialog` 的替身。**第三扇门。**

    【返回值为什么是 `""` 而不是 `None`】`filedialog` 那几个函数在
      **用户按取消**时返回的就是 `""`（复数那个是 `()`）。照抄这个值，
      调用方会走它**本来就写好的那条"用户取消了"分支** ——
      不需要在 44 个调用点上新增分支，也就不会有人漏加、不会有人加错。
      这与 `_HeadlessMessageBox` 里"取最保守的那个答案"是同一条规矩：
      没人点过"确定"，所以一律按"没选"算。

    【为什么不是 `None`】`:2850` 那类调用点写的是 `for f in paths:`。
      给它 `None` 会在**无头下**炸出一个 TypeError —— 那是把"挂死"
      换成"崩溃"，并没有变好；而 `()` 能正常走完"一个都没选"的分支。
      取值要按**调用方的形状**给，不是按"空值哪个更眼熟"给。
    """

    def askopenfilename(self, *a, **k):
        return _headless_emit("askopenfilename", a, k, "")

    def asksaveasfilename(self, *a, **k):
        return _headless_emit("asksaveasfilename", a, k, "")

    def askdirectory(self, *a, **k):
        return _headless_emit("askdirectory", a, k, "")

    def askopenfilenames(self, *a, **k):
        return _headless_emit("askopenfilenames", a, k, ())

    def askopenfile(self, *a, **k):
        return _headless_emit("askopenfile", a, k, None)

    def askopenfiles(self, *a, **k):
        return _headless_emit("askopenfiles", a, k, None)

    def asksaveasfile(self, *a, **k):
        return _headless_emit("asksaveasfile", a, k, None)


def _headless_emit(kind, args, kwargs, ret):
    """无头替身的**唯一记账出口**。三扇门（`messagebox` / `simpledialog` /
    `filedialog`）共用这一本账、这一句日志。

    抽成模块级函数而不是留在 `_HeadlessMessageBox._emit` 里，是因为后来加的两扇门
    不是它的子类 —— 它们要的是**同一本账**，不是同一条继承链。
    `_HeadlessMessageBox._emit` 仍然保留（`_sched_tab_inner.YesBox` 继承它并改写
    过），只是改成转调这里。
    """
    title = args[0] if len(args) > 0 else kwargs.get("title", "")
    message = args[1] if len(args) > 1 else kwargs.get("message", "")
    _HEADLESS_BOX_LOG.append((kind, str(title), str(message)))
    w = _HEADLESS_BOX_WRITE
    if w is not None:
        try:
            # 用「本该弹的框」这个说法，是因为**它确实没弹** —— 写"已弹出"是假消息。
            w("[无头·本该弹的框] (%s) %s —— %s"
              % (kind, title, " ".join(str(message).split())))
        except Exception:
            pass
    return ret

def _headless_wait_window(self, window=None):
    r"""无头模式下 `tkinter.Misc.wait_window` 的替身 —— **第四扇门**。

    【为什么前三扇门的办法够不着它】前三扇是"换掉**模块里那个名字**"
      （`messagebox` / `simpledialog` / `filedialog`）。而 `wait_window`
      **不是模块里的名字，是类上的方法**（`tkinter.Misc`）——
      换模块那一招对它是**无效**的。所以它一直是一扇**没装闸的门**：
      前三扇挡住之后，它成了唯一一条还能把无头进程**永久挂住**的路。
      机理与前三扇完全相同：**终止条件是人**。

    【返回值为什么是 `None`】实测扫过全部 8 个调用点
      （`file_transfer.py` 7 个 + `ui_dialogs.py:438` 1 个），
      用**两条独立判据**（AST 父节点是不是 `ast.Expr` / 源码行左边有没有 `=`·`return`·`if`）
      逐行核对、结论一致：**8 个点全部丢弃返回值，一个都不接**。
      而真的 `wait_window` 返回的本来就是 `None` —— 照抄它，形状上不可能对不上。
      ⇒ 用户交代的那句"用得着返回值的单独处理"，**实测下来是零个**。

    【返回之后为什么不炸】——**这才是真正的风险点**：把"挂死"换成"崩溃"并没有变好，
      而且崩溃比挂死更难查（挂死至少还看得见进程在那儿）。
      8 个点里有 6 个在 `wait_window` 之后**立刻读 `dlg.result`**。
      实查三个对话框类，`__init__` **都先给了初值**：
        `ui_dialogs.py:80` `FTPSettingsDialog` · `:205` `ScheduleDialog`
        · `:361` `FTPBrowserDialog`
      ⇒ 替身直接返回时 `dlg.result is None` ⇒ 调用方走它**本来就写好**的
      「用户取消了」分支。**与真按取消同形**，调用点上不需要新增任何分支。
      （这一条是**查出来的**，不是推出来的：要是那三个类没给初值，
      这里就得改成"替身返回前先替它们赋 `None`"，否则就是 AttributeError。）

    【为什么装在 `Misc` 上，而不是改那 8 个调用点】调用点的主语是 `self.root`
      （一个 `Tk`）和 `self`，共同基类正是 `Misc` —— 装在基类上才是
      **总闸装在咽喉上**，而不是"我算过这 8 条路今天不通"。
      同理：**第 9 个调用点明天冒出来，不需要谁记得回来补闸。**
      这与前三扇门那条教训是同一条（见 `_install_headless_dialogs` 的「覆盖错觉」段）。

    【对 GUI 模式零影响】它只在 `_install_headless_dialogs()` 里被装上去，
      而那个函数只在无头分支里被调用 —— GUI 模式下 `Misc.wait_window`
      仍是原生那一个，一个字都没动。
    """
    # 记进同一本账（`_headless_emit` 的唯一出口）。标题取对话框自己的标题，
    # 让日志里那一行能直接指出"本该开的是哪个框"。
    try:
        label = window.title() if window is not None else "（无参）"
    except Exception:                       # 窗口已销毁 / 不是 Toplevel
        label = repr(window)
    return _headless_emit("wait_window", (label,), {}, None)

def _install_headless_dialogs():
    """把**四扇**「模态门」换成替身。**只在无头模式下调，且只调一次。**

    【三扇门，不是一个】会"停下来等人"的 stdlib 模块是一**组**，共同点是
      **终止条件是人**：`messagebox`、`simpledialog`（三条路最后都落到
      `Dialog.__init__` 的 `wait_window`）、`filedialog`（原生
      `tk_getOpenFile` / `tk_getSaveFile` / `tk_chooseDirectory`）。

      这道闸原先只挡了第一扇。而它**长得像**已经把三扇都挡了 ——
      名字叫 messagebox，里面却连 `askstring` 都有。真实的
      `simpledialog.askstring`（`ft:2691/2965/2993`）一次都没被挡住。
      **"覆盖错觉"比明摆着的缺口更危险**：缺口至少会让人去找，
      错觉会让人放心。所以本函数现在按"门"来装，不按"某个模块"来装。

    【为什么是**两个**模块，不是一个】`ui_dialogs.py` 里写的是
    `from tkinter import ttk, messagebox` —— 那会在**它自己的命名空间里**
    再绑一个名字。换掉 `file_transfer.messagebox` 对它是**没有任何影响**的。
    所以本函数要把两边都换掉。

    【为什么必须换 ui_dialogs 那一个 —— 哪怕今天走不到】
      走到一个**真的**模态框的后果，在无头模式下不是"多弹一个框"，是
      **整个任务永远不结束**：对话框在等一个没有人会点的按钮。而这个进程
      正握着 `state_db` 的任务锁 —— 它不结束，锁就一直不放，
      **从那以后每一次定时运行都会以「让路」退出**。用户看到的现象是
      "任务从某天起再也不传文件了，界面上也没报错"。
      这是本项目最坏的一种失败形态，所以这里的取舍不是"现在走不走得到"，
      而是"**要不要靠一条会过期的可达性判断去挡它**"。
      总闸要装在咽喉上，不要装在"我算过这条路今天不通"上。

    【对 GUI 模式零影响】本函数只在无头分支里被调用（见 `__main__`），
      GUI 模式下它一次都不执行 —— 所以"改另一个模块"不会波及正常界面。
    """
    global messagebox, simpledialog, filedialog, _ORIG_WAIT_WINDOW
    if not isinstance(messagebox, _HeadlessMessageBox):
        messagebox = _HeadlessMessageBox()
    if not isinstance(simpledialog, _HeadlessSimpleDialog):
        simpledialog = _HeadlessSimpleDialog()
    if not isinstance(filedialog, _HeadlessFileDialog):
        filedialog = _HeadlessFileDialog()
    # 【这一路不能把无头启动搞挂】它是无人值守那条路的入口，
    #   而这里做的是"降低风险"的加固 —— 加固本身失败绝不该让它起不来。
    #   所以整段包在 try 里：没 import 上、模块被换成别的样子、都只是少一层保护。
    try:
        if not isinstance(ui_dialogs.messagebox, _HeadlessMessageBox):
            ui_dialogs.messagebox = messagebox
        # 【第 4 步·雷⑨】`maintenance_worker` 里也有 3 处弹框（清空状态库的二次确认）——
        #   不同样改绑，无头模式下会弹**真**模态框 ⇒ 没人点 ⇒ 进程不放 `state_db` 任务锁
        #   ⇒ 「从某天起再也不传文件了」（这段注释上面的最坏形态）。
        #   与上一句同样用 `hasattr` 守卫：不去凭空造一个它本来没有的属性。
        if hasattr(maintenance_worker, "messagebox") and not isinstance(
                maintenance_worker.messagebox, _HeadlessMessageBox):
            maintenance_worker.messagebox = messagebox
        # 【第 5 步(5a)·雷⑨ 同型】**三扇门一个都不能少** —— 本步把 5 处 `messagebox`
        #   与 `simpledialog` / `filedialog` 的调用一起搬进了 `transfer_form.py`。
        #   ⚠️ 首版**只改绑了 messagebox** ⇒ `_probe_headless_modal_doors` 当场抓出来：
        #   `filedialog.askopenfilenames` 在新模块里还是**真**门（返回 `()` 而不是中性值）
        #   —— 无头模式下那就是「原生对话框等人点」= 本项目最坏失败形态。
        #   门是**一组**（共同点＝终止条件是人），所以按门下刀，不按模块下刀。
        #   6b7 已同步扩成**三扇门**逐一枚举（少改绑一扇就停手）。
        if hasattr(transfer_form, "messagebox") and not isinstance(
                transfer_form.messagebox, _HeadlessMessageBox):
            transfer_form.messagebox = messagebox
        if hasattr(transfer_form, "simpledialog") and not isinstance(
                transfer_form.simpledialog, _HeadlessSimpleDialog):
            transfer_form.simpledialog = simpledialog
        if hasattr(transfer_form, "filedialog") and not isinstance(
                transfer_form.filedialog, _HeadlessFileDialog):
            transfer_form.filedialog = filedialog
        # 【第 5 步(5b)·雷⑨ 同型】`sched_tab` 里有 6 处 `messagebox`（`_sched_apply` /
        #   `_sched_delete` / `_sched_edit` / `_sched_new` / `_sched_register_all` /
        #   `_sched_copy_cmd`），搬走后不一起改绑 ⇒ 无头模式下弹**真**模态框
        #   ⇒ 没人点 ⇒ 进程不放任务锁 ⇒「从某天起再也不传文件了」（最坏形态）。
        #   ⚠️ 它**不用** `simpledialog` / `filedialog`（L6 里一处都没有）⇒ 只改绑这一扇。
        #   6b7 的机械判据会从树里枚举"引用过 messagebox 的生产模块"，本模块自动进名单。
        if hasattr(sched_tab, "messagebox") and not isinstance(
                sched_tab.messagebox, _HeadlessMessageBox):
            sched_tab.messagebox = messagebox
        # 【第 6 步·雷⑨ 第 7 次变体 —— 从"既有待办"翻成"必须修"（2026-10-14 用户裁定 A）】
        #   `transfer_core` 与 `ui_channel` **各有一份自己的门**
        #   （`transfer_core.py:29` `import tkinter.messagebox as messagebox`；
        #    `ui_channel.py:24,25` 同样自己 import 了 `filedialog` / `messagebox`）
        #   —— 而它们**从来没进过这份名单**。
        #   `74 §8.9` 第 12 项把这条缺口登记为「**既有待办**」，6b7 每轮机械枚举后
        #   把它**打印**出来，但**只打印、不断言** ⇒ 四轮（step3 / step4 / 5a / 5b）
        #   都被按「环境红（与 ref 对称、比值 1.000）」放过。
        #   🔴 2026-10-14 定性翻转（见 `_checks\_nightly\94` §五）：faulthandler 抓到实锤 ——
        #   无头跑完 → `mainloop` → `_drain_ui_queue` → `_dispatch_ui_message`
        #   → `_dispatch_task_end` → `_transfer_done_stream` → **真** `messagebox.showinfo`
        #   ⇒ 没人点 ⇒ 进程永不结束 ⇒ 不放 `state_db` 任务锁 ⇒ **本项目最坏失败形态**。
        #   【它绝不是"今天走不到"】`transfer_core` 有 26 个真弹框点，终态那一族
        #   （`1994` / `2003` / `2052` / `2059` / `2068` / `2075` / `2081`，
        #    重传侧 `2249`-`2280`）**跑完任一轮都会走到**。
        #   实测：补上下面这两处即把 `_verify_headless` B 组从 **17 条 FAIL 打到 0**。
        if hasattr(transfer_core, "messagebox") and not isinstance(
                transfer_core.messagebox, _HeadlessMessageBox):
            transfer_core.messagebox = messagebox
        if hasattr(ui_channel, "messagebox") and not isinstance(
                ui_channel.messagebox, _HeadlessMessageBox):
            ui_channel.messagebox = messagebox
        # 【门是**一组**，不是"某人记得写下的那几个名字"】`ui_channel` 还**自己 import 了**
        #   `filedialog`（`24` 行；两处真调用点 `183` `askdirectory` / `200`
        #   `asksaveasfilename`，「导出日志」族）。它在无头路径上**今天确实走不到**
        #   （要按按钮），但按本函数自己反复写下的那条规矩 ——
        #   **总闸装在咽喉上，不装在"我算过这条路今天不通"上**（见上文「覆盖错觉」段：
        #   缺口至少会让人去找，错觉会让人放心）—— 一并改绑。
        #   `transfer_core` 这三扇门里**只用 `messagebox`**（`simpledialog` / `filedialog`
        #   各 0 处）⇒ 它只绑一扇，不是漏绑。
        #   【谁防止这条名单再漏】`_probe_headless_modal_doors.py` 的 **②b 层**：
        #   从树里机械枚举"引用过某扇门的每个生产模块"，逐个断言"它自己那份名字已是替身"，
        #   **gap 非空 ⇒ 停手**（只打印不断言 = 事实上静默，那正是这四轮的教训）。
        if hasattr(ui_channel, "filedialog") and not isinstance(
                ui_channel.filedialog, _HeadlessFileDialog):
            ui_channel.filedialog = filedialog
        # `ui_dialogs` 今天只 `from tkinter import ttk, messagebox` —— 后两扇门
        #   在那边**还没有名字**。这两句现在什么也不做，但它们是**为将来装的**：
        #   哪一天有人在 ui_dialogs 里写一句 `from tkinter import filedialog`，
        #   那边就已经是替身了，不需要谁记得回来补这一刀。
        #   这正是"总闸装在咽喉上"——不靠"我算过这条路今天不通"。
        #   用 `hasattr` 挡住，是为了不去**凭空造**一个它本来没有的属性
        #   （造出来的是个假的进口，除了骗过检查没有任何用处）。
        if hasattr(ui_dialogs, "simpledialog") and not isinstance(
                ui_dialogs.simpledialog, _HeadlessSimpleDialog):
            ui_dialogs.simpledialog = simpledialog
        if hasattr(ui_dialogs, "filedialog") and not isinstance(
                ui_dialogs.filedialog, _HeadlessFileDialog):
            ui_dialogs.filedialog = filedialog
    except Exception:
        pass
    # 【第四扇门】`tkinter.Misc.wait_window` —— 它**不是模块里的名字，是类上的方法**，
    #   所以上面那一套"换模块里的名字"够不着它（见 `_headless_wait_window` 的 docstring）。
    #   装的是 `Misc` 这个共同基类，不是那 8 个调用点：**咽喉**，不是"我算过的那些路"。
    #   同样包在 try 里 —— 加固本身失败绝不该让无头启动起不来。
    #   【为什么要留一份原件】不是给生产用的，是给探针做**阳性对照**用的：
    #   `_probe_headless_modal_doors.py` 要能证明"没装闸时这个门真的是开着的"。
    try:
        if tk.Misc.wait_window is not _headless_wait_window:
            _ORIG_WAIT_WINDOW = tk.Misc.wait_window
            tk.Misc.wait_window = _headless_wait_window
    except Exception:
        pass

def _headless_code_note(code):
    """退出码 → 一句人话。**只在这一处写** —— 日志里那句、`config.py` 那五档常量、
    以及将来界面上的显示，念的必须是同一句话。"""
    return {
        config.SCHEDULE_EXIT_OK: "成功（没有失败）",
        config.SCHEDULE_EXIT_PARTIAL: "部分失败（有成功也有失败）",
        config.SCHEDULE_EXIT_LOCKED: "让路：另一个任务正在跑，本次没有执行",
        config.SCHEDULE_EXIT_FAILED: "完全失败（一个都没成功）",
        config.SCHEDULE_EXIT_CANNOT_START: "起不来（配置/路径/状态库不可用）",
    }.get(code, "未知退出码 %s" % code)

def _headless_exit_code(copied, skipped, failed, stopped):
    """按 `22_plan_stage4.md` §⑥ 那张表算退出码。

    【判据是"文件"不是"任务"】一个跑了 500 个文件、失败 3 个的任务，
    在"任务级"看是失败的，在"文件级"看是成功的 —— 而用户关心的永远是后者：
    "我的数据过去了没有"。所以分档吃的是计数。

    ⚠️ **`stopped`（= 中止）一律不算成功**，哪怕它是"复制完 499 个之后用户点了停止"。
    这一条是硬要求：`status='aborted'` 可能已经有 49 个文件成功了，
    按计数它会被算成"部分失败"（1）甚至"成功"（0），而"被中止"与"跑完了"
    是**两件不同的事** —— 后者可以不管，前者需要人去看一眼为什么停了。
    """
    if stopped:
        return config.SCHEDULE_EXIT_PARTIAL if copied > 0 else config.SCHEDULE_EXIT_FAILED
    if failed == 0:
        return config.SCHEDULE_EXIT_OK
    return config.SCHEDULE_EXIT_PARTIAL if copied > 0 else config.SCHEDULE_EXIT_FAILED

def _headless_status(code, aborted):
    """退出码 → 写回配置的 `last_status`（`config.SCHEDULE_STATUSES` 里的一个）。

    【`aborted` 为什么不并进 `failed`】见 `config.SCHEDULE_STATUSES` 那里的注释：
    它可能已经成功了一大半，显示成"失败"与显示成"成功"一样是假消息。
    """
    if aborted:
        return "aborted"
    return {
        config.SCHEDULE_EXIT_OK: "ok",
        config.SCHEDULE_EXIT_PARTIAL: "partial",
        config.SCHEDULE_EXIT_LOCKED: "skipped",
        config.SCHEDULE_EXIT_FAILED: "failed",
        config.SCHEDULE_EXIT_CANNOT_START: "error",
    }.get(code, "error")

def _ensure_ui_theme(tk_root):
    """确保 **`tk_root` 所在的那个 Tcl 解释器** 上有一套 ttkbootstrap 主题。

    【为什么不能简单写一句 `ttkbootstrap.Style(theme=...)`】
      它有两处与"一个进程里只有一个窗口"这个假设绑定，而这个项目**不满足**那个假设：

      ① `Style` 是**进程级单例**（`Style.instance`）。第二次调用 `Style(...)` 时
         `__init__` 会**直接 return**（只顺带 `theme_use` 一下），不会重新绑定 master。
      ② 它的 `__init__` 走 `super().__init__()` → stdlib `ttk.Style` 的
         `setup_master(None)` → **`tkinter._default_root`**，也就是"本进程建的**第一个** Tk"。
         构造时**没有** master 参数可以传（签名就是 `(self, theme=...)`）。

      单看这两条没毛病 —— 因为样式库看起来像全局的。**它不是**：
      `tkinter.Tk()` 每调一次就新建一个 Tcl 解释器，而 **ttk 的样式库是每个解释器一份**。
      于是第二个之后的 Tk 拿到的是一个**绑在别人身上**的 Style，后果按严重程度分两级：
        * 轻：控件退化成没皮肤的裸 ttk（**不报错** —— 测试里量出来的像素全是错的，
              而且看起来"只是不好看"，没有任何红灯）；
        * 重：主题要生成图片资源时（`scale_size()` 会去问 master 要 `tk scaling`）
              若那个 master 已经被 destroy，直接 `TclError: application has been destroyed`。
      这两条**在这个项目的测试里都是现实**：每个套件进程都会建好几个 Tk
      （`auto_test_stage2a.py` 一个文件里就有 4 处 `tk.Tk()`），谁也不是"唯一"。

    【做法】认解释器：对不上就把单例丢掉重建，并在重建期间临时把 `_default_root`
      指向本 root，好让那段**没有 master 参数**的 `super().__init__()` 落到正确的
      解释器上。用完立刻还原 —— 这个全局变量别的地方还要用。

    【为什么不用 `ttkbootstrap.Window`】它默认 `hdpi=True`，会按 DPI 缩放尺寸，
      等于给刚量好的那个尺寸盒子又加了一个变量。这里是 `tk.Tk()` +
      **只挂主题**，几何一律不许被主题碰。
    """
    cur = getattr(ttkbootstrap.Style, "instance", None)
    if cur is not None and getattr(cur, "_c_bound_tkapp", None) is tk_root.tk:
        return cur                       # 同一个解释器且已经挂过 —— 什么都不用做
    ttkbootstrap.Style.instance = None   # 换解释器（或第一次）：让单例重新生成
    prev = tk._default_root
    tk._default_root = tk_root
    try:
        st = ttkbootstrap.Style(theme=config.C_UI_THEME)
    finally:
        tk._default_root = prev
    st._c_bound_tkapp = tk_root.tk       # 认领标记，给上面那句复用判断用
    return st


class AppBootstrapper:
    r"""应用装配器 —— 语义与纪律见模块 docstring（**第 6 步**从 `FileTransferApp` 抽出）。

    持有两样：
      · `self._app`     = 宿主 `FileTransferApp` 实例（**不是** import —— 构造时注入）；
      · `self._host_ns` = 宿主**模块对象**（构造时注入，只读白名单面）。
    本类**不持有** `root`：它只在 `__init__` 构造期用到（建窗口 / 挂主题），
      之后所有 `self.root` 读的都是 `self._app.root`（同一个对象）。

    ⚠️ 类体内的装配序列**逐行搬自** `FileTransferApp.__init__`：顺序**就是**语义，
      一个字不许换（第 5b 步真红就是这个形态）。`self.` 的两义见模块 docstring。
    """

    def __init__(self, app, root, host_ns):
        # 【第 6 步】本类**不持有** `root`：它只在构造期用（建窗口 / 挂主题），
        #   之后所有 `self.root` 读的都是 `self._app.root`（同一个对象）。
        self._app = app
        # `host_ns` = 宿主**模块对象**（**不是** import —— 见模块 docstring 的
        #   "唯一不产生反向边"）。本模块只读它的**白名单**面（6 个句柄类 ＋ 3 个引导工具
        #   ＋ `_HEADLESS_TASK` ＋ `FileTransferApp`）；脚本 `NS_WHITELIST` 机械可审。
        self._host_ns = host_ns
        # ⚠️ 下面这一段是**逐行搬来的装配序列**：顺序**就是**语义，一个字不许换。
        #   最典型的一条：`_sched` 必须先于 `_ui`（后者的 handlers 表构造期就要取
        #   `self._sched._sched_check_done`）—— 第 5b 步就是在这条上撞的真红。
        # 【阶段2d-新增】本次启动时刻（秒）。恢复弹窗用它做判定基准：
        # 心跳**早于**此刻的 running 行 = 上次异常退出留下的；心跳晚于此刻的
        # = 另一个窗口正在传，那不是"待恢复的任务"，不能提示、更不能放弃它。
        # 必须在任何可能读库的动作之前取，所以放在 __init__ 第一行。
        self._app._boot_time = int(time.time())
        self._app.root = root

        # ---- 阶段四：无头模式的两个部分（实例状态 + 日志文件 sink） ----
        # 【第 1 步-搬走】`_log_sink` / `_log_sink_lock` / `_log_sink_path` 已归
        #   `ui_channel.UIChannel`（日志资源本来就属日志模块）。原先那句"必须摆在
        #   `__init__` 最前面"的理由随属性一起搬走：现在是 `_ui` 在构造时就位，
        #   宿主侧读它走 `self._ui._close_log_sink()` / `self._ui._log_sink_path_value()`，
        #   "定义早于使用点"这条约束仍然成立 —— 靠的是下面 `self._ui = ui_channel...`
        #   的位置（`__init__` 里没有任何一处会在它之前走无头收口，已实测）。
        # 【"这个进程是不是无头"由模块级 `_HEADLESS_TASK` 说了算，这三个是"跑成什么样"】
        # 默认值刻意取"最失败的那一档"：万一有哪条路径没走到收口函数，
        # 进程退出码也会是 4（起不来），**不会**因为"没来得及赋值"而被当成成功（0）。
        # 静默的成功是这套东西里最坏的结果 —— 它会让一个根本没跑的备份看起来跑过了。
        self._app._headless = False
        self._app._headless_name = self._host_ns._HEADLESS_TASK or ""
        self._app._headless_code = config.SCHEDULE_EXIT_CANNOT_START
        # 【"收过口没有"】两个收口函数（`_headless_bail` / `_headless_finish`）
        # 共用这一个旗标，语义是"终局已定，后面来的都不算"。
        # 为什么必须有它，见 `_headless_bail` 的注释（`root.quit()` 不中断调用栈）。
        self._app._headless_closed = False
        # 【"攒着，等日志文件开了再收口"】`__init__` 里有一部分失败发生在
        # **日志 sink 还没打开之前**（它要等 `_enter_headless`，那在构造之后）。
        # 在那里直接 bail 的话，这次失败**一个字节都落不到文件里** ——
        # 而这恰恰是最需要留下痕迹的一次（连启动都没启动起来）。
        # 所以攒在这里，由 `_headless_begin` 进门第一件事就处理。
        self._app._headless_deferred_bail = None

        # 【功能C1】主题必须在**建任何控件之前**挂上：ttk 的样式是建控件时查表的，
        # 之后再换主题，已经建好的控件不会自己回头重画（得手动 `_retheme`）。
        # 放在 `title` 之前无所谓先后，放在任何 `_build_ui` 之前**有所谓**。
        _ensure_ui_theme(root)
        self._app.root.title("文件传输工具 v%s" % config.APP_VERSION)
        # 【功能A/B-修 UI 缺陷】680x520 → 880x620 →（功能C1）880x640 →（C1 裁定）880x660
        #   →（D-1 步 5）**880x690**（这一笔的完整理由见下面 ③）。
        # **每次只改了这两个数字**，控件结构、位置、顺序、名称一个字符没动，
        # `resizable(False, False)` 保持（尺寸固定是设计选择，不是这次的议题）。
        #
        # 【为什么原来那个尺寸是**缺陷**，不是"紧凑的设计"】
        #   `main` 里的内容**一直就放不下**：实测功能A 之前需要 567px、窗口只给 480px。
        #   而 14 行 grid 里**只有第 12 行（日志区）带 weight=1** ——
        #   缺口 100% 由它承担，于是 `Text(height=8)`（声明 108px）实测只有 **61px ≈ 3.5 行**。
        #   那句 `height=8` **从来没有兑现过**，只是没人量过。
        #   功能A 又往下面加了一行（导出日志按钮，36px），把它压到 **25px ≈ 1.4 行**。
        #   日志区是这个工具的主要反馈面，"导出日志"导出的又正是它当前的内容 ——
        #   1.4 行是功能性的损坏，不是审美问题。
        #
        # 【新尺寸是怎么量出来的 —— 不是拍的，也不是"看着差不多"】
        #   高度：实测日志区高 = 窗口高 − 495（在 520/560/580/600/620/640 六点上严格线性）。
        #         要 ≥ 声明值 108px（`Text(height=8)`）→ 需 ≥ 603。
        #         取 **620** → 日志区 **125px**，比声明值多 17px 余量。
        #         （用户给的上限是 640，640 会给到 145px，但没必要占那块屏幕。）
        #         ⚠️ 上面这三个数是**功能A/B 当时**的实测值，功能C1 换肤后已作废 ——
        #            现在的实测值见本段末尾【功能C1】那一块，不要拿这行当现行基线。
        #   宽度：`main` 自己要 621px —— 注意这是**日志区那个 Text 的默认 80 字符宽**顶出来的，
        #         不是某一行的内容撑的，所以它是个"地板"，不会被别的行盖过。
        #         功能B 的速度/ETA 文字**实测最宽 188px**（"1023.9 MB/s · 剩余 23 小时 59 分"，
        #         字体取界面上真正在用的 Microsoft YaHei UI 9），加 pack 的 padx 约 198px。
        #         要让**新标签进来之后仍然谁都不用被压缩** → 621 + 198 + 40(padx) = 859。
        #         取 **880** → 内容区 840px，比需要多 21px；
        #         且进度条在挂上标签之后仍有约 **426px**（现在没有标签时是 424px）——
        #         即"加了功能B 的文字，进度条一点没变窄"。
        #
        # 【为什么把速度文字放进第 10 行，而不是另起一行】
        #   另起一行要占 ~30px 高度 → 窗口得 650，**超过用户给的 640 上限**；
        #   而插在中间的行会把 11/12/13 行号整体下移，那才真叫"改布局"。
        #   放进第 10 行（进度条+按钮那一行）高度不变、行号不变。
        #
        # 【功能C1：高度 620 → 640（**到达用户给的上限，没有超**）】
        #   换肤把各行的实际高度改了（ttk 控件的 padding 与 tk 原生不同），
        #   实测 `main.winfo_reqheight()`：**603 → 639**，即缺口从 +17 变成 **−19**。
        #   缺口照旧 100% 由第 12 行（日志区）承担 —— 日志区 125 → 131px。
        #   ⚠️ **131px 本身是达标的**（C1 验收写的是"日志区高度仍 ≥ 100px"），
        #      所以"能不能不改尺寸"这个问题，**用户已经预先答过了：能。**
        #      真正的问题在别处：`_verify_feature_b.py` 的 L8 是
        #      `main.winfo_reqheight() <= root.winfo_height()`，639 > 620 会让它**红**。
        #      而 L8 这条判据（"内容放得下"）**不能为了迁就主题去放宽** ——
        #      这个项目一路在防的就是"把红的判据改绿"。所以正确的动作是**修缺陷本身**，
        #      即把窗口加高到装得下，而不是把 L8 改成 `<= avail + 19`。
        #
        #   ① 当时为什么先取 **640**：
        #     640 是**当时**的"用户明确给过的上限"（见上面那段"窗口得 650，超过用户给的 640 上限"），
        #     实测 640 恰好够：`main` 要 639、窗口给 640 —— **余量只有 1px**。
        #     ⚠️ 那 1px 当时被如实标成"唯一一处不满意但没得选的地方"，并写进简报请用户裁定。
        #
        #   ② 【C1 裁定，2026-09-19：**窗口改为 880x660，上限抬到 680**】
        #     **1px 余量等于 0 余量** —— 任何字体/主题微调都会立刻吃掉日志区，
        #     而"下一次微调"在这个项目里已经发生过四次（T4）。用户的裁定原话：
        #       "加高 20px 用户感知不到，但工程差距巨大"；
        #       "我之前给的『600~620 上限』是拍脑袋的，**实测 640 是下界**"。
        #     → **新上限 680，窗口 880x660**，余量 **660 − 639 = 21px**（与功能A 当初同级）。
        #     ⚠️ 本函数仍只被授权在**用户给的上限之内**修尺寸缺陷 ——
        #        660 是用户**明确指定**的值，不是我自己挑的；下次要再动，先问。
        #
        #   顺带兑现了一件事：日志区的 `Text` 在 C1 之后字体变成了
        #     `TkDefaultFont`（雅黑 9，行高 17）—— ttkbootstrap 的 autostyle 干的，
        #     原来是 Tk 给 Text 的默认字体 `TkFixedFont`（新宋体 10，行高 13）。
        #     所以 `height=8` 的声明值从 108px 涨到 **150px**；
        #     640 给到的正好是 **150px** —— 这句声明**第一次被整行兑现**。
        #
        #   ③ 【D-1，2026-09-19：**窗口 880x660 → 880x690，上限 680 → 710**】
        #     原因不是审美，是**源路径那一行必须变高**：`Entry`（单行）→ `tk.Listbox(height=3)`
        #     （多选源列表），实测 `main.winfo_reqheight()` **639 → 664**。
        #     660 会变成 **−4px 缺口**（`main` 装不下，照旧 100% 由日志区承担）——
        #     正是 C1 那次刚修掉的老毛病。故抬到 **690**，余量 **690 − 664 = 26px**。
        #     **红线上次怎么没被挪、这次也怎么没被挪**：改的仍是**常数**，
        #     `_verify_feature_b.py` 的 L8 不等式 `main.winfo_reqheight() <= root.winfo_height()`
        #     **一个字没动**，仍要**真正成立**（实测 664 ≤ 690）才绿 ——
        #     不是把判据放宽，是把缺陷本身修掉（与 C1 那次同一条规矩）。
        #     日志区实测 **176px ≥ 声明值 150px**，仍整行兑现。
        #     ⚠️ 尺寸改动**每次都要先问**（见 ②）—— 这一次是 D-1 方案 §2 里
        #     **明确写出来、并经用户裁定"开始执行 7 步"**的，不是我自行挑的数。
        # 【阶段二】880x690 → **888x717**。两个数都是 `_probe_stage2_fit.py` 量出来的，
        #   不是拍的（探针把"三种分组壳的净开销"分开量，并自证了可加性）：
        #     高：重排省下 100px（三个标题行被分区标题顶替 + 扁平化/线程数两行合并），
        #         5 个 Labelframe 每区 +19px = +95，Notebook 标签栏 +34 → 净 +27，
        #         690 → 717。
        #     宽：880 时进度条实测预测 349，**破 L4 下限 350**；宽 +8 后回到 357。
        #
        #   【复量结果（`_probe_stage2_v2.py`，真实布局，不是外推）】
        #     窗口 **888x717** ✓ 与预测逐字相同；进度条 **357** ✓ 与预测逐字相同；
        #     Tab1/Tab2 **无一行被裁**（两页的"分配 ≥ 声明"都成立）。
        #     ⚠️ **只有日志区与预测不符：实测 256，预测 176（+80）**。以复量为准。
        #        原因不是算错了壳开销，而是 v1 漏了一个事实：**这一版的窗口高不再由
        #        Tab1 决定**。实测两页的声明高是 Tab1 **537** / Tab2 **628** ——
        #        要装下设置页（四个分区、5 个 Spinbox、一个 Listbox），窗口必须给到
        #        628 以上；而 Tab1 只需要 537，多出来的 **106px 全部落进 Tab1 唯一带
        #        `weight=1` 的那一行**，也就是日志区（分区声明 208 → 分配 314）。
        #        所以 176 这个预测值从一开始就不可达：它是"按 Tab1 反推"算的，
        #        而 Tab1 已经不是那个说了算的页面了。
        #     ⇒ **717 保持不动**：它已经是**下限附近**（Tab2 分配 643、声明 628，
        #        只剩 15px 余量），再压就要裁设置页；而多出来的高度全部变成日志，
        #        是这个工具最不亏的去处（这一行本来就是"窗口给多少它吃多少"）。
        #        ⚠️ 反过来要小心的是**往上加**：下次谁再往 Tab2 里塞东西，
        #        撑高的是**整个窗口**（Tab1 的日志区不会再被压缩来腾地方 ——
        #        它已经吃满了余量，没有余量可让），届时日志区**不会**变矮报警，
        #        只会让窗口变高。判据在 `_probe_stage2_v2.py` 的"逐行 分配 vs 声明"。
        self._app.root.geometry("888x717")
        self._app.root.resizable(False, False)

        self._app.source_path = StringVar()
        self._app.dest_path = StringVar()
        self._app.source_type = StringVar(value="local")
        self._app.dest_type = StringVar(value="local")
        self._app.transfer_mode = StringVar(value="copy")
        self._app.file_filter = StringVar()
        self._app.flatten = BooleanVar(value=False)
        self._app.thread_count = IntVar(value=1)
        # 【功能 3-4】第二个目标（一次任务同时复制到两处）。
        #   刻意**不做成 `dest_type2` 菜单**：FTP 目标的第二份要另走一套上传池与
        #   重连重放，风险远大于收益，本版只支持"本地 → 本地 × 2"。
        #   勾选框是总闸；不勾时 `dest_path2` 里留着上次的路径也不会生效。
        self._app.multi_dest = BooleanVar(value=False)
        self._app.dest_path2 = StringVar()

        # ---- 阶段二：设置页（Tab2）的默认参数 ----
        # 【为什么线程数**没有**新变量】它就是上面那个 `thread_count`。
        #   Tab2 的 Spinbox 与 Tab1 的 Spinbox **绑同一个 IntVar**，Tk 自己保证两边同步。
        #   另起一个 `default_thread_count` 会造出第二份真相：用户在哪个页面改的、
        #   哪个生效、落盘存哪个 —— 三个问题都要回答，而回答错了就是"改了没反应"。
        #   现在只有一个数，不存在"以谁为准"。
        #
        # 下面四个是**真的新数据**：它们在阶段二之前是写死在代码里的量
        # （`FTPHelper(timeout=10)`、`_copy_with_retry(attempts=4)`、
        #   `FTP_RECONNECT_ATTEMPTS=4`），现在提到界面上来。
        # **口径统一为"重试 N 次"**，N=3 对应代码里那个 4（试 1 次 + 重试 3 次）。
        # 取值范围在 config 里（`*_RANGE`），不在这里写死 —— 校验逻辑要与结构白名单同层。
        self._app.ftp_timeout = IntVar(value=config.DEFAULT_TIMEOUT_S)
        self._app.retry_count = IntVar(value=config.DEFAULT_RETRY_COUNT)
        self._app.reconnect_count = IntVar(value=config.DEFAULT_RECONNECT_COUNT)
        self._app.log_level = StringVar(value=config.DEFAULT_LOG_LEVEL)
        self._app.db_keep_days = IntVar(value=config.DEFAULT_DB_KEEP_DAYS)
        self._app.log_export_dir = StringVar(value=config.DEFAULT_LOG_EXPORT_DIR)
        # 【清空状态库】的那个勾选框（默认**不勾**：overwrites 是数据事故的唯一线索）。
        self._app.purge_overwrites = BooleanVar(value=False)

        # ---- 阶段四·P0-1：日期分层增量 ----
        # 【为什么只有一个 Tk 变量、没有配套的「启用」勾选框】
        #   见 `config.parse_date_template` 的注释：**模板非空就是启用**。
        #   用户裁定 A-② 是"选填模板"、规则 1 是"默认关闭"，而"没填"与"填了又清掉"
        #   在这套语义下是同一件事 —— 多一个勾选框就要多定义两种组合
        #   （勾了没填 / 填了没勾）分别算什么，而那两种组合的答案都必须与"空"一致。
        # 【为什么用 StringVar 而不是像 `file_filter` 那样直接读控件】
        #   它有三个读者：落盘（`_collect_ux_state`）、任务参数（`_collect_task_params`）、
        #   以及下面那个同步点标签。三处都从这一个变量取，才有唯一一份真相。
        #   **默认空串 = 功能关闭** ⇒ 没填模板的用户与本次改动之前逐字节相同。
        self._app.date_template = StringVar(value="")
        # 【P0-1】本次源正在用的剪枝器（`config.DateLayerPruner`），空模板时恒为 None。
        #   由 `_launch_source` 建、由 `_dispatch_task_end` 收；**每个源各一个** ——
        #   多源串行时每个源的根不同，共用一个会把上一个源的同步点算到下一个头上。
        #   它同时是"本次有没有启用日期分层"的唯一判据：四处 `os.walk` 与 FTP 调用点
        #   都只看 `is not None`，**没有一处去读 `date_template`**（后台线程不能碰 Tk 变量）。
        # 本次源对应的 `meta` 键。**与剪枝器同生共死**：`_date_layer_begin` 里一起置、
        # `_date_layer_finish` 里一起用。不在收尾时重算一遍 —— 那要再读一次 Tk 变量，
        # 而收尾可能发生在"用户已经把源框改掉了"之后（终态是异步投递过来的）。
        # 同步点标签的防抖句柄。见 `_schedule_syncpoint_refresh`。
        self._app._sync_refresh_id = None
        # 【P1】本次源的**判重记录内存快照**：`{src_path: (六个字段的 tuple)}`。
        #   由 `_launch_source` 建（`_skip_states_begin`）、由 `_dispatch_task_end` 收；
        #   **每个源各一份** —— 按 `src_root` 取数，共用一个会把上一个源/别的源的记录
        #   拿来判重。None 是**合法且必须支持的常态**（回滚开关、库降级、读失败、
        #   重传路径）：那时 `_should_skip` 逐文件查库，行为与 P1 之前**逐字节相同**。
        #   ⚠️ 它会被**工作线程**读（FTP 下载的每个 worker 都要判重）⇒ 建成之后
        #   **只读、不再改**，线程安全由"不改"保证，不靠锁。释放只发生在主线程的
        #   终态路径上，而那一刻所有 worker 已经结束（终态是 db-writer 投递过来的）。
        # 上一次预取的条数（只用于日志/验收读，不参与判定）。

        # ---- 阶段二：多套 FTP 配置 ----
        # 【为什么是普通 list/dict，不是 Tk 变量】它们是一份**结构**（若干套配置），
        #   不是某个控件的值。Tk 变量只适合"一个控件绑一个值"，
        #   把一份嵌套结构塞进去，同步规则立刻变成一团。
        #   界面刷新走 `_refresh_profile_list()`，是**单向**的：数据变了就重画。
        # 【为什么与 ftp_source_config/ftp_dest_config 并存】那两个变量是
        #   "本次任务这一侧用哪一份"，语义**一个字不改**；这里存的是"有哪几套可选"。
        #   前者是后者的一个引用，不是同一件事的两种存法。
        self._app.ftp_profiles = []
        self._app.ftp_active = ""

        # 【D-1】多来源列表（纯 Python list，**不是 Tk 变量**）。
        #   与 `source_path` 的分工 —— 这是整个 D-a 最要紧的一条约定：
        #     * `source_path` = **当前源**。语义**一个字没改**，仍是"这次用哪个源"。
        #       （硬条件：`auto_test_stage2d.py` 拿它当 ux_state 的 payload 逐条断言往返，
        #         9 个测试文件直接 `app.source_path.set(...)`。）
        #     * `source_list` = **批**。≥2 项时才走串行驱动器；≤1 项时**仍以
        #       `source_path` 为准**，于是"单源"是"多源"的真子集，旧行为逐字保住。
        #   列表选中项会**写回** `source_path`（`_on_source_select`），
        #   所以界面上永远是"选中的那一项 = 当前源"，不存在两个互相矛盾的真相。
        self._app.source_list = []
        # 【本轮修复】"本轮日志开过没有"的**一次性旗标**，归零在 `_start_transfer`。
        #   为什么不能用 `_src_index <= 1` 表达这件事，见 `_begin_run_log` 的注释
        #   —— 那正是这次要修的缺陷本身。
        # 【本轮修复·第二处】本轮**真起跑过几个源**。用途见 `_start_next_source`
        #   里 `if self._src_index > total:` 那个分支的注释。
        # 【D-1 裁定①】整条序列的累计计数，只服务收尾那一行 `全部完成：…`。
        #   `_completed`/`_skipped` 是**每源重置**的，跨源累加必须另存一份。
        # 【D-1 裁定③-方案C】跨源覆盖日志的**去洪**计数（见 `_audit_overwrite`）。
        #   按**整条序列**计（日志跨源是同一份），所以归零在 `_start_transfer`、
        #   不在 `_launch_source` —— 放到后者会让每个源都重新逐条打 50 条。

        self._app._pause_event = threading.Event()
        self._app._pause_event.set()
        self._app._stop_event = threading.Event()
        self._app._lock = threading.Lock()
        # 【第 3 步-搬走】`_total` / `_progress_stride` 已归 `ftp_transfer.FtpTransfer`
        #   （都是 FTP 那一路的量：`_total` 由 ftp 的 worker 写、`_progress_stride`
        #   由 ftp 在 `_total` 定后写）。宿主不再初始化 ⇒ 读它一律走 `self._ftp._total`。
        # 【第 1 步-搬走】`_progress_timer_id` / `_progress_marquee` 已归 `_ui`
        #   （进度条是它的控件）。宿主侧改调 `self._ui._cancel_timers()` /
        #   `self._ui._reset_progress_bar()`。
        # 【阶段2d-新增】连续失败计数与显式中止原因（方案 Q8）。
        # 【第 1 步-搬走】速度 / ETA 的全部状态（含上面那段"为什么按字节算"的说明）
        #   已整块搬进 `ui_channel.UIChannel.__init__`。宿主侧改调
        #   `self._ui._note_bytes()` / `_refresh_speed()` / `_speed_reset()` /
        #   `_start_speed_poll()` / `_set_speed_final_text()` / `_note_task_start_time()`。

        # 【阶段一 Bug2/Bug3-新增】扫描期的两个"死活指示器"，都只服务于状态栏文案。
        #
        #   _scan_dirs      **单元素列表**（盒子），里面装着已列举完的**目录数**
        #                   （扫描线程写、主线程读，只增不减）。
        #                   为什么是盒子不是 int：计数器要**跨线程边扫边写**，而
        #                   config._walk_remote_files 是生成器 —— 它没法"返回"一个
        #                   还没算完的数，只能由调用方递一个可变容器进去让它自增。
        #                   归零必须 `[0] = 0` **原地改**，不能重新绑一个新列表：
        #                   生成器手里握着的是旧那个引用。
        #                   为什么需要它：文件流的长度是"找到多少个文件"，而空目录
        #                   一个文件都不产出 —— 12 层目录扫 20 分钟时，光看文件数
        #                   完全看不出"是在动还是卡死"。
        #   _transfer_started  本**源**是否已经有文件真的开始搬了。这是"速度该不该
        #                   上屏"的判据 —— 原先用的是跑马灯，而跑马灯要等扫描**结束**
        #                   才关，流式之后那等于把速度压制整整 10~30 分钟（Bug3）。
        #
        # 两者都由 worker/生产者线程写、主线程在 500ms 心跳里读，与 _total 同一套约定：
        # 不加锁（写的是 int/bool，GIL 下不会撕裂，最坏读到 500ms 前的值 ——
        # 对指示器完全够用）。按**源**归零，位置在两个 progress_marquee 投递点旁边。
        self._app._scan_dirs = [0]
        # 【2026-11-04】扫描期**已发现**的累计量：`[文件数, 字节数]`。
        #   与 `_scan_dirs` 同一套注入方式（宿主建、按引用传），扫描器原地改。
        #   `ui_channel` 拿它渲染「已发现 N 个 / X GB」，`ftp_transfer` 负责累加。
        self._app._scan_stat = [0, 0]
        # 【第 1 步-搬走】`_transfer_started` 已归 `_ui`（它是"速度该不该上屏"的判据，
        #   与速度同一族）。宿主侧改调 `self._ui._reset_transfer_started()`。
        #   `_scan_dirs` **留在宿主**：扫描器握着那个 list 对象，按引用注入给 `_ui`。

        # ---- 阶段1：跨线程 UI 通道 ----
        # 【第 1 步-搬走】`_ui_queue` / `_ui_draining` / `_log_buf` / `_log_lock` /
        #   `_log_last_flush` / `_log_line_count` / `_log_truncated` 已归 `_ui`
        #   （队列无界、重入保护、日志聚合与行数缓存的理由，都随代码搬进了
        #   `ui_channel.UIChannel.__init__` 的注释里）。
        #   `_task_active` **留在宿主**（它是任务状态，归 transfer_core）。

        # ---- 阶段2a：状态库 ----
        self._app._db = state_db.TransferStateDB(config.STATE_DB_FILE)
        # 【D-1 新增】本次任务的**源根**，与 _current_task_id 同时确立、同时失效。
        # 它现在是判重身份的一半（files 主键 = filename + src_root），所以不能每次
        # 现算 —— 判重发生在工作线程里，而 self.source_path 是 Tk 控件，跨线程读它
        # 是不允许的。由主线程在 _claim_current_task 里取好存下来，工作线程只读。
        # 初值空串：**空串不是合法源根**（_start_transfer 显式拒绝空源路径），
        # 所以"还没认领任务"时它查不到任何行 → 判重不生效，也就不会误跳过。
        # 【阶段二】本次任务用的三个"设置页参数"的**快照**。
        #   为什么不在 worker 里直接读 `self.ftp_timeout.get()`：Tk 变量**跨线程读是不允许的**
        #   （与 `_task_extensions` 同一条规矩，见 `_start_local_to_local` 里那句
        #   "Tk 变量一律在主线程读好再传给后台线程"）。
        #   为什么不在 `__init__` 里读一次就完事：用户可能在任务之间改设置，
        #   而"改了没生效"与"改了生效了但你没注意"从界面上分不出来。
        #   所以快照点是**每次任务开始**（`_start_transfer` / `_retry_failed`）。
        # 初值取常量：没跑过任务时它们就该等于"从没在界面上改过"。

        self._app.ftp_source_config = None
        self._app.ftp_dest_config = None

        # ---- 【第 2 步】传输内核（`transfer_core.TransferCore`）----
        # 【构造点为什么在 `_ui` **之前**，而不是 `_build_ui()` 之后】两条硬约束把它夹在这儿：
        #   ① 下面 `_ui` 的 `handlers=` 里有 5 条指向本类**已搬走**的方法
        #      （done_stream / done / retry_done / error / task_end / terminal_fallback）⇒
        #      它们要知道"这些方法现在在哪"，所以内核必须先存在；
        #   ② `_build_ui()` → `_build_transfer_tab()` 里那 4 个按钮的 `command=` 也指向
        #      内核方法（`_start_transfer` / `_retry_failed` 等，在 `_build_transfer_tab` 里
        #      就绑定成 bound method）⇒ 内核必须早于 `_build_ui()`。
        #   ⇒ 于是"27 个注入对象全部已存在"那条原计划不成立：`_ui`（下一行）与 4 个按钮
        #      此刻还不存在。这 5 个改由 `attach_host_widgets()` 在 `_build_ui()` 之后补齐
        #      —— **逐个断言、只许挂一次**，fail-fast 不丢。
        # 【为什么 `ftp_*_config` / `source_list` 走 `live=` 视图、不按值传】
        #   这三个都是宿主**会在构造之后重新赋值**的属性（UI 保存 FTP 配置、
        #   `_apply_ux_state_dict` 恢复上次状态 / 套用任务参数、测试直接 `app.X = ...`）。
        #   按值传 = 内核只拿到构造那一刻的快照（初值 `None` / 空列表），宿主之后怎么改
        #   内核都看不见 —— 第 2 步实测到的两个真回归就是这个形态。视图只回指宿主、
        #   每次现读，所以宿主怎么改都可见（同 `_UiStatsView` 的模式）。
        _live = self._host_ns._HostLiveView(self._app)
        self._app._core = transfer_core.TransferCore(
            hooks=self._host_ns._HostHooks(self._app),
            launcher=self._host_ns._HostLauncher(self._app),
            live=_live,
            headless_task=self._host_ns._HEADLESS_TASK,
            local_like_src=self._host_ns._LOCAL_LIKE_SRC,
            is_unc_path=self._host_ns._is_unc_path,
            db=self._app._db,
            lock=self._app._lock,
            pause_event=self._app._pause_event,
            stop_event=self._app._stop_event,
            date_template=self._app.date_template,
            dest_path=self._app.dest_path,
            dest_path2=self._app.dest_path2,
            dest_type=self._app.dest_type,
            multi_dest=self._app.multi_dest,
            file_filter=self._app.file_filter,
            flatten=self._app.flatten,
            ftp_timeout=self._app.ftp_timeout,
            reconnect_count=self._app.reconnect_count,
            retry_count=self._app.retry_count,
            source_path=self._app.source_path,
            source_type=self._app.source_type,
            thread_count=self._app.thread_count,
            transfer_mode=self._app.transfer_mode)

        # 【第 1 步-搬走】UI 通道成了独立对象。构造点选在 `_build_ui()` **之前**：
        #   `_ui` 的构造器不碰控件（控件由 `_build_transfer_tab` 在建造过程中
        #   `attach_widgets()` 挂进来），但它必须先于 `_build_ui` 存在，理由与
        #   原先那句"`_ui_handlers` 必须在 `_build_ui()` 之后构建"是同一件事的两半 ——
        #   现在是"控件挂载早于消息表使用"，由挂载点保证。
        #   `handlers` 里那 8 条里有 1 条是 `terminal_fallback`：
        #   **ui → core 那条反向边的唯一出口**（ui_channel 不 import transfer_core）。
        # 【第 3 步】FTP 层。位置**必须在 `_core` 之后**（本类方法要 `self._core.X`）、
        #   **在 `_ui` 之前**（它要与 core 一样在 `_build_ui()` 之后补挂 `_ui`）。
        #   `live` **复用同一个对象** —— `ftp_source_config` / `ftp_dest_config` 会被
        #   `_fill_ui_from_task` / `_apply_active_profile` 重写 ⇒ 必须现读（第 2 步的教训，`63`）。
        # 【第 4 步】后台三件套（L4）。位置**在 `_core` 之后**（本类方法要读 `self._core.X`）、
        #   **在 `_ftp` 之前**（ftp 的构造参数 `on_part_reclaim` 就指向本类的
        #   `start_part_reclaim` —— 顺序反了会拿到 None）。
        #   三个 Tk 变量走**惰性取值器**（不是值）：读它们的次序在守卫之后，收"值"会改行为。
        self._app._maint = maintenance_worker.MaintenanceWorker(
            core=self._app._core,
            db=self._app._db,
            stop_event=self._app._stop_event,
            keep_days_of=lambda: int(self._app.db_keep_days.get()),
            purge_overwrites_of=lambda: bool(self._app.purge_overwrites.get()),
            date_template_of=lambda: self._app.date_template.get(),
        )
        # 【第 4 步·临时胶水】(a) 一行别名：让 `self.root.after(1000, self._start_db_maintenance)`
        #   那一行**逐字不变**（⇒ 锚点表一个字不用改）。**有界方法，不是 lambda** ——
        #   包一层 lambda 会把"读 Tk"提前到守卫之前（违约）。
        #   ⚠️ 新模块里那 8 个方法**一律沿用原方法名（带下划线）**，不改成公开名 ——
        #   所以这里必须是 `_start_db_maintenance`，不是 `start_db_maintenance`。
        #   📌 待办：**第 6 步拆 `app_bootstrapper` 时消掉这一行**（那时 `__init__` 直接持 `_maint`）。
        self._app._ftp = ftp_transfer.FtpTransfer(
            core=self._app._core,
            live=_live,
            lock=self._app._lock,
            pause_event=self._app._pause_event,
            stop_event=self._app._stop_event,
            scan_dirs=self._app._scan_dirs,
            scan_stat=self._app._scan_stat,
            source_path=self._app.source_path,
            dest_path=self._app.dest_path,
            flatten=self._app.flatten,
            thread_count=self._app.thread_count,
            on_part_reclaim=self._app._maint._start_part_reclaim,
        )
        # 【第 5 步(5b)】定时任务页（L6）。位置**必须在 `self._ui = UIChannel(...)` 之前** ——
        #   因为 `_ui` 的 handlers 表里有一项是 `"sched_check": self._sched._sched_check_done`
        #   （构造时就要取到这个有界方法）。而本类要用 `_ui` ⇒ **循环依赖** ⇒ `_ui`
        #   **不注入**，等下面 `_ui` 建好之后由 `attach_ui()` 补挂（既有 4 处同一套）。
        #   ⚠️ 这是**真缺陷逼出来的顺序**：首版把 `_sched` 建在 `_build_ui()` 之前、
        #      `_ui` 之后 ⇒ 构造期当场 `AttributeError: 'FileTransferApp' object has no
        #      attribute '_sched'`（被 p1 真差分抓到，回归里也会红）。
        #   它要的 `root` / `_core` / `_launch_argv_prefix` / `_HEADLESS_TASK` 到这里都已就绪。
        #   ⚠️ `_launch_argv_prefix` **故意不搬**（雷A）：它体内是 `os.path.abspath(__file__)`，
        #      留在宿主 ⇒ `__file__` 天然还是 `file_transfer.py`（计划任务才不会去跑新模块）。
        #      `__file__` 是"**本程序**怎么启动"的宿主知识 ⇒ 只把 callable 注入进去。
        #   `host=` 给的是**可写句柄**（白名单视图），不是宿主对象本身。
        self._app._sched = sched_tab.SchedTab(
            host=self._host_ns._HostSchedSink(self._app),
            root=self._app.root,
            core=self._app._core,
            launch_prefix=self._host_ns._launch_argv_prefix,
            headless_task=self._host_ns._HEADLESS_TASK,
        )
        # 【第 5 步(5b)·临时胶水】(a) 一行别名：让 `__init__` 里那两个
        #   `self.root.after(1100/1200, self._sched_*)` 启动点**逐字不变**
        #   （⇒ 锚点表一个字不用改）。与第 4 步 `_start_db_maintenance` 同一手法。
        #   ⚠️ `:1489` 的 `"sched_check"` 字典条目**不走别名** —— 它不是"启动点"，
        #      直接改文本更诚实（用户 2026-10-13 裁定②）。
        #   📌 待办：**第 6 步拆 `app_bootstrapper` 时消掉这两行**。
        self._app._ui = ui_channel.UIChannel(
            core=self._host_ns._UiStatsView(self._app),
            root=self._app.root,
            pause_event=self._app._pause_event,
            log_level=self._app.log_level,
            log_export_dir=self._app.log_export_dir,
            scan_dirs=self._app._scan_dirs,
            scan_stat=self._app._scan_stat,
            handlers={
                "done_stream": self._app._core._transfer_done_stream,
                "done": self._app._core._transfer_done,
                "retry_done": self._app._core._retry_failed_done,
                "error": self._app._core._transfer_error,
                "sched_check": self._app._sched._sched_check_done,
                # 【批 1 修复】「清空状态库」的回执。`maintenance_worker._purge_worker`
                #   把结果投成 `_post_ui("purge_done", rep)`；这里不登记 ⇒ 消息被
                #   分发端当"未知类型"丢掉（见 `137_立项_清空状态库回执丢失.md`）。
                "purge_done": self._app._maint._purge_done,
                "task_end": self._app._core._dispatch_task_end,
                "title": self._app._set_title,
                "terminal_fallback": self._app._core._transfer_finished,
            })
        # 【第 5 步(5a)】界面表单（L5）。位置**必须在 `self._build_ui()` 之前** ——
        #   下面 `_build_ui()` 里的两处调用已经改成 `self._form.…`。
        #   它要的 `root` / `_ui` / `_core` / `_maint` / `_entry_style` 到这里都已就绪。
        #   `host=` 给的是**可写句柄**（白名单视图），不是宿主对象本身。
        self._app._form = transfer_form.TransferForm(
            host=self._host_ns._HostFormSink(self._app),
            root=self._app.root,
            ui=self._app._ui,
            core=self._app._core,
            maint=self._app._maint,
            entry_style=self._app._entry_style,
            is_unc_path=self._host_ns._is_unc_path,
        )
        self._app._build_ui()
        # 【第 2 步】补挂那 5 个"构造时还不存在"的对象（`_ui` 与 4 个按钮）。
        #   点必须在这儿：`_ui` 在它上面建、4 个按钮在 `_build_transfer_tab` 里建
        #   （在 `_build_ui()` 之内），而 `_build_ui()` 一返回它们就全都存在了。
        #   这两行之间**没有任何内核方法被调用**（内核在构造期一次都没被调用过）。
        self._app._core.attach_host_widgets(
            ui=self._app._ui, start_btn=self._app.start_btn, pause_btn=self._app.pause_btn,
            stop_btn=self._app.stop_btn, retry_btn=self._app.retry_btn)
        # 【第 3 步】FTP 层同样要补挂 `_ui`（它的 handlers 表指向本类搬走的方法）。
        self._app._ftp.attach_ui(self._app._ui)
        # 【第 4 步】后台三件套同样要补挂 `_ui` 与两个控件
        #   （它的 handlers/控件都指向宿主搬走的方法）。
        #   入口名**唯一**（`attach`，不是 core/ftp 那两个名字）—— 理由见 `MaintenanceWorker.attach`。
        self._app._maint.attach(self._app._ui, self._app.purge_btn, self._app.retry_btn)
        # 【第 5 步(5b)】定时任务页补挂 `_ui`（构造那一刻它还不存在 ——
        #   循环依赖见 `__init__` 里 `self._sched = ...` 那一段注释）。
        #   与 `_core.attach_host_widgets` / `_ftp.attach_ui` / `_maint.attach`
        #   同一套：**只许挂一次**。
        self._app._sched.attach_channel(self._app._ui)

        self._app.root.protocol("WM_DELETE_WINDOW", self._app._on_close)

        # 【第 1 步-搬走】这张消息表已归 `_ui`（`_ui_handlers`），
        #   在它构造时建立：本类自己的 5 条 + 上面 `handlers=` 传进去的 **9 条**
        #   （批 1 之前是 8 条；新增的是 `"purge_done"`）。
        #   原先"必须在 _build_ui() 之后构建，因为 _update_log 依赖 log_text"
        #   这条约束，现在由 `attach_widgets()` 的挂载时机保证。

        # 【阶段2a】建表在主线程完成，早于任何 db-writer 线程；失败则整个功能降级而非崩溃。
        if config._STATE_DB_ENABLED:
            try:
                # 【任务1】返回值 = 迁移 v2→v3 时被丢弃的重复行条数（见 state_db 决策③）。
                # 只在**真的丢了**的时候出声：那意味着这些文件下次会重传一遍，
                # 用户看到"怎么又传了一遍"时得有个说法可查。为 0 时不打 ——
                # 每次启动都报一句"丢弃 0 条"是纯噪音。
                _dropped = self._app._db.ensure_schema()
                if _dropped:
                    self._app._ui._threadsafe_log(
                        f"[状态库] 升级到 schema v3：{_dropped} 条重复的历史续传记录被合并"
                        f"（同一路径只保留最近一条，这些文件下次会重新校验/重传）")
            except Exception as e:
                self._app._core._state_db_degraded = True
                # 【阶段四】无头模式下**不许弹框** —— 没有人会去点它，模态框会把
                #   这个进程永远挂在那儿，而且下一次到点还会再起一个。
                #   更要紧的是：无头模式是**无人值守的备份**，而"没建起状态库"
                #   意味着每一轮都要重传全部文件。把它降级成"能跑但慢"是**静默**的，
                #   用户永远不知道自己的定时备份其实一直在全量重传。
                #   所以这里不降级，直接判"起不来"（退出码 4）——
                #   与 `22_plan_stage4.md` §⑥ 那行"状态库不可用 → 4"逐字一致。
                if self._host_ns._HEADLESS_TASK is not None:
                    # ⚠️ 这里**不直接 bail**：日志 sink 还没打开（它在 `_enter_headless`
                    #   里开，而那要等构造完）。攒给 `_headless_begin` 去收 ——
                    #   见 `_headless_deferred_bail` 的定义处。
                    self._app._headless_deferred_bail = (
                        config.SCHEDULE_EXIT_CANNOT_START,
                        "状态库不可用，定时任务不跑（无头模式不做降级运行）：%s" % e)
                else:
                    messagebox.showwarning(
                        "状态库不可用",
                        f"无法建立传输状态库，本次运行将没有续传能力：\n{e}\n\n"
                        f"（库文件：{config.STATE_DB_FILE}）")

        if config._UI_QUEUE_ENABLED:
            self._app.root.after(config.UI_DRAIN_INTERVAL_MS, self._app._ui._drain_ui_queue)

        # 【功能A-新增】回填上次用的路径/模式/过滤/线程数。
        # 位置有三条约束，都不是随便放的：
        #   ① 必须在 `_build_ui()` 之后 —— 它写的是 Tk 变量；变量本身在更早处已建，
        #      但 `_update_log` 依赖 `self._ui.log_text`，回填要往日志区写一行提示；
        #   ② 必须在 `_ui_handlers` 建好之后 —— 提示走的是消息通道；
        #   ③ 必须在 drain 链启动之后 —— 排在它前面的话，那行提示要等到 100ms 后
        #      才被 drain 出去，而 200ms 时恢复弹窗就会覆盖日志区，提示可能一闪而过。
        # 与阶段2d 的恢复弹窗（200ms，会弹框问"要不要继续上次的任务"）**不冲突**：
        # 那个更具体，它回填的是一整份任务参数，晚于本方法执行，覆盖它是正确行为。
        # 【阶段四】无头模式下**跳过**：定时任务的参数来自 `--run-task` 指定的那条记录，
        #   不是"上次用的"。先回填一份上次的、再被任务参数覆盖，除了多写一行
        #   「已回填上次使用的路径」进日志文件之外没有任何效果 —— 而那一行是**假消息**
        #   （文件里显示的路径根本不是这次用的）。所以干脆不回填。
        if self._host_ns._HEADLESS_TASK is None:
            self._restore_ux_state()

        # 【阶段2d-新增】恢复上次任务的询问（方案 §7）。
        # 必须走 root.after，**不能**在这里同步弹：__init__ 跑完之前主窗口一帧都没画，
        # 此时弹模态框，用户看到的是"程序启动后卡住不动"，而不是"程序在问我要不要恢复"。
        # 【阶段四】无头模式下**压根不排这一枪** —— 它是个 `askyesno` 模态框，
        #   弹出来就没人点。见 `_prompt_resume_task` 开头那道守卫（两处都要有：
        #   这里不排是"不去弹"，那里守卫是"万一别处调它也不弹"）。
        if self._host_ns._HEADLESS_TASK is None:
            self._app.root.after(200, self._prompt_resume_task)

        # 【阶段5-4-新增】状态库的长期维护（Q1 第 4 项）。
        # 排在恢复弹窗**之后**（200ms → 1000ms）是有意的：维护对用户毫无交互，
        # 而恢复弹窗在等用户点「继续/放弃」。先弹窗、后维护，用户的感觉是
        # "程序立刻就在问我"，而不是"卡了一下才问我"。
        # 延迟本身就是"让界面先画出来"的那一道，维护线程再慢也压不住第一帧。
        #
        # 【阶段四】无头模式下**不做库维护**。理由是它自己那条硬前提：
        #   `state_db.py` 里写着"调用方负责保证**此刻没有任务在跑**"——
        #   而这一枪排在启动后 1 秒，定时任务此刻多半**正在传**。
        #   改到"任务跑完再做"则会把退出码的产出路径拉长（等维护跑完才退出），
        #   而库维护是**纯优化**（清陈旧行、回收 .part），跳过它没有任何副作用：
        #   下一轮 GUI 启动时照样会做。
        if self._host_ns._HEADLESS_TASK is None:
            self._app.root.after(1000, self._app._maint._start_db_maintenance)

        # 【阶段四】定时任务的启动自检，两件事，都在 `after` 里排队：
        #
        #   ① 把"上次定时任务跑得怎么样"打进日志区（`22_plan_stage4.md` §1②c）。
        #      它是**没有邮件渠道时唯一主动的一条** —— 用户只要打开工具就会看到，
        #      不需要他去翻 `~\.ftp_transfer_logs\` 下面那些文件。
        #   ② 后台查一遍注册状态，工具挪过位置就在顶部挂提示。
        #
        #   两件都必须**排在 `_restore_ux_state` 之后**：那一步会往日志区写一行
        #   「已回填上次使用的路径」，而这里这几行说的是"上次跑得怎么样"，
        #   顺序反过来读起来像是回填失败。
        if self._host_ns._HEADLESS_TASK is None:
            self._app.root.after(1100, self._app._sched._sched_startup_report)
            self._app.root.after(1200, self._app._sched._sched_check_startup)

    def _prompt_resume_task(self):
        """启动时若发现上次异常退出留下的 running 任务，询问是否恢复。

        三条判定/行为约束（方案 §7）：
          1) 只认 **heartbeat_at 早于本次启动时刻** 的 running 行。心跳新鲜的
             running 行说明另一个窗口正在传（Q6 的任务级锁允许窗口多开），
             那不是"上次的任务"，提示"放弃"会直接把别人正在跑的任务标成 aborted；
          2) [继续] 只**回填参数、不自动开始**（Q7）——几十万文件的传输不该在
             用户没点按钮时启动；
          3) [放弃] 写 status='aborted'，让任务级锁立刻释放，不必等 30 秒心跳过期。

        【阶段四】无头模式**直接返回**：下面那个 `askyesno` 没人点。
        `__init__` 也已经不排这一枪了 —— 两处都要有，理由不同：
        那边是"不去弹"，这里是"这个函数本身在无头下没有第二个说得通的语义"。
        无头模式撞上"上次没跑完"是**正常情况的一种**（上一次被停止/崩溃），
        处置已经由别处定义了：撞任务锁 → 退出码 2 让路（见 `_claim_current_task`）。
        在这里自作主张把上一次的任务标成 aborted 则是**替用户改状态**，
        而它连界面上那个"继续"的机会都没有 —— 越权。
        """
        if self._host_ns._HEADLESS_TASK is not None:
            return
        if not config._STATE_DB_ENABLED or self._app._core._state_db_degraded:
            return
        try:
            t = self._app._db.find_unfinished_task()
        except Exception as e:
            self._app._ui._threadsafe_log(f"[警告] 读取未完成任务失败：{e}")
            return
        if not t or t.get("heartbeat_at") is None or t["heartbeat_at"] >= self._app._boot_time:
            return

        detail = (
            "上次的任务没有正常结束：\n\n"
            f"  源：{t['src_type']}  {t['src_root']}\n"
            f"  目标：{t['dst_type']}  {t['dst_root']}\n"
            f"  模式：{'移动' if t['mode'] == 'move' else '复制'}\n"
            f"  开始于：{t['created_at']}\n\n"
            "选「是」：把上述参数填回界面，确认后自己点「开始传输」\n"
            "        （已传完的文件会被状态库判定为命中跳过，不会重传）\n"
            "选「否」：放弃这次任务，状态记为已中止")
        if messagebox.askyesno("恢复上次任务", detail):
            self._fill_ui_from_task(t)
            self._app._ui._threadsafe_log("已回填上次任务的参数，请确认后点击「开始传输」")
        else:
            try:
                # 用 abort_task 而不是 finish_task：此刻本进程还没起 db-writer，
                # 写连接 self._conn 是 None（见 abort_task 的说明）。
                self._app._db.abort_task(t["task_id"], "user_abort")
                self._app._ui._threadsafe_log(f"已放弃上次任务（task_id={t['task_id']}）")
            except Exception as e:
                self._app._ui._threadsafe_log(f"[警告] 放弃上次任务失败：{e}")

    # 【阶段2d-新增】[继续] 的回填实现。只动**参数**，不碰按钮状态、不启动线程。
    # FTP 任务的连接配置（含口令）不入库，回填不了：这里把地址和类型填上，
    # 用户点 ⚙ 重新确认一次连接信息即可——不这么做的话，点「开始传输」会被
    # "请先设置 FTP 源"挡下，而用户完全不知道缺的是哪一步。
    def _fill_ui_from_task(self, t):
        self._app.source_type.set(t["src_type"] or "local")
        self._app.dest_type.set(t["dst_type"] or "local")
        self._app.source_path.set(t["src_root"] or "")
        self._app.dest_path.set(t["dst_root"] or "")
        if t["mode"] in ("copy", "move"):
            self._app.transfer_mode.set(t["mode"])
        # claim_task 存的是 ",".join(sorted(extensions))，这里原样还原成过滤器输入框
        self._app.file_filter.set(t["extensions"] or "")
        self._app.flatten.set(bool(t["flatten"]))
        if t["src_type"] == "ftp":
            self._app.ftp_source_config = None
            self._app._ui._threadsafe_log("[提示] FTP 源连接信息不入库，请点「⚙」重新填写后再开始")
        if t["dst_type"] == "ftp":
            self._app.ftp_dest_config = None
            self._app._ui._threadsafe_log("[提示] FTP 目标连接信息不入库，请点「⚙」重新填写后再开始")

    # 【只记本地路径，FTP 那一侧存空串 —— 这是本功能里唯一一处我自己加的判断】
    # 理由与阶段2d 的 _fill_ui_from_task 完全同源：**FTP 连接配置（含口令）不入盘**。
    # 而一个 `ftp://host/dir` 地址在重启之后**没有连接配置就是不可用的** ——
    # 回填它只会造出一个"界面上看着填好了、点开始却撞上『请先设置 FTP 源』"的状态。
    # 那种"填了但不能用"比"没填"更容易让人以为是 bug。
    # 代价说清楚：**常用 FTP 源的用户，这一侧永远记不住东西**（口令记不住，地址就无意义）。
    def _collect_ux_state(self):
        """把界面上"该记住"的几项收成一个 dict。键名与 config.UX_STATE_FILE 的落盘格式一致。"""
        # 【D-1】`source_list` 只收**本地路径**，与 `source_path` 同一条规矩（见上方长注释）：
        # FTP 源在重启后**没有连接配置就是不可用的**，存下来只会造出"填了但不能用"。
        # 列表里那几项 ftp:// **原地留着、不落盘** —— 本次运行照样能跑（串行驱动器
        # 读的是内存里的 list），只是重启后不回来，与 `source_path` 的取舍完全一致。
        local_sources = [s for s in self._app.source_list if not s.startswith("ftp://")]
        return {
            # 【阶段三】`"share"` 与 `"local"` 同一条规矩：UNC 路径**记得住**。
            #   理由与上面 FTP 那段正好相反：共享**没有**"重启后不可用"的问题 ——
            #   凭据是登录会话级的、由 Windows 维护，重启后照样连得上。
            #   把 `"share"` 漏在这里的后果是"源框重启后空着"，而列表里那项还在
            #   （`local_sources` 收的就是非 ftp 的全部），是个**看着像丢了**的假象。
            "source_path": (self._app.source_path.get().strip()
                            if self._app.source_type.get() in self._host_ns._LOCAL_LIKE_SRC else ""),
            "source_list": local_sources,
            "dest_path": (self._app.dest_path.get().strip()
                          if self._app.dest_type.get() == "local" else ""),
            # 【功能 3-4】第二个目标。**只有勾着才落盘**：没勾时存的路径没有意义，
            #   而"下次打开发现第二目标还勾着"比"要重新填一次"更容易出事故
            #   （多写一份到一块快满的盘上）。
            "multi_dest": bool(self._app.multi_dest.get()),
            "dest_path2": (self._app.dest_path2.get().strip()
                           if self._app.multi_dest.get() else ""),
            "transfer_mode": self._app.transfer_mode.get(),
            "file_filter": self._app.file_filter.get().strip(),
            "thread_count": int(self._app.thread_count.get()),
            "flatten": bool(self._app.flatten.get()),
            # 【阶段二-新增】设置页的那几项默认参数。**只加键，不改已有的键** ——
            # 旧版本读到这份文件时，多出来的键它不认识，`_restore_ux_state` 那边
            # 是"逐项挑自己认识的"，所以向后兼容是结构自带的，不需要版本号。
            "ftp_timeout": int(self._app.ftp_timeout.get()),
            "retry_count": int(self._app.retry_count.get()),
            "reconnect_count": int(self._app.reconnect_count.get()),
            "log_level": self._app.log_level.get(),
            "db_keep_days": int(self._app.db_keep_days.get()),
            "log_export_dir": self._app.log_export_dir.get().strip(),
            # 【P0-1】日期分层模板。存的是**模板串**（不是"启用与否"）——
            # `_apply_ux_state_dict` 那边认不认得这个键，取决于它过不过 `parse_date_template`，
            # 所以一个手改的 `{"date_template": 123}` 或者 `"YYYY/XX"` 都只回落到默认空串，
            # 不会让程序带着一个非法模板跑起来（那会让剪枝判据整段失效）。
            "date_template": self._app.date_template.get().strip(),
            # ⚠️ **`purge_overwrites` 故意不在这里**。它是一次性授权（"这次连覆盖记录
            #    一起清"），不是偏好。存下来会让"勾过一次"变成"以后每次都勾着"，
            #    而那等于把默认值悄悄从"不勾"改成了"勾"—— 默认不勾是数据安全优先的
            #    裁定，不能被一次误点改写。所以它每次启动都从 False 开始。
        }

    # 【白名单为什么在这里，而不在 config】
    #   那份 JSON 用户能**直接编辑**。`{"thread_count": 999999}` 或
    #   `{"source_path": 12345}` 不该能改程序行为，所以每一项都单独验类型。
    #   而"355 这个数字合不合法"是界面层才知道的事（Spinbox 是 1~16），
    #   塞进常量模块会让两边各存一份、迟早对不上。
    #
    # 【为什么逐项验、而不是整份验完再回填】
    #   一个坏字段只该让**那一项**回到默认，不该让整份状态作废 ——
    #   用户手改文件时最常见的形态就是改坏了一个值，其余几个没问题。
    #
    # 【为什么不弹框告知】
    #   文件读坏 / 少几个键都不是错误，是"没有历史"。弹框会把一件好事变成一次打扰。
    #   真的回填到了东西才在日志区写一行 —— 那条日志本身也是在告诉用户
    #   "路径是程序填的，不是你自己之前打的"。
    def _restore_ux_state(self):
        st = config.load_ux_state()
        if not st:
            return                      # 文件不存在 / 读坏 / 不是 dict —— 等于首次启动
        filled = self._apply_ux_state_dict(st)
        # 【日志级别要在回填**之后**才生效】上面那句"已回填上次使用的路径"也得按当前
        #   级别过滤 —— 所以这条日志放在最后，而且它本身就是"常规档该不该留"的判据之一
        #   （它不属于 ROUTINE_LOG_PREFIXES 那三类，两档下都会显示）。
        if filled:
            self._app._ui._threadsafe_log("[界面] 已回填上次使用的路径，可直接点「开始传输」")

    def _apply_ux_state_dict(self, st):
        r"""把一份"界面状态"dict 灌进 Tk 变量。返回真正套用的项数。

        【为什么从 `_restore_ux_state` 里抽出来 —— 这是阶段四的一个结构性要求】
          定时任务的参数（`--run-task` 那条记录里的 `params`）**与"上次使用的路径"
          是同一批键**，也必须过**同一份白名单**。另写一份的后果不是"重复"，
          是**漂移**：两份白名单里只要有一项验得不一样，症状就是
          "界面上能填、定时任务跑起来不生效"，而这条恰恰是无人值守时最难查的
          —— 没有人看着它跑。
          所以这里是**唯一**一份"哪些键能影响程序行为"的答案。
          `_HEADLESS_TASK` 与非无头走的是同一个函数，差别只在调用方给的那份 dict。

        【验类型的规矩一条没变】见下面每一处的注释：这个 dict 的来源
        （`~\.ftp_transfer_ux.json` / `~\.ftp_transfer_schedules.json`）
        **用户都能直接编辑**，所以每一项都单独验类型、一个坏字段只让那一项回默认。
        """
        filled = 0
        if isinstance(st.get("source_path"), str) and st["source_path"]:
            self._app.source_path.set(st["source_path"])
            filled += 1
        # 【D-1】来源列表。旧版没有这个键 → 什么都不做，列表保持空，
        # 于是"单源"路径照旧生效（§7.5 的向后兼容）。
        # **逐项验类型**，与上面每一项同一条规矩：那份 JSON 用户能直接编辑，
        # 一个 `{"source_list": [1, 2]}` 不该能让 `_start_next_source` 拿到非字符串，
        # 更不该让一个坏元素把整张列表作废 —— 所以是"只收 str、丢弃其余"，
        # 而不是"有坏元素就整份不要"。
        raw_list = st.get("source_list")
        if isinstance(raw_list, list):
            self._app.source_list = [s for s in raw_list
                                if isinstance(s, str) and s.strip()]
            if self._app.source_list:
                # 列表恢复了，就把选中项落到**当前 `source_path` 那一项**上；
                # 找不到（比如 source_path 被清空了）就落到第一项 ——
                # 界面上"选中的那一项 = 当前源"这条约定必须在上屏的第一帧就成立。
                sel = 0
                cur = self._app.source_path.get()
                if cur in self._app.source_list:
                    sel = self._app.source_list.index(cur)
                self._app._form._refresh_source_listbox(select=sel)
                filled += 1
        if isinstance(st.get("dest_path"), str) and st["dest_path"]:
            self._app.dest_path.set(st["dest_path"])
            filled += 1
        # 【功能 3-4】第二个目标。两项都验类型，且**先路径后开关**：
        #   开关打开却没有路径 = 一按开始就被校验挡下，属于"配了但跑不了"。
        #   反过来（有路径、开关关）是安全状态，所以只在前一项形状对时才认开关。
        if isinstance(st.get("dest_path2"), str):
            self._app.dest_path2.set(st["dest_path2"])
        if isinstance(st.get("multi_dest"), bool) and self._app.dest_path2.get().strip():
            self._app.multi_dest.set(st["multi_dest"])
        if st.get("transfer_mode") in ("copy", "move"):
            self._app.transfer_mode.set(st["transfer_mode"])
        if isinstance(st.get("file_filter"), str):
            self._app.file_filter.set(st["file_filter"])
        tc = st.get("thread_count")
        # `isinstance(True, int)` 为真，所以要显式把 bool 挡在外面，
        # 否则一个手改的 `{"thread_count": true}` 会被当成 1。
        if isinstance(tc, int) and not isinstance(tc, bool) and 1 <= tc <= 16:
            self._app.thread_count.set(tc)
        if isinstance(st.get("flatten"), bool):
            self._app.flatten.set(st["flatten"])

        # 【阶段二-新增】设置页那几项。**验类型的规矩与上面逐字相同**：
        #   * 整数项一律"是 int、不是 bool、落在界面控件的合法区间内"三条齐全才收。
        #     `isinstance(True, int)` 为真这条坑已经在 thread_count 那边踩过一次，
        #     这里每一项都照着写了一遍 —— 复制这段判断比"抽个公共函数"更不容易出错，
        #     因为它短、而且每一项的取值范围都不一样（抽出去反而要额外传上下界）。
        #   * 区间取的是**设置页 Spinbox 的同一个常量**（`config.*_RANGE`），
        #     不是重新写一份数字：两份数字迟早对不上，而对不上的症状是
        #     "手改成的 999 被回填进 Spinbox，控件显示 999 但一点就到不了"。
        for key, var, lo, hi in (
                ("ftp_timeout", self._app.ftp_timeout, config.TIMEOUT_RANGE_S[0], config.TIMEOUT_RANGE_S[1]),
                ("retry_count", self._app.retry_count, config.RETRY_COUNT_RANGE[0], config.RETRY_COUNT_RANGE[1]),
                ("reconnect_count", self._app.reconnect_count, config.RECONNECT_COUNT_RANGE[0],
                 config.RECONNECT_COUNT_RANGE[1]),
                ("db_keep_days", self._app.db_keep_days, config.DB_KEEP_DAYS_RANGE[0],
                 config.DB_KEEP_DAYS_RANGE[1])):
            v = st.get(key)
            if isinstance(v, int) and not isinstance(v, bool) and lo <= v <= hi:
                var.set(v)
        # 日志级别只认那两个常量值。**不认识的字符串一律忽略**（回落到默认"详细"）——
        # 反过来写（"不是 normal 就当 verbose"）会把一个手改的错值静默变成有效值。
        if st.get("log_level") in config.LOG_LEVELS:
            self._app.log_level.set(st["log_level"])
        if isinstance(st.get("log_export_dir"), str):
            self._app.log_export_dir.set(st["log_export_dir"])
        # 【P0-1】日期分层模板。**校验用的是判定时那个同一个函数**（`parse_date_template`）
        #   —— 不是"再写一份看着差不多的检查"。两份检查迟早对不上，而对不上的症状是
        #   "界面上填得进去、跑起来判据整段失效"，且**无人值守时没人看得见**。
        #   验不过就**一项都不改**（保留当前值，默认是空串 = 关闭）：这是规则 2
        #   "缺失/非法一律退回最安全的动作"在本层的落点，比"清空"更保守 ——
        #   清空会把用户手打了一半的模板也抹掉，而非法值只该让**这一项**不生效。
        _tpl = st.get("date_template")
        if isinstance(_tpl, str):
            _segs, _err = config.parse_date_template(_tpl)
            if _err is None:
                self._app.date_template.set(config.format_date_template(_segs))

        # 【`filled` 的口径一个字没改】它数的仍然只有"源/目标路径"那两项
        #   （设置页那几项从头到尾都没进过这个计数）—— 抽出本函数时**刻意没顺手改**：
        #   `_restore_ux_state` 拿它决定要不要打那行「已回填上次使用的路径」，
        #   而"改计数口径"会让那行日志的出现条件变化，那是**行为改动**，
        #   不属于这次重构。无头模式那边只是拿它写一行数字，口径一样够用。
        return filled

    def _headless_fatal(self, code, msg):
        """记一条 + 定退出码。**只记不定终局** —— 终局由 `_headless_bail` 收。"""
        self._app._headless_code = code
        try:
            self._app._ui._update_log(msg)
        except Exception:
            pass

    def _headless_bail(self, code, msg):
        """任务没跑起来就收口：写日志 → 记退出码 → 结束主循环。**幂等，先到先得**。

        【为什么必须结束主循环】无头进程没有窗口可以关。不主动 `quit()` 的话，
        这个进程会一直停在 `mainloop()` 里，而任务锁**从头到尾没被拿到过**
        （所以也没有"等 30 秒心跳过期"这回事）—— 它就是纯粹地挂着不退出，
        schtasks 那边显示"正在运行"，永远不会结束。

        【幂等为什么是硬要求，不是谨慎】
          `root.quit()` **不中断当前调用栈**，它只是给主循环挂一个"该退了"的标记。
          于是"收口"这件事发生后，同一段代码会继续往下跑：撞锁 → 退出码 2 →
          接着 `_start_next_source` 里那条 `if not self._launch_source()` 又会想收口一次。
          没有幂等的话，第一次记的 2 **会被后来的 4 覆盖** ——
          而 2（让路）与 4（起不来）对用户是完全不同的两件事：
          前者是"今天有事，明天照常"，后者是"你的配置坏了，去修"。
        """
        if self._app._headless_closed:
            return
        self._app._headless_closed = True
        self._headless_fatal(code, msg)
        self._headless_fatal(code, "[无头] 退出码 %d：%s" % (code, _headless_code_note(code)))
        # 【收口了也要写回运行痕迹 —— 与 `_headless_finish` 对称】
        #   漏了这一条，界面上「上次运行」那一栏对**退出码 2 和 4 永远是空的**。
        #   而这两档恰恰是最需要被看见的："昨晚每一轮都撞锁让路了"与
        #   "昨晚每一轮都因为路径不存在没跑"，在界面上都显示成"从未运行过"
        #   —— 用户打开工具只会以为定时任务配得好好的。
        #   这不是"多加一个功能"，是**同一件事的另一半**：退出码是给 schtasks 看的，
        #   运行痕迹是给坐在界面前的这个人看的，两条都得有。
        self._headless_write_trace(code)
        self._headless_close_sink()
        try:
            self._app.root.quit()
        except Exception:
            pass

    def _headless_write_trace(self, code, aborted=False, copied=0, skipped=0, failed=0):
        """把这一轮的运行痕迹写回配置。**`_headless_finish` 与 `_headless_bail` 共用。**

        【为什么共用一个函数】两处要写的字段完全一样，唯一不同的是计数：
        正常收口时是真的数；没能起跑时那几个数就应该是 0（确实一个都没动）。
        两份各写一遍的后果是**迟早只剩一份对** —— 加一个字段就只加在一处，
        而症状是"起跑失败的那几次在界面上少一栏"。

        【为什么只改这几个键】`update_schedule_run` 是读改写。无头进程手里那份记录是
        **开跑之前**读的，跑完整份写回就会把这几个小时里用户在界面上改的参数一起盖掉
        —— 那正是 `_probe_profile_clobber.py` 实测过的丢失形态。
        """
        if not self._app._headless_name:
            return
        try:
            config.update_schedule_run(
                self._app._headless_name,
                last_run=datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                last_status=_headless_status(code, aborted),
                last_counts={"copied": copied, "skipped": skipped, "failed": failed},
                last_log=self._app._ui._log_sink_path_value() or "",
                exit_code=code)
        except Exception as e:
            # 写不进去**不影响退出码**：它是"给界面看的那一半"，
            # 而退出码是"给 schtasks 看的那一半"，两者互不依赖。
            self._headless_fatal(code, "[无头] 写回运行痕迹失败：%s" % e)

    def _headless_close_sink(self):
        """关日志文件。幂等（`_headless_finish` 与 `_headless_bail` 都可能调它）。"""
        f = self._app._ui._close_log_sink()      # 【第 1 步】取句柄并置空（原写法一字不差）
        if f is not None:
            try:
                f.flush()
                f.close()
            except Exception:
                pass

    def _enter_headless(self, task):
        """无头模式的装配：开日志 sink、把任务参数灌进界面变量、起开局那一枪。

        在 `mainloop()` **之前**调用（见 `_run_headless`）。
        """
        global _HEADLESS_BOX_WRITE
        self._app._headless = True
        self._app._headless_name = task["name"]
        self._app._headless_code = config.SCHEDULE_EXIT_CANNOT_START
        log_path = config.schedule_log_path(task["name"])
        opened = self._app._ui._open_log_sink(log_path)
        # 【把"本该弹的框"接到日志文件上】这一行之后，替身记的每一笔都会落盘。
        #   接在这里而不是 `__main__` 里，是因为写日志的通道（sink）到这里才存在。
        #   接上之前记下的那几笔（构造期，见下面 `_headless_begin` 的处理）
        #   这里补写一次 —— 它们恰恰是最该看见的：构造期弹框说明"连窗口都没装配好"。
        _HEADLESS_BOX_WRITE = self._app._ui._log_sink_write
        for _kind, _title, _msg in _HEADLESS_BOX_LOG:
            _HEADLESS_BOX_WRITE("[无头·本该弹的框] (%s) %s —— %s（构造期，补记）"
                                % (_kind, _title, " ".join(_msg.split())))
        # 先把"这次是谁、什么时候、日志在哪"写进文件 —— 这三行必须在最前面，
        # 它们是事后翻这个文件时的坐标系（尤其当任务连参数都没灌进去就挂了）。
        self._app._ui._log_sink_write("=" * 78)
        self._app._ui._log_sink_write("[无头] 定时任务「%s」开始于 %s"
                             % (task["name"], datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")))
        self._app._ui._log_sink_write("[无头] 程序：%s" % sys.executable)
        self._app._ui._log_sink_write("[无头] 触发：%s" % sched_tab._format_trigger(task.get("trigger") or {}))
        self._app._ui._log_sink_write("[无头] 日志：%s%s"
                             % (log_path, "" if opened else "（**打不开**，本次只有退出码可看）"))
        self._app._ui._log_sink_write("=" * 78)
        if not opened:
            # sink 没开成：上面那几行白写了。用 logger 的兜底路径再说一次是不行的
            # （它写的还是这个 sink），所以这里什么也不补 —— 退出码仍然是有效的信号。
            pass
        # 【参数灌进界面变量】走的是**与"回填上次使用的路径"同一个白名单**
        #   （`_apply_ux_state_dict`），不是另写一份。理由：那份白名单是
        #   "哪些键能被用户可编辑的 JSON 影响程序行为"的**唯一**答案，
        #   两份必然漂移，而漂移的症状是"界面上能填的定时任务跑起来不生效"。
        filled = self._apply_ux_state_dict(task.get("params") or {})
        self._app._ui._log_sink_write("[无头] 已套用 %d 项任务参数" % filled)
        # 开局那一枪排在 after(0)：让 mainloop 先跑起来再动手，
        # 这样 `_headless_bail` 里的 `root.quit()` 一定是"在 mainloop 内"被调的
        # （在 mainloop 之前调 quit 是无效的，进程照样会进去）。
        self._app.root.after(0, self._headless_begin)

    def _headless_begin(self):
        """开局：处理构造期攒下的失败 → 起看门狗 → 真的点一次「开始传输」。"""
        if self._app._headless_deferred_bail is not None:
            code, msg = self._app._headless_deferred_bail
            self._app._headless_deferred_bail = None
            self._headless_bail(code, "[无头] 启动阶段就失败了：" + msg)
            return                     # 不点「开始传输」—— 状态库都不可用
        self._app.root.after(_HEADLESS_START_TIMEOUT_MS, self._headless_start_timeout)
        try:
            self._app._core._start_transfer()
        except Exception as e:
            self._headless_bail(config.SCHEDULE_EXIT_CANNOT_START,
                                "[无头] 启动传输时异常：%s" % e)

    def _headless_start_timeout(self):
        """看门狗：到点时还没进入传输态 ⇒ 判"起不来"。

        【判据为什么是 `_task_active`】它是既有的、唯一的"任务真的在跑"的旗标
        （`_launch_source` 里置 True，`_transfer_finished` 里置 False）。
        任务跑起来之后这个回调就什么都不做 —— 它**只管开局**，不是超时杀任务：
        一次几十万文件的备份跑几小时是正常的，给它加总时长上限是错的。
        """
        if self._app._core._task_active:
            return
        self._headless_bail(
            config.SCHEDULE_EXIT_CANNOT_START,
            "[无头] 启动超时（%d 秒内没有进入传输态）—— 判为起不来"
            % (_HEADLESS_START_TIMEOUT_MS // 1000))

    def _headless_finish(self, spec, reason, copied, skipped, failed, stopped):
        """任务跑完的收口：算退出码 → 记运行痕迹 → 等 db-writer → 结束主循环。

        【为什么最后要等 db-writer】任务级锁是它收到 `__task_end__` 之后才释放的
        （见 `_db_writer_loop`）。不等就退，锁会一直挂到心跳过期（`DB_STALE_AFTER_S`
        = 30 秒）—— 下一个整点如果紧接着（比如"每 1 小时"），那一轮就会撞上
        自己上一轮留下的锁，白白让路一次（退出码 2）。
        """
        if self._app._headless_closed:
            return
        code = _headless_exit_code(copied, skipped, failed, stopped)
        self._app._headless_closed = True
        self._app._headless_code = code
        aborted = bool(stopped) or reason in ("consec_fail", "stopped")
        self._headless_fatal(code, "[无头] 结果：复制 %d 个，跳过 %d 个，失败 %d 个"
                                   "（%s）" % (copied, skipped, failed,
                                              _headless_code_note(code)))
        if aborted and code == config.SCHEDULE_EXIT_OK:
            # 理论上到不了（`_headless_exit_code` 已经把 aborted 挡在 0 之外），
            # 留着是**断言性的**：真到了说明两处的判据漂了，那必须看得见。
            self._headless_fatal(config.SCHEDULE_EXIT_PARTIAL,
                                 "[无头] **内部不一致**：任务被中止却算成了成功")
            code = config.SCHEDULE_EXIT_PARTIAL
        if aborted:
            self._headless_fatal(code, "[无头] 本次是**中止**结束（原因：%s）—— "
                                       "中止一律不算成功，进度库里的状态是 aborted"
                                       % (reason or "用户停止"))
        # 等 db-writer 排空（有界）。它自带 10 秒上限，等不到也不阻塞退出。
        t = self._app._core._db_writer_thread
        if t is not None and t.is_alive():
            t.join(10.0)
        self._headless_write_trace(code, aborted, copied, skipped, failed)
        self._headless_close_sink()
        try:
            self._app.root.quit()
        except Exception:
            pass


def _run_headless(task_name, host_ns):
    """无头跑一条定时任务。**返回进程退出码**（调用方 `sys.exit` 它）。

    【这个函数只做四件事】读配置 → 建窗口并藏起来 → 交给 app → 拿回退出码。
    真正的逻辑全在 `FileTransferApp` 那一侧 —— 因为路线 B 的全部要点就是
    "**复用**那一整套既有实现"，这个函数里多写一行都可能变成"第二套逻辑"。
    """
    if not task_name:
        sys.stderr.write("[无头] --run-task 没有给出任务名\n")
        return config.SCHEDULE_EXIT_CANNOT_START
    tasks = config.load_schedules()
    task = None
    for s in tasks:
        if s.get("name") == task_name:
            task = s
            break
    if task is None:
        # 配置里没有这条任务。**不改任何东西、不猜**：最可能的情况是用户刚把这条
        # 任务删了而系统里的计划任务还没删掉（删除顺序是先系统后配置，
        # 但失败时会留下孤儿任务 —— 见 `delete_schedule` 的注释）。
        sys.stderr.write("[无头] 配置里没有名为 %r 的定时任务\n" % task_name)
        return config.SCHEDULE_EXIT_CANNOT_START
    if not task.get("enabled", True):
        # 停用的任务照跑会**很难解释**：用户在界面上明明关了它。
        # 但"照跑"与"不跑"都不该是静默的 —— 所以记一条日志文件再返回 4。
        try:
            os.makedirs(config.SCHEDULE_LOG_DIR, exist_ok=True)
            with open(config.schedule_log_path(task["name"]), 'a', encoding='utf-8') as f:
                f.write("[无头] 定时任务「%s」在配置里是**停用**状态，本次不执行。\n"
                        "       （如果你刚刚在界面上关掉它，这就是预期行为；\n"
                        "        如果它还在系统里被定时唤起，请在界面上删掉这条任务，\n"
                        "        那会一并删掉系统里的计划任务。）\n" % task["name"])
        except Exception:
            pass
        return config.SCHEDULE_EXIT_CANNOT_START

    root = tk.Tk()
    # 【为什么是 withdraw 而不是 `tk.Tk(useTk=False)` 或干脆不建】
    #   路线 B 的核心取舍：**真的建一个窗口，然后把它藏起来**。
    #   不建的话，整套传输逻辑里每一处 `Tk 变量.get()` / `widget.configure` 都会炸；
    #   而"建了但不 withdraw"会在用户屏幕上**弹出一个窗口** ——
    #   无人值守的定时任务弹窗是最糟的一种打扰（尤其是它还会抢焦点）。
    #   `withdraw()` 必须在构造 app **之前**：构造里就会 `geometry(...)` 并上屏，
    #   先 withdraw 能让它一次都不闪。
    root.withdraw()
    # ⚠️ 入口类 `FileTransferApp` 住宿主 ⇒ 这里**注入**（本模块不许 import 宿主）。
    app = host_ns.FileTransferApp(root)
    app._boot._enter_headless(task)    # 起看门狗之前先灌参数（见该函数）
    root.mainloop()
    return app._headless_code
