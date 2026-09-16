# 项目长期记忆 · AGENTS.md

> 本文件是本项目的**长期记忆 / 协作约定**。任何 AI 助手或开发者接手本项目，请**先读这一份**，
> 再动代码或重做调研。如果本项目的事实发生变化，请**顺手更新本文件**。

---

## 0. 一句话定位

DeepSeek 多模态「**物体定位 + 打标**」演示软件：**流式输出** + **工具执行框架** + **网页打标前后对比**，
外加一套**合成图片打标准确率评测**，以及**思考模式开关**（网页勾选框 / CLI / 环境变量）。

- 工作目录：`E:\QsmyHyly-Code-Study-Workspace\deepseek物体定位演示软件`
- Python 3.11（Windows / PowerShell）
- 入口：`python main.py web | detect | tools | demo`，评测：`python scripts/benchmark.py`

---

## 1. 当前状态（务必保持最新）

- ✅ 真实模型 `deepseek-flash` 全链路实测通过（流式 + 工具执行 + 标注 + 网页 SSE）。
- ✅ 网页对比查看可用：并排 / 滑块 / 叠加三种模式 + 流式控制台。
- ✅ **前端已重新设计**（单文件 `objloc/web/static/index.html`）：左「图片来源 / 识别参数 / 手动标注」、
  中「对比查看区（主角）」、右「流式控制台（可折叠让宽度）」；单一 `state` + 幂等 `renderXxx()`；
  流式事件用 `run.token` 丢弃过期事件。设计说明写在 `index.html` 头部块注释里（不另开 md），约定见 4.7。
- ✅ 打标准确率评测：**40 个目标、检出率 100%、平均 IoU 0.896、标签准确率 100%**（详见第 7 节）。
- ✅ **坐标约定已切换为 0.0~1.0 相对比例**（见 4.3），实测 deepseek-flash 输出即符合，无需换算。
- ✅ 网页内置 **17 张测试图**（基准几何图 / 分辨率扫描 / 圆点阵 / 文字阶梯 / 竖线带 / **网页截图**，
  目录见 `objloc/samples.py`）：点选载入 → 识别 → **自动按真值算检出率、平均 IoU、标签准确率**，
  不用人眼判断（`GET /api/samples`、`POST /api/samples/{id}/load`、SSE 的 `annotated.accuracy`）。
  第 ⑤ 组「网页截图」是唯一一组图片由**真浏览器渲染**、真值由 **DOM `getBoundingClientRect()`** 给出的图，
  见 §7 的「网页截图组」。
- ✅ **思考模式可开关**（见 4.6）：网页勾选框 / `main.py detect --no-thinking` / `THINKING=0`。
  实测同批基准图：思考开 4.9s / 思考关 2.2s，检出率与标签准确率都是 100%，
  平均 IoU 0.842 → 0.873（`scripts/compare_thinking.py`，报告在 `runs/thinking_ab/`）。
- ✅ 未配置 Key 时自动降级为**离线 Mock**，保证零配置可演示。
- ✅ 网页截图组实测（第 ⑤ 组，4 图 28 目标）：**检出率 100%、平均 IoU 0.856、标签准确率 97%**，
  关掉思考同样 100% 检出、耗时减半（详见第 7 节）。
- ✅ 自测全绿：`tests/test_parser.py`、`tests/test_e2e.py`、`tests/test_benchmark.py`、`tests/test_docrefs.py`、
  `tests/test_web_samples.py`（均离线，不花 API）；
  另有前端回归自检 `tests/ui/`（id 双向校验 + 真实浏览器驱动页面），见 4.7。

---

## 2. 环境与配置

- **API Key**：`DEEPSEEK_API_KEY` 配置在 **Windows 用户级环境变量**（不在项目里，勿写进仓库/日志）。
  - ⚠️ 坑：**已打开的终端是旧环境，读不到新变量**。dsh 持久 PowerShell 里要显式刷新：
    ```powershell
    $env:DEEPSEEK_API_KEY = [Environment]::GetEnvironmentVariable('DEEPSEEK_API_KEY','User')
    ```
- 配置见 `objloc/config.py` + `.env.example`：

  | 变量 | 默认 | 说明 |
  |---|---|---|
  | `DEEPSEEK_API_KEY` | 空 | 空 → Mock |
  | `DEEPSEEK_BASE_URL` | `https://api.deepseek.com` | 任意 OpenAI 兼容服务 |
  | `DEEPSEEK_MODEL` | `deepseek-flash` | **就是 DeepSeek-V4.1-Flash**；官方另一档 `deepseek-v4-pro` **不支持图像理解**，本项目不能换（见 6.11） |
  | `LLM_PROVIDER` | `auto` | `auto`/`deepseek`/`mock` |
  | `MAX_TOOL_ROUNDS` | `8` | 工具调用最大轮数 |
  | `SYSTEM_PROMPT` | 内置 | 覆盖系统提示词 |
  | `THINKING` | `1` | 思考模式默认开关（`0` 关闭） |
  | `REASONING_EFFORT` | 空 | 思考强度 `low/medium/high/xhigh/max`，空 = 服务端默认 high |
  | `WEB_HOST`/`WEB_PORT` | `127.0.0.1`/`8765` | 网页服务 |

