# -*- coding: utf-8 -*-
"""图片登记与图片资源路由 —— 「浏览器交进来的图」怎么变成一条可用记录。

职责
----
- **落盘与登记**（三条来源走同一条逻辑）：_stream_to_temp / _register_image / _source_meta；
- **图片资源路由**：/api/upload、/api/upload_url、/api/file/{image_id}、
  /api/result/{name}（旧路由，向后兼容）、/api/samples*、/api/sample。

边界
----
只管"图片从哪来、存到哪、怎么取回"。识别与标注在 detect_api.py、历史记录在
history_api.py、进程内共享状态在 state.py。

上传侧的三条硬约束（大小上限 / 内容必须真解码一次 / 统一转 PNG 落盘）全部实现在本模块，
改动它们只需要动这一个文件 —— 这正是把它们收在一处的原因。

旧文件对应关系（等价重构）
--------------------------
本模块 = 拆分前 objloc/web/app.py（814 行）的「内置测试图」「图片登记」「内置示例」三节，
外加 /api/upload、/api/upload_url、/api/file/{image_id}、/api/result/{name} 四个路由，
逐行搬移；装饰器由 @app.* 机械改成 @router.*，函数体未改一个字。

@doc AGENTS.md#4.9-上传--结果存储--历史记录
（该文档给出上传三条硬约束，以及 /api/result 为什么必须继续存在。）
"""

from __future__ import annotations

import uuid
from pathlib import Path

from fastapi import APIRouter, File, HTTPException, UploadFile
from fastapi.responses import FileResponse

from objloc import config
from objloc import samples as samples_mod
from objloc import storage
from objloc.config import get_settings
from objloc.visualizer import load_image, resolve_font

from .state import IMAGES

router = APIRouter()

# 后缀白名单只是**第一道**检查（挡住明显不对的扩展名，省一次落盘）；
# 真正的内容校验是落盘后用 load_image 解码一次，见 _register_image。
ALLOWED_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp", ".bmp", ".gif"}

# 上传流的分块大小：边写边累计字节，才能在超限时立刻中断（而不是先写满磁盘再报错）
_UPLOAD_CHUNK = 1 << 20

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


@router.post("/api/upload")
async def upload(file: UploadFile | None = File(default=None)) -> dict:
    """接收浏览器上传的图片（大小上限 + 内容解码校验 + 统一转 PNG）。"""
    if file is None:
        raise HTTPException(status_code=400, detail="请提供 file 字段")

    suffix = Path(file.filename or "").suffix.lower()
    if suffix not in ALLOWED_SUFFIXES:
        raise HTTPException(status_code=400, detail=f"不支持的图片格式：{suffix or '未知'}")

    return _register_image(upload=file, filename=file.filename)


@router.post("/api/upload_url")
async def upload_url(payload: dict) -> dict:
    """通过图片 URL 登记。"""
    url = (payload or {}).get("url", "").strip()
    if not url:
        raise HTTPException(status_code=400, detail="缺少 url")
    return _register_image(source=url, filename=url.rsplit("/", 1)[-1])


@router.get("/api/file/{image_id}")
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


@router.get("/api/result/{name}")
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
# 内置测试图（目录见 objloc/samples/）
# --------------------------------------------------------------------------- #
@router.get("/api/samples")
def list_samples() -> dict:
    """返回按测试目的分组的内置测试图目录（不含真值，避免提前泄露答案）。"""
    groups = samples_mod.list_catalog()
    return {
        "dir": str(samples_mod.SAMPLES_DIR),
        "groups": groups,
        "total": sum(len(g["items"]) for g in groups),
    }


@router.get("/api/samples/{sample_id}/image")
def sample_image(sample_id: str) -> FileResponse:
    """测试图原图；首次访问时现场生成（大图需要一两秒）。"""
    try:
        _item, path = samples_mod.ensure(sample_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return FileResponse(path, media_type="image/png")


@router.post("/api/samples/{sample_id}/load")
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
# 内置示例（离线体验）
# --------------------------------------------------------------------------- #
SAMPLE_ITEMS = [
    {"bbox_2d": [0.18, 0.24, 0.43, 0.62], "label": "示例目标 A"},
    {"bbox_2d": [0.52, 0.30, 0.76, 0.70], "label": "示例目标 B"},
    {"point_2d": [0.64, 0.20], "label": "示例点位"},
]


@router.post("/api/sample")
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
