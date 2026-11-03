# -*- coding: utf-8 -*-
"""pytest 公共夹具。

【为什么测试这么少、这么挑】原项目有一整套 `_checks/` + 18 个 `_verify_*.py`
判据脚本，但它们**不在仓库里**（连本机都找不到了）。这套 `tests/` 是从零补的
最小骨架，只覆盖三类**出事会静默**的地方：

    ① schema 与列序（错位了没人报错，只是每次全量重传）
    ② Web 档的 HTTP 面（路由/流式，前端只看得到现象）
    ③ 前后端绑定一致性（少一个控件，页面照常打开、就是没有那个设置项）

覆盖面**不追求**替代原来那套回归 —— 那套要按原样找回来。
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
