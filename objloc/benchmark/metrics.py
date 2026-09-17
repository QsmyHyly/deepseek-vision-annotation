# -*- coding: utf-8 -*-
"""匹配、命中判定与指标聚合 —— 「这次打标算对了几分」。

职责
----
- 几何原语：`iou()`（框重合度）、`center_hit()`（中心点命中，另带面积比上限防作弊）；
- 匹配：`match_predictions()` 按 IoU 贪心配对预测框与真值框；
- 评分：`evaluate_sample()` 单张、`aggregate()` 多张汇总；
- 诊断：`rescale_predictions()` / `evaluate_any_space()` 判断模型给的是归一化还是像素坐标。

边界
----
只吃"已经产生的预测与真值"，不生成图片（synth.py）、不判标签词义（labels.py）。
纯逻辑，不依赖网络与模型。

两处刻意不统一的判定口径（都踩过坑，别顺手"统一"掉）
--------------------------------------------------
1. **中心点命中**：圆点阵与网页控件用 `match == "center"`，其余用 IoU 阈值。
   理由见 `center_hit()`：这两类目标的真值框太小，"框画得多紧"会盖过"有没有找到"。
2. **文本标签**：真值声明 `label_mode="text"` 时走文本比对，且把形状判定视为通过
   （`s_ok = True`），否则标签准确率会恒为 0 —— 那是真值与提示词打架，不是模型不行。

旧文件对应关系（等价重构）
--------------------------
本模块 = 拆分前 `objloc/benchmark.py` 的「评估指标」一节 + `center_hit()`，
函数体、舍入位数与字段名逐行照搬（字段名是下游 report.json 的契约）。

@doc AGENTS.md#7-打标准确率评测结论重要
（该文档给出这些指标在真实模型上的实测数值与判定口径对照表。）
"""

from __future__ import annotations

from typing import Iterable

from .labels import color_ok, shape_ok, text_label_ok


def center_hit(pred_box: Iterable[float], gt_box: Iterable[float],
               max_area_ratio: float = 25.0) -> bool:
    """圆形/点目标专用的命中判定：真值中心落在预测框内，且预测框没有大到离谱。

    为什么不能直接用 IoU 阈值：圆点阵里真值框就是那个圆本身，直径约为
    min(宽,高) 的 9%。在 2400×300 这种极端扁图上，圆点真值框的归一化宽度只有 0.011，
    模型即使把点找准了，只要框画得松一点 IoU 就掉到 0.5 以下——那测的是"框画得多紧"，
    不是"点定位准不准"，而圆点图的用途恰恰是量点位。

    max_area_ratio 用来挡住"框住整张图"的作弊解：预测框面积不得超过真值框的 25 倍。
    """
    px1, py1, px2, py2 = pred_box
    gx1, gy1, gx2, gy2 = gt_box
    cx, cy = (gx1 + gx2) / 2, (gy1 + gy2) / 2
    inside = min(px1, px2) <= cx <= max(px1, px2) and min(py1, py2) <= cy <= max(py1, py2)
    if not inside:
        return False
    pred_area = abs(px2 - px1) * abs(py2 - py1)
    gt_area = abs(gx2 - gx1) * abs(gy2 - gy1)
    if gt_area <= 0:
        return False
    return pred_area <= gt_area * max_area_ratio


def iou(box_a: Iterable[float], box_b: Iterable[float]) -> float:
    """两个 [x1,y1,x2,y2] 框的 IoU。"""
    ax1, ay1, ax2, ay2 = box_a
    bx1, by1, bx2, by2 = box_b
    ax1, ax2 = min(ax1, ax2), max(ax1, ax2)
    ay1, ay2 = min(ay1, ay2), max(ay1, ay2)
    bx1, bx2 = min(bx1, bx2), max(bx1, bx2)
    by1, by2 = min(by1, by2), max(by1, by2)

    ix1, iy1 = max(ax1, bx1), max(ay1, by1)
    ix2, iy2 = min(ax2, bx2), min(ay2, by2)
    iw, ih = max(0.0, ix2 - ix1), max(0.0, iy2 - iy1)
    inter = iw * ih
    if inter <= 0:
        return 0.0
    area_a = max(0.0, ax2 - ax1) * max(0.0, ay2 - ay1)
    area_b = max(0.0, bx2 - bx1) * max(0.0, by2 - by1)
    union = area_a + area_b - inter
    return inter / union if union > 0 else 0.0