- **模型事实（实测）**：`deepseek-flash` 是**思考模型**（默认开启），`reasoning_content` 有内容、
  `content` 才是正文；流式实现已同时处理两者。多模态图片用 `image_url`（http / data URL）。
  关闭思考用 `extra_body={"thinking": {"type": "disabled"}}`，见 4.6 与 `docs/DeepSeek-Thinking-Mode.md`。

---

## 3. 目录结构与职责

```
main.py                      CLI 入口（web / detect / tools / demo）
requirements.txt  .env.example  README.md  AGENTS.md
docs/                        DeepSeek 官方 API 文档快照
  DeepSeek-Chat-Completions-API.md / DeepSeek-Tool-Calls.md
  DeepSeek-Thinking-Mode.md  思考模式开关 / 强度 / reasoning_content 回传规则 + 本项目实测
  DeepSeek-Models-and-Pricing.md  模型名 ↔ 版本号对应、价格与限速、为什么只能用 deepseek-flash
objloc/                      ★核心包
  config.py                  集中配置（Settings 单例）；RUNS_DIR 指项目根
  providers.py               ★模型客户端：统一流式事件协议 + OpenAI 兼容 + 离线 Mock
  client.py                  客户端门面：stream_chat / stream_text / infer / 旧签名兼容
  parsing.py                 坐标 JSON 解析（容忍说明文字、代码块、单引号）
  tool_schema.py             由函数签名 + Annotated 注解生成工具 JSON Schema
  visualizer.py              图像加载 + 标注绘制（bbox/point，跨平台中文字体）
  agent.py                   ★Agent 主循环：流式输出 + 工具执行 + collect_items
  benchmark.py               ★合成图片生成 + IoU/匹配/准确率指标（纯逻辑）
  samples.py                 ★内置测试图片目录（网页「测试图片」面板与探测脚本共用的唯一生成源）
  tools/
    registry.py              ★工具框架：注册 / schema / 执行 / 上下文注入 / 异常回填
    builtin.py               内置工具集合
  web/
    app.py                   FastAPI：图片管理 + SSE 流式接口 + 静态页
    static/index.html        ★打标前后对比页面（原生 JS）
scripts/
  benchmark.py               ★打标准确率评测 CLI（生成图片 + 调模型 + 出报告）
  capture_web_samples.mjs    ★第 ⑤ 组「网页截图」的唯一生成源：Playwright 渲染仿真网页 →
                             输出 PNG + manifest.json（真值来自 DOM），产物在 runs/web_samples/
  report.py                  美化打印 report.json
  compare_thinking.py        ★思考模式 A/B 对照（同一批内置测试图，开/关各跑一遍比准确率与耗时）
tests/                       自测
  test_parser.py / test_e2e.py / test_benchmark.py / test_docrefs.py
  test_web_samples.py        网页截图组：真值与 PNG 是否同源、能否被自动打分（见 §7）
                             纯离线，不花 API、也不需要服务在跑
  ui/                        前端回归自检（见 4.7）
    check_frontend_contract.py  id 双向校验（离线、不需要服务）
    ui_check.mjs                真实浏览器驱动页面，交互与几何断言（离线，但需要服务在跑）
    ui_check_detect.mjs         浏览器端真实 SSE 全链路（花 API）
    smoke_detect.py             HTTP 层 SSE 冒烟（花 API）
runs/                        运行产物（uploads、benchmark、标注图、web 日志）
```

数据流：`web/app.py` → `objloc.agent.run_agent` →（`providers.stream_chat` 流式事件 ↔
`objloc.tools.registry.execute` 工具执行）→ `visualizer.render_annotations` 出标注图 → SSE 推前端。

---

## 4. 关键约定（改代码前必读）

### 4.1 流式事件协议（providers.py）
统一产出：`reasoning` / `content` / `tool_call_delta` / `finish`；终端、网页、SSE 共用一套实现。
`tool_call_delta` 按 `index` 聚合，参数是分片字符串，需要累加。

