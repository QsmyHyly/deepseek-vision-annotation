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
    GET  /api/file/{image_id}  原图（内存表里没有时按 source.image_id 从历史记录反查）
    GET  /api/result/{name}    标注结果图（旧路由，向后兼容：查 runs/scratch/ 与 runs/ 根）
    GET  /api/history          历史记录列表（按 created_at 倒序，不含 items）
    GET  /api/history/{run_id} 单条完整记录（= meta.json + url 字段）
    DELETE /api/history/{run_id}  删一条记录
    DELETE /api/history        清空全部记录
    POST /api/history/gc       清理孤儿源图（只在被显式调用时执行）
    GET  /api/history/{run_id}/annotated[?w=160]  标注图 / 现场缩略图
    GET  /api/history/{run_id}/source             源图

存储布局、meta.json schema 与上述历史接口的字段口径见
@doc AGENTS.md#4.9-上传--结果存储--历史记录
（该文档解决「打标记录存哪、存什么、怎么读回来」的问题；磁盘读写全在 objloc/storage.py，
本模块不自己拼 history/ 路径。）

坐标约定见 AGENTS.md#4.3：一律 0.0~1.0 的相对比例；若模型给出 0~1000 旧刻度，
这里会自动换算并向前端推送 warning（见 objloc/parsing.py: normalize_to_unit）。
"""

from __future__ import annotations

import json
import time
import uuid
from pathlib import Path
from typing import Any, Iterator

from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles

from objloc import config
from objloc import samples as samples_mod
from objloc import storage
from objloc.agent import build_messages, collect_items, run_agent
from objloc.config import WEB_STATIC_DIR, get_settings
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

# 内存中的图片登记表（进程级）。⚠️ 它**仍然**是内存字典：重启后当前图片就要重新上传。
# 打标记录本身已经持久化（runs/history/，见 objloc/storage.py），重启后可查、可看原图，
# 但「当前选中的图片」这个会话态没有落盘 —— 别把这两件事混为一谈。
IMAGES: dict[str, dict[str, Any]] = {}
REGISTRY = build_default_registry()

# 后缀白名单只是**第一道**检查（挡住明显不对的扩展名，省一次落盘）；
# 真正的内容校验是落盘后用 load_image 解码一次，见 _register_image。
ALLOWED_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp", ".bmp", ".gif"}

# 上传流的分块大小：边写边累计字节，才能在超限时立刻中断（而不是先写满磁盘再报错）
_UPLOAD_CHUNK = 1 << 20


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
def _stream_to_temp(upload: UploadFile, image_id: str) -> Path:
    """把上传流分块写进 uploads/ 下的临时文件，边写边累计字节。

    为什么要边写边数：一次性 read() 整个文件再判大小，等于先把几个 GB 落盘再拒绝。
    超限 / 空文件 / 写盘出错都会**先删掉半截临时文件**再抛错 ——
    以前「先按原后缀整文件落盘再校验」的做法会把残留文件留在磁盘上（见 AGENTS.md#4.9）。

    返回临时文件路径；调用方负责在解码/转存完成后删除它。
    """
    settings = get_settings()
    limit_bytes: int | None = None
    try:
        mb = int(settings.max_upload_mb)
        limit_bytes = mb * 1024 * 1024 if mb > 0 else None   # 0 = 不限
    except Exception:  # noqa: BLE001 - 配置异常不该让上传整个不可用，退回默认 20MB
        limit_bytes = 20 * 1024 * 1024

    tmp = config.UPLOAD_DIR / f".incoming_{image_id}.part"
    written = 0
    try:
        with tmp.open("wb") as fp:
            while True:
                chunk = upload.file.read(_UPLOAD_CHUNK)
                if not chunk:
                    break
                written += len(chunk)
                if limit_bytes is not None and written > limit_bytes:
                    raise HTTPException(
                        status_code=413,
                        detail=f"图片超过 {settings.max_upload_mb} MB 上限（MAX_UPLOAD_MB 可调，0 = 不限）",
                    )
                fp.write(chunk)
        if written == 0:
            raise HTTPException(status_code=400, detail="上传内容为空")
    except BaseException:  # 含 HTTPException 与 KeyboardInterrupt：任何中断都不留残骸
        tmp.unlink(missing_ok=True)
        raise
    return tmp


def _register_image(*, path: Path | None = None, source: str | None = None,
                    filename: str | None = None, sample_id: str | None = None,
                    upload: UploadFile | None = None,
                    image_id: str | None = None) -> dict:
    """登记一张图片到内存表 IMAGES，并把它落到 uploads/。

    三条来源走**同一条落盘逻辑**（以前 /api/upload 与 /api/upload_url 各写一份，行为不一致）：
    - upload  ：浏览器上传的字节流 → 临时文件（带大小上限）→ **真正解码一次** → 转存 PNG
    - source  ：http(s) URL / data URL → load_image 直接取图 → 转存 PNG
    - path    ：进程内已知的本地文件（内置测试图 / /api/sample 现场生成的示例图）→ 原地引用，
                不复制进 uploads/：它是共享资产，复制一份只会多出无人清理的副本
                （meta.source.sample_id 负责记住它是哪张测试图）。

    统一转 PNG 存 uploads/<image_id>.png；用户原始文件名只记进 meta.source.filename，
    不因落盘改名而丢。
    """
    image_id = image_id or uuid.uuid4().hex[:12]
    local_path: Path
    img = None

    if upload is not None:
        tmp = _stream_to_temp(upload, image_id)
        try:
            img = load_image(tmp)
        except Exception as exc:  # noqa: BLE001 - 解码失败 = 内容不是图片
            tmp.unlink(missing_ok=True)
            raise HTTPException(status_code=400, detail=f"文件内容不是可解码的图片：{exc}") from exc
        try:
            dest = config.UPLOAD_DIR / f"{image_id}.png"
            img.save(dest, format="PNG")
        finally:
            tmp.unlink(missing_ok=True)   # 临时文件用完即删
        local_path = dest
    elif path is not None:
        local_path = Path(path)
        try:
            img = load_image(local_path)
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(status_code=400, detail=f"无法加载图片：{exc}") from exc
    elif source:
        try:
            img = load_image(source)
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(status_code=400, detail=f"无法加载图片：{exc}") from exc
        local_path = config.UPLOAD_DIR / f"{image_id}.png"
        img.save(local_path, format="PNG")
    else:
        raise HTTPException(status_code=400, detail="缺少图片")

    record = {
        "id": image_id,
        "path": str(local_path),
        "filename": filename or local_path.name,
        "width": img.size[0],
        "height": img.size[1],
        # 源图的像素级指纹（暂不做去重，先留证据；见 AGENTS.md#4.9 的 meta schema）
        "sha256": storage.sha256_of_image(img),
        "url": f"/api/file/{image_id}",
        # 来自内置测试图时记下 id，标注完成后可以用真值自动打分
        "sample_id": sample_id,
    }
    IMAGES[image_id] = record
    return record


def _source_meta(record: dict) -> dict:
    """由内存登记表里的图片记录组装 meta.source（字段口径见 AGENTS.md#4.9）。"""
    return storage.make_source(
        image_id=record["id"],
        path=record["path"],
        filename=record.get("filename"),
        width=record.get("width"),
        height=record.get("height"),
        sha256=record.get("sha256"),
        sample_id=record.get("sample_id"),
    )


