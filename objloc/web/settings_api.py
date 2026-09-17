# -*- coding: utf-8 -*-
"""用户偏好路由 —— 持久化在项目根 config.local.json 的那六项识别参数。

职责
----
GET / PATCH / DELETE /api/settings 三个接口，外加"非法值 -> 400"的错误码翻译。

边界
----
规格、优先级、原子写、读侧容错全在 objloc/userprefs.py；本模块**不做校验**，
只把 PrefsError 翻成 HTTP 400。改校验规则请改 userprefs，别在这里加第二份。

旧文件对应关系（等价重构）
--------------------------
本模块 = 拆分前 objloc/web/app.py 的「用户偏好」一节，逐行搬移；
装饰器由 @app.* 机械改成 @router.*。

@doc AGENTS.md#4.10-持久化用户偏好configlocaljson
（该文档解决「哪些设置能长期保存、存在哪、优先级怎么排」的问题。）
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException

from objloc import userprefs

router = APIRouter()

# --------------------------------------------------------------------------- #
# 用户偏好：持久化在项目根的 config.local.json
# 规格、优先级与容错口径全在 objloc/userprefs.py，本层只做 HTTP 包装 + 错误码翻译。
# @doc AGENTS.md#4.10-持久化用户偏好configlocaljson
# （该文档解决「哪些设置能长期保存、存在哪、优先级怎么排」的问题。）
# --------------------------------------------------------------------------- #
@router.get("/api/settings")
def read_settings() -> dict:
    """当前生效的全部偏好：值 / 来源 / 内置默认 / 可选范围。

    「来源」（file / env / default）是给界面看的：环境变量 THINKING=0 与配置文件里的
    thinking 会打架，界面必须能说清这个值到底是谁定的，否则用户会以为「保存没生效」。
    """
    return userprefs.effective()


@router.patch("/api/settings")
def update_settings(payload: dict) -> dict:
    """按 key 合并保存若干项偏好（部分更新），返回保存后的完整快照。

    保存后 config.get_settings() 会因为文件 mtime 变化自动重建单例（见 objloc/config.py），
    所以**下一次识别立刻用新设置，不必重启服务** —— 这正是这个接口存在的意义。
    """
    try:
        return userprefs.save(payload or {})
    except userprefs.PrefsError as exc:
        # 值非法 = 用户输入问题，400 而不是 500；detail 里带上「哪一项、哪里不对」
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.delete("/api/settings")
def reset_settings(keys: str | None = None) -> dict:
    """删掉偏好键，让它们回落到「环境变量 > 内置默认」。

    keys 用逗号分隔（?keys=thinking,prompt），省略 = 全部重置。
    这是「配置文件盖住了环境变量」的唯一出口：删掉键，环境变量就重新生效。
    """
    parsed = [k.strip() for k in keys.split(",") if k.strip()] if keys else None
    try:
        return userprefs.reset(parsed)
    except userprefs.PrefsError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
