"use strict";

/* 文件传输工具 — Web 前端（纯 HTML/CSS/JS，无框架、无构建器）。
 *
 * 后端契约（web_app.py）：
 *   GET  /api/state       → 界面快照 {title, vars, widgets, source_list, ftp_profiles, ftp_active, messages}
 *   POST /api/invoke      → {action, target?, value?, name?, path?, index?} → {ok, result|error}
 *                           （action="semantic" 时 name 的取值表 = web_app.WebBridge._semantic，
 *                             含 add_source / add_source_text / remove_source /
 *                             reset_syncpoint / export_log / refresh_sched / sched_reload /
 *                             sched_form / sched_save / sched_delete / sched_copy_cmd /
 *                             profile_new / profile_edit / profile_delete / profile_set_active）
 *   POST /api/dialog      → {id, answer} → {ok}      （应答后端弹框）
 *   GET  /api/fs?path=..  → {path, parent, dirs[, files]} | {ok:false, error}（files=1 才列文件）
 *   GET  /api/fs/drives   → ["C:\\", ...]
 *   POST /api/pick        → {kind, initial} → {ok, path, cancelled} | {ok:false, error}
 *                           （系统原生对话框；本地路径的主路。失败时前端退回自绘框）
 *   GET  /api/ftp?path=.. → {path, parent, dirs, files} | {ok:false, error}（真连远端服务器）
 *   POST /api/ftp/test    → {ok, message}            （「测试连接」，纯网络、不占主循环）
 *
 * 刷新模型：每 500ms 拉一次全量快照重渲染（不做 SSE/WS —— 见项目纪律）。
 * 渲染原则：正在被用户编辑的控件**不覆盖**（否则打字打到一半会被拽回去）。
 *
 * 弹框模型（对应后端 WebBridge._dialog）：快照里的 `messages` 是流水 ——
 *   · needs_answer=true（ask*）：还没答的渲染成模态框，点按钮 POST /api/dialog；
 *   · 通知类（info/warning/error）：每条新出现的弹一次 toast。
 */

/* 轮询改成 SSE 之后，这里只是**兜底通道**的节奏（见 startEventStream）。
 * SSE 正常时一帧都不发；只有 EventSource 没建立/断了才按这个间隔补。 */
const POLL_MS = 5000;
let state = null;
let logClearedTo = 0;
let dialogShownId = 0;
let tickInFlight = false;
let eventSource = null;
const seenNotices = new Set();
const picker = { mode: "dest", path: "" };

/* 设置页的**草稿**。`values` 里只放"改过、还没保存"的键 —— 500ms 快照轮询必须跳过
 * 这些键，否则用户正在填的值每半秒被后端旧值拽回去一次（打字打到一半跳字）。
 * `errors` 是保存失败时后端逐键回的"人话"，用来标红与在保存栏里列出来。 */
const draft = { values: new Map(), errors: {} };

/* 设置页字段的中文名：保存失败时后端只给键名（`ftp_timeout`），直接摆给用户看
 * 等于让他去猜这是哪一格。这张表只用于**显示**，不参与任何校验。 */
const SETTING_LABELS = {
  thread_count: "并发线程数",
  ftp_timeout: "连接超时",
  retry_count: "文件重试次数",
  reconnect_count: "断线重连次数",
  db_keep_days: "保留天数",
  log_level: "日志级别",
  log_export_dir: "日志备份目录",
};

const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));

/* ---------------- 与后端说话 ---------------- */

async function apiState() {
  const r = await fetch("/api/state", { cache: "no-store" });
  if (!r.ok) throw new Error("state " + r.status);
  return r.json();
}

async function invoke(payload, quiet) {
  const r = await fetch("/api/invoke", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
  const j = await r.json();
  /* `quiet`：调用方自己会把错误显示在**更近的地方**（如配置表单里的 #pf-hint），
   * 就不用再弹一次顶栏 toast —— 同一句错说两遍会让人以为是两次失败。 */
  if (!j.ok && !quiet) toast(j.error || "操作失败");
  return j;
}

/* 语义动作的统一取用口：`/api/invoke` 的回执是**信封**（`{ok, result|error}`），
 * 而语义动作自己要说的那件事在 `result` 里（例如 `sched_save` 返回 `{ok:false, error}`）。
 * 直接看信封会拿到"请求没坏"而不是"这件事成没成" —— 两者恰好都有个 `ok` 字段，
 * 混淆的症状是"保存失败却当成功把框关了"。 */
async function semantic(name, payload, quiet) {
  const j = await invoke({ action: "semantic", name, payload: payload || {} }, quiet);
  return (j && j.result !== undefined) ? j.result : j;
}

async function postDialog(id, answer) {
  try {
    await fetch("/api/dialog", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ id, answer }),
    });
  } catch (e) { toast("应答失败：" + e.message); }
  dialogShownId = 0;   // 允许下一条待答弹框显示
}

/* ---------------- 小工具 ---------------- */

function toast(msg) {
  const el = $("#conn-badge");
  el.textContent = String(msg).slice(0, 60);
  el.className = "badge badge-err";
  /* ⚠️ 这个徽章同时承担"连接状态"和"一句话提示"两个角色，而 `tick()` **每 500ms**
   * 都会 `setConn(true)`。原先 toast 刚写上去就被下一拍盖回「已连接」——
   * 所有提示**最多存活半秒**，看起来像"点了没反应"（本轮真浏览器体检抓到的）。
   * 现在记一个到期时刻，`setConn(true)` 在提示期内让位；断开仍然照实显示。 */
  toast._until = Date.now() + 2500;
  clearTimeout(toast._t);
  toast._t = setTimeout(() => { toast._until = 0; setConn(true); }, 2500);
}

function setConn(ok) {
  if (ok && Date.now() < (toast._until || 0)) return;
  const el = $("#conn-badge");
  el.textContent = ok ? "已连接" : "已断开";
  el.className = "badge " + (ok ? "badge-ok" : "badge-err");
}

/* 常驻提示：错误/警告留在这儿，直到用户点「知道了」。
 * toast 只负责"刚才发生了一件事"，这条负责"用户离开屏幕十分钟回来还看得见"。 */
function showNotice(kind, text) {
  const bar = $("#notice-bar");
  const isErr = /error/i.test(kind || "");
  bar.className = "notice" + (isErr ? "" : " notice-warn");
  $("#notice-text").textContent = String(text || "");
  bar.hidden = false;
}

function hideNotice() { $("#notice-bar").hidden = true; }

const editing = (el) => el === document.activeElement;
const wmodel = (name) => (state && state.widgets && state.widgets[name]) || null;
const wopts = (name) => (wmodel(name) || {}).opts || {};
const wtext = (name) => {
  const m = wmodel(name);
  if (!m) return "";
  if (m.kind === "text") return m.text || "";
  return wopts(name).text || "";
};

/* ---------------- 渲染 ---------------- */

function render() {
  if (!state) return;
  document.title = state.title || "文件传输工具";
  $("#app-title").textContent = state.title || "文件传输工具";
  renderHeader();

  renderVars();
  renderWidgetStates();
  renderSources();
  renderProfiles();
  renderProgress();
  renderLog();
  renderSchedule();
  renderDialogs();
}

function renderHeader() {
  const ver = state.version || "";
  $("#ver-badge").textContent = ver ? ("v" + ver) : "";
  const mode = ((state.runtime || {}).mode || "").toLowerCase();
  const modeEl = $("#mode-badge");
  if (mode === "c3") {
    modeEl.textContent = "WebView2 窗口";
    modeEl.className = "badge badge-ok";
  } else if (mode === "c4") {
    modeEl.textContent = "系统浏览器";
    modeEl.className = "badge";
  }
  const rt = state.runtime || {};
  $("#about-line").textContent = "版本 v" + (ver || "?")
    + "　运行方式：" + (rt.mode === "c3" ? "WebView2 窗口" : (rt.mode === "c4" ? "系统浏览器" : "测试装配"))
    + (rt.port ? ("　端口：" + rt.port) : "")
    + "　数据目录：" + (state.data_dir || "（见诊断包）");
}

