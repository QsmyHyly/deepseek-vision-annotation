# -*- coding: utf-8 -*-
"""meta.json 的读取与列表 —— 每次都**现场扫盘**，没有任何内存缓存。

职责
----
- `list_runs()` / `load_run()`：历史列表与单条记录；
- `annotated_path()` / `resolve_source_path()` / `find_source_by_image_id()`：
  把 meta 里的相对路径解析回磁盘上的文件；
- 坏记录的跳过与计数（`RunList.broken`）。

边界
----
只负责**读**：写入在 records.py、删除在 retention.py。
`urls()` 只拼 URL 字符串，不碰路由层。

为什么不用内存缓存
------------------
「持久化」的定义不是「存下来」，而是**重启后还能读回来**。所以 list_runs / load_run
每次都现场扫 `runs/history/*/meta.json`。一个几十条的目录扫盘代价可以忽略，
换来的是进程重启、热重载、多 worker 下行为一致 —— 这正是原来 IMAGES 内存字典栽跟头的地方。

单个坏记录不能炸整张列表
------------------------
meta.json 可能被截断（写盘中途断电）、被手改坏或版本不兼容。读不出来的记录跳过并计入
broken 计数，其余照常返回（历史面板不能因为一条脏数据整页空白）。

旧文件对应关系（等价重构）
--------------------------
本模块 = 拆分前 `objloc/storage.py` 的「读取」一节，函数体逐行照搬。

@doc AGENTS.md#4.9-上传--结果存储--历史记录
（该文档给出列表响应字段与三个 URL 的口径。）
"""

from __future__ import annotations

import json
from pathlib import Path

from objloc import config

from .paths import history_dir, is_valid_run_id
from .thumbs import THUMB_DEFAULT_WIDTH


def _meta_path(run_id: str) -> Path:
    return history_dir() / run_id / "meta.json"


def _read_meta(run_id: str) -> dict | None:
    """读一条 meta.json；任何异常（截断 / 非法 JSON / 不是对象 / 缺字段）都返回 None。

    刻意 catch 宽泛异常：坏记录的成因不可穷举，而「读不出来的记录」对调用方的语义只有一种 ——
    跳过。让异常冒出去会把整张历史列表打成 500。
    """
    if not is_valid_run_id(run_id):
        return None
    try:
        raw = _meta_path(run_id).read_text(encoding="utf-8")
        meta = json.loads(raw)
    except Exception:  # noqa: BLE001
        return None
    if not isinstance(meta, dict):
        return None
    # 必需字段缺失 = 坏记录：宁可当它不存在，也不要把半条记录喂给前端
    if not meta.get("run_id") or "created_at" not in meta or "source" not in meta:
        return None
    meta["run_id"] = run_id  # 以目录名为准，防止文件内容被改乱后 url 拼错
    return meta


def urls(run_id: str) -> dict:
    """一条记录的三个 URL（列表项与单条共用同一口径）。"""
    base = f"/api/history/{run_id}"
    return {
        "annotated_url": f"{base}/annotated",
        "source_url": f"{base}/source",
        "thumb_url": f"{base}/annotated?w={THUMB_DEFAULT_WIDTH}",
    }


def _list_item(meta: dict) -> dict:
    """列表项：**不含 items**（一次几十条会撑爆响应），但给出 n_items 与 URL。"""
    src = meta.get("source") or {}
    return {
        "run_id": meta.get("run_id"),
        "created_at": meta.get("created_at"),
        "kind": meta.get("kind"),
        "prompt": meta.get("prompt"),
        "thinking": meta.get("thinking"),
        "model": meta.get("model"),
        "duration_ms": meta.get("duration_ms"),
        "summary": meta.get("summary"),
        "accuracy": meta.get("accuracy"),
        "source": {
            "image_id": src.get("image_id"),
            "filename": src.get("filename"),
            "width": src.get("width"),
            "height": src.get("height"),
        },
        "n_items": len(meta.get("items") or []),
        **urls(str(meta.get("run_id") or "")),
    }


class RunList(list):
    """list_runs 的第一项：就是记录列表，另挂 total / broken 两个统计量。

    为什么用子类而不是返回三元组：契约里 list_runs 的签名是 (records, total)，
    而 broken 又必须能被上层看到（否则坏记录会静默消失）。把它挂在列表对象上，
    解包写法保持不变，需要诊断时读 records.broken 即可。
    """

    total: int = 0
    broken: int = 0


