# -*- coding: utf-8 -*-
"""删除与保留策略 —— 「记录怎么消失」的唯一实现。

职责
----
- `delete_run()` / `clear_runs()`：删一条 / 清空（`_rmtree_retry` 负责短重试）；
- `history_max()` / `enforce_retention()`：`HISTORY_MAX` 闸门，按 run_id 删最老的；
- `gc_orphan_sources()`：清理 uploads/ 里谁也不引用的孤儿源图。

边界
----
不读单条记录的内容（那是 index.py 的事），只做「按 id 删目录」与「按引用关系删文件」。
⚠️ 本模块**不提供自动触发**：`enforce_retention()` 由 records.record_run 显式调用，
`gc_orphan_sources()` 只在 `POST /api/history/gc` 被显式调用时执行。

旧文件对应关系（等价重构）
--------------------------
本模块 = 拆分前 `objloc/storage.py` 的「删除 / 保留策略」一节，函数体逐行照搬。

@doc AGENTS.md#4.9-上传--结果存储--历史记录
（该文档解释「删除必须删不掉就如实说」与「为什么不在启动时自动 GC」。）
"""

from __future__ import annotations

import shutil
import sys
import time
from pathlib import Path
from typing import Iterable

from objloc import config

from .index import _scan_metas, resolve_source_path
from .paths import history_dir, is_valid_run_id, uploads_dir


def _rmtree_retry(directory: Path, attempts: int = 5) -> bool:
    """删掉整个目录，返回是否真的删掉了。会短重试几次。

    ⚠️ 为什么不能只写 `shutil.rmtree` + `except OSError: pass`（本机实测踩过）：
    一次性删二十来条记录时，偶发有几条 rmtree 抛 WinError 32（文件正被杀软扫描、
    被索引器或前一个请求的读句柄占着），原先那种写法会**静默跳过** ——
    接口照样返回 200、界面照样说「已清空 22 条」，盘上却剩 5 条。
    **静默的部分成功比直接失败更难查**，所以这里重试，仍失败就打印到 stderr 并返回 False，
    由调用方如实记数（API 回给前端的 `removed` 因此是"真删掉的条数"）。

    ⚠️ 这里必须走 `shutil.rmtree` 的**属性查找**（而不是 `from shutil import rmtree`）：
    tests/test_storage.py 第 11 组靠替换 `shutil.rmtree` 来模拟「文件被占用」，
    换成本地绑定的名字就打不上桩了。
    """
    last: OSError | None = None
    for i in range(attempts):
        try:
            shutil.rmtree(directory)
            return True
        except FileNotFoundError:
            return True        # 已经没了（并发删除 / 本来就不在），对调用方来说算成功
        except OSError as exc:
            last = exc
            if i < attempts - 1:
                time.sleep(0.05 * (i + 1))   # 给占用方一点时间松手
    print(f"[storage] 删除失败（重试 {attempts} 次后仍被占用？）：{directory}：{last}", file=sys.stderr)
    return False


def delete_run(run_id: str) -> bool:
    """删一条记录（连 runs/history/<run_id>/ 整个目录）；不存在或 run_id 非法返回 False。

    ⚠️ 非法 run_id 直接返回 False、**不做任何文件操作**：rmtree 一个未验证的拼接路径
    等于把删除权交给请求方。
    """
    if not is_valid_run_id(run_id):
        return False
    directory = history_dir() / run_id
    if not directory.is_dir():
        return False
    return _rmtree_retry(directory)


def clear_runs() -> int:
    """清空全部历史记录，返回**真正删掉**的条数（不动 uploads/，源图由 gc 负责）。

    返回的是实删条数而不是"碰到过几条"：删不掉的会留在盘上，调用方（以及界面上那句
    「已清空 N 条」）必须能看出来，见 `_rmtree_retry` 的说明。
    """
    root = history_dir()
    if not root.exists():
        return 0
    removed = 0
    for entry in list(root.iterdir()):
        if not entry.is_dir() or not is_valid_run_id(entry.name):
            continue  # 目录里若有别的东西，不碰
        if _rmtree_retry(entry):
            removed += 1
    return removed


def history_max() -> int:
    """保留上限，来自 Settings.history_max（环境变量 HISTORY_MAX，0 = 不限）。"""
    try:
        return int(config.get_settings().history_max)
    except Exception:  # noqa: BLE001
        return 200


def enforce_retention() -> int:
    """按 run_id 升序删最老的，直到条数 <= history_max；返回删除条数。

    这是防「只增不减」的闸门 —— runs/ 根目录那 162 张散图就是这么攒出来的。
    上限为 0 表示不限，直接返回。
    """
    limit = history_max()
    if limit <= 0:
        return 0
    metas, _broken = _scan_metas()
    # 只按目录名排序（run_id 字典序即时间序），不依赖 meta 内容，坏记录也删得掉
    ids = sorted(m.get("run_id") for m in metas if m.get("run_id"))
    extra = len(ids) - limit
    if extra <= 0:
        return 0
    removed = 0
    for run_id in ids[:extra]:
        if delete_run(str(run_id)):
            removed += 1
    return removed


def gc_orphan_sources(keep_paths: Iterable[str] | None = None) -> dict:
    """清理孤儿源图：uploads/ 里既不在 keep_paths、又不被任何 history 引用的文件。

    keep_paths 是「当前进程内存表 IMAGES 里登记过的源图」（刚上传、还没打标的图）。
    ⚠️ **只在被显式调用时执行**（POST /api/history/gc），绝不在 import / 启动时自动跑：
    刚上传还没打标的图同样满足「无引用」，启动时自动清会把用户刚传的图删掉。

    返回 {"removed": n, "freed_bytes": m}。
    """
    keep: set[Path] = set()
    for raw in keep_paths or ():
        try:
            keep.add(Path(str(raw)).resolve())
        except Exception:  # noqa: BLE001
            continue

    referenced: set[Path] = set()
    metas, _broken = _scan_metas()
    for meta in metas:
        found = resolve_source_path(meta)
        if found is not None:
            referenced.add(found.resolve())

    directory = uploads_dir()
    removed = 0
    freed = 0
    if not directory.exists():
        return {"removed": removed, "freed_bytes": freed}
    for entry in list(directory.iterdir()):
        if not entry.is_file():
            continue
        try:
            resolved = entry.resolve()
        except OSError:
            continue
        if resolved in keep or resolved in referenced:
            continue
        try:
            size = entry.stat().st_size
            entry.unlink()
        except OSError:
            continue
        removed += 1
        freed += size
    return {"removed": removed, "freed_bytes": freed}