function renderVars() {
  const vars = state.vars || {};
  const ranges = state.ranges || {};
  $$("[data-var]").forEach((el) => {
    const key = el.dataset.var;
    /* 数值框的合法区间由后端发过来（Tk 档是 Spinbox 的 from_/to，读的就是 config 里
     * 那几个 RANGE 常量）。**不抄进 HTML** —— 抄一份就是第二份真相：config 改了区间，
     * HTML 里那份还在按老区间卡人，而没有任何地方会报错。 */
    const rk = el.dataset.range;
    if (rk && ranges[rk]) {
      el.min = ranges[rk][0];
      el.max = ranges[rk][1];
    }
    if (!(key in vars)) return;
    /* 【草稿优先】这个键有未保存的改动 ⇒ **所有**同键控件都不刷新。
     * 只按 `activeElement` 跳过是不够的：用户点一下空白处（焦点离开输入框）后，
     * 下一拍快照就会把草稿值拽回旧值 —— 看起来像"改了但没记住"。 */
    if (draft.values.has(key)) return;
    if (editing(el)) return;
    const val = vars[key];
    if (el.type === "radio") {
      el.checked = String(val) === el.value;
    } else if (el.type === "checkbox") {
      el.checked = !!val && String(val) !== "0" && String(val).toLowerCase() !== "false";
    } else {
      el.value = val === null || val === undefined ? "" : val;
    }
  });
}

/* 按钮的**禁用态**也必须跟着后端走（老版靠 `configure(state="disabled")`：
 * 传输一开跑，「开始」灰掉、「停止/暂停」亮起来）。不渲染的话，Web 档
 * 五个按钮永远可点 —— 点「停止」时后端只会当成"没在跑"，看起来像没反应。 */
function renderWidgetStates() {
  $$("[data-click]").forEach((el) => {
    const m = wmodel(el.dataset.click);
    if (!m) return;
    el.disabled = String(m.state || "").indexOf("disabled") >= 0;
  });
  /* 「启/停」这两个字是后端随选中项改写的（`_sched_on_select` 里
   * `configure(text="停用"/"启用")`）—— 前端不能把它写死。 */
  const toggle = $("#sched-toggle");
  if (toggle) {
    const label = wtext("sched_toggle_btn");
    if (label) toggle.textContent = label;
  }
  /* 「暂停/继续」是**同一个按钮**：`_toggle_pause` 在暂停时把它写成
   * `configure(text="继续")`。这里必须跟「启/停」一样从模型里取 —— 写死成
   * 「暂停」的话，用户暂停后看到的仍是「暂停」，会以为"再点一次是重新暂停"，然后去点
   * 灰掉的「开始传输」（任务活动期间它本来就不可点），现象就是"点开始没反应"。
   * 按钮的 `state` 每轮都在同步（上面那段），文字漏同步等于只修了一半。 */
  const pause = $('[data-click="pause_btn"]');
  if (pause) {
    const label = wtext("pause_btn");
    if (label) pause.textContent = label;
  }
  /* 任务进行中时「开始传输」是灰的：把原因挂在 title 上，别让人反复点。
   * 暂停状态下恢复的正确入口是那个已经变成「继续」的按钮。 */
  const start = $('[data-click="start_btn"]');
  if (start) {
    start.title = start.disabled ? "任务进行中：要恢复请点「继续」，开始只用于新任务" : "";
  }
}

function renderSources() {
  const m = wmodel("source_listbox");
  const ul = $("#source-list");
  const items = (m && m.items) || state.source_list || [];
  /* ⚠️ `selection` 是**标量下标**（int 或 null），不是数组 —— 写成 `new Set(m.selection)`
   * 会在"选中了某一行"时抛 `TypeError: number 1 is not iterable`，而这一句在 `render()`
   * 里 ⇒ **整个界面停更**（进度、日志、列表全不动，连接徽章变成"未连接"）。
   * 症状像"服务挂了"，根因在这一行。三个列表渲染器统一走 `selIdx()`。 */
  const sel = selIdx(m);
  ul.innerHTML = "";
  items.forEach((it, i) => {
    const li = document.createElement("li");
    li.textContent = it;
    if (i === sel) li.classList.add("is-sel");
    li.title = it;
    li.onclick = () => invoke({ action: "select_list", target: "source_listbox", index: i });
    ul.appendChild(li);
  });
}

function renderProfiles() {
  const m = wmodel("profile_listbox");
  const ul = $("#profile-list");
  const items = (m && m.items) || [];
  /* 选中项以后端那个 Listbox 的 selection 为准（它就是「编辑…/删除/设为当前」的作用对象）。
   * ⚠️ 这个函数里踩过两个坑，都记在这儿：
   *   1) 不能用 `it === state.ftp_active` 判断 —— 那**永远不会成立**：item 文本带
   *      「● 」/三个空格前缀（见 transfer_form._refresh_profile_list），而 ftp_active 是纯名字。
   *      症状是"当前那一套"从来不高亮：一行死代码，不报错。
   *   2) `selection` 是**标量下标**（web_app._widget_model 透出的就是替身的 `_curselection`，
   *      一个 int，或没选中时的 null），**不是** Tk `curselection()` 那个元组。
   *      当成数组用会两头都错：`new Set(1)` 抛 TypeError（render 整个挂掉 ⇒ 进度/日志/列表
   *      跟着一起停更），而 `0` 又被 `|| []` 吃掉（选第一行永远不高亮）。
   *      这两种症状同样都不报错、只是"界面上看不见"—— 靠无头浏览器真点一遍才抓出来的。
   */
  const sel = profileSelection(m);
  ul.innerHTML = "";
  items.forEach((it, i) => {
    const li = document.createElement("li");
    li.textContent = it;
    if (i === sel) li.classList.add("is-sel");
    li.title = it;
    li.onclick = () => invoke({ action: "select_list", target: "profile_listbox", index: i });
    /* 双击 = 「编辑」（与「编辑…」按钮同一个入口）。
     * 用**行号**先选中再打开：编辑的保存走 `profile_edit` 语义，它读的是后端
     * Listbox 的当前选中项，不能只靠前端这份行号。双击的两下 click 与这条是同一批
     * 事件，等 `select_list` 回来再开框，避免保存时改到上一行。 */
    li.ondblclick = async () => {
      await invoke({ action: "select_list", target: "profile_listbox", index: i });
      openProfileForm("edit", i);
    };
    ul.appendChild(li);
  });
  const sum = $("#profile-summary");
  sum.textContent = wtext("profile_summary") ||
    (items.length ? "" : "还没有配置。点「新建…」添加一套 FTP 连接信息。");
}

/* ---------------- FTP 配置表单（新建 / 编辑） ----------------
 *
 * 【为什么不能像「删除」那样只发一个语义动作】新建/编辑在中段要用一个 Tk 对话框
 * （`ui_dialogs.FTPSettingsDialog`），而 Web 档下 `tk.Toplevel` 只是个替身：画不出窗口、
 * `wait_window` 立刻返回。所以这一档把"取值"搬进本表单，取值之后交给后端同一个入口
 * （`_profile_save_fields`）—— 保存与刷新那半截两档**共用**。
 * 【名字为什么编辑时是只读的】Tk 档的编辑对话框里**根本没有名字这一项**（名字是
 * "选中的那一套"），所以这里也不给改 —— 改名等于另一套，不该悄悄发生。
 * 【失败为什么不关框】后端的 `error` 就是给用户看的那句话（"端口必须是数字"），
 * 显示在框里、改一个字符再点保存即可；关掉框会让人从头再填一遍。
 */
const profileForm = { mode: "new" };

function profileRows() { return (state && state.ftp_profiles) || []; }

/* 把后端那个 Listbox 的选中项读成"下标或 null"。
 * 后端给的是标量（int / null，见 renderProfiles 那段注释）；这里顺手把字符串数字也认了，
 * 免得将来某处把下标塞成 "1" 时静默失效。 */
