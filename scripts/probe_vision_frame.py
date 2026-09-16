# -*- coding: utf-8 -*-
"""探测 deepseek-flash 做物体定位时"实际看到的"到底是什么。

背景（用户疑虑）：接口文档说图片进入模型前会被自动缩放（<544x544 放大，大图缩到
总像素约等于 1300x1300），且**不回传缩放后的尺寸**。那么模型输出的"像素坐标"
究竟落在哪个坐标系里？它看到的画面有没有被压扁、有没有补黑边？

本脚本用三组**可程序化判定**的实验来回答，不靠人眼：

1) frame  —— 帧几何探测（核心）
   生成一张带 9 个已知位置彩色圆点的图，让模型**只用像素坐标**回答 bbox。
   每个圆点已知真值比例 f = 真值像素 / 图宽，于是"模型使用的帧尺寸"可反推：
       W' = 预测中心x / f_x        （对每个点独立算一次）
   再用最小二乘拟合 预测 = a * f + c：
       a  = 模型工作的帧尺寸（它自称的像素单位）
       c  = 常数偏移；c≈0 表示没有 letterbox/补边（纯线性缩放）
       R2 = 线性度；≈1 表示它确实在一个线性缩放的帧里按比例定位
   对多种原始尺寸各跑一次，即可看出 a 与原始尺寸/文档预测缩放尺寸的关系。

2) bands  —— 有效分辨率阶梯
   一张大图切成 10 个横带，每带里竖线的**间距与条数都已知**，让模型用 JSON 报每带条数，
   程序化比对。用它量出"模型实际能分辨的最小线间距"，即它真正看到多少细节。

3) accuracy —— 分辨率扫描（复用项目合成图 + 真值）
   在 5 种原始尺寸下跑真实评测，全部指标由代码按 IoU/颜色/形状判定：
   检出率、平均 IoU、模型是否退回像素坐标空间。

用法：
    python scripts/probe_vision_frame.py --parts frame,bands
    python scripts/probe_vision_frame.py --parts accuracy --count 3
    python scripts/probe_vision_frame.py --self-test      # 不花 API，只校验几何拟合数学
    python scripts/probe_vision_frame.py --dry-run        # 只生成图片，不调模型

输出：runs/vision_probe/ 下的图片、*_report.json 与 summary.md
"""

from __future__ import annotations

import argparse
import json
import math
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from PIL import Image, ImageDraw  # noqa: E402

from objloc.agent import build_messages, collect_items, run_agent  # noqa: E402
from objloc.benchmark import (  # noqa: E402
    PALETTE,
    aggregate,
    evaluate_any_space,
    evaluate_sample,
    make_sample,
)
from objloc.config import PROJECT_ROOT, get_settings  # noqa: E402
from objloc.parsing import decode_json_points  # noqa: E402
from objloc.providers import build_client  # noqa: E402
# 图片生成器统一来自 objloc.samples：网页「测试图片」面板用的就是这几个函数，
# 保证「手动点出来的结论」和「脚本测出来的结论」在测同一批图。
from objloc.samples import (  # noqa: E402
    BAND_SPECS,
    MARKER_FRACTIONS,
    TEXT_FONT_SIZES,
    make_band_image,
    make_marker_image,
    make_text_image,
    render_scaled,
)
from objloc.visualizer import image_to_data_url_from_source  # noqa: E402

# --------------------------------------------------------------------------- #
# 通用：调用模型
# --------------------------------------------------------------------------- #
def ask_model(client, image_path, prompt, *, system=None, detail=None):
    """发一次带图请求，返回 (正文文本, 思考文本)。不使用工具，避免 agent 循环干扰探测。"""
    data_url = image_to_data_url_from_source(image_path)
    block = {"type": "image_url", "image_url": {"url": data_url}}
    if detail:
        block["image_url"]["detail"] = detail
    messages = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": [block, {"type": "text", "text": prompt}]})

    content, reasoning = [], []
    for event in client.stream_chat(messages, tools=None):
        if event.get("type") == "content":
            content.append(event["text"])
        elif event.get("type") == "reasoning":
            reasoning.append(event["text"])
    return "".join(content), "".join(reasoning)


