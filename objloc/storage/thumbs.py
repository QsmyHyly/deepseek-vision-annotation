# -*- coding: utf-8 -*-
"""缩略图：现场等比缩放 + 内存 LRU。**不落盘**。

职责
----
把 `GET /api/history/{run_id}/annotated?w=160` 需要的缩略图算出来并缓存。

边界 / 为什么这样设计
---------------------
- **不落盘**：落盘又会攒出一堆没人清理的缩略图文件，那正是 storage 这个模块要根治的问题；
- **带缓存**：列表一页 50 条，每条都要一张缩略图，而原标注图可能有 2 MB；
  没有缓存时每滚一次列表就重新解码 50 张大图。
  缓存键 = (绝对路径, mtime_ns, 文件大小, 目标宽度)：文件被覆盖后 mtime/size 变了，
  旧条目自然失效（不会拿着旧图骗人），而同一个文件的重复请求直接命中。
- **线程安全**：FastAPI 的同步路由跑在线程池里，会并发进来，所以 LRU 的读写都在锁内。
- 用 OrderedDict + Lock 而不是 functools.lru_cache：lru_cache 的键必须是可哈希的不可变值，
  而「文件 mtime」这种随时间变化的键会不断产生新条目，旧条目又永远命中不到 —— 等于内存泄漏。
  这里显式控制容量与淘汰。

本模块不依赖包内其它模块（谁都可以 import 它），因此 `THUMB_DEFAULT_WIDTH` 可以安全地
被 index.py 借去拼 `thumb_url`。

旧文件对应关系（等价重构）
--------------------------
本模块 = 拆分前 `objloc/storage.py` 的「缩略图」一节，函数体逐行照搬。

@doc AGENTS.md#4.9-上传--结果存储--历史记录
（该文档解释「?w= 缩略图为什么现场缩放 + 内存 LRU」。）
"""

from __future__ import annotations

import io
import threading
from collections import OrderedDict
from pathlib import Path

from PIL import Image

# 缩略图 LRU 容量。列表一页默认 50 条，留一倍余量即可；每张缩略图几十 KB，压力很小。
THUMB_CACHE_SIZE = 64
THUMB_DEFAULT_WIDTH = 160

_thumb_cache: "OrderedDict[tuple, bytes]" = OrderedDict()
_thumb_lock = threading.Lock()


def render_thumbnail(path: str | Path, width: int = THUMB_DEFAULT_WIDTH) -> bytes:
    """把图片等比缩放到宽 `width`，返回 PNG 字节。**不写磁盘**。

    为什么带缓存：列表一页 50 条，每条都要一张缩略图，而原标注图可能有 2 MB；
    没有缓存时每滚一次列表就重新解码 50 张大图。
    缓存键 = (绝对路径, mtime_ns, 文件大小, 目标宽度)：文件被覆盖后 mtime/size 变了，
    旧条目自然失效（不会拿着旧图骗人），而同一个文件的重复请求直接命中。
    线程安全：FastAPI 的同步路由跑在线程池里，会并发进来，所以 LRU 的读写都在锁内。
    """
    target_width = max(1, int(width or THUMB_DEFAULT_WIDTH))
    p = Path(path)
    stat = p.stat()  # 文件不存在会抛 FileNotFoundError，由路由层转 404
    key = (str(p.resolve()), stat.st_mtime_ns, stat.st_size, target_width)

    with _thumb_lock:
        hit = _thumb_cache.get(key)
        if hit is not None:
            _thumb_cache.move_to_end(key)
            return hit

    with Image.open(p) as img:
        rgb = img.convert("RGB")
        src_w, src_h = rgb.size
        target_height = max(1, round(src_h * target_width / max(1, src_w)))
        thumb = rgb.resize((target_width, target_height), Image.Resampling.LANCZOS)
        buffer = io.BytesIO()
        thumb.save(buffer, format="PNG")
    data = buffer.getvalue()

    with _thumb_lock:
        _thumb_cache[key] = data
        _thumb_cache.move_to_end(key)
        while len(_thumb_cache) > THUMB_CACHE_SIZE:
            _thumb_cache.popitem(last=False)
    return data


def clear_thumbnail_cache() -> None:
    """清空缩略图缓存（自测用；也让「删了文件但进程还拿着旧图」有解）。"""
    with _thumb_lock:
        _thumb_cache.clear()