def match_predictions(preds: list[dict], gts: list[dict]) -> list[tuple[int, int, float]]:
    """按 IoU 贪心匹配预测框与真值框，返回 [(pred_idx, gt_idx, iou)]。"""
    pairs: list[tuple[float, int, int]] = []
    for pi, pred in enumerate(preds):
        if "bbox_2d" not in pred:
            continue
        for gi, gt in enumerate(gts):
            v = iou(pred["bbox_2d"], gt["bbox_2d"])
            if v > 0:
                pairs.append((v, pi, gi))
    pairs.sort(reverse=True)

    used_p: set[int] = set()
    used_g: set[int] = set()
    matches: list[tuple[int, int, float]] = []
    for v, pi, gi in pairs:
        if pi in used_p or gi in used_g:
            continue
        used_p.add(pi)
        used_g.add(gi)
        matches.append((pi, gi, v))
    return matches


def rescale_predictions(preds: list[dict], width: int, height: int) -> list[dict]:
    """把「像素坐标」的预测换算成 0.0~1.0 的相对比例。

    仅用于诊断：模型若无视约定直接给像素值，这里按原图尺寸换算，
    得到「如果外部帮忙换算」的上限成绩。真实链路不会这样兜底，
    因为服务端缩放后的帧尺寸调用方拿不到（见 scripts/probe_vision_frame.py）。
    """
    out: list[dict] = []
    for p in preds:
        if not isinstance(p, dict) or "bbox_2d" not in p:
            continue
        x1, y1, x2, y2 = p["bbox_2d"]
        out.append({
            **p,
            "bbox_2d": [
                x1 / width,
                y1 / height,
                x2 / width,
                y2 / height,
            ],
        })
    return out


def evaluate_any_space(
    preds: list[dict],
    gts: list[dict],
    width: int,
    height: int,
    *,
    iou_threshold: float = 0.5,
) -> dict:
    """分别按「已归一化」和「像素坐标」两种解释评估，取更优者。

    用于诊断模型是否遵守了 0.0~1.0 相对坐标约定：
    - space == "normalized" 表示模型直接给的就是归一化坐标；
    - space == "pixel"      表示模型给的是像素坐标（约定遵守失败），
      此时 *_pixel 指标代表「如果外部帮忙换算」能达到的准确率上限。
    """
    as_is = evaluate_sample(preds, gts, iou_threshold=iou_threshold)
    as_pixel = evaluate_sample(
        rescale_predictions(preds, width, height), gts, iou_threshold=iou_threshold
    )
    better_pixel = as_pixel["mean_iou"] > as_is["mean_iou"]
    best = as_pixel if better_pixel else as_is
    return {
        "space": "pixel" if better_pixel else "normalized",
        "hits_best": best["hits"],
        "detection_rate_best": best["detection_rate"],
        "mean_iou_best": best["mean_iou"],
        "label_accuracy_best": best["label_accuracy"],
        "report_normalized": as_is,
        "report_pixel": as_pixel,
    }