function profileSelection(box) {
  const raw = box && box.selection;
  if (raw === null || raw === undefined || raw === "") return null;
  const n = Number(raw);
  return Number.isFinite(n) ? n : null;
}

/* 三个列表（源 / FTP 配置 / 计划任务）**统一**用它取选中项 —— 它们是同一种东西，
 * 而它踩过的坑（标量 vs 数组）值得只踩一次。 */
function selIdx(box) { return profileSelection(box); }
function wsel(boxName) { return profileSelection(wmodel(boxName)); }

function selectedProfileIdx() {
  const idx = profileSelection(wmodel("profile_listbox"));
  const rows = profileRows();
  if (idx === null || !rows[idx]) return null;
  return idx;
}

async function withSelectedProfile(semanticName) {
  /* 删除 / 设为当前都作用于**列表里选中的那一套**（与 Tk 档"选着 B 就改 B"同一条规矩）。
   * 所以先把前端正显示着的那个选中项确定地送回后端（`select_list` 改的就是同一个替身控件），
   * 再触发动作 —— 省这一步的话，两边的"选中项"会有最多一个轮询周期（500ms）的时差，
   * 用户点得够快就会作用到上一套上。 */
  const idx = selectedProfileIdx();
  if (idx === null) { toast("请先在列表里选择一套配置。"); return; }
  await invoke({ action: "select_list", target: "profile_listbox", index: idx });
  await invoke({ action: "semantic", name: semanticName });
}

/* `rowIdx` 只给「⚙」用：它刚把选中项写给后端，而本地 `state` 要等下一次轮询
 * （最多 500ms）才更新 —— 此时按 `selectedProfileIdx()` 取会拿到**旧选中项**，
 * 症状是点了源/目标旁边那个 ⚙，打开的却是列表里上一次选中的那一套。 */
function openProfileForm(mode, rowIdx) {
  const rows = profileRows();
  let row = null;
  if (mode === "edit") {
    const idx = (rowIdx === undefined || rowIdx === null)
      ? selectedProfileIdx() : rowIdx;
    if (idx === null || !rows[idx]) { toast("请先在列表里选择一套配置。"); return; }
    row = rows[idx];
  }
  profileForm.mode = mode;
  const nameEl = $("#pf-name");
  nameEl.value = row ? (row.name || "") : "";
  nameEl.readOnly = mode === "edit";
  $("#pf-title").textContent = row ? ("编辑 FTP 配置 - " + (row.name || "")) : "新建 FTP 配置";
  $("#pf-host").value = row ? (row.host || "") : "";
  $("#pf-port").value = row ? (row.port || 21) : 21;
  $("#pf-user").value = row ? (row.user || "") : "";
  $("#pf-pass").value = row ? (row.password || "") : "";
  $("#pf-dir").value = row ? (row.remote_dir || "/") : "/";
  $("#pf-hint").textContent = "";
  $("#profile-form").hidden = false;
  (mode === "edit" ? $("#pf-host") : nameEl).focus();
}

function closeProfileForm() { $("#profile-form").hidden = true; }

async function saveProfileForm() {
  const isNew = profileForm.mode === "new";
  const j = await invoke({
    action: "semantic",
    name: isNew ? "profile_new" : "profile_edit",
    payload: {
      name: $("#pf-name").value,
      host: $("#pf-host").value,
      port: $("#pf-port").value,
      user: $("#pf-user").value,
      password: $("#pf-pass").value,
      remote_dir: $("#pf-dir").value,
    },
  }, true);                       /* quiet：错误显示在 #pf-hint，不再弹一次 toast */
  if (j && j.ok) { closeProfileForm(); return; }
  $("#pf-hint").textContent = (j && j.error) || "保存失败";
}

function renderProgress() {
  const o = wopts("progress");
  const value = Number(o.value || 0);
  const max = Number(o.maximum || 0) || 1;
  const indet = o.mode === "indeterminate";
  const box = $(".progress");
  box.classList.toggle("is-indet", indet);
  const pct = indet ? 0 : Math.max(0, Math.min(100, (value / max) * 100));
  if (!indet) $("#prog-fill").style.width = pct.toFixed(1) + "%";
  $("#prog-text").textContent = indet ? "扫描中…" : `${value} / ${o.maximum || 0}`;
  $("#speed-text").textContent = wtext("speed_label") || "";
  $("#sync-text").textContent = wtext("sync_label") || "";
}

/* 日志筛选只看**显示**这一层：`log_text` 本身一个字不动，导出拿到的仍是完整正文。
 * 反过来做（在后端真删行）会让"筛选一下再导出"悄悄丢内容，而用户看不见这件事。 */
const LOG_ERR_RE = /失败|错误|异常|警告|error|fail|exception/i;

function renderLog() {
  const pre = $("#log-text");
  const full = wtext("log_text") || "";
  if (full.length < logClearedTo) logClearedTo = 0;
  let body = full.slice(logClearedTo);
  const q = ($("#log-filter").value || "").trim();
  const errOnly = $("#log-err-only").checked;
  if (q || errOnly) {
    const ql = q.toLowerCase();
    body = body.split("\n")
      .filter((line) => !q || line.toLowerCase().includes(ql))
      .filter((line) => !errOnly || LOG_ERR_RE.test(line))
      .join("\n");
  }
  const atBottom = pre.scrollTop + pre.clientHeight >= pre.scrollHeight - 8;
  pre.textContent = body;
  if (atBottom) pre.scrollTop = pre.scrollHeight;
}

function renderSchedule() {
  const banner = $("#sched-banner");
  banner.textContent = wtext("sched_banner_label") || "";
  const m = wmodel("sched_listbox");
  const ul = $("#sched-list");
  const items = (m && m.items) || [];
  const sel = selIdx(m);            /* 同 renderSources 那条注释：标量，不是数组 */
  ul.innerHTML = "";
  items.forEach((it, i) => {
    const li = document.createElement("li");
    li.textContent = it;
    if (i === sel) li.classList.add("is-sel");
    li.onclick = () => invoke({ action: "select_list", target: "sched_listbox", index: i });
    ul.appendChild(li);
  });
  $("#sched-detail").textContent = wtext("sched_detail") || "";
  $("#sched-cmd").textContent = wtext("sched_cmd_text") || "";
  $("#sched-hint").textContent = wtext("sched_hint") || "";
  /* 日期分层那一组的同步点显示（老版在 Tab3 的同一个 Labelframe 里）。 */
  $("#sync-text-2").textContent = wtext("sync_label") || "";
}

/* 弹框通道：通知类弹 toast，应答类弹模态（契约见文件头） */
const DIALOG_LABELS = {
  askyesno: ["是", "否"],
  askquestion: ["是", "否"],
  askokcancel: ["确定", "取消"],
  askretrycancel: ["重试", "取消"],
};

function renderDialogs() {
  const msgs = (state && state.messages) || [];
  msgs.forEach((m) => {
    if (!m.needs_answer && !seenNotices.has(m.id)) {
      seenNotices.add(m.id);
      const text = (m.title ? m.title + "：" : "") + (m.message || "");
      toast(text);
      /* 错误 / 警告额外留在顶部提示条里 —— toast 2.5 秒就没了，而"备份失败"
       * 这类消息恰恰是用户离开屏幕回来后最需要看到的那一条。 */
      if (/error|warning/i.test(m.kind || "")) showNotice(m.kind, text);
    }
  });
  const box = $("#dialog");
  const pending = msgs.find((m) => m.needs_answer && m.answer === null);
  if (!pending) { box.hidden = true; dialogShownId = 0; return; }
  if (pending.id === dialogShownId) return;   // 同一条已在显示，不重置输入
  dialogShownId = pending.id;
  const labels = DIALOG_LABELS[pending.kind] || ["确定", "取消"];
  $("#dialog-title").textContent = pending.title || "请确认";
  $("#dialog-kind").textContent = pending.kind;
  $("#dialog-msg").textContent = pending.message || "";
  $("#dialog-yes").textContent = labels[0];
  $("#dialog-no").textContent = labels[1];
  $("#dialog-yes").onclick = () => postDialog(pending.id, true);
  $("#dialog-no").onclick = () => postDialog(pending.id, false);
  box.hidden = false;
}