### 4.2 工具执行框架（tools/registry.py）
- `register(func)`：schema 从**函数名 + docstring 首行 + 参数 Annotated 注解**自动生成，不要手写 schema。
- `execute(name, args, context=...)`：解析参数 → 执行 → 序列化；异常会作为文本回填给模型，不抛穿。
- **上下文注入（重要）**：模型无法知道的参数（如"当前图片地址"）用
  `registry.register_with_context(func, {"source"})` 注册；`source` **从 schema 隐藏**，
  执行时由 `run_agent(tool_context={"source": ...})` 注入并**覆盖模型编造的值**。
  - 背景：真实模型曾编造 `source=https://example.com/image.png` 导致 404，故引入。
- Agent 循环：模型返回 `tool_calls` → 逐个执行 → `role="tool"` 回填 → 下一轮，直到不再请求工具或达 `MAX_TOOL_ROUNDS`。
- 新增工具 = 写函数 + `registry.register(func)`。
- **坐标归一化在工具层做**（`objloc/tools/builtin.py: _unitize()`）：模型能调用的
  `parse_coordinates` / `decode_json_points` / `extract_coordinates` / `annotate_image`
  都会把 0~1000 旧刻度换算成 0.0~1.0 再返回，工具描述里也明写了"坐标必须是 0.0~1.0"。
  ⚠️ `objloc/parsing.py` 里的同名函数是**纯解析、原样返回**，**不要**往核心里加归一化——
  `scripts/probe_vision_frame.py` 靠它读模型的原始输出以判断"模型到底给的哪个坐标空间"。

### 4.3 坐标约定（曾踩坑！）
统一 **0.0~1.0 的相对比例**（小数）：`x = 像素x / 图宽`，`y = 像素y / 图高`，保留 3~4 位小数。
```json
[{"bbox_2d": [x1,y1,x2,y2], "label": "名称"}, {"point_2d": [x,y], "label": "名称"}]
```
- 提示词见 `objloc/config.py: DEFAULT_SYSTEM_PROMPT`（`SYSTEM_PROMPT` 可覆盖），**必须显式给出换算公式 + 禁止像素值**。
- ⚠️ 历史教训：旧提示词里"无需考虑图片分辨率或其它坐标系转换"与"坐标一律使用归一化值"互相矛盾，
  模型经常直接输出**像素坐标**，检出率只有 40%（改成显式公式后 100%）。改提示词后**务必重跑评测**。
- ⚠️ **为什么不能用像素**（实测证据：`scripts/probe_vision_frame.py` + `runs/vision_probe/`）：
  DeepSeek 会在进模型前缩放图片且**不回传缩放后尺寸**；模型输出的"像素"落在它**每次自己编的画布**上
  （同一张图三次调用给出 1000x750 / 1000x800 / 1024x768 等不同答案），所以像素值既不准确也不可复现。
  缩放本身是**纯线性、等比、无补边**的（拟合 R²≈1、截距≈0），因此相对比例不受影响。
- **各环节的归一化位置**（哪一层负责，别重复实现）：

  | 环节 | 归一化？ | 位置 |
  |---|---|---|
  | 系统提示词 | ✅ 给出换算公式 + 禁止像素值 | `config.py: DEFAULT_SYSTEM_PROMPT` |
  | 模型可调用的工具（解析 / 标注） | ✅ 工具层统一 | `tools/builtin.py: _unitize()` |
  | 纯解析核心 | ❌ 刻意保持原样 | `parsing.py`（探针依赖） |
  | 网页 SSE 最终链路、手动标注 | ✅ | `web/app.py` → `normalize_to_unit()` |
  | 画标注图 | ✅ 内部再兜底一次 | `visualizer.py: annotate()` |

- **为什么是 0~1（来历，便于对外讲清楚）**：归一化坐标不是检测圈发明的，它出身于计算机图形学的
  **设备无关坐标**——1977 年 SIGGRAPH 的 Core 报告提出 **NDC（Normalized Device Coordinates）**，
  1985 年 GKS 成为 ISO 7942 后把 NDC 写进国际标准，取值就是 `[0,1]`，动机是"换设备换分辨率都不用改描述"。
  检测圈里把 bbox 归一化推成事实标准的是 **YOLO（2015，标签 `cx cy w h` 全部除以图宽高）**，
  其后 DETR（2020）直接回归归一化 `cxcywh`；而 PASCAL VOC / COCO 一直用像素。
  到了 VLM 时代又摆回去了：**0~1000 整数刻度**成了主流（Qwen2-VL / Qwen3-VL 用它，
  Qwen2.5-VL 中途改成绝对像素，各家仍在摇摆），因为大模型是逐 token 生成，
  `0.1234` 位数不定、精度抖动，整数刻度更好生成、也好和视觉 patch 网格对齐。
  ⇒ **检测网络里"归一化更好"的理由，在语言模型里并不完全成立**，所以本项目才要显式规定刻度并做兜底。
  归到一句话：归一化的价值是**把设备/分辨率的自由度从数据里挤出去**——对当年的绘图仪和今天的 VLM 都成立。