def evaluate_sample(preds: list[dict], gts: list[dict], *, iou_threshold: float = 0.5) -> dict:
    """评估单张图片的预测结果。"""
    bbox_preds = [p for p in preds if isinstance(p, dict) and "bbox_2d" in p]
    matches = match_predictions(bbox_preds, gts)
    # good 只用于统计"命中数"，判定口径与 per_target.hit 保持一致
    good = [
        (pi, gi, v) for pi, gi, v in matches
        if (center_hit(bbox_preds[pi]["bbox_2d"], gts[gi]["bbox_2d"])
            if gts[gi].get("match") == "center" else v >= iou_threshold)
    ]

    label_hits = 0
    color_hits = 0
    shape_hits = 0
    per_target = []
    for pi, gi, v in matches:
        pred_label = str(bbox_preds[pi].get("label", ""))
        gt = gts[gi]
        text_mode = gt.get("label_mode") == "text"
        if text_mode:
            # 文本标签口径（网页截图等）：目标是界面上的中文名称，没有颜色/形状可言，
            # 直接比文本；s_ok 恒真，避免 color_ok/shape_ok 把标签准确率压成 0。
            c_ok = text_label_ok(gt.get("label", ""), pred_label,
                                 aliases=gt.get("aliases") or [])
            s_ok = True
        else:
            c_ok = color_ok(gt["color_name"], pred_label)
            # 有些真值不要求判形状（例如圆点阵，图里只写了颜色名，提示词也只问颜色），
            # 此时把 shape_ok 视为通过，否则标签准确率会恒为 0——那是真值与提示词打架。
            s_ok = True if gt.get("expect_shape") is False else shape_ok(gt["kind"], pred_label)
        l_ok = c_ok and s_ok
        label_hits += int(l_ok)
        color_hits += int(c_ok)
        shape_hits += int(s_ok)
        per_target.append({
            "gt_label": gt["label"],
            "pred_label": pred_label,
            # text 模式下列的 color_ok 实际含义是"文本标签是否对上"，字段名沿用以免破坏既有消费方
            "label_mode": "text" if text_mode else "color_shape",
            "iou": round(v, 3),
            "color_ok": c_ok,
            "shape_ok": s_ok,
            "label_ok": l_ok,
            # 圆形/点目标按"中心点落在框内"判定命中，其余仍用 IoU 阈值，理由见 center_hit()
            "hit": (center_hit(bbox_preds[pi]["bbox_2d"], gt["bbox_2d"])
                    if gt.get("match") == "center" else v >= iou_threshold),
        })

    n_gt = len(gts)
    n_pred = len(preds)
    n_match = len(matches)
    return {
        "gt_count": n_gt,
        "pred_count": n_pred,
        "pred_bbox_count": len(bbox_preds),
        "pred_point_count": len(preds) - len(bbox_preds),
        "matched": n_match,
        "hits": len(good),
        "detection_rate": round(len(good) / n_gt, 3) if n_gt else 0.0,
        "precision": round(len(good) / n_pred, 3) if n_pred else 0.0,
        "mean_iou": round(sum(v for _, _, v in matches) / n_match, 3) if n_match else 0.0,
        "label_accuracy": round(label_hits / n_match, 3) if n_match else 0.0,
        "color_accuracy": round(color_hits / n_match, 3) if n_match else 0.0,
        "shape_accuracy": round(shape_hits / n_match, 3) if n_match else 0.0,
        "per_target": per_target,
    }


def aggregate(reports: list[dict]) -> dict:
    """汇总多张图片的评估结果。"""
    total_gt = sum(r["gt_count"] for r in reports)
    total_pred = sum(r["pred_count"] for r in reports)
    total_hits = sum(r["hits"] for r in reports)
    total_matched = sum(r["matched"] for r in reports)

    all_pairs = [t for r in reports for t in r["per_target"]]
    mean_iou = (sum(t["iou"] for t in all_pairs) / len(all_pairs)) if all_pairs else 0.0

    def _acc(key: str) -> float:
        if not all_pairs:
            return 0.0
        return sum(int(t[key]) for t in all_pairs) / len(all_pairs)

    return {
        "images": len(reports),
        "total_gt": total_gt,
        "total_pred": total_pred,
        "total_hits": total_hits,
        "total_matched": total_matched,
        "detection_rate": round(total_hits / total_gt, 3) if total_gt else 0.0,
        "precision": round(total_hits / total_pred, 3) if total_pred else 0.0,
        "mean_iou": round(mean_iou, 3),
        "label_accuracy": round(_acc("label_ok"), 3),
        "color_accuracy": round(_acc("color_ok"), 3),
        "shape_accuracy": round(_acc("shape_ok"), 3),
        # 允许「像素坐标」解释时的上限（用于诊断坐标约定是否被遵守）
        "total_hits_best_space": sum(r.get("hits_best", r["hits"]) for r in reports),
        "detection_rate_best_space": (
            round(sum(r.get("hits_best", r["hits"]) for r in reports) / total_gt, 3)
            if total_gt else 0.0
        ),
        "pixel_space_images": sum(1 for r in reports if r.get("coord_space") == "pixel"),
    }
