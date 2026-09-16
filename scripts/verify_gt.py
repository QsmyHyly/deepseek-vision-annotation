# -*- coding: utf-8 -*-
"""独立复核：直接从渲染好的 PNG 像素里重新测量图形包围盒，与代码生成的 GT 比对。

不依赖任何生成时的中间数据，只读取图片本身 —— 相当于「第三方」重新量一遍。

用法：
    python scripts/verify_gt.py runs/benchmark_v2_hard
"""

from __future__ import annotations

import json
import os
import sys
from collections import Counter

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PIL import Image

BG = (247, 249, 252)
BG_TOL = 12


def _is_bg(r: int, g: int, b: int) -> bool:
    return abs(r - BG[0]) <= BG_TOL and abs(g - BG[1]) <= BG_TOL and abs(b - BG[2]) <= BG_TOL


def find_components(path: str, min_area: int = 300, drop_full_frame: bool = True):
    """返回 [(bbox_px, area, center_rgb)]，bbox_px=(x1,y1,x2,y2)。"""
    img = Image.open(path).convert("RGB")
    W, H = img.size
    px = img.load()

    mask = bytearray(W * H)
    for y in range(H):
        row = y * W
        for x in range(W):
            r, g, b = px[x, y]
            if not _is_bg(r, g, b):
                mask[row + x] = 1

    visited = bytearray(W * H)
    comps = []
    for start in range(W * H):
        if not mask[start] or visited[start]:
            continue
        stack = [start]
        visited[start] = 1
        x1 = x2 = start % W
        y1 = y2 = start // W
        area = 0
        while stack:
            p = stack.pop()
            area += 1
            x = p % W
            y = p // W
            if x < x1: x1 = x
            if x > x2: x2 = x
            if y < y1: y1 = y
            if y > y2: y2 = y
            if x > 0 and mask[p - 1] and not visited[p - 1]:
                visited[p - 1] = 1; stack.append(p - 1)
            if x < W - 1 and mask[p + 1] and not visited[p + 1]:
                visited[p + 1] = 1; stack.append(p + 1)
            if y > 0 and mask[p - W] and not visited[p - W]:
                visited[p - W] = 1; stack.append(p - W)
            if y < H - 1 and mask[p + W] and not visited[p + W]:
                visited[p + W] = 1; stack.append(p + W)

        bbox = (x1, y1, x2, y2)
        w, h = x2 - x1 + 1, y2 - y1 + 1
        if drop_full_frame and w > W * 0.98 and h > H * 0.98:
            continue  # 外边框
        if area < min_area:
            continue  # 文字/噪点

        # 取连通域内出现最多的颜色作为主色
        colors = Counter()
        for yy in range(y1, min(y2 + 1, H)):
            for xx in range(x1, min(x2 + 1, W)):
                if visited[yy * W + xx]:
                    colors[px[xx, yy]] += 1
        main_color = colors.most_common(1)[0][0] if colors else (0, 0, 0)
        comps.append((bbox, area, main_color))

    comps.sort(key=lambda c: -c[1])
    return comps, (W, H)


def main() -> int:
    root = sys.argv[1] if len(sys.argv) > 1 else "runs/benchmark/images"
    gt_path = os.path.join(root, "ground_truth.json")
    data = json.load(open(gt_path, encoding="utf-8"))

    total_max_err = 0.0
    for sample in data:
        W, H = sample["width"], sample["height"]
        comps, _ = find_components(sample["path"])
        gts = sample["ground_truth"]

        print("=" * 100)
        print(f"{sample['name']}  ({W}x{H})  检测到 {len(comps)} 个色块，GT {len(gts)} 个")
        print(f"{'来源':<6}{'bbox_px (重测/GT)':<44}{'相对比例 (重测)':<34}{'误差(px)'}")
        for bbox, area, color in comps:
            x1, y1, x2, y2 = bbox
            # 找最接近的 GT（按中心点）
            cx, cy = (x1 + x2) / 2, (y1 + y2) / 2
            best, best_d = None, 1e18
            for g in gts:
                gx1, gy1, gx2, gy2 = [v * (W if i % 2 == 0 else H)
                                      for i, v in enumerate(g["bbox_2d"])]
                gcx, gcy = (gx1 + gx2) / 2, (gy1 + gy2) / 2
                d = (gcx - cx) ** 2 + (gcy - cy) ** 2
                if d < best_d:
                    best, best_d = g, d
            gx1, gy1, gx2, gy2 = [round(v) for v in (
                best["bbox_2d"][0] * W, best["bbox_2d"][1] * H,
                best["bbox_2d"][2] * W, best["bbox_2d"][3] * H)]
            err = max(abs(x1 - gx1), abs(y1 - gy1), abs(x2 - gx2), abs(y2 - gy2))
            total_max_err = max(total_max_err, err)
            norm = [round(x1 / W, 4), round(y1 / H, 4),
                    round(x2 / W, 4), round(y2 / H, 4)]
            print(f"{'重测':<6}{(x1, y1, x2, y2)!s:<44}{norm!s:<34}{err}")
            print(f"{'GT  ':<6}{(gx1, gy1, gx2, gy2)!s:<44}{[round(v, 4) for v in best['bbox_2d']]!s:<34}  [{best['label']}]")

    print("=" * 100)
    print(f"全部样本最大边框像素误差: {total_max_err} px（外描边宽 5px，中心线绘制，故约 ±3px 属正常）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