/* ---------------- 事件绑定 ---------------- */

function readVarValue(el) {
  if (el.type === "checkbox") return el.checked;
  if (el.type === "radio") return el.value;
  if (el.type === "number") return String(Number(el.value) || el.value);
  return el.value;
}

function bindVars() {
  $$("[data-var]").forEach((el) => {
    el.addEventListener("change", () => {
      const key = el.dataset.var;
      if (el.type === "radio" && !el.checked) return;
      const val = readVarValue(el);
      /* 设置页那几格是**草稿**：只记在本地，点「保存」才一次提交。
       * 传输页的线程数没有 `data-draft`，仍旧即时生效（它是本次运行参数）。 */
      if (el.hasAttribute("data-draft")) { setDraft(key, val); return; }
      invoke({ action: "set_var", target: key, value: val });
    });
  });
}

/* ---------------- 设置页：草稿 / 保存 / 放弃 ---------------- */

function sameValue(a, b) {
  if (a === b) return true;
  const norm = (x) => (x === null || x === undefined) ? "" : String(x);
  return norm(a) === norm(b);
}

function setDraft(key, value) {
  const current = ((state && state.vars) || {})[key];
  if (sameValue(value, current)) draft.values.delete(key);
  else draft.values.set(key, value);
  delete draft.errors[key];
  updateDraftUi();
}

function updateDraftUi() {
  const n = draft.values.size;
  const dirty = n > 0;
  $("#settings-save").disabled = !dirty;
  $("#settings-revert").disabled = !dirty;
  $("#settings-savebar").classList.toggle("is-dirty", dirty);
  $("#settings-status").textContent = dirty
    ? ("有 " + n + " 项改动还没保存。")
    : "改动会先留在这里，点「保存」才生效并写入配置文件。";
  const tab = $('.tab[data-tab="settings"]');
  if (tab) tab.classList.toggle("has-dirty", dirty);
  $$("[data-draft]").forEach((el) => {
    el.classList.toggle("is-invalid", !!draft.errors[el.dataset.var]);
  });
  const msgs = Object.keys(draft.errors)
    .filter((k) => draft.errors[k])
    .map((k) => (SETTING_LABELS[k] ? SETTING_LABELS[k] + "：" : "") + draft.errors[k]);
  $("#settings-err").textContent = msgs.join("；");
}

async function saveSettings() {
  if (!draft.values.size) return;
  const values = {};
  draft.values.forEach((v, k) => { values[k] = v; });
  $("#settings-save").disabled = true;
  let j = null;
  try {
    const r = await fetch("/api/settings/save", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ values }),
    });
    j = await r.json();
  } catch (e) {
    draft.errors = { _: "保存请求失败：" + e.message };
    updateDraftUi();
    return;
  }
  if (!j || j.ok !== true) {
    draft.errors = (j && j.errors) || { _: "保存失败" };
    updateDraftUi();
    showNotice("showerror", "设置没有保存，改动仍留在页面上。");
    return;
  }
  draft.values.clear();
  draft.errors = {};
  updateDraftUi();
  /* 立刻把"后端真正应用后的值"拉回来，不等下一拍轮询 —— 用户点完保存
   * 看到的第一眼就该是结果（数字被后端规整过也能当场看见）。 */
  try { state = await apiState(); render(); } catch (e) { /* 下一拍还会再拉 */ }
  if (j.persisted) {
    toast("设置已保存。");
  } else {
    const msg = "设置已在本次运行中生效，但写入配置文件失败 —— 下次启动会回到旧值。";
    toast(msg);
    showNotice("showwarning", msg);
  }
}

function revertSettings() {
  draft.values.clear();
  draft.errors = {};
  updateDraftUi();
  renderVars();          /* 把输入框改回后端那份值 */
  toast("已放弃未保存的改动。");
}

function bindButtons() {
  $$("[data-click]").forEach((el) => {
    /* 「导出日志」自带处理器（见 `exportLog`）：它要的是**日志正文**，不是
     * 后端那个会卡在 `filedialog` 替身上的 `_export_log`。这一条跳过，
     * 免得两个处理器同时挂在同一个按钮上。 */
    if (el.dataset.click === "export_log_btn") return;
    el.addEventListener("click", () => invoke({ action: "click", target: el.dataset.click }));
  });
  $$("[data-semantic]").forEach((el) => {
    el.addEventListener("click", () => invoke({ action: "semantic", name: el.dataset.semantic }));
  });
}

function bindTabs() {
  $$(".tab").forEach((t) => {
    t.addEventListener("click", () => {
      $$(".tab").forEach((x) => x.classList.toggle("is-active", x === t));
      $$(".pane").forEach((p) => p.classList.toggle("is-active", p.id === "pane-" + t.dataset.tab));
    });
  });
}

/* 导出日志：老版 `ui._export_log` 的最后一跳是系统「另存为」；浏览器里对应的是
 * **下载**（`<a download>` + Blob，同样会弹出一个保存框，位置由用户选）。
 * 正文与空日志那句话都由后端给（`export_log_text`），前端不另写一份文案。
 *
 * 【为什么不直接点后端那个按钮】`_export_log` 中间要过 `filedialog.asksaveasfilename`，
 * 而 Web 档这个替身的记录器从没装上 ⇒ 永远返回空串 ⇒ 点了**什么都不发生**。
 */
async function exportLog() {
  const j = await semantic("export_log_text");
  if (!j || j.ok !== true) {
    if (j && j.empty) toast(j.error || "日志区是空的。");
    return;
  }
  const text = String(j.text || "");
  const body = text.endsWith("\n") ? text : text + "\n";   // 与 Tk 档一样补收尾换行
  const d = new Date();
  const p = (n) => String(n).padStart(2, "0");
  const name = "transfer_log_"
    + d.getFullYear() + p(d.getMonth() + 1) + p(d.getDate()) + "_"
    + p(d.getHours()) + p(d.getMinutes()) + p(d.getSeconds()) + ".txt";
  const url = URL.createObjectURL(new Blob([body], { type: "text/plain;charset=utf-8" }));
  const a = document.createElement("a");
  a.href = url;
  a.download = name;
  document.body.appendChild(a);
  a.click();
  a.remove();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}

/* ---------------- 维护：打开目录 / 诊断包 / 退出 ---------------- */

async function openFolder(which) {
  try {
    const r = await fetch("/api/open_folder", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ which }),
    });
    const j = await r.json();
    if (!j || j.ok !== true) toast("打不开：" + ((j && j.error) || "未知错误"));
  } catch (e) {
    toast("打不开：" + e.message);
  }
}

async function quitApp() {
  if (draft.values.size && !confirm("有未保存的设置改动，确定退出吗？")) return;
  try {
    await fetch("/api/quit", { method: "POST" });
  } catch (e) { /* 服务端可能先退出，连接断了是正常现象 */ }
  toast("正在退出…");
}

/* ---------------- 键盘快捷键 ---------------- */
function onKeyDown(e) {
  /* Ctrl+S：保存设置（只在设置页生效 —— 其它页没有可保存的东西，
   * 抢掉浏览器的"保存网页"也没有意义）。 */
  if ((e.ctrlKey || e.metaKey) && !e.altKey && (e.key === "s" || e.key === "S")) {
    const pane = $("#pane-settings");
    if (pane && pane.classList.contains("is-active")) {
      e.preventDefault();
      saveSettings();
    }
    return;
  }
  if (e.key === "Escape") closeTopmost();
}

/* Esc 关最上层的框。`#dialog` **不在此列**：它代表后端正在等一个答复
 * （askyesno 之类），Esc 静默关掉没人应答，后端会等到超时再落回默认值 ——
 * 那等于替用户做了一个他看不见的选择。 */
