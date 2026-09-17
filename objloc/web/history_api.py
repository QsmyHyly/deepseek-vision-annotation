# -*- coding: utf-8 -*-
"""历史记录路由 —— 打标记录的读 / 删 / 清空 / 孤儿 GC。

职责
----
7 个 HTTP 接口的一层薄包装：列表、单条、删一条、清空、孤儿源图 GC、
标注图（可带 ?w= 现场缩略）、源图。磁盘读写全部委托 objloc/storage ——
本模块不自己拼 history/ 路径，也不直接读 meta.json。

边界
----
只做 HTTP 语义翻译（404 怎么给、缩略图 Content-Type 是什么）。
目录穿越防护、保留策略、"删不掉就如实说"都在 storage 包里。

旧文件对应关系（等价重构）
--------------------------
本模块 = 拆分前 objloc/web/app.py 的「历史记录」一节，逐行搬移；
装饰器由 @app.* 机械改成 @router.*。

@doc AGENTS.md#4.9-上传--结果存储--历史记录
（该文档给出这些接口的字段口径与列表响应结构。）
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse, Response

from objloc import storage
from objloc.config import get_settings

from .state import IMAGES

router = APIRouter()

# --------------------------------------------------------------------------- #
# 历史记录（持久化入口，磁盘读写全部委托给 objloc/storage/）
# --------------------------------------------------------------------------- #
@router.get("/api/history")
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


@router.get("/api/history/{run_id}")
def history_get(run_id: str) -> dict:
    """单条完整记录（meta.json + 三个 url 字段）。run_id 非法或记录损坏一律 404。"""
    meta = storage.load_run(run_id)
    if meta is None:
        raise HTTPException(status_code=404, detail="记录不存在")
    return meta


@router.delete("/api/history/{run_id}")
def history_delete(run_id: str) -> dict:
    """删一条记录（连 runs/history/<run_id>/ 整个目录）。非法 run_id 不触碰任何文件。"""
    if not storage.delete_run(run_id):
        raise HTTPException(status_code=404, detail="记录不存在")
    return {"deleted": True, "run_id": run_id}


@router.delete("/api/history")
def history_clear() -> dict:
    """清空全部历史记录（不动 uploads/ 源图，那是 gc 的职责）。"""
    return {"removed": storage.clear_runs()}


@router.post("/api/history/gc")
def history_gc() -> dict:
    """清理孤儿源图：uploads/ 里既不在当前内存表、又不被任何历史记录引用的文件。

    ⚠️ 只在被显式调用时执行，**不在启动时自动跑**：刚上传、还没打标的图同样「无引用」，
    自动清会把用户刚传的图删掉（见 AGENTS.md#4.9）。
    """
    return storage.gc_orphan_sources({rec.get("path") for rec in IMAGES.values() if rec.get("path")})


@router.get("/api/history/{run_id}/annotated")
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


@router.get("/api/history/{run_id}/source")
def history_source(run_id: str) -> FileResponse:
    """源图：路径解析失败（文件被删、meta 坏）一律 404，不抛异常。"""
    meta = storage.load_run(run_id)
    path = storage.resolve_source_path(meta) if meta else None
    if path is None:
        raise HTTPException(status_code=404, detail="源图不存在")
    return FileResponse(path)
