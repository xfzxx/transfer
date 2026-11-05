# -*- coding: utf-8 -*-
"""FTP 对话框 —— 第二刀从 file_transfer.py 搬出（见 PROJECT_STATE §7.4）。

搬来两个类，**类体逐字未改**：
    FTPSettingsDialog   FTP 连接设置（含"测试连接"）
    FTPBrowserDialog    FTP 远程目录浏览（选目录 / 选文件）

依赖方向只有一个：ui_dialogs → config。
**本模块绝不 import file_transfer** —— 那会形成
file_transfer → ui_dialogs → file_transfer 的循环依赖。

【为什么写 `import config as app_config`，而不是 `import config`】
本文件里 `config` 这个名字**已经被占用了**：`_test_connection` 与 `_on_save`
各有一个局部变量 `config`（= FTP 连接配置字典，随类一起搬来、逐字未改）。
若按惯例写 `import config`、再在 `_on_save` 里写 `config.save_config(...)`，
那句就成了"对 dict 取属性" → AttributeError ——
**与第一刀 `save_config(config)` 那个 P0 bug 是同一个形态**（见 _checks/_shadow_audit.py）。
用别名 app_config 之后，模块引用与局部名在词法上不可能再撞车；
而搬迁代码一行未动 —— `_verify_split.py` 只需把 `app_config.X` 还原成裸名 `X`，
即可与拆分前的 AST 逐节点对齐（与第一刀的 `config.X` → `X` 同一条 Normalize 思路）。

【为什么不写 `from config import load_last_config, save_config`】
`check_injection_points.py` 的 R5（scan_reexport）**无条件拦截任何生产文件里的
`from config import X`** —— 它是"不 re-export"这条架构决策的可执行守卫，
本文件同样是生产文件，一样受它管。
"""
import ftplib
import re
import tkinter as tk
from tkinter import ttk, messagebox

# 【功能C2】界面主题。本模块原先是"纯 stdlib + config"，加这一行是 C2 的**必要代价**：
#   `tk.Button` **不认** `bootstyle=`（实测 `TclError: unknown option "-bootstyle"`），
#   而用户给 C2 划的范围是"**只给按钮加 bootstyle**，不动控件结构"——
#   于是按钮的**类**必须换成 `ttkbootstrap.Button`，与 C1 在主窗口用的是同一个手法。
#   ⚠️ 这不是"顺手多引入一个依赖"：ttkbootstrap 早已是本项目的唯一第三方依赖
#      （`file_transfer.py` 顶部那行注释），这里只是让**第二个** UI 文件也用它。
#   依赖方向不受影响，反而是它把边界划得更清楚：
#     * `config` 不许 import 它 —— C19 守着（UI 库不许下沉到常量模块）；
#     * `ftp_client` / `state_db` 不许 import 它 —— 纯 worker 层，不碰 UI；
#     * `ui_dialogs` 本来就是 UI 层，它用主题库是本分。
import ttkbootstrap

import config as app_config


