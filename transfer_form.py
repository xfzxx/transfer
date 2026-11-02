# -*- coding: utf-8 -*-
r"""界面表单（`TransferForm`）—— 从 `FileTransferApp` 里切出来的第 5 个真模块（**5a**）。

【它管什么】**前两个页签的全部界面与交互**（`_nightly\55` 的分层：L5「传输表单」）：
  · 「传输」页   —— 源列表（增/删/选/滚轮）、目标、模式/线程/过滤、开始/暂停/停止/重试、
                    进度与速度标签、日志框与导出按钮、两个"去设置"跳转按钮
  · 「设置」页   —— FTP 配置档（新建/编辑/删除/设为当前、列表与摘要）、库保留天数、
                    日志级别与导出目录、清空状态库按钮
  · 源/目标的本地与共享路径选择（`filedialog` / `simpledialog`）

【层级】**L5** —— 只 import L0（`config`）+ `ui_dialogs`；不认识 `FileTransferApp`
  （它只拿到一个**抽象可写句柄** `_HostFormSink`）⇒ 不存在 L5→L7 的反向依赖。

【装配】宿主把 5 个跨层对象**同名同对象**注入（`root` / `_ui` / `_core` / `_maint` /
  `_entry_style`）；32 个界面名字走 property 代理（**只读 17 / 可写 15**），
  读写两向都落回宿主**同一个**属性槽。

【为什么 property 分成"只读/可写"两半】判据只有一条：**这个名字在 L5 里有没有
  `self.<名> = ...`（重绑）**。只读面故意**不给 setter** —— 万一将来有人写了重绑，
  当场 `AttributeError`（响亮失败）比"悄悄把宿主那个对象换成新的"好得多。
"""
import os

import tkinter as tk
from tkinter import filedialog, messagebox, simpledialog
from tkinter import font as tkfont

import ttkbootstrap

import config
import ui_dialogs


def _quiet_option_menu(om):
    """把两个 `OptionMenu` 从"大块主色按钮"压成和「浏览」同款的描边样式。

    【为什么需要这一句 —— 这是 ttkbootstrap 的一个覆盖面缺口，不是我的偏好】
      `ttkbootstrap.OptionMenu` **就是** `tkinter.ttk.OptionMenu`（那个类名在
      ttkbootstrap 里只是重新导出了一遍，没有任何自己的 `__init__`），
      所以它**不认 `bootstyle=` 参数** —— 传了会被当成 ttk 的未知选项。
      而 cosmo 主题给 `TMenubutton` 的默认配色**就是主色**（实测 bg=#2780e3、
      fg=#ffffff），于是照默认建出来的两个下拉框，和「开始传输」是同一块蓝 ——
      界面上会有三个"最重的"元素抢视线，而这两个下拉框只是一个次要的路径类型选择。
      对比：同一行的「浏览」「⚙」是 `secondary-outline`（bg=#ffffff / fg=#7E8081）。
      把下拉框调成同一个样式，一行里的三个控件才是一个重量级。

    【为什么不是 `om.configure(bootstyle=...)`】
      上面说了，它不认这个参数。真正的入口是 `Bootstyle.update_ttk_widget_style`，
      而它**只返回样式名、不替你设上去**（ttkbootstrap 自己的控件是拿返回值
      传给 `super().__init__(style=...)` 的）。所以这里必须**自己 set 回去** ——
      只调不设的写法不报错、样式名也不变，是最容易被误认为"生效了"的那种失败。

    【为什么不干脆换个控件类】`ttkbootstrap.Menubutton` 认 `bootstyle`，
      但它不是 `OptionMenu`：菜单要自己建、变量到按钮文字的联动要自己接
      （`OptionMenu` 的价值恰恰是这段联动）。那是在**重写一个控件**，
      超出 C1"只换控件类"的范围。这一句只改样式名，控件的构造方式和行为一个字没动。
    """
    om.configure(style=ttkbootstrap.Bootstyle.update_ttk_widget_style(om, "secondary-outline"))
    return om


