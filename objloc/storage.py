# -*- coding: utf-8 -*-
"""打标历史记录的唯一读写入口。

## 这个模块解决什么问题

历史记录之前只有一张 PNG：坐标 / 标签 / 提示词 / 准确率全在进程内存里，服务一重启就没了，
而 PNG 本身也反查不回任何来源（文件名还是随机 uuid）。于是「打标」只留下产物，没留下记录。
本模块把**每一次打标**变成一条自描述、可重启读回的记录。

## 目录布局（见 AGENTS.md#4.9-上传--结果存储--历史记录）

    runs/
      uploads/            源图。统一转 PNG，文件名 <image_id>.png（原始文件名记在 meta 里）
      scratch/            无归属的临时标注图（CLI detect / 模型自己调的 annotate_image）
      history/<run_id>/
        meta.json         记录元数据（schema v1，唯一真相来源）
        annotated.png     标注图

``` 下面这些约定是本模块对外承诺的行为，改代码前先读完：

- **run_id** = `YYYYMMDD-HHMMSS-<6位hex>`，定长且高位是时间 ⇒ **字典序 == 时间序**。
  列表排序、保留策略（删最老）都直接靠它，不必解析时间。同秒内多次调用靠「每秒一个随机起点 +
  进程内自增」保证不重复，同时保持生成顺序与字典序一致。
- **为什么不用内存缓存**：「持久化」的定义不是「存下来」，而是**重启后还能读回来**。
  所以 list_runs / load_run 每次都现场扫 runs/history/*/meta.json。
  一个几十条的目录扫盘代价可以忽略，换来的是进程重启、热重载、多 worker 下行为一致 ——
  这正是原来 IMAGES 内存字典栽跟头的地方。
- **单个坏记录不能炸整张列表**：meta.json 可能被截断（写盘中途断电）、被手改坏或版本不兼容。
  读不出来的记录跳过并计入 broken 计数，其余照常返回（历史面板不能因为一条脏数据整页空白）。
- **防目录穿越**：run_id 只允许 `^[0-9]{8}-[0-9]{6}-[0-9a-f]{6}$`。
  不匹配一律当作「不存在」，绝不拿未验证的拼接路径去 unlink/rmtree。
- **缩略图不落盘**：`?w=` 现场缩图并放进内存 LRU（缓存键含 mtime），不写磁盘 ——
  落盘又会攒出一堆没人清理的缩略图文件，正是本模块要根治的问题。

@doc AGENTS.md#4.9-上传--结果存储--历史记录
（该文档解决「目录布局、meta schema、HTTP 接口分别是什么」的问题。）
"""

from __future__ import annotations

import hashlib
import io
import json
import random
import re
import shutil
import threading
import time
from collections import OrderedDict
from pathlib import Path
from typing import Any, Iterable

from PIL import Image

from objloc import config

# --------------------------------------------------------------------------- #
# 常量
# --------------------------------------------------------------------------- #

# 记录 schema 版本：字段增删改语义时 +1（前端可据此做兼容分支）
SCHEMA_VERSION = 1
APP_VERSION = "2.0"

# run_id 的唯一合法形状。⚠️ 所有对外接收 run_id 的入口（list/load/delete/url 拼接）
# 都必须先过这一关，否则 "../../etc/passwd" 这类输入会变成真实的删除目标。
RUN_ID_RE = re.compile(r"^[0-9]{8}-[0-9]{6}-[0-9a-f]{6}$")

# 缩略图 LRU 容量。列表一页默认 50 条，留一倍余量即可；每张缩略图几十 KB，压力很小。
THUMB_CACHE_SIZE = 64
THUMB_DEFAULT_WIDTH = 160

# 生成的 run_id 序列状态（见 new_run_id）
_seq_lock = threading.Lock()
_seq_stamp: str | None = None      # 当前这一秒的时间戳前缀
_seq_start = 0                     # 本秒的随机起点
_seq_count = 0                     # 本秒已发出的条数

# 缩略图 LRU。用 OrderedDict + Lock 而不是 functools.lru_cache：
# lru_cache 的键必须是可哈希的不可变值，而「文件 mtime」这种随时间变化的键会不断产生新条目，
# 旧条目又永远命中不到 —— 等于内存泄漏。这里显式控制容量与淘汰。
_thumb_cache: "OrderedDict[tuple, bytes]" = OrderedDict()
_thumb_lock = threading.Lock()


# --------------------------------------------------------------------------- #
# 路径解析（一律**动态**读 config.*，不要在导入期绑定常量）
# --------------------------------------------------------------------------- #
# 为什么动态读：测试要把这些目录指向 tempfile 临时目录，只要改 objloc.config 上的模块级常量
# （或给 storage 一个可注入的根）就能生效；如果写成 from objloc.config import HISTORY_DIR，
# 导入期就被固化成真路径，测试产物会漏进项目的 runs/。
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


# --------------------------------------------------------------------------- #
# run_id 生成
# --------------------------------------------------------------------------- #
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


# --------------------------------------------------------------------------- #
# 写入
# --------------------------------------------------------------------------- #
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


# --------------------------------------------------------------------------- #
# 读取（每次都现场扫盘，无内存缓存 —— 见模块头注释）
# --------------------------------------------------------------------------- #
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


# --------------------------------------------------------------------------- #
# 删除 / 保留策略
# --------------------------------------------------------------------------- #
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
    try:
        shutil.rmtree(directory)
    except OSError:
        return False
    return True


def clear_runs() -> int:
    """清空全部历史记录，返回删除条数（不动 uploads/，源图由 gc 负责）。"""
    root = history_dir()
    if not root.exists():
        return 0
    removed = 0
    for entry in list(root.iterdir()):
        if not entry.is_dir() or not is_valid_run_id(entry.name):
            continue  # 目录里若有别的东西，不碰
        try:
            shutil.rmtree(entry)
            removed += 1
        except OSError:
            pass
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


# --------------------------------------------------------------------------- #
# 缩略图（现场缩放 + 内存 LRU；不落盘）
# --------------------------------------------------------------------------- #
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
