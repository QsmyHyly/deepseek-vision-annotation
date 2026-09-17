# -*- coding: utf-8 -*-
"""meta.json 的组装与原子写入 —— 打标记录的**唯一写入路径**。

职责
----
- `make_source()`：按 schema v1 组装 `source` 子对象（含尺寸与像素级指纹）；
- `record_run()`：把一次打标写成 `history/<run_id>/meta.json` + `annotated.png`；
- 写完之后调一次保留策略（`retention.enforce_retention()`）。

边界
----
只负责**写**：读回在 index.py、删除在 retention.py。
写入是原子的（先写同目录 .tmp 再 replace），半截的 meta.json 虽然会被读侧当成坏记录跳过，
但那毕竟丢数据，rename 在同一文件系统上是原子的，能直接避免这种半成品。

旧文件对应关系（等价重构）
--------------------------
本模块 = 拆分前 `objloc/storage.py` 的「写入」一节（含 schema 常量），函数体逐行照搬。

@doc AGENTS.md#4.9-上传--结果存储--历史记录
（该文档给出 meta.json schema v1 的逐字段说明。）
"""

from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path
from typing import Any, Iterable

from PIL import Image

from objloc import config

from .paths import _require_run_id, new_run_id, run_dir
from .retention import enforce_retention

# 记录 schema 版本：字段增删改语义时 +1（前端可据此做兼容分支）
SCHEMA_VERSION = 1
APP_VERSION = "2.0"


def _now_iso() -> str:
    """本地时间 ISO（秒精度），与 run_id 的前缀同源，便于人工对表。"""
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime())


def sha256_of_image(image: Image.Image) -> str:
    """源图的**像素级**指纹（不是文件字节）。

    刻意用 tobytes() 而不是读文件：同一张图重新编码一次（PNG 压缩参数、Pillow 版本不同）
    文件字节就变了，像素却不变。暂时不做去重，先留证据（见 AGENTS.md#4.9 的 schema 注释）。
    """
    return hashlib.sha256(image.tobytes()).hexdigest()


def relative_source_path(path: str | Path) -> str:
    """把绝对路径转成「相对项目根、正斜杠」的写法存进 meta。

    换机器 / 挪目录后仍可解析，也不会把 C:/Users/xxx 这种本机路径写进记录（正斜杠在 Windows 上同样可解析）。
    """
    p = Path(path)
    try:
        rel = p.resolve().relative_to(Path(config.PROJECT_ROOT).resolve())
    except Exception:  # noqa: BLE001 - 不在项目内（例如临时目录）就原样存绝对路径
        return Path(p).as_posix()   # as_posix 顺带把 Windows 反斜杠换成正斜杠
    return rel.as_posix()


def make_source(
    *,
    image_id: str,
    path: str | Path,
    filename: str | None = None,
    width: int | None = None,
    height: int | None = None,
    sha256: str | None = None,
    sample_id: str | None = None,
) -> dict:
    """按 §4.9 schema 组装 source 子对象（宽高/指纹缺省时现场从文件读）。"""
    p = Path(path)
    if width is None or height is None or sha256 is None:
        with Image.open(p) as img:
            img = img.convert("RGB")
            width = width if width is not None else img.size[0]
            height = height if height is not None else img.size[1]
            sha256 = sha256 or sha256_of_image(img)
    return {
        "image_id": image_id,
        "filename": filename or p.name,
        "path": relative_source_path(p),
        "width": int(width),
        "height": int(height),
        "sha256": sha256,
        "sample_id": sample_id,
    }


def record_run(
    *,
    kind: str,
    source: dict,
    items: Iterable[dict] | None = None,
    prompt: str | None = None,
    thinking: bool | None = None,
    reasoning_effort: str | None = None,
    model: str | None = None,
    duration_ms: int | None = None,
    accuracy: dict | None = None,
    warnings: Iterable[str] | None = None,
    notice: str | None = None,
    summary: dict | None = None,
    annotated: Image.Image | bytes | str | Path | None = None,
    run_id: str | None = None,
    created_at: str | None = None,
    enforce: bool = True,
) -> dict:
    """写一条历史记录：`history/<run_id>/meta.json` + `annotated.png`，返回 meta 字典。

    参数
    ----
    kind : "detect"（调模型）| "annotate"（本地给定坐标）
    source : 见 make_source()，字段名与 §4.9 schema 严格一致
    items : 坐标对象列表（0.0~1.0 相对比例，见 AGENTS.md#4.3-坐标约定）
    thinking / reasoning_effort / model / duration_ms : detect 记真实值；annotate 传 None
        （annotate 不调模型，没有耗时与模型可言，记 0 或默认值都是在编造证据）
    warnings : 旧刻度换算 / 坐标越界等告警，**原样**留档（不合并、不改写）
    annotated : 标注图。可传 PIL 图像 / PNG 字节 / 已落在本记录目录里的 annotated.png 路径
        （web 层用 render_annotations(output_dir=run_dir(run_id), stem="annotated") 直接生成到位，
        这里就不再重新编码一遍）。传 None 则不写 annotated.png。
    enforce : 写完是否执行一次保留策略（默认 True；批量造数据时可关掉）

    返回写进 meta.json 的那个字典（不含 url 字段，url 由 urls() 现算）。
    """
    rid = _require_run_id(run_id) if run_id is not None else new_run_id()
    directory = run_dir(rid)
    items_list = [it for it in (items or []) if isinstance(it, dict)]

    # ---- 标注图 ----
    annotated_name: str | None = None
    if annotated is not None:
        target = directory / "annotated.png"
        if isinstance(annotated, Image.Image):
            annotated.convert("RGB").save(target, format="PNG")
            annotated_name = target.name
        elif isinstance(annotated, (bytes, bytearray)):
            target.write_bytes(bytes(annotated))
            annotated_name = target.name
        else:
            src = Path(annotated)
            if src.resolve() == target.resolve():
                # 已经由 render_annotations 直接写到位，不重复编码（避免二次有损/无色差）
                annotated_name = target.name
            else:
                with Image.open(src) as img:
                    img.convert("RGB").save(target, format="PNG")
                annotated_name = target.name

    meta: dict[str, Any] = {
        "schema": SCHEMA_VERSION,
        "run_id": rid,
        "created_at": created_at or _now_iso(),
        "kind": kind,
        "source": dict(source or {}),
        "prompt": prompt,
        "thinking": thinking,
        "reasoning_effort": reasoning_effort,
        "model": model,
        "duration_ms": duration_ms,
        "items": items_list,
        "summary": summary if summary is not None else _summarize(items_list),
        "accuracy": accuracy,
        "warnings": list(warnings or []),
        "notice": notice,
        "app_version": APP_VERSION,
    }

    # 原子写：先写 .tmp 再 replace。半截的 meta.json 会被读侧当成坏记录跳过，
    # 但那毕竟是丢数据；rename 在同一文件系统上是原子的，能直接避免这种半成品。
    tmp = directory / "meta.json.tmp"
    tmp.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(directory / "meta.json")

    if enforce:
        enforce_retention()
    return meta


def _summarize(items: list[dict]) -> dict:
    """统计条目数（与 visualizer.summarize 同口径，但这里不依赖 visualizer 以免循环导入）。"""
    bbox = sum(1 for it in items if "bbox_2d" in it)
    point = sum(1 for it in items if "point_2d" in it)
    return {"total": len(items), "bbox_count": bbox, "point_count": point,
            "labels": [str(it.get("label", "")) for it in items]}
