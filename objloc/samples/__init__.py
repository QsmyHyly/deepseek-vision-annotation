# -*- coding: utf-8 -*-
"""内置测试图片目录（对外门面）—— 网页「测试图片」面板、探测脚本、评测脚本共用的唯一生成源。

为什么要有这个模块
------------------
"准确率由程序判定"的前提是图片答案已知。本项目所有测试图都由代码生成：每个图形/圆点的
真实位置在绘制时就拿到了，因此真值不依赖人眼标注，也不需要把二进制图片提交进仓库。

## 包内职责划分（原本是一个 626 行的 objloc/samples.py）

| 子模块 | 一句话职责 | 原文件对应段落 |
|---|---|---|
| `generators.py` | 生成器 1~4：纯 Python 绘制的内置测试图 + 真值 | 「生成器 1」~「生成器 4」 |
| `web_samples.py` | 生成器 5：真浏览器网页快照（真值来自 DOM） | 「生成器 5」 |
| `catalog.py` | 目录定义 + 各组提示词 + 对外接口（登记与调度） | 「目录定义」「对外接口」 |

⚠️ 提示词**只描述"找什么"，不描述"坐标怎么给"**：坐标口径的唯一来源是 `config.DEFAULT_SYSTEM_PROMPT`，
在本包里再抄一份就会让两种口径漂移（见 AGENTS.md#4.4-系统提示词）。

⚠️ 这次拆分是**按行区间逐行搬移**，没有任何改写：函数体、真值字段名、提示词文本、
随机数调用顺序一律原样（调用顺序决定同一 seed 下画出的图是否一致）。

@doc AGENTS.md#4.3-坐标约定
@doc AGENTS.md#7-打标准确率评测结论重要
（后一篇给出这些测试图在真实模型上的实测结论与判定口径对照表。）
"""

from __future__ import annotations

from .catalog import (
    BENCH_PROMPT,
    BAND_PROMPT,
    BY_ID,
    CATALOG,
    MARKER_PROMPT,
    RESOLUTION_RASTERS,
    RESOLUTION_REFERENCE,
    SAMPLES_DIR,
    TEXT_PROMPT,
    WEB_ARTICLE_PROMPT,
    WEB_DASHBOARD_PROMPT,
    WEB_FORM_PROMPT,
    WEB_SHOP_PROMPT,
    TestImage,
    accuracy,
    as_items,
    ensure,
    get,
    ground_truth,
    list_catalog,
    path_of,
)
from .generators import (
    BAND_SPECS,
    MARKER_FRACTIONS,
    TEXT_FONT_SIZES,
    make_band_image,
    make_marker_image,
    make_text_image,
    markers_to_gt,
    random_codes,
    render_scaled,
)
from .web_samples import (
    WEB_CAPTURE_SCRIPT,
    WEB_MANIFEST,
    WEB_SAMPLES_DIR,
    capture_web_samples,
    web_entry,
    web_targets_to_gt,
)

# 私有辅助与目录常量一并转出：拆分前它们就是 objloc.samples 的模块属性，
# 保持可用，避免「内部重构」意外变成对外破坏。
# （模块级可变状态 _WEB_MANIFEST 不转出：转出来只是一份快照；重抓有 force=True 这个正经出口。）
from .catalog import (  # noqa: F401
    _GROUP_BENCH,
    _GROUP_LEGIBILITY,
    _GROUP_MARKER,
    _GROUP_RES,
    _GROUP_WEB,
    _GT_CACHE,
    _band_builder,
    _bench_builder,
    _marker_builder,
    _text_builder,
)
from .generators import _CODE_ALPHABET, _draw_shape  # noqa: F401
from .web_samples import _web_builder  # noqa: F401

# 旧 objloc/samples.py 里顺带可用的**领域符号**也一并转出（真值模型、评分函数、字体解析、
# 根路径常量）。为什么转这些、不转 json / Path / Image：后者是 import 顺带进来的
# stdlib/三方名，从来不是本模块的接口；把偶然当契约转出来，只会让"公开面"越来越虚。
from objloc.benchmark import (  # noqa: F401
    PALETTE,
    Sample,
    Shape,
    evaluate_any_space,
    evaluate_sample,
    make_sample,
)
from objloc.config import PROJECT_ROOT, RUNS_DIR  # noqa: F401
from objloc.visualizer import resolve_font  # noqa: F401

__all__ = [
    "TestImage", "CATALOG", "SAMPLES_DIR", "BY_ID",
    "get", "path_of", "ensure", "ground_truth", "as_items", "list_catalog", "accuracy",
    "make_marker_image", "markers_to_gt", "make_text_image", "make_band_image",
    "render_scaled", "random_codes", "MARKER_FRACTIONS", "TEXT_FONT_SIZES", "BAND_SPECS",
    "WEB_SAMPLES_DIR", "WEB_MANIFEST", "WEB_CAPTURE_SCRIPT",
    "capture_web_samples", "web_entry", "web_targets_to_gt",
]