# --------------------------------------------------------------------------- #
# 数学：线性拟合与文档预测
# --------------------------------------------------------------------------- #
def fit_affine(xs, ys):
    """最小二乘拟合 y = a*x + c，返回 (a, c, r2)；样本不足返回 None。"""
    n = len(xs)
    if n < 2:
        return None
    mx, my = sum(xs) / n, sum(ys) / n
    sxx = sum((x - mx) ** 2 for x in xs)
    if sxx == 0:
        return None
    sxy = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    a = sxy / sxx
    c = my - a * mx
    ss_res = sum((y - (a * x + c)) ** 2 for x, y in zip(xs, ys))
    ss_tot = sum((y - my) ** 2 for y in ys)
    r2 = 1.0 - ss_res / ss_tot if ss_tot > 0 else 1.0
    return a, c, r2


def doc_rescaled_scale(width, height):
    """按官方文档口径推算的缩放因子。

    文档：总像素小于约 544x544 的图保持长宽比放大；更大的保持长宽比缩小，
    缩后总像素约相当于 1300x1300。这里给出的是一个**假设**，用于和实测对照。
    """
    total = width * height
    lo, hi = 544.0 * 544.0, 1300.0 * 1300.0
    if total < lo:
        return math.sqrt(lo / total)
    if total > hi:
        return math.sqrt(hi / total)
    return 1.0


# --------------------------------------------------------------------------- #
# 实验 1：帧几何探测
# --------------------------------------------------------------------------- #
# 圆点位置与生成函数统一由 objloc.samples 提供（网页「测试图片」用的是同一份实现）


# 帧几何探测用的原始尺寸：覆盖「会被放大 / 不变 / 会被缩小」以及极端长宽比
FRAME_SIZES = [
    (320, 240),    # 小图（文档说会被放大）
    (640, 480),    # 恰在 544x544 门槛附近
    (900, 720),    # 项目基准尺寸
    (1600, 1200),  # 中等
    (3000, 2250),  # 大图（必然被缩小）
    (4000, 3000),  # 更大
    (2400, 300),   # 极端扁（8:1）
    (800, 2400),   # 极端竖（1:3）
]

FRAME_PROMPT = (
    "图中有 9 个彩色圆点，每个圆点右侧写着它的中文颜色名。\n"
    "请**只使用像素坐标**（你在这张图上量到的绝对像素位置），给出每个圆点的包围盒 bbox_2d，"
    "不要换算成 0~1000 的归一化值。\n"
    "先输出一个 JSON 数组，形如：\n"
    '[{"bbox_2d": [x1, y1, x2, y2], "label": "颜色名"}, ...]\n'
    "然后在 JSON 之后另起一行，逐行回答两个附加问题（不要写进 JSON）：\n"
    "SIZE: 你看到的这张图片大约是 宽x高 多少像素\n"
    "BORDER: 图片四周是否有黑边或白边（有/没有）"
)


def _color_of(label, palette):
    """从模型的 label 里认出调色板颜色名（含同义词）。"""
    text = str(label or "")
    for name in palette:
        if name in text:
            return name
    synonyms = {"红": "红色", "绿": "绿色", "蓝": "蓝色", "橙": "橙色", "橘": "橙色",
                "紫": "紫色", "黄": "黄色", "青": "青色", "粉": "粉色", "灰": "灰色",
                "棕": "棕色", "褐": "棕色"}
    for key, name in synonyms.items():
        if key in text:
            return name
    return None


