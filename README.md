# DeepSeek 物体定位演示软件（v2 架构）

> 📌 **项目长期记忆 / 协作约定见 [`AGENTS.md`](./AGENTS.md)** —— 接手本项目请先读它。

基于 DeepSeek 多模态模型的「图像目标定位 + 打标」演示程序，包含四块能力：

1. **流式输出**：模型的思考过程与正文边生成边显示（终端 / 网页 / SSE 统一事件协议）。
2. **工具执行框架**：模型输出工具调用后，由后端真正执行对应 Python 函数，再把结果回填给模型继续推理。
3. **网页对比查看**：浏览器页面支持「打标前 / 打标后」并排、滑块、叠加三种对比方式。
4. **打标准确率评测**：用代码批量生成「已知答案」的图片，自动计算检出率 / IoU / 标签准确率。
5. **思考模式开关**：可关闭模型的思维链（更快、更省 token），网页勾选框 / CLI / 环境变量三处可控。

---

## 1. 目录结构

```
deepseek物体定位演示软件/
├── main.py                  # CLI 入口：web / detect / tools / demo
├── requirements.txt
├── .env.example
├── README.md
├── AGENTS.md                # 项目长期记忆（先读这个）
├── docs/                    # DeepSeek 官方 API 文档快照
│   ├── DeepSeek-Chat-Completions-API.md
│   ├── DeepSeek-Tool-Calls.md
│   ├── DeepSeek-Thinking-Mode.md   # 思考模式开关 / 强度 / reasoning_content 回传规则
│   └── DeepSeek-Models-and-Pricing.md  # 模型名 ↔ 版本号对应、为什么只能用 deepseek-flash
├── objloc/                  # ★ 核心包
│   ├── config.py            #   集中配置
│   ├── providers.py         #   模型客户端（流式事件协议 + OpenAI 兼容 + 离线 Mock）
│   ├── client.py            #   客户端门面（stream_chat / infer / 旧签名兼容）
│   ├── parsing.py           #   坐标 JSON 解析
│   ├── tool_schema.py       #   由函数签名生成工具 JSON Schema
│   ├── visualizer.py        #   图像加载 + 标注绘制（跨平台中文字体）
│   ├── agent.py             #   Agent 主循环（流式 + 工具执行）
│   ├── benchmark.py         #   合成图片生成 + 准确率指标（纯逻辑）
│   ├── samples.py           #   内置测试图片目录（网页面板与脚本共用的生成源）
│   │                        #   第 ⑤ 组「网页截图」的真值来自抓取脚本产出的 manifest.json
│   ├── tools/               #   工具执行框架
│   │   ├── registry.py
│   │   └── builtin.py
│   └── web/                 #   FastAPI 服务 + 对比页面
│       ├── app.py
│       └── static/index.html
├── scripts/
│   ├── benchmark.py         # 评测 CLI：生成图片 + 调模型 + 出报告
│   ├── report.py            # 美化打印评测报告
│   ├── compare_thinking.py  # 思考模式 A/B 对照（开/关各跑一遍比准确率与耗时）
│   ├── probe_vision_frame.py    # 探测模型实际看到的画面几何（缩放 / 补边 / 最小可读字号）
│   ├── verify_gt.py             # 独立复核：从渲染好的 PNG 像素重量包围盒，验证真值本身没错
│   └── capture_web_samples.mjs  # 第 ⑤ 组网页截图的唯一生成源（Playwright 渲染 + DOM 真值）
├── tests/                   # 自测
│   ├── test_parser.py       #   纯离线，不花 API、不需要服务
│   ├── test_e2e.py
│   ├── test_benchmark.py
│   └── ui/                  #   前端回归自检（改 index.html 后跑）
└── runs/                    # 运行产物（上传图、标注图、评测结果、日志）
```

---

## 2. 架构总览