- **旧刻度兜底**：模型偶尔仍会输出 0~1000 的整数刻度。`objloc/parsing.py: normalize_to_unit()`
  在整批坐标上判定，**无损除以 1000** 换算回 0.0~1.0（常量 `LEGACY_SCALE` / `LEGACY_SCALE_NOTICE`），
  网页同时弹 warning；`visualizer.annotate()` 再兜底一次，保证任何入口都不会把旧刻度画到左上角。
- **提示词里怎么讲「分辨率」（2026-09 加入，三条都要留着）**。模型的处境是：它**看不到**图片的真实像素尺寸
  （服务端先缩放再喂给它，且不回传缩放后尺寸），但它很容易"脑补"一个尺寸（实测同一张图三次调用
  分别答 1000×750 / 1000×800 / 1024×768）。所以提示词必须正面交代这件事，而不是回避：
  1. **明确告知它拿不到、也推不出真实分辨率**，凭"看起来多大"猜出的数字一律不准，不要写进输出、
     更不能当计算依据（连"通过工具查到的宽高"也要点破：那是**原图**的尺寸，不是它眼里的那张图的栅格）；
  2. **明确告知它感知到的数值本身就是归一化数据**——它对位置的感知天生是"相对整幅图的比例"，
     不是像素，让它可以放心相信这个比例感，而不是先编一个尺寸再换算；
  3. **点破刻度的不确定性**：它无法确定自己感知到的是 0~1 还是 0~1000（两者是同一个位置，差 1000 倍），
     然后立刻给出**确定的输出契约**——本项目统一按 0~1 输出，感知到 0~1000 就除以 1000 再写。
  ⚠️ 第 3 条只讲"你可能拿不准刻度"，**必须紧跟一句确定的换算规则**；只留不确定性不给规则，等于把
  4.3 开头那个"提示词自相矛盾 → 检出率 40%"的坑重新挖一遍。改完提示词必须重跑第 7 节的评测。

### 4.4 系统提示词
`objloc/config.py: DEFAULT_SYSTEM_PROMPT`（可用 `SYSTEM_PROMPT` 覆盖）。当前为显式换算版 + 分辨率说明版，
要点：给换算公式、禁像素值、讲清"看不到真实分辨率"、"感知值本就是归一化数据"、点破 0~1 / 0~1000 的刻度不确定
并给出确定的 0~1 输出契约。

- **它是全项目唯一的坐标口径来源**：网页 / CLI / 评测 / 内置测试图共用一个 system 消息
  （`objloc/agent.py: build_messages` 取 `Settings.system_prompt`），要改口径**只改这一处**，
  别在 `objloc/samples.py` 的各条任务提示词里再抄一份——那些提示词只描述"找什么"，不描述"坐标怎么给"。
- **工具描述只有首行会进模型**：schema 由 `objloc/tool_schema.py: build_tool` 取 docstring 的
  `splitlines()[0]`，所以**面向模型的约束必须写在首行**，下面的正文是给人看的
  （`get_image_info` 就吃过这个亏：警告写在第二段，模型根本读不到）。

### 4.5 Mock 模式
`LLM_PROVIDER=auto` 且无 Key → `MockVisionClient`，假装流式并调 `parse_coordinates`。
写测试时**必须显式强制 mock**（见 `tests/test_e2e.py` 顶部）。

### 4.6 思考模式开关（thinking）
DeepSeek 默认先输出思维链（`reasoning_content`）再给正文。本项可关。

- **参数形态**：`thinking` **不是** Chat Completions 的顶层字段，必须走
  `extra_body={"thinking": {"type": "enabled"/"disabled"}}`；强度用顶层 `reasoning_effort`。
  构造见 `objloc/config.py: thinking_payload()`，合法值白名单 `REASONING_EFFORTS`。
- **三层开关**（按优先级）：
  1. 按次覆盖 —— 网页「思考模式」勾选框（`POST /api/detect` 的 `thinking` 字段）、
     `run_agent(thinking=...)`、`main.py detect --no-thinking`、`scripts/benchmark.py --no-thinking`；
  2. 服务端默认 —— 环境变量 `THINKING=0/1`（`Settings.thinking`）；
  3. 缺省开启。
- **一处实现**：`objloc/providers.py: resolve_thinking()` 是唯一解析处，真实客户端与 Mock 共用，
  别再各写一份（曾经 Mock 漏了 effort 合法性校验，被自测抓出来）。
