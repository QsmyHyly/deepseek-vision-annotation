# -*- coding: utf-8 -*-
"""标签判定口径 —— 「模型说的名字对不对」。

职责
----
两套判定口径，由真值里的 `label_mode` 选择：
- 默认（几何图形）：颜色名 + 形状名，见 `color_ok` / `shape_ok`；
- `label_mode="text"`（网页截图等界面元素）：标签就是界面上的中文名称，
  走 `text_label_ok` 的文本比对（含别名）。

同时提供判定所需的**词表**：`PALETTE`（中文颜色名 → RGB）、`COLOR_SYNONYMS`（同义颜色说法）、
`SHAPE_CN`（形状英文名 → 中文名）。词表放这里而不是生成侧，是因为它只服务于「判定」。

边界
----
不做几何计算、不画图、不聚合指标（分别在 metrics.py / synth.py）。

旧文件对应关系（等价重构）
--------------------------
本模块 = 拆分前 `objloc/benchmark.py` 的「调色板」+「形状/颜色/文本标签判定」三节，
函数体与注释逐行照搬。
"""

from __future__ import annotations

import re
from typing import Iterable

# --------------------------------------------------------------------------- #
# 调色板：中文颜色名 + RGB
# --------------------------------------------------------------------------- #
PALETTE: dict[str, tuple[int, int, int]] = {
    "红色": (214, 48, 49),
    "绿色": (46, 160, 67),
    "蓝色": (52, 96, 219),
    "橙色": (245, 148, 20),
    "紫色": (150, 70, 200),
    "黄色": (232, 197, 20),
    "青色": (32, 178, 190),
    "粉色": (238, 120, 180),
    "灰色": (130, 138, 150),
    "棕色": (140, 90, 50),
}

COLOR_SYNONYMS: dict[str, list[str]] = {
    "红色": ["红"],
    "绿色": ["绿"],
    "蓝色": ["蓝"],
    "橙色": ["橙", "橘"],
    "紫色": ["紫"],
    "黄色": ["黄"],
    "青色": ["青", "蓝绿"],
    "粉色": ["粉"],
    "灰色": ["灰"],
    "棕色": ["棕", "褐"],
}

SHAPE_CN = {
    "rect": "矩形",
    "circle": "圆形",
    "ellipse": "椭圆形",
    "triangle": "三角形",
}


def shape_ok(kind: str, label: str) -> bool:
    """判断预测标签是否描述了正确的形状（容忍常见同义说法）。"""
    if kind == "rect":
        return any(k in label for k in ("矩形", "长方形", "方形", "四边形", "方块"))
    if kind == "circle":
        return ("圆" in label and "椭" not in label)
    if kind == "ellipse":
        return "椭" in label
    if kind == "triangle":
        return any(k in label for k in ("三角", "角形"))
    return False


def color_ok(color_name: str, label: str) -> bool:
    """判断预测标签是否描述了正确的颜色。"""
    synonyms = COLOR_SYNONYMS.get(color_name, [color_name[0]])
    return any(s in label for s in synonyms)


# 文本标签归一化时要去掉的噪声字符：空白 + 中英文常见标点。
# 网页元素的名称里经常带这些（"总销售额（今日）"、"加入购物车 >"），不归一化会误判为读错。
_TEXT_NOISE = re.compile(r"[\s，。、,.:：;；!！?？\"'“”‘’()（）\[\]【】<>《》/\\|_\-—~\`·]+")


def _fold_text(value: str) -> str:
    """归一化文本标签：去掉空白与标点、统一小写。"""
    return _TEXT_NOISE.sub("", str(value or "")).lower()


def text_label_ok(gt_label: str, pred_label: str, *, aliases: Iterable[str] = (),
                  min_len: int = 2) -> bool:
    """「文本标签」的判定口径：归一化后相等，或一方包含另一方。

    为什么不能复用 color_ok / shape_ok：那套是给几何图形用的（颜色名 + 形状名），
    而网页截图里的目标标签是**界面上的中文名称**（"总销售额"、"加入购物车"），
    既没有颜色也没有形状，硬套会得到恒为 0 的标签准确率。

    为什么允许"一方包含另一方"：模型常在名称前后补限定语（"KPI 卡片：总销售额"），
    也会把长文案截断（"无线降噪耳机" → "降噪耳机"）——这两种都算读对了。
    为防单字误命中，要求被包含的一方至少 min_len 个字符。

    aliases 是**同一元素的其它合理叫法**（真值里的 `aliases`，例如搜索框既写"搜索商品"
    也可以叫"搜索框"）。它只用来避免"答对了却判错"，不是用来兜住错误答案的：
    别名必须是"看着这张图的人也可能这么说"的名字。
    ⚠️ 别把模型可能给出的答案整批抄成别名——那样这项指标就失去意义了。

    真值在声明 `label_mode="text"` 时才会走到这里（见 evaluate_sample）。
    """
    a = _fold_text(gt_label)
    if not a:
        return False
    for candidate in (a, *(_fold_text(x) for x in aliases)):
        b = _fold_text(pred_label)
        if not candidate or not b:
            continue
        if candidate == b:
            return True
        if len(candidate) >= min_len and len(b) >= min_len and (candidate in b or b in candidate):
            return True
    return False
