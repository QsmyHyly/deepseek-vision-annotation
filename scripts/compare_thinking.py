# -*- coding: utf-8 -*-
"""思考模式 A/B 对照：同一批内置测试图，分别开启/关闭思考模式跑一遍。

用途：回答"关掉思考到底有多大差别"——用**程序判定**的检出率、平均 IoU、
标签准确率，加上耗时与 token 消耗，而不是靠肉眼看输出好不好。

用法：
    python scripts\compare_thinking.py                       # 默认 3 张可评测测试图
    python scripts\compare_thinking.py --samples bench_01,marker_900
    python scripts\compare_thinking.py --repeat 2            # 每张图每种模式跑几遍（取均值）
    python scripts\compare_thinking.py --out runs\thinking_ab

坐标约定见 AGENTS.md#4.3；思考模式协议见 docs/DeepSeek-Thinking-Mode.md。
"""

from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
import time
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from objloc import samples as samples_mod  # noqa: E402
from objloc.agent import build_messages, collect_items, run_agent  # noqa: E402
from objloc.config import PROJECT_ROOT, get_settings  # noqa: E402
from objloc.parsing import normalize_to_unit  # noqa: E402
from objloc.visualizer import image_to_data_url_from_source  # noqa: E402

# 默认选 3 张自带真值、且难度递进的图（见 objloc/samples.py 的目录）
DEFAULT_SAMPLES = ["bench_01", "bench_02", "bench_03"]


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="思考模式 A/B 对照（开启 vs 关闭）")
    p.add_argument("--samples", default=",".join(DEFAULT_SAMPLES),
                   help="逗号分隔的内置测试图 id（可用 python -c \"from objloc import samples; ...\" 查看全部）")
    p.add_argument("--repeat", type=int, default=1, help="每张图每种模式重复几次（取均值）")
    p.add_argument("--provider", default=None, help="auto/deepseek/mock，覆盖 LLM_PROVIDER")
    p.add_argument("--no-tools", action="store_true", help="不启用工具执行")
    p.add_argument("--out", default=str(PROJECT_ROOT / "runs" / "thinking_ab"))
    return p.parse_args()


def run_once(sample, *, thinking: bool, use_tools: bool) -> dict:
    """跑一次识别，返回耗时 / 输出规模 / 准确率。"""
    item, path = samples_mod.ensure(sample)
    image_url = image_to_data_url_from_source(path)
    messages = build_messages(item.prompt, image=image_url)

    started = time.time()
    events: list[dict] = []
    error = None
    try:
        for event in run_agent(
            messages,
            use_tools=use_tools,
            tool_context={"source": path},
            thinking=thinking,
        ):
            events.append(event)
    except Exception as exc:  # noqa: BLE001 - 单次失败不该中断整轮对照
        error = f"{type(exc).__name__}: {exc}"
    elapsed = time.time() - started

    final_text = next(
        (e.get("content") or "" for e in reversed(events) if e.get("type") == "done"), ""
    )
    reasoning = "".join(e["text"] for e in events if e.get("type") == "reasoning")
    items = collect_items(events, final_text)
    items, converted = normalize_to_unit(items)

    accuracy = samples_mod.accuracy(sample, items, item.width, item.height) if item.evaluable else None
    return {
        "thinking": thinking,
        "sample": sample,
        "elapsed_s": round(elapsed, 1),
        "reasoning_chars": len(reasoning),
        "content_chars": len(final_text),
        "items": len(items),
        "legacy_converted": converted,
        "error": error,
        "accuracy": accuracy,
    }


def _fmt(acc: dict | None) -> str:
    if not acc:
        return "无真值"
    return (f"检出 {acc['hits']}/{acc['gt_count']}  IoU {acc['mean_iou']:.3f}  "
            f"标签 {acc['label_accuracy'] * 100:.0f}%  空间 {acc['coord_space']}")


def main() -> int:
    args = parse_args()
    if args.provider:
        os.environ["LLM_PROVIDER"] = args.provider

    ids = [s.strip() for s in args.samples.split(",") if s.strip()]
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    settings = get_settings()
    print(f"provider={settings.resolved_provider()} model={settings.model} "
          f"默认思考={'开' if settings.thinking else '关'} 工具={'关' if args.no_tools else '开'}")

    rows: list[dict] = []
    for sample_id in ids:
        for thinking in (True, False):
            for _ in range(max(1, args.repeat)):
                row = run_once(sample_id, thinking=thinking, use_tools=not args.no_tools)
                rows.append(row)
                tag = "思考开" if thinking else "思考关"
                print(f"  {sample_id:<12} {tag}  {row['elapsed_s']:>6.1f}s  "
                      f"思考{row['reasoning_chars']:>5}字符  目标{row['items']:>2}  "
                      f"{_fmt(row['accuracy'])}" + (f"  [ERROR {row['error']}]" if row["error"] else ""))

    # 汇总：两种模式分别取均值（准确率按可评测图聚合，避免被无真值图稀释）
    summary = {}
    for thinking in (True, False):
        group = [r for r in rows if r["thinking"] == thinking]
        accs = [r["accuracy"] for r in group if r["accuracy"]]
        summary["on" if thinking else "off"] = {
            "runs": len(group),
            "mean_elapsed_s": round(statistics.mean(r["elapsed_s"] for r in group), 1),
            "mean_reasoning_chars": round(statistics.mean(r["reasoning_chars"] for r in group)),
            "evaluable_images": len(accs),
            "detection_rate": round(statistics.mean(a["detection_rate"] for a in accs), 3) if accs else None,
            "mean_iou": round(statistics.mean(a["mean_iou"] for a in accs), 3) if accs else None,
            "label_accuracy": round(statistics.mean(a["label_accuracy"] for a in accs), 3) if accs else None,
        }

    report = {"model": settings.model, "provider": settings.resolved_provider(),
              "samples": ids, "repeat": args.repeat, "rows": rows, "summary": summary}
    report_path = out_dir / "report.json"
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    print("\n=== 汇总（均值） ===")
    print(f"{'模式':<8}{'耗时':>9}{'思考字符':>10}{'检出率':>9}{'平均IoU':>10}{'标签准确率':>11}")
    for key, label in (("on", "思考开"), ("off", "思考关")):
        s = summary[key]
        det = "—" if s["detection_rate"] is None else f"{s['detection_rate'] * 100:.0f}%"
        iou = "—" if s["mean_iou"] is None else f"{s['mean_iou']:.3f}"
        lab = "—" if s["label_accuracy"] is None else f"{s['label_accuracy'] * 100:.0f}%"
        print(f"{label:<8}{s['mean_elapsed_s']:>8.1f}s{s['mean_reasoning_chars']:>10}{det:>9}{iou:>10}{lab:>11}")
    print(f"\n报告：{report_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
