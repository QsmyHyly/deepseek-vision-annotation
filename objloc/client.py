"""DeepSeek 客户端（对外兼容层）。

新架构下，真正的客户端实现在 providers.py（统一流式事件协议），Agent 循环在
agent.py（流式 + 工具执行）。本模块保留为一个稳定、易用的门面，提供：

- stream_chat(...)        : 流式输出（生成器，逐段产出事件）
- stream_text(...)        : 只产出正文增量的最简流式封装
- infer(...)              : 非流式一次性调用
- inference_with_api(...) : 兼容旧版 deepseek41-vl-2d.py 的原始函数签名
"""

from __future__ import annotations

import os
from typing import Iterator

from openai import OpenAI

from objloc.config import DEFAULT_SYSTEM_PROMPT, get_settings
from objloc.providers import build_client


def _resolve_model(model_id: str | None) -> str:
    return model_id or get_settings().model


def _legacy_client() -> OpenAI:
    """保留旧版直接构造 OpenAI 客户端的行为（便于对照/兼容）。"""
    settings = get_settings()
    return OpenAI(
        api_key=settings.api_key or os.getenv("DEEPSEEK_API_KEY"),
        base_url=settings.base_url,
    )


# --------------------------------------------------------------------------- #
# 新的流式接口
# --------------------------------------------------------------------------- #
def stream_chat(
    messages: list[dict],
    *,
    tools: list[dict] | None = None,
    model_id: str | None = None,
    thinking: bool | None = None,
    reasoning_effort: str | None = None,
) -> Iterator[dict]:
    """流式调用，逐段产出事件（reasoning / content / tool_call_delta / finish）。

    这是本模块推荐使用的入口，配合 agent.run_agent 可实现「流式 + 工具执行」。

    Args:
        thinking: 是否开启思考模式（DeepSeek 的 reasoning_content）。
            None 表示沿用 Settings.thinking / THINKING 环境变量的默认值；
            传 False 可关闭思考，模型直接给正文（更快、更省 token）。
        reasoning_effort: 思考强度 low/medium/high/xhigh/max，仅思考开启时生效。
    """
    settings = get_settings()
    if model_id and model_id != settings.model:
        settings = type(settings)(**{**settings.__dict__, "model": model_id})
    client = build_client(settings)
    yield from client.stream_chat(
        messages, tools=tools, thinking=thinking, reasoning_effort=reasoning_effort
    )


def stream_text(
    image_url: str,
    prompt: str = "",
    *,
    sys_prompt: str | None = None,
    model_id: str | None = None,
    thinking: bool | None = None,
) -> Iterator[str]:
    """最简流式封装：只产出正文文本增量，方便直接打印。"""
    for event in stream_chat(
        _build_messages(image_url, prompt, sys_prompt),
        model_id=model_id,
        thinking=thinking,
    ):
        if event.get("type") == "content":
            yield event["text"]


# --------------------------------------------------------------------------- #
# 非流式接口
# --------------------------------------------------------------------------- #
def infer(
    image_url: str | None = None,
    prompt: str = "",
    *,
    sys_prompt: str | None = None,
    model_id: str | None = None,
    messages: list[dict] | None = None,
    tools: list[dict] | None = None,
) -> str:
    """非流式调用，返回模型文本。"""
    client = _legacy_client()
    msgs = messages or _build_messages(image_url, prompt, sys_prompt)
    kwargs: dict = {"model": _resolve_model(model_id), "messages": msgs}
    if tools:
        kwargs["tools"] = tools
    completion = client.chat.completions.create(**kwargs)
    return completion.choices[0].message.content or ""


def _build_messages(image_url: str | None, prompt: str, sys_prompt: str | None) -> list[dict]:
    messages = [
        {"role": "system", "content": sys_prompt or DEFAULT_SYSTEM_PROMPT}
    ]
    if image_url:
        messages.append({
            "role": "user",
            "content": [
                {"type": "image_url", "image_url": {"url": image_url}},
                {"type": "text", "text": prompt},
            ],
        })
    else:
        messages.append({"role": "user", "content": prompt})
    return messages


# --------------------------------------------------------------------------- #
# 旧版兼容
# --------------------------------------------------------------------------- #
def inference_with_api(image_url, prompt="",
                       sys_prompt=DEFAULT_SYSTEM_PROMPT,
                       # deepseek-flash = DeepSeek-V4.1-Flash；不要改成 deepseek-v4-pro，
                       # 那一档不支持图像理解（见 objloc/config.py 的同名说明）
                       model_id="deepseek-flash"):
    """调用 DeepSeek 多模态模型完成图像理解与目标定位（兼容旧签名）。

    Args:
        image_url: 图片地址，支持 http(s) 外链或 base64 data URL。
        prompt: 用户提示词，默认为空。
        sys_prompt: 系统提示词。
        model_id: 模型名称。

    Returns:
        模型输出的文本内容。
    """
    return infer(
        image_url=image_url,
        prompt=prompt,
        sys_prompt=sys_prompt,
        model_id=model_id,
    )


__all__ = [
    "stream_chat",
    "stream_text",
    "infer",
    "inference_with_api",
]