def probe_frame_once(client, out_dir, width, height, *, detail=None, tag=""):
    """对一种原始尺寸跑一次帧几何探测，返回结果字典。"""
    name = f"markers_{width}x{height}{tag}"
    img_path = Path(out_dir) / "markers" / f"{name}.png"
    truth = make_marker_image(img_path, width, height)

    started = time.time()
    text = reasoning = ""
    error = None
    try:
        text, reasoning = ask_model(client, img_path, FRAME_PROMPT, detail=detail)
    except Exception as exc:  # noqa: BLE001 - 探测脚本要记录而不是抛出
        error = f"{type(exc).__name__}: {exc}"

    preds = [p for p in decode_json_points(text) if isinstance(p, dict)] if text else []
    by_color = {t["label"]: t for t in truth}
    samples = []          # (f, 预测坐标) 供拟合
    per_marker = []
    for pred in preds:
        color = _color_of(pred.get("label"), PALETTE)
        marker = by_color.get(color or "")
        if marker is None:
            continue
        if "bbox_2d" in pred:
            try:
                x1, y1, x2, y2 = [float(v) for v in pred["bbox_2d"]]
            except Exception:  # noqa: BLE001
                continue
            px, py = (x1 + x2) / 2, (y1 + y2) / 2
        elif "point_2d" in pred:
            try:
                px, py = [float(v) for v in pred["point_2d"]]
            except Exception:  # noqa: BLE001
                continue
        else:
            continue
        per_marker.append({
            "label": color, "fx": marker["fx"], "fy": marker["fy"],
            "pred_x": px, "pred_y": py,
            "implied_w": px / marker["fx"] if marker["fx"] else None,
            "implied_h": py / marker["fy"] if marker["fy"] else None,
        })
        samples.append((marker["fx"], px, marker["fy"], py))

    fit_x = fit_affine([s[0] for s in samples], [s[1] for s in samples])
    fit_y = fit_affine([s[2] for s in samples], [s[3] for s in samples])
    scale = doc_rescaled_scale(width, height)

    result = {
        "image": str(img_path), "width": width, "height": height, "detail": detail or "auto",
        "matched": len(per_marker), "expected": len(truth),
        "fit_x": fit_x, "fit_y": fit_y,
        "doc_scale": round(scale, 4),
        "doc_frame": [round(width * scale, 1), round(height * scale, 1)],
        "per_marker": per_marker,
        "final_text": text,
        "reasoning_chars": len(reasoning),
        "elapsed_s": round(time.time() - started, 1),
        "error": error,
    }
    if fit_x and fit_y:
        result["frame"] = [round(fit_x[0], 1), round(fit_y[0], 1)]
        result["offset"] = [round(fit_x[1], 1), round(fit_y[1], 1)]
        result["verdict"] = judge_frame(result)
    return result


def judge_frame(result):
    """把拟合结果翻译成人话。"""
    a_x, c_x, r2_x = result["fit_x"]
    a_y, c_y, r2_y = result["fit_y"]
    W, H = result["width"], result["height"]
    notes = []
    if abs(a_x - 1000) < 80 and abs(a_y - 1000) < 80:
        notes.append("模型把 0~1000 归一化网格当成像素在用（a≈1000）")
    elif abs(a_x / W - 1) < 0.1 and abs(a_y / H - 1) < 0.1:
        notes.append("模型报的数值≈原图尺寸（在原始像素空间回答）")
    else:
        notes.append(f"模型工作的帧 ≈ {a_x:.0f}x{a_y:.0f}（原图 {W}x{H} 的 {a_x / W:.2f} 倍）")
    aspect_model = a_x / a_y if a_y else 0
    aspect_true = W / H
    notes.append(
        f"帧长宽比 {aspect_model:.3f} vs 原图 {aspect_true:.3f}"
        + ("（等比缩放）" if abs(aspect_model - aspect_true) / aspect_true < 0.05 else "（不等比！）")
    )
    if abs(c_x) > 0.02 * a_x or abs(c_y) > 0.02 * a_y:
        notes.append(f"存在非零偏移 c=({c_x:.1f},{c_y:.1f}) → 疑似补边/letterbox")
    else:
        notes.append("截距≈0 → 没有补边，纯线性缩放")
    notes.append(f"线性度 R2=({r2_x:.3f},{r2_y:.3f})")
    return "；".join(notes)


def extract_answer_line(text, key):
    """取 'SIZE: ...' / 'BORDER: ...' 这类行。"""
    for line in (text or "").splitlines():
        stripped = line.strip()
        if stripped.upper().startswith(key.upper()):
            return stripped.split(":", 1)[-1].strip()
    return ""


# --------------------------------------------------------------------------- #
# 实验 2：有效分辨率阶梯（竖线带）
# --------------------------------------------------------------------------- #
BAND_PROMPT = (
    "这张图从上到下分成 {n} 个横带，每个带左侧写着带编号（1 到 {n}）。\n"
    "每个带里有一簇竖直的黑色细线；有的带线条太密，可能糊成一片看不清。\n"
    "请数出每个带里**你能分辨出来的竖线条数**（完全看不清就填 0）。\n"
    '只输出 JSON：{{"bands": [{{"index": 1, "lines": 5}}, ...]}}'
)


