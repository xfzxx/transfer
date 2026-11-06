# -*- coding: utf-8 -*-
r"""生成程序图标 `app.ico`（256/128/64/48/32/16 六个尺寸）。

【为什么要自己画】仓库里原来一个图标都没有，spec 里也没有 `icon=` ——
打出来的 exe 用的是 PyInstaller 的默认图标，桌面和开始菜单上是那个通用图标，
医院的人一眼看不出这是哪个软件。而"看不出"的另一个后果是 IT 装完后
**无法从文件属性确认版本**（版本资源也没有）。

【设计】临床蓝底（`#0a58ca`，与 `bootstrap-light` 主题的主色一致）+ 白色双向箭头，
一眼能看出"传输"。不做细描边：16x16 下任何细节都会糊成一团，只保留大色块。

用法：
    python tools/make_icon.py         # 生成 app.ico（覆盖）
"""
import os

from PIL import Image, ImageDraw

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(ROOT, "app.ico")

BLUE = (10, 88, 202, 255)        # #0a58ca —— 与界面主色一致
WHITE = (255, 255, 255, 255)


def draw(size):
    """按尺寸画一张。大尺寸加圆角与箭头细节，小尺寸只留最粗的形状。"""
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)

    # 圆角方块底
    r = max(2, int(size * 0.18))
    d.rounded_rectangle([0, 0, size - 1, size - 1], radius=r, fill=BLUE)

    # 双向箭头：上箭头向右、下箭头向左（"传过去 / 传回来"）
    w = size
    if size <= 32:
        # 【小尺寸单独一套比例】16px 上按大图比例画，两根箭头会糊成一坨
        # （实测只有 5px 的间隙）。这里把杆加粗、箭头加大、两行拉得更开，
        # 宁可看起来"笨"，也要在任务栏/标题栏上认得出是双向箭头。
        thick = max(2, int(w * 0.19))
        head = max(3, int(w * 0.28))
        pad = int(w * 0.14)
        y1, y2 = int(w * 0.31), int(w * 0.69)
    else:
        thick = max(2, int(w * 0.11))    # 箭杆粗细
        head = max(3, int(w * 0.20))     # 箭头长度
        pad = int(w * 0.22)
        y1 = int(w * 0.36)
        y2 = int(w * 0.64)
    left, right = pad, w - pad

    # 上箭头（右向）
    d.rectangle([left, y1 - thick // 2, right - head, y1 + thick // 2], fill=WHITE)
    d.polygon([(right, y1), (right - head, y1 - head), (right - head, y1 + head)], fill=WHITE)

    # 下箭头（左向）
    d.rectangle([left + head, y2 - thick // 2, right, y2 + thick // 2], fill=WHITE)
    d.polygon([(left, y2), (left + head, y2 - head), (left + head, y2 + head)], fill=WHITE)

    return img


def main():
    sizes = [256, 128, 64, 48, 32, 16]
    imgs = [draw(s) for s in sizes]
    imgs[0].save(OUT, format="ICO", sizes=[(s, s) for s in sizes])
    print("已生成 %s（%.1f KB，%d 个尺寸）" % (OUT, os.path.getsize(OUT) / 1024.0, len(sizes)))
    for s in sizes:
        draw(s).save(os.path.join(ROOT, "build", "icon_%d.png" % s)) if os.path.isdir(
            os.path.join(ROOT, "build")) else None
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
