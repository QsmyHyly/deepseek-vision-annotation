# -*- coding: utf-8 -*-
"""合成样本生成 —— 「已知答案」的几何图形图片从哪来。

职责
----
- 数据模型：`Shape`（一个图形的种类/颜色/像素框，能自己导出归一化真值）、
  `Sample`（一张图 + 它的全部图形，能导出 ground_truth()）；
- 绘制：`_draw_shape()` 把 Shape 画到画布上（shape 生成与分辨率重渲染共用同一份）；
- 生成：`make_sample()` 单张、`make_samples()` 批量落盘 + ground_truth.json。

边界
----
只负责"造出图和它的真值"，不负责评分（metrics.py）也不负责词表（labels.py）。
真值坐标一律与项目一致：0.0~1.0 的相对比例（占宽/高）。

为什么真值要由这里统一生成
--------------------------
`Shape.to_gt()` 是几何图真值的唯一出口：`objloc/samples/` 的各组测试图与
`scripts/benchmark.py` 的评测图都从这里取真值，口径不会各处漂移。

旧文件对应关系（等价重构）
--------------------------
本模块 = 拆分前 `objloc/benchmark.py` 的「数据模型」+「图像生成」两节，
函数体与随机数调用顺序逐行照搬（⚠️ 调用顺序决定同一 seed 下画出的图是否逐字节一致，
不要为了"整理"而调整 `rng` 的使用次序）。

@doc AGENTS.md#4.3-坐标约定
（该文档规定真值为什么是 0.0~1.0 的相对比例。）
"""

from __future__ import annotations

import json
import random
from dataclasses import asdict, dataclass, field
from pathlib import Path

from PIL import Image, ImageDraw

from objloc.visualizer import resolve_font

from .labels import PALETTE, SHAPE_CN


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
