# -*- coding: utf-8 -*-
"""判据：源列表**多选**（Tk / 替身 / 前端 三处必须同一套语义）。

【为什么这块值得单独钉】多选的失效**不会报错**：
  · 后端只认 `sel[0]` → 用户按 Ctrl 选了 5 个，点「－」只删掉 1 个（看着像没响应）；
  · 替身把选中存成标量 → 前端高亮只有一行，而后端以为选了 5 个；
  · 前端把 `selection` 当标量用 → `new Set(1)` 直接抛 TypeError，
    整个 `render()` 停更（进度、日志全不动，看着像服务挂了）。

三条都不抛到用户看得见的地方，只能靠判据。
"""
import inspect

import transfer_form
import web_app
import web_kit


# ---------------------------------------------------------------- 替身 Listbox
def _lb():
    w = web_kit.Widget("listbox")
    w._items = ["a", "b", "c", "d"]
    return w


def test_curselection_is_tuple_sorted_and_multi():
    w = _lb()
    w.selection_clear(0, "end")
    w.selection_set(2)
    w.selection_set(0)
    assert w.curselection() == (0, 2), "应返回升序元组，且是多个"


def test_selection_set_appends_like_tk():
    """Tk 的 `selection_set` 是**追加**，要替换得先 clear —— 替身必须照抄。"""
    w = _lb()
    w.selection_clear(0, "end")
    w.selection_set(1)
    w.selection_set(3, 3)
    assert w.curselection() == (1, 3)


def test_selection_set_range():
    w = _lb()
    w.selection_clear(0, "end")
    w.selection_set(1, 3)
    assert w.curselection() == (1, 2, 3)


def test_selection_clear_empties():
    w = _lb()
    w.selection_set(0)
    w.selection_clear(0, "end")
    assert w.curselection() == ()


def test_select_many_sets_at_once():
    w = _lb()
    w.select_many([3, 1, 1])
    assert w.curselection() == (1, 3), "去重 + 升序"


# ---------------------------------------------------------------- 后端动作
def test_bridge_select_list_accepts_indices():
    """`select_list` 收 indices（多选），并且**当前源 = 第一个选中项**。"""
    b = web_app.build()
    app = b.app
    app.source_list[:] = [r"D:\a", r"D:\b", r"D:\c"]
    app._form._refresh_source_listbox()

    got = b._invoke({"action": "select_list", "target": "source_listbox",
                     "indices": [0, 2]})
    assert got == [0, 2]
    model = b._widget_model(b.widgets["source_listbox"])
    assert model["selection"] == [0, 2], "模型要透出多个下标（前端据此高亮多行）"
    assert app.source_path.get() == r"D:\a", "当前源仍取第一个选中项（单一真相不变）"


def test_bridge_select_list_still_accepts_scalar_index():
    """老写法（index）必须继续有效 —— 前端缓存旧 JS 时不能直接崩。"""
    b = web_app.build()
    b.app.source_list[:] = ["x", "y"]
    b.app._form._refresh_source_listbox()
    assert b._invoke({"action": "select_list", "target": "source_listbox",
                      "index": 1}) == 1


# ---------------------------------------------------------------- Tk 侧逻辑
class _FakeListbox(object):
    def __init__(self, selected):
        self.selected = list(selected)
        self.items = []

    def curselection(self):
        return tuple(self.selected)

    def delete(self, a, b):
        self.items = []

    def insert(self, where, value):
        self.items.append(value)

    def selection_clear(self, a, b):
        self.selected = []

    def selection_set(self, first, last=None):
        hi = first if last is None else last
        self.selected = sorted(set(self.selected) | set(range(first, hi + 1)))

    def activate(self, i):
        return None

    def see(self, i):
        return None


class _FakeVar(object):
    def __init__(self):
        self.value = "keep"

    def set(self, v):
        self.value = v


def _form_method(name):
    for obj in vars(transfer_form).values():
        if inspect.isclass(obj) and name in obj.__dict__:
            return obj.__dict__[name]
    raise AssertionError("找不到 %s" % name)


class _FakeForm(object):
    _selected_indices = _form_method("_selected_indices")
    _refresh_source_listbox = _form_method("_refresh_source_listbox")
    _remove_selected_source = _form_method("_remove_selected_source")
    _move_selected_source = _form_method("_move_selected_source")


def _fake_form(items, selected):
    f = _FakeForm()
    f.source_list = list(items)
    f.source_listbox = _FakeListbox(selected)
    f.source_path = _FakeVar()
    return f


def test_remove_deletes_all_selected():
    f = _fake_form(["a", "b", "c", "d"], [1, 3])
    f._remove_selected_source()
    assert f.source_list == ["a", "c"], f.source_list


def test_remove_all_leaves_list_empty_and_clears_source_path():
    f = _fake_form(["a", "b"], [0, 1])
    f._remove_selected_source()
    assert f.source_list == []
    assert f.source_path.value == "", "列表删空必须清掉 source_path（否则开始键还拿老值传）"


def test_remove_none_selected_is_silent():
    f = _fake_form(["a"], [])
    f._remove_selected_source()
    assert f.source_list == ["a"]


def test_move_block_keeps_inner_order():
    f = _fake_form(["a", "b", "c", "d"], [1, 2])       # 选中 b,c
    f._move_selected_source(+1)
    assert f.source_list == ["a", "d", "b", "c"], f.source_list
    assert f.source_listbox.selected == [2, 3], "整块移动后要把整块重新选中"


def test_move_block_at_edge_does_nothing():
    f = _fake_form(["a", "b", "c"], [0, 1])
    assert f._move_selected_source(-1) is None
    assert f.source_list == ["a", "b", "c"]


def test_move_single_still_swaps():
    f = _fake_form(["a", "b", "c"], [0])
    assert f._move_selected_source(+1) == 1
    assert f.source_list == ["b", "a", "c"]
