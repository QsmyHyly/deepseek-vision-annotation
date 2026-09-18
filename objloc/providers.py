"""统一的模型客户端抽象。

对外只暴露一种「流式事件」迭代器，屏蔽底层 OpenAI/DeepSeek SDK 的细节：

    for event in client.stream_chat(messages, tools):
        event["type"] in {"reasoning", "content", "tool_call_delta", "finish"}

事件类型：
- reasoning      : 思考过程增量（DeepSeek 思考模式 / reasoning_content）
- content        : 正文增量
- tool_call_delta: 工具调用参数增量（按 index 聚合后即一次完整调用）
- finish         : 本轮结束，携带 finish_reason

上层 agent.py 负责把这些增量拼装成 assistant 消息，并在有 tool_calls 时执行工具。

思考模式（thinking）由 stream_chat 的 thinking / reasoning_effort 两个关键字参数控制，
取值来自 objloc.config.Settings（THINKING / REASONING_EFFORT 环境变量），
也可以由调用方按次覆盖（网页上的「思考模式」开关就是这么传下来的）。
关闭后服务端不再产生 reasoning_content，因此 reasoning 事件自然消失。

@doc docs/DeepSeek-Thinking-Mode.md#思考模式开关与思考强度控制
（该文档解决"thinking 参数为什么必须走 extra_body、effort 各档怎么映射"的问题。）

@doc docs/DeepSeek-Chat-Completions-API.md#请求
（该文档解决"请求体有哪些字段、流式 chunk 长什么样"的问题，本模块按它拼装与解析。）
"""

from __future__ import annotations

import json
from typing import Any, Iterator, Protocol

# 0.1.3 起，thinking 的合并规则由库提供（此前本项目与库各写了一份，规则已经开始漂移）。
# 这是本项目第一次依赖 qsmy-deepseek-locator —— 关系从「库单向抽取自本项目」变成了双向共用，
# 依赖方向没有反转：库不依赖本项目，只是把两边共用的规则收拢到一处。
from qsmy_deepseek_locator import merge_thinking

from objloc.config import (
    IMAGE_DETAILS,
    Settings,
    get_settings,
    thinking_payload,
)
from objloc.parsing import decode_json_points


class ChatClient(Protocol):
    """模型客户端协议。"""

    name: str

    def stream_chat(
        self,
        messages: list[dict],
        tools: list[dict] | None = None,
        *,
        thinking: bool | None = None,
        reasoning_effort: str | None = None,
    ) -> Iterator[dict]:
        ...


def image_part(url: str, detail: str | None = None) -> dict:
    """构造一条 image_url 内容块，可选带上 detail 字段。

    Args:
        url: http(s) 外链或 data URL。
        detail: low / high / original / auto（见 config.IMAGE_DETAILS）。
            空串、None 或不认识的值一律**不带该字段**，交回服务端按 auto 处理。

    为什么非法值不报错：这个函数在"拼一次识别请求"的主路径上，为它抛异常会让整轮识别挂掉，
    而写错一个 detail 最多只是"没生效"。需要严格校验的是持久化那一步 ——
    config.local.json 与 PATCH /api/settings 那边会拒绝非法值（objloc/userprefs.py）。
    """
    payload: dict[str, Any] = {"url": url}
    if detail and detail in IMAGE_DETAILS:
        payload["detail"] = detail
    return {"type": "image_url", "image_url": payload}


def resolve_thinking(
    settings: Settings, thinking: bool | None, reasoning_effort: str | None = None
) -> tuple[bool, str | None]:
    """把「按次覆盖」与「配置默认值」合并成最终生效的 (是否思考, 思考强度)。

    真实客户端与 Mock 共用这一份实现，避免两边语义漂移
    （曾经各写一份，Mock 漏掉了强度合法性校验）。

    思考强度只在开启思考时才可能有值，且必须是 REASONING_EFFORTS 里的合法项，
    否则返回 None —— 表示不传该参数，交由服务端按默认 high 处理，
    这样非法输入不会变成 400，也不会悄悄改变模型行为。
    """
    # 规则本体在库里（0.1.3 上游化）：本函数此前与库各持一份，两份规则**并不严格等价** ——
    # 本项目的写法把「默认值为 None」也当成关闭，库把 None 当成「两样都不传」。
    # 这个项目恰好不会触发差异（settings.thinking 是纯 bool），但规则重复本身就是下一次漂移的入口。
    enabled, effort = merge_thinking(
        thinking,
        reasoning_effort,
        default_thinking=settings.thinking,
        default_effort=settings.reasoning_effort,
    )
    # 本项目口径与库仍有一处刻意的不同：这里的「没表态」按关闭处理（返回 bool 而非 None），
    # 因为下游消费方（extra_body）只需要 enabled/disabled 两种状态。
    return (False if enabled is None else enabled), effort