class TransferForm(object):
    r"""界面表单（L5）。装配与"同一份"的保证见模块 docstring 与 `_HostFormSink`。"""

    def __init__(self, host, root, ui, core, maint, entry_style, is_unc_path):
        # 【装配】`host` 是**可写句柄**（`_HostFormSink`，白名单 + 属性代理回宿主），
        #   不是宿主对象本身 ⇒ L5 不认识 `FileTransferApp`。
        self._host = host
        # 【同名同对象注入】下面 5 个**逐字同名** ⇒ 27 个方法体里对应的行**一字都不用改**，
        #   而且读的是**同一个对象**（天然实时，不存在"按值快照不更新"）。
        self.root = root
        self._ui = ui
        self._core = core
        self._maint = maint
        self._entry_style = entry_style
        # 【唯一要改文本的名字】`_is_unc_path` 是模块级函数、且**同时被 core 用** ⇒ 属 L2 侧，
        #   不能搬进 L5 ⇒ 注入（新模块里 2 处调用点已改成 `self._is_unc_path(...)`）。
        self._is_unc_path = is_unc_path

    # ---------- 只读 16 个：L5 方法体里 **0 处** `self.<名> = ` ----------
    #   故意**不给 setter**：读出去的就是宿主那**一个**对象 —— `self.X.set(...)` /
    #   `self.X.append(...)` / 控件方法 都是"对**返回对象**做变异"，与 setter 无关；
    #   而万一将来有人在 L5 里写了 `self.X = ...`（重绑），**当场 AttributeError**，
    #   比"悄悄把宿主那个对象换成新的"（L7 侧挂在旧对象上的引用全失效）好得多。
    #   —— 与第 4 步 6b4 同一条纪律：「响亮失败」优先于「静默兼容」。
    @property
    def db_keep_days(self):
        return self._host.db_keep_days

    @property
    def dest_path(self):
        return self._host.dest_path

    @property
    def dest_path2(self):
        return self._host.dest_path2

    @property
    def multi_dest(self):
        return self._host.multi_dest

    @property
    def dest_type(self):
        return self._host.dest_type

    @property
    def file_filter(self):
        return self._host.file_filter

    @property
    def flatten(self):
        return self._host.flatten

    @property
    def ftp_timeout(self):
        return self._host.ftp_timeout

    @property
    def log_export_dir(self):
        return self._host.log_export_dir

    @property
    def log_level(self):
        return self._host.log_level

    @property
    def purge_overwrites(self):
        return self._host.purge_overwrites

    @property
    def reconnect_count(self):
        return self._host.reconnect_count

    @property
    def retry_count(self):
        return self._host.retry_count

    @property
    def source_path(self):
        return self._host.source_path

    @property
    def source_type(self):
        return self._host.source_type

    @property
    def thread_count(self):
        return self._host.thread_count

    @property
    def transfer_mode(self):
        return self._host.transfer_mode

    @property
    def source_list(self):
        return self._host.source_list

    # ---------- 可写 17 个：L5 方法体里 **有** `self.<名> = ` ----------
    #   setter **写回宿主同一个槽**（不是新建、不是自己存一份）⇒ 全局只有一份。
    @property
    def dst_settings_btn(self):
        return self._host.dst_settings_btn

    @dst_settings_btn.setter
    def dst_settings_btn(self, value):
        self._host.dst_settings_btn = value

    @property
    def export_log_btn(self):
        return self._host.export_log_btn

    @export_log_btn.setter
    def export_log_btn(self, value):
        self._host.export_log_btn = value

    @property
    def pause_btn(self):
        return self._host.pause_btn

    @pause_btn.setter
    def pause_btn(self, value):
        self._host.pause_btn = value

    @property
    def profile_listbox(self):
        return self._host.profile_listbox

    @profile_listbox.setter
    def profile_listbox(self, value):
        self._host.profile_listbox = value

    @property
    def profile_summary(self):
        return self._host.profile_summary

    @profile_summary.setter
    def profile_summary(self, value):
        self._host.profile_summary = value

    @property
    def purge_btn(self):
        return self._host.purge_btn

    @purge_btn.setter
    def purge_btn(self, value):
        self._host.purge_btn = value

    @property
    def purge_overwrites_check(self):
        return self._host.purge_overwrites_check

    @purge_overwrites_check.setter
    def purge_overwrites_check(self, value):
        self._host.purge_overwrites_check = value

    @property
    def retry_btn(self):
        return self._host.retry_btn

    @retry_btn.setter
    def retry_btn(self, value):
        self._host.retry_btn = value

    @property
    def source_listbox(self):
        return self._host.source_listbox

    @source_listbox.setter
    def source_listbox(self, value):
        self._host.source_listbox = value

    @property
    def src_scrollbar(self):
        return self._host.src_scrollbar

    @src_scrollbar.setter
    def src_scrollbar(self, value):
        self._host.src_scrollbar = value

    @property
    def src_settings_btn(self):
        return self._host.src_settings_btn

    @src_settings_btn.setter
    def src_settings_btn(self, value):
        self._host.src_settings_btn = value

    @property
    def start_btn(self):
        return self._host.start_btn

    @start_btn.setter
    def start_btn(self, value):
        self._host.start_btn = value

    @property
    def stop_btn(self):
        return self._host.stop_btn

    @stop_btn.setter
    def stop_btn(self, value):
        self._host.stop_btn = value

    @property
    def ftp_active(self):
        return self._host.ftp_active

    @ftp_active.setter
    def ftp_active(self, value):
        self._host.ftp_active = value

    @property
    def ftp_source_config(self):
        return self._host.ftp_source_config

    @ftp_source_config.setter
    def ftp_source_config(self, value):
        self._host.ftp_source_config = value

    @property
    def ftp_dest_config(self):
        return self._host.ftp_dest_config

    @ftp_dest_config.setter
    def ftp_dest_config(self, value):
        self._host.ftp_dest_config = value

    @property
    def ftp_profiles(self):
        return self._host.ftp_profiles

    @ftp_profiles.setter
    def ftp_profiles(self, value):
        self._host.ftp_profiles = value


    def _build_transfer_tab(self, main):
        """Tab1「传输」。五个分区，各自的**内部**与阶段二之前逐字相同。"""
        # ---- 源路径 ----
        # 【分区标题顶替了一行 Label】原来的 `Label("源文件/文件夹")` 独占 row0，
        #   现在它是 Labelframe 的标题 —— 同一句话少占 26px。这就是探针里
        #   `SAVE_TITLE_ROWS` 记的那一笔。
        src_group = ttkbootstrap.Labelframe(main, text="源文件/文件夹")
        src_group.grid(row=0, column=0, columnspan=3, sticky="ew", pady=(0,10))
        src_frame = ttkbootstrap.Frame(src_group)
        src_frame.pack(fill="x")
        # 【D-1】`Entry`（单行）→ `tk.Listbox(height=3)`（多来源列表）。
        #   **pack 的位置/side/fill/expand/padx 与原来逐字相同**，控件结构、行号、
        #   后面那些控件的顺序一个没动 —— 换的是这一个控件本身，不是布局。
        #
        #   【为什么是 `tk.Listbox` 而不是 `ttkbootstrap.Listbox`】
        #     实测：`ttkbootstrap` **1.20.4 里根本没有 `Listbox` 这个属性**
        #     （`module 'ttkbootstrap' has no attribute 'Listbox'`，见简报 11 §1.3 发现①）。
        #     原生 `tk.Listbox` 是标准库控件，**不是新依赖**。
        #
        #   【皮肤为什么要手配】原生 Listbox 默认是"凹陷边框 + 系统配色"，
        #     放在扁平白底的 cosmo 里很扎眼。下面每个值都是**实测对齐周边 `Entry`** 的
        #     （`st.lookup('TEntry', …)`：background=#ffffff / foreground=#373a3c /
        #     darkcolor=#ced4da / borderwidth=1），不是我挑的颜色。
        #     `highlightthickness=1` + `highlightbackground` 就是 Listbox 版的"那圈边框"
        #     （它在没有 fieldbackground 概念，边框走 highlight 环），
        #     聚焦时 `highlightcolor` 换成主色 —— 与 Entry 的聚焦表现一致。
        _st = ttkbootstrap.Style.instance
        _c = _st.colors
        self.source_listbox = tk.Listbox(
            src_frame, height=3, width=38, exportselection=False,
            bg=_st.lookup("TEntry", "background") or _c.bg,
            fg=_st.lookup("TEntry", "foreground") or _c.fg,
            selectbackground=_c.primary, selectforeground="#ffffff",
            relief="flat", borderwidth=0, highlightthickness=1,
            highlightbackground=_st.lookup("TEntry", "darkcolor") or _c.border,
            highlightcolor=_c.primary,
            activestyle="none")
        self.source_listbox.pack(side="left", fill="x", expand=True, padx=(0,4))
        # 选中项 → `source_path`。**这条绑定是"单一真相"的关键**：
        #   `source_path` 仍是"当前源"（9 个测试文件直接 set 它），
        #   列表选中只是**改写它的一个入口**，不是第二个真相来源。
        self.source_listbox.bind("<<ListboxSelect>>", self._on_source_select)
        # 【D-1 裁定②】可见滚动条。**此处推翻了方案 §3.5 的"不加滚动条"**：
        #   用户裁定——"选 10 个源看不到第 4~10 个就是**功能缺陷**"，
        #   硬约束 6 在源路径区范围内已解除，所以多这一个控件是被授权的。
        #   规则：**始终显示**（不做动态显隐，省一处状态判断）。
        # 实测（`_probe_scrollbar.py`）：加它 **+0** 高 **+0** 宽
        #   —— 声明 11x50 都小于 Listbox 的 56，`main` 需求高/宽一个像素都没动，
        #   所以窗口 880x690 **不需要再动**（顺带被 `_probe_alloc.py` 复核过：
        #   分配 690 ≥ 声明 662，余量 28px，无一行被裁）。
        # ⚠️ `ttkbootstrap.Scrollbar` **就是** `tkinter.ttk.Scrollbar`（1.20.4 里没包一层），
        #   所以它**不吃 `bootstyle`**。样式由主题的 `Vertical.TScrollbar` 决定（cosmo 自带）。
        # ⚠️ `state="disabled"` **两种实现都不支持**（实测 TclError），
        #   所以"没得滚时灰掉"做不到 —— 见简报 §裁定② 的说明。
        self.src_scrollbar = ttkbootstrap.Scrollbar(src_frame, orient="vertical",
                                                    command=self.source_listbox.yview)
        self.src_scrollbar.pack(side="left", fill="y")
        self.source_listbox.configure(yscrollcommand=self.src_scrollbar.set)
        # 滚轮：Tk 的 `Listbox` **自带**类绑定（一格走 4 项），这里用实例绑定**压住**它，
        # 定成一格一项 —— 详见 `_on_source_listbox_wheel` 的 docstring（那里面有一条
        # 被实测证伪的旧注释，别照抄）。
        self.source_listbox.bind("<MouseWheel>", self._on_source_listbox_wheel)
        # ⚠️ **这里比原来多传了一个 "local"**，不是笔误。
        #   `tk.OptionMenu(master, var, value, *values)` 的 `value` **是**菜单里的一项；
        #   而 `ttk.OptionMenu(master, var, default, *values)` 的 `default`
        #   **只用来初始化变量、不进菜单**（stdlib `set_menu()`：只有 `*values` 被
        #   `add_radiobutton`，`default` 只做 `self._variable.set(default)`）。
        #   照原样少传一个的后果：菜单里**只剩 "ftp"**，用户**再也选不回 "local"** ——
        #   界面照样显示 "local"（变量初值），但一点菜单就发现没有那一项。
        #   这属于"能跑、能显示、但有一个选项消失了"，不会报任何错。
        # 【阶段三】菜单多一项 `"share"`。**"local" 出现两次不是笔误**（见下面那段
        #   ⚠️ 注释：ttk 的 `default` 不进菜单，所以要多传一个）。
        #   `"share"` 只加在**源侧** —— 目标侧的 UNC 用那个可直编的 Entry 就填得了，
        #   给它也加一个菜单值等于让用户多选一次、还多一种状态，不划算。
        #   ⚠️ 菜单宽度按**最长的值**算：`"share"` 与 `"local"` 都是 5 字符，
        #   所以这一步对布局是 **+0 宽 +0 高**（有 `_probe_tab2_note.py` 复核）。
        _quiet_option_menu(ttkbootstrap.OptionMenu(src_frame, self.source_type,
                                                   "local", "local", "ftp", "share")).pack(side="left", padx=(0,4))
        ttkbootstrap.Button(src_frame, text="浏览", command=self._browse_source,
                            bootstyle="secondary-outline").pack(side="left", padx=2)
        # 【D-1】新增「－」= 删除列表里选中的那一项。
        #   样式 `secondary-outline` 与「浏览」「⚙」同款（沿用 C2 定的"次要动作用描边"）。
        #   ⚠️ **不是**每项旁一个「×」：那样要自建可滚动 Frame，是 Tk 里出名的坑
        #   （滚轮绑定层级、内层变宽 Canvas 不跟、resize 抖动）。方案 §3.5 有记录。
        ttkbootstrap.Button(src_frame, text="－", command=self._remove_selected_source,
                            bootstyle="secondary-outline").pack(side="left", padx=2)
        self.src_settings_btn = ttkbootstrap.Button(src_frame, text="⚙", bootstyle="secondary-outline",
                                                    command=lambda: self._ftp_settings(is_source=True))
        self.src_settings_btn.pack(side="left", padx=2)

        # ---- 目标路径 ----
        # 【行号 2/3 → 分区 row1】同源区：Label 变分区标题。**区内的控件一字未动**。
        dst_group = ttkbootstrap.Labelframe(main, text="目标文件夹")
        dst_group.grid(row=1, column=0, columnspan=3, sticky="ew", pady=(0,10))
        dst_frame = ttkbootstrap.Frame(dst_group)
        dst_frame.pack(fill="x")
        ttkbootstrap.Entry(dst_frame, textvariable=self.dest_path, width=38).pack(side="left", fill="x", expand=True, padx=(0,4))
        _quiet_option_menu(ttkbootstrap.OptionMenu(dst_frame, self.dest_type,
                                                   "local", "local", "ftp")).pack(side="left", padx=(0,4))
        ttkbootstrap.Button(dst_frame, text="浏览", command=self._browse_dest,
                            bootstyle="secondary-outline").pack(side="left", padx=2)
        self.dst_settings_btn = ttkbootstrap.Button(dst_frame, text="⚙", bootstyle="secondary-outline",
                                                    command=lambda: self._ftp_settings(is_source=False))
        self.dst_settings_btn.pack(side="left", padx=2)

        # 【功能 3-4】第二个目标：一遍扫描 / 一次判重，落两处。
        # 放在目标区**里面**（不新开分区）：它描述的就是"目标"，与上面那行是同一件事
        # 的第二份落点；新开一个分区会多占一行标题高度，而 Tab1 的高度余量已经为 0。
        dst2_frame = ttkbootstrap.Frame(dst_group)
        dst2_frame.pack(fill="x", pady=(6, 0))
        ttkbootstrap.Checkbutton(dst2_frame, text="同时复制到第二个目标", variable=self.multi_dest,
                                 bootstyle="primary").pack(side="left", padx=(0, 8))
        ttkbootstrap.Entry(dst2_frame, textvariable=self.dest_path2, width=30).pack(
            side="left", fill="x", expand=True, padx=(0, 4))
        ttkbootstrap.Button(dst2_frame, text="浏览", command=self._browse_dest2,
                            bootstyle="secondary-outline").pack(side="left", padx=2)

        # ---- 选项（传输模式 / 文件过滤 / 扁平化 / 线程数）----
        # 【这里是"省 100px"里最实的一笔，逐行对应探针的账】
        #   原来这四件事占了 **6 行**（row4 标签 / row5 单选 / row6 标签 / row7 输入框 /
        #   row8 勾选 / row9 线程），现在压成 **3 行**：
        #     * row4 那个"传输模式"标签 → 挪到单选钮**同一行**，省 26px；
        #     * 过滤的 Label 与 Entry **同一行**（它本来就没省高度：Entry 自己就有 26px，
        #       并肩站只是少一行的**空白**，不减少占用 —— 探针的账里也没算它）；
        #     * row8 的勾选框（22px）与 row9 的线程行（43px）合并 → 省 22px。
        #   探针的总账 100px 分两块，都在本函数里看得见：
        #     源区标题 26 + 目标区标题 26 + 本区"传输模式"标题 26 = **78**（三个 Label 变分区标题）；
        #     扁平化勾选框 22 与线程行 43 合并 → 只留较高者 43，省 **22**。
        #   ⚠️ 过滤那一行**不在账里**：Label 与 Entry 并肩站并不减少占用（Entry 本来就有 26px），
        #      省掉的只是标签自己的那一行空白 —— 探针没算它，这里也不该算。
        # 【为什么合并的是"勾选框 + 线程数"而不是别的两行】
        #   它俩是同一类东西（怎么传：结构保不保、并发几个），并肩站语义上说得通；
        #   而"过滤"是选哪些文件、"模式"是复制还是移动，各自独立成行更清楚。
        opt_group = ttkbootstrap.Labelframe(main, text="选项")
        opt_group.grid(row=2, column=0, columnspan=3, sticky="ew", pady=(0,10))
        opt_group.columnconfigure(1, weight=1)

        #  第 1 行：传输模式
        ttkbootstrap.Label(opt_group, text="传输模式").grid(row=0, column=0, sticky="w", padx=(0,10))
        mode_frame = ttkbootstrap.Frame(opt_group)
        mode_frame.grid(row=0, column=1, sticky="w", pady=(0,6))
        ttkbootstrap.Radiobutton(mode_frame, text="复制", variable=self.transfer_mode, value="copy",
                                 bootstyle="primary").pack(side="left", padx=(0,20))
        ttkbootstrap.Radiobutton(mode_frame, text="移动", variable=self.transfer_mode, value="move",
                                 bootstyle="primary").pack(side="left")

        #  第 2 行：文件过滤
        ttkbootstrap.Label(opt_group, text="文件类型过滤 (如 .txt,.jpg)").grid(row=1, column=0, sticky="w",
                                                                            padx=(0,10))
        ttkbootstrap.Entry(opt_group, textvariable=self.file_filter).grid(row=1, column=1, sticky="ew",
                                                                          pady=(0,6))

        #  第 3 行：扁平化 + 线程数
        opt_row3 = ttkbootstrap.Frame(opt_group)
        opt_row3.grid(row=2, column=0, columnspan=2, sticky="ew")
        ttkbootstrap.Checkbutton(opt_row3, text="扁平化传输（不保留目录结构）", variable=self.flatten,
                                 bootstyle="primary").pack(side="left")
        thread_frame = ttkbootstrap.Frame(opt_row3)
        thread_frame.pack(side="left", padx=(18,0))
        ttkbootstrap.Label(thread_frame, text="并发线程数:").pack(side="left")
        ttkbootstrap.Spinbox(thread_frame, from_=1, to=16, textvariable=self.thread_count, width=4, state="readonly").pack(side="left", padx=(6,0))
        # 【原来写的是 `fg="gray"`】换成主题的 secondary 色：两者都是"弱化的提示文字"，
        #   区别是 `"gray"` 是写死的，而 `bootstyle="secondary"` 会跟着主题走
        #   （实测 cosmo 下是 #7E8081 前景 + 白底，不是灰底徽章 —— 这点量过才敢用）。
        ttkbootstrap.Label(thread_frame, text="(移动盘/机械盘建议1；多线程仅本地传输有效)",
                           bootstyle="secondary").pack(side="left", padx=(6,0))

        # ---- 进度条和按钮 ----
        # 【row10 → 分区 row3】这一区**没有省任何高度**，控件也是原样搬进来的：
        #   它是探针里"进度条宽度会掉到 349"的那个受害者 —— 每套一层 Labelframe 就
        #   吃掉 2px 宽，而窗口宽必须留住进度条的 **350px 硬下限**（L4）。
        #   这就是 `geometry` 从 880 抬到 888 的**全部理由**，不是看着窄。
        prog_group = ttkbootstrap.Labelframe(main, text="进度")
        prog_group.grid(row=3, column=0, columnspan=3, sticky="ew", pady=(0,10))
        control_frame = ttkbootstrap.Frame(prog_group)
        control_frame.pack(fill="x")
        progress = ttkbootstrap.Progressbar(control_frame, mode="determinate")
        progress.pack(side="left", fill="x", expand=True, padx=(0,10))
        self._ui.attach_widgets(progress=progress)   # 【第 1 步】控件归 _ui（挂载点紧跟创建处）
        btn_frame = ttkbootstrap.Frame(control_frame)
        btn_frame.pack(side="right")
        # 【四个按钮的 bootstyle 是一次配色决定，不改任何行为】
        #   开始=primary（主操作，主题蓝）、停止=danger（红，通则）、
        #   暂停/重传失败=secondary（灰，次要操作）。禁用态由主题自己画成弱化样式，
        #   而 `state="disabled"` 的**初值**与后面所有 `configure(state=...)` 都没动。
        self.start_btn = ttkbootstrap.Button(btn_frame, text="开始传输", command=self._core._start_transfer,
                                             bootstyle="primary")
        self.start_btn.pack(side="left", padx=(0,6))
        self.pause_btn = ttkbootstrap.Button(btn_frame, text="暂停", command=self._core._toggle_pause,
                                             state="disabled", bootstyle="secondary")
        self.pause_btn.pack(side="left")
        self.stop_btn = ttkbootstrap.Button(btn_frame, text="停止", command=self._core._toggle_stop,
                                            state="disabled", bootstyle="danger")
        self.stop_btn.pack(side="left", padx=(6,0))
        self.retry_btn = ttkbootstrap.Button(btn_frame, text="重传失败", command=self._core._retry_failed,
                                             state="disabled", bootstyle="secondary")
        self.retry_btn.pack(side="left", padx=(6,0))

        # ---- 速度 / ETA（功能B）----
        # 【硬约束 9：这算"修 UI 缺陷"还是"改 UI 布局"】
        #   上面 4 个按钮、进度条的**创建顺序、pack 顺序、坐标一个字符没动** ——
        #   本段是**追加**在这一行末尾的：唯一的动作是往 row10 里多 pack 一个控件。
        #   按钮仍然吸附右边缘、进度条仍然 expand，用户看到的是"进度条右边多了个
        #   灰色的速度数字"，操作方式没变（没有按钮被挪/改名/删除）。
        #
        # 【为什么放在进度条右侧、而不是另起一行】
        #   另起一行要占约 30px 高度 → 窗口得 650，**超过用户给的 640 上限**；
        #   而插在中间的行会把 11/12/13 行号整体下移，那才真叫"改布局"。
        #   放进 row10：高度不变（这一行本来就被按钮撑着）、行号不变。
        #
        # 【宽度必须钉死 —— 这是本段唯一一个"看起来多余"的设计】
        #   标签文字每 500ms 变一次长度（"12.3 MB/s" → "1023.9 MB/s · 剩余 23 小时 59 分"）。
        #   不钉的话，旁边那条 `expand=True` 的进度条会被**推来推去**，
        #   用户看到的是进度条在抖 —— 比不显示速度还糟。
        #   Tk 的 Label 没有像素级 width，所以套一个 pack_propagate(False) 的 Frame：
        #   Frame 宽度锁死，Label 在里面靠右对齐填满。
        #   宽度取"界面上可能出现的最长一条"的实测值 + 余量，**不是拍的**。
        # 【功能C1：这一行的取字体方式换了，原因是换控件换出来的】
        #   原来是 `tkfont.Font(font=self.start_btn.cget("font"))` —— 靠**读按钮的
        #   `-font` 选项**拿到界面上真正在用的字体。而 `start_btn` 现在是 ttk 按钮，
        #   **ttk 控件根本没有 `-font` 这个选项**（字体是样式的一部分，不是控件的），
        #   这一句会当场 `TclError: unknown option "-font"`。
        #   改法不是"随便找个字体凑合"：ttk 控件的样式字体实测是**空串**
        #   （`Style.lookup("TButton","font") == ""`），空串的含义就是"用控件类默认字体"，
        #   也就是 `TkDefaultFont`（实测 Microsoft YaHei UI 9pt）——**和换肤前一模一样**。
        #   更关键的是下面给 `speed_label` 显式钉了同一个字体：
        #   **量的是它，画的也是它**，不再依赖"按钮和标签恰好用同一个默认字体"这个巧合。
        #   【阶段一 Bug2/Bug3 预算 +10 → +15】上面这段注释是"量什么画什么"的由来，
        #   +10 是当时的余量。阶段一新加的扫描态文案更长（实测最长一档从 188px 涨到
        #   194px："1023.9 MB/s · 已传 999999 个文件"，比原来的 ETA 档多 6px），
        #   +10 不够，按裁定放到 +15 —— 这是**文案变长的必然修正，不是改布局**。
        #   【裁定② 追认 +11px】实测 188 → 194px，预算 198 → **209**（`_spd_w` 是
        #   量出来的最大值 + 15，194 + 15 = 209）。裁定原文："+5 是拍脑袋估的，实测
        #   194px 是硬数据；进度条 355px 仍 > 下限 350，L4 未破。数据优先于预估。"
        #   【下面这张表的语义：穷举 `_speed_text` 真的会产出的最长一档】所以新增的
        #   ① ② ⓪ 三档**必须**进来（② 就是新的最长档，不进来的话量出来的宽度是错的）。
        #   ⚠️ ⓪（已暂停）比 ② 短（"已暂停" 3 字 vs "1023.9 MB/s"），列进来是为了
        #   让"这张表穷举了全部档位"这句话**在表里看得见**，不是为了抬预算 ——
        #   它不会改变 `max()` 的结果。
        #   但**不要**去凑 ① ② 的拼接 —— "扫描中… 已扫 N 个目录 · 已传 M 个文件"
        #   这种组合永远不会出现：_speed_text 里扫描态二选一，且 ② 按裁定不含目录数。
        #   把不可能出现的串放进表里，预算会虚涨到 300+px，白白压缩日志区。
        _spd_font = tkfont.nametofont("TkDefaultFont")
        _spd_w = max(_spd_font.measure(s) for s in (
            "1023.9 MB/s · 剩余 23 小时 59 分",     # 速度三整数位 + 小时级 ETA（扫描已结束）
            "999.9 KB/s · 剩余 59 分 59 秒",
            "1023.9 MB/s · 已传 999999 个文件",     # 【阶段一】扫描中已在传（②）的最长一档
            "扫描中… 已扫 999999 个目录",           # 【阶段一】扫描中还没开始传（①）的最长一档
            "已暂停 · 已传 999999 个文件",           # 【裁定②】暂停态（⓪）的最长一档
            "扫描中…",
            "完成",
        )) + 15
        speed_frame = ttkbootstrap.Frame(control_frame, width=_spd_w)
        # fill="y" 不能省：pack_propagate(False) 之后 Frame 的**高度**不再由子控件撑开，
        # 而它的默认 height 是 0 —— 不加这一句，标签会被裁成 0 像素高（看起来就是没显示）。
        # 宽度由 _spd_w 锁死，高度由这一行的高度（按钮撑出来的）决定，正是想要的。
        speed_frame.pack(side="left", fill="y")
        speed_frame.pack_propagate(False)
        # `font=_spd_font` 是**显式钉的**，不是多余的：上面那段宽度就是按这个字体量的，
        # 显式绑上去之后"量什么"和"画什么"由构造保证一致，不再依赖"标签会自己
        # 挑到同一个默认字体"。换主题若将来改了默认字体，这一处会跟着变宽/变窄，
        # 而 `_verify_feature_c1.py` 的像素断言会当场红 —— 那正是想要的行为。
        speed_label = ttkbootstrap.Label(speed_frame, text="", anchor="e",
                                         bootstyle="secondary", font=_spd_font)
        speed_label.pack(fill="both", expand=True)
        self._ui.attach_widgets(speed_label=speed_label)   # 【第 1 步】

        # ---- 日志 ----
        # 【分区 row4 —— 全窗口唯一"吃掉剩余高度"的那一格】
        #   原来挂在 `main` 的 row12（`weight=1` 也在那一行）；现在多了一层 Labelframe，
        #   于是 **weight 要跟着搬进来**：`main.rowconfigure(4, weight=1)` 让分区本身撑满，
        #   `log_group.rowconfigure(0, weight=1)` 再把多出来的高度让给里面的 log_frame。
        #   ⚠️ 少写任何一条，症状都是"日志区被压成一条缝"—— 而这个窗口的每一行
        #      都是从日志区身上借的高度（见下面那段教训），**它是第一个受害者**。
        log_group = ttkbootstrap.Labelframe(main, text="传输日志")
        log_group.grid(row=4, column=0, columnspan=3, sticky="nsew")
        log_group.rowconfigure(0, weight=1)
        log_group.columnconfigure(0, weight=1)
        log_frame = ttkbootstrap.Frame(log_group)
        log_frame.grid(row=0, column=0, sticky="nsew")
        # 【日志区为什么仍然用 `height=8` 这个"行数"而不是像素】
        #   它是全窗口唯一带 `weight=1` 的一行，实际高度由"窗口给多少"决定，
        #   `height=8` 是**声明**（108px）—— 功能A 那次它曾被挤到 25px，
        #   就是"声明从未兑现"的现场（见 PROJECT_STATE §7.5.2）。
        #   换主题会改各行的实际高度（ttk 控件的 padding 与 tk 原生不同），
        #   省下来或超出去的部分**全部落在这一行身上** —— 所以换肤之后必须重新量。
        log_text = ttkbootstrap.Text(log_frame, height=8, state="disabled", wrap="word")
        scrollbar = ttkbootstrap.Scrollbar(log_frame, orient="vertical", command=log_text.yview)
        log_text.configure(yscrollcommand=scrollbar.set)
        scrollbar.pack(side="right", fill="y")
        log_text.pack(side="left", fill="both", expand=True)
        self._ui.attach_widgets(log_text=log_text)   # 【第 1 步】
        main.rowconfigure(4, weight=1)
        main.columnconfigure(1, weight=1)

        # ---- 日志操作（功能A）----
        # 【硬约束 6：绝不改 UI 布局 —— 这里怎么算不违反它】
        #   本行**只新增一个控件**：不删、不移、不改名任何既有控件，
        #   上面 12 行 grid 的行号/列号/columnspan/sticky 一个字符没动。
        # 【这一行曾经**确实**压坏了日志区，记录在案】
        #   加它的那一刻窗口还是 `680x520`，而 `main` 那时已经需要 603px ——
        #   缺口 100% 落在唯一带 weight=1 的第 12 行身上，
        #   日志区从 61px（本来就没兑现 `height=8` 的 108px）被压到 **25px ≈ 1.4 行**。
        #   这是**实测出来的**、不是我预先想到的：验收脚本的布局断言把它挡了下来，
        #   按 STOP 条件 4 停下等裁定，用户裁定"这是修 UI 缺陷、不是改 UI 布局"并授权改尺寸。
        #   功能A/B 落定时窗口是 `880x620`、日志区 125px；功能C1 换肤后是
        #   `880x640`、日志区 **150px**（= 新字体下 `height=8` 的声明值，整行兑现）；
        #   D-1 步 5 窗口抬到 `880x690`、日志区 **176px**（源路径区换列表撑高了
        #   `main`，窗口是**跟着净增**的，没有从日志区借——见上面 ③）。
        #   ⚠️ **教训**：这个窗口的每一行都是从日志区身上借的高度。
        #   以后再往 `main` 里加东西，先量 `main.winfo_reqheight()`（不是各行 reqheight 的和，
        #   那个会漏掉 116px 的 pady），再决定要不要动 `geometry`。
        # 【row13 → log_group 内部 row1】它跟日志区是**同一个分区的两行**，不该再挂 main：
        #   挂出去的话"导出日志"会跑到分区框外面，看起来不属于日志区。
        log_op_frame = ttkbootstrap.Frame(log_group)
        log_op_frame.grid(row=1, column=0, sticky="e", pady=(6,0))
        self.export_log_btn = ttkbootstrap.Button(log_op_frame, text="导出日志", command=self._ui._export_log,
                                                  bootstyle="secondary-outline")
        self.export_log_btn.pack(side="right")
    def _build_settings_tab(self, main):
        """Tab2「设置」。**全是新增控件**，Tab1 里没有任何一个控件搬到这来。

        【为什么线程数在 Tab2 又出现一次，而且共用同一个 Tk 变量】
          它在语义上确实是"默认参数"，跟超时/重试是同一类东西，放在设置页理所当然。
          而它**不能**在 Tab2 另开一个 IntVar —— 那样两个控件各存一份值：
          Tab1 里调到 8、Tab2 里显示 1，用户点开始到底用哪个？只能靠"切页时同步"，
          而同步代码正是这个项目里反复出问题的那类东西（谁先谁后、谁覆盖谁）。
          共用一个 IntVar 之后，两个 Spinbox 是**同一个值的两个视图**，
          不存在"哪个是真的"这个问题 —— 也就不需要任何同步代码。
        """
        main.columnconfigure(0, weight=1)
        main.rowconfigure(3, weight=1)      # 最后一块（日志导出）吃掉剩余高度，视觉上不挤顶

        # ---- ① FTP 配置（多套）----
        prof_group = ttkbootstrap.Labelframe(main, text="FTP 配置")
        prof_group.grid(row=0, column=0, sticky="ew", pady=(0,10))
        prof_group.columnconfigure(0, weight=1)

        list_row = ttkbootstrap.Frame(prof_group)
        list_row.grid(row=0, column=0, columnspan=2, sticky="ew")
        self.profile_listbox = tk.Listbox(
            list_row, height=4, width=30, exportselection=False,
            bg=self._entry_style("background"), fg=self._entry_style("foreground"),
            selectbackground=ttkbootstrap.Style.instance.colors.primary,
            selectforeground="#ffffff", relief="flat", borderwidth=0,
            highlightthickness=1,
            highlightbackground=self._entry_style("darkcolor"),
            highlightcolor=ttkbootstrap.Style.instance.colors.primary,
            activestyle="none")
        self.profile_listbox.pack(side="left", fill="x", expand=True, padx=(0,4))
        self.profile_listbox.bind("<<ListboxSelect>>", self._on_profile_select)
        prof_sb = ttkbootstrap.Scrollbar(list_row, orient="vertical",
                                         command=self.profile_listbox.yview)
        prof_sb.pack(side="left", fill="y")
        self.profile_listbox.configure(yscrollcommand=prof_sb.set)

        prof_btns = ttkbootstrap.Frame(prof_group)
        prof_btns.grid(row=0, column=2, sticky="nw", padx=(8,0))
        ttkbootstrap.Button(prof_btns, text="新建…", width=8, command=self._profile_new,
                            bootstyle="primary").pack(fill="x", pady=(0,4))
        # 「编辑…」和「删除」都作用于**选中的那一套**，不是当前那一套 —— 见
        # `_profile_edit` 的注释（选着 B 点编辑却改到 A，是静默改错地方）。
        ttkbootstrap.Button(prof_btns, text="编辑…", width=8, command=self._profile_edit,
                            bootstyle="secondary-outline").pack(fill="x", pady=(0,4))
        ttkbootstrap.Button(prof_btns, text="删除", width=8, command=self._profile_delete,
                            bootstyle="secondary-outline").pack(fill="x", pady=(0,4))
        ttkbootstrap.Button(prof_btns, text="设为当前", width=8, command=self._profile_set_active,
                            bootstyle="secondary-outline").pack(fill="x")

        # 选中配置的摘要。**口令一个字符都不显示**（连星号都不给）：
        # 设置页是会被截图、被旁人看到的那一屏，而这里本来也没必要看口令。
        self.profile_summary = ttkbootstrap.Label(prof_group, text="", bootstyle="secondary",
                                                  anchor="w", justify="left")
        self.profile_summary.grid(row=1, column=0, columnspan=3, sticky="ew", pady=(8,0))

        # ---- ② 默认参数 ----
        param_group = ttkbootstrap.Labelframe(main, text="默认参数")
        param_group.grid(row=1, column=0, sticky="ew", pady=(0,10))
        param_group.columnconfigure(1, weight=1)

        def _spin(row, label, var, lo, hi, tip):
            ttkbootstrap.Label(param_group, text=label).grid(row=row, column=0, sticky="w",
                                                             padx=(0,10), pady=2)
            box = ttkbootstrap.Frame(param_group)
            box.grid(row=row, column=1, sticky="w", pady=2)
            ttkbootstrap.Spinbox(box, from_=lo, to=hi, textvariable=var, width=5,
                                 state="readonly").pack(side="left")
            if tip:
                ttkbootstrap.Label(box, text=tip, bootstyle="secondary").pack(side="left", padx=(8,0))

        _spin(0, "并发线程数:", self.thread_count, 1, 16,
              "（移动盘/机械盘建议 1；多线程仅本地传输有效）")
        _spin(1, "连接超时(秒):", self.ftp_timeout, config.TIMEOUT_RANGE_S[0],
              config.TIMEOUT_RANGE_S[1], "（FTP 连接与等待响应的上限）")
        # 【口径写在界面上，不写在文档里】用户填 3、代码里是 4 —— 这句话必须让用户看见，
        #   否则"我设了重试 3 次，日志里怎么说试了 4 次"就成了一次误报的 bug。
        _spin(2, "文件重试次数:", self.retry_count, config.RETRY_COUNT_RANGE[0],
              config.RETRY_COUNT_RANGE[1], "（每个文件失败后重试几次；退避 1/3/9 秒）")
        # 【追认第 3 点：口径写在界面上】"断线重连次数"只在**池化连接**那条路上生效 ——
        #   单连接重放（`_is_transport_error` 那条）走的是另一套重连，不吃这个数。
        #   用户填 5、日志里却按别的次数重连，跟上面"重试 3 次 vs 4 次"是同一类误报，
        #   所以"仅连接池生效"这行小字必须让用户看见。
        #   ⚠️ 它是**并进这一行提示语**的，不是另起一行 —— 实测（`_probe_tab2_note.py`）：
        #   另起一行会让设置页内容高 649px > 页高 643px，底部那块被裁 6px；
        #   而横向余量有 502px（这行提示语现在才 256px）。所以并排是零代价的那个做法。
        #   要改成两行，得先把窗口高度抬上去，那是另一件事、不该顺手做。
        _spin(3, "断线重连次数:", self.reconnect_count, config.RECONNECT_COUNT_RANGE[0],
              config.RECONNECT_COUNT_RANGE[1], "（FTP 连接断开后的重连次数 · 仅连接池生效）")

        #  日志级别：两档。**默认"详细"，且"常规"是减法** —— 切到常规只少三类行，
        #  其余一行不少（`config.ROUTINE_LOG_PREFIXES` 是那三类的定义处）。
        ttkbootstrap.Label(param_group, text="日志级别:").grid(row=4, column=0, sticky="w",
                                                             padx=(0,10), pady=2)
        lvl_box = ttkbootstrap.Frame(param_group)
        lvl_box.grid(row=4, column=1, sticky="w", pady=2)
        ttkbootstrap.Radiobutton(lvl_box, text="详细", variable=self.log_level,
                                 value=config.LOG_LEVEL_VERBOSE, bootstyle="primary"
                                 ).pack(side="left", padx=(0,16))
        ttkbootstrap.Radiobutton(lvl_box, text="常规", variable=self.log_level,
                                 value=config.LOG_LEVEL_NORMAL, bootstyle="primary"
                                 ).pack(side="left", padx=(0,10))
        ttkbootstrap.Label(lvl_box, text="（常规：不显示扫描进度 / 回收 / 库维护三类）",
                           bootstyle="secondary").pack(side="left")

        # ---- ③ 状态库维护 ----
        db_group = ttkbootstrap.Labelframe(main, text="传输状态库")
        db_group.grid(row=2, column=0, sticky="ew", pady=(0,10))
        db_group.columnconfigure(1, weight=1)

        ttkbootstrap.Label(db_group, text="记录保留天数:").grid(row=0, column=0, sticky="w",
                                                             padx=(0,10), pady=2)
        keep_box = ttkbootstrap.Frame(db_group)
        keep_box.grid(row=0, column=1, sticky="w", pady=2)
        ttkbootstrap.Spinbox(keep_box, from_=config.DB_KEEP_DAYS_RANGE[0],
                             to=config.DB_KEEP_DAYS_RANGE[1], textvariable=self.db_keep_days,
                             width=6, state="readonly").pack(side="left")
        ttkbootstrap.Label(keep_box, text="（更早的记录会被自动清理，不影响已传输的文件）",
                           bootstyle="secondary").pack(side="left", padx=(8,0))

        purge_row = ttkbootstrap.Frame(db_group)
        purge_row.grid(row=1, column=0, columnspan=2, sticky="ew", pady=(8,0))
        self.purge_btn = ttkbootstrap.Button(purge_row, text="清空状态库…", command=self._maint._purge_state_db,
                                             bootstyle="danger")
        self.purge_btn.pack(side="left")
        # 【「同时清空覆盖记录」默认不勾，而且**不落盘**】
        #   `overwrites` 表是覆盖事故的唯一线索（哪个文件被谁盖了、原先什么大小）。
        #   它跟 `files`（续传用，清掉只是重传一遍）完全不是一个量级的东西 ——
        #   所以它**单独一个勾选框、默认不勾**，且不写进 ux_state：
        #   一次勾选不该变成"以后每次都勾着"（那等于把默认值悄悄改成了勾）。
        self.purge_overwrites_check = ttkbootstrap.Checkbutton(
            purge_row, text="同时清空覆盖记录（overwrites）", variable=self.purge_overwrites,
            bootstyle="danger-round-toggle")
        self.purge_overwrites_check.pack(side="left", padx=(14,0))
        ttkbootstrap.Label(db_group, text="（清空后：已传记录丢失，下次会重新传输；导出/日志不受影响）",
                           bootstyle="secondary", anchor="w").grid(row=2, column=0, columnspan=2,
                                                                  sticky="ew", pady=(6,0))

        # ---- ④ 日志导出 ----
        exp_group = ttkbootstrap.Labelframe(main, text="日志导出")
        exp_group.grid(row=3, column=0, sticky="new")
        exp_group.columnconfigure(1, weight=1)
        ttkbootstrap.Label(exp_group, text="默认导出目录:").grid(row=0, column=0, sticky="w",
                                                             padx=(0,10), pady=2)
        ttkbootstrap.Entry(exp_group, textvariable=self.log_export_dir).grid(row=0, column=1,
                                                                          sticky="ew", pady=2)
        ttkbootstrap.Button(exp_group, text="浏览…", command=self._ui._pick_log_export_dir,
                            bootstyle="secondary-outline").grid(row=0, column=2, padx=(8,0), pady=2)
        ttkbootstrap.Label(exp_group, text="（留空 = 每次导出时用系统默认位置）",
                           bootstyle="secondary", anchor="w").grid(row=1, column=0, columnspan=3,
                                                                  sticky="ew", pady=(6,0))

        self._load_ftp_profiles()
    # ---- FTP 设置按钮 ----
    def _ftp_settings(self, is_source):
        """Tab1 的 ⚙：改的是**当前激活的那一套**配置。

        【阶段二改了什么】只多传了一个 `profile_name`。对话框内部那一行
        （`save_config` → `save_profile`）因此写回的正是当前这套；
        `self.ftp_active` 为空（一份都没有）时传 None，对话框退化成旧行为。
        下面的 `setattr` / 路径回填 / 弹框**一个字符没动**。
        """
        attr = "ftp_source_config" if is_source else "ftp_dest_config"
        current_cfg = getattr(self, attr)
        dlg = ui_dialogs.FTPSettingsDialog(self.root, initial_config=current_cfg,
                                           profile_name=self.ftp_active or None)
        self.root.wait_window(dlg)
        if dlg.result:
            setattr(self, attr, dlg.result)
            path_var = self.source_path if is_source else self.dest_path
            host = dlg.result['host']
            rd = dlg.result['remote_dir']
            path_var.set(f"ftp://{host}{rd}")
            # 对话框已经把这一套写盘了，这里只把内存里的两份视图同步过来
            # （列表里的名字 + 摘要）。**不重新读盘** —— 刚写下去的东西自己知道是什么。
            self._sync_profile_after_save(dlg.result)
            messagebox.showinfo("已保存", "FTP 配置已更新")
    # 【为什么"当前配置"要同时存在于 self.ftp_active 与磁盘的 active 字段】
    #   磁盘那份是"下次启动时的默认"，内存这份是"这一次点 ⚙ 时写回哪一套"。
    #   两者在**切换当前配置**时同时更新（`_profile_set_active`），所以不会各说各话；
    #   而 `_ftp_settings` 只认内存那份 —— 因为用户点 ⚙ 的时候，
    #   他要改的就是屏幕上写着"当前"的那一套。
    def _load_ftp_profiles(self):
        """启动时读一次。旧扁平格式的升级**只发生在内存里**（见 config.load_profiles）。"""
        try:
            self.ftp_profiles, self.ftp_active = config.load_profiles()
        except Exception as e:
            # 读配置失败不该拦住程序启动，但**不能装作没发生** ——
            # 用户会看到"我的配置不见了"，没有这行日志就无从查起。
            self.ftp_profiles, self.ftp_active = [], ""
            self._ui._threadsafe_log("[警告] 读取 FTP 配置失败：%s: %s" % (type(e).__name__, e))
            return
        self._refresh_profile_list()
        if self.ftp_profiles:
            self._apply_active_profile()
    def _refresh_profile_list(self, select=None):
        """把内存里的 `ftp_profiles` 画到 Listbox 上。**唯一的画法**，谁改都走它。

        `select` 为 None 时选"当前激活的那一套"；找不到就选第一项。
        这条约定与 Tab1 来源列表的 `_refresh_source_listbox(select=...)` 同源：
        列表上"选中的那一项"必须有个确定的含义，不能是"上次点剩的那个"。
        """
        self.profile_listbox.delete(0, "end")
        for p in self.ftp_profiles:
            mark = "● " if p["name"] == self.ftp_active else "   "
            self.profile_listbox.insert("end", mark + p["name"])
        if not self.ftp_profiles:
            self.profile_summary.configure(text="还没有配置。点「新建…」添加一套 FTP 连接信息。")
            return
        idx = select
        if idx is None:
            idx = 0
            for i, p in enumerate(self.ftp_profiles):
                if p["name"] == self.ftp_active:
                    idx = i
                    break
        idx = max(0, min(idx, len(self.ftp_profiles) - 1))
        self.profile_listbox.selection_clear(0, "end")
        self.profile_listbox.selection_set(idx)
        self.profile_listbox.see(idx)
        self._show_profile_summary(idx)
    def _show_profile_summary(self, idx):
        p = self.ftp_profiles[idx]
        cur = "（当前使用）" if p["name"] == self.ftp_active else ""
        # ⚠️ **口令一个字符都不显示**，连掩码都不给：这一屏会被截图、被旁人看到，
        #    而"确认一下地址填对没有"根本不需要口令。
        self.profile_summary.configure(
            text="%s%s\n%s@%s:%s%s" % (p["name"], cur, p["user"] or "(匿名)",
                                       p["host"], p["port"], p["remote_dir"]))
    def _selected_profile_idx(self):
        sel = self.profile_listbox.curselection()
        if not sel:
            return None
        idx = sel[0]
        if not (0 <= idx < len(self.ftp_profiles)):
            return None
        return idx
    def _on_profile_select(self, event=None):
        idx = self._selected_profile_idx()
        if idx is not None:
            self._show_profile_summary(idx)
    def _sync_profile_after_save(self, cfg):
        """对话框保存成功之后，把内存里的这一套更新掉（名字不变，内容变了）。"""
        if not self.ftp_active:
            # 一份都没有时的旧行为路径（profile_name=None → save_config 整份覆盖）：
            # 内存里那份配置也已经是新的了（`_ftp_settings` 里的 setattr 干的），
            # 所以这里什么都不用做 —— 唯一要防的是"以后有人加代码时忘了这条路径"。
            return
        for i, p in enumerate(self.ftp_profiles):
            if p["name"] == self.ftp_active:
                self.ftp_profiles[i] = dict(cfg, name=self.ftp_active)
                break
        self._refresh_profile_list()
    def _profile_new(self):
        """新建一套。`profile_name` 传的是**用户刚输入的新名字** —— 写盘时若已存在同名，
        `save_profile` 是"就地更新"而不是报错，所以这里必须先挡住重名。"""
        name = simpledialog.askstring("新建 FTP 配置", "给这套配置起个名字：", parent=self.root)
        if name is None:
            return                      # 取消
        name = name.strip()
        if not name:
            messagebox.showwarning("新建 FTP 配置", "名字不能为空。")
            return
        if any(p["name"] == name for p in self.ftp_profiles):
            # 挡在这里而不是靠 save_profile 覆盖：用户点"新建"却把老的那套改了，
            # 是**静默改错地方**，与"选着 B 点编辑改到 A"是同一类错误。
            messagebox.showwarning("新建 FTP 配置", "已经有一套叫「%s」的配置了，换个名字。" % name)
            return
        dlg = ui_dialogs.FTPSettingsDialog(self.root, initial_config=None, profile_name=name)
        self.root.wait_window(dlg)
        if not dlg.result:
            return
        self._profile_after_save(name, "已新建")
    def _profile_edit(self):
        """编辑**选中的**那一套（不是当前那套）。

        【为什么是"选中的"】用户在一列名字里点中 B 再点「编辑…」，他的意思百分之百是
        改 B。若这里图省事去改 `self.ftp_active`，他改完、保存、界面一切正常 ——
        只有下次连 A 的时候才发现地址变了。这类"静默改错地方"没有任何反馈可依赖，
        所以必须在实现上让它不可能发生：传进去的就是选中的那个名字。
        """
        idx = self._selected_profile_idx()
        if idx is None:
            messagebox.showwarning("编辑 FTP 配置", "请先在列表里选择一套配置。")
            return
        p = self.ftp_profiles[idx]
        dlg = ui_dialogs.FTPSettingsDialog(self.root, initial_config=dict(p),
                                           profile_name=p["name"])
        self.root.wait_window(dlg)
        if not dlg.result:
            return
        self._profile_after_save(p["name"], "已更新", select=idx,
                                 was_active=(p["name"] == self.ftp_active))
    def _profile_after_save(self, name, verb, select=None, was_active=False):
        """一套配置保存成功之后的**同一截尾**：重读盘 → 重画列表 →（必要时）回填 Tab1 → 记日志。

        【为什么必须"重读盘"，而不是就地改内存那一条】**盘才是事实**：
        `config.save_profile` 里那道 `_clean_profile` 会洗掉不合法的字段（端口越界回落 21、
        `remote_dir` 开头补 `/`）。内存若只做局部更新，界面上就会显示一套**磁盘上并不存在**
        的配置 —— 下次 ⚙ 打开时拿到的还是旧值，用户只会觉得"我改了但没生效"。

        【为什么抽成一个方法】三个调用方（`_profile_new` / `_profile_edit` /
        `_profile_save_fields`）的后半截**逐字相同**。写成三份的唯一后果是将来改一处忘两处，
        而那种病的症状是"Tk 档与 Web 档对同一次编辑给出不同结果"——
        没有任何一条测试会红，只能靠人看出来。

        `verb` 是日志里那个动词（"已新建" / "已更新"），由调用方给：它区分的是**用户干了什么**，
        不是本方法干了什么。`was_active` 同理 —— "改的是不是当前那一套"要在**保存之前**判定。
        """
        self.ftp_profiles, self.ftp_active = config.load_profiles()
        self._refresh_profile_list(select=select)
        if was_active:
            # 改的若是**当前**这套，Tab1 里的那份内存配置与路径显示都得跟着走，
            # 否则用户回到传输页看到的是旧地址，点开始却用新地址。
            self._apply_active_profile()
        self._ui._threadsafe_log("[配置] %s FTP 配置「%s」" % (verb, name))
    def _profile_save_fields(self, fields, is_new):
        r"""把**一份表单字段**写成一套配置（**不经过 Tk 对话框**）。Web 前端的「新建/编辑…」走这里。

        【为什么需要它 —— `_profile_new` / `_profile_edit` 那两条路在 Web 档走不通】
        它们的中段是 `ui_dialogs.FTPSettingsDialog`（一个 `tk.Toplevel`）：Web 档下没有窗口
        可画（`web_kit` 只给替身，`wait_window` 立刻返回、`result` 恒为 None）。所以**"取值"
        那一步两档只能各走各的**；而"存 + 刷新"那一步必须是同一份（`_profile_after_save`），
        否则同一次编辑在两档会得到不同结果。

        `fields` = 前端表单里那六个键（`name` / `host` / `port` / `user` / `password` /
        `remote_dir`）。失败**抛 `ValueError`**（Tk 档由对话框自己 `showwarning`，它到不了这里）：
        Web 档由 `web_app._semantic` 翻成 `{ok:false, error}` → 前端把这句话显示在表单里、
        **不关框**，用户改一个字符再点保存即可。

        【为什么把校验收在这里，而不是交给 `config.save_profile`】`_clean_profile` 遇到
        非数字/越界的端口是**静默回落 21**。那正好是最坏的那种失败：用户以为改成了 21xx，
        实际连的是 21。所以那两句提示必须挡在写盘之前 —— 判据与文案与
        `ui_dialogs.FTPSettingsDialog._get_config` **逐字相同**（那是 Tk 档的同一道关）。
        """
        name = (fields.get("name") or "").strip()
        if not name:
            raise ValueError("名字不能为空。")
        if is_new and any(p["name"] == name for p in self.ftp_profiles):
            # 与 `_profile_new` 同一条挡法：点"新建"却把老的那套改了，是**静默改错地方**。
            raise ValueError("已经有一套叫「%s」的配置了，换个名字。" % name)
        if not is_new and not any(p["name"] == name for p in self.ftp_profiles):
            # 要编辑的这套在盘上已经不在了（另一处删了 / 文件被手改过）。说清楚，
            # 而不是让它悄悄"就地新建"出一套同名的来。
            raise ValueError("配置「%s」已经不在了，刷新列表后再试。" % name)
        host = (fields.get("host") or "").strip()
        if not host:
            raise ValueError("服务器地址不能为空")
        try:
            port = int(str(fields.get("port")).strip())
        except (TypeError, ValueError):
            raise ValueError("端口必须是数字")
        remote_dir = (fields.get("remote_dir") or "").strip()
        if not remote_dir.startswith("/"):
            remote_dir = "/" + remote_dir
        cfg = {"host": host, "port": port,
               "user": (fields.get("user") or "").strip(),
               "password": (fields.get("password") or "").strip(),
               "remote_dir": remote_dir}
        idx = None
        for i, p in enumerate(self.ftp_profiles):
            if p["name"] == name:
                idx = i
                break
        # "改的是不是当前那一套"必须在保存前判定（保存之后 `ftp_active` 可能已经变了）。
        was_active = (name == self.ftp_active)
        config.save_profile(name, cfg)      # 新建 = 追加；编辑 = 就地更新同名那套（不改名字）
        self._profile_after_save(name, "已新建" if is_new else "已更新",
                                 select=idx, was_active=was_active)
    def _profile_delete(self):
        idx = self._selected_profile_idx()
        if idx is None:
            messagebox.showwarning("删除 FTP 配置", "请先在列表里选择一套配置。")
            return
        p = self.ftp_profiles[idx]
        if len(self.ftp_profiles) <= 1:
            # 与 `config.delete_profile` 的护栏同一个口径（它也不删最后一套）。
            # 两处都挡是有意的：config 那层是"数据不许坏"，这一层是"用户得听懂为什么"。
            messagebox.showwarning("删除 FTP 配置", "这是最后一套配置，不能删除。")
            return
        if not messagebox.askyesno("删除 FTP 配置", "确定删除配置「%s」？\n\n"
                                   "（只删这一套的连接信息，已传输的文件与状态库不受影响）" % p["name"]):
            return
        self.ftp_profiles, self.ftp_active = config.delete_profile(p["name"])
        self._refresh_profile_list()
        # 删掉的若是**当前**那套，`delete_profile` 已经把 active 换成了剩下的第一套
        # （见 config 里的实现）—— 这里跟着把 Tab1 的内存配置也切过去，
        # 否则"当前配置"这个名字指向 A、Tab1 里拿着的却还是被删掉的 B。
        self._apply_active_profile()
        self._ui._threadsafe_log("[配置] 已删除 FTP 配置「%s」" % p["name"])
    def _profile_set_active(self):
        idx = self._selected_profile_idx()
        if idx is None:
            messagebox.showwarning("设为当前", "请先在列表里选择一套配置。")
            return
        self.ftp_active = self.ftp_profiles[idx]["name"]
        config.save_profiles(self.ftp_profiles, self.ftp_active)
        self._refresh_profile_list(select=idx)
        self._apply_active_profile()
        self._ui._threadsafe_log("[配置] 当前 FTP 配置已切换为「%s」" % self.ftp_active)
    def _apply_active_profile(self):
        """把"当前配置"填进 Tab1 的源/目标内存配置与路径显示。

        【只填"用着 FTP 的那一侧"】两侧都是 FTP 时（FTP → FTP）以源为准：
        一份配置同时描述源和目标本来就是不成立的（目标可能根本在另一个服务器），
        而本工具的既有形态是"一侧本地 + 一侧 FTP"。这里做的是**保守回填**：
        哪一侧的类型是 ftp 就填哪一侧，都不是就填源侧（用户接下来多半会点源侧的 ⚙）。
        """
        cfg = None
        for p in self.ftp_profiles:
            if p["name"] == self.ftp_active:
                cfg = p
                break
        if cfg is None:
            return
        stripped = {k: cfg[k] for k in ("host", "port", "user", "password", "remote_dir")}
        for is_source in (True, False):
            if (self.source_type.get() if is_source else self.dest_type.get()) == "ftp":
                setattr(self, "ftp_source_config" if is_source else "ftp_dest_config", dict(stripped))
                path_var = self.source_path if is_source else self.dest_path
                path_var.set("ftp://%s%s" % (stripped["host"], stripped["remote_dir"]))
                return
        self.ftp_source_config = dict(stripped)
    # ---- 浏览源/目标 ----
    def _browse_source(self):
        st = self.source_type.get()
        if st == "local":
            self._pick_local_source()
        elif st == "share":
            # 【阶段三】共享**不走** `_pick_local_source` 那个三项菜单：
            #   那三项里"选择文件/选择文件夹"都是系统对话框，而系统对话框**到不了
            #   未挂载的 UNC**（用户正是还没连上才来填的）。共享只留一个入口 ——
            #   手打 UNC，理由见 `_pick_share_source`。
            self._pick_share_source()
        else:
            self._handle_ftp_browse(is_source=True)
    def _browse_dest(self):
        if self.dest_type.get() == "local":
            path = filedialog.askdirectory(title="选择目标文件夹")
            if path:
                self.dest_path.set(os.path.normpath(path))
        else:
            self._handle_ftp_browse(is_source=False)
    def _browse_dest2(self):
        """第二个目标只可能是本地目录，所以**不分叉**：没有 FTP 那一支。

        选完顺手把勾选框打上 —— 点「浏览」的意图就是"我要用第二个目标"，
        再让用户回头去勾一下是多余的一步（勾选框仍然可以单独关掉）。
        """
        path = filedialog.askdirectory(title="选择第二个目标文件夹")
        if path:
            self.dest_path2.set(os.path.normpath(path))
            self.multi_dest.set(True)
    def _handle_ftp_browse(self, is_source):
        cfg_attr = "ftp_source_config" if is_source else "ftp_dest_config"
        path_attr = "source_path" if is_source else "dest_path"
        cfg = getattr(self, cfg_attr)
        current_path = getattr(self, path_attr).get()

        if not cfg:
            dlg = ui_dialogs.FTPSettingsDialog(self.root)
            self.root.wait_window(dlg)
            if dlg.result:
                setattr(self, cfg_attr, dlg.result)
                cfg = dlg.result
                getattr(self, path_attr).set(f"ftp://{cfg['host']}{cfg['remote_dir']}")
            else:
                return

        initial = cfg.get('remote_dir', '/')
        if current_path and current_path.startswith("ftp://"):
            try:
                after_host = current_path.split("/", 3)[-1]
                if after_host:
                    initial = "/" + after_host
            except:
                pass

        dlg = ui_dialogs.FTPBrowserDialog(self.root, cfg, select_mode="dir", initial_dir=initial)
        self.root.wait_window(dlg)
        if dlg.result:
            full_url = f"ftp://{cfg['host']}{dlg.result}"
            getattr(self, path_attr).set(full_url)
            # 【D-1】FTP **源**同样进列表 —— §7.4 明确允许两类混排，而"这次要跑哪些源"
            # 只有一个答案来源（`source_list`）。不进列表的话，一个 ftp:// 源会显示在
            # `source_path` 里却**不参与串行驱动器**，那正是"填了但不生效"的坏形状。
            # FTP **目标**不进列表（列表是来源专用的）。
            if is_source:
                self._add_source(full_url)
            cfg['remote_dir'] = dlg.result
            # 只更新**当前那一套**：整份 `save_config` 会把 JSON 覆盖成扁平 5 键，
            # 其余几套配置（含主机/口令）会从磁盘上消失。没有当前配置名时
            # 保留旧的整份覆盖（此时也只有一套，兼容既有调用口径）。
            if self.ftp_active:
                config.save_profile(self.ftp_active, cfg)
                return
            config.save_config(cfg)   # 可选：自动保存到文件
    # ---- 【D-1】来源列表的三个动作：追加 / 删除 / 选中写回 ----
    def _add_source(self, path):
        """把一个来源**追加**到列表（去重、去空白）。返回它最终所在的下标。

        去重是必须的：同一个源加两次会在串行驱动器里跑两遍，
        第二遍因为判重全部跳过 —— 用户看到的是"有个任务什么都没干"。
        """
        path = (path or "").strip()
        if not path:
            return None
        if path in self.source_list:
            idx = self.source_list.index(path)
        else:
            self.source_list.append(path)
            idx = len(self.source_list) - 1
        self._refresh_source_listbox(select=idx)
        return idx
    def _remove_selected_source(self):
        """「－」按钮：删掉列表里选中的那一项。"""
        sel = self.source_listbox.curselection()
        if not sel:
            # 没选中任何一项就按"没有可删的"处理，**不弹窗**：
            # 这个按钮的语义足够直白，误点一下不该被打断。
            return
        i = sel[0]
        if 0 <= i < len(self.source_list):
            del self.source_list[i]
        if not self.source_list:
            # 列表删空了，`source_path` 必须一起清 —— 否则界面上一项都看不到，
            # 点「开始」却仍会拿这个残留值当"单源兜底"传出去。
            self.source_path.set("")
            self._refresh_source_listbox()
            return
        # 删完把选中落到**原来那一项的位置**上（越界则落到最后一项），
        # 免得每删一个光标就跳回顶部 —— 连着删几个的时候很烦。
        self._refresh_source_listbox(select=min(i, len(self.source_list) - 1))
    def _move_selected_source(self, delta):
        """把选中的源上移 / 下移一格（`delta` = -1 / +1）。→ 新下标（动不了返回 None）。

        【为什么要能排序】源是按列表顺序**串行**跑的，顺序就是执行顺序。以前只能
        删掉再重新加一遍来调整，加回去却落在末尾 —— 想换两个源的先后得全重来。
        到头 / 没选中都**静默不动**（与 `_remove_selected_source` 同一口径：
        按钮语义直白，误点一下不该弹窗）。
        """
        sel = self.source_listbox.curselection()
        if not sel:
            return None
        i = int(sel[0])
        j = i + int(delta)
        if not (0 <= i < len(self.source_list)) or not (0 <= j < len(self.source_list)):
            return None
        lst = self.source_list
        lst[i], lst[j] = lst[j], lst[i]
        self._refresh_source_listbox(select=j)
        return j
    def _dedupe_sources(self):
        """去掉列表里的重复项（保留第一次出现的位置）。→ 删掉的条数。"""
        seen, out = set(), []
        for s in self.source_list:
            if s not in seen:
                seen.add(s)
                out.append(s)
        removed = len(self.source_list) - len(out)
        if removed:
            self.source_list[:] = out
            self._refresh_source_listbox(select=(len(out) - 1) if out else None)
        return removed
    def _on_source_select(self, _event=None):
        """列表选中项 → `source_path`（单一真相的写回口）。"""
        sel = self.source_listbox.curselection()
        if sel and 0 <= sel[0] < len(self.source_list):
            self.source_path.set(self.source_list[sel[0]])
    def _on_source_listbox_wheel(self, event):
        """滚轮：**一格 = 一项**。

        列表只有 3 行可见，5 个源以后就有内容滚出视野了 —— 而**不加可见滚动条**
        （那会多一个控件、动布局）。所以滚轮是"列表能用"的必要条件，不是锦上添花。

        ⚠️ **本方法被 `_probe_e_wheel.py` 证伪过一次，如实记下。**
        原注释写的是「Tk 的滚轮事件**不会**自动绑到 Listbox 上，必须自己接」。
        **这句是错的**：`bind_class("Listbox", "<MouseWheel>")` 本来就有类绑定
        （Tk 8.6 自带）——
            if {%D >= 0} { %W yview scroll [expr {-%D/30}] units }
            else         { %W yview scroll [expr {(29-%D)/30}] units }
        把本方法 `unbind` 掉再一格一格发事件，滚轮**照样动**（探针 ② 实测）。

        那为什么还要这个方法？因为**类绑定一格走 4 项**（delta=-120 → (29+120)/30 = 4），
        而列表只有 3 行可见。实测轨迹（7 项，顶行只可能落在 0 或 4）：
            类绑定单独跑：[0.0, 0.571, 0.571, …]   ← 一格**直接到底**
        步长(4) > 页高(3) 的后果是**有的行永远进不了视野**：7 项时可视窗口只有
        [0,1,2] 和 [4,5,6] 两种，**第 3 项用户怎么滚都看不到**。列表里是路径，
        要一行一行读，所以这里定成**一格一项**。

        实现的关键一步是末尾的 `return "break"`：实例绑定返回 "break" 会**压住类绑定**。
        不写它的话两个绑定各滚各的（1 + 4 = 5 项，照样顶到底）——这正是
        `_verify_feature_e.py` A7b 第一次跑出 0.0 → 0.571 的原因。
        """
        self.source_listbox.yview_scroll(-1 if event.delta > 0 else 1, "units")
        return "break"
    def _refresh_source_listbox(self, select=None):
        """把 `source_list` 刷进 Listbox，并把选中落到 `select`（可选）。

        **列表与 `source_path` 的同步点**。规则只有一条：
        选中项 = 当前源；没得选（列表空）时才把 `source_path` 空着不管
        —— 空列表**不清 `source_path`**，那正是"单源兜底"能成立的原因。
        """
        self.source_listbox.delete(0, "end")
        for s in self.source_list:
            self.source_listbox.insert("end", s)
        if select is not None and 0 <= select < len(self.source_list):
            self.source_listbox.selection_clear(0, "end")
            self.source_listbox.selection_set(select)
            self.source_listbox.activate(select)
            self.source_listbox.see(select)
            self.source_path.set(self.source_list[select])
    def _pick_local_source(self):
        menu = tk.Menu(self.root, tearoff=0)
        menu.add_command(label="选择文件", command=self._pick_source_file)
        menu.add_command(label="选择文件夹", command=self._pick_source_dir)
        # 【D-1】第三项。前两项都要过系统对话框，而"加一个已经知道路径的目录"
        # （比如刚从别处复制来的路径串）没有对应入口 —— 只能先随便选一个再改，
        # 而 Listbox 是**只读**的，改不了。这一项补的就是这个口子。
        menu.add_separator()
        menu.add_command(label="手工添加…", command=self._pick_source_manual)
        menu.tk_popup(self.root.winfo_pointerx(), self.root.winfo_pointery())
    def _pick_source_file(self):
        path = filedialog.askopenfilename(title="选择文件")
        if path:
            # 【D-1】语义从"覆盖当前源"改成"**追加**到列表"（§7.3 形态 a）。
            # 覆盖式在多源下是致命的：加第二个源会把第一个源**悄悄抹掉**，
            # 而界面上看不出发生过什么。
            self._add_source(os.path.normpath(path))
    def _pick_source_dir(self):
        path = filedialog.askdirectory(title="选择文件夹")
        if path:
            self._add_source(os.path.normpath(path))
    def _pick_source_manual(self):
        """菜单第三项：手打一个路径加进列表。"""
        cur = self.source_path.get().strip()
        path = simpledialog.askstring("手工添加来源", "输入文件或文件夹的完整路径：",
                                      initialvalue=cur, parent=self.root)
        if path is None:
            return                     # 取消：不弹任何提示
        path = path.strip()
        if not path:
            return
        # **这里刻意不判"路径不存在"**：网络盘、还没插上的移动盘都是合法来源，
        # 而"源不可用"会在串行驱动器真跑到它的时候被跳过并记一行日志
        # （见 `_start_next_source`）—— 那里才是能判准的时点。
        self._add_source(os.path.normpath(path) if not path.startswith("ftp://") else path)
    # 【阶段三-新增】共享文件夹的入口。
    #
    # 【为什么只留"手打"这一个入口，不给它一个三项菜单】
    #   本地那个三项菜单（选择文件/选择文件夹/手工添加）前两项都要过**系统对话框**，
    #   而系统对话框**到不了一个还没连上的 UNC** —— 用户之所以来点这一项，
    #   十有八九正是因为资源管理器里那次连接还没做/失效了。给他一个打不开的
    #   对话框，比不给更糟（看起来像坏了）。
    #
    # 【为什么刻意不判"路径不存在"】与 `_pick_source_manual` 逐字同源：
    #   网络盘、还没连上的共享都是合法来源；判不准就**别判**，
    #   留给串行驱动器真跑到它的时候去跳过并记一行（`_start_next_source`）。
    #   ⚠️ 这里**不能**"顺手"加一次 `os.path.exists` 做友好提示：
    #   打一个**没碰过的**死主机会冻 **21 秒**（实测，见 `_probe_unc_stat_first.py`），
    #   在模态对话框里冻 21 秒比不提示难受得多。
    def _pick_share_source(self):
        cur = self.source_path.get().strip()
        path = simpledialog.askstring(
            "添加共享文件夹",
            "输入共享路径，形如：\n\n"
            "    \\\\服务器\\共享名\n"
            "    \\\\服务器\\共享名\\子目录\n\n"
            "（口令由 Windows 管理：请先在**资源管理器**里连一次该共享，\n"
            "　本工具不代填、也不代存凭据。）",
            initialvalue=cur if self._is_unc_path(cur) else "", parent=self.root)
        if path is None:
            return                     # 取消：不弹任何提示
        path = path.strip()
        if not path:
            return
        if not self._is_unc_path(path):
            # 不是 UNC 就**原样收下**，只提示一句 —— 不弹错、不拒绝。
            # 理由：`D:\x` 从这个框里进来完全是合法的（用户用它当"手工添加"用），
            # 而串行驱动器的类型写回会把它认成 `"local"`，**行为上没有任何问题**。
            # 反过来"这里必须填 UNC，否则报错"会让一个本来能用的入口变窄。
            self._ui._update_log("[提示] 这不是共享路径（应以 \\\\ 或 // 开头），"
                             "将按**本地路径**处理：%s" % path)
        self._add_source(path)
