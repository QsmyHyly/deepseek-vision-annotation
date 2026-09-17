# -*- coding: utf-8 -*-
"""识别与标注路由 —— 本项目的两条主链路。

职责
----
- POST /api/annotate：本地标注（给定坐标直接画，离线可用）；
- POST /api/detect：SSE 流式识别（流式输出 + 工具执行 + 标注图 + 历史落盘）；
- 两者的公共件：_sample_accuracy（内置测试图按真值自动打分）、
  _sse（SSE 编码）、_pick_int / _pick_detail（按次覆盖参数的容错解析）。

边界
----
不负责图片从哪来（images.py），也不负责历史记录的磁盘细节（storage 包）。
本模块是"把 agent、storage、visualizer 串成一轮请求"的地方 ——
SSE 事件类型与字段是对前端的契约，改动前先看 AGENTS.md#4.1-流式事件协议。

旧文件对应关系（等价重构）
--------------------------
本模块 = 拆分前 objloc/web/app.py 的「标注」「流式识别」两节，逐行搬移；
装饰器由 @app.* 机械改成 @router.*，函数体（含 SSE 事件字段）未改一个字。

@doc docs/DeepSeek-Thinking-Mode.md#本项目中的落地位置
（该文档解决"thinking 参数怎么传、effort 怎么映射"的问题。）
"""

from __future__ import annotations

import json
import time
import uuid
from typing import Any, Iterator

from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse

from objloc import samples as samples_mod
from objloc import storage
from objloc.agent import build_messages, collect_items, run_agent
from objloc.config import IMAGE_DETAILS, get_settings
from objloc.parsing import (
    LEGACY_SCALE_NOTICE,
    check_coordinate_range,
    format_coordinate_warnings,
    normalize_to_unit,
)
from objloc.visualizer import (
    coerce_items,
    image_to_data_url_from_source,
    render_annotations,
    summarize,
)

from .images import _source_meta
from .state import IMAGES, REGISTRY

router = APIRouter()

# --------------------------------------------------------------------------- #
# 标注
# --------------------------------------------------------------------------- #
def _sample_accuracy(record: dict, items: list[dict]) -> dict | None:
    """若当前图片来自内置测试图且带真值，就用真值给这次打标打分（不靠人眼）。"""
    sample_id = (record or {}).get("sample_id")
    if not sample_id:
        return None
    try:
        return samples_mod.accuracy(sample_id, items, record["width"], record["height"])
    except Exception as exc:  # noqa: BLE001 - 打分失败不该影响主流程
        print(f"[web] accuracy failed for {sample_id}: {exc}")
        return None


@router.post("/api/annotate")
async def annotate_api(payload: dict) -> dict:
    """本地标注：不需要调用模型，直接按传入坐标绘制。"""
    image_id = (payload or {}).get("image_id")
    record = IMAGES.get(image_id)
    if not record:
        raise HTTPException(status_code=404, detail="图片不存在，请先上传")

    items = coerce_items(payload.get("items") or payload.get("text") or [])
    if not items:
        raise HTTPException(status_code=400, detail="没有可绘制的坐标")

    # 旧刻度兜底：整批坐标若都是 0~1000，先在画图前换算成 0.0~1.0
    items, converted = normalize_to_unit(items)

    # 标注图**直接画进历史记录的目录**（history/<run_id>/annotated.png），
    # 不再往 runs/ 根目录丢随机名 PNG；run_id 先取好，路径拼接由 storage 负责。
    run_id = storage.new_run_id()
    _img, path = render_annotations(record["path"], items,
                                    output_dir=storage.run_dir(run_id), stem="annotated")

    # 告警原样留档（不合并、不改写），与返回给前端的口径一致
    warning_msgs: list[str] = []
    notice = LEGACY_SCALE_NOTICE if converted else None
    if converted:
        warning_msgs.append(LEGACY_SCALE_NOTICE)
    warnings = check_coordinate_range(items)
    if warnings:
        warning_msgs.append(format_coordinate_warnings(warnings))

    result = {
        "annotated_url": f"/api/history/{run_id}/annotated?t={uuid.uuid4().hex[:6]}",
        "summary": summarize(items),
        "items": items,
        "run_id": run_id,
    }
    if converted:
        result["notice"] = LEGACY_SCALE_NOTICE
    if warnings:
        result["coord_warnings"] = warnings
        result["warning"] = format_coordinate_warnings(warnings)
    accuracy = _sample_accuracy(record, items)
    if accuracy:
        result["accuracy"] = accuracy

    # 落一条历史记录。本地标注不调模型：duration_ms / model / thinking 一律 null
    # —— 记 0 或默认值等于编造证据（见 AGENTS.md#4.9 的 schema 注释）。
    storage.record_run(
        run_id=run_id,
        kind="annotate",
        source=_source_meta(record),
        items=items,
        prompt=(payload.get("prompt") or "").strip() or None,
        thinking=None,
        reasoning_effort=None,
        model=None,
        duration_ms=None,
        accuracy=accuracy,
        warnings=warning_msgs,
        notice=notice,
        annotated=path,
    )
    return result


