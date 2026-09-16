"""Agent 主循环：流式输出 + 工具执行。

流程：
    1. 调用模型（stream=True），把 reasoning / content / tool_call 增量实时吐出；
    2. 若本轮出现 tool_calls，则交给 ToolRegistry 逐个执行，并把 role=tool 结果
       追加进消息历史，然后进入下一轮；
    3. 直到模型不再请求工具（finish_reason=stop）为止。

对外只暴露一个事件生成器 run_agent(...)，Web / CLI 都消费同一套事件，
因此流式展示与工具执行只有一份实现。

思考模式（thinking）：
    由 run_agent(thinking=...) 按次覆盖，缺省取 Settings.thinking；
    关闭后模型不再产出 reasoning 事件，正文照常。

@doc docs/DeepSeek-Thinking-Mode.md#工具调用
（该文档解决"带 tools 时 reasoning_content 要不要回传给 API"的问题。）

@doc docs/DeepSeek-Tool-Calls.md#在对话中间插入工具调用
（该文档解决"assistant 消息里的 tool_calls 与 role=tool 结果该怎么排布"的问题，
本模块第 2 步追加消息历史时按它的格式来。）
"""

from __future__ import annotations

import json
from typing import Any, Iterator

from objloc.config import get_settings
from objloc.parsing import decode_json_points, to_items
from objloc.providers import ChatClient, build_client
from objloc.tools import ToolRegistry, build_default_registry


def build_messages(
    prompt: str,
    *,
    image: str | None = None,
    system_prompt: str | None = None,
    history: list[dict] | None = None,
) -> list[dict]:
    """拼装首轮消息列表（system + 可选的图片/文本 user 消息）。"""
    settings = get_settings()
    messages: list[dict] = [
        {"role": "system", "content": system_prompt or settings.system_prompt}
    ]
    if history:
        messages.extend(history)

    if image:
        messages.append({
            "role": "user",
            "content": [
                {"type": "image_url", "image_url": {"url": image}},
                {"type": "text", "text": prompt or "请识别图中主要目标并输出坐标。"},
            ],
        })
    else:
        messages.append({"role": "user", "content": prompt})
    return messages


def _accumulate_tool_calls(pending: dict[int, dict]) -> list[dict]:
    """把流式增量拼装成 OpenAI 格式的 tool_calls 列表。"""
    calls = []
    for index in sorted(pending):
        entry = pending[index]
        if not entry.get("name"):
            continue
        calls.append({
            "id": entry.get("id") or f"call_{index}",
            "type": "function",
            "function": {
                "name": entry["name"],
                "arguments": entry.get("arguments") or "{}",
            },
        })
    return calls