# --------------------------------------------------------------------------- #
# OpenAI 兼容客户端（DeepSeek 使用 OpenAI 兼容协议）
# --------------------------------------------------------------------------- #
class OpenAICompatClient:
    """基于 openai SDK 的流式客户端，兼容 DeepSeek / OpenAI 等接口。"""

    def __init__(self, settings: Settings | None = None):
        self.settings = settings or get_settings()
        self.name = self.settings.resolved_provider()
        self._client = None

    @property
    def client(self):
        if self._client is None:
            from openai import OpenAI

            self._client = OpenAI(
                api_key=self.settings.api_key,
                base_url=self.settings.base_url,
                timeout=self.settings.request_timeout,
            )
        return self._client

    def resolve_thinking(
        self, thinking: bool | None, reasoning_effort: str | None = None
    ) -> tuple[bool, str | None]:
        """按次覆盖与配置默认值的合并逻辑，见模块级 resolve_thinking()。"""
        return resolve_thinking(self.settings, thinking, reasoning_effort)

    def stream_chat(
        self,
        messages: list[dict],
        tools: list[dict] | None = None,
        *,
        thinking: bool | None = None,
        reasoning_effort: str | None = None,
    ) -> Iterator[dict]:
        enabled, effort = self.resolve_thinking(thinking, reasoning_effort)

        kwargs: dict[str, Any] = {
            "model": self.settings.model,
            "messages": messages,
            "stream": True,
            # thinking 不是 Chat Completions 的顶层字段，必须走 extra_body 才能传下去
            "extra_body": thinking_payload(enabled),
        }
        if effort:
            kwargs["reasoning_effort"] = effort
        if tools:
            kwargs["tools"] = tools
            kwargs["tool_choice"] = "auto"

        stream = self.client.chat.completions.create(**kwargs)

        for chunk in stream:
            if not chunk.choices:
                continue
            choice = chunk.choices[0]
            delta = choice.delta

            reasoning = getattr(delta, "reasoning_content", None)
            if reasoning:
                yield {"type": "reasoning", "text": reasoning}

            content = getattr(delta, "content", None)
            if content:
                yield {"type": "content", "text": content}

            for tc in getattr(delta, "tool_calls", None) or []:
                fn = getattr(tc, "function", None)
                yield {
                    "type": "tool_call_delta",
                    "index": getattr(tc, "index", 0),
                    "id": getattr(tc, "id", None),
                    "name": getattr(fn, "name", None) if fn else None,
                    "arguments": getattr(fn, "arguments", None) if fn else None,
                }

            if choice.finish_reason:
                yield {"type": "finish", "reason": choice.finish_reason}


