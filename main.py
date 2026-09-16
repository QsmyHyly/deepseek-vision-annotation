"""命令行入口。

用法：
    python main.py web                        # 启动网页对比查看服务
    python main.py detect --image <url|path>  # 命令行流式识别并保存标注图
    python main.py detect --image a.png --no-thinking   # 关闭思考模式（更快、更省 token）
    python main.py tools                      # 查看已注册工具
    python main.py demo                       # 用内置示例跑一遍（流式 + 工具执行）
"""

from __future__ import annotations

import argparse
import sys

from objloc.agent import build_messages, extract_items, run_agent
from objloc.config import get_settings
from objloc.tools import build_default_registry
from objloc.visualizer import image_to_data_url_from_source, render_annotations


def _print_stream(events, *, show_reasoning: bool = True):
    """把 agent 事件流打印到终端。"""
    final_text = ""
    all_events = []
    for event in events:
        all_events.append(event)
        etype = event.get("type")
        if etype == "reasoning" and show_reasoning:
            sys.stdout.write(f"\033[90m{event['text']}\033[0m")
        elif etype == "content":
            sys.stdout.write(event["text"])
        elif etype == "tool_call":
            print(f"\n\033[36m[tool_call]\033[0m {event['name']}({event['arguments']})")
        elif etype == "tool_result":
            flag = "ok" if event["ok"] else "fail"
            print(f"\033[36m[tool_result {flag}]\033[0m {event['content'][:300]}")
        elif etype == "done":
            final_text = event.get("content") or ""
        sys.stdout.flush()
    print()
    return final_text, all_events


def cmd_detect(args) -> int:
    settings = get_settings()
    print(f"[provider] {settings.resolved_provider()}  [model] {settings.model}")
    image = args.image
    # 本地路径也转成 data URL，保证多模态请求可携带
    try:
        image_arg = image_to_data_url_from_source(image)
    except Exception as exc:  # noqa: BLE001
        print(f"图片加载失败：{exc}")
        return 1

    thinking = None if getattr(args, "thinking", None) is None else args.thinking
    if thinking is not None:
        print(f"[thinking] {'开启' if thinking else '关闭'}")

    messages = build_messages(args.prompt, image=image_arg)
    final_text, _events = _print_stream(
        run_agent(
            messages,
            tool_context={"source": image},
            thinking=thinking,
            reasoning_effort=getattr(args, "reasoning_effort", None),
        )
    )
    items = extract_items(final_text)

    if items:
        # 走默认目录（runs/scratch/）：CLI 的标注图没有历史记录归属，不该污染 runs/ 根目录
        _img, path = render_annotations(image, items)
        print(f"[annotated] {path}")
    else:
        print("[warn] 未从模型输出解析到坐标")
    return 0


def cmd_demo(args) -> int:
    prompt = (
        "图中示例坐标如下：\n"
        '[{"bbox_2d": [0.18, 0.24, 0.43, 0.62], "label": "目标 A"}, '
        '{"point_2d": [0.64, 0.2], "label": "点位"}]\n'
        "请调用工具解析并整理这些坐标。"
    )
    messages = build_messages(prompt)
    final_text, _events = _print_stream(run_agent(messages))
    print("final:", final_text)
    return 0


def cmd_tools(args) -> int:
    registry = build_default_registry()
    for item in registry.describe():
        print(f"- {item['name']}: {item['description']}")
    return 0


def cmd_web(args) -> int:
    from objloc.web.app import main as web_main

    web_main()
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="DeepSeek 物体定位演示软件")
    sub = parser.add_subparsers(dest="command", required=True)

    p_web = sub.add_parser("web", help="启动网页对比查看服务")
    p_web.set_defaults(func=cmd_web)

    p_detect = sub.add_parser("detect", help="命令行流式识别")
    p_detect.add_argument("--image", required=True, help="图片 URL 或本地路径")
    p_detect.add_argument("--prompt", default="识别图中的主要目标，输出坐标与中文名称。")
    # 思考模式：默认 None = 用 THINKING 环境变量的默认值（开启）
    p_detect.add_argument("--thinking", dest="thinking", action="store_true", default=None,
                          help="强制开启思考模式（等价 THINKING=1）")
    p_detect.add_argument("--no-thinking", dest="thinking", action="store_false",
                          help="关闭思考模式，模型直接输出正文（更快、更省 token）")
    p_detect.add_argument("--reasoning-effort", default=None,
                          choices=["low", "medium", "high", "xhigh", "max"],
                          help="思考强度，仅在思考开启时生效")
    p_detect.set_defaults(func=cmd_detect)

    p_tools = sub.add_parser("tools", help="查看已注册工具")
    p_tools.set_defaults(func=cmd_tools)

    p_demo = sub.add_parser("demo", help="内置示例（流式 + 工具执行）")
    p_demo.set_defaults(func=cmd_demo)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
