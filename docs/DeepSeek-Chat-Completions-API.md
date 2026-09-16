# Chat Completions API（DeepSeek）

> 来源：<https://api-docs.deepseek.com/zh-cn/api/create-chat-completion>
> 抓取时间：2026-09-13
> 说明：本文档为对官方在线文档的快照整理，页面为动态 schema 浏览器，字段级约束可能未完整呈现，请以官方页面为准。

---

## 接口

```http
POST /chat/completions
```

根据输入的上下文，来让模型补全对话内容。

- **Base URL**：`https://api.deepseek.com`
- **Content-Type**：`application/json`
- **认证**：`Authorization: Bearer ${DEEPSEEK_API_KEY}`

---

## 请求

- application/json

### Body

**必填**

消息列表（Array），元素为以下消息类型之一：

- System message
- User message
- Assistant message
- Tool message

其中每条消息的 `content` 可以是：

- **纯文本**（Text content）
- **内容块数组**（Array of content parts），块类型包括：
  - Text content part
  - Image content part
  - File content part

其他参数：

- `tools`（Array）：可供模型调用的工具列表
- `tool_choice`：工具选择策略，取值类型为
  - `ChatCompletionToolChoice`
  - `ChatCompletionNamedToolChoice`
- `user_id`（string）：用户标识，用途如下：
  - 可用于区分您业务侧的用户身份，以帮助我们进行内容安全处理。
  - 可用于 KVCache 缓存隔离，以进行隐私管理。
  - 可用于我们对您业务侧用户进行调度隔离。
  - 关于 `user_id` 参数更详细的描述，请参考「限速与隔离」。

---

## 响应

### 200（无流式）

返回一个 **chat completion** 对象。

- application/json

#### Schema

```json
{
  "id": "string",
  "choices": [
    {
      "finish_reason": "stop",
      "index": 0,
      "message": {
        "content": "string",
        "reasoning_content": "string",
        "tool_calls": [
          {
            "id": "string",
            "type": "function",
            "function": {
              "name": "string",
              "arguments": "string"
            }
          }
        ],
        "role": "assistant"
      },
      "logprobs": {
        "content": [
          {
            "token": "string",
            "logprob": 0,
            "bytes": [0],
            "top_logprobs": [
              {
                "token": "string",
                "logprob": 0,
                "bytes": [0]
              }
            ]
          }
        ],
        "reasoning_content": [
          {
            "token": "string",
            "logprob": 0,
            "bytes": [0],
            "top_logprobs": [
              {
                "token": "string",
                "logprob": 0,
                "bytes": [0]
              }
            ]
          }
        ]
      }
    }
  ],
  "created": 0,
  "model": "string",
  "system_fingerprint": "string",
  "object": "chat.completion",
  "usage": {
    "completion_tokens": 0,
    "prompt_tokens": 0,
    "prompt_tokens_details": {
      "cached_tokens": 0
    },
    "prompt_cache_hit_tokens": 0,
    "prompt_cache_miss_tokens": 0,
    "total_tokens": 0,
    "completion_tokens_details": {
      "reasoning_tokens": 0
    }
  }
}
```

#### Example

```json
{
  "id": "930c60df-bf64-41c9-a88e-3ec75f81e00e",
  "choices": [
    {
      "finish_reason": "stop",
      "index": 0,
      "message": {
        "content": "Hello! How can I help you today?",
        "role": "assistant"
      },
      "logprobs": null
    }
  ],
  "created": 1705651092,
  "model": "deepseek-flash",
  "object": "chat.completion",
  "system_fingerprint": "fp_7a09fdf9c2",
  "usage": {
    "completion_tokens": 10,
    "prompt_tokens": 16,
    "total_tokens": 26,
    "prompt_tokens_details": {
      "cached_tokens": 0
    },
    "prompt_cache_hit_tokens": 0,
    "prompt_cache_miss_tokens": 16
  }
}
```

---

### 200（流式）

返回一系列 **chat completion chunk** 对象的流式输出。

- text/event-stream

#### Schema

```json
[
  {
    "id": "string",
    "choices": [
      {
        "delta": {
          "content": "string",
          "reasoning_content": "string",
          "role": "assistant",
          "tool_calls": [
            {
              "index": 0,
              "id": "string",
              "type": "function",
              "function": {
                "name": "string",
                "arguments": "string"
              }
            }
          ]
        },
        "logprobs": {
          "content": [
            {
              "token": "string",
              "logprob": 0,
              "bytes": [0],
              "top_logprobs": [
                {
                  "token": "string",
                  "logprob": 0,
                  "bytes": [0]
                }
              ]
            }
          ],
          "reasoning_content": [
            {
              "token": "string",
              "logprob": 0,
              "bytes": [0],
              "top_logprobs": [
                {
                  "token": "string",
                  "logprob": 0,
                  "bytes": [0]
                }
              ]
            }
          ]
        },
        "finish_reason": "stop",
        "index": 0
      }
    ],
    "created": 0,
    "model": "string",
    "system_fingerprint": "string",
    "object": "chat.completion.chunk"
  }
]
```