def probe_bands(client, out_dir, width=4000, height=1500, *, detail=None):
    """跑一次竖线带探测。"""
    img_path = Path(out_dir) / "bands" / f"bands_{width}x{height}.png"
    truth = make_band_image(img_path, width, height, BAND_SPECS)
    prompt = BAND_PROMPT.format(n=len(BAND_SPECS))
    started = time.time()
    text = reasoning = ""
    error = None
    try:
        text, reasoning = ask_model(client, img_path, prompt, detail=detail)
    except Exception as exc:  # noqa: BLE001
        error = f"{type(exc).__name__}: {exc}"

    parsed = decode_json_points(text) if text else {}
    if isinstance(parsed, dict):
        pass                                  # 形如 {"bands": [...]}
    elif isinstance(parsed, list) and parsed and isinstance(parsed[0], dict) and "bands" in parsed[0]:
        parsed = parsed[0]                    # 形如 [{"bands": [...]}]
    elif isinstance(parsed, list):
        parsed = {"bands": parsed}            # 形如 [{"index":1,"lines":3}, ...]
    bands = parsed.get("bands") if isinstance(parsed, dict) else None
    answers = {}
    for item in bands or []:
        if isinstance(item, dict) and "index" in item:
            try:
                answers[int(item["index"])] = int(item.get("lines", 0))
            except (TypeError, ValueError):
                continue

    scale = doc_rescaled_scale(width, height)
    rows = []
    for t in truth:
        got = answers.get(t["index"])
        rows.append({
            **t,
            "predicted": got,
            "ok": got == t["lines"],
            "rescaled_spacing": round(t["spacing"] * scale, 2),
        })
    correct = [r for r in rows if r["predicted"] is not None]
    return {
        "image": str(img_path), "width": width, "height": height,
        "doc_scale": round(scale, 4),
        "rows": rows,
        "answered": len(correct), "correct": sum(1 for r in correct if r["ok"]),
        "final_text": text, "reasoning_chars": len(reasoning),
        "elapsed_s": round(time.time() - started, 1), "error": error,
    }


# --------------------------------------------------------------------------- #
# 实验 3：文字可读性阶梯 —— 直接量「模型实际能看到多细的细节」
# --------------------------------------------------------------------------- #
# 同一个模型、同一套字号（单位：**原始像素**），只改图片栅格分辨率。
# 如果服务端真的把大图缩小了，那么大图上的"最小可读字号"会按缩放因子等比变大，
# 由此可反推出真实缩放因子 s：
#     s ≈ 像素基准阈值 / 该尺寸下的阈值
# 这比数竖线可靠：读出来/读不出来是一个二值、可程序化判定的信号。
TEXT_SIZES = [(450, 360), (900, 720), (1800, 1440), (3600, 2880)]

TEXT_PROMPT = (
    "图中从上到下分成 {n} 行，每行左侧是行号，右侧是一段 {k} 位的大写字母数字代码。\n"
    "请逐行转写你**能看清**的代码；看不清的行代码填 null。\n"
    '只输出 JSON：{{"lines": [{{"index": 1, "code": "ABCD"}}, ...]}}'
)


def probe_text(client, out_dir, size):
    """在一种分辨率下跑文字可读性阶梯。"""
    import random as _random

    width, height = size
    rng = _random.Random(sum(size))
    codes = ["".join(rng.choice(_CODE_ALPHABET) for _ in range(4)) for _ in TEXT_FONT_SIZES]
    img_path = Path(out_dir) / "text" / f"text_{width}x{height}.png"
    truth = make_text_image(img_path, width, height, TEXT_FONT_SIZES, codes)
    prompt = TEXT_PROMPT.format(n=len(TEXT_FONT_SIZES), k=4)
    started = time.time()
    text = reasoning = ""
    error = None
    try:
        text, reasoning = ask_model(client, img_path, prompt)
    except Exception as exc:  # noqa: BLE001
        error = f"{type(exc).__name__}: {exc}"

    parsed = decode_json_points(text) if text else {}
    if isinstance(parsed, list) and parsed and isinstance(parsed[0], dict) and "lines" in parsed[0]:
        parsed = parsed[0]
    answers = {}
    for item in (parsed.get("lines") if isinstance(parsed, dict) else None) or []:
        if isinstance(item, dict) and "index" in item:
            try:
                answers[int(item["index"])] = (item.get("code") or "").strip().upper()
            except (TypeError, ValueError):
                continue

    rows = []
    for t in truth:
        got = answers.get(t["index"])
        rows.append({**t, "read": got,
                     "ok": bool(got) and got == t["code"].upper(),
                     "answered": got is not None})
    readable = [r["font_px"] for r in rows if r["ok"]]
    return {
        "image": str(img_path), "width": width, "height": height,
        "doc_scale": round(doc_rescaled_scale(width, height), 4),
        "rows": rows,
        "min_readable_font_px": min(readable) if readable else None,
        "correct": sum(1 for r in rows if r["ok"]), "total": len(rows),
        "final_text": text, "reasoning_chars": len(reasoning),
        "elapsed_s": round(time.time() - started, 1), "error": error,
    }


