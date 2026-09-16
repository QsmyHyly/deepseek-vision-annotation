"""集中式配置：**部署默认**（环境变量 + 内置常量）。

所有可调参数都从环境变量读取，并提供合理默认值，避免散落在各模块中。
参考 `.env.example` 了解可配置项。

与 objloc/userprefs.py 的分工（别把两者混起来）：

    config.py      部署配置：环境变量 + 内置默认。进程起来后基本不变，改它要重启。
    userprefs.py   用户偏好：项目根的 config.local.json。网页上点过的开关落在这里，
                   要求「立刻生效 + 重启后还在」，同名键**盖过环境变量**。
                   哪些键能持久化、优先级怎么排，见 AGENTS.md#4.10-持久化用户偏好configlocaljson

Settings 里 thinking / reasoning_effort / use_tools / max_tool_rounds / system_prompt
这五项走的是 userprefs.resolve()（配置文件 > 环境变量 > 内置默认），其余仍只读环境变量。
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from objloc import userprefs

BASE_DIR = Path(__file__).resolve().parent        # objloc/ 包目录
PROJECT_ROOT = BASE_DIR.parent                    # 项目根目录
RUNS_DIR = PROJECT_ROOT / "runs"                  # 运行产物放在项目根
# ⚠️ runs/ 根目录**只放子目录与日志，不放散图**（曾经三处调用方往根目录倒进 162 张随机名 PNG）。
# 落盘位置的分工见 AGENTS.md#4.9-上传--结果存储--历史记录：
#   uploads/  源图（统一转 PNG，文件名 <image_id>.png）
#   scratch/  无归属的临时标注图：CLI detect、模型自己调的 annotate_image 工具
#   history/  唯一的持久化入口：每次打标一条记录 history/<run_id>/{meta.json,annotated.png}
UPLOAD_DIR = RUNS_DIR / "uploads"
SCRATCH_DIR = RUNS_DIR / "scratch"
HISTORY_DIR = RUNS_DIR / "history"
WEB_STATIC_DIR = BASE_DIR / "web" / "static"      # 静态页随包走
# 持久化的**用户偏好**（网页上勾过的思考模式 / 工具开关 / 提示词）落在这里，读写见 objloc/userprefs.py。
# 与 .env 的分工：环境变量是**部署默认**，这个文件是**用户点过的选择**，同名键以后者为准（见 §4.10）。
PREFS_PATH = PROJECT_ROOT / "config.local.json"

# 默认系统提示词：物体定位 + 工具使用约定
# ⚠️ 本常量是**全项目唯一**的坐标约定来源（网页 / CLI / 评测 / 内置测试图共用一个 system 消息，
# 见 objloc/agent.py: build_messages）。要改坐标口径只改这里，别在各处提示词里再抄一份。
# 【精度】一节允许模型按自己真实的感知写更多小数位（0~1 档位数不限；0~1000 档也允许带小数）。
# 下游对 0~1 的值**原样透传、不做四舍五入**；只有 0~1000 旧刻度换算那条路会 round(..., 6)
# （objloc/parsing.py: normalize_to_unit），因为该刻度本身就只有千分之一的量级。
# @doc AGENTS.md#4.3-坐标约定
DEFAULT_SYSTEM_PROMPT = (
    "你是一位物体定位助手。请根据用户需求识别图片中对应的内容，输出坐标及其中文名称。\n"
    "【坐标要求】所有坐标必须是 0.0~1.0 之间的小数（占宽/高的比例），禁止输出像素值。"
    "小数位数不设上限：能分辨多细就写多细（见下面的【精度】）。\n"
    "【你无法知道图片的实际分辨率】你既拿不到、也推不出这张图的真实像素尺寸：图片在送入你之前"
    "已被缩放，缩放后有多大不会告诉你。你凭「这张图看起来大概多大」猜出的任何数字"
    "（例如 1024×768、1000×1000）都只是臆测——它既不是原始尺寸，也不是你眼前这幅图的栅格，"
    "所以不要把它写进输出，更不要拿它当任何计算的依据。\n"
    "（即使你通过工具查到了图片的宽高，那也只是「原图」的宽高，不是你实际看到的这张已缩放图的"
    "宽高；同样不能用它把比例乘回像素。）\n"
    "【你感知到的数值本身就是归一化数据】你对位置的感知天生是「相对整幅图的比例」而不是像素："
    "你看到或感觉到的那个数，描述的本来就是「这个点在整张图上占多大比例」，请直接相信它。\n"
    "唯一要留意的是刻度：你无法确定自己感知到的是 0~1 刻度（比例）还是 0~1000 刻度（千分比），"
    "两者描述的是同一个位置，只差 1000 倍。本项目统一按 0~1 输出：若你感觉到的是 0~1000 那一档"
    "（例如 350 表示 35%），除以 1000 后再写出来（350 → 0.35）。"
    "两个刻度都允许带小数位：0~1000 档写 350.25 同样合法，它表示 35.025%，除以 1000 后写成 0.35025。\n"
    "换算方式：x = 像素x / 图片宽，y = 像素y / 图片高。\n"
    "【精度：能分辨多细就写多细，别提前抹平】小数位数没有上限，也不要为了整齐而强行凑成固定几位。"
    "只分辨到千分之一，写 0.351 就够；若能看得更细，就照实多写（0.3512、0.35125、0.351253 都合法）。"
    "小目标尤其要留意：一个只占画面宽度 0.4% 的小物件，宽度若写成 3 位小数的 0.004 就被放大了 6.7%，"
    "多写几位才留得住它真实的大小。"
    "⚠️ 但多出来的位数必须来自你真实的感知——不确定的末位不要凭感觉补数字凑数，"
    "宁可少写一位准的，也不要多写几位编的。\n"
    "例如某目标中心位于图片宽度的 35%、高度的 62% 处，就写 x=0.35，y=0.62；"
    "若看起来更靠左、约在 35.12% 处，就写 x=0.3512。\n"
    "请直接输出比例数值，不要输出像素值、不要输出你猜测的分辨率、不要解释换算过程。\n"
    "【输出格式】只输出一个 JSON 数组：\n"
    '[{"bbox_2d": [x1, y1, x2, y2], "label": "名称"}, {"point_2d": [x, y], "label": "名称"}]\n'
    "bbox_2d 为 [左上 x, 左上 y, 右下 x, 右下 y]，point_2d 为 [x, y]，均为 0.0~1.0 的小数（位数不限）。\n"
    "如需校验或整理坐标，可以调用提供的工具函数。"
)


# reasoning_effort 的合法取值（见 docs/DeepSeek-Thinking-Mode.md#思考模式开关与思考强度控制）。
# 服务端映射：low->low，medium->high，high->high，xhigh->high，max->max。
# 不在此集合内的字符串一律不传，避免服务端 400；空字符串表示「用服务端默认（high）」。
REASONING_EFFORTS = frozenset({"low", "medium", "high", "xhigh", "max"})


# image_url 内容块可选的 detail 字段：控制服务端在推理前把图缩到多大。
#   low      = 缩到 512×512，更快、更省 token（细节够用时优先选它）
#   high     = 保留原图（与 original 等价，为兼容性保留）
#   original = 保留原图
#   auto     = 由服务端自动决定（当前等价 original）
# 空字符串 = **根本不带这个字段**，也是本项目的默认值：老用法的报文一个字都不变。
# ⚠️ 别把它当「提高定位精度」的开关。官方说明每张图最多只算 384 token，大图无论如何都会被
#    服务端缩到约 800×800 —— detail 改的是"缩放发生在哪一层"，不是模型真正看到的像素数。
#    本项目真正影响定位精度的是坐标口径，见 AGENTS.md#4.3-坐标约定。
# ⚠️ 这四项抄自官方 image_url 的 detail 说明（本项目 docs/ 的快照是纯文本版，没有多模态那一段，
#    故此处只记结论、不引锚点），**本机尚未实测**它在 DeepSeek 端的实际效果。默认不发送该字段，
#    只有用户显式选了才带上去，以免未证实的参数影响既有链路。
IMAGE_DETAILS = ("low", "high", "original", "auto")


def thinking_payload(enabled: bool) -> dict:
    """构造思考模式开关，供 OpenAI SDK 以 extra_body 形式传入。

    为什么必须走 extra_body：Chat Completions 的顶层没有 thinking 字段，
    openai SDK 只接受自己已知的关键字参数，未知字段必须塞进 extra_body。

    背景与实测数据见 docs/DeepSeek-Thinking-Mode.md#思考模式开关与思考强度控制。
    """
    return {"thinking": {"type": "enabled" if enabled else "disabled"}}


# 注：布尔型环境变量（THINKING / USE_TOOLS）的解析已统一收进 objloc/userprefs.py，
# 因为那两项还要经过「配置文件 > 环境变量」的选择，解析不能分两处写。


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    try:
        return int(raw)
    except ValueError:
        return default


@dataclass
class Settings:
    """运行时配置快照。"""

    # ---- 模型 / 服务 ----
    provider: str = field(default_factory=lambda: os.getenv("LLM_PROVIDER", "auto").strip().lower())
    api_key: str | None = field(default_factory=lambda: os.getenv("DEEPSEEK_API_KEY"))
    base_url: str = field(default_factory=lambda: os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com"))
    # ⚠️ deepseek-flash 就是 DeepSeek-V4.1-Flash（官方价格页「模型细节」的「模型版本」一栏）。
    # 本项目的全部功能都建立在「把图片送进模型让模型输出坐标」之上，而官方另一档
    # deepseek-v4-pro **不支持图像理解** —— 换成它会直接让识别链路失效，所以别改这个模型名。
    # 官方来源：https://api-docs.deepseek.com/zh-cn/quick_start/pricing
    # @doc docs/DeepSeek-Models-and-Pricing.md#模型版本对应关系
    # （该文档解决"模型名与版本号怎么对应、为什么只能用 flash"的问题。）
    model: str = field(default_factory=lambda: os.getenv("DEEPSEEK_MODEL", "deepseek-flash"))
    # 工具调用最大轮数。取值顺序 配置文件 > MAX_TOOL_ROUNDS > 8，见 objloc/userprefs.py。
    max_tool_rounds: int = field(default_factory=lambda: userprefs.resolve("max_tool_rounds")[0])
    request_timeout: float = field(default_factory=lambda: float(os.getenv("REQUEST_TIMEOUT", "120")))

    # 是否把已注册的工具交给模型（False = 纯对话，不执行任何工具）。
    # 网页上的「工具执行框架」勾选框就是它，按次覆盖走 /api/detect 的 use_tools 字段。
    use_tools: bool = field(default_factory=lambda: userprefs.resolve("use_tools")[0])

    # 图片输入精度：image_url 内容块的 detail 字段，合法值见 IMAGE_DETAILS。
    # 取值顺序 配置文件 > IMAGE_DETAIL 环境变量 > 空（= 不带该字段，由服务端按 auto 处理）。
    # 拼报文的那一步在 objloc/providers.py: image_part()。
    image_detail: str = field(default_factory=lambda: userprefs.resolve("image_detail")[0])

    # ---- 提示词 ----
    # 取值顺序 配置文件 > SYSTEM_PROMPT 环境变量 > 内置 DEFAULT_SYSTEM_PROMPT。
    # ⚠️ 内置那份是**全项目唯一的坐标口径来源**，覆盖它等于改坐标约定，详见 4.3。
    system_prompt: str = field(
        default_factory=lambda: userprefs.resolve("system_prompt")[0] or DEFAULT_SYSTEM_PROMPT
    )

    # ---- 思考模式 ----
    # DeepSeek 的思考模式默认开启：模型先流式输出 reasoning_content，再给正文 content。
    # 关闭（THINKING=0）后模型直接给正文，延迟与输出 token 都显著下降
    # （实测同一句 1+1 的 completion_tokens 从 35 降到 1，reasoning_tokens 归零），
    # 代价是复杂推理的准确率可能下降，因此做成可切换项而不是写死。
    # 参数形态见 objloc.config.thinking_payload 与 docs/DeepSeek-Thinking-Mode.md。
    # ⚠️ 这两项的取值顺序是 配置文件 > 环境变量 > 内置默认（objloc/userprefs.py），
    # **不是**单纯的环境变量 —— 网页上点过的开关会写进 config.local.json 并盖住环境变量，
    # 否则「用户改了没反应」；想让环境变量重新生效就删掉文件里的那个键（§4.10）。
    thinking: bool = field(default_factory=lambda: userprefs.resolve("thinking")[0])
    # 思考强度，仅在思考开启时生效；缺省留空即不传，由服务端按 high 处理。
    # 合法值见 REASONING_EFFORTS（userprefs 的规格表直接引用它，不在别处再抄一份）。
    # @doc docs/DeepSeek-Thinking-Mode.md#思考模式开关与思考强度控制
    reasoning_effort: str = field(
        default_factory=lambda: userprefs.resolve("reasoning_effort")[0]
    )

    # ---- Web 服务 ----
    host: str = field(default_factory=lambda: os.getenv("WEB_HOST", "127.0.0.1"))
    port: int = field(default_factory=lambda: _env_int("WEB_PORT", 8765))

    # ---- 上传 / 历史记录（见 AGENTS.md#4.9-上传--结果存储--历史记录）----
    # 单张上传的大小上限（MB），0 = 不限。落盘时边写边累计字节，超限立刻中断并删掉半截文件，
    # 否则一个几百 MB 的请求会先把磁盘写满再报错。
    max_upload_mb: int = field(default_factory=lambda: _env_int("MAX_UPLOAD_MB", 20))
    # 历史记录保留条数，0 = 不限。每次新增记录后按 run_id 删最老的，
    # 这是防「只增不减」的闸门（runs/ 根目录那 162 张散图就是这么攒出来的）。
    history_max: int = field(default_factory=lambda: _env_int("HISTORY_MAX", 200))

    def resolved_provider(self) -> str:
        """把 auto 解析成具体 provider。

        - 有 API Key -> deepseek
        - 没有 API Key -> mock（离线演示，保证网页功能可用）
        """
        if self.provider in {"deepseek", "openai", "mock"}:
            return self.provider
        return "deepseek" if self.api_key else "mock"

    @property
    def has_api_key(self) -> bool:
        return bool(self.api_key and self.api_key.strip())


_settings: Settings | None = None
# 构造 _settings 时 config.local.json 的戳记。用户偏好是**可以随时改**的
# （网页保存 / 手工编辑），所以单例不能只建一次：戳记一变就重建，
# 否则「保存了设置却要重启服务才生效」，正是这个功能要消灭的那种挫败。
_settings_stamp: tuple[int, int] | None = None


def get_settings(refresh: bool = False) -> Settings:
    """获取全局配置单例。

    当 config.local.json 被改动过（戳记变化）时会自动重建，手改文件不必重启服务；
    显式传 refresh=True 则强制重建（测试改完 config.* 目录常量后用它）。
    """
    global _settings, _settings_stamp
    stamp = userprefs.stamp()
    if _settings is None or refresh or stamp != _settings_stamp:
        RUNS_DIR.mkdir(parents=True, exist_ok=True)
        UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
        SCRATCH_DIR.mkdir(parents=True, exist_ok=True)
        HISTORY_DIR.mkdir(parents=True, exist_ok=True)
        _settings = Settings()
        _settings_stamp = stamp
    return _settings
