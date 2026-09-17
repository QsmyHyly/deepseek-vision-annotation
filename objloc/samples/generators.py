# -*- coding: utf-8 -*-
"""生成器 1~4：纯 Python 绘制的内置测试图 + 它们的真值。

职责
----
- 生成器 1：彩色圆点阵（量帧几何 / 坐标约定）：make_marker_image / markers_to_gt；
- 生成器 2：文字可读性阶梯（量有效分辨率）：make_text_image / random_codes；
- 生成器 3：竖线带（量最小可分辨线间距）：make_band_image；
- 生成器 4：把已知场景等比缩放到任意栅格（分辨率扫描）：render_scaled。

边界
----
只管"怎么画 + 真值是什么"。**目录定义与提示词不在这里**（catalog.py），
**网页截图组也不在这里**（web_samples.py，那组图不是 Python 画的）。

旧文件对应关系（等价重构）
--------------------------
本模块 = 拆分前 `objloc/samples.py`（626 行）的「生成器 1」~「生成器 4」四节，
逐行搬移、无改写。其余两节分别落到 catalog.py（目录定义 + 对外接口）
与 web_samples.py（生成器 5）。

@doc AGENTS.md#4.3-坐标约定
（该文档规定真值为什么是 0.0~1.0 的相对比例。）
"""

from __future__ import annotations

import random
from pathlib import Path

from PIL import Image, ImageDraw

from objloc.benchmark import PALETTE, Sample, Shape, _draw_shape
from objloc.visualizer import resolve_font

# --------------------------------------------------------------------------- #
# 生成器 1：彩色圆点阵（量帧几何 / 坐标约定）
# --------------------------------------------------------------------------- #
# 圆点位置（相对坐标），刻意在四角/四边/中心铺开，便于拟合出斜率与截距
MARKER_FRACTIONS = [
    (0.12, 0.14), (0.50, 0.12), (0.88, 0.16),
    (0.14, 0.50), (0.50, 0.50), (0.86, 0.52),
    (0.12, 0.86), (0.50, 0.88), (0.88, 0.84),
]


def make_marker_image(path, width: int, height: int) -> list[dict]:
    """生成彩色圆点阵，返回真值 [{label, cx, cy, fx, fy, radius}]（像素坐标）。"""
    img = Image.new("RGB", (width, height), (247, 249, 252))
    draw = ImageDraw.Draw(img)
    radius = max(6, int(0.045 * min(width, height)))
    font = resolve_font(max(12, int(min(width, height) * 0.035)))
    names = list(PALETTE.keys())[:len(MARKER_FRACTIONS)]

    truth = []
    for (fx, fy), name in zip(MARKER_FRACTIONS, names):
        cx, cy = fx * width, fy * height
        draw.ellipse([cx - radius, cy - radius, cx + radius, cy + radius],
                     fill=PALETTE[name], outline=(30, 30, 30), width=2)
        draw.text((cx + radius + 6, cy - radius), name, fill=(40, 40, 40), font=font)
        truth.append({"label": name, "cx": cx, "cy": cy, "fx": fx, "fy": fy, "radius": radius})
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    img.save(path, format="PNG")
    return truth


def markers_to_gt(truth: list[dict], width: int, height: int) -> list[dict]:
    """把圆点真值转成可评测的 bbox 真值（0.0~1.0，kind=circle）。

    两个刻意选择（都踩过坑）：
    - label **只写颜色名**，不加"圆形"后缀。图上每个点旁边写的就是颜色名，
      MARKER_PROMPT 也只问颜色；若真值要求"红色圆形"而提示词只要颜色，
      标签准确率会恒为 0%——那是真值与提示词打架，不是模型不行。
    - match="center"：圆点直径只有 min(宽,高) 的 9%，在极端扁图上真值框窄到 0.011，
      用 IoU 0.5 判定等于在考"框画得多紧"，而这张图要测的是点位。详见 benchmark.center_hit()。
    """
    gt = []
    for item in truth:
        r = item["radius"]
        gt.append({
            "bbox_2d": [
                round((item["cx"] - r) / width, 4),
                round((item["cy"] - r) / height, 4),
                round((item["cx"] + r) / width, 4),
                round((item["cy"] + r) / height, 4),
            ],
            "label": item["label"],
            "kind": "circle",
            "color_name": item["label"],
            "expect_shape": False,   # 提示词只要颜色，不判形状
            "match": "center",       # 点位判定：真值中心落在预测框内即算命中
        })
    return gt