# --------------------------------------------------------------------------- #
# 实验 4：同一张图重复调用 —— 模型自称的"帧"是否稳定
# --------------------------------------------------------------------------- #
def probe_repeat(client, out_dir, size, repeats=3):
    """同一张图、同一提示词，重复调用，看它每次认定的画布尺寸是否一致。"""
    rows = []
    for i in range(repeats):
        row = probe_frame_once(client, out_dir, *size, tag=f"_rep{i + 1}")
        row["size_answer"] = extract_answer_line(row["final_text"], "SIZE")
        rows.append(row)
    frames = [r["frame"] for r in rows if r.get("frame")]
    return {
        "width": size[0], "height": size[1], "repeats": repeats, "rows": rows,
        "frames": frames,
        "size_answers": [r["size_answer"] for r in rows],
    }


# --------------------------------------------------------------------------- #
# 实验 5：分辨率扫描（复用项目合成图 + 真值，全程序化判定）
# --------------------------------------------------------------------------- #
ACCURACY_PROMPT = (
    "识别图中的几何图形。为每个图形输出 bbox_2d 坐标与中文名称，"
    "名称需包含颜色与形状（例如：蓝色矩形、红色圆形、绿色三角形）。"
)

# 分辨率扫描：**场景完全相同**，只改栅格分辨率。
# 场景先在 900x720 的设计画布上用 objloc.benchmark.make_sample 生成，
# 再整体等比放大/缩小到目标尺寸渲染 —— 这样各尺寸的真值（归一化坐标）逐字节相同，
# 精度差异才只能归因于"栅格分辨率 / 服务端重采样"，而不是"场景变了"。
ACCURACY_REFERENCE = (900, 720)
ACCURACY_SIZES = [(360, 288), (900, 720), (1800, 1440), (2700, 2160)]


