# -*- coding: utf-8 -*-
"""路径解析 + run_id 生成 —— 「记录该放哪、这个 id 合不合法」的唯一答案。

职责
----
1. 把 `objloc.config` 上的目录常量解析成 Path；
2. 生成 run_id、校验 run_id 形状。

边界
----
**不碰文件内容**：不读 `meta.json`、不写标注图、不做删除（分别在 index.py / records.py /
retention.py）。本模块只回答"路径与 id"这两个问题 —— 于是测试可以放心地只改
`config.RUNS_DIR` 就把全部产物挪走。

为什么路径一律**动态**读 config.*，不在导入期绑定常量
------------------------------------------------------
测试要把这些目录指向 tempfile 临时目录，只要改 `objloc.config` 上的模块级常量就能生效；
如果写成 `from objloc.config import HISTORY_DIR`，导入期就被固化成真路径，
测试产物会漏进项目的 runs/。

旧文件对应关系（等价重构）
--------------------------
本模块 = 拆分前 `objloc/storage.py` 的「常量」+「路径解析」+「run_id 生成」三节，
函数体与注释逐行照搬，无行为改动。

@doc AGENTS.md#4.9-上传--结果存储--历史记录
（该文档解决「目录布局、meta schema、HTTP 接口分别是什么」的问题。）
"""

from __future__ import annotations

import random
import re
import threading
import time
from pathlib import Path
from typing import Any

from objloc import config

# run_id 的唯一合法形状。⚠️ 所有对外接收 run_id 的入口（list/load/delete/url 拼接）
# 都必须先过这一关，否则 "../../etc/passwd" 这类输入会变成真实的删除目标。
RUN_ID_RE = re.compile(r"^[0-9]{8}-[0-9]{6}-[0-9a-f]{6}$")

# 生成的 run_id 序列状态（见 new_run_id）
_seq_lock = threading.Lock()
_seq_stamp: str | None = None      # 当前这一秒的时间戳前缀
_seq_start = 0                     # 本秒的随机起点
_seq_count = 0                     # 本秒已发出的条数


def history_dir() -> Path:
    """历史记录根目录（runs/history）。"""
    return Path(config.HISTORY_DIR)


def uploads_dir() -> Path:
    """源图目录（runs/uploads）。"""
    return Path(config.UPLOAD_DIR)


def run_dir(run_id: str) -> Path:
    """取某条记录的目录，顺带创建。run_id 非法时抛 ValueError。

    给调用方（web/app.py）用，避免它在外面自己拼 "history/<run_id>" 这类路径。
    """
    _require_run_id(run_id)
    path = history_dir() / run_id
    path.mkdir(parents=True, exist_ok=True)
    return path


def _require_run_id(run_id: Any) -> str:
    """校验 run_id 形状；不合法直接抛 ValueError（调用方一律转成「不存在」）。"""
    text = "" if run_id is None else str(run_id)
    if not RUN_ID_RE.match(text):
        raise ValueError(f"非法 run_id：{text!r}")
    return text


def is_valid_run_id(run_id: Any) -> bool:
    """对外暴露的形状检查（路由层可据此直接 404，不必进 try/except）。"""
    return bool(RUN_ID_RE.match("" if run_id is None else str(run_id)))


def _max_existing_suffix(stamp: str) -> int | None:
    """同一秒内**已经落盘**的 run_id 的最大后缀；一个都没有则返回 None。

    为什么需要它：进程重启（或模块重载）会清空内存里的序列状态，此后随机起点重新掷。
    如果重启恰好落在同一秒内，新掷的起点可能比磁盘上已有的 id 小 ——
    那新记录在字典序上排到了旧记录**前面**，保留策略「删最老」就会反过来删掉刚写的那条。
    所以新一轮序列的起点必须高过同一秒内已有的最大值。
    一秒只扫一次目录，代价可以忽略。
    """
    root = history_dir()
    if not root.exists():
        return None
    best: int | None = None
    try:
        for entry in root.iterdir():
            name = entry.name
            if not name.startswith(stamp + "-") or not is_valid_run_id(name):
                continue
            try:
                value = int(name[-6:], 16)
            except ValueError:
                continue
            best = value if best is None else max(best, value)
    except OSError:
        return None
    return best


def new_run_id(now: time.struct_time | None = None) -> str:
    """生成 run_id：`YYYYMMDD-HHMMSS-<6位hex>`，字典序即时间序。

    为什么不用纯随机 hex 后缀：同一秒内生成的多条记录如果后缀随机，字典序就会变成乱序，
    而列表排序与「删最老」的保留策略都直接依赖这一定长可比的字符串。
    做法是「每秒一个随机起点 + 进程内自增」：
    - 同一秒内后缀严格递增 ⇒ 生成顺序 == 字典序；
    - 每秒换随机起点 ⇒ 进程重启后同一秒内也不会撞上前一次运行留下的 id（概率 1/4096，
      真撞上了还有下面的存在性检查兜底：往后挪直到空闲）。
    """
    global _seq_stamp, _seq_start, _seq_count

    cand = ""
    with _seq_lock:
        stamp = time.strftime("%Y%m%d-%H%M%S", now or time.localtime())
        if stamp != _seq_stamp:
            _seq_stamp = stamp
            # 同一秒内可能已经有上一轮进程写下的记录：起点取「已有最大值」，否则取随机值
            # （随机值是为了让正常连写的一批 id 看起来分散，而不是从 000001 开始排）
            seen = _max_existing_suffix(stamp)
            _seq_start = seen if seen is not None else random.randrange(0, 0x1000)
            _seq_count = 0
        # 存在性检查也在锁内：它同时保证了「返回的 id 一定还没被占用」
        for _ in range(0x1000000):
            _seq_count += 1
            suffix = (_seq_start + _seq_count) % 0x1000000
            cand = f"{stamp}-{suffix:06x}"
            if not (history_dir() / cand).exists():
                break
    return cand
