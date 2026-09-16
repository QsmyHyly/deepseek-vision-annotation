# -*- coding: utf-8 -*-
"""内置测试图片目录 —— 网页「测试图片」面板、探测脚本、评测脚本共用的唯一生成源。

为什么要有这个模块
------------------
"准确率由程序判定"的前提是图片答案已知。本项目所有测试图都由代码生成：每个图形/圆点
的真实位置在绘制时就拿到了，因此真值不依赖人眼标注，也不需要把二进制图片提交进仓库。

三处使用者必须是同一份实现，否则网页上手动测试的结果和脚本评测的结论对不上：

- 网页「测试图片」面板：GET /api/samples、POST /api/samples/{id}/load
- 探测脚本 scripts/probe_vision_frame.py：量模型实际看到的帧几何 / 有效分辨率
- 评测脚本 scripts/benchmark.py：批量准确率

约定
----
- 真值坐标一律 **0.0~1.0 相对比例**（与 objloc/config.py 的提示词一致），
  由 objloc.benchmark.Shape.to_gt / Sample.ground_truth 统一生成；
- 图片按需生成到 runs/samples/ 并缓存（runs/ 是可清理的运行产物目录）；
- 文字阶梯、竖线带这类图**没有可用的坐标真值**（它们测的是"模型能看多细"），
  accuracy() 对它们返回 None，由脚本按"读没读对"另行判定；
- 第 ⑤ 组「网页截图」是唯一一组**不由 Python 绘制**的图：真浏览器渲染 + DOM 真值，
  见下面的"生成器 5"，真值与 PNG 同源同生同灭。

@doc AGENTS.md#4.3-坐标约定
@doc AGENTS.md#7-打标准确率评测结论重要
"""

from __future__ import annotations

import json
import random
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from PIL import Image, ImageDraw

from objloc.benchmark import (
    PALETTE,
    Sample,
    Shape,
    _draw_shape,
    evaluate_any_space,
    evaluate_sample,
    make_sample,
)
from objloc.config import PROJECT_ROOT, RUNS_DIR
from objloc.visualizer import resolve_font

SAMPLES_DIR = RUNS_DIR / "samples"


# --------------------------------------------------------------------------- #
# 生成器 1：彩色圆点阵（量帧几何 / 坐标约定）
# --------------------------------------------------------------------------- #
# 圆点位置（相对坐标），刻意在四角/四边/中心铺开，便于拟合出斜率与截距
MARKER_FRACTIONS = [
    (0.12, 0.14), (0.50, 0.12), (0.88, 0.16),
    (0.14, 0.50), (0.50, 0.50), (0.86, 0.52),
    (0.12, 0.86), (0.50, 0.88), (0.88, 0.84),
]


def make_marker_image(path, width: int, height: int) -> list[dict]:
    """生成彩色圆点阵，返回真值 [{label, cx, cy, fx, fy, radius}]（像素坐标）。"""
    img = Image.new("RGB", (width, height), (247, 249, 252))
    draw = ImageDraw.Draw(img)
    radius = max(6, int(0.045 * min(width, height)))
    font = resolve_font(max(12, int(min(width, height) * 0.035)))
    names = list(PALETTE.keys())[:len(MARKER_FRACTIONS)]

    truth = []
    for (fx, fy), name in zip(MARKER_FRACTIONS, names):
        cx, cy = fx * width, fy * height
        draw.ellipse([cx - radius, cy - radius, cx + radius, cy + radius],
                     fill=PALETTE[name], outline=(30, 30, 30), width=2)
        draw.text((cx + radius + 6, cy - radius), name, fill=(40, 40, 40), font=font)
        truth.append({"label": name, "cx": cx, "cy": cy, "fx": fx, "fy": fy, "radius": radius})
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    img.save(path, format="PNG")
    return truth


