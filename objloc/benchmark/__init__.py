# -*- coding: utf-8 -*-
"""合成测试图片 + 打标准确率评估（纯逻辑，不依赖网络/模型）。

用途：
- make_samples()  用代码生成若干张「已知答案」的几何图形图片 + ground truth；
- iou / match_predictions / evaluate_sample / aggregate  计算打标准确率。

坐标约定与项目一致：0.0~1.0 的相对比例（占宽/高）。

标签判定有两套口径，由真值里的 `label_mode` 选择：
- 默认（几何图形）：颜色名 + 形状名，见 color_ok / shape_ok；
- `label_mode="text"`（网页截图等界面元素）：标签就是界面上的中文名称，见 text_label_ok。

## 包内职责划分（原本是一个 555 行的 objloc/benchmark.py）

| 子模块 | 一句话职责 | 原文件对应段落 |
|---|---|---|
| `labels.py` | 标签判定口径 + 词表（颜色 / 形状 / 文本别名） | 「调色板」「形状/颜色/文本标签判定」 |
| `metrics.py` | 几何命中判定、匹配与指标聚合、坐标空间诊断 | 「评估指标」+ center_hit() |
| `synth.py` | 合成样本生成：已知答案的几何图 + 真值 | 「数据模型」「图像生成」 |

⚠️ 这次拆分是**纯搬移**：函数体、舍入位数、返回字段名一律原样（字段名是 report.json 的契约）。

@doc AGENTS.md#7-打标准确率评测结论重要
（该文档给出这套指标在真实模型上的实测数值与判定口径对照表。）
"""

from __future__ import annotations

from .labels import (
    COLOR_SYNONYMS,
    PALETTE,
    SHAPE_CN,
    color_ok,
    shape_ok,
    text_label_ok,
)
from .metrics import (
    aggregate,
    center_hit,
    evaluate_any_space,
    evaluate_sample,
    iou,
    match_predictions,
    rescale_predictions,
)
from .synth import (
    Sample,
    Shape,
    make_sample,
    make_samples,
)

# 私有辅助一并转出：拆分前它们是 objloc.benchmark 的模块属性，
# objloc/samples/ 就依赖 benchmark._draw_shape（分辨率重渲染共用同一份绘制逻辑）。
from .labels import _fold_text, _TEXT_NOISE  # noqa: F401
from .synth import _darken, _draw_shape  # noqa: F401
# 项目内顺带可见的名字（拆分前 objloc.benchmark.resolve_font 能取到）。
# 只有 stdlib / 三方名（json、Path、Image、dataclass…）不再转出 —— 那些不是本模块的接口。
from objloc.visualizer import resolve_font  # noqa: F401

__all__ = [
    "PALETTE", "SHAPE_CN", "Shape", "Sample",
    "make_sample", "make_samples",
    "iou", "match_predictions", "evaluate_sample", "aggregate",
    "rescale_predictions", "evaluate_any_space",
    "shape_ok", "color_ok", "text_label_ok",
]