# ===================== FTP 设置对话框 =====================
class FTPSettingsDialog(tk.Toplevel):
    def __init__(self, parent, initial_config=None, profile_name=None):
        super().__init__(parent)
        # 【阶段二-新增 profile_name】这套设置**保存回哪一套**。
        #   调用方两种：
        #     * Tab1 的 ⚙ → 传当前激活的那套名字（改的就是当前这套）；
        #     * Tab2 的「编辑…」→ 传**选中的**那一套（不是当前那套 —— 用户选着 B 点编辑，
        #       结果改到了 A，那是静默改错地方）。
        #   `None` = 没有多套概念 → 落回 `save_config` 的整份覆盖（旧行为逐字保留）。
        #   ⚠️ 这个参数会一路传到 `_on_save`，**没有它就会把整个配置文件覆盖成扁平单套**，
        #      把其余几套静默清掉 —— 与 §7.4 那个 P0 是同一个形态。
        # 【阶段二-第三处调用方的兜底】`None` 有**两个来源**，必须分开处理：
        #   ① 调用方没传（`FTPBrowserDialog._open_settings` 原先就是这种）；
        #   ② 磁盘上一份配置都没有（首次运行）。
        #   ① 必须落到"当前 profile"，否则保存会走 `save_config` 把整份配置文件覆盖掉；
        #   ② 必须保持今天的行为，因为那时磁盘上本来就没有 profiles 可言。
        #   `load_profiles()` 正好能分辨这两者 —— 它返回空列表就是 ②，返回非空就是 ①。
        #   ⚠️ **判据是 `_profiles`，不是 `_active`**：`_active` 可能是空串，而空串
        #      falsy 却不是 None，传下去会变成 `save_profile("", cfg)` ——
        #      那条路在 `_clean_profile` 里因为名字为空被判成非法，**静默不保存**：
        #      对话框照常关闭、`result` 照常有值、界面照常回填，只有磁盘上什么都没变。
        #      那比"覆盖"更难查（覆盖至少看得出来配置变了）。
        #   `load_profiles` 内部每段都是裸 `except → return ([], "")`，构造上不抛，
        #   所以这里不需要 try。
        if profile_name is None:
            _profiles, _active = app_config.load_profiles()
            profile_name = _active if _profiles else None
        self.profile_name = profile_name
        self.title("FTP 连接设置" if profile_name is None
                   else "FTP 连接设置 - %s" % profile_name)
        self.geometry("360x360")
        self.resizable(False, False)
        self.result = None

        if initial_config:
            last = initial_config
        else:
            last = app_config.load_last_config()

        tk.Label(self, text="服务器地址:").pack(pady=(10, 0))
        self.host_entry = tk.Entry(self)
        self.host_entry.pack(pady=2, fill="x", padx=10)
        if last: self.host_entry.insert(0, last.get("host", ""))

        tk.Label(self, text="端口:").pack()
        self.port_entry = tk.Entry(self)
        self.port_entry.insert(0, str(last.get("port", 21)) if last else "21")
        self.port_entry.pack(pady=2, fill="x", padx=10)

        tk.Label(self, text="用户名:").pack()
        self.user_entry = tk.Entry(self)
        if last: self.user_entry.insert(0, last.get("user", ""))
        self.user_entry.pack(pady=2, fill="x", padx=10)

        tk.Label(self, text="密码:").pack()
        self.pass_entry = tk.Entry(self, show="*")
        if last: self.pass_entry.insert(0, last.get("password", ""))
        self.pass_entry.pack(pady=2, fill="x", padx=10)

        tk.Label(self, text="远程目录 (如 /pub/data):").pack()
        self.dir_entry = tk.Entry(self)
        self.dir_entry.insert(0, last.get("remote_dir", "/") if last else "/")
        self.dir_entry.pack(pady=2, fill="x", padx=10)

        # ---- 【2026-11-05 FTPS】加密模式 + 自签名 ----
        # 【为什么用 `OptionMenu` 而不是 Combobox】`ttkbootstrap.Combobox` 在替身层
        #   没有对应实现，C3/C4 装配时会炸；而 `OptionMenu` 是本项目已经在用的控件
        #   （源/目标类型那两个下拉就是它），替身层有，两档都安全。
        # 【为什么显示中文标签而不是 none/explicit】这是给医院信息科看的界面，
        #   存的是键（落盘/比较用），显示的是标签（`app_config.TLS_MODE_LABELS`）——
        #   两套字符串只在这一个下拉里做一次映射。
        tk.Label(self, text="加密模式:").pack()
        _tls_now = (last.get("tls_mode") or "none") if last else "none"
        self.tls_var = tk.StringVar(value=app_config.TLS_MODE_LABELS.get(_tls_now, "无（明文）"))
        _tls_menu = tk.OptionMenu(self, self.tls_var,
                                  *[app_config.TLS_MODE_LABELS[m] for m in app_config.TLS_MODES])
        _tls_menu.pack(pady=2, fill="x", padx=10)

        tk.Label(self, text="").pack()          # 一点呼吸空间（这个框本来就偏挤）
        self.selfsigned_var = tk.BooleanVar(
            value=bool(last.get("allow_selfsigned", False)) if last else False)
        tk.Checkbutton(self, text="允许自签名证书（内网自建服务器常见）",
                       variable=self.selfsigned_var).pack(anchor="w", padx=10)

        # 【功能C2】层次感修复 —— 只加 bootstyle，**结构/位置/顺序/文字一个字未动**。
        #   改前实测：这三个按钮的底色**全是主色 `#2780e3`**（`_probe_c2_fit.py` 量的），
        #   也就是用户说的"三蓝一片"—— 三个按钮重量级完全一样，看不出哪个是主操作。
        #   改后：只有「保存」是实心主按钮，另外两个是白底描边（与主窗口的
        #   「浏览」「⚙」同款 `secondary-outline`），主次一眼分得开。
        #   ⚠️ 为什么不是"给 tk.Button 直接加 bootstyle"：它不认这个参数（见文件头 C2 注释）。
        #   ⚠️ 为什么「测试连接」不算主按钮：它是**可选的前置动作**，用户来这个框
        #      九成是为了改完就存；把"测试"也做成实心块，等于又造一次"几块蓝抢视线"。
        btn_frame = tk.Frame(self)
        btn_frame.pack(pady=15, fill="x", padx=10)
        ttkbootstrap.Button(btn_frame, text="测试连接", width=10, command=self._test_connection,
                            bootstyle="secondary-outline").pack(side="left", expand=True, padx=3)
        ttkbootstrap.Button(btn_frame, text="保存", width=10, command=self._on_save,
                            bootstyle="primary").pack(side="left", expand=True, padx=3)
        ttkbootstrap.Button(btn_frame, text="取消", width=10, command=self.destroy,
                            bootstyle="secondary-outline").pack(side="left", expand=True, padx=3)

    def _get_config(self):
        host = self.host_entry.get().strip()
        if not host:
            messagebox.showwarning("警告", "服务器地址不能为空")
            return None
        try:
            port = int(self.port_entry.get().strip())
        except (TypeError, ValueError):  # 端口输入必须是整数
            messagebox.showwarning("警告", "端口必须是数字")
            return None
        user = self.user_entry.get().strip()
        password = self.pass_entry.get().strip()
        remote_dir = self.dir_entry.get().strip()
        if not remote_dir.startswith('/'):
            remote_dir = '/' + remote_dir
        # 标签 → 键。认不出来（用户手改过？）就当明文 —— 与配置清洗的回落一致。
        _rev = {v: k for k, v in app_config.TLS_MODE_LABELS.items()}
        tls_mode = _rev.get(self.tls_var.get(), "none")
        return {
            'host': host,
            'port': port,
            'user': user,
            'password': password,
            'remote_dir': remote_dir,
            # 【2026-11-05 FTPS】选"无"时这两项分别是 'none' / False，
            # 与升级前保存的配置逐字节一致 —— 老库不需要迁移。
            'tls_mode': tls_mode,
            'allow_selfsigned': bool(self.selfsigned_var.get()),
        }

    def _test_connection(self):
        config = self._get_config()
        if not config: return
        try:
            # 【2026-11-05】测试连接必须用**配置里那个加密模式**去连 ——
            #   否则在只开 FTPS 的服务器上，用户填对了却永远"连接失败"。
            _mode = config.get('tls_mode', 'none')
            if _mode in ("explicit", "implicit"):
                import ftp_client                       # 迟导入：避免与界面模块成环
                cls = ftp_client.ftp_class_for(_mode)
                ftp = cls(context=ftp_client.build_tls_context(
                    config.get('allow_selfsigned', False)))
                ftp.connect(config['host'], config['port'], timeout=10)
                ftp_client.secure_session(ftp, _mode, config['host'],
                                          config['user'], config['password'])
            else:
                ftp = ftplib.FTP()
                ftp.connect(config['host'], config['port'], timeout=10)
                ftp.login(config['user'], config['password'])
            # 启用 UTF-8
            try:
                ftp.voidcmd('OPTS UTF8 ON')
            except Exception:  # 可选能力：OPTS UTF8
                pass
            if hasattr(ftp, 'encoding'):
                ftp.encoding = 'utf-8'
            ftp.cwd(config['remote_dir'])
            ftp.nlst()
            ftp.quit()
            messagebox.showinfo("成功", "FTP 连接测试成功！")
        except Exception as e:
            messagebox.showerror("连接失败", f"无法连接到 FTP 服务器：\n{e}")

    def _on_save(self):
        config = self._get_config()
        if config:
            self.result = config
            # 【阶段二-唯一改动的那一行】save_config → save_profile。
            #   profile_name=None 时 save_profile 内部直接转调 save_config，
            #   所以"单套时代"的调用方行为与本行改动前**逐字相同**。
            app_config.save_profile(self.profile_name, config)
            self.destroy()