def probe_accuracy(client, out_dir, size, *, count=3, n_shapes=4, seed=7):
    """在一种原始尺寸下跑合成图评测，指标全部由代码判定。"""
    width, height = size
    img_dir = Path(out_dir) / "accuracy" / f"{width}x{height}" / "images"
    img_dir.mkdir(parents=True, exist_ok=True)
    samples = []
    for i in range(count):
        reference, _ = make_sample(i, seed=seed + i * 1000, width=ACCURACY_REFERENCE[0],
                                   height=ACCURACY_REFERENCE[1], n_shapes=n_shapes)
        samples.append(render_scaled(reference, width, height,
                                     img_dir / f"sample_{i + 1:02d}.png"))
    reports = []
    for sample in samples:
        started = time.time()
        events, final_text, error = [], "", None
        try:
            data_url = image_to_data_url_from_source(sample.path)
            messages = build_messages(ACCURACY_PROMPT, image=data_url)
            for event in run_agent(messages, tool_context={"source": sample.path}):
                events.append(event)
                if event.get("type") == "done":
                    final_text = event.get("content") or ""
                elif event.get("type") == "error":
                    error = event.get("message")
        except Exception as exc:  # noqa: BLE001
            error = f"{type(exc).__name__}: {exc}"
        preds = collect_items(events, final_text)
        gts = sample.ground_truth()
        report = evaluate_sample(preds, gts)
        space = evaluate_any_space(preds, gts, sample.width, sample.height)
        report.update({
            "name": sample.name, "elapsed_s": round(time.time() - started, 1),
            "error": error, "predictions": preds, "ground_truth": gts,
            "coord_space": space["space"],
            "detection_rate_best": space["detection_rate_best"],
            "mean_iou_best": space["mean_iou_best"],
        })
        reports.append(report)
    summary = aggregate(reports)
    out = {
        "width": width, "height": height,
        "doc_scale": round(doc_rescaled_scale(width, height), 4),
        "summary": summary, "reports": reports,
    }
    Path(out_dir).mkdir(parents=True, exist_ok=True)
    (Path(out_dir) / f"accuracy_{width}x{height}.json").write_text(
        json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    return out


# --------------------------------------------------------------------------- #
# 自检 / 主流程
# --------------------------------------------------------------------------- #
def self_test() -> int:
    """不花 API，用构造数据验证拟合与判读逻辑。"""
    # 构造：帧 1300x975（等比），无偏移
    fx = [m[0] for m in MARKER_FRACTIONS]
    fy = [m[1] for m in MARKER_FRACTIONS]
    fit_x = fit_affine(fx, [f * 1300 for f in fx])
    fit_y = fit_affine(fy, [f * 975 for f in fy])
    ok = (abs(fit_x[0] - 1300) < 1e-6 and abs(fit_x[1]) < 1e-6 and fit_x[2] > 0.999
          and abs(fit_y[0] - 975) < 1e-6)
    print(f"[self-test] 等比帧拟合 a=({fit_x[0]:.3f},{fit_y[0]:.3f}) c=({fit_x[1]:.2e},{fit_y[1]:.2e}) "
          f"R2=({fit_x[2]:.4f},{fit_y[2]:.4f}) -> {'OK' if ok else 'FAIL'}")
    # 构造：带 letterbox 偏移 60
    fit_lb = fit_affine(fy, [f * 975 + 60 for f in fy])
    print(f"[self-test] letterbox 偏移检出 c_y={fit_lb[1]:.2f} -> "
          f"{'OK' if abs(fit_lb[1] - 60) < 1e-6 else 'FAIL'}")
    verdict = judge_frame({"fit_x": fit_x, "fit_y": fit_y, "width": 4000, "height": 3000})
    print(f"[self-test] 判读：{verdict}")
    # 文档缩放口径
    for size in ACCURACY_SIZES + [(4000, 1500)]:
        s = doc_rescaled_scale(*size)
        print(f"[self-test] {size} -> 缩放 {s:.3f} => {size[0] * s:.0f}x{size[1] * s:.0f}")
    return 0 if ok else 1


def main() -> int:
    parser = argparse.ArgumentParser(description="探测 deepseek-flash 实际看到的图像与坐标系")
    parser.add_argument("--parts", default="frame,text,bands",
                        help="要跑的探测组，逗号分隔：frame,text,bands,repeat,accuracy")
    parser.add_argument("--out", default=str(PROJECT_ROOT / "runs" / "vision_probe"))
    parser.add_argument("--count", type=int, default=3, help="accuracy 组每种尺寸的图片数")
    parser.add_argument("--n-shapes", type=int, default=4)
    parser.add_argument("--detail", default=None, help="image_url.detail，默认不传（等价 auto）")
    parser.add_argument("--self-test", action="store_true", help="只校验数学，不调模型")
    parser.add_argument("--dry-run", action="store_true", help="只生成图片，不调模型")
    args = parser.parse_args()

    if args.self_test:
        return self_test()

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    parts = [p.strip() for p in args.parts.split(",") if p.strip()]

    if args.dry_run:
        for size in FRAME_SIZES:
            make_marker_image(out_dir / "markers" / f"markers_{size[0]}x{size[1]}.png", *size)
        make_band_image(out_dir / "bands" / "bands_4000x1500.png", 4000, 1500, BAND_SPECS)
        print(f"[dry-run] 图片已生成到 {out_dir}")
        return 0

    settings = get_settings(refresh=True)
    print(f"[env] provider={settings.resolved_provider()} model={settings.model}")
    if settings.resolved_provider() != "deepseek":
        print("[fatal] 没有可用 API Key，探测必须走真实模型")
        return 2
    client = build_client(settings)

    report = {"model": settings.model, "parts": parts}

    if "frame" in parts:
        frame_rows = []
        for size in FRAME_SIZES:
            row = probe_frame_once(client, out_dir, *size)
            row["size_answer"] = extract_answer_line(row["final_text"], "SIZE")
            row["border_answer"] = extract_answer_line(row["final_text"], "BORDER")
            frame_rows.append(row)
            print(f"[frame] {size[0]}x{size[1]}: 匹配 {row['matched']}/{row['expected']} "
                  f"帧={row.get('frame')} 偏移={row.get('offset')} "
                  f"doc预测={row['doc_frame']} | {row.get('verdict', row.get('error'))}")
            print(f"        SIZE->{row['size_answer']!r} BORDER->{row['border_answer']!r}")
        if args.detail:
            for size in [(3000, 2250), (900, 720)]:
                row = probe_frame_once(client, out_dir, *size,
                                       detail=args.detail, tag=f"_detail_{args.detail}")
                row["size_answer"] = extract_answer_line(row["final_text"], "SIZE")
                row["border_answer"] = extract_answer_line(row["final_text"], "BORDER")
                frame_rows.append(row)
                print(f"[frame] detail={args.detail} {size[0]}x{size[1]}: 帧={row.get('frame')} "
                      f"SIZE->{row['size_answer']!r} | {row.get('verdict', row.get('error'))}")
        report["frame"] = frame_rows
        (out_dir / "frame_report.json").write_text(
            json.dumps(frame_rows, ensure_ascii=False, indent=2), encoding="utf-8")

    if "bands" in parts:
        band_row = probe_bands(client, out_dir)
        report["bands"] = band_row
        (out_dir / "bands_report.json").write_text(
            json.dumps(band_row, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"[bands] 回答 {band_row['answered']}/{len(BAND_SPECS)} 带，条数正确 "
              f"{band_row['correct']}/{band_row['answered']}")
        for r in band_row["rows"]:
            print(f"       band{r['index']:>2} 间距{r['spacing']:>3}px(缩放后{r['rescaled_spacing']:>5}) "
                  f"真值{r['lines']} 模型{r['predicted']} {'OK' if r['ok'] else '×'}")

    if "text" in parts:
        text_rows = []
        for size in TEXT_SIZES:
            row = probe_text(client, out_dir, size)
            text_rows.append(row)
            detail = ", ".join(
                f"{r['font_px']}px={'OK' if r['ok'] else ('读错' if r['answered'] else 'null')}"
                for r in row["rows"]
            )
            print(f"[text] {size[0]}x{size[1]} (doc缩放 {row['doc_scale']}): "
                  f"最小可读字号 {row['min_readable_font_px']}px | {detail}")
        report["text"] = text_rows
        (out_dir / "text_report.json").write_text(
            json.dumps(text_rows, ensure_ascii=False, indent=2), encoding="utf-8")

    if "repeat" in parts:
        repeat_rows = []
        for size in [(900, 720), (3000, 2250)]:
            row = probe_repeat(client, out_dir, size, repeats=3)
            repeat_rows.append(row)
            print(f"[repeat] {size[0]}x{size[1]} 三次拟合帧: {row['frames']} "
                  f"模型自述: {row['size_answers']}")
        report["repeat"] = repeat_rows
        (out_dir / "repeat_report.json").write_text(
            json.dumps(repeat_rows, ensure_ascii=False, indent=2), encoding="utf-8")

    if "accuracy" in parts:
        acc_rows = []
        for size in ACCURACY_SIZES:
            row = probe_accuracy(client, out_dir, size, count=args.count, n_shapes=args.n_shapes)
            acc_rows.append({k: v for k, v in row.items() if k != "reports"})
            s = row["summary"]
            print(f"[accuracy] {size[0]}x{size[1]} (doc 缩放 {row['doc_scale']}): "
                  f"检出 {s['total_hits']}/{s['total_gt']} IoU {s['mean_iou']:.3f} "
                  f"标签 {s['label_accuracy']:.0%} 像素空间图 {s['pixel_space_images']}/{s['images']}")
        report["accuracy"] = acc_rows
        (out_dir / "accuracy_summary.json").write_text(
            json.dumps(acc_rows, ensure_ascii=False, indent=2), encoding="utf-8")

    (out_dir / "probe_report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n报告：{out_dir / 'probe_report.json'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
