"""Web 服务：流式识别 + 打标前后对比页面。

接口一览：
    GET  /                     对比页面（web/static/index.html）
    GET  /api/health           运行状态（provider / 是否有 Key / 工具列表）
    GET  /api/tools            已注册工具清单
    POST /api/upload           上传图片或提交图片 URL -> 返回 image_id
    POST /api/detect           SSE 流式识别（流式输出 + 工具执行），结束前给出标注图
                               请求体可带 thinking: true/false 按次开关思考模式
    POST /api/annotate         本地标注：给定坐标 JSON -> 返回标注图（离线可用）
    POST /api/sample           载入内置示例图与示例坐标，便于零配置体验
    GET  /api/samples          内置测试图目录（按测试目的分组，见 objloc/samples.py）
    GET  /api/samples/{id}/image  测试图原图（缺失时现场生成）
    POST /api/samples/{id}/load   把测试图设为当前图片，并回传真值（可自动算准确率）
    GET  /api/file/{image_id}  原图
    GET  /api/result/{name}    标注结果图

坐标约定见 AGENTS.md#4.3：一律 0.0~1.0 的相对比例；若模型给出 0~1000 旧刻度，
这里会自动换算并向前端推送 warning（见 objloc/parsing.py: normalize_to_unit）。
"""

from __future__ import annotations

import json
import shutil
import uuid
from pathlib import Path
from typing import Any, Iterator

from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from objloc import samples as samples_mod
from objloc.agent import build_messages, collect_items, run_agent
from objloc.config import RUNS_DIR, UPLOAD_DIR, WEB_STATIC_DIR, get_settings
from objloc.parsing import (
    LEGACY_SCALE_NOTICE,
    check_coordinate_range,
    format_coordinate_warnings,
    normalize_to_unit,
)
from objloc.tools import build_default_registry
from objloc.visualizer import (
    coerce_items,
    image_to_data_url_from_source,
    load_image,
    render_annotations,
    resolve_font,
    summarize,
)

