"use strict";

/* 文件传输工具 — Web 前端（纯 HTML/CSS/JS，无框架、无构建器）。
 *
 * 后端契约（web_app.py）：
 *   GET  /api/state       → 界面快照 {title, vars, widgets, source_list, ftp_profiles, ftp_active, messages}
 *   POST /api/invoke      → {action, target?, value?, name?, path?, index?} → {ok, result|error}
 *                           （action="semantic" 时 name 的取值表 = web_app.WebBridge._semantic，
 *                             含 add_source / remove_source / reset_syncpoint / export_log /
 *                             refresh_sched / sched_reload / profile_new / profile_edit /
 *                             profile_delete / profile_set_active）
 *   POST /api/dialog      → {id, answer} → {ok}      （应答后端弹框）
 *   GET  /api/fs?path=..  → {path, parent, dirs} | {ok:false, error}
 *   GET  /api/fs/drives   → ["C:\\", ...]
 *
 * 刷新模型：每 500ms 拉一次全量快照重渲染（不做 SSE/WS —— 见项目纪律）。
 * 渲染原则：正在被用户编辑的控件**不覆盖**（否则打字打到一半会被拽回去）。
 *
 * 弹框模型（对应后端 WebBridge._dialog）：快照里的 `messages` 是流水 ——
 *   · needs_answer=true（ask*）：还没答的渲染成模态框，点按钮 POST /api/dialog；
 *   · 通知类（info/warning/error）：每条新出现的弹一次 toast。
 */

const POLL_MS = 500;
let state = null;
let logClearedTo = 0;
let dialogShownId = 0;
const seenNotices = new Set();
const picker = { mode: "dest", path: "" };

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
  clearTimeout(toast._t);
  toast._t = setTimeout(() => setConn(true), 2500);
}

function setConn(ok) {
  const el = $("#conn-badge");
  el.textContent = ok ? "已连接" : "已断开";
  el.className = "badge " + (ok ? "badge-ok" : "badge-err");
}

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

  renderVars();
  renderSources();
  renderProfiles();
  renderProgress();
  renderLog();
  renderSchedule();
  renderDialogs();
}

function renderVars() {
  const vars = state.vars || {};
  $$("[data-var]").forEach((el) => {
    const key = el.dataset.var;
    if (!(key in vars) || editing(el)) return;
    const val = vars[key];
    if (el.type === "radio") {
      el.checked = String(val) === el.value;
    } else if (el.type === "checkbox") {
      el.checked = !!val && String(val) !== "0" && String(val).toLowerCase() !== "false";
    } else if (document.activeElement !== el) {
      el.value = val === null || val === undefined ? "" : val;
    }
  });
}

function renderSources() {
  const m = wmodel("source_listbox");
  const ul = $("#source-list");
  const items = (m && m.items) || state.source_list || [];
  const sel = new Set((m && m.selection) || []);
  ul.innerHTML = "";
  items.forEach((it, i) => {
    const li = document.createElement("li");
    li.textContent = it;
    if (sel.has(i)) li.classList.add("is-sel");
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
    /* 双击 = 「设为当前」（Tk 档也是双击，见 transfer_form 里 profile_listbox 的绑定）。
     * 用**行号**先选中再触发：双击的两下 click 与这条是同一批事件，靠轮询回来的
     * selection 去判断"选的是哪一套"会慢半拍。 */
    li.ondblclick = async () => {
      await invoke({ action: "select_list", target: "profile_listbox", index: i });
      await invoke({ action: "semantic", name: "profile_set_active" });
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

function openProfileForm(mode) {
  const rows = profileRows();
  let row = null;
  if (mode === "edit") {
    const idx = selectedProfileIdx();
    if (idx === null) { toast("请先在列表里选择一套配置。"); return; }
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

function renderLog() {
  const pre = $("#log-text");
  const full = wtext("log_text") || "";
  if (full.length < logClearedTo) logClearedTo = 0;
  const atBottom = pre.scrollTop + pre.clientHeight >= pre.scrollHeight - 8;
  pre.textContent = full.slice(logClearedTo);
  if (atBottom) pre.scrollTop = pre.scrollHeight;
}

function renderSchedule() {
  const banner = $("#sched-banner");
  banner.textContent = wtext("sched_banner_label") || "";
  const m = wmodel("sched_listbox");
  const ul = $("#sched-list");
  const items = (m && m.items) || [];
  const sel = new Set((m && m.selection) || []);
  ul.innerHTML = "";
  items.forEach((it, i) => {
    const li = document.createElement("li");
    li.textContent = it;
    if (sel.has(i)) li.classList.add("is-sel");
    li.onclick = () => invoke({ action: "select_list", target: "sched_listbox", index: i });
    ul.appendChild(li);
  });
  $("#sched-detail").textContent = wtext("sched_detail") || "";
  $("#sched-cmd").textContent = wtext("sched_cmd_text") || "";
  $("#sched-hint").textContent = wtext("sched_hint") || "";
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
      toast((m.title ? m.title + "：" : "") + (m.message || ""));
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

function bindVars() {
  $$("[data-var]").forEach((el) => {
    const key = el.dataset.var;
    const evt = el.type === "text" || el.type === "number" ? "change" : "change";
    el.addEventListener(evt, () => {
      let val;
      if (el.type === "checkbox") val = el.checked;
      else if (el.type === "radio") { if (!el.checked) return; val = el.value; }
      else if (el.type === "number") val = String(Number(el.value) || el.value);
      else val = el.value;
      invoke({ action: "set_var", target: key, value: val });
    });
  });
}

function bindButtons() {
  $$("[data-click]").forEach((el) => {
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

function bindMisc() {
  $("#log-clear").addEventListener("click", () => {
    logClearedTo = (wtext("log_text") || "").length;
    $("#log-text").textContent = "";
  });
  $("#reset-sync").addEventListener("click", () => invoke({ action: "semantic", name: "reset_syncpoint" }));
  $("#src-browse").addEventListener("click", () => openPicker("source"));
  $("#dst-browse").addEventListener("click", () => openPicker("dest"));

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

/* ---------------- 目录选择器 ---------------- */

async function openPicker(mode) {
  picker.mode = mode;
  $("#picker-title").textContent = mode === "source" ? "选择源文件夹" : "选择目标文件夹";
  $("#picker").hidden = false;
  const start = mode === "source"
    ? (wmodel("source_listbox") && (state.vars && state.vars.source_path) || "")
    : ((state.vars && state.vars.dest_path) || "");
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
    const url = "/api/fs" + (path ? "?path=" + encodeURIComponent(path) : "");
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
    if (picker.mode === "source") {
      await invoke({ action: "semantic", name: "add_source", path: picker.path });
    } else {
      await invoke({ action: "set_var", target: "dest_path", value: picker.path });
    }
    closePicker();
  });
}

/* ---------------- 主循环 ---------------- */

async function tick() {
  try {
    state = await apiState();
    setConn(true);
    render();
  } catch (e) {
    setConn(false);
  }
}

function main() {
  bindTabs();
  bindVars();
  bindButtons();
  bindMisc();
  bindPicker();
  tick();
  setInterval(tick, POLL_MS);
}

document.addEventListener("DOMContentLoaded", main);
