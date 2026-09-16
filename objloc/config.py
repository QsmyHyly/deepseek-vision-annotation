"""集中式配置。

所有可调参数都从环境变量读取，并提供合理默认值，避免散落在各模块中。
参考 `.env.example` 了解可配置项。
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent        # objloc/ 包目录
PROJECT_ROOT = BASE_DIR.parent                    # 项目根目录
RUNS_DIR = PROJECT_ROOT / "runs"                  # 运行产物放在项目根
UPLOAD_DIR = RUNS_DIR / "uploads"
WEB_STATIC_DIR = BASE_DIR / "web" / "static"      # 静态页随包走

# 默认系统提示词：物体定位 + 工具使用约定
# ⚠️ 本常量是**全项目唯一**的坐标约定来源（网页 / CLI / 评测 / 内置测试图共用一个 system 消息，
# 见 objloc/agent.py: build_messages）。要改坐标口径只改这里，别在各处提示词里再抄一份。
# @doc AGENTS.md#4.3-坐标约定
DEFAULT_SYSTEM_PROMPT = (
    "你是一位物体定位助手。请根据用户需求识别图片中对应的内容，输出坐标及其中文名称。\n"
    "【坐标要求】所有坐标必须是 0.0~1.0 之间的小数（占宽/高的比例），禁止输出像素值。\n"
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
    "（例如 350 表示 35%），除以 1000 后再写出来（0.35）。\n"
    "换算方式：x = 像素x / 图片宽，y = 像素y / 图片高，结果保留 3~4 位小数。\n"
    "例如某目标中心位于图片宽度的 35%、高度的 62% 处，就写 x=0.35，y=0.62。\n"
    "请直接输出比例数值，不要输出像素值、不要输出你猜测的分辨率、不要解释换算过程。\n"
    "【输出格式】只输出一个 JSON 数组：\n"
    '[{"bbox_2d": [x1, y1, x2, y2], "label": "名称"}, {"point_2d": [x, y], "label": "名称"}]\n'
    "bbox_2d 为 [左上 x, 左上 y, 右下 x, 右下 y]，point_2d 为 [x, y]，均为 0.0~1.0 的小数。\n"
    "如需校验或整理坐标，可以调用提供的工具函数。"
)


# reasoning_effort 的合法取值（见 docs/DeepSeek-Thinking-Mode.md#思考模式开关与思考强度控制）。
# 服务端映射：low->low，medium->high，high->high，xhigh->high，max->max。
# 不在此集合内的字符串一律不传，避免服务端 400；空字符串表示「用服务端默认（high）」。
REASONING_EFFORTS = frozenset({"low", "medium", "high", "xhigh", "max"})


def thinking_payload(enabled: bool) -> dict:
    """构造思考模式开关，供 OpenAI SDK 以 extra_body 形式传入。

    为什么必须走 extra_body：Chat Completions 的顶层没有 thinking 字段，
    openai SDK 只接受自己已知的关键字参数，未知字段必须塞进 extra_body。

    背景与实测数据见 docs/DeepSeek-Thinking-Mode.md#思考模式开关与思考强度控制。
    """
    return {"thinking": {"type": "enabled" if enabled else "disabled"}}


def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "y", "on"}


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
    max_tool_rounds: int = field(default_factory=lambda: _env_int("MAX_TOOL_ROUNDS", 8))
    request_timeout: float = field(default_factory=lambda: float(os.getenv("REQUEST_TIMEOUT", "120")))

    # ---- 提示词 ----
    system_prompt: str = field(default_factory=lambda: os.getenv("SYSTEM_PROMPT", DEFAULT_SYSTEM_PROMPT))

    # ---- 思考模式 ----
    # DeepSeek 的思考模式默认开启：模型先流式输出 reasoning_content，再给正文 content。
    # 关闭（THINKING=0）后模型直接给正文，延迟与输出 token 都显著下降
    # （实测同一句 1+1 的 completion_tokens 从 35 降到 1，reasoning_tokens 归零），
    # 代价是复杂推理的准确率可能下降，因此做成可切换项而不是写死。
    # 参数形态见 objloc.config.thinking_payload 与 docs/DeepSeek-Thinking-Mode.md。
    thinking: bool = field(default_factory=lambda: _env_bool("THINKING", True))
    # 思考强度，仅在思考开启时生效；缺省留空即不传，由服务端按 high 处理。
    # 合法值见 REASONING_EFFORTS。
    # @doc docs/DeepSeek-Thinking-Mode.md#思考模式开关与思考强度控制
    reasoning_effort: str = field(
        default_factory=lambda: os.getenv("REASONING_EFFORT", "").strip().lower()
    )

    # ---- Web 服务 ----
    host: str = field(default_factory=lambda: os.getenv("WEB_HOST", "127.0.0.1"))
    port: int = field(default_factory=lambda: _env_int("WEB_PORT", 8765))

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


def get_settings(refresh: bool = False) -> Settings:
    """获取全局配置单例。"""
    global _settings
    if _settings is None or refresh:
        RUNS_DIR.mkdir(parents=True, exist_ok=True)
        UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
        _settings = Settings()
    return _settings
