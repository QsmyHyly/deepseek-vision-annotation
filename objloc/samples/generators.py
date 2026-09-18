# -*- coding: utf-8 -*-
"""生成器 1~4：实现已上游到 qsmy-deepseek-locator，本文件是**注入本项目字体的包装层**。

为什么是「上游 + 注入」而不是直接转出
------------------------------------
这几个生成器是跨项目共用的评测素材（库自己的 bench 也要用），实现只该有一份，所以搬进了库。
但库**不打包字体**，默认用当前机器的系统字体；本项目的 resolve_font 自带字体字节，
两者画出的文字像素不同（实测圆点阵差 5265 像素，全在文字区，几何真值不受影响）。
评测素材要的是「换台机器跑，图还是同一张」，所以这层包装必须把本项目的字体解析传下去 ——
否则本项目的历史评测结论（尤其「文字可读性阶梯」那组，它测的就是字形）不再可比。

不画图的那些名字（markers_to_gt / random_codes，以及 samples/__init__.py 依赖的
_CODE_ALPHABET / _draw_shape）直接转出，行为与上游化之前一致。

真值仍一律是 0.0~1.0 的相对比例 —— 坐标口径的唯一来源仍是 config.DEFAULT_SYSTEM_PROMPT。
@doc AGENTS.md#4.3-坐标约定
"""

from __future__ import annotations

from qsmy_deepseek_locator.bench_generators import (
    BAND_SPECS,
    MARKER_FRACTIONS,
    TEXT_FONT_SIZES,
    _CODE_ALPHABET,
    make_band_image as _make_band_image,
    make_marker_image as _make_marker_image,
    make_text_image as _make_text_image,
    markers_to_gt,
    random_codes,
    render_scaled as _render_scaled,
)
from qsmy_deepseek_locator.bench_shapes import _draw_shape

from objloc.visualizer import resolve_font

# --------------------------------------------------------------------------- #
# 画图的四个函数：唯一要做的就是把**本项目自带的字体解析**注入进库的实现
# --------------------------------------------------------------------------- #
# 库不打包字体（体积 + 许可），默认探测的是当前机器的系统字体 —— 那样同一段代码在不同
# 机器上画出的文字像素并不相同。而这几张图是评测素材，「换台机器跑、图还是同一张」是它的
# 基本要求；本项目的 resolve_font 自带字体字节，正是为此。实测：不注入时圆点阵会差 5265 个
# 像素（全落在文字区），几何真值一字不差 —— 图看上去完全正常，只有历史结论悄悄不可比。


def make_marker_image(path, width: int, height: int) -> list[dict]:
    """彩色圆点阵。实现与真值口径见库 bench_generators.make_marker_image。"""
    return _make_marker_image(path, width, height, font_resolver=resolve_font)


def make_text_image(path, width: int, height: int, sizes, codes) -> list[dict]:
    """文字可读性阶梯。**这组图测的就是字形**，所以字体必须走本项目自带的那份。"""
    return _make_text_image(path, width, height, sizes, codes, font_resolver=resolve_font)


def make_band_image(path, width: int, height: int, specs) -> list[dict]:
    """竖线带（量最小可分辨线间距）。"""
    return _make_band_image(path, width, height, specs, font_resolver=resolve_font)


def render_scaled(reference, width: int, height: int, path):
    """把参考场景等比缩放到任意栅格（分辨率扫描用）。"""
    return _render_scaled(reference, width, height, path, font_resolver=resolve_font)


__all__ = [
    "BAND_SPECS",
    "MARKER_FRACTIONS",
    "TEXT_FONT_SIZES",
    "make_band_image",
    "make_marker_image",
    "make_text_image",
    "markers_to_gt",
    "random_codes",
    "render_scaled",
]