#### Example

```text
data: {"id": "1f633d8bfc032625086f14113c411638", "choices": [{"index": 0, "delta": {"content": "", "role": "assistant"}, "finish_reason": null, "logprobs": null}], "created": 1718345013, "model": "deepseek-flash", "system_fingerprint": "fp_a49d71b8a1", "object": "chat.completion.chunk"}
data: {"choices": [{"delta": {"content": "Hello", "role": "assistant"}, "finish_reason": null, "index": 0, "logprobs": null}], "created": 1718345013, "id": "1f633d8bfc032625086f14113c411638", "model": "deepseek-flash", "object": "chat.completion.chunk", "system_fingerprint": "fp_a49d71b8a1"}
data: {"choices": [{"delta": {"content": "!", "role": "assistant"}, "finish_reason": null, "index": 0, "logprobs": null}], "created": 1718345013, "id": "1f633d8bfc032625086f14113c411638", "model": "deepseek-flash", "object": "chat.completion.chunk", "system_fingerprint": "fp_a49d71b8a1"}
data: {"choices": [{"delta": {"content": " How", "role": "assistant"}, "finish_reason": null, "index": 0, "logprobs": null}], "created": 1718345013, "id": "1f633d8bfc032625086f14113c411638", "model": "deepseek-flash", "object": "chat.completion.chunk", "system_fingerprint": "fp_a49d71b8a1"}
data: {"choices": [{"delta": {"content": " can", "role": "assistant"}, "finish_reason": null, "index": 0, "logprobs": null}], "created": 1718345013, "id": "1f633d8bfc032625086f14113c411638", "model": "deepseek-flash", "object": "chat.completion.chunk", "system_fingerprint": "fp_a49d71b8a1"}
data: {"choices": [{"delta": {"content": " I", "role": "assistant"}, "finish_reason": null, "index": 0, "logprobs": null}], "created": 1718345013, "id": "1f633d8bfc032625086f14113c411638", "model": "deepseek-flash", "object": "chat.completion.chunk", "system_fingerprint": "fp_a49d71b8a1"}
data: {"choices": [{"delta": {"content": " assist", "role": "assistant"}, "finish_reason": null, "index": 0, "logprobs": null}], "created": 1718345013, "id": "1f633d8bfc032625086f14113c411638", "model": "deepseek-flash", "object": "chat.completion.chunk", "system_fingerprint": "fp_a49d71b8a1"}
data: {"choices": [{"delta": {"content": " you", "role": "assistant"}, "finish_reason": null, "index": 0, "logprobs": null}], "created": 1718345013, "id": "1f633d8bfc032625086f14113c411638", "model": "deepseek-flash", "object": "chat.completion.chunk", "system_fingerprint": "fp_a49d71b8a1"}
data: {"choices": [{"delta": {"content": " today", "role": "assistant"}, "finish_reason": null, "index": 0, "logprobs": null}], "created": 1718345013, "id": "1f633d8bfc032625086f14113c411638", "model": "deepseek-flash", "object": "chat.completion.chunk", "system_fingerprint": "fp_a49d71b8a1"}
data: {"choices": [{"delta": {"content": "?", "role": "assistant"}, "finish_reason": null, "index": 0, "logprobs": null}], "created": 1718345013, "id": "1f633d8bfc032625086f14113c411638", "model": "deepseek-flash", "object": "chat.completion.chunk", "system_fingerprint": "fp_a49d71b8a1"}
data: {"choices": [{"delta": {"content": "", "role": null}, "finish_reason": "stop", "index": 0, "logprobs": null}], "created": 1718345013, "id": "1f633d8bfc032625086f14113c411638", "model": "deepseek-flash", "object": "chat.completion.chunk", "system_fingerprint": "fp_a49d71b8a1", "usage": {"completion_tokens": 9, "prompt_tokens": 17, "total_tokens": 26, "prompt_tokens_details": {"cached_tokens": 0}, "prompt_cache_hit_tokens": 0, "prompt_cache_miss_tokens": 17}}
data: [DONE]
```
