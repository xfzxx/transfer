# -*- coding: utf-8 -*-
r"""前端绑定一致性检查 —— 抓「静默缺失」这一整类 bug。

================================================================================
【为什么需要这个脚本】
================================================================================
Web 档的界面是**两处手写**的：

    后端 `file_transfer` / `transfer_form` 里造控件 → 前端 `web/index.html` 里画控件

两边靠**名字**对上（`data-var="db_keep_days"`）。名字写错、或者后端加了设置项
忘了往 HTML 里加一个 `<input>`，**不会有任何报错**：

    · 页面照常打开、按钮照常能点、控制台一片安静；
    · 只是那个设置项在 Web 档里**根本不存在**——用户看不到，永远改不了。

这类「静默缺失」是本项目最贵的一种缺陷（Tk 档一切正常，只有 Web 档少东西，
而没人会去数两档的控件个数）。这个脚本把这件事变成**可执行的判据**。

================================================================================
【判据（四个方向，各自独立报）】
================================================================================
    后端视角           前端视角         判据含义
    ─────────────────────────────────────────────────────────────────────
    vars               data-var         ① HTML 里的变量，后端必须真的登记过
    _EDITABLE_SETTINGS data-var         ② 每个可编辑设置项，HTML 必须有输入控件
    widgets            data-click       ③ HTML 点的按钮，后端必须真的有这个控件
    button 型 widgets  data-click       ④ 后端登记的按钮，HTML 得能点到（WARN）
    _semantic          app.js           ⑤ 前端调的语义动作，后端必须能按名字找到落点

①③⑤ 是 **ERROR**：名字对不上就会当场坏（点下去 500 / "未知语义动作"）。
②④ 是 **WARN**：可能是有意为之（比如某个按钮只有 Tk 档需要），但要人看一眼。

================================================================================
【用法】
================================================================================
    python tools/check_bindings.py          # 在项目根目录跑
    python tools/check_bindings.py -v       # 打印全部清单

（用的是装了 requirements.txt 的那个解释器即可；CI 里由 `pytest` 顺带调一次。）

退出码：0 = 无 ERROR；1 = 有 ERROR。
"""
import argparse
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

HTML = os.path.join(ROOT, "web", "index.html")
JS = os.path.join(ROOT, "web", "app.js")


def _read(path):
    with open(path, "r", encoding="utf-8") as f:
        return f.read()


# 后端控件名 → 前端 DOM id 的**显式登记表**（只在两边故意不同名时用）
#
# 为什么需要它：`_semantic` / 控件名用的是下划线（`src_settings_btn`），
# 而前端那两个 ⚙ 按钮的 id 是 `src-gear` / `dst-gear`（HTML 里 id 习惯用连字符）。
# 名字对不上，机器就没法自动认出来，只能落到"WARN 可能缺失"里 —— 而它们**在**。
# 与其让判据长期假红（会训练所有人无视红色），不如把这种"故意的不同名"登记下来：
# 新按钮要么走 data-click（自动匹配），要么在这里登记一行。
WIDGET_TO_DOM_ID = {
    "src_settings_btn": "src-gear",
    "dst_settings_btn": "dst-gear",
}


def html_bindings():
    """→ (data-var 集合, data-click 集合, id 集合)"""
    html = _read(HTML)
    var_names = set(re.findall(r'data-var="([A-Za-z0-9_]+)"', html))
    click_names = set(re.findall(r'data-click="([A-Za-z0-9_]+)"', html))
    ids = set(re.findall(r'id="([A-Za-z0-9_-]+)"', html))
    return var_names, click_names, ids


def js_bound_ids():
    """被 JS 按 id 绑过事件的元素。

    【为什么需要这条】`data-click` 不是唯一的绑定方式。实测 `#src-gear` / `#dst-gear`
    两个 ⚙ 按钮走的是 `$("#src-gear").addEventListener("click", ...)` —— 它们**在**，
    只是没有 data-click。只看 data-click 会把这它们误报成"Web 缺入口"。
    宁可让判据多认一种绑定方式，也不要制造假红：一条会误报的检查，
    训练的是所有人**无视红色**。
    """
    js = _read(JS)
    ids = set(re.findall(
        r"""\$\("#([A-Za-z0-9_-]+)"\)\s*\.\s*(?:addEventListener|onclick|on\()""", js))
    return ids