function closeTopmost() {
  if (!$("#ask-text").hidden) { closeAskText(null); return; }
  if (!$("#sched-form").hidden) { closeSchedForm(); return; }
  if (!$("#profile-form").hidden) { closeProfileForm(); return; }
  if (!$("#ftp-picker").hidden) { closeFtpPicker(); return; }
  if (!$("#picker").hidden) { closePicker(); return; }
  hideSrcMenu();
}

function bindMisc() {
  $('[data-click="export_log_btn"]').addEventListener("click", exportLog);
  /* 「清屏」点击由 `[data-semantic="clear_log"]` 统一绑定（见 bindButtons）——
   * 真正清空在后端，这样导出拿到的也是清过之后的那份。这里只把前端截断点归零：
   * 后端清空后正文变短，旧的截断点会把下一轮新日志又切掉一截。 */
  $("#log-clear").addEventListener("click", () => { logClearedTo = 0; });
  $("#log-filter").addEventListener("input", renderLog);
  $("#log-err-only").addEventListener("change", renderLog);

  $("#notice-close").addEventListener("click", hideNotice);

  /* 维护与诊断 */
  $("#open-data-dir").addEventListener("click", () => openFolder("data"));
  $("#open-log-dir").addEventListener("click", () => openFolder("log"));
  $("#download-diag").addEventListener("click", () => {
    window.location.href = "/api/diagnostics.zip";   /* 服务端带 attachment 头 */
  });
  $("#quit-app").addEventListener("click", quitApp);

  /* 设置页保存栏 */
  $("#settings-save").addEventListener("click", saveSettings);
  $("#settings-revert").addEventListener("click", revertSettings);

  document.addEventListener("keydown", onKeyDown);
  $("#reset-sync").addEventListener("click", () => invoke({ action: "semantic", name: "reset_syncpoint" }));
  $("#reset-sync-2").addEventListener("click", () => invoke({ action: "semantic", name: "reset_syncpoint" }));

  /* 源「浏览」按**源类型**分三路（老版 `_browse_source` 同款）：本地 → 三项菜单；
   * FTP → 远程目录浏览器；共享 → 手打 UNC。 */
  $("#src-browse").addEventListener("click", browseSource);
  $("#src-menu-file").addEventListener("click", () => { hideSrcMenu(); openPicker("source-file"); });
  $("#src-menu-dir").addEventListener("click", () => { hideSrcMenu(); openPicker("source-dir"); });
  $("#src-menu-text").addEventListener("click", async () => {
    hideSrcMenu();
    /* 初值 = 当前源路径（老版 `_pick_source_manual` 的 `initialvalue=cur`）。 */
    const cur = (state && state.vars && state.vars.source_path) || "";
    const p = await askText("手工添加来源", "输入文件或文件夹的完整路径：", cur);
    if (p && p.trim()) {
      await invoke({ action: "semantic", name: "add_source_text",
                     payload: { path: p.trim() } });
    }
  });
  document.addEventListener("click", (e) => {
    if (!e.target.closest("#src-menu") && e.target.id !== "src-browse") hideSrcMenu();
  });

  /* 目标「浏览」：类型是 FTP 就开远程浏览器（老版 `_browse_dest` 同款分叉）。 */
  $("#dst-browse").addEventListener("click", () => {
    if (((state.vars || {}).dest_type || "local") === "ftp") openFtpPicker("dest");
    else openPicker("dest");
  });
  /* 第二个目标**只可能是本地目录**（后端校验也这么认），所以没有 FTP 分叉。 */
  $("#dst2-browse").addEventListener("click", () => openPicker("dest2"));

  /* 「⚙」：改的是**当前激活那一套**配置（老版 `_ftp_settings`）。 */
  $("#src-gear").addEventListener("click", openActiveProfileForm);
  $("#dst-gear").addEventListener("click", openActiveProfileForm);

  /* 日志导出目录的「浏览…」（老版 `ui._pick_log_export_dir`）。 */
  $("#logdir-browse").addEventListener("click", () => openPicker("logdir"));

  /* 定时任务的 新建 / 编辑 / 删除（老版 Tab3 那三个按钮）。 */
  $("#sched-new").addEventListener("click", () => openSchedForm("new"));
  $("#sched-edit").addEventListener("click", () => openSchedForm("edit"));
  $("#sched-del").addEventListener("click", deleteSched);
  $("#sched-copy").addEventListener("click", copySchedCmd);
  $("#sf-save").addEventListener("click", saveSchedForm);
  $("#sf-cancel").addEventListener("click", closeSchedForm);
  $("#sf-close").addEventListener("click", closeSchedForm);

  /* 单行文本输入（手工添加 / 共享路径）。 */
  $("#at-ok").addEventListener("click", () => closeAskText($("#at-input").value));
  $("#at-cancel").addEventListener("click", () => closeAskText(null));
  $("#at-close").addEventListener("click", () => closeAskText(null));
  $("#at-input").addEventListener("keydown", (e) => {
    if (e.key === "Enter") closeAskText($("#at-input").value);
    if (e.key === "Escape") closeAskText(null);
  });

  /* FTP 配置那一组（与 Tk 档 Tab2 的四个按钮一一对应） */
  $("#prof-new").addEventListener("click", () => openProfileForm("new"));
  $("#prof-edit").addEventListener("click", () => openProfileForm("edit"));
  $("#prof-del").addEventListener("click", () => withSelectedProfile("profile_delete"));
  $("#prof-active").addEventListener("click", () => withSelectedProfile("profile_set_active"));
  $("#pf-save").addEventListener("click", saveProfileForm);
  $("#pf-cancel").addEventListener("click", closeProfileForm);
  $("#pf-close").addEventListener("click", closeProfileForm);
  ["#pf-name", "#pf-host", "#pf-port", "#pf-user", "#pf-pass", "#pf-dir"].forEach((id) => {
    $(id).addEventListener("keydown", (e) => { if (e.key === "Enter") saveProfileForm(); });
  });
}

/* ---------------- 目录选择器 ----------------
 *
 * 本地路径**一律交给系统原生对话框**（与 Tk 档是同一个 `filedialog`：目录树、盘符、
 * 图标、新建文件夹、键盘导航都在）。后端 `/api/pick` 在请求线程上开框、阻塞到用户
 * 关框，所以这里的 `await` 会挂很久 —— 这是模态的正常形态，不是卡死。
 *
 * `openPickerFallback` 是**退路**，不是主路：系统对话框起不来（没有 Tk、宿主不允许
 * 建窗口、后端抛异常）时退回原来那个自绘框 —— 能选，总比"点了没反应"强。
 */
let pickBusy = false;

async function openPicker(mode) {
  if (pickBusy) return;            // 连点两下「浏览」只开一个框
  pickBusy = true;
  const vars = (state && state.vars) || {};
  let initial = "";
  if (mode === "dest") initial = vars.dest_path || "";
  else if (mode === "dest2") initial = vars.dest_path2 || "";
  else if (mode === "logdir") initial = vars.log_export_dir || "";
  else initial = vars.source_path || "";
  try {
    const r = await fetch("/api/pick", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ kind: mode, initial }),
    });
    const j = await r.json();
    if (!j || j.ok !== true) {
      console.warn("原生对话框不可用，退回自绘框：", j && j.error);
      openPickerFallback(mode);
      return;
    }
    if (!j.path) return;           // 取消：什么都不做（与 Tk 档 `if path:` 一致）
    await applyPicked(mode, j.path);
  } catch (e) {
    console.warn("原生对话框请求失败，退回自绘框：", e);
    openPickerFallback(mode);
  } finally {
    pickBusy = false;
  }
}

