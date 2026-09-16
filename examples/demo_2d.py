"""物体定位演示脚本（兼容层）。

原脚本把「调用模型 / 解析坐标 / 绘制标注」都揉在一个文件里，且硬编码了
NotoSansCJK-Regular.ttc 字体（在 Windows 上会直接报错）。新架构拆分为：

- providers / deepseek_client : 模型调用（流式 + 工具）
- point_parser                : 坐标解析
- visualizer                  : 标注绘制（跨平台字体）
- agent                       : 流式 + 工具执行循环

本文件仅保留原函数名作为薄封装，方便旧代码继续调用。

用法：
    python deepseek41-vl-2d.py                 # 内置示例坐标
    python deepseek41-vl-2d.py --image <url|path>
"""

from __future__ import annotations

import argparse

from objloc.agent import build_messages, extract_items, run_agent
from objloc.config import RUNS_DIR
from objloc.visualizer import annotate, image_to_data_url_from_source, load_image, render_annotations

SAMPLE_URL = "https://help-static-aliyun-doc.aliyuncs.com/file-manage-files/zh-CN/20251031/dhsvgy/img_2.png"
SAMPLE_RESPONSE = '[{"bbox_2d": [0.18, 0.24, 0.43, 0.62], "label": "示例目标"}]'


def parse_json(json_output: str) -> str:
    """移除 Markdown 代码块标记（保留旧函数名）。"""
    lines = json_output.splitlines()
    for i, line in enumerate(lines):
        if line.strip().startswith("```"):
            json_output = "\n".join(lines[i + 1:])
            json_output = json_output.split("```")[0]
            break
    return json_output


def plot_bounding_boxes(img_path, bounding_boxes):
    """在图像上绘制边界框并标注名称（保留旧签名）。"""
    image = annotate(img_path, bounding_boxes)
    image.show()
    return image


def plot_points(im, text):
    """在图像上绘制点标注（保留旧签名）。"""
    image = annotate(im, text, box_width=2, point_radius=4, font_size=14)
    image.show()
    return image


def plot_points_json(im, text):
    """按 point_2d JSON 列表绘制（保留旧签名）。"""
    return plot_points(im, text)


def run_object_detection(img_url, model_response, *, save: bool = True):
    """加载图片、绘制标注。"""
    image = load_image(img_url)
    annotated, path = render_annotations(
        image, model_response, output_dir=RUNS_DIR, stem="legacy_annotated"
    )
    if save:
        print(f"[saved] {path}")
    annotated.show()
    return annotated


def main() -> int:
    parser = argparse.ArgumentParser(description="物体定位演示（兼容层）")
    parser.add_argument("--image", default=SAMPLE_URL, help="图片 URL 或本地路径")
    parser.add_argument("--prompt", default="识别图中的主要目标，输出坐标与中文名称。")
    parser.add_argument("--offline", action="store_true", help="不调用模型，使用内置示例坐标")
    args = parser.parse_args()

    if args.offline:
        print("[offline] 使用内置示例坐标")
        run_object_detection(args.image, SAMPLE_RESPONSE)
        return 0

    print(f"[model] 调用模型：{args.image}")
    try:
        image_arg = image_to_data_url_from_source(args.image)
        messages = build_messages(args.prompt, image=image_arg)
        final_text = ""
        for event in run_agent(messages, tool_context={"source": args.image}):
            if event["type"] == "content":
                print(event["text"], end="", flush=True)
            elif event["type"] == "tool_call":
                print(f"\n[tool_call] {event['name']}({event['arguments']})")
            elif event["type"] == "tool_result":
                print(f"[tool_result] {event['content'][:200]}")
            elif event["type"] == "done":
                final_text = event.get("content") or ""
        print()
        items = extract_items(final_text)
        if items:
            run_object_detection(args.image, items)
        else:
            print("[warn] 未解析到坐标")
    except Exception as exc:
        print(f"[error] 模型调用失败：{exc}")
        print("提示：可加 --offline 使用内置示例坐标演示绘制流程。")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
