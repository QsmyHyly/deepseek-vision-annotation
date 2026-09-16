# DeepSeek 模型与价格（官方文档快照）

> 来源：<https://api-docs.deepseek.com/zh-cn/quick_start/pricing>（抓取日期 2026-09-16）
> 代码里的引用标记：`@doc docs/DeepSeek-Models-and-Pricing.md#<锚点>`
>
> 该文档解决什么问题：模型名与版本号的对应关系、以及**为什么本项目只能用 `deepseek-flash`**。
> 价格与模型阵容会变动，若本页与官网冲突，以官网为准并更新本页。

---

## 模型版本对应关系

价格页「模型细节」表格给出的对应关系：

| 调用时用的模型名 | 实际模型版本 | 备注 |
|---|---|---|
| `deepseek-flash` | **DeepSeek-V4.1-Flash** | **本项目使用的就是它** |
| `deepseek-v4-pro` | DeepSeek-V4-Pro-0813 | 本项目不可用，见下节 |

旧模型名 `deepseek-v4-flash`、`deepseek-v4-flash-vision-exp` **仍可调用，但对应模型已下线**，
请求由 DeepSeek-V4.1-Flash 提供服务，并按 Flash 价格计费。

⇒ 也就是说：**`deepseek-flash` 就是 V4.1 的 Flash 档**。本项目配置里写 `deepseek-flash`，
实际跑的就是 DeepSeek-V4.1-Flash，不需要也不能改成别的名字。

## 本项目为什么只用 deepseek-flash

价格页的「图像理解」一行：

| 能力 | `deepseek-flash` | `deepseek-v4-pro` |
|---|---|---|
| 图像理解 | **支持** | **不支持** |
| Tool Calls | 支持 | 支持 |
| Json Output | 支持 | 支持 |
| 上下文长度 | 1M | 1M |
| 最大输出 | 384K | 384K |
| 思考模式 | 支持（默认开启） | 支持（默认开启） |

本项目的全部功能都建立在「把图片送进模型、让模型输出坐标」之上，
而 **`deepseek-v4-pro` 不支持图像理解** ⇒ 换成它之后识别链路直接失效。
因此 `objloc/web/app.py` 里那个按次覆盖模型的 `model` 参数只是逃生口，正常不要用。

## 价格与限速

单位：元 / 百万 tokens。「空闲时段」价格为「高峰时段」的一半；
高峰时段为北京时间周一至周五 9:00-12:00、14:00-18:00，其余为空闲时段。

| 计费项 | `deepseek-flash` 空闲 | `deepseek-flash` 高峰 | `deepseek-v4-pro` 空闲 | `deepseek-v4-pro` 高峰 |
|---|---|---|---|---|
| 输入（缓存命中） | 0.02 | 0.04 | 0.15 | 0.30 |
| 输入（缓存未命中） | 1 | 2 | 4.5 | 9.0 |
| 输出 | 4 | 8 | 13.5 | 27.0 |
| 并发限制 | 2500 | | 500 | |

BASE URL（OpenAI 格式）：`https://api.deepseek.com`；Anthropic 格式：`https://api.deepseek.com/anthropic`。

## 本项目中的落地位置

| 位置 | 内容 |
|---|---|
| `objloc/config.py` | `Settings.model` 默认值 `deepseek-flash`，注释里写明它就是 V4.1-Flash |
| `objloc/client.py` | 客户端门面 `model_id` 的默认值 |
| `objloc/web/app.py` | `/api/detect` 的按次覆盖 `model` 参数（逃生口，正常不用） |
| `.env.example` / `README.md` / `AGENTS.md` | `DEEPSEEK_MODEL` 配置项说明 |