# ===================== 阶段四：定时任务编辑对话框 =====================
class ScheduleDialog(tk.Toplevel):
    r"""新建 / 编辑一条定时任务。**只管"叫什么、什么时候跑"**。

    【为什么传输参数不在这里编辑】任务的 `params`（源/目标/模式/过滤/线程数…）
    由调用方在打开本框**之前**从界面快照好、原样带进来、原样带出去。
    在框里再放一套路径输入框的后果不是"多几个控件"，而是**第二份真相**：
    用户在「传输」页填了 `D:\A`，在这个框里填了 `D:\B`，保存完切回传输页
    看到的还是 `A` —— 他无法知道任务到点会用哪个。
    现在只有一条规则，且写在标题下面那行字里：
    **任务参数 = 你点保存那一刻「传输」页上的设置。**

    【校验为什么调 `app_config._clean_schedule`】那是**唯一一份**"什么样的任务
    是合法的"的答案。这里另写一份的后果与上面同源：两份白名单迟早漂移，
    而漂移的症状是"界面上能存、任务跑起来不生效"（无人值守时最难查）。
    所以本框只负责"把界面上的东西凑成一个 dict"，**合不合法由 config 说了算**。
    """
    _TYPE_LABELS = [("daily", "每天"), ("weekly", "每周"),
                    ("hourly", "每隔 N 小时"), ("once", "单次")]

    def __init__(self, parent, initial=None, params=None, params_preview=""):
        super().__init__(parent)
        self.result = None
        self.title("新建定时任务" if not initial else "编辑定时任务 - %s"
                   % initial.get("name", ""))
        self.geometry("460x430")
        self.resizable(False, False)
        self.transient(parent)

        init = initial or {}
        self._initial = dict(init)      # 整份留着，保存时在原记录上改（见 `_on_save`）
        trig = init.get("trigger") or {}
        # 调用方没带参数进来时，**回落到原记录那份**，而不是回落成空 dict ——
        # 空 dict 会让"只改个时间"顺手把任务变成"没有源也没有目标"，
        # 而它在界面上看起来只是改了时间。
        self.params = (dict(params) if params is not None
                       else dict(init.get("params") or {}))

        # ---- 任务名 ----
        tk.Label(self, text="任务名（在本工具与「任务计划程序」里都用它）:",
                 anchor="w").pack(fill="x", padx=12, pady=(12, 0))
        self.name_entry = tk.Entry(self)
        self.name_entry.pack(fill="x", padx=12, pady=(2, 0))
        self.name_entry.insert(0, init.get("name", ""))

        # ---- 触发类型 ----
        tk.Label(self, text="什么时候跑:", anchor="w").pack(fill="x", padx=12, pady=(10, 0))
        self.type_var = tk.StringVar(value=trig.get("type", "daily"))
        type_row = tk.Frame(self)
        type_row.pack(fill="x", padx=12, pady=2)
        for code, label in self._TYPE_LABELS:
            tk.Radiobutton(type_row, text=label, value=code, variable=self.type_var,
                           command=self._sync_type).pack(side="left")

        # ---- 时间 ----
        tk.Label(self, text="时间（24 小时制，HH:MM）:", anchor="w").pack(
            fill="x", padx=12, pady=(8, 0))
        self.time_entry = tk.Entry(self, width=10)
        self.time_entry.pack(anchor="w", padx=12, pady=2)
        self.time_entry.insert(0, trig.get("time", "02:30"))

        # ---- 每周：星期几 ----
        self.days_frame = tk.Frame(self)
        self.days_frame.pack(fill="x", padx=12)
        tk.Label(self.days_frame, text="星期几（可多选）:", anchor="w").pack(fill="x")
        day_row = tk.Frame(self.days_frame)
        day_row.pack(fill="x", pady=2)
        _cn = {"MON": "一", "TUE": "二", "WED": "三", "THU": "四",
               "FRI": "五", "SAT": "六", "SUN": "日"}
        have = set(trig.get("days") or [])
        self.day_vars = {}
        for code in app_config.SCHEDULE_DAY_CODES:
            v = tk.BooleanVar(value=(code in have))
            self.day_vars[code] = v
            tk.Checkbutton(day_row, text="周" + _cn[code], variable=v).pack(side="left")

        # ---- 每隔 N 小时 ----
        self.hours_frame = tk.Frame(self)
        self.hours_frame.pack(fill="x", padx=12)
        tk.Label(self.hours_frame, text="每隔几小时（1~23）:", anchor="w").pack(fill="x")
        self.hours_var = tk.IntVar(value=int(trig.get("every_hours", 4) or 4))
        ttkbootstrap.Spinbox(self.hours_frame, from_=1, to=23, width=5,
                             textvariable=self.hours_var).pack(anchor="w", pady=2)

        # ---- 单次：日期 ----
        self.date_frame = tk.Frame(self)
        self.date_frame.pack(fill="x", padx=12)
        tk.Label(self.date_frame, text="日期（MM/DD/YYYY）:", anchor="w").pack(fill="x")
        self.date_entry = tk.Entry(self.date_frame, width=14)
        self.date_entry.pack(anchor="w", pady=2)
        self.date_entry.insert(0, trig.get("date", ""))

        # ---- 参数预览（只读，来自调用方）----
        tk.Label(self, text="这个任务会用到的传输设置（保存那一刻的「传输」页）:",
                 anchor="w", fg="#666666").pack(fill="x", padx=12, pady=(10, 0))
        tk.Label(self, text=params_preview or "（未从界面取到任何参数）",
                 anchor="w", justify="left", fg="#666666",
                 wraplength=430).pack(fill="x", padx=12)
        tk.Label(self, text="保存后**立即**写入系统计划任务（已存在的同名任务会被覆盖）。",
                 anchor="w", fg="#666666", wraplength=430,
                 justify="left").pack(fill="x", padx=12, pady=(6, 0))

        btn_frame = tk.Frame(self)
        btn_frame.pack(side="bottom", pady=12, fill="x", padx=12)
        ttkbootstrap.Button(btn_frame, text="保存", width=10, command=self._on_save,
                            bootstyle="primary").pack(side="left", expand=True, padx=3)
        ttkbootstrap.Button(btn_frame, text="取消", width=10, command=self.destroy,
                            bootstyle="secondary-outline").pack(side="left", expand=True, padx=3)

        self._sync_type()

    def _sync_type(self):
        """只把当前类型用得上的那一组控件放出来。

        【为什么是 pack/pack_forget 而不是全摆着】摆着的话，"每周"那一排星期
        会跟"每隔 N 小时"的 Spinbox 同时出现 —— 用户没有任何办法知道哪个作数。
        这里**不改任何控件的值**，只改它们在不在这屏上；
        切回原来的类型，刚才填的值原样还在（变量没动过）。
        """
        t = self.type_var.get()
        self.days_frame.pack_forget()
        self.hours_frame.pack_forget()
        self.date_frame.pack_forget()
        if t == "weekly":
            self.days_frame.pack(fill="x", padx=12)
        elif t == "hourly":
            self.hours_frame.pack(fill="x", padx=12)
        elif t == "once":
            self.date_frame.pack(fill="x", padx=12)

    def _on_save(self):
        t = self.type_var.get()
        trig = {"type": t, "time": self.time_entry.get().strip()}
        if t == "weekly":
            trig["days"] = [c for c in app_config.SCHEDULE_DAY_CODES
                            if self.day_vars[c].get()]
        elif t == "hourly":
            try:
                trig["every_hours"] = int(self.hours_var.get())
            except Exception:
                trig["every_hours"] = None
        elif t == "once":
            trig["date"] = self.date_entry.get().strip()

        # 【为什么从原记录整份出发，而不是白手起家拼一个 dict】
        #   `_clean_schedule` 会**丢掉**它认不出的键 —— 其中包括 `last_run` /
        #   `last_status` / `last_counts` / `last_log` / `exit_code` 这一整组运行痕迹。
        #   白手起家拼的话，"把每天 02:30 改成 03:00"这个动作会**顺手抹掉
        #   "上次运行：部分失败（成功 118 / 失败 3）"** —— 用户改完时间，
        #   那条最该看的记录就没了，而且没有任何提示说它没了。
        #   从原记录出发，改的只有 name/trigger 两项，其余原样过一遍。
        rec = dict(self._initial)
        rec["name"] = self.name_entry.get().strip()
        rec["trigger"] = trig
        clean = app_config._clean_schedule(rec)
        if clean is None:
            # 不合法就**留在框里**让用户改，而不是关掉再把错丢给调用方 ——
            # 关掉的话用户填的东西全没了，还得重新填一遍才发现是哪里不合法。
            messagebox.showwarning(
                "这项填写不合法",
                "请检查：\n"
                "  · 任务名不能为空、不能含 \\ / : * ? \" < > | 这些字符，不超过 60 字；\n"
                "  · 时间要写成 24 小时制的 HH:MM（如 02:30，不能写 2:30）；\n"
                "  · 每周至少要勾一天；间隔是 1~23 的整数；\n"
                "  · 单次的日期要写成 MM/DD/YYYY（如 09/20/2026）。")
            return
        # 参数以调用方带进来的那份为准（它是"保存那一刻的传输页"，比原记录新）。
        clean["params"] = self.params
        self.result = clean
        self.destroy()