async function applyPicked(mode, path) {
  if (mode === "dest") {
    await invoke({ action: "set_var", target: "dest_path", value: path });
  } else if (mode === "dest2") {
    /* 第二目标与 Tk 档 `_browse_dest2` 同一条：写路径并顺手勾上「启用第二目标」。 */
    await invoke({ action: "set_var", target: "dest_path2", value: path });
    await invoke({ action: "set_var", target: "multi_dest", value: true });
  } else if (mode === "logdir") {
    await invoke({ action: "set_var", target: "log_export_dir", value: path });
  } else {
    /* source-file / source-dir：与 Tk 档 `_pick_source_file/_pick_source_dir` 同一条 ——
       **追加**到源列表，不覆盖（覆盖式在多源下会悄悄抹掉前一个）。 */
    await invoke({ action: "semantic", name: "add_source", path });
  }
}

/* ---- 退路：自绘框（系统对话框不可用时才走到这里） ---- */

const PICKER_TITLES = {
  "source-dir": "选择源文件夹",
  "source-file": "选择源文件",
  dest: "选择目标文件夹",
  dest2: "选择第二个目标文件夹",
  logdir: "选择日志导出目录",
};

async function openPickerFallback(mode) {
  picker.mode = mode;
  $("#picker-title").textContent = PICKER_TITLES[mode] || "选择文件夹";
  $("#picker").hidden = false;
  const vars = state.vars || {};
  let start = "";
  if (mode === "dest") start = vars.dest_path || "";
  else if (mode === "dest2") start = vars.dest_path2 || "";
  else if (mode === "logdir") start = vars.log_export_dir || "";
  else start = vars.source_path || "";
  await loadDrives();
  await pickerLoad(start || "");
}

function closePicker() { $("#picker").hidden = true; }

async function loadDrives() {
  const sel = $("#picker-drives");
  if (sel.options.length) return;
  try {
    const r = await fetch("/api/fs/drives");
    const drives = await r.json();
    sel.innerHTML = "";
    drives.forEach((d) => {
      const o = document.createElement("option");
      o.value = d; o.textContent = d;
      sel.appendChild(o);
    });
    sel.onchange = () => pickerLoad(sel.value);
  } catch (e) { /* 无盘符（非 Windows）：忽略 */ }
}

async function pickerLoad(path) {
  const err = $("#picker-err");
  err.textContent = "";
  try {
    /* 选文件那一档要多列一层文件（老版源菜单第一项是"选择文件"，
     * `transfer_form._pick_source_file`）。其余档只列目录。 */
    const wantFiles = picker.mode === "source-file" ? "&files=1" : "";
    const url = "/api/fs" + (path ? "?path=" + encodeURIComponent(path) : "") + wantFiles;
    const r = await fetch(url);
    const j = await r.json();
    if (j.ok === false) { err.textContent = j.error || "打不开"; return; }
    picker.path = j.path;
    $("#picker-path").value = j.path;
    const ul = $("#picker-dirs");
    ul.innerHTML = "";
    (j.dirs || []).forEach((name) => {
      const li = document.createElement("li");
      li.textContent = name;
      li.onclick = () => pickerLoad(j.path.replace(/[\\/]+$/, "") + "\\" + name);
      ul.appendChild(li);
    });
    (j.files || []).forEach((name) => {
      const li = document.createElement("li");
      li.textContent = name;
      li.title = name;
      /* 点一个文件 = 立刻收下（老版那个 `askopenfilename` 也是选完即用）。 */
      li.onclick = async () => {
        if (picker.mode !== "source-file") return;
        await invoke({ action: "semantic", name: "add_source",
                       path: j.path.replace(/[\\/]+$/, "") + "\\" + name });
        closePicker();
      };
      ul.appendChild(li);
    });
    $("#picker-up").disabled = !j.parent;
    $("#picker-up").onclick = j.parent ? () => pickerLoad(j.parent) : null;
  } catch (e) {
    err.textContent = "列举失败：" + e.message;
  }
}

function bindPicker() {
  $("#picker-close").addEventListener("click", closePicker);
  $("#picker-go").addEventListener("click", () => pickerLoad($("#picker-path").value));
  $("#picker-path").addEventListener("keydown", (e) => { if (e.key === "Enter") pickerLoad($("#picker-path").value); });
  $("#picker-pick").addEventListener("click", async () => {
    if (!picker.path) return;
    if (picker.mode === "dest") {
      await invoke({ action: "set_var", target: "dest_path", value: picker.path });
    } else if (picker.mode === "dest2") {
      /* 选完顺手勾上总闸（老版 `_browse_dest2` 同款）。 */
      await invoke({ action: "set_var", target: "dest_path2", value: picker.path });
      await invoke({ action: "set_var", target: "multi_dest", value: true });
    } else if (picker.mode === "logdir") {
      await invoke({ action: "set_var", target: "log_export_dir", value: picker.path });
    } else {
      await invoke({ action: "semantic", name: "add_source", path: picker.path });
    }
    closePicker();
  });
}

function bindFtpPicker() {
  $("#ftp-close").addEventListener("click", closeFtpPicker);
  $("#ftp-cancel").addEventListener("click", closeFtpPicker);
  $("#ftp-go").addEventListener("click", () => ftpLoad($("#ftp-path").value));
  $("#ftp-path").addEventListener("keydown", (e) => {
    if (e.key === "Enter") ftpLoad($("#ftp-path").value);
  });
  $("#ftp-pick-dir").addEventListener("click", () => ftpPick("dir"));
  $("#ftp-pick-file").addEventListener("click", () => ftpPick("file"));
  /* 「FTP 设置」：老版在浏览框里也能打开配置框（`FTPBrowserDialog._open_settings`）。 */
  $("#ftp-settings").addEventListener("click", () => {
    closeFtpPicker();
    openActiveProfileForm();
  });
  /* 「测试连接」：纯网络，走后端 /api/ftp/test（不占主循环），结果显示在表单提示行。 */
  $("#pf-test").addEventListener("click", async () => {
    $("#pf-hint").textContent = "正在测试…";
    try {
      const r = await fetch("/api/ftp/test", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          host: $("#pf-host").value,
          port: $("#pf-port").value,
          user: $("#pf-user").value,
          password: $("#pf-pass").value,
          remote_dir: $("#pf-dir").value,
        }),
      });
      const j = await r.json();
      $("#pf-hint").textContent = j.message || (j.ok ? "连接成功" : "连接失败");
    } catch (e) {
      $("#pf-hint").textContent = "无法连接到 FTP 服务器：\n" + e.message;
    }
  });
}

/* ---------------- FTP 远程目录浏览器 ----------------
 *
 * 老版是 `ui_dialogs.FTPBrowserDialog`：一棵远程目录树 + 「选择当前目录」/「选择当前文件」
 * /「FTP 设置」/「取消」。Web 档没有系统对话框可开，所以目录由**后端真连服务器**列
 * （`GET /api/ftp?path=…`，后端复用 `ftp_client.FTPHelper.list_dir` 的 MLSD/LIST 解析）。
 * 这一屏就是那次"选不了 FTP 文件夹"的正解。
 */
const ftpPicker = { mode: "dest", path: "/", file: null, cfgHost: "" };

async function openFtpPicker(mode) {
  ftpPicker.mode = mode;
  ftpPicker.file = null;
  $("#ftp-title").textContent = mode === "source" ? "选择 FTP 源目录" : "选择 FTP 目标目录";
  $("#ftp-picker").hidden = false;
  const cur = mode === "source"
    ? ((state.vars && state.vars.source_path) || "")
    : ((state.vars && state.vars.dest_path) || "");
  const start = ftpStartPath(cur);
  /* 记住这台服务器地址：选中之后要拼成 `ftp://host/dir`（与 Tk 的
   * `_handle_ftp_browse` 逐字同形 —— 它写的就是 `f"ftp://{cfg['host']}{dlg.result}"`）。 */
  const rows = (state && state.ftp_profiles) || [];
  const act = (state && state.ftp_active) || "";
  const row = rows.find((p) => p.name === act) || rows[0];
  ftpPicker.cfgHost = row ? (row.host || "") : "";
  await ftpLoad(start);
}