- **`reasoning_content` 回传**：带 `tools` 时官方要求历史轮次的 `reasoning_content` 原样回传
  （`objloc/agent.py`）。本项目**只在确有思考内容时**附加该字段，关闭思考时就不带。
  官方的"不回传会 400"在本机实测**没有复现**，但仍按官方写法来，不依赖未证实行为。
- **实测结论**（`scripts/compare_thinking.py`，3 张基准图 × 4 目标）：思考关 **快 2.2 倍**
  （4.9s → 2.2s），检出率/标签准确率都是 100%，平均 IoU 0.842 → 0.873。
  ⇒ 这类"看图找图形"的任务关掉思考没有损失；难任务再开。
- 关闭思考时 `temperature` 等参数才有效；思考模式下这些参数**传了也不生效**（官方说明）。

@doc docs/DeepSeek-Thinking-Mode.md#本项目中的落地位置
（该文档解决"参数怎么传、effort 怎么映射、实测数据在哪"的问题。）

### 4.7 前端页面（objloc/web/static/index.html）
单文件、零构建、无外部依赖（不引框架 / CDN / 字体 / 图标）。**页面就是全部前端**，改它就是改前端。
设计说明与状态模型写在文件头部的块注释里（按"文档就近写在代码里"的约定，不另开 .md）。

- **信息架构**：左栏 = 图片来源（Tab：本地上传 / 图片 URL / 内置测试图）+ 识别参数 + 手动标注；
  中栏 = 对比查看区（主角，占满剩余高度）；右栏 = 流式控制台（可折叠成竖条，把宽度让给对比区）。
- **状态**：单一 `state` + 幂等 `renderXxx()`，不要就地改 DOM；流式事件带 `run.token`，
  取消 / 换图 / 重新识别时用它丢弃过期事件（否则旧流会把新结果覆盖掉）。
- **对比几何**：不手算宽高比容器。原图按 contain 排版，再用一次 `getBoundingClientRect`
  把标注层精确盖上（`syncFrame` + `ResizeObserver`）——手算过 aspect-ratio 容器，出过 letterbox 错位。
- ⚠️ **三种对比模式的左右顺序必须一致：原图在左、标注图在右**。并排模式天然如此；
  滑块 / 叠加公用同一个 stack，靠 `clip-path: inset(0 0 0 pct%)` 切**标注层**实现，
  切掉左侧后底层原图自然露在左边。`tests/ui/ui_check.mjs` 有一条断言专门锁这个方向，别改反。
- **提示分层**（别把三类信息混在一起）：错误 / 告警 → 顶部 `#alertBar`（可关闭、带详情，
  不会被日志冲走）；操作进度 → 左栏 `#statusLine`；模型输出 → 右栏三段可折叠控制台。
- **改完必须跑**：`node --check`（抽 script 块）+ `python tests/ui/check_frontend_contract.py`
  + `node tests/ui/ui_check.mjs`，命令见第 5 节。

### 4.8 文档与注释约定

- **文档就近写在代码里**：说明、设计取舍、用法默认写进代码注释（文件头块注释 + docstring +
  关键行行内注释）。不为每个小功能另开孤立的 `.md`。
- **只有这些才单独成文**：跨模块整体架构、面向非代码读者的教程/交付说明、官方文档快照与
  需要长期对外引用的规范。本仓库的 `docs/` 只放最后一类（DeepSeek 官方文档快照）。
- **代码必须引用独立文档**：凡 `docs/` 下的文档，都要在**对应的代码位置**留下引用注释，
  统一标记 `@doc <路径>#<锚点>`，并补一句「该文档解决什么问题」。禁止只写「详见文档」。
- ⚠️ **锚点是人类可读标签**（如 `AGENTS.md#4.3-坐标约定`），**不是 GitHub 自动 slug**
  （自动 slug 会去掉点号，生成 `#43-坐标约定曾踩坑`）。两者不等价是刻意的取舍，
  校验按「忽略标点与大小写」的宽松口径比对。
- **文档挪动锚点时必须同步全仓库的 `@doc` 引用**，并在注释里保留旧锚点的迁移说明。
- 这条约定由 `tests/test_docrefs.py` 兜底（离线）：校验目标文件存在、`docs/` 无孤儿文档、
  锚点能对上标题。改了文档或引用的位置，跑一次就知道有没有断。

---

## 5. 常用命令

