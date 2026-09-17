# -*- coding: utf-8 -*-
"""内置测试图片目录 —— 网页「测试图片」面板、探测脚本、评测脚本共用的唯一生成源。

为什么要有这个模块
------------------
"准确率由程序判定"的前提是图片答案已知。本项目所有测试图都由代码生成：每个图形/圆点的
真实位置在绘制时就拿到了，因此真值不依赖人眼标注，也不需要把二进制图片提交进仓库。

三处使用者必须是同一份实现，否则网页上手动测试的结果和脚本评测的结论对不上：

- 网页「测试图片」面板：GET /api/samples、POST /api/samples/{id}/load
- 探测脚本 scripts/probe_vision_frame.py：量模型实际看到的帧几何 / 有效分辨率
- 评测脚本 scripts/benchmark.py：批量准确率

职责
----
- `TestImage` 数据类与 `CATALOG` 目录（17 张图，分五组）；
- 各组提示词 —— 只描述"找什么"；
- 对外接口：get / path_of / ensure / ground_truth / as_items / list_catalog / accuracy。

边界
----
不画图（generators.py）、不抓网页（web_samples.py）：本模块只做"登记与调度"，
把某个 id 交给对应的 builder，并把真值缓存起来。

⚠️ 提示词**只描述"找什么"，不描述"坐标怎么给"**：坐标口径的唯一来源是
`objloc/config.py: DEFAULT_SYSTEM_PROMPT`（见 AGENTS.md#4.4-系统提示词），
在这里再抄一份会让两种口径漂移。

旧文件对应关系（等价重构）
--------------------------
本模块 = 拆分前 `objloc/samples.py` 的「目录定义」+「对外接口」两节（含 SAMPLES_DIR），
逐行搬移、无改写。

@doc AGENTS.md#4.3-坐标约定
@doc AGENTS.md#7-打标准确率评测结论重要
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from objloc.benchmark import evaluate_any_space, evaluate_sample, make_sample
from objloc.config import RUNS_DIR

from .generators import (
    BAND_SPECS,
    TEXT_FONT_SIZES,
    make_band_image,
    make_marker_image,
    make_text_image,
    markers_to_gt,
    random_codes,
    render_scaled,
)
from .web_samples import _web_builder

SAMPLES_DIR = RUNS_DIR / "samples"

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
