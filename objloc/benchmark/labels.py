# -*- coding: utf-8 -*-
"""标签判定口径 —— 实现已上游到 qsmy-deepseek-locator（2026-09-18），这里只做转出。

三套口径与它们所需的词表（PALETTE / COLOR_SYNONYMS / SHAPE_CN）都搬进了库的
bench_shapes.py（词表在库里的位置与本项目一致：紧挨判定函数）。本文件保留原模块路径
与全部名字（含 _fold_text / _TEXT_NOISE 这两个被转出的私有名），调用方不必改。

两套判定口径，由真值里的 label_mode 选择：
- 默认（几何图形）：颜色名 + 形状名，见 color_ok / shape_ok；
- label_mode="text"（网页截图等界面元素）：标签就是界面上的中文名称，走 text_label_ok（含别名）。
另外真值可声明 expect_shape=False（圆点阵就是这样：图上只写了颜色名，提示词也只问颜色），
此时只比颜色 —— 否则标签准确率会恒为 0，那是真值与提示词打架，不是模型不行。
"""

from __future__ import annotations

from qsmy_deepseek_locator.bench_shapes import (
    COLOR_SYNONYMS,
    PALETTE,
    SHAPE_CN,
    _TEXT_NOISE,
    _fold_text,
    color_ok,
    shape_ok,
    text_label_ok,
)

__all__ = [
    "PALETTE",
    "COLOR_SYNONYMS",
    "SHAPE_CN",
    "shape_ok",
    "color_ok",
    "text_label_ok",
]
