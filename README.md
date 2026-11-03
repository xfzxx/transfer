# transfer —— 纯 Tk 版（冻结分支）

这是**加入浏览器界面之前的最后一代**。界面只有一个 Tk 窗口，不需要 `pywebview`、
不需要 WebView2 Runtime、不会打开任何浏览器。

| | |
|---|---|
| 分支起点 | `20c675f`（2026-10-17） |
| 原始快照 | 标签 `v0.9-tk` 指向同一个提交，**未经本文件改动**的原始状态 |
| 当前开发 | 已转入 `main`（Web 档：`--c3` WebView2 窗口壳 / `--c4` 系统浏览器） |

## 为什么留这个分支

`main` 在 2026-10-18 引入了 Web 界面（`f7b2211` 落地前端，`7ba9fba` 把打包入口切到
`web_app.py`）。从那以后 exe 默认走浏览器壳，纯 Tk 窗口这条线就冻结了。

留成独立分支是为了：想回到没有浏览器的形态时，不用翻历史猜是哪个提交。

## 运行

```bash
pip install ttkbootstrap Pillow
python file_transfer.py
```

这一版**不需要** `pywebview`，也**不认** `--c3` / `--c4` / `--port` 这些参数。

## 打包

```bash
pyinstaller 文件传输工具.spec
```

这一版 `文件传输工具.spec` 的入口就是 `file_transfer.py`（`main` 上已改成 `web_app.py`，
两个分支的 spec **不通用**，别混用）。

## 注意

- **分支冻结**，不再接受新功能；`main` 上的修复不会自动回流到这里。
- 数据落点规则相同：环境变量 `FTP_TRANSFER_DATA_DIR` > 程序同级 `data\` >
  `%LOCALAPPDATA%\文件传输工具\data`（仅打包后兜底）。
- `data\` 含 FTP 明文口令，已在 `.gitignore` 屏蔽，不入库。