/* 从 `ftp://host/dir/sub` 里抠出 `/dir/sub` 当起始目录；不是 ftp URL 就从头开始。
 * （Tk 那段是 `current_path.split("/", 3)[-1]`，同一个意图。） */
function ftpStartPath(cur) {
  if (!cur || cur.indexOf("ftp://") !== 0) return "/";
  const rest = cur.slice("ftp://".length);
  const i = rest.indexOf("/");
  return i < 0 ? "/" : (rest.slice(i) || "/");
}

function closeFtpPicker() { $("#ftp-picker").hidden = true; }

async function ftpLoad(path) {
  const err = $("#ftp-err");
  err.textContent = "";
  try {
    const url = "/api/ftp" + (path ? "?path=" + encodeURIComponent(path) : "");
    const r = await fetch(url);
    const j = await r.json();
    if (j.ok === false) { err.textContent = j.error || "打不开"; return; }
    ftpPicker.path = j.path;
    ftpPicker.file = null;
    $("#ftp-path").value = j.path;
    const ul = $("#ftp-list");
    ul.innerHTML = "";
    (j.dirs || []).forEach((name) => {
      const li = document.createElement("li");
      li.textContent = "[目录] " + name;
      li.onclick = () => ftpLoad((j.path.replace(/\/+$/, "") || "") + "/" + name);
      ul.appendChild(li);
    });
    (j.files || []).forEach((f) => {
      const li = document.createElement("li");
      li.textContent = f.name + (f.size ? "  (" + f.size + " B)" : "");
      li.title = f.name;
      li.onclick = () => {
        ftpPicker.file = f.name;
        $$("#ftp-list li").forEach((x) => x.classList.remove("is-sel"));
        li.classList.add("is-sel");
      };
      /* 双击 = 选中并直接确认（与 Tk 档 `FTPBrowserDialog` 双击即返回同一习惯；
       * 也顺手把 file 再写一遍 —— 双击的前两下 click 已经选过了，这里是幂等的）。 */
      li.ondblclick = () => { ftpPicker.file = f.name; ftpPick("file"); };
      ul.appendChild(li);
    });
    $("#ftp-up").disabled = !j.parent;
    $("#ftp-up").onclick = j.parent ? () => ftpLoad(j.parent) : null;
  } catch (e) {
    err.textContent = "列举失败：" + e.message;
  }
}

async function ftpPick(kind) {
  if (kind === "file" && !ftpPicker.file) {
    $("#ftp-err").textContent = "请先在下面的列表里点一个文件。";
    return;
  }
  const base = ftpPicker.path.replace(/\/+$/, "");
  const full = base + (kind === "file" ? "/" + ftpPicker.file : "") || "/";
  const url = "ftp://" + ftpPicker.cfgHost + full;
  if (ftpPicker.mode === "source") {
    await invoke({ action: "set_var", target: "source_path", value: url });
    await invoke({ action: "semantic", name: "add_source", path: url });
  } else {
    await invoke({ action: "set_var", target: "dest_path", value: url });
  }
  closeFtpPicker();
}

/* ---------------- 「⚙」：直达当前那一套 FTP 配置的编辑表单 ----------------
 * 老版的 ⚙（`transfer_form._ftp_settings`）改的是**当前激活的那一套**，路径回填到
 * 源/目标那一行。Web 档的等价物：切到「设置」页 + 打开那套配置的表单。
 */
function openActiveProfileForm() {
  const rows = profileRows();
  const act = (state && state.ftp_active) || "";
  const idx = rows.findIndex((p) => p.name === act);
  $$(".tab").forEach((x) => x.classList.toggle("is-active", x.dataset.tab === "settings"));
  $$(".pane").forEach((p) => p.classList.toggle("is-active", p.id === "pane-settings"));
  if (!rows.length) { openProfileForm("new"); return; }
  const i = idx >= 0 ? idx : 0;
  invoke({ action: "select_list", target: "profile_listbox", index: i }).then(() => {
    openProfileForm("edit", i);
  });
}

/* ---------------- 单行文本输入（老版的 simpledialog.askstring 两个入口） ---------------- */
let askTextResolve = null;

function askText(title, message, initial) {
  $("#at-title").textContent = title;
  $("#at-msg").textContent = message || "";
  $("#at-input").value = initial || "";
  $("#ask-text").hidden = false;
  $("#at-input").focus();
  return new Promise((res) => { askTextResolve = res; });
}

function closeAskText(value) {
  $("#ask-text").hidden = true;
  const r = askTextResolve;
  askTextResolve = null;
  if (r) r(value);
}

/* ---------------- 定时任务表单（老版 ScheduleDialog） ----------------
 * 老版那个 `tk.Toplevel` 在 Web 档画不出来（见 135 报告 §4.3），所以表单搬到前端；
 * 但"攒记录 / 校验 / 存盘"仍调后端同一份（`config._clean_schedule` + `_sched_save`）。
 */
const schedForm = { mode: "new", initial: {}, params: {}, types: [], dayCodes: [] };
const DAY_CN = { MON: "一", TUE: "二", WED: "三", THU: "四", FRI: "五", SAT: "六", SUN: "日" };

async function openSchedForm(mode) {
  const j = await semantic("sched_form", { mode }, true);
  if (!j || j.error || j.initial === undefined) {
    toast((j && j.error) || "打不开表单");
    return;
  }
  schedForm.mode = mode;
  schedForm.initial = j.initial || {};
  schedForm.params = j.params || {};
  schedForm.types = j.types || [];
  schedForm.dayCodes = j.day_codes || [];

  const trig = schedForm.initial.trigger || {};
  $("#sf-title").textContent = (mode === "edit" && schedForm.initial.name)
    ? ("编辑定时任务 - " + schedForm.initial.name) : "新建定时任务";
  $("#sf-name").value = schedForm.initial.name || "";
  $("#sf-time").value = trig.time || "02:30";
  $("#sf-hours").value = trig.every_hours || 4;
  $("#sf-date").value = trig.date || "";
  $("#sf-preview").textContent = j.preview || "（未从界面取到任何参数）";
  $("#sf-hint").textContent = "";

  const typesEl = $("#sf-types");
  typesEl.innerHTML = "";
  schedForm.types.forEach(([code, label]) => {
    const l = document.createElement("label");
    l.className = "radio";
    l.innerHTML = '<input type="radio" name="sf-type"> ';
    l.appendChild(document.createTextNode(label));
    const inp = l.querySelector("input");
    inp.value = code;
    inp.checked = code === (trig.type || "daily");
    inp.onchange = schedSyncType;
    typesEl.appendChild(l);
  });

  const daysEl = $("#sf-days");
  daysEl.innerHTML = "";
  const have = new Set(trig.days || []);
  schedForm.dayCodes.forEach((code) => {
    const l = document.createElement("label");
    l.className = "check";
    const inp = document.createElement("input");
    inp.type = "checkbox";
    inp.value = code;
    inp.checked = have.has(code);
    l.appendChild(inp);
    l.appendChild(document.createTextNode(" 周" + (DAY_CN[code] || code)));
    daysEl.appendChild(l);
  });

  schedSyncType();
  $("#sched-form").hidden = false;
  $("#sf-name").focus();
}

/* 只把当前类型用得上的那一组放出来（与 `ScheduleDialog._sync_type` 同一条：
 * 摆着的话"每周"和"每隔 N 小时"会同时出现，用户没法知道哪个作数）。 */
function schedSyncType() {
  const t = ($("#sf-types").querySelector("input:checked") || {}).value || "daily";
  $("#sf-days-row").hidden = t !== "weekly";
  $("#sf-hours-row").hidden = t !== "hourly";
  $("#sf-date-row").hidden = t !== "once";
}

function closeSchedForm() { $("#sched-form").hidden = true; }

