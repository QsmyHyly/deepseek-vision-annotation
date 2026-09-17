# -*- coding: utf-8 -*-
"""打标历史记录的唯一读写入口（对外门面）。

## 这个模块解决什么问题

历史记录之前只有一张 PNG：坐标 / 标签 / 提示词 / 准确率全在进程内存里，服务一重启就没了，
而 PNG 本身也反查不回任何来源（文件名还是随机 uuid）。于是「打标」只留下产物，没留下记录。
本包把**每一次打标**变成一条自描述、可重启读回的记录。

## 目录布局（见 AGENTS.md#4.9-上传--结果存储--历史记录）

    runs/
      uploads/            源图。统一转 PNG，文件名 <image_id>.png（原始文件名记在 meta 里）
      scratch/            无归属的临时标注图（CLI detect / 模型自己调的 annotate_image）
      history/<run_id>/
        meta.json         记录元数据（schema v1，唯一真相来源）
        annotated.png     标注图

## 包内职责划分（原本是一个 721 行的 objloc/storage.py）

| 子模块 | 一句话职责 | 原文件对应段落 |
|---|---|---|
| `paths.py` | 目录与 run_id：路径解析、id 生成与形状校验 | 「常量」「路径解析」「run_id 生成」 |
| `records.py` | meta.json 的组装与原子写入 | 「写入」 |
| `index.py` | 现场扫盘读取（列表 / 单条 / 路径解析 / 反查） | 「读取」 |
| `retention.py` | 删除与保留策略（重试、HISTORY_MAX、孤儿 GC） | 「删除 / 保留策略」 |
| `thumbs.py` | `?w=` 缩略图：现场缩放 + 内存 LRU（不落盘） | 「缩略图」 |

⚠️ 这次拆分是**纯搬移**：函数体、默认值、异常口径、stderr 文案一律原样。
对外契约（`from objloc.storage import xxx`、`import objloc.storage as storage`、
`objloc.storage.new_run_id()` 这类属性访问）由本文件 re-export 保持不变。

@doc AGENTS.md#4.9-上传--结果存储--历史记录
（该文档解决「目录布局、meta schema、HTTP 接口分别是什么」的问题。）

## 本包对外承诺的行为（改代码前先读完）

- **run_id** = `YYYYMMDD-HHMMSS-<6位hex>`，定长且高位是时间 ⇒ **字典序 == 时间序**。
  列表排序、保留策略（删最老）都直接靠它，不必解析时间。
- **防目录穿越**：run_id 只允许 `^[0-9]{8}-[0-9]{6}-[0-9a-f]{6}$`。
  不匹配一律当作「不存在」，绝不拿未验证的拼接路径去 unlink/rmtree。
- **持久化的定义是「重启后还能读回来」**：所以读取一律现场扫盘，不维护内存缓存。
- **单个坏记录不能炸整张列表**：读不出来的跳过并计入 broken 计数。
- **缩略图不落盘**：`?w=` 现场缩图并放进内存 LRU（缓存键含 mtime）。
"""

from __future__ import annotations

from .index import (
    RunList,
    annotated_path,
    find_source_by_image_id,
    list_runs,
    load_run,
    resolve_source_path,
    urls,
)
from .paths import (
    RUN_ID_RE,
    history_dir,
    is_valid_run_id,
    new_run_id,
    run_dir,
    uploads_dir,
)
from .records import (
    APP_VERSION,
    SCHEMA_VERSION,
    make_source,
    record_run,
    relative_source_path,
    sha256_of_image,
)
from .retention import (
    clear_runs,
    delete_run,
    enforce_retention,
    gc_orphan_sources,
    history_max,
)
from .thumbs import (
    THUMB_CACHE_SIZE,
    THUMB_DEFAULT_WIDTH,
    clear_thumbnail_cache,
    render_thumbnail,
)

# 私有辅助函数一并转出：它们原本就是 objloc.storage 的模块属性，拆分前 storage._rmtree_retry
# 这类写法是能用的。保持可用，避免「内部重构」意外变成对外破坏。
# （模块级**可变状态**如 _seq_stamp / _thumb_cache 不转出：转出来只会是一份读到的快照，
#   反而误导；缓存清空有 clear_thumbnail_cache() 这个正经出口。）
from .index import _list_item, _meta_path, _read_meta, _scan_metas, _sort_key  # noqa: F401
from .paths import _max_existing_suffix, _require_run_id  # noqa: F401
from .records import _now_iso, _summarize  # noqa: F401
from .retention import _rmtree_retry  # noqa: F401
# 项目内顺带可见的模块别名（拆分前 objloc.storage.config 能取到）。
# 只有 stdlib / 三方名（json、Path、Image、threading…）与模块级可变状态不再转出 ——
# 前者不是本模块的接口，后者转出来只是一份快照，反而误导。
from objloc import config  # noqa: F401

__all__ = [
    # 路径与 id
    "RUN_ID_RE", "history_dir", "uploads_dir", "run_dir", "is_valid_run_id", "new_run_id",
    # 写入
    "SCHEMA_VERSION", "APP_VERSION", "sha256_of_image", "relative_source_path",
    "make_source", "record_run",
    # 读取
    "RunList", "urls", "list_runs", "load_run", "annotated_path",
    "resolve_source_path", "find_source_by_image_id",
    # 删除与保留
    "delete_run", "clear_runs", "history_max", "enforce_retention", "gc_orphan_sources",
    # 缩略图
    "THUMB_CACHE_SIZE", "THUMB_DEFAULT_WIDTH", "render_thumbnail", "clear_thumbnail_cache",
]
