# -*- coding: utf-8 -*-
"""内置工具集合 —— 实现已上游到 qsmy_deepseek-locator（0.2.0），这里只做转出。

六个工具（解析坐标 ×3 / 看图片信息 / 画标注图 / 列调色板）与它们的坐标归一化包装都在库里。

⚠️ 本项目的 `runs/scratch/` 输出目录**不在这里注入**，而是在 objloc/agent.py 的
run_agent 里作为工具运行上下文注入（库不认这个目录，也不该认）。
所以直接调用 build_default_registry() 拿到的注册表，其 annotate_image 需要调用方
自己给 output_dir，否则按库的默认落在当前工作目录。
"""

from __future__ import annotations

from qsmy_deepseek_locator.tools.builtin import (
    annotate_image,
    decode_json_points,
    extract_coordinates,
    get_image_info,
    list_palette_colors,
    parse_coordinates,
)
from qsmy_deepseek_locator.tools.builtin import build_default_registry

__all__ = [
    "annotate_image",
    "build_default_registry",
    "decode_json_points",
    "extract_coordinates",
    "get_image_info",
    "list_palette_colors",
    "parse_coordinates",
]