async function saveSchedForm() {
  const t = ($("#sf-types").querySelector("input:checked") || {}).value || "daily";
  const days = $$("#sf-days input:checked").map((x) => x.value);
  const r = await semantic("sched_save", {
    type: t,
    time: $("#sf-time").value,
    days,
    every_hours: $("#sf-hours").value,
    date: $("#sf-date").value,
    name: $("#sf-name").value,
    initial: schedForm.initial,
    params: schedForm.params,
  }, true);
  if (r && r.ok) { closeSchedForm(); return; }
  $("#sf-hint").textContent = (r && r.error) || (r && r.message) || "保存失败";
}

/* 删除：作用于列表里选中那一条（与 Tk 档"选着 B 就删 B"同规矩）。
 * 二次确认由后端 `_sched_delete` 里的 askyesno 发起 —— 它经替身层流到前端模态框。 */
async function deleteSched() {
  const i = wsel("sched_listbox");
  if (i === null) { toast("请先在列表里选一条任务。"); return; }
  await invoke({ action: "select_list", target: "sched_listbox", index: i });
  await invoke({ action: "semantic", name: "sched_delete" });
}

/* 「复制」：老版 `_sched_copy_cmd` 把命令行塞进系统剪贴板。浏览器里剪贴板属于
 * 前端，所以这里先真复制，再叫后端走一遍同一个方法（补上那行日志；
 * 后端那份文本取自同一个 `sched_cmd_text`，两边不会各说各话）。 */
async function copySchedCmd() {
  const txt = ($("#sched-cmd").textContent || "").trim();
  if (!txt) { toast("上面还没有命令可复制 —— 先在列表里选一条任务。"); return; }
  try {
    await navigator.clipboard.writeText(txt);
  } catch (e) {
    /* 没有剪贴板权限（非安全上下文 / 用户拒绝）时的兜底：临时 textarea + execCommand。
     * 两条都失败也不拦着 —— 后端那行"已复制"仍会记，用户至少能手工选中复制。 */
    const ta = document.createElement("textarea");
    ta.value = txt;
    ta.style.position = "fixed";
    ta.style.opacity = "0";
    document.body.appendChild(ta);
    ta.select();
    try { document.execCommand("copy"); } catch (_) { /* ignore */ }
    ta.remove();
  }
  await invoke({ action: "semantic", name: "sched_copy_cmd" });
}

/* ---------------- 源「浏览」：老版那个三项菜单 ---------------- */
function showSrcMenu() {
  const menu = $("#src-menu");
  const btn = $("#src-browse");
  const r = btn.getBoundingClientRect();
  menu.style.left = Math.max(8, r.left - 80) + "px";
  menu.style.top = (r.bottom + 4) + "px";
  menu.hidden = false;
}

function hideSrcMenu() { $("#src-menu").hidden = true; }

async function browseSource() {
  const t = (state && state.vars && state.vars.source_type) || "local";
  if (t === "ftp") { await openFtpPicker("source"); return; }
  if (t === "share") {
    /* 老版共享那条只留"手打"一个入口（系统对话框到不了还没连上的 UNC）。
     * 提示语与 `transfer_form._pick_share_source` **逐字相同**（含空行与全角空格）。 */
    const cur = (state && state.vars && state.vars.source_path) || "";
    const initial = /^[\\/]{2}/.test(cur) ? cur : "";
    const p = await askText(
      "添加共享文件夹",
      "输入共享路径，形如：\n\n"
      + "    \\\\服务器\\共享名\n"
      + "    \\\\服务器\\共享名\\子目录\n\n"
      + "（口令由 Windows 管理：请先在**资源管理器**里连一次该共享，\n"
      + "　本工具不代填、也不代存凭据。）",
      initial);
    if (p && p.trim()) {
      await invoke({ action: "semantic", name: "add_source_text",
                     payload: { path: p.trim(), share: true } });
    }
    return;
  }
  showSrcMenu();
}

/* ---------------- 主循环 ---------------- */

async function tick() {
  /* 单飞：上一轮还没回来就不再发一轮。
   * 这一行是「进度条倒退」的根治点 —— setInterval 不管上一轮的生死，
   * 主线程忙的时候请求会排队，乱序返回的旧快照会盖掉新的。 */
  if (tickInFlight) return;
  tickInFlight = true;
  try {
    state = await apiState();
    setConn(true);
    render();
  } catch (e) {
    setConn(false);
  } finally {
    tickInFlight = false;
  }
}

/* ---------------- 状态推送（SSE） ----------------
 *
 * 早先是 setInterval(tick, 500)。两个毛病：请求会叠加（上一轮没回来照样发下一轮）、
 * 响应会乱序（旧快照后到，把新的盖掉 ⇒ 进度条倒退、按钮闪回）。
 * 现在改成后端一条 /api/events 长连接，变了才推一帧，严格有序。
 *
 * 兜底：浏览器不支持 EventSource、或者这条连接没建立起来时，退回 5 秒一次的
 * 慢轮询（够用，且不会再叠加 —— tick 里有单飞）。EventSource 自己带重连，
 * 断了不用我们管，readyState 会在 CONNECTING/CLOSED 之间跳，那期间由兜底补。
 */
function startEventStream() {
  if (typeof EventSource !== "function") return false;
  try {
    eventSource = new EventSource("/api/events");
  } catch (e) {
    eventSource = null;
    return false;
  }
  eventSource.onmessage = (ev) => {
    try {
      state = JSON.parse(ev.data);
      setConn(true);
      render();
    } catch (e) {
      /* 坏帧就丢掉，下一帧会补一份完整的 */
    }
  };
  eventSource.onerror = () => {
    /* 交给 EventSource 自动重连；只有它彻底关掉时才让兜底轮询接手 */
    if (eventSource && eventSource.readyState === 2) setConn(false);
  };
  return true;
}

/* ---------------- 页面存活报到 ----------------
 *
 * C4（系统浏览器）没有"窗口已关闭"这种宿主事件，只能靠前端报到/告别：
 * 打开时 hello，离开时 bye；最后一个页面 bye 之后 8 秒没人回来，后端自行退出。
 * 没有这一对，关掉标签页后进程会一直挂在后台占着端口。
 *
 * ⚠️ id 必须是**本次页面加载**唯一，不能用 sessionStorage：复制标签页会共享
 *    sessionStorage，两个页面就会共用一个 id —— 关掉其中一个，后端以为"全走了"。
 *    页面刷新则是一次正常的 bye + hello，两条消息都发得出去。
 */
const CLIENT_ID = "c" + Math.random().toString(36).slice(2) + Date.now().toString(36);

function clientHello() {
  fetch("/api/hello", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ client: CLIENT_ID }),
  }).catch(() => { /* 推送不到不影响界面 */ });
}

function clientBye() {
  try {
    if (navigator.sendBeacon) {
      /* sendBeacon 发的是 text/plain 字符串 —— 后端 `_read_json` 认这种裸文本。 */
      navigator.sendBeacon("/api/bye", CLIENT_ID);
      return;
    }
  } catch (e) { /* 落到下面的 fetch */ }
  try {
    fetch("/api/bye", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ client: CLIENT_ID }),
      keepalive: true,
    });
  } catch (e) { /* ignore */ }
}

function main() {
  bindTabs();
  bindVars();
  bindButtons();
  bindMisc();
  bindPicker();
  bindFtpPicker();
  updateDraftUi();
  clientHello();
  window.addEventListener("pagehide", clientBye);
  /* 有未保存的设置改动时离开页面要拦一下 —— 草稿只在浏览器内存里，
   * 页面一关就没了，而用户可能以为"填了就在"。 */
  window.addEventListener("beforeunload", (e) => {
    if (draft.values.size) {
      e.preventDefault();
      e.returnValue = "";
    }
  });
  tick();
  startEventStream();
  /* 兜底：SSE 没连上（或断在中途）才补一次轮询。readyState !== 1 即 OPEN。 */
  setInterval(() => {
    if (!eventSource || eventSource.readyState !== 1) tick();
  }, POLL_MS);
}

document.addEventListener("DOMContentLoaded", main);
