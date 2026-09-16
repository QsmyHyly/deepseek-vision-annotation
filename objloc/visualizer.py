"""图像可视化：把模型输出的归一化坐标绘制成标注图。

坐标约定：0.0~1.0 的相对比例，x 对应宽、y 对应高。

同时提供「打标前 / 打标后」对比所需的工具函数：
- load_image        统一加载本地路径 / URL / bytes
- annotate          在 PIL 图像上绘制 bbox 与 point
- render_annotations 便捷函数：输入源与坐标文本，输出标注图与保存路径
- image_to_png_bytes / image_to_data_url
"""

from __future__ import annotations

import base64
import io
import os
import uuid
from pathlib import Path
from typing import Any, Iterable

import requests
from PIL import Image, ImageColor, ImageDraw, ImageFont

from objloc.config import SCRATCH_DIR
from objloc.parsing import normalize_to_unit

# 手写色优先，其后追加 PIL 命名颜色并去重（保持首次出现顺序）
_EXTRA_COLORS = [name for (name, _code) in ImageColor.colormap.items()]
COLORS = list(dict.fromkeys([
    "red", "green", "blue", "yellow", "orange", "pink", "purple", "brown", "gray",
    "beige", "turquoise", "cyan", "magenta", "lime", "navy", "maroon", "teal",
    "olive", "coral", "lavender", "violet", "gold", "silver",
] + _EXTRA_COLORS))

# 跨平台中文字体候选（原实现硬编码 NotoSansCJK，在 Windows 会直接报错）
_FONT_CANDIDATES = [
    "NotoSansCJK-Regular.ttc",
    "NotoSansCJKsc-Regular.otf",
    "msyh.ttc", "msyhbd.ttc",          # 微软雅黑
    "simhei.ttf", "simsun.ttc",         # 黑体 / 宋体
    "PingFang.ttc", "Hiragino Sans GB.ttc",  # macOS
    "wqy-microhei.ttc", "wqy-zenhei.ttc",    # Linux
    "DejaVuSans.ttf", "Arial.ttf",
]

_FONT_DIRS = [
    Path(os.environ.get("WINDIR", "C:/Windows")) / "Fonts",
    Path("/usr/share/fonts"),
    Path("/usr/local/share/fonts"),
    Path.home() / ".fonts",
    Path("/System/Library/Fonts"),
    Path("/Library/Fonts"),
]

_font_cache: dict[int, ImageFont.FreeTypeFont] = {}


def _find_font_file() -> str | None:
    for name in _FONT_CANDIDATES:
        for directory in _FONT_DIRS:
            if not directory.exists():
                continue
            candidate = directory / name
            if candidate.exists():
                return str(candidate)
        # 有些系统字体在子目录中，做一次浅层递归查找
        for directory in _FONT_DIRS:
            if not directory.exists():
                continue
            for found in directory.rglob(name):
                return str(found)
    return None


def resolve_font(size: int = 16) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    """获取支持中文的字体，找不到时退回 PIL 内置位图字体。"""
    if size in _font_cache:
        return _font_cache[size]
    path = _find_font_file()
    font: ImageFont.FreeTypeFont | ImageFont.ImageFont
    if path:
        try:
            font = ImageFont.truetype(path, size=size)
        except Exception:
            font = ImageFont.load_default()
    else:
        font = ImageFont.load_default()
    _font_cache[size] = font
    return font


# --------------------------------------------------------------------------- #
# 图像加载 / 导出
# --------------------------------------------------------------------------- #
def load_image(source: str | bytes | Path | Image.Image) -> Image.Image:
    """加载图像。source 支持：PIL Image、bytes、本地路径、http(s) URL、data URL。"""
    if isinstance(source, Image.Image):
        return source.convert("RGB") if source.mode not in ("RGB", "RGBA") else source.copy()

    if isinstance(source, Path):
        source = str(source)

    if isinstance(source, bytes):
        return Image.open(io.BytesIO(source)).convert("RGB")

    text = str(source)
    if text.startswith("data:"):
        _header, _, b64 = text.partition(",")
        return Image.open(io.BytesIO(base64.b64decode(b64))).convert("RGB")

    if text.startswith(("http://", "https://")):
        resp = requests.get(text, timeout=60)
        resp.raise_for_status()
        return Image.open(io.BytesIO(resp.content)).convert("RGB")

    return Image.open(text).convert("RGB")


def image_to_png_bytes(image: Image.Image) -> bytes:
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


def image_to_data_url(image: Image.Image, fmt: str = "PNG") -> str:
    buffer = io.BytesIO()
    image.save(buffer, format=fmt)
    mime = "image/png" if fmt.upper() == "PNG" else f"image/{fmt.lower()}"
    return f"data:{mime};base64,{base64.b64encode(buffer.getvalue()).decode('ascii')}"


def image_to_data_url_from_source(source: str | bytes | Path | Image.Image) -> str:
    """把任意图像源转成 data URL（可用于多模态请求的 image_url）。"""
    return image_to_data_url(load_image(source))


# --------------------------------------------------------------------------- #
# 标注绘制
# --------------------------------------------------------------------------- #
def _ratio_to_abs(value: float, total: int) -> int:
    """相对比例 0.0~1.0 -> 像素。越界值会被夹紧，避免画到画布外。

    注意：旧刻度 0~1000 的坐标在进入 annotate() 时已被 normalize_to_unit 换算，
    这里只处理 0.0~1.0；万一仍收到超范围的值，夹紧到边界是刻意的兜底行为
    （宁可画在边上，也不要抛异常中断整张图的标注）。
    """
    try:
        ratio = float(value)
    except (TypeError, ValueError):
        ratio = 0.0
    ratio = min(1.0, max(0.0, ratio))
    return int(round(ratio * total))