def markers_to_gt(truth: list[dict], width: int, height: int) -> list[dict]:
    """把圆点真值转成可评测的 bbox 真值（0.0~1.0，kind=circle）。

    两个刻意选择（都踩过坑）：
    - label **只写颜色名**，不加"圆形"后缀。图上每个点旁边写的就是颜色名，
      MARKER_PROMPT 也只问颜色；若真值要求"红色圆形"而提示词只要颜色，
      标签准确率会恒为 0%——那是真值与提示词打架，不是模型不行。
    - match="center"：圆点直径只有 min(宽,高) 的 9%，在极端扁图上真值框窄到 0.011，
      用 IoU 0.5 判定等于在考"框画得多紧"，而这张图要测的是点位。详见 benchmark.center_hit()。
    """
    gt = []
    for item in truth:
        r = item["radius"]
        gt.append({
            "bbox_2d": [
                round((item["cx"] - r) / width, 4),
                round((item["cy"] - r) / height, 4),
                round((item["cx"] + r) / width, 4),
                round((item["cy"] + r) / height, 4),
            ],
            "label": item["label"],
            "kind": "circle",
            "color_name": item["label"],
            "expect_shape": False,   # 提示词只要颜色，不判形状
            "match": "center",       # 点位判定：真值中心落在预测框内即算命中
        })
    return gt


# --------------------------------------------------------------------------- #
# 生成器 2：文字可读性阶梯（量有效分辨率）
# --------------------------------------------------------------------------- #
# 字号单位是**原始像素**：同一套字号在不同栅格上渲染，模型的最小可读字号会随
# 服务端缩放因子等比变大，由此可反推真实缩放倍数。
TEXT_FONT_SIZES = [64, 48, 36, 28, 22, 16, 12, 9, 7, 5]
_CODE_ALPHABET = "ACDEFGHJKLMNPQRTUVWXY34679"


def random_codes(count: int, seed: int = 20260101) -> list[str]:
    """生成 count 个 4 位随机代码（去掉易混字符，避免判读歧义）。"""
    rng = random.Random(seed)
    return ["".join(rng.choice(_CODE_ALPHABET) for _ in range(4)) for _ in range(count)]


def make_text_image(path, width: int, height: int, sizes, codes) -> list[dict]:
    """生成字号阶梯图；返回真值 [{index, font_px, code}]。"""
    img = Image.new("RGB", (width, height), (255, 255, 255))
    draw = ImageDraw.Draw(img)
    line_h = height / len(sizes)
    index_font = resolve_font(max(14, int(min(width, height) * 0.022)))
    truth = []
    for i, (size, code) in enumerate(zip(sizes, codes)):
        top = i * line_h
        draw.rectangle([0, top, width - 1, top + line_h - 1], outline=(232, 232, 232), width=2)
        draw.text((width * 0.01, top + line_h / 2 - index_font.size * 0.6),
                  f"{i + 1}", fill=(0, 0, 0), font=index_font)
        font = resolve_font(size)
        draw.text((width * 0.10, top + line_h / 2 - size * 0.62), code, fill=(0, 0, 0), font=font)
        truth.append({"index": i + 1, "font_px": size, "code": code})
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    img.save(path, format="PNG")
    return truth


# --------------------------------------------------------------------------- #
# 生成器 3：竖线带（量最小可分辨线间距）
# --------------------------------------------------------------------------- #
BAND_SPECS = [  # (竖线间距 px, 条数) —— 间距递增、条数打乱，防止模型靠猜
    (2, 5), (3, 3), (4, 7), (5, 4), (6, 6),
    (8, 5), (10, 3), (12, 7), (16, 4), (24, 6),
]