@app.post("/api/upload")
async def upload(file: UploadFile | None = File(default=None)) -> dict:
    """接收浏览器上传的图片（大小上限 + 内容解码校验 + 统一转 PNG）。"""
    if file is None:
        raise HTTPException(status_code=400, detail="请提供 file 字段")

    suffix = Path(file.filename or "").suffix.lower()
    if suffix not in ALLOWED_SUFFIXES:
        raise HTTPException(status_code=400, detail=f"不支持的图片格式：{suffix or '未知'}")

    return _register_image(upload=file, filename=file.filename)


@app.post("/api/upload_url")
async def upload_url(payload: dict) -> dict:
    """通过图片 URL 登记。"""
    url = (payload or {}).get("url", "").strip()
    if not url:
        raise HTTPException(status_code=400, detail="缺少 url")
    return _register_image(source=url, filename=url.rsplit("/", 1)[-1])


@app.get("/api/file/{image_id}")
def get_file(image_id: str) -> FileResponse:
    """原图。内存表里没有时从历史记录按 source.image_id 反查。

    为什么要有反查：IMAGES 是进程内字典，服务重启后就是空的；而历史面板里的老记录
    仍然在页面上，它们引用的 /api/file/<image_id> 若不反查就会全变成裂图。
    """
    record = IMAGES.get(image_id)
    if record:
        return FileResponse(record["path"])
    found = storage.find_source_by_image_id(image_id)
    if found is None:
        raise HTTPException(status_code=404, detail="图片不存在")
    return FileResponse(found)


