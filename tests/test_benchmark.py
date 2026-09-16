# -*- coding: utf-8 -*-
"""benchmark 模块离线自测：图片生成 + IoU/匹配/指标（不调用模型）。"""

import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from objloc.benchmark import (  # noqa: E402
    aggregate,
    color_ok,
    evaluate_any_space,
    evaluate_sample,
    iou,
    make_samples,
    match_predictions,
    rescale_predictions,
    shape_ok,
    text_label_ok,
)

FAILED = []


def check(name, cond, extra=""):
    print(("PASS " if cond else "FAIL ") + name + ((" | " + str(extra)) if (not cond and extra) else ""))
    if not cond:
        FAILED.append(name)


# ---- IoU ----
check("iou identical", abs(iou([0, 0, 10, 10], [0, 0, 10, 10]) - 1.0) < 1e-9)
check("iou disjoint", iou([0, 0, 10, 10], [20, 20, 30, 30]) == 0.0)
check("iou half overlap", abs(iou([0, 0, 10, 10], [5, 0, 15, 10]) - (50 / 150)) < 1e-6)
check("iou reversed coords", abs(iou([10, 10, 0, 0], [0, 0, 10, 10]) - 1.0) < 1e-9)

# ---- 形状/颜色判定 ----
check("shape rect", shape_ok("rect", "蓝色矩形"))
check("shape rect synonym", shape_ok("rect", "红色长方形"))
check("shape circle", shape_ok("circle", "绿色圆形"))
check("shape circle not ellipse", not shape_ok("circle", "绿色椭圆形"))
check("shape ellipse", shape_ok("ellipse", "绿色椭圆形"))
check("shape triangle", shape_ok("triangle", "蓝色三角形"))
check("color blue", color_ok("蓝色", "蓝色矩形"))
check("color blue short", color_ok("蓝色", "深蓝矩形"))
check("color wrong", not color_ok("蓝色", "红色矩形"))

# ---- 文本标签判定（网页截图等界面元素用的口径，见 benchmark.text_label_ok）----
check("text exact", text_label_ok("总销售额", "总销售额"))
check("text punctuation", text_label_ok("总销售额（今日）", "总销售额 今日"))
check("text qualifier", text_label_ok("总销售额", "KPI 卡片：总销售额"))
check("text truncation", text_label_ok("无线降噪耳机", "降噪耳机"))
check("text wrong", not text_label_ok("总销售额", "退款率"))
check("text single char guard", not text_label_ok("我", "我们"))
check("text empty", not text_label_ok("总销售额", ""))
# 别名：同一个元素的不同叫法都算对（搜索框在图上写的是占位文字"搜索商品"）
check("text alias hit", text_label_ok("搜索商品", "搜索框", aliases=["搜索框", "搜索栏"]))
check("text alias hit 2", text_label_ok("搜索商品", "搜索栏", aliases=["搜索框", "搜索栏"]))
check("text alias miss", not text_label_ok("搜索商品", "购物车", aliases=["搜索框", "搜索栏"]))
check("text alias empty ok", text_label_ok("搜索商品", "搜索商品", aliases=[]))

# ---- 生成图片 ----
with tempfile.TemporaryDirectory() as tmp:
    samples = make_samples(3, seed=7, out_dir=tmp, width=600, height=480, n_shapes=3)
    check("generated 3 samples", len(samples) == 3)
    check("images written", all(os.path.exists(s.path) for s in samples))
    check("ground truth file", os.path.exists(os.path.join(tmp, "ground_truth.json")))

    for s in samples:
        gt = s.ground_truth()
        check(f"{s.name} gt count", len(gt) == 3)
        for item in gt:
            x1, y1, x2, y2 = item["bbox_2d"]
            ok = 0.0 <= x1 < x2 <= 1.0 and 0.0 <= y1 < y2 <= 1.0
            if not ok:
                check(f"{s.name} gt in range", False, item)
                break
        else:
            check(f"{s.name} gt in range", True)

    # ---- 完美预测 ----
    for s in samples[:1]:
        gt = s.ground_truth()
        perfect = [{"bbox_2d": g["bbox_2d"], "label": g["label"]} for g in gt]
        rep = evaluate_sample(perfect, gt)
        check("perfect detection_rate", rep["detection_rate"] == 1.0, rep)
        check("perfect mean_iou", rep["mean_iou"] == 1.0, rep)
        check("perfect label_accuracy", rep["label_accuracy"] == 1.0, rep)
        check("perfect precision", rep["precision"] == 1.0, rep)

        # 完全错误的标签
        wrong = [{"bbox_2d": g["bbox_2d"], "label": "某物"} for g in gt]
        rep2 = evaluate_sample(wrong, gt)
        check("wrong labels -> 0", rep2["label_accuracy"] == 0.0, rep2)

        # 空预测
        rep3 = evaluate_sample([], gt)
        check("empty preds", rep3["detection_rate"] == 0.0 and rep3["pred_count"] == 0)

    # ---- 匹配 ----
    gts = [{"bbox_2d": [0, 0, 100, 100]}, {"bbox_2d": [200, 200, 300, 300]}]
    preds = [{"bbox_2d": [2, 2, 98, 98]}, {"bbox_2d": [205, 205, 295, 295]}]
    m = match_predictions(preds, gts)
    check("match count", len(m) == 2, m)

    # ---- 汇总 ----
    agg = aggregate([evaluate_sample([{"bbox_2d": g["bbox_2d"], "label": g["label"]} for g in s.ground_truth()],
                                     s.ground_truth()) for s in samples])
    check("aggregate images", agg["images"] == 3)
    check("aggregate detection", agg["detection_rate"] == 1.0, agg)

    # ---- 坐标空间诊断 ----
    # 构造「像素坐标」预测：应被识别为 pixel，且换算后命中
    s = samples[0]
    gt = s.ground_truth()
    pixel_preds = [{
        "bbox_2d": [
            g["bbox_2d"][0] * s.width,
            g["bbox_2d"][1] * s.height,
            g["bbox_2d"][2] * s.width,
            g["bbox_2d"][3] * s.height,
        ],
        "label": g["label"],
    } for g in gt]
    diag = evaluate_any_space(pixel_preds, gt, s.width, s.height)
    check("detect pixel space", diag["space"] == "pixel", diag)
    check("pixel space ceiling", diag["detection_rate_best"] == 1.0, diag)

    # 相对比例预测：应被识别为 normalized
    norm_preds = [{"bbox_2d": g["bbox_2d"], "label": g["label"]} for g in gt]
    diag2 = evaluate_any_space(norm_preds, gt, s.width, s.height)
    check("detect normalized space", diag2["space"] == "normalized", diag2)

    # rescale_predictions 正确性
    scaled = rescale_predictions([{"bbox_2d": [0, 0, 450, 360], "label": "x"}], 900, 720)
    check("rescale to ratio", scaled[0]["bbox_2d"] == [0.0, 0.0, 0.5, 0.5], scaled)

print()
if FAILED:
    print("FAILED:", FAILED)
    sys.exit(1)
print("ALL BENCHMARK TESTS PASSED")
