# -*- coding: utf-8 -*-
"""美化打印 benchmark 报告：python scripts/report.py [runs/benchmark/report.json]"""

import json
import sys


def main() -> int:
    path = sys.argv[1] if len(sys.argv) > 1 else "runs/benchmark/report.json"
    data = json.load(open(path, encoding="utf-8"))
    summary = data.get("summary", {})
    reports = data.get("reports", [])

    for rep in reports:
        print("=" * 96)
        space = rep.get("coord_space", "-")
        print(f"{rep['name']}  |  命中 {rep['hits']}/{rep['gt_count']}  |  平均IoU {rep['mean_iou']}  "
              f"|  坐标空间 {space}  |  工具 {rep.get('tool_calls')}  |  {rep.get('elapsed_s')}s")
        print(f"  预测数 {rep['pred_count']} (bbox {rep['pred_bbox_count']} / point {rep['pred_point_count']})")
        for t in rep["per_target"]:
            flag = "HIT " if t["hit"] else "MISS"
            print(f"  [{flag}] IoU={t['iou']:.3f}  GT='{t['gt_label']}'  ->  PRED='{t['pred_label']}'"
                  f"  (颜色{'✓' if t['color_ok'] else '✗'} 形状{'✓' if t['shape_ok'] else '✗'})")
        for gt, pred in zip(rep.get("ground_truth", []), rep.get("predictions", [])):
            if "bbox_2d" in pred:
                print(f"        GT {gt['bbox_2d']}  PRED {pred['bbox_2d']}")
        if rep.get("error"):
            print("  ERROR:", rep["error"])

    print("=" * 96)
    print("汇总：")
    for k, v in summary.items():
        print(f"  {k:16s}: {v}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