@app.get("/api/result/{name}")
def get_result(name: str) -> FileResponse:
    """旧的标注结果路由：向后兼容保留。

    新流程的标注图一律走 /api/history/{run_id}/annotated（见 AGENTS.md#4.9），
    所以这里**不再有新写入**。之所以还留着，是因为还有两条非网页产出落在 runs/scratch/
    （CLI `main.py detect`、模型自己调的 annotate_image 工具），它们只在终端打印本地路径；
    有这个路由才能把那个文件名拼成 URL，直接在浏览器里看。
    ⚠️ 以前只查 RUNS_DIR 根 —— 而根目录已经不再写图了，那样这个路由会**永远 404**，
    等于一条不可达的死路由。
    """
    # 防目录穿越
    safe = Path(name).name
    for base in (config.SCRATCH_DIR, config.RUNS_DIR):
        path = base / safe
        if path.exists():
            return FileResponse(path, media_type="image/png")
    raise HTTPException(status_code=404, detail="结果不存在")


# --------------------------------------------------------------------------- #
# 历史记录（持久化入口，磁盘读写全部委托给 objloc/storage.py）
# --------------------------------------------------------------------------- #
@app.get("/api/history")
def history_list(limit: int = 50, offset: int = 0, q: str | None = None) -> dict:
    """历史记录列表：按 created_at 倒序，可按 prompt / 文件名做大小写不敏感子串过滤。

    ⚠️ 每次都现场扫 runs/history/*/meta.json（storage 不维护内存缓存），
    所以服务重启后这一接口照样能读回全部记录 —— 这正是「持久化」的验收点。
    列表项**不含 items**（一次几十条会撑爆响应），单条查询才带全文。
    """
    records, total = storage.list_runs(limit=limit, offset=offset, q=q)
    return {
        "records": list(records),
        "total": total,
        "limit": limit,
        "offset": offset,
        "max_records": get_settings().history_max,
    }


@app.get("/api/history/{run_id}")
def history_get(run_id: str) -> dict:
    """单条完整记录（meta.json + 三个 url 字段）。run_id 非法或记录损坏一律 404。"""
    meta = storage.load_run(run_id)
    if meta is None:
        raise HTTPException(status_code=404, detail="记录不存在")
    return meta


@app.delete("/api/history/{run_id}")
def history_delete(run_id: str) -> dict:
    """删一条记录（连 runs/history/<run_id>/ 整个目录）。非法 run_id 不触碰任何文件。"""
    if not storage.delete_run(run_id):
        raise HTTPException(status_code=404, detail="记录不存在")
    return {"deleted": True, "run_id": run_id}


@app.delete("/api/history")
def history_clear() -> dict:
    """清空全部历史记录（不动 uploads/ 源图，那是 gc 的职责）。"""
    return {"removed": storage.clear_runs()}


@app.post("/api/history/gc")
def history_gc() -> dict:
    """清理孤儿源图：uploads/ 里既不在当前内存表、又不被任何历史记录引用的文件。

    ⚠️ 只在被显式调用时执行，**不在启动时自动跑**：刚上传、还没打标的图同样「无引用」，
    自动清会把用户刚传的图删掉（见 AGENTS.md#4.9）。
    """
    return storage.gc_orphan_sources({rec.get("path") for rec in IMAGES.values() if rec.get("path")})


@app.get("/api/history/{run_id}/annotated")
def history_annotated(run_id: str, w: int | None = None):
    """标注图；带 ?w=160 时现场缩成缩略图（内存 LRU，**不落盘**）。"""
    path = storage.annotated_path(run_id)
    if path is None:
        raise HTTPException(status_code=404, detail="标注图不存在")
    if w:
        try:
            data = storage.render_thumbnail(path, w)
        except Exception as exc:  # noqa: BLE001 - 坏图不该 500 得很含糊
            raise HTTPException(status_code=500, detail=f"缩略图生成失败：{exc}") from exc
        return Response(content=data, media_type="image/png")
    return FileResponse(path, media_type="image/png")


@app.get("/api/history/{run_id}/source")
def history_source(run_id: str) -> FileResponse:
    """源图：路径解析失败（文件被删、meta 坏）一律 404，不抛异常。"""
    meta = storage.load_run(run_id)
    path = storage.resolve_source_path(meta) if meta else None
    if path is None:
        raise HTTPException(status_code=404, detail="源图不存在")
    return FileResponse(path)


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

    # 整轮耗时：从请求进入算起（含模型流式输出与工具执行），落进历史记录的 duration_ms。
    # 计时放在这里而不是生成器内部，是因为 StreamingResponse 的生成器何时被驱动由 ASGI 决定。
    started = time.perf_counter()

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
                    settings = get_settings()
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

    # 示例图是现场生成的一次性资产：落 uploads/ 并沿用同一个 image_id（两边同名，避免
    # 文件叫 A、记录里 image_id 叫 B 这种对不上的情况）。
    image_id = uuid.uuid4().hex[:12]
    path = config.UPLOAD_DIR / f"{image_id}.png"
    img.save(path, format="PNG")
    record = _register_image(path=path, filename="sample.png", image_id=image_id)
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