```
                 ┌──────────────────────── 前端（浏览器） ────────────────────────┐
                 │  index.html : 上传/URL/示例 → 对比查看器 → 流式控制台        │
                 └───────────────┬───────────────────────────────┬───────────────┘
                                 │ POST /api/detect (SSE)        │ POST /api/annotate
                                 ▼                               ▼
        ┌────────────────────────────────────────────────────────────────────┐
        │                        objloc/web/app.py (FastAPI)                 │
        └───────────────┬───────────────────────────────┬────────────────────┘
                        ▼                               ▼
        ┌───────────────────────────┐        ┌────────────────────────────┐
        │  agent.run_agent(...)     │        │ visualizer.render_annotations│
        │  流式输出 + 工具执行循环   │        │ 打标绘制（bbox / point）     │
        └───────┬───────────┬───────┘        └────────────────────────────┘
     stream_chat│           │execute(name, args, context)
                ▼           ▼
     ┌────────────────┐  ┌──────────────────────────────┐
     │ providers.py   │  │ tools/registry.ToolRegistry  │
     │ 统一流式事件： │  │ 注册表 → schema → 执行 → 结果 │
     │ reasoning /    │  │ 内置：parse_coordinates、     │
     │ content /      │  │ get_image_info、annotate_image│
     │ tool_call_delta│  └──────────────────────────────┘
     │ / finish       │
     └────────────────┘
```

### 2.1 流式输出

统一事件协议（`objloc/providers.py`）：

| 事件类型 | 含义 |
|---|---|
| `reasoning` | 思考过程增量（DeepSeek `reasoning_content`） |
| `content` | 正文增量 |
| `tool_call_delta` | 工具调用参数增量（按 `index` 聚合） |
| `finish` | 本轮结束，携带 `finish_reason` |

终端、网页、SSE 共用同一套事件，因此流式展示只有一份实现。简易入口：`objloc.client.stream_text()`。

### 2.2 工具执行框架

- `register(func)`：schema 从**函数名 + docstring + 参数 `Annotated` 注解**自动生成。
- `execute(name, args, context=...)`：解析参数 → 执行 → 序列化；异常回填给模型，不抛穿。
- **上下文注入**：模型无法知道的参数（如当前图片地址）用
  `registry.register_with_context(func, {"source"})` 注册，`source` 从 schema 隐藏、执行时注入并覆盖模型编造值。
- `run_agent` 循环执行工具直到模型不再请求。

内置工具：`parse_coordinates`、`decode_json_points`、`extract_coordinates`、
`get_image_info`、`annotate_image`、`list_palette_colors`。

### 2.3 网页对比查看

`objloc/web/static/index.html`（单文件、原生 JS、零构建、无外部依赖）：**并排 / 滑块 / 叠加**三种对比模式，
实时流式控制台（思考过程、正文、工具调用与结果），并支持无 Key 的「手动标注」。

三栏布局：左栏「图片来源（本地上传 / 图片 URL / 内置测试图）+ 识别参数 + 手动标注」，
中栏「对比查看区」（主角，占满剩余高度），右栏「流式控制台」（可折叠成竖条，把宽度让给对比区）。
三种对比模式的左右顺序统一为**原图在左、标注图在右**；窄屏（< 1100px）自动退化为单列。

设计说明、状态模型与取舍写在 `index.html` 头部的块注释里。改完页面务必跑一遍自检：

```bash
python tests/ui/check_frontend_contract.py   # id 双向校验：缺失 / 死元素必须为 0（离线）
node   tests/ui/ui_check.mjs                 # 真实浏览器驱动页面：对比几何 / 滑块 / 折叠 / 窄屏（离线）
```

---

## 3. 快速开始

```bash
pip install -r requirements.txt

# 配置 API Key（Windows 用户级环境变量 / .env 均可）
set DEEPSEEK_API_KEY=sk-xxx        # Windows
export DEEPSEEK_API_KEY=sk-xxx     # Linux/macOS

python main.py web                 # → http://127.0.0.1:8765
python main.py detect --image <url|path>   # 命令行流式识别并保存标注图
python main.py detect --image a.png --no-thinking   # 关闭思考模式（更快、更省 token）
python main.py demo                        # 离线跑通流式 + 工具执行（Mock）
```

未配置 `DEEPSEEK_API_KEY` 时自动进入**离线 Mock 模式**，仍可体验流式输出、工具执行与对比页面。

**注意**：若你刚在系统里新增了环境变量，**需要新开终端**（已打开的会话读不到）。

---

## 4. 打标准确率评测

用代码生成「已知答案」的几何图形图片（随机颜色 × 形状 × 位置），真值换算成 0.0~1.0 相对比例坐标，
让真实模型打标，再按 IoU（默认阈值 0.5）匹配，并按颜色 / 形状判定标签。

