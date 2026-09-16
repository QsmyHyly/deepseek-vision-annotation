# 思考模式（DeepSeek）

> 来源：<https://api-docs.deepseek.com/zh-cn/guides/thinking_mode>
> 抓取时间：2026-09-14
> 说明：本文档为对官方在线文档的快照整理，请以官方页面为准。
> 代码里的引用标记：`@doc docs/DeepSeek-Thinking-Mode.md#<锚点>`

DeepSeek 模型支持思考模式：在输出最终回答之前，模型会先输出一段思维链内容，以提升最终答案的准确性。

---

## 思考模式开关与思考强度控制

| 能力 | OpenAI 格式 | Anthropic 格式 | Responses API 格式 |
| --- | --- | --- | --- |
| 思考模式开关 | `{"thinking": {"type": "enabled/disabled"}}` | — | `{"reasoning": {"effort": "none/low/high/max"}}`（none = 关闭） |
| 思考强度控制 | `{"reasoning_effort": "low/high/max"}` | `{"output_config": {"effort": "low/high/max"}}` | `{"reasoning": {"effort": ...}}` |

- 思考模式**默认打开**，且 effort 默认为 `high`。
- 用户设置的 effort 与服务端实际推理 effort 的映射（Flash 档与 Pro 档一致）：

  > 原文写的是 `deepseek-v4-flash`，那是旧模型名。该名字**仍可调用但对应模型已下线**，
  > 请求会路由到 DeepSeek-V4.1-Flash；当前应使用 `deepseek-flash`。
  > 对应关系见 `docs/DeepSeek-Models-and-Pricing.md#模型版本对应关系`。

  | 请求传入 | 实际映射 |
  | --- | --- |
  | low | low |
  | medium | high |
  | high | high |
  | xhigh | high |
  | max | max |

- ⚠️ **在 OpenAI SDK 的 Chat Completions 里，`thinking` 必须放进 `extra_body`**：

  ```python
  response = client.chat.completions.create(
      model="deepseek-v4-pro",
      messages=messages,
      reasoning_effort="high",
      extra_body={"thinking": {"type": "enabled"}},   # 关闭就写 "disabled"
  )
  ```

---

## 输入输出参数

思考模式**不支持** `temperature`、`top_p`、`presence_penalty`、`frequency_penalty`。
为了兼容已有软件，设置这些参数不会报错，但也不会生效。

思考模式下，思维链内容通过 `reasoning_content` 参数返回，与 `content` 同级。
在后续轮次的请求中，`reasoning_content` 是否需要回传、是否会被拼接进上下文，取决于请求是否携带 `tools` 参数：

- **携带 `tools`**：历史轮次的 `reasoning_content` 均应回传给 API，并会被拼接进上下文（见下节）。
- **未携带 `tools`**：`reasoning_content` 无需回传；即使传入 API 也会被忽略，不会拼接进上下文。

---

## 工具调用

思考模式下支持工具调用：模型在给出最终答案前，可以进行多轮思考与工具调用。

官方文档指出：**携带了 `tools` 参数的请求，在后续所有请求中必须完整回传 `reasoning_content`**——
即使该轮模型未实际进行工具调用；若未正确回传，API 会返回 400。

被 append 的 assistant 消息应当同时带上三个字段：

```text
messages.append({
    'role': 'assistant',
    "content": response.choices[0].message.content,
    "reasoning_content": response.choices[0].message.reasoning_content,
    "tool_calls": response.choices[0].message.tool_calls,
})
```

### 本项目的实测补充（2026-09-14，模型 `deepseek-flash`）

官方的"必须回传否则 400"在实测中**并未触发**：带 tools 且开启思考的第 2 轮，
故意不回传 `reasoning_content` 依然返回 200 且答案正确。
本项目仍然按官方要求回传（`objloc/agent.py`，仅在确有思考内容时附加该字段），
不依赖这条未经证实的宽松行为。

实测还确认：

| 请求 | reasoning_content 长度 | completion_tokens |
| --- | --- | --- |
| 不传 thinking（默认开启） | 111 | 35 |
| `thinking: disabled` | 0 | 1 |
| `thinking: enabled` + `reasoning_effort: low` | 58 | 20 |
| `thinking: disabled` + `reasoning_effort: high` | 0 | 1 |

⇒ `thinking: disabled` 生效且**优先于** `reasoning_effort`；
关闭思考时整条思维链模板都不会注入（`prompt_tokens` 同步从 38 降到 12）。

### 打标准确率 A/B（`scripts/compare_thinking.py`）

同一批内置测试图、同一提示词，只切换思考开关：

| 模式 | 平均耗时 | 思维链字符 | 检出率 | 平均 IoU | 标签准确率 |
| --- | --- | --- | --- | --- | --- |
| 思考开 | 4.9 s | 1436 | 100% | 0.842 | 100% |
| 思考关 | 2.2 s | 0 | 100% | 0.873 | 100% |

（3 张基准几何图 × 4 目标，2026-09-14 实测。）
结论：**在这类"看图找几何图形"的任务上，关掉思考约快 2.2 倍而准确率不下降**；
是否开启取决于任务难度，所以项目把它做成开关而不是写死。

---

## 本项目中的落地位置

| 位置 | 作用 |
| --- | --- |
| `objloc/config.py: thinking_payload()` | 构造 `extra_body` 载荷 |
| `objloc/config.py: REASONING_EFFORTS` | effort 合法值白名单（非法值不透传，避免 400） |
| `objloc/providers.py: resolve_thinking()` | 合并"按次覆盖 + 配置默认值"，真实客户端与 Mock 共用 |
| `objloc/providers.py: OpenAICompatClient.stream_chat` | 真正把参数发出去的地方 |
| `objloc/agent.py: run_agent(thinking=...)` | 按次覆盖入口，并决定是否回传 `reasoning_content` |
| `objloc/web/app.py: /api/detect` | 网页开关对应的 `thinking` 请求字段 |
| `objloc/web/static/index.html` | 「思考模式」勾选框（选择记在 localStorage） |
| 环境变量 `THINKING` / `REASONING_EFFORT` | 服务端默认值 |
| `scripts/compare_thinking.py` | 开关 A/B 对照，程序判定准确率 |