def run_agent(
    messages: list[dict],
    *,
    client: ChatClient | None = None,
    registry: ToolRegistry | None = None,
    max_rounds: int | None = None,
    use_tools: bool = True,
    tool_context: dict | None = None,
    thinking: bool | None = None,
    reasoning_effort: str | None = None,
) -> Iterator[dict]:
    """执行 agent 循环，逐个产出事件字典。

    Args:
        tool_context: 运行上下文，用于注入工具中不暴露给模型的参数
            （如 {"source": 当前图片路径}）。
        thinking: 是否开启思考模式；None 表示用 Settings.thinking（THINKING 环境变量）。
            关闭后模型直接给正文，不再有 reasoning 事件。
        reasoning_effort: 思考强度（low/medium/high/xhigh/max），仅在思考开启时生效。

    事件类型：
        round_start / reasoning / content / tool_call / tool_result /
        message / done / error
    """
    settings = get_settings()
    client = client or build_client(settings)
    # 先解析成确定的布尔值，保证「真实客户端 / Mock 客户端 / 网络层」看到的是同一个决定
    thinking_enabled = settings.thinking if thinking is None else bool(thinking)
    registry = registry or build_default_registry()
    rounds = max_rounds or settings.max_tool_rounds
    tools = registry.spec() if use_tools else None

    final_content = ""
    for round_index in range(rounds):
        yield {"type": "round_start", "index": round_index, "thinking": thinking_enabled}

        content_parts: list[str] = []
        reasoning_parts: list[str] = []
        pending_tool_calls: dict[int, dict] = {}
        finish_reason: str | None = None

        try:
            for event in client.stream_chat(
                messages,
                tools=tools,
                thinking=thinking_enabled,
                reasoning_effort=reasoning_effort,
            ):
                etype = event.get("type")
                if etype == "reasoning":
                    reasoning_parts.append(event["text"])
                    yield {"type": "reasoning", "text": event["text"]}
                elif etype == "content":
                    content_parts.append(event["text"])
                    yield {"type": "content", "text": event["text"]}
                elif etype == "tool_call_delta":
                    idx = event.get("index", 0)
                    entry = pending_tool_calls.setdefault(
                        idx, {"id": "", "name": "", "arguments": ""}
                    )
                    if event.get("id"):
                        entry["id"] = event["id"]
                    if event.get("name"):
                        entry["name"] = event["name"]
                    if event.get("arguments"):
                        entry["arguments"] += event["arguments"]
                elif etype == "finish":
                    finish_reason = event.get("reason")
        except Exception as exc:  # noqa: BLE001 - 网络/SDK 异常需要反馈到前端
            yield {"type": "error", "message": f"模型调用失败：{exc}"}
            return

        content = "".join(content_parts)
        reasoning = "".join(reasoning_parts)
        tool_calls = _accumulate_tool_calls(pending_tool_calls)

        assistant_message: dict[str, Any] = {"role": "assistant", "content": content or None}
        # 思考模式下，带 tools 的请求要求把历史轮次的 reasoning_content 原样回传，
        # 否则模型会丢掉上一轮的思考上下文（官方文档甚至说会 400）。
        # 这里只在「确实有思考内容」时回传：关闭思考模式时本来就没有该字段，
        # 而不是补一个空串——空串是否被服务端接受没有实测依据，不冒这个险。
        if reasoning and thinking_enabled:
            assistant_message["reasoning_content"] = reasoning
        if tool_calls:
            assistant_message["tool_calls"] = tool_calls
        messages.append(assistant_message)
        yield {"type": "message", "message": assistant_message, "finish_reason": finish_reason}

        if not tool_calls:
            final_content = content
            yield {
                "type": "done",
                "reason": finish_reason or "stop",
                "content": final_content,
                "rounds": round_index + 1,
                "thinking": thinking_enabled,
                "messages": messages,
            }
            return

        # ---- 执行工具 ----
        for call in tool_calls:
            fn = call["function"]
            name = fn["name"]
            raw_args = fn["arguments"]
            yield {
                "type": "tool_call",
                "id": call["id"],
                "name": name,
                "arguments": raw_args,
            }
            result = registry.execute(name, raw_args, context=tool_context)
            yield {
                "type": "tool_result",
                "id": call["id"],
                "name": name,
                "ok": result.ok,
                "content": result.content,
                "elapsed_ms": round(result.elapsed_ms, 1),
            }
            messages.append({
                "role": "tool",
                "tool_call_id": call["id"],
                "content": result.content,
            })

    yield {
        "type": "done",
        "reason": "max_rounds",
        "content": final_content,
        "rounds": rounds,
        "thinking": thinking_enabled,
        "messages": messages,
    }


def extract_items(text: str) -> list[dict]:
    """从模型最终文本中抽取坐标对象列表，供标注使用。"""
    if not text:
        return []
    data = decode_json_points(text)
    if isinstance(data, dict):
        return [data]
    if isinstance(data, list):
        return [d for d in data if isinstance(d, dict)]
    return []


def collect_items(events: list[dict], final_text: str = "") -> list[dict]:
    """从事件流与最终文本中收集可标注的坐标对象。

    优先用最终正文里的 JSON；若没有，则回溯工具执行结果
    （模型可能把坐标交给 parse_coordinates 等工具处理）。
    """
    items = [d for d in extract_items(final_text) if "bbox_2d" in d or "point_2d" in d]
    if items:
        return items

    for event in reversed(events):
        if event.get("type") != "tool_result" or not event.get("ok"):
            continue
        try:
            data = json.loads(event.get("content") or "")
        except Exception:  # noqa: BLE001
            continue
        got = [d for d in to_items(data) if "bbox_2d" in d or "point_2d" in d]
        if got:
            return got
    return []


def collect_final_text(messages: list[dict]) -> str:
    """取最后一条 assistant 正文。"""
    for msg in reversed(messages):
        if msg.get("role") == "assistant" and msg.get("content"):
            return str(msg["content"])
    return ""


def run_agent_simple(prompt: str, image: str | None = None, **kwargs) -> dict:
    # thinking / reasoning_effort 等参数通过 **kwargs 透传给 run_agent
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
