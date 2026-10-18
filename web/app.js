"use strict";

/* 文件传输工具 — Web 前端（纯 HTML/CSS/JS，无框架、无构建器）。
 *
 * 后端契约（web_app.py）：
 *   GET  /api/state       → 界面快照 {title, vars, widgets, source_list, ftp_profiles, ftp_active, messages}
 *   POST /api/invoke      → {action, target?, value?, name?, path?, index?} → {ok, result|error}
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

async function invoke(payload) {
  const r = await fetch("/api/invoke", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
  const j = await r.json();
  if (!j.ok) toast(j.error || "操作失败");
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
  ul.innerHTML = "";
  items.forEach((it, i) => {
    const li = document.createElement("li");
    li.textContent = it;
    if (it === state.ftp_active) li.classList.add("is-sel");
    li.onclick = () => invoke({ action: "select_list", target: "profile_listbox", index: i });
    ul.appendChild(li);
  });
  const sum = $("#profile-summary");
  sum.textContent = wtext("profile_summary") ||
    (items.length ? "" : "还没有配置。点「新建…」添加一个 FTP 连接信息。");
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

/* C3 专用：pywebview 的 JS→Python 调用派发在**主线程**上，用它当主线程节拍器，
   替 `ui_channel` 的 UI 泵跑 `root.run_pending()`（C4 由 HTTP 等待循环泵）。
   C4 下 `window.pywebview` 不存在，此函数是零成本 no-op —— 前端仍是**同一份**。 */
function hostPump() {
  try {
    if (window.pywebview && window.pywebview.api && window.pywebview.api.pump) {
      window.pywebview.api.pump();
    }
  } catch (e) { /* 宿主未就绪：忽略 */ }
}

function main() {
  bindTabs();
  bindVars();
  bindButtons();
  bindMisc();
  bindPicker();
  tick();
  setInterval(tick, POLL_MS);
  setInterval(hostPump, 50);
}

document.addEventListener("DOMContentLoaded", main);