app = FastAPI(title="DeepSeek 物体定位演示", version="2.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# 内存中的图片登记表（进程级，演示够用）
IMAGES: dict[str, dict[str, Any]] = {}
REGISTRY = build_default_registry()

ALLOWED_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp", ".bmp", ".gif"}


# --------------------------------------------------------------------------- #
# 页面与静态资源
# --------------------------------------------------------------------------- #
@app.get("/")
def index() -> FileResponse:
    return FileResponse(WEB_STATIC_DIR / "index.html")


@app.get("/api/health")
def health() -> dict:
    settings = get_settings()
    return {
        "ok": True,
        "provider": settings.resolved_provider(),
        "has_api_key": settings.has_api_key,
        "model": settings.model,
        "base_url": settings.base_url,
        "tools": REGISTRY.names(),
        "max_tool_rounds": settings.max_tool_rounds,
        # 思考模式的默认值（网页开关的初始状态）；按次覆盖走 /api/detect 的 thinking 字段
        "thinking": settings.thinking,
        "reasoning_effort": settings.reasoning_effort or "server-default",
    }


@app.get("/api/tools")
def tools() -> dict:
    return {"tools": REGISTRY.describe()}


# --------------------------------------------------------------------------- #
# 内置测试图（目录见 objloc/samples.py）
# --------------------------------------------------------------------------- #
@app.get("/api/samples")
def list_samples() -> dict:
    """返回按测试目的分组的内置测试图目录（不含真值，避免提前泄露答案）。"""
    groups = samples_mod.list_catalog()
    return {
        "dir": str(samples_mod.SAMPLES_DIR),
        "groups": groups,
        "total": sum(len(g["items"]) for g in groups),
    }


@app.get("/api/samples/{sample_id}/image")
def sample_image(sample_id: str) -> FileResponse:
    """测试图原图；首次访问时现场生成（大图需要一两秒）。"""
    try:
        _item, path = samples_mod.ensure(sample_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return FileResponse(path, media_type="image/png")


@app.post("/api/samples/{sample_id}/load")
async def load_sample(sample_id: str) -> dict:
    """把内置测试图设为当前图片，并回传它的真值与建议提示词。"""
    try:
        item, path = samples_mod.ensure(sample_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    record = _register_image(path=path, filename=item.filename, sample_id=item.id)
    return {
        **record,
        "sample": {
            "id": item.id,
            "title": item.title,
            "group": item.group,
            "purpose": item.purpose,
            "evaluable": item.evaluable,
        },
        "prompt": item.prompt,
        # 真值：0.0~1.0 相对比例，可直接填进手动标注框，也可用于结果复核
        "ground_truth": samples_mod.as_items(samples_mod.ground_truth(item.id)),
    }


# --------------------------------------------------------------------------- #
# 图片登记
# --------------------------------------------------------------------------- #
def _register_image(*, path: Path | None = None, source: str | None = None,
                    filename: str | None = None, sample_id: str | None = None) -> dict:
    image_id = uuid.uuid4().hex[:12]
    local_path: Path

    if path is not None:
        local_path = path
    elif source:
        try:
            img = load_image(source)
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(status_code=400, detail=f"无法加载图片：{exc}") from exc
        local_path = UPLOAD_DIR / f"{image_id}.png"
        img.save(local_path, format="PNG")
    else:
        raise HTTPException(status_code=400, detail="缺少图片")

    img = load_image(local_path)
    record = {
        "id": image_id,
        "path": str(local_path),
        "filename": filename or local_path.name,
        "width": img.size[0],
        "height": img.size[1],
        "url": f"/api/file/{image_id}",
        # 来自内置测试图时记下 id，标注完成后可以用真值自动打分
        "sample_id": sample_id,
    }
    IMAGES[image_id] = record
    return record


@app.post("/api/upload")
async def upload(file: UploadFile | None = File(default=None)) -> dict:
    """接收浏览器上传的图片。"""
    if file is None:
        raise HTTPException(status_code=400, detail="请提供 file 字段")

    suffix = Path(file.filename or "").suffix.lower()
    if suffix not in ALLOWED_SUFFIXES:
        raise HTTPException(status_code=400, detail=f"不支持的图片格式：{suffix or '未知'}")

    image_id = uuid.uuid4().hex[:12]
    dest = UPLOAD_DIR / f"{image_id}{suffix}"
    with dest.open("wb") as fp:
        shutil.copyfileobj(file.file, fp)
    return _register_image(path=dest, filename=file.filename)


@app.post("/api/upload_url")
async def upload_url(payload: dict) -> dict:
    """通过图片 URL 登记。"""
    url = (payload or {}).get("url", "").strip()
    if not url:
        raise HTTPException(status_code=400, detail="缺少 url")
    return _register_image(source=url, filename=url.rsplit("/", 1)[-1])


@app.get("/api/file/{image_id}")
def get_file(image_id: str) -> FileResponse:
    record = IMAGES.get(image_id)
    if not record:
        raise HTTPException(status_code=404, detail="图片不存在")
    return FileResponse(record["path"])


@app.get("/api/result/{name}")
def get_result(name: str) -> FileResponse:
    # 防目录穿越
    safe = Path(name).name
    path = RUNS_DIR / safe
    if not path.exists():
        raise HTTPException(status_code=404, detail="结果不存在")
    return FileResponse(path, media_type="image/png")


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


@app.post("/api/annotate")
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

    _img, path = render_annotations(record["path"], items, stem=f"{image_id}_annotated")
    result = {
        "annotated_url": f"/api/result/{path.name}?t={uuid.uuid4().hex[:6]}",
        "summary": summarize(items),
        "items": items,
    }
    if converted:
        result["notice"] = LEGACY_SCALE_NOTICE
    warnings = check_coordinate_range(items)
    if warnings:
        result["coord_warnings"] = warnings
        result["warning"] = format_coordinate_warnings(warnings)
    accuracy = _sample_accuracy(record, items)
    if accuracy:
        result["accuracy"] = accuracy
    return result


# --------------------------------------------------------------------------- #
# 流式识别
# --------------------------------------------------------------------------- #
def _sse(event: dict) -> str:
    return f"data: {json.dumps(event, ensure_ascii=False)}\n\n"


@app.post("/api/detect")
async def detect(payload: dict):
    """SSE 流式识别：边流式输出边执行工具，结束时回传标注图。

    请求体字段：image_id / prompt / use_tools / model / thinking / reasoning_effort。
    thinking 省略时沿用服务端默认（THINKING 环境变量），显式传 false 即关闭思考模式。

    @doc docs/DeepSeek-Thinking-Mode.md#本项目中的落地位置
    """
    payload = payload or {}
    record = IMAGES.get(payload.get("image_id"))
    if not record:
        raise HTTPException(status_code=404, detail="图片不存在，请先上传")

    prompt = (payload.get("prompt") or "").strip() or "请识别图中的主要目标，并按约定输出坐标。"
    use_tools = bool(payload.get("use_tools", True))
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

    # 传给多模态模型的图片使用 data URL，避免外链不可达
    image_data_url = image_to_data_url_from_source(record["path"])

    def event_stream() -> Iterator[str]:
        events: list[dict] = []
        messages = build_messages(prompt, image=image_data_url)
        try:
            client = None
            if model_id:
                from objloc.providers import OpenAICompatClient
                from objloc.config import get_settings as _gs
                settings = _gs()
                client = OpenAICompatClient(
                    type(settings)(**{**settings.__dict__, "model": model_id})
                )
            for event in run_agent(
                messages,
                client=client,
                registry=REGISTRY,
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

        final_text = ""
        for event in reversed(events):
            if event.get("type") == "done":
                final_text = event.get("content") or ""
                break

        items = collect_items(events, final_text)
        if items:
            # 旧刻度兜底：整批坐标若都是 0~1000，先换算成 0.0~1.0 再画
            items, converted = normalize_to_unit(items)
            if converted:
                yield _sse({"type": "warning", "message": LEGACY_SCALE_NOTICE})
            # 坐标越界检查：DeepSeek 会等比缩放图片，模型偶尔会误输出像素坐标
            coord_warnings = check_coordinate_range(items)
            if coord_warnings:
                yield _sse({
                    "type": "warning",
                    "message": format_coordinate_warnings(coord_warnings),
                    "coord_warnings": coord_warnings,
                })
            try:
                _img, path = render_annotations(
                    record["path"], items, stem=f"{record['id']}_annotated"
                )
                annotated_event = {
                    "type": "annotated",
                    "url": f"/api/result/{path.name}?t={uuid.uuid4().hex[:6]}",
                    "summary": summarize(items),
                    "items": items,
                }
                # 内置测试图自带真值 -> 直接用程序算准确率，不靠人眼判断
                accuracy = _sample_accuracy(record, items)
                if accuracy:
                    annotated_event["accuracy"] = accuracy
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


# --------------------------------------------------------------------------- #
# 内置示例（离线体验）
# --------------------------------------------------------------------------- #
SAMPLE_ITEMS = [
    {"bbox_2d": [0.18, 0.24, 0.43, 0.62], "label": "示例目标 A"},
    {"bbox_2d": [0.52, 0.30, 0.76, 0.70], "label": "示例目标 B"},
    {"point_2d": [0.64, 0.20], "label": "示例点位"},
]


@app.post("/api/sample")
async def sample() -> dict:
    """生成一张内置示例图，用于零配置体验对比页面。"""
    from PIL import Image, ImageDraw

    width, height = 900, 720
    img = Image.new("RGB", (width, height), (245, 247, 250))
    draw = ImageDraw.Draw(img)
    for x in range(0, width, 45):
        draw.line([(x, 0), (x, height)], fill=(233, 237, 243), width=1)
    for y in range(0, height, 45):
        draw.line([(0, y), (width, y)], fill=(233, 237, 243), width=1)

    # 目标 A：蓝色圆角矩形（对应 bbox_2d [0.18,0.24,0.43,0.62] 的相对位置）
    draw.rounded_rectangle([162, 173, 387, 446], radius=24,
                           fill=(196, 219, 255), outline=(79, 140, 255), width=4)
    # 目标 B：橙色椭圆（对应 bbox_2d [0.52,0.30,0.76,0.70]）
    draw.ellipse([468, 216, 684, 504], fill=(255, 224, 178),
                 outline=(245, 158, 11), width=4)
    # 点位：绿色圆点（对应 point_2d [0.64,0.20]）
    draw.ellipse([576 - 14, 144 - 14, 576 + 14, 144 + 14],
                 fill=(190, 242, 200), outline=(34, 197, 94), width=4)

    font = resolve_font(28)
    small = resolve_font(20)
    draw.text((196, 190), "A", fill=(30, 64, 175), font=font)
    draw.text((556, 330), "B", fill=(180, 83, 9), font=font)
    draw.text((30, 24), "内置示例图 · 打标前", fill=(120, 134, 160), font=small)

    image_id = uuid.uuid4().hex[:12]
    path = UPLOAD_DIR / f"{image_id}.png"
    img.save(path, format="PNG")
    record = _register_image(path=path, filename="sample.png")
    return {"image": record, "sample_items": SAMPLE_ITEMS}


# 静态资源（放在最后，避免覆盖 API 路由）
app.mount("/static", StaticFiles(directory=str(WEB_STATIC_DIR)), name="static")


def main() -> None:
    """python -m web.app 或 python web/app.py 直接启动。"""
    import uvicorn

    settings = get_settings()
    print(f"[web] provider={settings.resolved_provider()} model={settings.model}")
    print(f"[web] http://{settings.host}:{settings.port}")
    uvicorn.run(app, host=settings.host, port=settings.port, log_level="info")


if __name__ == "__main__":
    main()