def _scan_metas() -> tuple[list[dict], int]:
    """扫盘读全部 meta：返回 (metas, broken)。目录不存在 = 还没写过记录，不是错误。"""
    root = history_dir()
    metas: list[dict] = []
    broken = 0
    if not root.exists():
        return metas, broken
    try:
        entries = list(root.iterdir())
    except OSError:
        return metas, broken
    for entry in entries:
        if not entry.is_dir() or not is_valid_run_id(entry.name):
            continue
        meta = _read_meta(entry.name)
        if meta is None:
            broken += 1
            continue
        metas.append(meta)
    return metas, broken


def _sort_key(meta: dict) -> tuple[str, str]:
    """按 created_at 倒序；同秒的记录再按 run_id 倒序（run_id 定长可比，字典序即时间序）。"""
    return (str(meta.get("created_at") or ""), str(meta.get("run_id") or ""))


def list_runs(limit: int = 50, offset: int = 0, q: str | None = None) -> tuple[RunList, int]:
    """历史列表：按 created_at 倒序，可选子串过滤与分页。

    参数
    ----
    limit / offset : 分页（limit <= 0 视为不限制，方便内部调用）
    q : 大小写不敏感的子串，同时匹配 prompt 与 source.filename

    返回
    ----
    (records, total)：records 是 RunList（列表项不含 items），total 是**过滤后**的总条数
    （不是本页条数）。records.broken 是本次扫盘跳过的坏记录数。
    """
    metas, broken = _scan_metas()
    metas.sort(key=_sort_key, reverse=True)

    needle = (q or "").strip().lower()
    if needle:
        filtered: list[dict] = []
        for meta in metas:
            prompt = str(meta.get("prompt") or "").lower()
            filename = str((meta.get("source") or {}).get("filename") or "").lower()
            if needle in prompt or needle in filename:
                filtered.append(meta)
        metas = filtered

    total = len(metas)
    start = max(0, int(offset or 0))
    end = None if not limit or int(limit) <= 0 else start + int(limit)
    page = metas[start:end]

    records = RunList(_list_item(m) for m in page)
    records.total = total
    records.broken = broken
    return records, total


def load_run(run_id: str) -> dict | None:
    """读单条完整记录 = meta.json + 三个 url 字段；不存在或坏记录返回 None。"""
    meta = _read_meta(str(run_id))
    if meta is None:
        return None
    return {**meta, **urls(meta["run_id"])}


def annotated_path(run_id: str) -> Path | None:
    """标注图路径；不存在返回 None（路由层转 404）。"""
    if not is_valid_run_id(run_id):
        return None
    path = history_dir() / run_id / "annotated.png"
    return path if path.is_file() else None


def resolve_source_path(meta: dict | None) -> Path | None:
    """把 meta.source.path（相对项目根的正斜杠路径）解析成绝对路径。

    文件不存在、路径为空、meta 结构不对 —— 一律返回 None，**不抛异常**：
    调用方（源图路由 / gc）面对的都只是「这张图没了」这一种语义。
    """
    if not isinstance(meta, dict):
        return None
    rel = ((meta.get("source") or {}) if isinstance(meta.get("source"), dict) else {}).get("path")
    if not rel:
        return None
    try:
        path = Path(str(rel))
        if not path.is_absolute():
            path = Path(config.PROJECT_ROOT) / path
        return path if path.is_file() else None
    except Exception:  # noqa: BLE001
        return None


def find_source_by_image_id(image_id: str) -> Path | None:
    """按 source.image_id 反查源图（新到旧）。

    用途：`GET /api/file/{image_id}` 在内存表 IMAGES 里找不到时（服务重启过、
    或者换了浏览器会话）还能从历史记录里把原图捞回来 —— 否则刷新页面后历史项的原图全是裂图。
    """
    if not image_id:
        return None
    metas, _broken = _scan_metas()
    metas.sort(key=_sort_key, reverse=True)
    for meta in metas:
        if str((meta.get("source") or {}).get("image_id") or "") == str(image_id):
            found = resolve_source_path(meta)
            if found is not None:
                return found
    return None