# --------------------------------------------------------------------------- #
# 生成器 2：文字可读性阶梯（量有效分辨率）
# --------------------------------------------------------------------------- #
# 字号单位是**原始像素**：同一套字号在不同栅格上渲染，模型的最小可读字号会随
# 服务端缩放因子等比变大，由此可反推真实缩放倍数。
TEXT_FONT_SIZES = [64, 48, 36, 28, 22, 16, 12, 9, 7, 5]
_CODE_ALPHABET = "ACDEFGHJKLMNPQRTUVWXY34679"


def random_codes(count: int, seed: int = 20260101) -> list[str]:
    """生成 count 个 4 位随机代码（去掉易混字符，避免判读歧义）。"""
    rng = random.Random(seed)
    return ["".join(rng.choice(_CODE_ALPHABET) for _ in range(4)) for _ in range(count)]


def make_text_image(path, width: int, height: int, sizes, codes) -> list[dict]:
    """生成字号阶梯图；返回真值 [{index, font_px, code}]。"""
    img = Image.new("RGB", (width, height), (255, 255, 255))
    draw = ImageDraw.Draw(img)
    line_h = height / len(sizes)
    index_font = resolve_font(max(14, int(min(width, height) * 0.022)))
    truth = []
    for i, (size, code) in enumerate(zip(sizes, codes)):
        top = i * line_h
        draw.rectangle([0, top, width - 1, top + line_h - 1], outline=(232, 232, 232), width=2)
        draw.text((width * 0.01, top + line_h / 2 - index_font.size * 0.6),
                  f"{i + 1}", fill=(0, 0, 0), font=index_font)
        font = resolve_font(size)
        draw.text((width * 0.10, top + line_h / 2 - size * 0.62), code, fill=(0, 0, 0), font=font)
        truth.append({"index": i + 1, "font_px": size, "code": code})
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    img.save(path, format="PNG")
    return truth


# --------------------------------------------------------------------------- #
# 生成器 3：竖线带（量最小可分辨线间距）
# --------------------------------------------------------------------------- #
BAND_SPECS = [  # (竖线间距 px, 条数) —— 间距递增、条数打乱，防止模型靠猜
    (2, 5), (3, 3), (4, 7), (5, 4), (6, 6),
    (8, 5), (10, 3), (12, 7), (16, 4), (24, 6),
]


def make_band_image(path, width: int, height: int, specs) -> list[dict]:
    """生成竖线带图片；返回真值 [{index, spacing, lines}]。"""
    img = Image.new("RGB", (width, height), (255, 255, 255))
    draw = ImageDraw.Draw(img)
    band_h = height // len(specs)
    font = resolve_font(max(18, band_h // 4))
    truth = []
    for i, (spacing, lines) in enumerate(specs):
        top = i * band_h
        draw.rectangle([0, top, width - 1, top + band_h - 1], outline=(200, 200, 200), width=2)
        draw.text((20, top + band_h // 2 - band_h // 8), str(i + 1), fill=(0, 0, 0), font=font)
        span = (lines - 1) * spacing
        x0 = width // 2 - span // 2
        for k in range(lines):
            x = x0 + k * spacing
            draw.rectangle([x, top + band_h // 5, x + 1, top + band_h - band_h // 5], fill=(0, 0, 0))
        truth.append({"index": i + 1, "spacing": spacing, "lines": lines})
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    img.save(path, format="PNG")
    return truth


# --------------------------------------------------------------------------- #
# 生成器 4：把已知场景等比缩放到任意栅格（分辨率扫描用）
# --------------------------------------------------------------------------- #
def render_scaled(reference: Sample, width: int, height: int, path) -> Sample:
    """把参考场景等比缩放到目标分辨率重新渲染，返回该尺寸下的 Sample。

    场景先在参考画布（默认 900x720）上生成，再整体缩放渲染 —— 各尺寸的真值
    （相对比例坐标）因此逐字节相同，精度差异才只能归因于栅格分辨率/服务端重采样，
    而不是"场景变了"。注意必须保持宽高比一致，否则图形会被拉伸。
    """
    scale = width / reference.width
    img = Image.new("RGB", (width, height), (247, 249, 252))
    draw = ImageDraw.Draw(img)
    draw.rectangle([0, 0, width - 1, height - 1], outline=(210, 218, 228), width=2)
    shapes = [
        Shape(kind=s.kind, color_name=s.color_name, rgb=s.rgb,
              bbox_px=tuple(round(v * scale, 1) for v in s.bbox_px))
        for s in reference.shapes
    ]
    for shape in shapes:
        _draw_shape(draw, shape)
    draw.text((16, 12), "#1", fill=(150, 160, 175), font=resolve_font(max(10, int(22 * scale))))
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    img.save(path, format="PNG")
    return Sample(name=path.stem, path=str(path), width=width, height=height, shapes=shapes)
