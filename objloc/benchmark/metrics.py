# -*- coding: utf-8 -*-
"""匹配、命中判定与指标聚合 —— 实现已上游到 qsmy-deepseek-locator（2026-09-18），这里只做转出。

为什么改成转出
--------------
这套判分口径（IoU 匹配 / 中心点命中 / 文本标签 / 坐标空间诊断）是本项目与库**共用的一套规则**：
库自己的 bench 要用它，安卓 App 镜像过同一份代码，本项目的历史评测结论也靠它。
规则放在库里维护才只有一份，本文件保留原模块路径与全部名字，所以调用方一行都不用改。

⚠️ 转出前后**判分结果逐字段一致**：2026-09-18 用 20 组随机数据（几何图真值 / 圆点阵真值 /
文本标签真值，含点预测与"框住整图"的极端框）对拍过两边实现，per_target 与所有汇总字段全等。

两处刻意不统一的判定口径（都踩过坑，别顺手"统一"掉）—— 详细理由在库的 bench_score.py：
1. **中心点命中**：圆点阵与网页控件用真值里的 match="center"，其余用 IoU 阈值；
2. **文本标签**：真值声明 label_mode="text" 时走文本比对，且形状判定视为通过。

@doc AGENTS.md#7-打标准确率评测结论重要
（该文档给出这些指标在真实模型上的实测数值与判定口径对照表。）
"""

from __future__ import annotations

from qsmy_deepseek_locator.bench_score import (
    aggregate,
    center_hit,
    evaluate_any_space,
    evaluate_sample,
    match_predictions,
    rescale_predictions,
)
# 本项目历史上把这个函数叫 iou()；库里叫 box_iou（在 parsing.py，与图省事的名字相比，
# 它所在的模块才是它真正的归属）。两边实现逐行等价，这里保留旧名以免调用点改写。
from qsmy_deepseek_locator.parsing import box_iou as iou

__all__ = [
    "center_hit",
    "iou",
    "match_predictions",
    "rescale_predictions",
    "evaluate_any_space",
    "evaluate_sample",
    "aggregate",
]