# --------------------------------------------------------------------------- #
# 离线 Mock 客户端（无 API Key 时用于演示流式 + 工具执行链路）
# --------------------------------------------------------------------------- #
class MockVisionClient:
    """离线演示客户端。

    用于在没有 DeepSeek API Key / 没有网络时，仍然可以完整体验：
    流式输出 -> 模型请求调用工具 -> 框架执行工具 -> 回填结果 -> 继续流式输出。
    """

    name = "mock"

    def __init__(self, settings: Settings | None = None):
        self.settings = settings or get_settings()

    def _tool_call_round(self, messages: list[dict]) -> dict:
        """构造一轮「模型要求调用 parse_coordinates 工具」的响应。"""
        # 只从 user 消息里找坐标（跳过 system，避免匹配到提示词里的 schema 示例）
        raw = ""
        for msg in reversed(messages):
            if msg.get("role") != "user":
                continue
            content = msg.get("content")
            if isinstance(content, list):
                text = "\n".join(
                    part.get("text", "")
                    for part in content
                    if isinstance(part, dict) and part.get("type") == "text"
                )
            else:
                text = content or ""
            # 用真实解析器验证这段文本确实含有坐标
            if text and decode_json_points(text):
                raw = text
                break

        if not raw:
            # 没有可用坐标时给出示例，保证离线演示仍能看到效果
            # （坐标必须是 0.0~1.0 相对比例，见 AGENTS.md#4.3）
            raw = '[{"bbox_2d": [0.1, 0.12, 0.52, 0.64], "label": "示例目标"}, ' \
                  '{"point_2d": [0.7, 0.2], "label": "示例点位"}]'

        return {
            "reasoning": "我需要先把候选坐标整理成结构化数据，因此调用 parse_coordinates 工具。",
            "content": "",
            "tool_calls": [
                {
                    "id": "call_mock_1",
                    "name": "parse_coordinates",
                    "arguments": json.dumps({"text": raw}, ensure_ascii=False),
                }
            ],
        }

    def _final_round(self, messages: list[dict]) -> dict:
        tool_payload = ""
        for msg in reversed(messages):
            if msg.get("role") == "tool":
                tool_payload = str(msg.get("content", ""))
                break

        try:
            parsed = json.loads(tool_payload)
            bboxes = parsed.get("bboxes", [])
            labels = parsed.get("bbox_labels", [])
        except Exception:
            bboxes, labels = [], []

        summary = "已完成坐标解析与校验。"
        if bboxes:
            named = "、".join(str(x) for x in labels if x)
            summary += f"共识别 {len(bboxes)} 个目标" + (f"：{named}。" if named else "。")
        summary += "（当前为离线 Mock 模式，未调用真实模型）"

        return {
            "reasoning": "工具已返回结构化坐标，可以据此生成标注结果。",
            "content": summary,
            "tool_calls": [],
        }

    def resolve_thinking(
        self, thinking: bool | None, reasoning_effort: str | None = None
    ) -> tuple[bool, str | None]:
        """与 OpenAICompatClient 同一份实现（Mock 只是不真的消费 effort）。"""
        return resolve_thinking(self.settings, thinking, reasoning_effort)

    def stream_chat(
        self,
        messages: list[dict],
        tools: list[dict] | None = None,
        *,
        thinking: bool | None = None,
        reasoning_effort: str | None = None,
    ) -> Iterator[dict]:
        import time

        enabled, _effort = self.resolve_thinking(thinking, reasoning_effort)

        has_tool_result = any(m.get("role") == "tool" for m in messages)
        stage = self._final_round(messages) if has_tool_result else self._tool_call_round(messages)

        # 模拟思考流；关闭思考模式时真实接口不会返回 reasoning_content，这里同样跳过，
        # 让「思考开关」在离线 Mock 下也有可见区别。
        if enabled:
            for ch in _chunks(stage["reasoning"], 6):
                yield {"type": "reasoning", "text": ch}
                time.sleep(0.02)

        # 模拟正文流
        for ch in _chunks(stage["content"], 4):
            yield {"type": "content", "text": ch}
            time.sleep(0.02)

        # 模拟工具调用增量
        for tc in stage["tool_calls"]:
            args = tc["arguments"]
            yield {"type": "tool_call_delta", "index": 0, "id": tc["id"], "name": tc["name"], "arguments": ""}
            step = 24
            for i in range(0, len(args), step):
                yield {"type": "tool_call_delta", "index": 0, "id": None, "name": None, "arguments": args[i:i + step]}

        yield {"type": "finish", "reason": "tool_calls" if stage["tool_calls"] else "stop"}


def _chunks(text: str, size: int) -> Iterator[str]:
    for i in range(0, len(text), size):
        yield text[i:i + size]


def build_client(settings: Settings | None = None) -> ChatClient:
    """按配置创建客户端。无 Key 时回退到 Mock，保证离线可演示。"""
    settings = settings or get_settings()
    provider = settings.resolved_provider()
    if provider == "mock":
        return MockVisionClient(settings)
    return OpenAICompatClient(settings)
