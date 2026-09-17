# -*- coding: utf-8 -*-
"""持久化的用户偏好：项目根目录下的 config.local.json。

**它解决什么问题**：网页「识别参数」里那些开关（思考模式 / 思考强度 / 工具调用 / 提示词）
原本只活在浏览器的 localStorage 里 —— 换个浏览器、清一次缓存就没了，而且**服务端自己看不到**：
CLI、评测脚本、以及 /api/detect 省略字段时的兜底值，用的仍是环境变量那一套。
本模块把这份「用户点过的选择」落成项目根下的一个 JSON 文件：重启服务还在、换浏览器还在、
能手改、CLI 也能读到。

**优先级（低 → 高）**：内置默认 < 环境变量 < config.local.json < 单次请求里的字段。

    配置文件排在环境变量**之上**，是因为这一层的语义是「用户明确点过的开关」，
    而环境变量是「部署方的默认值」—— 用户点过就该算数，否则网页上改了没反应。
    代价要说清：THINKING=0 这类环境变量会被文件里的同名键盖住。
    想让它重新生效，走 DELETE /api/settings 把键删掉（或直接手删文件里那一行）。

**只认文件里「显式出现过」的键**：没写的键一律回落到环境变量 / 内置默认。
所以「从来没碰过设置」与「把设置改回默认值」是两件事，前者完全不受本文件影响 ——
这也保证了新增这项功能对老用法 100% 向后兼容。

**原子写**：先写 <文件名>.tmp 再 os.replace() 覆盖。中途崩溃最多丢掉这一次修改，
不会留下半截 JSON 让下次启动解析失败（读侧同样容错：文件损坏 = 全部回落默认 + 一条警告）。

**读盘按 mtime 缓存**：手改文件后不必重启服务 —— objloc/config.py: get_settings() 每次
比对 stamp()，戳记变了就重建 Settings 单例。

**未知键原样保留**：文件里出现本模块不认识的键时，不删（手写注释性的键、或未来版本写的键
不该被这一版抹掉），只在警告里点名，避免把拼错的键当成「生效了」。

@doc AGENTS.md#410-持久化用户偏好configlocaljson
（该文档解决「哪些设置能长期保存、存在哪、优先级怎么排」的问题。）
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any


class PrefsError(ValueError):
    """偏好值非法（未知键 / 类型不对 / 超范围）。HTTP 层应当把它转成 400。"""


@dataclass(frozen=True)
class Spec:
    """一项可持久化偏好的声明。

    这张表既是**校验依据**，也是 GET /api/settings 自描述的依据 ——
    前端不必把取值范围再抄一份，照它渲染即可（本仓库有过教训：前后端各写一份选项列表，
    后端加了新的 effort 档位、前端下拉框里没有，两边对不上）。
    """

    key: str
    kind: str                      # bool / int / str / enum
    default: Any
    label: str                     # 给界面看的中文名
    env: str | None = None         # 对应的环境变量名（None = 只能用配置文件改）
    choices: tuple[str, ...] = ()  # kind == "enum" 时的合法取值
    minimum: int | None = None     # kind == "int" 时的下界
    maximum: int | None = None
    max_len: int | None = None     # kind == "str" 时的长度上限
    ui: bool = True                # 是否在网页「识别参数」卡片里暴露


# 规格表**延迟构造**：reasoning_effort 的合法取值 REASONING_EFFORTS 定义在 objloc/config.py，
# 而 config.py 又要反过来 import 本模块（Settings 的字段从这里取值），模块级 import 会成环。
# 放到函数里 import —— 等到真正调用时 config 早已加载完毕。
_SPECS: dict[str, Spec] | None = None


def _specs() -> dict[str, Spec]:
    """返回全部可持久化偏好的规格（首次调用时构造，之后复用）。"""
    global _SPECS
    if _SPECS is None:
        from objloc import config

        _SPECS = {s.key: s for s in (
            Spec(
                key="thinking", kind="bool", default=True, env="THINKING", label="思考模式",
            ),
            Spec(
                key="reasoning_effort", kind="enum", default="", env="REASONING_EFFORT",
                label="思考强度", choices=("",) + tuple(sorted(config.REASONING_EFFORTS)),
            ),
            Spec(
                key="use_tools", kind="bool", default=True, env="USE_TOOLS",
                label="工具执行框架",
            ),
            Spec(
                key="max_tool_rounds", kind="int", default=8, env="MAX_TOOL_ROUNDS",
                label="工具调用最大轮数", minimum=1, maximum=64,
            ),
            # 合法取值引用 config.IMAGE_DETAILS，别在这里再抄一份（同 reasoning_effort 的写法）
            Spec(
                key="image_detail", kind="enum", default="", env="IMAGE_DETAIL",
                label="图片输入精度", choices=("",) + tuple(config.IMAGE_DETAILS),
            ),
            Spec(
                key="prompt", kind="str", default="", label="提示词", max_len=4000,
            ),
            # system_prompt 刻意不标 ui：坐标口径就写在里面（objloc/config.py: DEFAULT_SYSTEM_PROMPT），
            # 让网页随手改会把全项目的坐标约定改坏（见 AGENTS.md#4.3-坐标约定）。
            Spec(
                key="system_prompt", kind="str", default=None, env="SYSTEM_PROMPT",
                label="系统提示词", max_len=8000, ui=False,
            ),
        )}
    return _SPECS


# --------------------------------------------------------------------------- #
# 取值 / 校验
# --------------------------------------------------------------------------- #
_TRUE = {"1", "true", "yes", "y", "on"}
_FALSE = {"0", "false", "no", "n", "off"}


def _as_bool(raw: Any) -> bool:
    if isinstance(raw, bool):
        return raw
    text = str(raw).strip().lower()
    if text in _TRUE:
        return True
    if text in _FALSE:
        return False
    raise ValueError("需要 true / false")


def _coerce(spec: Spec, raw: Any, *, origin: str) -> Any:
    """把来源不一的原始值（JSON 值 / 环境变量字符串 / 请求体）校验成规范值。

    Args:
        origin: 出错信息里用的来源说明（"配置文件" / 环境变量名 / "请求体"）——
            只有错误信息用得到，但很关键：调用方需要知道该去哪一行改。

    Raises:
        ValueError: 类型不对或超出范围。
    """
    where = f"{spec.label}（{spec.key}，来自{origin}）"
    if spec.kind == "bool":
        try:
            return _as_bool(raw)
        except ValueError:
            raise ValueError(f"{where} 不是合法的布尔值，只接受 true/false") from None
    if spec.kind == "int":
        # bool 是 int 的子类，True 会被 int(str(True)) 拒掉但 int(True) 不会，这里显式挡掉
        if isinstance(raw, bool):
            raise ValueError(f"{where} 不是整数")
        try:
            value = int(str(raw).strip())
        except (TypeError, ValueError):
            raise ValueError(f"{where} 不是整数") from None
        if spec.minimum is not None and value < spec.minimum:
            raise ValueError(f"{where} 不能小于 {spec.minimum}")
        if spec.maximum is not None and value > spec.maximum:
            raise ValueError(f"{where} 不能大于 {spec.maximum}")
        return value
    if spec.kind == "enum":
        value = str(raw).strip().lower()
        if value not in spec.choices:
            allowed = " / ".join(("（空 = 服务端默认）",) + tuple(c for c in spec.choices if c))
            raise ValueError(f"{where} 不在合法取值内：{allowed}")
        return value
    # kind == "str"
    if not isinstance(raw, str):
        raise ValueError(f"{where} 需要字符串")
    if spec.max_len is not None and len(raw) > spec.max_len:
        raise ValueError(f"{where} 超过 {spec.max_len} 字")
    return raw


# --------------------------------------------------------------------------- #
# 位置与戳记
# --------------------------------------------------------------------------- #
def prefs_path() -> Path:
    """配置文件路径。**每次调用现取** config.PREFS_PATH，便于测试改指临时目录
    （与 objloc/storage/ 动态读 config.RUNS_DIR 的约定一致）。
    """
    from objloc import config

    return Path(config.PREFS_PATH)


def stamp() -> tuple[int, int] | None:
    """配置文件的戳记 (mtime_ns, size)；文件不存在返回 None。

    objloc/config.py: get_settings() 靠它判断「手改过文件没有」，因此这里必须只是
    **一次 stat**，不能顺手把内容也读进来。
    """
    try:
        st = prefs_path().stat()
    except OSError:
        return None
    return (st.st_mtime_ns, st.st_size)


# --------------------------------------------------------------------------- #
# 读
# --------------------------------------------------------------------------- #
_raw_cache: dict[str, Any] | None = None
_raw_cache_stamp: tuple[int, int] | None = None
_raw_cache_warnings: list[str] = []


def _load_raw() -> tuple[dict[str, Any], list[str]]:
    """读盘并按 mtime 缓存，返回 (原始 dict, 警告列表)。

    容错口径：文件不存在 = 空配置（不是错误）；文件损坏 = 空配置 + 一条警告。
    **绝不抛异常** —— 一个坏掉的偏好文件不该让整个服务起不来。
    """
    global _raw_cache, _raw_cache_stamp, _raw_cache_warnings
    now = stamp()
    if _raw_cache is not None and now == _raw_cache_stamp:
        return _raw_cache, _raw_cache_warnings

    data: dict[str, Any] = {}
    warnings: list[str] = []
    path = prefs_path()
    if now is not None:
        try:
            loaded = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            warnings.append(f"偏好文件 {path.name} 读不出来（{exc}），本次全部改用内置默认值。")
        else:
            if isinstance(loaded, dict):
                data = loaded
            else:
                warnings.append(f"偏好文件 {path.name} 的顶层不是一个 JSON 对象，已忽略。")

    _raw_cache, _raw_cache_stamp, _raw_cache_warnings = data, now, warnings
    return data, warnings


def file_values() -> dict[str, Any]:
    """配置文件里**显式出现且合法**的键值对。非法项被跳过（警告见 warnings()）。"""
    data, _w = _load_raw()
    specs = _specs()
    out: dict[str, Any] = {}
    for key, raw in data.items():
        spec = specs.get(key)
        if spec is None:
            continue
        try:
            out[key] = _coerce(spec, raw, origin="配置文件")
        except ValueError:
            continue
    return out


def warnings() -> list[str]:
    """配置文件的全部毛病：解析失败 / 顶层不是对象 / 非法值 / 未知键。"""
    data, msgs = _load_raw()
    specs = _specs()
    issues = list(msgs)
    for key, raw in data.items():
        spec = specs.get(key)
        if spec is None:
            issues.append(f"偏好文件里的 {key!r} 不是已知设置项，已忽略（保留原样，未被覆盖）。")
            continue
        try:
            _coerce(spec, raw, origin="配置文件")
        except ValueError as exc:
            issues.append(f"{exc}，已改用下一层来源。")
    return issues


def resolve(key: str) -> tuple[Any, str]:
    """按 配置文件 > 环境变量 > 内置默认 解析一项偏好，返回 (值, 来源)。

    来源取值："file" / "env" / "default" —— 界面必须把它显示出来，
    否则用户会以为「文件里改了却没反应」，其实是被更上层的来源盖着。
    """
    specs = _specs()
    spec = specs.get(key)
    if spec is None:
        raise KeyError(key)

    values = file_values()
    if key in values:
        return values[key], "file"

    if spec.env:
        raw = os.getenv(spec.env)
        # 空字符串 = 没设（把环境变量设成空是常见的「清空」手法，不该覆盖默认值）
        if raw is not None and raw.strip() != "":
            try:
                return _coerce(spec, raw, origin=f"环境变量 {spec.env}"), "env"
            except ValueError:
                pass
    return spec.default, "default"


def effective() -> dict[str, Any]:
    """给界面用的完整快照：每项的当前值、来源、内置默认、可选范围。

    界面靠它一次渲染到位（不必把取值范围在前端再抄一份），也靠它显示「这个值是从哪来的」。
    """
    specs = _specs()
    values: dict[str, Any] = {}
    sources: dict[str, str] = {}
    for key in specs:
        value, source = resolve(key)
        # system_prompt 的内置默认在 config.py（DEFAULT_SYSTEM_PROMPT），这里给不出具体文本，
        # 空串代替即可，免得前端显示成 null 让人以为坏了
        values[key] = value if value is not None else ""
        sources[key] = source
    return {
        "path": str(prefs_path()),
        "exists": stamp() is not None,
        "values": values,
        "sources": sources,
        "defaults": {k: ("" if s.default is None else s.default) for k, s in specs.items()},
        "meta": {
            k: {
                "label": s.label, "kind": s.kind, "env": s.env, "ui": s.ui,
                "choices": list(s.choices), "minimum": s.minimum, "maximum": s.maximum,
                "max_len": s.max_len,
            }
            for k, s in specs.items()
        },
        "warnings": warnings(),
    }


# --------------------------------------------------------------------------- #
# 写
# --------------------------------------------------------------------------- #
def _write(data: dict[str, Any]) -> None:
    """原子写：先写同目录的 .tmp，再 os.replace 覆盖（同盘 rename 是原子操作）。"""
    path = prefs_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(
        json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    os.replace(tmp, path)


def save(patch: dict[str, Any]) -> dict[str, Any]:
    """校验并合并写入若干项偏好，返回写入后的 effective() 快照。

    **先整体校验、再落盘**：一半合法一半非法的请求必须一项都不写 ——
    否则用户看到 400，其中一半却已经改了，这比直接失败更难排查。

    文件里已有的未知键原样保留（见模块头注释）。

    Raises:
        PrefsError: 请求体不是对象、为空、含未知键，或有值不合法。
    """
    if not isinstance(patch, dict) or not patch:
        raise PrefsError('请求体必须是一个非空 JSON 对象，例如 {"thinking": false}')
    specs = _specs()
    cleaned: dict[str, Any] = {}
    errors: list[str] = []
    unknown: list[str] = []
    for key, raw in patch.items():
        spec = specs.get(key)
        if spec is None:
            unknown.append(str(key))
            continue
        try:
            cleaned[key] = _coerce(spec, raw, origin="请求体")
        except ValueError as exc:
            errors.append(str(exc))
    if unknown:
        errors.append(f"未知设置项：{', '.join(sorted(unknown))}")
    if errors:
        raise PrefsError("；".join(errors))

    current, _w = _load_raw()
    _write({**current, **cleaned})
    return effective()


def reset(keys: list[str] | None = None) -> dict[str, Any]:
    """删掉文件里的若干键（None = 全部已知键），让它们回落到环境变量 / 内置默认。

    文件因此变空时**连着文件一起删掉** —— 留一个空的 {} 会让「到底配过没有」变得含糊。

    Raises:
        PrefsError: keys 里出现了未知的设置项名。
    """
    specs = _specs()
    if keys is None:
        keys = list(specs)
    for key in keys:
        if key not in specs:
            raise PrefsError(f"未知设置项：{key}")

    current, _w = _load_raw()
    remaining = {k: v for k, v in current.items() if k not in keys}
    if remaining:
        _write(remaining)
    else:
        try:
            prefs_path().unlink()
        except OSError:
            pass
    return effective()