def annotate(
    image: Image.Image | str | Path | bytes,
    items: Iterable[dict] | str,
    *,
    box_width: int = 3,
    point_radius: int = 5,
    font_size: int = 22,
    draw_label: bool = True,
) -> Image.Image:
    """在图像上绘制 bbox_2d / point_2d 标注。

    Args:
        image: 图像源。
        items: 坐标对象列表，或包含 JSON 的字符串。
    Returns:
        新的标注图像（不修改原图）。
    """
    img = load_image(image).convert("RGB")
    data = _coerce_items(items)
    # 兼容旧刻度：若整批坐标都是 0~1000，先无损换算成 0.0~1.0 再绘制
    data, _converted = normalize_to_unit(data)
    width, height = img.size

    draw = ImageDraw.Draw(img)
    font = resolve_font(font_size)

    for i, item in enumerate(data):
        if not isinstance(item, dict):
            continue
        color = COLORS[i % len(COLORS)]
        label = str(item.get("label", "") or "")

        if "bbox_2d" in item:
            try:
                x1, y1, x2, y2 = item["bbox_2d"]
            except Exception:
                continue
            ax1, ay1 = _ratio_to_abs(x1, width), _ratio_to_abs(y1, height)
            ax2, ay2 = _ratio_to_abs(x2, width), _ratio_to_abs(y2, height)
            if ax1 > ax2:
                ax1, ax2 = ax2, ax1
            if ay1 > ay2:
                ay1, ay2 = ay2, ay1
            draw.rectangle(((ax1, ay1), (ax2, ay2)), outline=color, width=box_width)
            if draw_label and label:
                _draw_label(draw, (ax1, max(0, ay1 - font_size - 6)), label, color, font)

        if "point_2d" in item:
            try:
                x, y = item["point_2d"]
            except Exception:
                continue
            cx, cy = _ratio_to_abs(x, width), _ratio_to_abs(y, height)
            draw.ellipse(
                [(cx - point_radius, cy - point_radius), (cx + point_radius, cy + point_radius)],
                outline=color, width=box_width,
            )
            draw.ellipse(
                [(cx - 1, cy - 1), (cx + 1, cy + 1)], fill=color,
            )
            if draw_label and label:
                _draw_label(draw, (cx + point_radius + 4, cy + 2), label, color, font)

    return img


def _draw_label(draw: ImageDraw.ImageDraw, xy, text: str, color, font) -> None:
    """绘制带底色的标签，保证在任意背景上可读。"""
    try:
        l, t, r, b = draw.textbbox(xy, text, font=font)
    except Exception:
        draw.text(xy, text, fill=color, font=font)
        return
    pad = 3
    draw.rectangle((l - pad, t - pad, r + pad, b + pad), fill=color)
    # 依据底色亮度选择黑/白文字
    try:
        rgb = ImageColor.getrgb(color)
        luminance = 0.299 * rgb[0] + 0.587 * rgb[1] + 0.114 * rgb[2]
        text_color = (0, 0, 0) if luminance > 140 else (255, 255, 255)
    except Exception:
        text_color = (255, 255, 255)
    draw.text(xy, text, fill=text_color, font=font)


def _coerce_items(items) -> list[dict]:
    """把输入统一成坐标对象列表（委托给 point_parser.to_items 做规范化）。"""
    from objloc.parsing import to_items

    if isinstance(items, str):
        return to_items(items)
    if isinstance(items, (dict, tuple)):
        return to_items(items)
    return to_items(list(items))


def coerce_items(items: Iterable[dict] | str) -> list[dict]:
    """公开入口：把任意坐标输入统一成坐标对象列表。"""
    return _coerce_items(items)


def render_annotations(
    image_source: str | bytes | Path | Image.Image,
    items: Iterable[dict] | str,
    *,
    output_dir: Path | None = None,
    stem: str | None = None,
) -> tuple[Image.Image, Path]:
    """绘制标注并保存为 PNG，返回 (标注图, 保存路径)。

    ⚠️ **不传 output_dir 就落到 runs/scratch/，不要往 runs/ 根目录写**：
    根目录曾经被三处调用方（main.py、tools/builtin.py、web/app.py）倒进 162 张随机命名的散图，
    既不可清理也反查不回来源。无归属的临时标注图一律去 scratch/；
    要长期留存就显式指定 storage.run_dir(run_id) + stem="annotated" 走历史记录
    （见 AGENTS.md#4.9-上传--结果存储--历史记录）。
    """
    img = annotate(image_source, items)
    out_dir = Path(output_dir or SCRATCH_DIR)
    out_dir.mkdir(parents=True, exist_ok=True)
    name = stem or f"annotated_{uuid.uuid4().hex[:12]}"
    path = out_dir / f"{name}.png"
    img.save(path, format="PNG")
    return img, path


def summarize(items: list[dict] | str) -> dict[str, Any]:
    """统计坐标对象数量，供接口/页面展示。"""
    data = _coerce_items(items)
    bboxes = [d for d in data if isinstance(d, dict) and "bbox_2d" in d]
    points = [d for d in data if isinstance(d, dict) and "point_2d" in d]
    return {
        "total": len(data),
        "bbox_count": len(bboxes),
        "point_count": len(points),
        "labels": [str(d.get("label", "")) for d in data if isinstance(d, dict)],
    }