# ===================== FTP 远程浏览对话框 =====================
class FTPBrowserDialog(tk.Toplevel):
    def __init__(self, parent, ftp_config, select_mode="dir", initial_dir="/"):
        super().__init__(parent)
        self.ftp_config = ftp_config.copy()
        self.select_mode = select_mode
        self.initial_dir = initial_dir if initial_dir else "/"
        self.result = None
        self.ftp = None
        self._loaded = set()

        self.title("FTP 远程浏览")
        self.geometry("500x450")

        self.tree = ttk.Treeview(self, show="tree")
        self.tree.heading("#0", text="远程目录结构", anchor="w")
        self.tree.pack(fill="both", expand=True, padx=5, pady=5)

        self.tree.bind("<Double-1>", self._on_double_click)

        # 【功能C2】同上，只加 bootstyle。改前实测这 4 个按钮底色**全是** `#2780e3`。
        #   这一行的层次比设置框多一档，说明见下面「选择当前文件」那行的注释。
        bottom_frame = tk.Frame(self)
        bottom_frame.pack(pady=10, fill="x", padx=5)
        ttkbootstrap.Button(bottom_frame, text="选择当前目录", command=self._select_current_dir,
                            bootstyle="primary").pack(side="left", padx=5)
        if select_mode == "file":
            # 这一档只在 `select_mode == "file"` 时**和上面那个同时出现**，所以不能也做成
            # 实心主按钮 —— 两个并排的实心块就是"几块蓝"，正是 C2 要修的东西。
            # 但它俩是**同一角色**（都是"就用当前选中的这个"），谁也不能被降成次要 →
            # 用 `primary-outline`：同一个主色的描边版，重量级在「实心」与「次要」之间。
            # （当前 app 只会用 `select_mode="dir"`，这条分支走不到；但代码在，就得对。）
            ttkbootstrap.Button(bottom_frame, text="选择当前文件", command=self._select_current_file,
                                bootstyle="primary-outline").pack(side="left", padx=5)
        ttkbootstrap.Button(bottom_frame, text="FTP 设置", command=self._open_settings,
                            bootstyle="secondary-outline").pack(side="left", padx=5)
        ttkbootstrap.Button(bottom_frame, text="取消", command=self.destroy,
                            bootstyle="secondary-outline").pack(side="right", padx=5)

        self._connect_and_build()

    def _connect_and_build(self):
        if self.ftp:
            try:
                self.ftp.quit()
            except Exception:  # 关闭旧连接，失败无所谓
                pass
        try:
            self.ftp = ftplib.FTP()
            self.ftp.connect(self.ftp_config['host'], self.ftp_config['port'], timeout=10)
            self.ftp.login(self.ftp_config['user'], self.ftp_config['password'])
            # 启用 UTF-8
            try:
                self.ftp.voidcmd('OPTS UTF8 ON')
            except Exception:  # 可选能力：OPTS UTF8
                pass
            if hasattr(self.ftp, 'encoding'):
                self.ftp.encoding = 'utf-8'
        except Exception as e:
            messagebox.showerror("连接失败", str(e))
            self.destroy()
            return

        for item in self.tree.get_children():
            self.tree.delete(item)
        self._loaded.clear()

        self.tree.insert("", "end", "/", text="/", open=False)
        self._load_children("/")
        self.tree.bind("<<TreeviewOpen>>", self._on_user_open)
        self.after(100, self._navigate_to_dir, self.initial_dir)

    def _open_settings(self):
        # 【阶段二-显式指名】这一处是 `FTPSettingsDialog` 的**第三个调用方**，
        #   在阶段二之前它传不出 `profile_name` —— 于是保存走 `save_config`
        #   把整份配置文件覆盖成扁平单套，**其余几套静默消失**（`_probe_profile_clobber.py`
        #   实测：2 套 → 1 套「默认」，磁盘顶层键从 profiles 列表变成扁平 5 键）。
        #   这里显式指名，与上面 `__init__` 里那句解析**结果等价、是冗余的** ——
        #   冗余是刻意的：`__init__` 那层保证"任何不传名字的调用方都掉不进
        #   `save_config`"，这一层保证"读代码的人在这一行就能看出写回哪一套"。
        #   判据同样是 `_profiles` 而非 `_active`（空串不是 None，见 `__init__` 的注释）。
        _profiles, _active = app_config.load_profiles()
        dlg = FTPSettingsDialog(self, initial_config=self.ftp_config,
                                profile_name=_active if _profiles else None)
        self.wait_window(dlg)
        if dlg.result:
            self.ftp_config.update(dlg.result)
            self._connect_and_build()

    def _load_children(self, path):
        if path in self._loaded:
            return self.tree.get_children(path)

        for child in self.tree.get_children(path):
            self.tree.delete(child)

        children = []
        try:
            self.ftp.cwd(path)
            lines = []
            try:
                self.ftp.retrlines('MLSD', lines.append)
            except ftplib.error_perm:
                lines = []
                self.ftp.retrlines('LIST', lines.append)
                unix_pattern = re.compile(r'^([\-d])([rwx\-]{9})\s+\d+\s+\w+\s+\w+\s+(\d+)\s+\w+\s+\d+\s+[\d:]+')
                win_dir_pattern = re.compile(r'^(\d{2}-\d{2}-\d{2})\s+(\d{2}:\d{2}[AP]M)\s+<DIR>\s+(.+)$')
                win_file_pattern = re.compile(r'^(\d{2}-\d{2}-\d{2})\s+(\d{2}:\d{2}[AP]M)\s+(\d+)\s+(.+)$')
                for line in lines:
                    line = line.rstrip()
                    if not line:
                        continue
                    unix_match = unix_pattern.match(line)
                    if unix_match:
                        filetype = unix_match.group(1)
                        name_start = unix_match.end()
                        name = line[name_start:].strip()
                        if name in ('.', '..'):
                            continue
                        full = (path.rstrip('/') + '/' + name) if path != '/' else ('/' + name)
                        if filetype == 'd':
                            node = self.tree.insert(path, "end", full, text=name, open=False)
                            self.tree.insert(node, "end", full + "/dummy", text="dummy")
                            children.append(node)
                        elif filetype == '-' and self.select_mode == "file":
                            self.tree.insert(path, "end", full, text=name, tags=("file",))
                        continue
                    win_dir_match = win_dir_pattern.match(line)
                    if win_dir_match:
                        name = win_dir_match.group(3).strip()
                        if name in ('.', '..'):
                            continue
                        full = (path.rstrip('/') + '/' + name) if path != '/' else ('/' + name)
                        node = self.tree.insert(path, "end", full, text=name, open=False)
                        self.tree.insert(node, "end", full + "/dummy", text="dummy")
                        children.append(node)
                        continue
                    win_file_match = win_file_pattern.match(line)
                    if win_file_match and self.select_mode == "file":
                        name = win_file_match.group(4).strip()
                        full = (path.rstrip('/') + '/' + name) if path != '/' else ('/' + name)
                        self.tree.insert(path, "end", full, text=name, tags=("file",))
                        continue
                    parts = line.split()
                    if len(parts) < 9:
                        continue
                    name = ' '.join(parts[8:])
                    if name in ('.', '..'):
                        continue
                    full = (path.rstrip('/') + '/' + name) if path != '/' else ('/' + name)
                    if parts[0].startswith('d'):
                        node = self.tree.insert(path, "end", full, text=name, open=False)
                        self.tree.insert(node, "end", full + "/dummy", text="dummy")
                        children.append(node)
                    elif parts[0].startswith('-') and self.select_mode == "file":
                        self.tree.insert(path, "end", full, text=name, tags=("file",))
                self._loaded.add(path)
                return children

            for line in lines:
                parts = line.split(';')
                name = parts[-1].strip()
                if name in ('.', '..'):
                    continue
                attrs = {}
                for part in parts[:-1]:
                    if '=' in part:
                        k, v = part.strip().split('=', 1)
                        attrs[k.lower()] = v
                item_type = attrs.get('type', '')
                full = (path.rstrip('/') + '/' + name) if path != '/' else ('/' + name)
                if item_type == 'dir':
                    node = self.tree.insert(path, "end", full, text=name, open=False)
                    self.tree.insert(node, "end", full + "/dummy", text="dummy")
                    children.append(node)
                elif item_type == 'file' and self.select_mode == "file":
                    self.tree.insert(path, "end", full, text=name, tags=("file",))
            self._loaded.add(path)
        except ftplib.error_perm:
            self.tree.insert(path, "end", path + "/noaccess", text="<无权限>", tags=("error",))
        return children

    def _on_user_open(self, event):
        item = self.tree.focus()
        if not item:
            return
        children = self.tree.get_children(item)
        if len(children) == 1 and self.tree.item(children[0], "text") == "dummy":
            self.tree.delete(children[0])
            self._load_children(item)

    def _navigate_to_dir(self, target_path):
        target_path = target_path.rstrip("/") or "/"
        if target_path == "/":
            self.tree.selection_set("/")
            self.tree.see("/")
            return
        parts = [p for p in target_path.split("/") if p]
        current_node = "/"
        for part in parts:
            if not self.tree.item(current_node, "open"):
                self.tree.item(current_node, open=True)
            self._load_children(current_node)
            self.update_idletasks()
            found = None
            for child in self.tree.get_children(current_node):
                if self.tree.item(child, "text") == part:
                    found = child
                    break
            if not found:
                self.tree.selection_set(current_node)
                self.tree.see(current_node)
                messagebox.showwarning("浏览提示", f"目录 '{part}' 不存在或无权限访问", parent=self)
                return
            current_node = found
        self.tree.selection_set(current_node)
        self.tree.see(current_node)

    def _on_double_click(self, event):
        item = self.tree.focus()
        if not item:
            return
        if self.tree.item(item, "tags") == ("file",) and self.select_mode == "file":
            self.result = item
            self.destroy()
        else:
            if self.tree.item(item, "open"):
                self.tree.item(item, open=False)
            else:
                self.tree.item(item, open=True)

    def _select_current_dir(self):
        item = self.tree.focus()
        if not item:
            messagebox.showwarning("提示", "请先选择一个目录")
            return
        if self.tree.item(item, "tags") == ("file",):
            item = self.tree.parent(item)
        self.result = item
        self.destroy()

    def _select_current_file(self):
        if self.select_mode != "file":
            return
        item = self.tree.focus()
        if not item:
            messagebox.showwarning("提示", "请先选择一个文件")
            return
        if self.tree.item(item, "tags") == ("file",):
            self.result = item
            self.destroy()
        else:
            messagebox.showwarning("提示", "请选择一个文件节点")