```powershell
cd 'E:\QsmyHyly-Code-Study-Workspace\deepseek物体定位演示软件'

# 启动网页（真实模型；确保当前会话有 DEEPSEEK_API_KEY）
python main.py web                       # → http://127.0.0.1:8765

# CLI 流式识别 + 出标注图
python main.py detect --image "runs\uploads\xxx.png" --prompt "识别主要目标"
python main.py detect --image a.png --no-thinking          # 关闭思考模式（更快、更省 token）
python main.py detect --image a.png --thinking --reasoning-effort low

# 思考模式 A/B 对照（程序判定准确率，不靠肉眼）
python scripts\compare_thinking.py                        # 默认 bench_01/02/03
python scripts\compare_thinking.py --samples marker_wide,res_1800 --repeat 2

# 离线跑通流式 + 工具执行
python main.py demo

# 打标准确率评测
python scripts\benchmark.py --count 5 --n-shapes 3 --annotate     # 生成图片 + 调模型评测
python scripts\benchmark.py --images-only --count 5               # 只生成图片
python scripts\report.py runs\benchmark\report.json               # 美化打印报告

# 内置测试图（网页同款）与帧几何探测
python -c "from objloc import samples; print(samples.list_catalog())"   # 看目录
python scripts\probe_vision_frame.py --self-test                       # 校验拟合数学，不花 API

# 自测（不花 API）
python tests\test_parser.py
python tests\test_e2e.py
python tests\test_benchmark.py
python tests\test_docrefs.py     # @doc 交叉引用：目标文件存在 / docs 无孤儿 / 锚点对得上（见 4.8）
python tests\test_web_samples.py # 网页截图组：真值/PNG/尺寸同源 + 完美预测应满分（见 7）
                                 # 缺 node/Playwright 时打印 SKIP 并以 0 退出，不假装通过

# 前端回归自检（改 index.html 后必跑；前两条离线，后两条花 API）
python tests\ui\check_frontend_contract.py   # id 双向校验：缺失 / 死元素必须为 0
node tests\ui\ui_check.mjs                   # 真实浏览器驱动页面：对比几何 / 滑块 / 折叠 / 窄屏 / 告警
node tests\ui\ui_check_detect.mjs            # 浏览器端真实 SSE 全链路
python tests\ui\smoke_detect.py              # HTTP 层 SSE 冒烟
```

**后台重启服务的标准姿势**（注意刷新环境变量）：
```powershell
$root='E:\QsmyHyly-Code-Study-Workspace\deepseek物体定位演示软件'
Get-NetTCPConnection -LocalPort 8765 -State Listen -EA SilentlyContinue | % { Stop-Process -Id $_.OwningProcess -Force }
$env:DEEPSEEK_API_KEY = [Environment]::GetEnvironmentVariable('DEEPSEEK_API_KEY','User')
$env:LLM_PROVIDER='auto'; $env:PYTHONIOENCODING='utf-8'
Start-Process python -ArgumentList '-m','objloc.web.app' -WorkingDirectory $root -WindowStyle Hidden `
  -RedirectStandardOutput "$root\runs\web.out.log" -RedirectStandardError "$root\runs\web.err.log"