```bash
# 生成图片 + 调模型评测（默认 5 张 × 3 图形）
python scripts/benchmark.py --count 5 --n-shapes 3 --annotate

# 只生成图片（不花钱，先看素材）
python scripts/benchmark.py --images-only --count 5

# 加压场景
python scripts/benchmark.py --count 10 --seed 2026 --n-shapes 4 --annotate --out runs/benchmark_v2_hard

# 查看报告
python scripts/report.py runs/benchmark/report.json
```

产物：`runs/benchmark/images/*.png` + `ground_truth.json`、`runs/benchmark/report.json`、`annotated/*.png`。

### 实测结果

| 场景 | 检出率 | 平均 IoU | 颜色 | 形状 | 标签(颜色+形状) |
|---|---|---|---|---|---|
| 5 图 × 3 图形（旧提示词，坐标约定含糊） | 40% (6/15) | 0.655 | 100% | 100% | 100% |
| 5 图 × 3 图形（**修正提示词**） | **100%** (15/15) | **0.897** | 100% | 100% | 100% |
| 10 图 × 4 图形（加压） | **100%** (40/40) | **0.896** | 100% | 100% | 100% |

> 关键发现：模型"看懂图 + 认颜色形状"很强（标签 100%），主要失分来自**坐标约定没遵守**——
> 旧提示词里"无需考虑图片分辨率/坐标系转换"与"使用归一化值"互相矛盾，模型经常直接输出**像素坐标**，
> 导致检出率只有 40%。改为显式换算公式后提升到 100%。详见 `AGENTS.md` 第 7 节。

指标定义：**检出率** = 命中数 / 真值数；**平均 IoU** 仅在匹配对上统计；
**精确率** = 命中数 / 预测数。

---

## 5. 配置项（`.env.example`）

| 变量 | 默认 | 说明 |
|---|---|---|
| `DEEPSEEK_API_KEY` | 空 | API Key，空则进入 Mock 模式 |
| `DEEPSEEK_BASE_URL` | `https://api.deepseek.com` | 可指向任意 OpenAI 兼容服务 |
| `DEEPSEEK_MODEL` | `deepseek-flash` | 模型名。`deepseek-flash` **就是 DeepSeek-V4.1-Flash**；官方另一档 `deepseek-v4-pro` 不支持图像理解，本项目不能换 |
| `LLM_PROVIDER` | `auto` | `auto` / `deepseek` / `mock` |
| `MAX_TOOL_ROUNDS` | `8` | 工具调用最大轮数 |
| `REQUEST_TIMEOUT` | `120` | 单次请求超时（秒） |
| `WEB_HOST` / `WEB_PORT` | `127.0.0.1` / `8765` | 网页服务监听地址 |
| `SYSTEM_PROMPT` | 内置 | 覆盖系统提示词 |
| `THINKING` | `1` | 思考模式默认开关（`0` 关闭） |
| `REASONING_EFFORT` | 空 | 思考强度 `low/medium/high/xhigh/max`，空 = 服务端默认 `high` |

---

