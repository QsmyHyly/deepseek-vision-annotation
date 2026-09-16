"""合成测试图片 + 打标准确率评估（纯逻辑，不依赖网络/模型）。

用途：
- make_samples()  用代码生成若干张「已知答案」的几何图形图片 + ground truth；
- iou / match_predictions / evaluate_sample / aggregate  计算打标准确率。

坐标约定与项目一致：0.0~1.0 的相对比例（占宽/高）。

标签判定有两套口径，由真值里的 `label_mode` 选择：
- 默认（几何图形）：颜色名 + 形状名，见 color_ok / shape_ok；
- `label_mode="text"`（网页截图等界面元素）：标签就是界面上的中文名称，见 text_label_ok。
"""

from __future__ import annotations

import json
import random
import re
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Iterable

from PIL import Image, ImageDraw

from objloc.visualizer import resolve_font

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


def _darken(rgb: tuple[int, int, int], factor: float = 0.65) -> tuple[int, int, int]:
    return tuple(max(0, int(c * factor)) for c in rgb)  # type: ignore[return-value]


# --------------------------------------------------------------------------- #
# 数据模型
# --------------------------------------------------------------------------- #
@dataclass
class Shape:
    kind: str
    color_name: str
    rgb: tuple[int, int, int]
    bbox_px: tuple[float, float, float, float]  # x1,y1,x2,y2 像素

    @property
    def label(self) -> str:
        return f"{self.color_name}{SHAPE_CN[self.kind]}"

    def to_gt(self, width: int, height: int) -> dict:
        x1, y1, x2, y2 = self.bbox_px
        return {
            "bbox_2d": [
                round(x1 / width, 4),
                round(y1 / height, 4),
                round(x2 / width, 4),
                round(y2 / height, 4),
            ],
            "label": self.label,
            "kind": self.kind,
            "color_name": self.color_name,
        }


@dataclass
class Sample:
    name: str
    path: str
    width: int
    height: int
    shapes: list[Shape] = field(default_factory=list)

    def ground_truth(self) -> list[dict]:
        return [s.to_gt(self.width, self.height) for s in self.shapes]

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "path": self.path,
            "width": self.width,
            "height": self.height,
            "shapes": [asdict(s) for s in self.shapes],
            "ground_truth": self.ground_truth(),
        }


# --------------------------------------------------------------------------- #
# 图像生成
# --------------------------------------------------------------------------- #
def _draw_shape(draw: ImageDraw.ImageDraw, shape: Shape) -> None:
    x1, y1, x2, y2 = shape.bbox_px
    box = [x1, y1, x2, y2]
    fill = shape.rgb
    outline = _darken(shape.rgb)
    if shape.kind == "rect":
        draw.rectangle(box, fill=fill, outline=outline, width=5)
    elif shape.kind == "circle":
        draw.ellipse(box, fill=fill, outline=outline, width=5)
    elif shape.kind == "ellipse":
        draw.ellipse(box, fill=fill, outline=outline, width=5)
    elif shape.kind == "triangle":
        cx = (x1 + x2) / 2
        draw.polygon([(cx, y1), (x2, y2), (x1, y2)], fill=fill, outline=outline)
    else:
        raise ValueError(f"未知形状: {shape.kind}")


def make_sample(
    index: int,
    *,
    seed: int | None = None,
    width: int = 900,
    height: int = 720,
    n_shapes: int = 3,
) -> tuple[Sample, Image.Image]:
    """生成一张图片（返回 Sample 与 PIL 图像，不落盘）。"""
    rng = random.Random(seed if seed is not None else index)

    # 画布：浅色底 + 细边框
    img = Image.new("RGB", (width, height), (247, 249, 252))
    draw = ImageDraw.Draw(img)
    draw.rectangle([0, 0, width - 1, height - 1], outline=(210, 218, 228), width=2)

    # 把画布切成 2x2 网格，尽量保证图形不重叠
    cols, rows = 2, 2
    cells = [(c, r) for r in range(rows) for c in range(cols)]
    rng.shuffle(cells)
    cells = cells[:n_shapes]

    kinds = list(SHAPE_CN.keys())
    colors = list(PALETTE.keys())
    used_colors: set[str] = set()

    shapes: list[Shape] = []
    cw, ch = width / cols, height / rows

    for i, (c, r) in enumerate(cells):
        margin = 60
        cx1, cy1 = c * cw + margin, r * ch + margin
        cx2, cy2 = (c + 1) * cw - margin, (r + 1) * ch - margin

        kind = rng.choice(kinds)
        # 颜色尽量不重复，便于标签区分
        avail = [x for x in colors if x not in used_colors] or colors
        color_name = rng.choice(avail)
        used_colors.add(color_name)

        # 在单元格内随机取一个子框
        bw = rng.uniform(0.55, 0.9) * (cx2 - cx1)
        bh = rng.uniform(0.55, 0.9) * (cy2 - cy1)
        x1 = rng.uniform(cx1, cx2 - bw)
        y1 = rng.uniform(cy1, cy2 - bh)
        x2, y2 = x1 + bw, y1 + bh

        if kind == "circle":
            d = min(bw, bh)
            x2, y2 = x1 + d, y1 + d
        elif kind == "ellipse":
            # 拉长，避免和圆形混淆
            y2 = y1 + bh * 0.6
        elif kind == "rect":
            # 避免正方形（否则模型可能说“正方形”）
            if abs(bw - bh) < 30:
                bw += 50
                x2 = min(x1 + bw, cx2)

        shapes.append(Shape(
            kind=kind,
            color_name=color_name,
            rgb=PALETTE[color_name],
            bbox_px=(round(x1, 1), round(y1, 1), round(x2, 1), round(y2, 1)),
        ))

    for shape in shapes:
        _draw_shape(draw, shape)

    # 左上角写序号（不影响坐标识别）
    font = resolve_font(22)
    draw.text((16, 12), f"#{index + 1}", fill=(150, 160, 175), font=font)

    sample = Sample(name=f"sample_{index + 1:02d}", path="", width=width, height=height, shapes=shapes)
    return sample, img


def make_samples(
    count: int = 5,
    *,
    seed: int = 42,
    out_dir: str | Path,
    width: int = 900,
    height: int = 720,
    n_shapes: int = 3,
    clean: bool = True,
) -> list[Sample]:
    """批量生成图片与 ground truth，返回 Sample 列表（已落盘）。"""
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    if clean:
        for old in out.glob("*.png"):
            old.unlink()
        gt_file = out / "ground_truth.json"
        if gt_file.exists():
            gt_file.unlink()

    samples: list[Sample] = []
    for i in range(count):
        sample, img = make_sample(
            i, seed=seed + i * 1000, width=width, height=height, n_shapes=n_shapes
        )
        path = out / f"{sample.name}.png"
        img.save(path, format="PNG")
        sample.path = str(path)
        samples.append(sample)

    with (out / "ground_truth.json").open("w", encoding="utf-8") as fp:
        json.dump([s.to_dict() for s in samples], fp, ensure_ascii=False, indent=2)
    return samples


# --------------------------------------------------------------------------- #
# 评估指标
# --------------------------------------------------------------------------- #
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


__all__ = [
    "PALETTE", "SHAPE_CN", "Shape", "Sample",
    "make_sample", "make_samples",
    "iou", "match_predictions", "evaluate_sample", "aggregate",
    "rescale_predictions", "evaluate_any_space",
    "shape_ok", "color_ok", "text_label_ok",
]
