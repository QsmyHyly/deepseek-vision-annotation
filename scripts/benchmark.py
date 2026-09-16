# -*- coding: utf-8 -*-
"""打标准确率评测：用代码生成一批「已知答案」的图形图片，让真实模型定位打标，
再按 IoU / 颜色 / 形状计算准确率。

用法：
    python scripts/benchmark.py --count 5                 # 生成图片 + 调模型评测
    python scripts/benchmark.py --images-only             # 只生成图片
    python scripts/benchmark.py --provider mock --count 2 # 不花 API 跑通流程

输出：
    runs/benchmark/images/*.png + ground_truth.json
    runs/benchmark/report.json
    runs/benchmark/annotated/*.png （带 --annotate）
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from objloc.agent import build_messages, collect_items, run_agent  # noqa: E402
from objloc.benchmark import (  # noqa: E402
    aggregate,
    evaluate_any_space,
    evaluate_sample,
    make_samples,
)
from objloc.config import get_settings, PROJECT_ROOT  # noqa: E402
from objloc.visualizer import image_to_data_url_from_source, render_annotations  # noqa: E402

DEFAULT_PROMPT = (
    "识别图中的几何图形。为每个图形输出 bbox_2d 坐标与中文名称，"
    "名称需包含颜色与形状（例如：蓝色矩形、红色圆形、绿色三角形）。"
)


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="生成测试图片并评测模型打标准确率")
    p.add_argument("--count", type=int, default=5, help="图片数量")
    p.add_argument("--seed", type=int, default=42, help="随机种子")
    p.add_argument("--n-shapes", type=int, default=3, help="每张图的图形数量")
    p.add_argument("--width", type=int, default=900)
    p.add_argument("--height", type=int, default=720)
    p.add_argument("--provider", default=None, help="auto/deepseek/mock，覆盖 LLM_PROVIDER")
    p.add_argument("--prompt", default=DEFAULT_PROMPT, help="用户提示词")
    p.add_argument("--system-prompt", default=None, help="覆盖系统提示词（用于 A/B 测试）")
    p.add_argument("--out", default=str(PROJECT_ROOT / "runs" / "benchmark"))
    p.add_argument("--images-only", action="store_true", help="只生成图片，不调用模型")
    p.add_argument("--no-tools", action="store_true", help="评测时不启用工具执行")
    p.add_argument("--no-thinking", action="store_true",
                   help="关闭思考模式（模型直接给正文，更快更省 token）")
    p.add_argument("--annotate", action="store_true", help="保存标注结果图")
    p.add_argument("--iou", type=float, default=0.5, help="判定命中的 IoU 阈值")
    return p.parse_args()


def main() -> int:
    args = parse_args()
    if args.provider:
        os.environ["LLM_PROVIDER"] = args.provider
    if args.system_prompt:
        os.environ["SYSTEM_PROMPT"] = args.system_prompt

    out_dir = Path(args.out)
    img_dir = out_dir / "images"
    ann_dir = out_dir / "annotated"

    print(f"[1/3] 生成 {args.count} 张测试图片 -> {img_dir}")
    samples = make_samples(
        args.count,
        seed=args.seed,
        out_dir=img_dir,
        width=args.width,
        height=args.height,
        n_shapes=args.n_shapes,
    )
    for s in samples:
        print(f"      {s.name}: {len(s.shapes)} 个图形 -> {', '.join(x.label for x in s.shapes)}")

    if args.images_only:
        print("[done] 仅生成图片")
        return 0

    settings = get_settings()
    thinking_default = "关闭" if args.no_thinking else f"{'开启' if settings.thinking else '关闭'}（默认）"
    print(f"[2/3] 调用模型评测 provider={settings.resolved_provider()} "
          f"model={settings.model} thinking={thinking_default}")

    reports = []
    report_path = out_dir / "report.json"

    for idx, sample in enumerate(samples):
        print(f"      [{idx + 1}/{len(samples)}] {sample.name} ...", end="", flush=True)
        started = time.time()

        events: list[dict] = []
        final_text = ""
        error = None
        try:
            image_url = image_to_data_url_from_source(sample.path)
            messages = build_messages(args.prompt, image=image_url)
            for event in run_agent(
                messages,
                use_tools=not args.no_tools,
                tool_context={"source": sample.path},
                thinking=False if args.no_thinking else None,
            ):
                events.append(event)
                if event.get("type") == "done":
                    final_text = event.get("content") or ""
                elif event.get("type") == "error":
                    error = event.get("message")
        except Exception as exc:  # noqa: BLE001
            error = f"{type(exc).__name__}: {exc}"

        preds = collect_items(events, final_text)
        gts = sample.ground_truth()
        report = evaluate_sample(preds, gts, iou_threshold=args.iou)
        space = evaluate_any_space(preds, gts, sample.width, sample.height, iou_threshold=args.iou)
        report.update({
            "name": sample.name,
            "image": sample.path,
            "elapsed_s": round(time.time() - started, 1),
            "error": error,
            "predictions": preds,
            "ground_truth": gts,
            "tool_calls": [e.get("name") for e in events if e.get("type") == "tool_call"],
            "reasoning_chars": sum(len(e.get("text", "")) for e in events if e.get("type") == "reasoning"),
            "coord_space": space["space"],
            "hits_best": space["hits_best"],
            "detection_rate_best": space["detection_rate_best"],
            "mean_iou_best": space["mean_iou_best"],
            "final_text": final_text,
        })
        reports.append(report)

        if args.annotate and preds:
            try:
                ann_dir.mkdir(parents=True, exist_ok=True)
                _img, p = render_annotations(sample.path, preds, output_dir=ann_dir, stem=f"{sample.name}_pred")
                report["annotated"] = str(p)
            except Exception as exc:  # noqa: BLE001
                report["annotated_error"] = str(exc)

        status = (f"ERROR {error}" if error else
                  f"hit {report['hits']}/{report['gt_count']} IoU {report['mean_iou']} "
                  f"[coord={report['coord_space']}, best {report['hits_best']}/{report['gt_count']}]")
        print(f" {status} ({report['elapsed_s']}s)")

        with report_path.open("w", encoding="utf-8") as fp:
            json.dump({"summary": aggregate(reports), "reports": reports}, fp, ensure_ascii=False, indent=2)

    summary = aggregate(reports)
    print("\n================ 评测汇总 ================")
    print(f"图片数        : {summary['images']}")
    print(f"真值目标数    : {summary['total_gt']}")
    print(f"预测目标数    : {summary['total_pred']}")
    print(f"命中(IoU≥{args.iou}) : {summary['total_hits']}  -> 检出率 {summary['detection_rate']:.1%}")
    print(f"精确率        : {summary['precision']:.1%}")
    print(f"平均 IoU      : {summary['mean_iou']:.3f}")
    print(f"颜色准确率    : {summary['color_accuracy']:.1%}")
    print(f"形状准确率    : {summary['shape_accuracy']:.1%}")
    print(f"标签(颜色+形状)准确率: {summary['label_accuracy']:.1%}")
    print(f"--- 坐标约定诊断 ---")
    print(f"输出像素坐标的图片数  : {summary['pixel_space_images']}/{summary['images']}")
    print(f"换算后检出率(上限)    : {summary['detection_rate_best_space']:.1%} "
          f"({summary['total_hits_best_space']}/{summary['total_gt']})")
    print(f"\n报告已保存: {report_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