## 6. Web API

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/` | 对比查看页面 |
| GET | `/api/health` | 运行状态（provider / 工具列表） |
| GET | `/api/tools` | 已注册工具清单 |
| POST | `/api/upload` | 上传图片（multipart） |
| POST | `/api/upload_url` | 通过 URL 登记图片 |
| POST | `/api/detect` | **SSE 流式识别**（流式输出 + 工具执行 + 标注）；请求体可带 `thinking: true/false` |
| POST | `/api/annotate` | 手动标注（给定坐标 JSON，离线可用） |
| POST | `/api/sample` | 载入内置示例图 |
| GET | `/api/samples` | 内置测试图目录（按测试目的分组，见 `objloc/samples.py`） |
| GET | `/api/samples/{id}/image` | 测试图原图（首次访问时现场生成） |
| POST | `/api/samples/{id}/load` | 把测试图设为当前图片，并回传真值与建议提示词 |
| GET | `/api/file/{id}` | 原图 |
| GET | `/api/result/{name}` | 标注结果图 |

页面左侧「**测试图片**」下拉里有 17 张内置测试图（基准几何图 / 分辨率扫描 / 彩色圆点 /
文字可读性阶梯 / 竖线条纹 / **网页截图**），点「载入测试图」即可；**带真值的图在识别完成后会自动算出
检出率、平均 IoU、标签准确率**（后端 `objloc/samples.py: accuracy()`，全部由程序判定，不靠人眼）。
其中「网页截图」一组是唯一由**真浏览器渲染**、真值取自 **DOM `getBoundingClientRect()`** 的图：
由 `scripts/capture_web_samples.mjs`（Playwright）一次性抓取到 `runs/web_samples/`，删了会自动重抓。
「填入真值」按钮把真值坐标填进手动标注框，可离线画出标准答案做对照。

`/api/detect` 的 SSE 事件类型：`round_start`、`reasoning`、`content`、`tool_call`、
`tool_result`、`message`、`done`、`annotated`、`warning`、`error`、`eof`。

其中 `annotated` 事件在图片来自内置测试图时会额外带一个 `accuracy` 字段
（检出率 / 平均 IoU / 标签准确率 / 坐标空间诊断 `coord_space`）。

---

## 7. 坐标约定

模型输出 **0.0~1.0 的相对比例**（`x = 像素x / 图宽`，`y = 像素y / 图高`，保留 3~4 位小数）：

```json
[
  {"bbox_2d": [x1, y1, x2, y2], "label": "目标名称"},
  {"point_2d": [x, y], "label": "目标名称"}
]
```

`objloc.parsing.decode_json_points` 能容忍代码块标记、前后说明文字、单引号等常见情况。

**模型能调用的工具同样只认 0.0~1.0**：`parse_coordinates` / `decode_json_points` /
`extract_coordinates` / `annotate_image` 都在工具层做了统一换算（`tools/builtin.py: _unitize()`），
工具描述里也写明了坐标约定；万一模型仍给 0~1000 旧刻度，会自动除以 1000 换算回来。
纯解析核心 `objloc/parsing.py` 刻意保持"原样解析"，因为坐标空间探针 `probe_vision_frame.py` 要靠它读原始输出。

---

## 8. 思考模式开关

DeepSeek 默认先输出一段思维链（`reasoning_content`）再给正文。本项目可以把它**关掉**：

| 入口 | 用法 |
|---|---|
| 网页 | 左栏「② 识别参数」卡片里的 **思考模式** 勾选框与 **思考强度** 下拉（选择记在浏览器本地） |
| API | `POST /api/detect` 的 `thinking` 字段（`true`/`false`，省略则用服务端默认） |
| CLI | `python main.py detect --no-thinking`、`--thinking --reasoning-effort low` |
| 评测 | `python scripts/benchmark.py --no-thinking` |
| 服务端默认 | 环境变量 `THINKING=0`、`REASONING_EFFORT=low` |

实现要点：`thinking` 不是 Chat Completions 的顶层字段，**必须通过 `extra_body` 传**
（`{"thinking": {"type": "enabled"/"disabled"}}`）；强度 `reasoning_effort` 反而是顶层参数。
合法强度：`low` / `medium` / `high` / `xhigh` / `max`。详见 `docs/DeepSeek-Thinking-Mode.md`。

### 实测差别（脚本：`scripts/compare_thinking.py`）

```bash
python scripts/compare_thinking.py                       # 3 张基准图，开关各跑一遍
python scripts/compare_thinking.py --samples marker_900 --repeat 2
```

| 场景 | 模式 | 平均耗时 | 思维链字符 | 检出率 | 平均 IoU | 标签准确率 |
|---|---|---|---|---|---|---|
| 基准几何图 ×3 | 思考开 | 4.9 s | 1436 | 100% | 0.842 | 100% |
| 基准几何图 ×3 | **思考关** | **2.2 s** | 0 | 100% | 0.873 | 100% |

> 结论：在「看图找几何图形」这类任务上，**关掉思考约快 2.2 倍，准确率不下降**。
> 关掉后网页右栏一个 `reasoning` 事件都不会出现，事件总数大幅减少；
> 密集小目标、需要推理比较的难任务建议保留思考模式。

> ⚠️ 换模型 / 换服务商前请先验证该模型是否支持 `thinking` 参数，不要假设通用。
