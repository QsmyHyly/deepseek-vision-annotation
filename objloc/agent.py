# -*- coding: utf-8 -*-
"""Agent 主循环 —— 实现已上游到 qsmy-deepseek-locator（0.2.0），这里只做转出与适配。

为什么改成薄层
--------------
工具循环（多轮调用 + 工具执行）是本项目、库、安卓 App 三边共用的一套编排逻辑：
事件协议一样、工具框架一样、连「工具报错要回填给模型而不是抛穿」这种细节都一样。
三份实现就是三次改漏的机会，所以规则本体搬进了库的 `qsmy_deepseek_locator.agent`。

本文件留下两件本项目独有的事（库不该知道的事）：

1. **客户端适配**（`_ClientAdapter`）：本项目的 ChatClient 是 `stream_chat(messages,
   tools=, thinking=, reasoning_effort=)`（按次覆盖思考开关），库期望的是
   `stream(messages, *, settings, tools, log)`（从 settings 读）。
2. **输出目录注入**：模型自己调的 annotate_image 落在本项目的 `runs/scratch/`
   （见 AGENTS.md#4.9）。库不认这个目录，所以由这里作为运行上下文注入。

⚠️ 事件名的一处**必须**转换：本项目的增量事件叫 `tool_call_delta`，库里叫 `tool_call`；
而库里 agent **对外** yield 的 `tool_call` 是**拼好的完整调用** —— 同名不同义。
不换名的话，agent 会把「完整的调用」当成「增量分片」再拼一次，参数直接变成垃圾。

@doc docs/DeepSeek-Tool-Calls.md#在对话中间插入工具调用
（该文档解决"assistant 消息里的 tool_calls 与 role=tool 结果该怎么排布"的问题 ——
本文件把客户端事件改名、库那一层再按 index 拼装，两处都按它的格式来。）

@doc docs/DeepSeek-Thinking-Mode.md#工具调用
（该文档解决"带 tools 时 reasoning_content 要不要回传给 API"的问题。）
"""

from __future__ import annotations

from typing import Any, Iterator

from qsmy_deepseek_locator.agent import (
    build_messages as _lib_build_messages,
    collect_final_text,
    collect_items,
    extract_items,
)
from qsmy_deepseek_locator.agent import run_agent as _lib_run_agent
from qsmy_deepseek_locator.config import Settings as _LibSettings

from objloc.config import get_settings
from objloc.providers import ChatClient, build_client
from objloc.visualizer import SCRATCH_DIR


class _ClientAdapter:
    """把本项目的 ChatClient 适配成库 agent 期望的客户端契约（见模块头注释）。"""

    def __init__(self, client: ChatClient) -> None:
        self._client = client

    def stream(self, messages, *, settings=None, tools=None, log=None) -> Iterator[dict]:
        thinking = None if settings is None else settings.thinking
        effort = None if settings is None else settings.reasoning_effort
        for event in self._client.stream_chat(
            messages, tools=tools, thinking=thinking, reasoning_effort=effort
        ):
            if event.get("type") == "tool_call_delta":
                yield {**event, "type": "tool_call"}   # 增量改名，见模块头注释
            else:
                yield event


def build_messages(
    prompt: str,
    *,
    image: str | None = None,
    system_prompt: str | None = None,
    image_detail: str | None = None,
    history: list[dict] | None = None,
) -> list[dict]:
    """拼装首轮消息列表（system + 可选的图片/文本 user 消息）。

    system 提示词与图片精度都**默认取本项目 Settings**（config.local.json > 环境变量 > 内置），
    这是本项目的老行为；库那一层是纯参数化的，默认值由调用方给。
    """
    settings = get_settings()
    return _lib_build_messages(
        prompt,
        image_url=image,
        system_prompt=system_prompt or settings.system_prompt,
        image_detail=image_detail if image_detail is not None else settings.image_detail,
        history=history,
    )


def run_agent(
    messages: list[dict],
    *,
    client: ChatClient | None = None,
    registry: Any | None = None,
    max_rounds: int | None = None,
    use_tools: bool = True,
    tool_context: dict | None = None,
    thinking: bool | None = None,
    reasoning_effort: str | None = None,
) -> Iterator[dict]:
    """执行 agent 循环，逐个产出事件字典（事件类型与字段与库完全一致）。

    tool_context 里会**自动补上本项目的 output_dir**（模型调的标注图落 runs/scratch/），
    调用方传的同名键优先。
    """
    settings = get_settings()
    client = client or build_client(settings)

    context = {"output_dir": str(SCRATCH_DIR)}
    context.update(tool_context or {})

    lib_settings = _LibSettings(
        thinking=settings.thinking,
        reasoning_effort=settings.reasoning_effort,
        max_tool_rounds=settings.max_tool_rounds,
        system_prompt=settings.system_prompt,
        image_detail=settings.image_detail,
    )
    yield from _lib_run_agent(
        messages,
        client=_ClientAdapter(client),
        registry=registry,
        settings=lib_settings,
        max_rounds=max_rounds,
        use_tools=use_tools,
        tool_context=context,
        thinking=thinking,
        reasoning_effort=reasoning_effort,
    )


def run_agent_simple(prompt: str, image: str | None = None, **kwargs) -> dict:
    """非流式便捷入口：执行完整 agent 循环并返回最终结果。"""
    messages = build_messages(prompt, image=image)
    events = list(run_agent(messages, **kwargs))
    done = next((e for e in events if e["type"] == "done"), None)
    text = done.get("content", "") if done else ""
    return {
        "events": events,
        "text": text,
        "items": extract_items(text),
        "messages": done.get("messages") if done else messages,
    }


__all__ = [
    "build_messages",
    "collect_final_text",
    "collect_items",
    "extract_items",
    "run_agent",
    "run_agent_simple",
]