# --------------------------------------------------------------------------- #
# 流式识别
# --------------------------------------------------------------------------- #
def _sse(event: dict) -> str:
    return f"data: {json.dumps(event, ensure_ascii=False)}\n\n"


def _pick_int(raw: Any, fallback: int, low: int, high: int) -> int:
    """取一个可选的整数按次覆盖值；缺省 / 解析不了 / 越界一律回落到 fallback。

    这里刻意不抛 400：轮数是个"调优旋钮"而不是关键输入，为它打断整轮识别不划算 ——
    真正需要严格校验的是持久化那一步（objloc/userprefs.py 会拒绝非法值）。
    """
    if raw is None or isinstance(raw, bool):
        return fallback
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return fallback
    return value if low <= value <= high else fallback


def _pick_detail(raw: Any, fallback: str) -> str:
    """取一个可选的图片精度覆盖值；缺省 / 不认识的值一律回落到 fallback。

    与 _pick_int 同样的取舍：这是"调优旋钮"，写错一个值不该让整轮识别挂掉，
    真正需要严格校验的是持久化那一步（PATCH /api/settings 会拒绝非法值）。
    """
    value = str(raw or "").strip().lower()
    return value if value in IMAGE_DETAILS else fallback


@router.post("/api/detect")
async def detect(payload: dict):
    """SSE 流式识别：边流式输出边执行工具，结束时回传标注图。

    请求体字段：image_id / prompt / use_tools / max_tool_rounds / model /
    thinking / reasoning_effort / image_detail。
    thinking 与 use_tools 省略时沿用**当前生效**的默认值（config.local.json > 环境变量 > 内置，
    见 objloc/userprefs.py §4.10），显式传 true/false 才按次覆盖；
    max_tool_rounds 越界或缺省时同样回落到 Settings.max_tool_rounds；
    不认识的 image_detail 同样回落（见 _pick_detail）。

    @doc docs/DeepSeek-Thinking-Mode.md#本项目中的落地位置
    """
    payload = payload or {}
    record = IMAGES.get(payload.get("image_id"))
    if not record:
        raise HTTPException(status_code=404, detail="图片不存在，请先上传")

    # 本轮的配置快照：整个请求只取一次，中途用户改配置也不会让同一轮识别前后用两套设置。
    # get_settings() 会在 config.local.json 变动时自动重建单例，所以这里拿到的一定是最新值。
    settings = get_settings()

    prompt = (payload.get("prompt") or "").strip() or "请识别图中的主要目标，并按约定输出坐标。"
    # 工具开关：缺省（None）时沿用 Settings.use_tools（config.local.json / 环境变量），
    # 显式传 true/false 才按次覆盖 —— 与 thinking 同一套三态语义，
    # 别写成 `bool(payload.get("use_tools", True))`，那会把「没传」变成「强制开启」。
    use_tools_raw = payload.get("use_tools", None)
    use_tools = settings.use_tools if use_tools_raw is None else bool(use_tools_raw)
    max_rounds = _pick_int(payload.get("max_tool_rounds"), settings.max_tool_rounds, 1, 64)
    # 按次覆盖模型的逃生口，正常流程用不到：网页从不发送这个字段。
    # ⚠️ 本项目是视觉定位，只有 deepseek-flash（= DeepSeek-V4.1-Flash）支持图像理解，
    # 覆盖成 deepseek-v4-pro 会让请求直接失败。
    # @doc docs/DeepSeek-Models-and-Pricing.md#本项目为什么只用-deepseek-flash
    model_id = (payload.get("model") or "").strip() or None

    # 思考模式：字段缺省（None）时沿用 Settings.thinking，显式传 true/false 则按次覆盖。
    # 注意不能用 bool(payload.get("thinking", True))——那会把「没传」变成「强制开启」。
    thinking_raw = payload.get("thinking", None)
    thinking = None if thinking_raw is None else bool(thinking_raw)
    effort = (payload.get("reasoning_effort") or "").strip() or None
    # 图片输入精度（image_url 的 detail）：只认 config.IMAGE_DETAILS 里的四项，
    # 缺省/空串/写错都回落到 Settings.image_detail，理由同 _pick_int。
    detail = _pick_detail(payload.get("image_detail"), settings.image_detail)

    # 传给多模态模型的图片使用 data URL，避免外链不可达
    image_data_url = image_to_data_url_from_source(record["path"])

    # 整轮耗时：从请求进入算起（含模型流式输出与工具执行），落进历史记录的 duration_ms。
    # 计时放在这里而不是生成器内部，是因为 StreamingResponse 的生成器何时被驱动由 ASGI 决定。
    started = time.perf_counter()

    def event_stream() -> Iterator[str]:
        events: list[dict] = []
        messages = build_messages(prompt, image=image_data_url, image_detail=detail)
        try:
            client = None
            if model_id:
                from objloc.providers import OpenAICompatClient

                client = OpenAICompatClient(
                    type(settings)(**{**settings.__dict__, "model": model_id})
                )
            for event in run_agent(
                messages,
                client=client,
                registry=REGISTRY,
                max_rounds=max_rounds,
                use_tools=use_tools,
                tool_context={"source": record["path"]},
                thinking=thinking,
                reasoning_effort=effort,
            ):
                events.append(event)
                yield _sse(event)
        except Exception as exc:  # noqa: BLE001
            err = {"type": "error", "message": f"服务端异常：{exc}"}
            events.append(err)
            yield _sse(err)

        duration_ms = int((time.perf_counter() - started) * 1000)

        final_text = ""
        for event in reversed(events):
            if event.get("type") == "done":
                final_text = event.get("content") or ""
                break

        items = collect_items(events, final_text)
        if items:
            # 告警原样留档（旧刻度换算 / 坐标越界），与推给前端的口径一致
            warning_msgs: list[str] = []
            notice = None
            # 旧刻度兜底：整批坐标若都是 0~1000，先换算成 0.0~1.0 再画
            items, converted = normalize_to_unit(items)
            if converted:
                notice = LEGACY_SCALE_NOTICE
                warning_msgs.append(LEGACY_SCALE_NOTICE)
                yield _sse({"type": "warning", "message": LEGACY_SCALE_NOTICE})
            # 坐标越界检查：DeepSeek 会等比缩放图片，模型偶尔会误输出像素坐标
            coord_warnings = check_coordinate_range(items)
            if coord_warnings:
                warning_msgs.append(format_coordinate_warnings(coord_warnings))
                yield _sse({
                    "type": "warning",
                    "message": format_coordinate_warnings(coord_warnings),
                    "coord_warnings": coord_warnings,
                })
            try:
                # 标注图直接画进历史记录目录：history/<run_id>/annotated.png
                run_id = storage.new_run_id()
                _img, path = render_annotations(
                    record["path"], items, output_dir=storage.run_dir(run_id), stem="annotated"
                )
                annotated_event = {
                    "type": "annotated",
                    "url": f"/api/history/{run_id}/annotated?t={uuid.uuid4().hex[:6]}",
                    "summary": summarize(items),
                    "items": items,
                    "run_id": run_id,
                }
                # 内置测试图自带真值 -> 直接用程序算准确率，不靠人眼判断
                accuracy = _sample_accuracy(record, items)
                if accuracy:
                    annotated_event["accuracy"] = accuracy
                # 落历史记录：坐标 / 标签 / 提示词 / 准确率从此不再只活在内存里。
                # 只在真的画出了标注图时才记 —— 一条没有 annotated.png 的记录在历史面板里
                # 只会是张裂图，而「没解析到坐标」这件事前端已经用 warning 说清了。
                try:
                    storage.record_run(
                        run_id=run_id,
                        kind="detect",
                        source=_source_meta(record),
                        items=items,
                        prompt=prompt,
                        # thinking 缺省时记「实际生效」的那个值，而不是 None（否则看不出这轮开没开）
                        thinking=settings.thinking if thinking is None else bool(thinking),
                        reasoning_effort=(effort or settings.reasoning_effort or None),
                        model=model_id or settings.model,
                        duration_ms=duration_ms,
                        accuracy=accuracy,
                        warnings=warning_msgs,
                        notice=notice,
                        annotated=path,
                    )
                except Exception as exc:  # noqa: BLE001 - 落记录失败不该打断整条 SSE
                    yield _sse({"type": "warning", "message": f"历史记录写入失败：{exc}"})
                yield _sse(annotated_event)
            except Exception as exc:  # noqa: BLE001
                yield _sse({"type": "error", "message": f"标注失败：{exc}"})
        else:
            yield _sse({"type": "warning", "message": "模型输出中没有解析到坐标，未生成标注图。"})

        yield _sse({"type": "eof"})

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