def make_band_image(path, width: int, height: int, specs) -> list[dict]:
    """生成竖线带图片；返回真值 [{index, spacing, lines}]。"""
    img = Image.new("RGB", (width, height), (255, 255, 255))
    draw = ImageDraw.Draw(img)
    band_h = height // len(specs)
    font = resolve_font(max(18, band_h // 4))
    truth = []
    for i, (spacing, lines) in enumerate(specs):
        top = i * band_h
        draw.rectangle([0, top, width - 1, top + band_h - 1], outline=(200, 200, 200), width=2)
        draw.text((20, top + band_h // 2 - band_h // 8), str(i + 1), fill=(0, 0, 0), font=font)
        span = (lines - 1) * spacing
        x0 = width // 2 - span // 2
        for k in range(lines):
            x = x0 + k * spacing
            draw.rectangle([x, top + band_h // 5, x + 1, top + band_h - band_h // 5], fill=(0, 0, 0))
        truth.append({"index": i + 1, "spacing": spacing, "lines": lines})
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    img.save(path, format="PNG")
    return truth


# --------------------------------------------------------------------------- #
# 生成器 4：把已知场景等比缩放到任意栅格（分辨率扫描用）
# --------------------------------------------------------------------------- #
def render_scaled(reference: Sample, width: int, height: int, path) -> Sample:
    """把参考场景等比缩放到目标分辨率重新渲染，返回该尺寸下的 Sample。

    场景先在参考画布（默认 900x720）上生成，再整体缩放渲染 —— 各尺寸的真值
    （相对比例坐标）因此逐字节相同，精度差异才只能归因于栅格分辨率/服务端重采样，
    而不是"场景变了"。注意必须保持宽高比一致，否则图形会被拉伸。
    """
    scale = width / reference.width
    img = Image.new("RGB", (width, height), (247, 249, 252))
    draw = ImageDraw.Draw(img)
    draw.rectangle([0, 0, width - 1, height - 1], outline=(210, 218, 228), width=2)
    shapes = [
        Shape(kind=s.kind, color_name=s.color_name, rgb=s.rgb,
              bbox_px=tuple(round(v * scale, 1) for v in s.bbox_px))
        for s in reference.shapes
    ]
    for shape in shapes:
        _draw_shape(draw, shape)
    draw.text((16, 12), "#1", fill=(150, 160, 175), font=resolve_font(max(10, int(22 * scale))))
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    img.save(path, format="PNG")
    return Sample(name=path.stem, path=str(path), width=width, height=height, shapes=shapes)


# --------------------------------------------------------------------------- #
# 生成器 5：真实网页快照（Playwright 抓取，真值来自 DOM）
# --------------------------------------------------------------------------- #
# 与前面四组最大的不同：图**不是 Python 画出来的**，而是真浏览器渲染出来的网页截图；
# 真值也不是"我画的图形在哪"，而是浏览器自己报的 getBoundingClientRect()。
#
# 为什么不把真值抄进本文件：真值必须与 PNG 严格同源。抄一份就有漂移风险——
# 改了页面 HTML 重抓一次，PNG 变了而抄来的真值没变，评测会**安静地**给出错误结论。
# 这里读 manifest 取真值，两者由同一次抓取产出，天然同步；
# runs/ 被清掉时 build() 会自动重抓一次，保证可自愈。
#
# 演示时要讲清楚的局限：这一组考的是"读懂界面语义"（按钮/卡片/图表区在哪、叫什么），
# 不是前面几组那种纯几何定位。网页元素的边界本来就有歧义（一张卡片从哪算起？），
# 所以 IoU 一般明显低于几何图——这是任务性质决定的，不是模型退步。
WEB_SAMPLES_DIR = RUNS_DIR / "web_samples"
WEB_MANIFEST = WEB_SAMPLES_DIR / "manifest.json"
WEB_CAPTURE_SCRIPT = PROJECT_ROOT / "scripts" / "capture_web_samples.mjs"

_WEB_MANIFEST: dict | None = None


def capture_web_samples(*, force: bool = False) -> dict:
    """跑一次 Playwright 抓取（node scripts/capture_web_samples.mjs），返回 manifest。

    抓取是确定性的（页面内容固定、无动画/随机数），重复跑得到的 PNG 与真值一致，
    因此 runs/web_samples/ 可以当纯缓存看待——删掉只会让它重跑一次。
    """
    global _WEB_MANIFEST
    if _WEB_MANIFEST is not None and not force:
        return _WEB_MANIFEST
    if force or not WEB_MANIFEST.exists():
        if not WEB_CAPTURE_SCRIPT.exists():
            raise RuntimeError(
                f"缺少网页快照抓取脚本：{WEB_CAPTURE_SCRIPT}\n"
                "第 ⑤ 组「网页截图」测试图由它生成，见 AGENTS.md 第 5 节。"
            )
        proc = subprocess.run(
            ["node", str(WEB_CAPTURE_SCRIPT)],
            cwd=str(PROJECT_ROOT), capture_output=True, text=True,
            encoding="utf-8", errors="replace",
        )
        if proc.returncode != 0:
            raise RuntimeError(
                "网页快照抓取失败（需要本机可用的 node + Playwright，"
                "路径约定见 tests/ui/ui_check.mjs）：\n" + (proc.stderr or "")[-2000:]
            )
    if not WEB_MANIFEST.exists():
        raise RuntimeError(f"抓取脚本没有产出 {WEB_MANIFEST}")
    _WEB_MANIFEST = json.loads(WEB_MANIFEST.read_text(encoding="utf-8"))
    return _WEB_MANIFEST


def web_entry(sample_id: str, *, force: bool = False) -> dict:
    """取某个网页快照在 manifest 里的条目（含 targets 真值）。"""
    manifest = capture_web_samples(force=force)
    for entry in manifest.get("samples", []):
        if entry.get("id") == sample_id:
            return entry
    raise KeyError(f"manifest 里没有网页快照：{sample_id}")


def web_targets_to_gt(targets: list[dict]) -> list[dict]:
    """把抓取脚本产出的 targets 转成本项目统一的真值格式。

    label_mode="text" 是关键：网页目标的标签是界面上的中文名称（"总销售额"、"加入购物车"），
    既没有颜色也没有形状，必须走文本比对口径，否则标签准确率恒为 0。
    判定实现在 objloc/benchmark.py: text_label_ok，理由见该函数的注释。

    命中判定统一用 match="center"（真值中心落在预测框内即算找到，另带面积比上限防作弊），
    理由是实测出来的，和圆点阵同一类问题：
    网页控件的框高常常只有图高的 4%~6%（900px 高的截图里一个输入框才 45px），
    而模型画的框系统性偏高约 30%——本地化其实是对的，IoU 却卡在 0.5 上下随机翻车。
    那测的是"框画得多紧"，不是"有没有找到这个界面元素"，而这一组要测的恰恰是后者。
    框的紧不紧另由 mean_iou 体现，两个指标一起看。
    """
    gt = []
    for t in targets:
        box = [min(max(float(v), 0.0), 1.0) for v in t["bbox_2d"][:4]]
        if len(box) != 4 or box[0] >= box[2] or box[1] >= box[3]:
            continue   # 退化的框算不出 IoU，直接丢弃而不是让整张图评分失真
        entry = {
            "bbox_2d": [round(v, 4) for v in box],
            "label": str(t["label"]),
            "label_mode": "text",
            "expect_shape": False,
            # 中心点命中，理由见 web_targets_to_gt 的 docstring
            "match": "center",
        }
        # 同一元素的其它合理叫法（抓取脚本的 data-gt-alias），见 benchmark.text_label_ok
        aliases = [str(a) for a in (t.get("aliases") or []) if str(a).strip()]
        if aliases:
            entry["aliases"] = aliases
        gt.append(entry)
    return gt


def _web_builder(sample_id: str):
    """构造某个网页快照的生成器：把抓来的 PNG 复制到 samples 目录，返回 DOM 真值。"""
    def build(path) -> list[dict]:
        entry = web_entry(sample_id)
        src = WEB_SAMPLES_DIR / str(entry.get("file") or f"{sample_id}.png")
        if not src.exists():
            # manifest 还在但 PNG 被删了：强制重抓一次自愈
            entry = web_entry(sample_id, force=True)
            src = WEB_SAMPLES_DIR / str(entry.get("file") or f"{sample_id}.png")
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        if src.resolve() != path.resolve():
            shutil.copyfile(src, path)
        return web_targets_to_gt(entry.get("targets", []))

    return build


# --------------------------------------------------------------------------- #
# 目录定义
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class TestImage:
    """一张内置测试图的完整描述（元数据 + 生成函数）。"""

    id: str
    group: str
    title: str
    purpose: str          # 这张图是拿来测什么的（网页上直接显示）
    prompt: str           # 建议提示词（点选后自动填入输入框）
    width: int
    height: int
    build: Callable[[Path], list[dict]]   # 生成图片 -> 真值列表（可为空）
    evaluable: bool = False               # 真值是否可用于自动算准确率

    @property
    def filename(self) -> str:
        return f"{self.id}.png"


# 分辨率扫描：场景完全相同，只改栅格。参考场景与 scripts/probe_vision_frame.py 的
# ACCURACY_REFERENCE 一致（900x720 / seed 7），保证网页手动测试与脚本结论可比。
RESOLUTION_REFERENCE = (900, 720)
RESOLUTION_RASTERS = [(360, 288), (900, 720), (1800, 1440), (2700, 2160)]

BENCH_PROMPT = (
    "识别图中的几何图形。为每个图形输出 bbox_2d 坐标与中文名称，"
    "名称需包含颜色与形状（例如：蓝色矩形、红色圆形、绿色三角形）。"
)
MARKER_PROMPT = "识别图中所有彩色圆点，为每个圆点输出 bbox_2d 坐标与中文颜色名称。"
TEXT_PROMPT = (
    "图中从上到下分成 {n} 行，每行左侧是行号，右侧是一段 4 位的大写字母数字代码。\n"
    "请逐行转写你**能看清**的代码；看不清的行代码填 null。\n"
    '只输出 JSON：{{"lines": [{{"index": 1, "code": "ABCD"}}, ...]}}'
)
# 网页截图的提示词：**只描述要找哪几类区域，不报出名称**。
# 报了名称就等于把答案递给模型，检出率会虚高到失去意义；只给类别，模型必须自己去读图上的字。
# 但也不能只说"找出所有元素"——那样模型会把整页几十个元素都框回来，命中率反而难看，
# 而且"哪算一个元素"本来就有歧义。所以这里给的是"类别 + 位置线索"的中庸问法。
# ⚠️ 提示词里**不许出现真值标签本身**（比如"待办事项""限时秒杀"）。
# 踩过：早先写"② 页面顶部的活动横幅"，模型就照着提示词的词回了"活动横幅"，
# 而图上印的是"限时秒杀"——它把提示词当成了答案，标签准确率被自己的提问拉低。
# 所以这里只给"位置 + 类别"的线索（"图表右侧的列表面板"），名称一律让模型自己去图上读。
WEB_DASHBOARD_PROMPT = (
    "这是一张电商后台数据看板的网页截图。请定位图中这些区域，"
    "并用它们在界面上显示的中文标题作为名称：\n"
    "① 顶部导航栏里的搜索框；② 四张 KPI 指标卡片（标题写在卡片左上角）；\n"
    "③ 图表面板（标题写在面板左上角）；④ 图表右侧的列表面板（标题写在面板左上角）。"
)
WEB_SHOP_PROMPT = (
    "这是一张商品列表页的网页截图。请定位：\n"
    "① 顶部导航栏里的搜索框；② 页面顶部的促销区域（名称取它在图上显示的文字）；\n"
    "③ 商品网格里的每一张商品卡片（名称取卡片上的商品名）。"
)
WEB_FORM_PROMPT = (
    "这是一张登录设置表单页的网页截图。请定位图中的页面主标题，"
    "以及每一个表单控件（输入框、下拉框、主按钮），"
    "并用它上方或自身显示的中文名称作为名称。"
)
WEB_ARTICLE_PROMPT = (
    "这是一张新闻文章详情页的网页截图。请定位：\n"
    "① 文章大标题；② 封面图区域；③ 紧跟封面的开场段落（名称取该段开头的文字）；\n"
    "④ 右侧边栏的栏目标题；⑤ 右侧每一张卡片（名称取卡片标题）。"
)

BAND_PROMPT = (
    "这张图从上到下分成 {n} 个横带，每个带左侧写着带编号（1 到 {n}）。\n"
    "每个带里有一簇竖直的黑色细线；有的带线条太密，可能糊成一片看不清。\n"
    "请数出每个带里**你能分辨出来的竖线条数**（完全看不清就填 0）。\n"
    '只输出 JSON：{{"bands": [{{"index": 1, "lines": 5}}, ...]}}'
)


def _bench_builder(index: int, seed: int, raster: tuple[int, int] | None, n_shapes: int = 4):
    """构造 benchmark 合成图生成器；raster 为 None 时用参考分辨率。"""
    def build(path) -> list[dict]:
        reference, img = make_sample(
            index, seed=seed, width=RESOLUTION_REFERENCE[0],
            height=RESOLUTION_REFERENCE[1], n_shapes=n_shapes,
        )
        if raster is None or raster == RESOLUTION_REFERENCE:
            path = Path(path)
            path.parent.mkdir(parents=True, exist_ok=True)
            img.save(path, format="PNG")
            reference.path = str(path)
            return reference.ground_truth()
        return render_scaled(reference, raster[0], raster[1], path).ground_truth()

    return build


def _marker_builder(width: int, height: int):
    def build(path) -> list[dict]:
        return markers_to_gt(make_marker_image(path, width, height), width, height)

    return build


def _text_builder(width: int, height: int):
    def build(path) -> list[dict]:
        codes = random_codes(len(TEXT_FONT_SIZES))
        make_text_image(path, width, height, TEXT_FONT_SIZES, codes)
        return []   # 真值是"读没读对"，不是坐标，交给脚本判定

    return build


def _band_builder(width: int, height: int):
    def build(path) -> list[dict]:
        make_band_image(path, width, height, BAND_SPECS)
        return []

    return build


_GROUP_BENCH = "① 基准几何图 · 带真值，可自动算 IoU"
_GROUP_RES = "② 分辨率扫描 · 同一场景、不同栅格（带真值）"
_GROUP_MARKER = "③ 彩色圆点 · 点位与极端长宽比（带真值）"
_GROUP_LEGIBILITY = "④ 细节可读性 · 无坐标真值，看模型能看清多细"
_GROUP_WEB = "⑤ 网页截图 · 真浏览器渲染，真值来自 DOM"

CATALOG: list[TestImage] = [
    # ---- ① 基准几何图 ----
    TestImage("bench_01", _GROUP_BENCH, "基准场景 A（900×720）",
              "合成几何图，4 个已知形状；网页打标后自动算检出率与平均 IoU。",
              BENCH_PROMPT, 900, 720, _bench_builder(0, seed=7, raster=None), evaluable=True),
    TestImage("bench_02", _GROUP_BENCH, "基准场景 B（900×720）",
              "同 A，换一组随机颜色/形状/位置，避免只测一张图偶然全对。",
              BENCH_PROMPT, 900, 720, _bench_builder(1, seed=1007, raster=None), evaluable=True),
    TestImage("bench_03", _GROUP_BENCH, "基准场景 C（900×720）",
              "同 A，第三组随机场景。",
              BENCH_PROMPT, 900, 720, _bench_builder(2, seed=2007, raster=None), evaluable=True),

    # ---- ② 分辨率扫描（场景与基准 A 完全相同） ----
    TestImage("res_0360", _GROUP_RES, "同一场景 · 360×288",
              "与基准 A 完全相同的场景，只是栅格更小：验证小图是否影响精度。",
              BENCH_PROMPT, 360, 288, _bench_builder(0, seed=7, raster=(360, 288)), evaluable=True),
    TestImage("res_0900", _GROUP_RES, "同一场景 · 900×720（参考）",
              "与基准 A 完全相同的场景，作为分辨率扫描的参考点。",
              BENCH_PROMPT, 900, 720, _bench_builder(0, seed=7, raster=(900, 720)), evaluable=True),
    TestImage("res_1800", _GROUP_RES, "同一场景 · 1800×1440",
              "与基准 A 完全相同的场景，栅格放大一倍：验证精度是否随分辨率变化。",
              BENCH_PROMPT, 1800, 1440, _bench_builder(0, seed=7, raster=(1800, 1440)), evaluable=True),
    TestImage("res_2700", _GROUP_RES, "同一场景 · 2700×2160（大图）",
              "与基准 A 完全相同的场景但栅格很大：服务端一定会缩放它，用来观察精度损失。",
              BENCH_PROMPT, 2700, 2160, _bench_builder(0, seed=7, raster=(2700, 2160)), evaluable=True),

    # ---- ③ 彩色圆点 ----
    TestImage("marker_900", _GROUP_MARKER, "圆点阵 900×720",
              "9 个已知位置的彩色圆点：最容易看出坐标约定是否被遵守（旧刻度会全部挤到左上角）。",
              MARKER_PROMPT, 900, 720, _marker_builder(900, 720), evaluable=True),
    TestImage("marker_wide", _GROUP_MARKER, "极端扁图 2400×300（8:1）",
              "极端长宽比：验证服务端缩放是否保持宽高比（若有补边，圆点会整体偏移）。",
              MARKER_PROMPT, 2400, 300, _marker_builder(2400, 300), evaluable=True),
    TestImage("marker_tall", _GROUP_MARKER, "极端竖图 800×2400（1:3）",
              "极端长宽比（竖向）：与服务端 letterbox 行为互相印证。",
              MARKER_PROMPT, 800, 2400, _marker_builder(800, 2400), evaluable=True),

    # ---- ④ 细节可读性 ----
    TestImage("text_900", _GROUP_LEGIBILITY, "文字阶梯 900×720",
              "10 档字号的随机代码：测模型在常规尺寸下最小能读清多少像素的字。",
              TEXT_PROMPT.format(n=len(TEXT_FONT_SIZES)), 900, 720,
              _text_builder(900, 720), evaluable=False),
    TestImage("text_3600", _GROUP_LEGIBILITY, "文字阶梯 3600×2880（大图）",
              "同一套字号、更大的栅格：若服务端缩小了图片，最小可读字号会明显变大。",
              TEXT_PROMPT.format(n=len(TEXT_FONT_SIZES)), 3600, 2880,
              _text_builder(3600, 2880), evaluable=False),
    TestImage("bands_4000", _GROUP_LEGIBILITY, "竖线条纹 4000×1500",
              "10 个间距已知的竖线带：测最小可分辨线间距（注意模型有系统性少数倾向）。",
              BAND_PROMPT.format(n=len(BAND_SPECS)), 4000, 1500,
              _band_builder(4000, 1500), evaluable=False),

    # ---- ⑤ 网页截图（图片由 Playwright 渲染，真值来自 DOM，见"生成器 5"）----
    # 尺寸是**声明的**：manifest 里也有，tests/test_web_samples.py 会把三者
    # （声明 / manifest / PNG 实际像素）对一遍，防止改了页面却忘了更新这里。
    TestImage("web_dashboard", _GROUP_WEB, "电商数据看板（1440×1056）",
              "看板类布局：4 张 KPI 卡片 + 趋势图区 + 待办面板 + 顶部搜索框。"
              "图上干扰文字很多，模型得先挑对区块、再把区块名和框对上。",
              WEB_DASHBOARD_PROMPT, 1440, 1056, _web_builder("web_dashboard"), evaluable=True),
    TestImage("web_shop", _GROUP_WEB, "商品列表页（1440×1023）",
              "商品网格：6 张商品卡以商品名为标签，卡内还有价格/描述/按钮等干扰文本；"
              "另有秒杀横幅与搜索框。测多目标平铺时的定位精度。",
              WEB_SHOP_PROMPT, 1440, 1023, _web_builder("web_shop"), evaluable=True),
    TestImage("web_form", _GROUP_WEB, "登录设置表单页（1440×900）",
              "表单控件：字段名写在控件上方，要把标签正确绑到输入框/下拉/按钮上（placeholder 也算可见文本）。"
              "考的是语义相邻而不是像素相邻——真值只框控件本体，见图内注释。",
              WEB_FORM_PROMPT, 1440, 900, _web_builder("web_form"), evaluable=True),
    TestImage("web_article", _GROUP_WEB, "新闻文章详情页（1440×1150）",
              "图文混排长页：大标题 + 封面图区 + 导语段 + 右栏 3 张推荐卡。"
              "长文本区块的边界本来就有歧义，框大框小都会掉 IoU。",
              WEB_ARTICLE_PROMPT, 1440, 1150, _web_builder("web_article"), evaluable=True),
]

BY_ID: dict[str, TestImage] = {item.id: item for item in CATALOG}

# 生成过的真值缓存：避免每次取真值都重画一遍大图
_GT_CACHE: dict[str, list[dict]] = {}


# --------------------------------------------------------------------------- #
# 对外接口
# --------------------------------------------------------------------------- #
def get(sample_id: str) -> TestImage | None:
    """按 id 取测试图定义。"""
    return BY_ID.get(str(sample_id or ""))


def path_of(sample_id: str) -> Path:
    """测试图在本地的路径（可能尚未生成）。"""
    return SAMPLES_DIR / f"{sample_id}.png"


def ensure(sample_id: str, *, force: bool = False) -> tuple[TestImage, Path]:
    """确保测试图已生成，返回 (定义, 路径)。缺失时现场生成并缓存真值。"""
    item = get(sample_id)
    if item is None:
        raise KeyError(f"未知的测试图: {sample_id}")

    path = path_of(item.id)
    if force or not path.exists():
        _GT_CACHE[item.id] = item.build(path) or []
    elif item.id not in _GT_CACHE:
        # 图片已存在但缓存丢了（比如服务重启）：重新生成一次以拿回真值。
        # 生成是确定性的（固定 seed），重画不会改变图片内容。
        _GT_CACHE[item.id] = item.build(path) or []
    return item, path


def ground_truth(sample_id: str) -> list[dict]:
    """取测试图真值（0.0~1.0 相对坐标；无真值的图返回空列表）。"""
    item, _path = ensure(sample_id)
    return _GT_CACHE.get(item.id, [])


def as_items(gt: list[dict]) -> list[dict]:
    """把真值裁剪成可直接标注/展示的坐标对象（去掉 kind/color_name 等内部字段）。"""
    return [{"bbox_2d": list(g["bbox_2d"]), "label": g["label"]} for g in gt]


def list_catalog() -> list[dict]:
    """给前端的目录元数据（不含真值，避免把答案提前泄露到页面上）。"""
    groups: list[dict] = []
    index: dict[str, dict] = {}
    for item in CATALOG:
        bucket = index.get(item.group)
        if bucket is None:
            bucket = {"group": item.group, "items": []}
            index[item.group] = bucket
            groups.append(bucket)
        bucket["items"].append({
            "id": item.id,
            "title": item.title,
            "purpose": item.purpose,
            "prompt": item.prompt,
            "width": item.width,
            "height": item.height,
            "evaluable": item.evaluable,
            "url": f"/api/samples/{item.id}/image",
        })
    return groups


def accuracy(sample_id: str, preds: list[dict], width: int, height: int) -> dict | None:
    """用真值给一次打标结果打分；无真值的测试图返回 None。

    同时给出 evaluate_any_space 的坐标空间诊断：space == "pixel" 说明模型又退回
    像素坐标了（约定未被遵守），此时 *_best 代表"如果外部帮忙换算"的上限成绩。
    """
    gt = ground_truth(sample_id)
    if not gt:
        return None
    report = evaluate_sample(preds, gt)
    space = evaluate_any_space(preds, gt, width, height)
    return {
        "sample_id": sample_id,
        "gt_count": report["gt_count"],
        "hits": report["hits"],
        "detection_rate": report["detection_rate"],
        "mean_iou": report["mean_iou"],
        "label_accuracy": report["label_accuracy"],
        "precision": report["precision"],
        "coord_space": space["space"],
        "detection_rate_best": space["detection_rate_best"],
        "mean_iou_best": space["mean_iou_best"],
        "per_target": report["per_target"],
        "ground_truth": as_items(gt),
    }


__all__ = [
    "TestImage", "CATALOG", "SAMPLES_DIR", "BY_ID",
    "get", "path_of", "ensure", "ground_truth", "as_items", "list_catalog", "accuracy",
    "make_marker_image", "markers_to_gt", "make_text_image", "make_band_image",
    "render_scaled", "random_codes", "MARKER_FRACTIONS", "TEXT_FONT_SIZES", "BAND_SPECS",
    "WEB_SAMPLES_DIR", "WEB_MANIFEST", "WEB_CAPTURE_SCRIPT",
    "capture_web_samples", "web_entry", "web_targets_to_gt",
]