```

---

## 6. 已知坑 / 注意事项

1. **旧会话读不到新环境变量** —— 重启服务前务必刷新 `$env:DEEPSEEK_API_KEY`。
2. **坐标约定**（见 4.3）—— 提示词别写"不用管分辨率"，必须给换算公式 + 禁止像素值；
   还要正面交代三件事：**模型拿不到真实分辨率**（不准脑补）、**它感知到的数值本就是归一化数据**、
   **刻度可能是 0~1 也可能是 0~1000（统一按 0~1 输出）**。改提示词后必须重跑第 7 节评测。
3. **字体**：别硬编码 `NotoSansCJK`（Windows 没有）。用 `visualizer.resolve_font`（命中 `msyh.ttc`）。
4. **真实模型是思考模型**（默认开启），会先出大量 `reasoning_content`，网页事件很多（700+ 条）属正常。
   想少看点就关掉思考（见 4.6）：关掉后**一个 reasoning 事件都没有**，事件总数大幅下降。
5. **别把 API Key 写进代码/记忆/日志**。
6. `runs/` 是运行产物目录，可清理。
7. 编辑 `config.py` 相邻字符串字面量会**自动拼接**，注意每句结尾标点/换行。
8. 包内导入统一用 **`from objloc.xxx import ...`**（绝对导入）；`main.py` / `scripts/*` 自带
   `sys.path` 引导到项目根。
9. **思考参数走 extra_body**：直接给 `create()` 传 `thinking=...` 会被 SDK 拒绝（未知关键字），
   必须 `extra_body={"thinking": {...}}`；`reasoning_effort` 反而是顶层参数。
10. **不是所有模型都认 `thinking`**：本机 `deepseek-flash` 实测支持开关与 effort（见文档）；
   换模型/换服务商前先用 `scripts/compare_thinking.py` 或一次裸调验证，别假设通用。
11. **本项目只能用 `deepseek-flash`（= DeepSeek-V4.1-Flash）**。官方只有两档模型，
   另一档 `deepseek-v4-pro` **不支持图像理解**——本项目全靠图片输入，换成它识别链路直接失效。
   `objloc/web/app.py` 里那个按次覆盖模型的 `model` 参数只是逃生口，网页从不发送它。
   官方来源：<https://api-docs.deepseek.com/zh-cn/quick_start/pricing>
   @doc docs/DeepSeek-Models-and-Pricing.md#模型版本对应关系

---

## 7. 打标准确率评测结论（重要）

评测方式：`objloc/benchmark.py` 用代码生成「已知答案」的几何图形图片（随机颜色 × 形状 × 位置），
把真值换算成 0.0~1.0 相对比例坐标，让真实模型打标，再按 IoU（阈值 0.5）匹配、按颜色/形状判定标签。
指标定义：检测率 = 命中数 / 真值数；平均 IoU 仅在匹配对上统计。

### 结果 A/B 对照（同一批图片：5 张 × 3 图形 = 15 目标）

| 提示词版本 | 检出率 | 平均 IoU | 颜色 | 形状 | 标签 | 输出像素坐标的图片 |
|---|---|---|---|---|---|---|
| 旧（"无需考虑分辨率…转换由外部处理"） | **40%** (6/15) | 0.655 | 100% | 100% | 100% | **3/5** |
| 新（显式换算公式 + 禁止像素值） | **100%** (15/15) | 0.897 | 100% | 100% | 100% | 0/5 |

**结论**：模型"看懂图 + 认颜色形状"的能力很强（标签 100%），失败几乎全来自**坐标约定没遵守**。
把提示词改为显式换算后，检出率 40% → 100%，平均 IoU 0.897。

### 加压场景（10 张 × 4 图形 = 40 目标，seed 2026）

- 检出率 **100%**（40/40），平均 IoU **0.896**，颜色/形状/标签准确率均 **100%**。
- 报告：`runs/benchmark_v2_hard/report.json`；对照基线：`runs/benchmark/report_before.json`。

> 复现：`python scripts\benchmark.py --count 10 --seed 2026 --n-shapes 4 --annotate --out runs\benchmark_v2_hard`

### GT 正确性独立复核（`scripts/verify_gt.py`）

为了排除"是不是我生成的 GT 本身就有问题"，写了一个**独立复核工具**：它不读任何生成时的中间数据，
而是直接从**渲染好的 PNG 像素**里用连通域分析重新量一遍每个图形的包围盒，再和 `ground_truth.json` 比对。

```bash
python scripts\verify_gt.py runs\benchmark\images        # 5 图 × 3 图形
python scripts\verify_gt.py runs\benchmark_v2_hard\images # 10 图 × 4 图形
```

结果：**全部 55 个图形（15 + 40）边框误差 ≤ 1px**（外描边宽 5px、按中心线绘制，±3px 内属正常）。
⇒ 说明 GT 与绘制结果一致，之前 40% 的失败**不是 GT 的问题**，而是模型坐标空间的问题（见上表）。

### 内置测试图的判定口径（`objloc/samples.py`）

五种图的真值生成方式不同，判定口径也不同，别混着看：

| 图组 | 真值来源 | 命中判定 | 标签判定 |
|---|---|---|---|
| ① 基准几何图 / ② 分辨率扫描 | `benchmark.Shape.to_gt()` | IoU ≥ 0.5 | 颜色 + 形状都要对 |
| ③ 彩色圆点 | `markers_to_gt()` | **中心点落在预测框内**（`benchmark.center_hit()`） | 只判颜色 |
| ④ 文字阶梯 / 竖线带 | 无坐标真值 | — | 不参与自动打分 |
| ⑤ 网页截图 | 浏览器 DOM（`scripts/capture_web_samples.mjs` 的 manifest.json） | **中心点落在预测框内**（`match: "center"`，理由见下） | **文本标签**（`label_mode="text"`，支持 `aliases` 别名） |

圆点图为什么特殊（两个都踩过坑）：

- **标签只写颜色名**。图上每个点旁写的就是颜色名，`MARKER_PROMPT` 也只问颜色；
  早年真值写成「红色圆形」，而提示词只要颜色 → 标签准确率恒为 0%，看起来像模型不行，
  实际是真值与提示词打架。真值里用 `expect_shape: False` 关掉形状判定。
- **命中按中心点判**。圆点直径只有 `min(宽,高)` 的 9%，在 2400×300 这类极端扁图上
  真值框归一化宽度只有 0.011，用 IoU 0.5 等于在考「框画得多紧」而不是「点定位准不准」。
  真值里用 `match: "center"` 切换判定方式，`center_hit()` 另有面积比上限防「框住整图」作弊。

### 网页截图组（第 ⑤ 组，2026-09 实测）

4 张由 **Playwright 渲染的真网页截图**（`scripts/capture_web_samples.mjs`），共 **28 个目标**；
真值不是画出来的，而是浏览器 `getBoundingClientRect()` 报出来的，与 PNG 由同一次抓取产出。

| 图 | 尺寸 | 目标 | 检测率 | 平均 IoU | 标签准确率 |
|---|---|---|---|---|---|
| `web_dashboard` 电商数据看板 | 1440×1056 | 7 | 7/7 | 0.877 | 100% |
| `web_shop` 商品列表页 | 1440×1023 | 8 | 8/8 | 0.923 | 88% |
| `web_form` 登录设置表单页 | 1440×900 | 6 | 6/6 | 0.880 | 100% |
| `web_article` 新闻文章详情页 | 1440×1150 | 7 | 7/7 | 0.742 | 100% |

（上表是**思考模式开启**；**关掉思考同样 28/28 全检出，平均耗时从 5.2s 降到 2.8s**，
报告在 `runs/web_ab/report.json`。）

> 复现：`python scripts\compare_thinking.py --samples web_dashboard,web_shop,web_form,web_article --out runs\web_ab`

**这一组踩到的三个坑**（都改在生成侧，不在模型侧）：

1. **真值框画在 CSS 块上会冤枉模型**。真值原本挂在带 label 的外层 `.field` 上，
   框把上方那行 label 也圈了进去，而模型自然只框输入框 → IoU 掉到 0.62，
   看着像"模型定位不准"，其实是在考"框画得多紧"。⇒ 真值改挂**控件本体**；
   块级标题（`h1` / `h2`）则挂到行内 `span` 上，否则块框横跨整张卡片。
2. **命中判定用 `match="center"`**（同圆点阵）。网页控件框高常常只有图高的 4%~6%
   （900px 截图里一个输入框才 45px），模型画的框系统性偏高约 30%——本地化其实是对的，
   IoU 却卡在 0.5 上下随机翻车。测"有没有找到"就用中心点，测"框得多紧"另看 `mean_iou`。
3. **提示词里不能出现真值标签本身**。早先写"② 页面顶部的活动横幅"，
   模型就照着提示词的词回了"活动横幅"，而图上印的是"限时秒杀"——
   它把提示词当成了答案。⇒ 提示词只给"位置 + 类别"（如"图表右侧的列表面板"），名称让模型自己去图上读。

另外网页元素的标签是**界面上的中文名称**，既没有颜色也没有形状，所以真值标 `label_mode="text"`，
走文本比对（`benchmark.text_label_ok`），并允许登记**别名**（`aliases`，来自抓取脚本的 `data-gt-alias`）：
搜索框在图上只有占位文字"搜索商品"，答"搜索框"显然也是对的，不该判错。
⚠️ 别名只用来避免"答对了却判错"，不能拿来兜住错误答案。

### 极端长宽比圆点图的实测结论（2026-09 实测）

| 图 | 思考开 | 思考关 | 标签准确率 |
|---|---|---|---|
| `marker_900`（常规） | 检出 9/9 | 7/9 | 100% |
| `marker_wide`（2400×300，8:1） | 检出 0/9 | 1/9 | 100% |
| `marker_tall`（800×2400，1:3） | 检出 3/9 | 3/9 | 100% |

⇒ 模型在这类图上**颜色全认对、位置基本全错**。推测原因：服务端会把图缩放后再送模型
（见 4.3 的帧探针结论），2400×300 缩到长边 1000 左右时圆点直径只剩 5~6 px，已经接近
有效分辨率下限。**这不是本项目代码的问题，是模型能力的边界**——演示时别把它当成 bug。

> 复现：`python scripts\compare_thinking.py --samples marker_900,marker_wide,marker_tall --out runs\thinking_ab_marker`

---

## 8. 待办 / 可扩展方向

- [ ] 评测：加入更多干扰（重叠图形、背景纹理、旋转文字、密集小目标），以及"点定位"精度评测。
- [ ] 评测：支持 A/B 多提示词自动对比（已有 `--system-prompt`，可再写批量脚本）。
- [ ] 前端：对比模式增加"差异高亮"、缩放/平移、标注列表点选定位。
- [ ] 后端：会话/历史持久化（当前 `IMAGES` 为进程内内存字典，重启即失）。
- [ ] 工具：增加 `crop_image`、`zoom_region` 等二次观察工具，让 agent "放大再看"。
- [ ] 支持多图 / 批量打标对比。