def js_bindings():
    """→ (SETTING_LABELS 的键, 前端调用到的语义动作名)"""
    js = _read(JS)
    m = re.search(r"const SETTING_LABELS\s*=\s*\{(.*?)\n\}\s*;", js, re.S)
    labels = set()
    if m:
        labels = set(re.findall(r"^\s*([A-Za-z0-9_]+)\s*:", m.group(1), re.M))
    # semantic("xxx") / semantic('xxx')
    actions = set(re.findall(r"""semantic\(\s*["']([A-Za-z0-9_]+)["']""", js))
    return labels, actions


def backend_bindings():
    """起一次无 Tk 装配拿到**真**的登记表，再静态解析 `_semantic` 的动作名。"""
    import web_app

    bridge = web_app.build()
    snap = bridge.snapshot()

    src = _read(os.path.join(ROOT, "web_app.py"))
    actions = set(re.findall(r'if action == "([A-Za-z0-9_]+)"', src))
    for group in re.findall(r"if action in \(([^)]*)\)", src):
        actions |= set(re.findall(r'"([A-Za-z0-9_]+)"', group))

    widget_kinds = {name: (m or {}).get("kind") for name, m in snap["widgets"].items()}
    return set(snap["vars"]), widget_kinds, actions, set(web_app._EDITABLE_SETTINGS)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("-v", "--verbose", action="store_true", help="打印完整清单")
    args = ap.parse_args()

    html_vars, html_clicks, html_ids = html_bindings()
    js_labels, js_actions = js_bindings()
    be_vars, be_widgets, be_actions, editable = backend_bindings()

    # 「前端点得到」的两种形态：data-click 统一绑定，或 id + JS 自己绑。
    reachable_clicks = set(html_clicks) | (html_ids & js_bound_ids())
    # 名字故意不同名的那几个，按登记表折算成后端控件名
    reachable_widgets = set(reachable_clicks) | {
        w for w, dom in WIDGET_TO_DOM_ID.items() if dom in reachable_clicks}

    errors, warns = [], []

    def err(code, msg):
        errors.append("[%s] %s" % (code, msg))

    def warn(code, msg):
        warns.append("[%s] %s" % (code, msg))

    # ① HTML 里的变量 → 后端必须登记过
    for name in sorted(html_vars - be_vars):
        err("①data-var", "HTML 绑定了 `%s`，但后端没有登记这个变量（拼错？还是变量被删了？）" % name)

    # ② 可编辑设置项 → HTML 必须有输入控件
    for name in sorted(editable - html_vars):
        err("②setting", "`%s` 是后端登记的可编辑设置项，但 index.html 里没有它的输入控件"
                         "（Web 档会静默缺失：用户看不到、改不了）" % name)

    # ③ HTML 点的按钮 → 后端必须有这个控件
    for name in sorted(html_clicks - set(be_widgets)):
        err("③data-click", "HTML 绑定了按钮 `%s`，但后端没有这个控件" % name)

    # ④ 后端登记的按钮 → 前端得点得到（data-click 或 id+JS 绑定都算；WARN）
    button_names = {n for n, k in be_widgets.items() if k in ("button", "ttkbutton")}
    for name in sorted(button_names - reachable_widgets):
        warn("④button", "后端有按钮 `%s`，前端既没有 data-click 也没有按 id 绑事件"
                        "（若本就不该出现在 Web 档可忽略）" % name)

    # ⑤ 前端调的语义动作 → 后端必须能按名字找到落点
    for name in sorted(js_actions - be_actions):
        err("⑤action", "app.js 调了语义动作 `%s`，但 web_app._semantic 里没有这一支"
                       "（点下去会返回「未知语义动作」）" % name)

    # 附：SETTING_LABELS 的键应该在 HTML 里有对应输入（WARN）
    for name in sorted(js_labels - html_vars):
        warn("⑥label", "SETTING_LABELS 里有 `%s`，但 HTML 没有同名 data-var（标签会成为死条目）" % name)

    if args.verbose:
        print("data-var    (%d): %s" % (len(html_vars), ", ".join(sorted(html_vars))))
        print("data-click  (%d): %s" % (len(html_clicks), ", ".join(sorted(html_clicks))))
        print("后端 vars   (%d)" % len(be_vars))
        print("后端按钮    (%d)" % len(button_names))
        print("语义动作 后端(%d): %s" % (len(be_actions), ", ".join(sorted(be_actions))))
        print("语义动作 前端(%d): %s" % (len(js_actions), ", ".join(sorted(js_actions))))
        print()

    for line in errors:
        print("ERROR " + line)
    for line in warns:
        print("WARN  " + line)

    print()
    print("结果：ERROR %d 条，WARN %d 条" % (len(errors), len(warns)))
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
