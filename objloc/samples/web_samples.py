# -*- coding: utf-8 -*-
"""生成器 5：真实网页快照（Playwright 抓取，**真值来自浏览器 DOM**）。

职责
----
- `capture_web_samples()`：跑一次 `scripts/capture_web_samples.mjs` 并读回 manifest；
- `web_entry()`：取某个快照在 manifest 里的条目；
- `web_targets_to_gt()`：把 DOM 报出来的框转成本项目统一的真值格式；
- `_web_builder()`：把抓来的 PNG 复制进 samples 目录，返回 DOM 真值。

边界
----
只管第 ⑤ 组 —— 唯一一组**不由 Python 绘制**的图（前四组在 generators.py）。
本模块不改 PNG、不合成真值，只做搬运与格式转换。
"为什么真值必须读 manifest 而不能抄进代码""match=center 的由来""这一组的局限"
三段理由写在下方块注释里，逐行照搬未删。

旧文件对应关系（等价重构）
--------------------------
本模块 = 拆分前 `objloc/samples.py` 的「生成器 5」一节，逐行搬移、无改写。

@doc AGENTS.md#7-打标准确率评测结论重要
（该文档给出网页截图组的实测检出率 / 平均 IoU / 标签准确率。）
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

from objloc.config import PROJECT_ROOT, RUNS_DIR

# --------------------------------------------------------------------------- #
# 生成器 5：真实网页快照（Playwright 抓取，真值来自 DOM）
# --------------------------------------------------------------------------- #
# 与前面四组最大的不同：图**不是 Python 画出来的**，而是真浏览器渲染出来的网页截图；
# 真值也不是"我画的图形在哪"，而是浏览器自己报的 getBoundingClientRect()。
#
# 为什么不把真值抄进本文件：真值必须与 PNG 严格同源。抄一份就有漂移风险——
# 改了页面 HTML 重抓一次，PNG 变了而抄来的真值没变，评测会**安静地**给出错误结论。
# 这里读 manifest 取真值，两者由同一次抓取产出，天然同步；
# runs/ 被清掉时 build() 会自动重抓一次，保证可自愈。
#
# 演示时要讲清楚的局限：这一组考的是"读懂界面语义"（按钮/卡片/图表区在哪、叫什么），
# 不是前面几组那种纯几何定位。网页元素的边界本来就有歧义（一张卡片从哪算起？），
# 所以 IoU 一般明显低于几何图——这是任务性质决定的，不是模型退步。
WEB_SAMPLES_DIR = RUNS_DIR / "web_samples"
WEB_MANIFEST = WEB_SAMPLES_DIR / "manifest.json"
WEB_CAPTURE_SCRIPT = PROJECT_ROOT / "scripts" / "capture_web_samples.mjs"

_WEB_MANIFEST: dict | None = None


def capture_web_samples(*, force: bool = False) -> dict:
    """跑一次 Playwright 抓取（node scripts/capture_web_samples.mjs），返回 manifest。

    抓取是确定性的（页面内容固定、无动画/随机数），重复跑得到的 PNG 与真值一致，
    因此 runs/web_samples/ 可以当纯缓存看待——删掉只会让它重跑一次。
    """
    global _WEB_MANIFEST
    if _WEB_MANIFEST is not None and not force:
        return _WEB_MANIFEST
    if force or not WEB_MANIFEST.exists():
        if not WEB_CAPTURE_SCRIPT.exists():
            raise RuntimeError(
                f"缺少网页快照抓取脚本：{WEB_CAPTURE_SCRIPT}\n"
                "第 ⑤ 组「网页截图」测试图由它生成，见 AGENTS.md 第 5 节。"
            )
        proc = subprocess.run(
            ["node", str(WEB_CAPTURE_SCRIPT)],
            cwd=str(PROJECT_ROOT), capture_output=True, text=True,
            encoding="utf-8", errors="replace",
        )
        if proc.returncode != 0:
            raise RuntimeError(
                "网页快照抓取失败（需要本机可用的 node + Playwright，"
                "路径约定见 tests/ui/ui_check.mjs）：\n" + (proc.stderr or "")[-2000:]
            )
    if not WEB_MANIFEST.exists():
        raise RuntimeError(f"抓取脚本没有产出 {WEB_MANIFEST}")
    _WEB_MANIFEST = json.loads(WEB_MANIFEST.read_text(encoding="utf-8"))
    return _WEB_MANIFEST


def web_entry(sample_id: str, *, force: bool = False) -> dict:
    """取某个网页快照在 manifest 里的条目（含 targets 真值）。"""
    manifest = capture_web_samples(force=force)
    for entry in manifest.get("samples", []):
        if entry.get("id") == sample_id:
            return entry
    raise KeyError(f"manifest 里没有网页快照：{sample_id}")


def web_targets_to_gt(targets: list[dict]) -> list[dict]:
    """把抓取脚本产出的 targets 转成本项目统一的真值格式。

    label_mode="text" 是关键：网页目标的标签是界面上的中文名称（"总销售额"、"加入购物车"），
    既没有颜色也没有形状，必须走文本比对口径，否则标签准确率恒为 0。
    判定实现在 objloc/benchmark/labels.py: text_label_ok，理由见该函数的注释。

    命中判定统一用 match="center"（真值中心落在预测框内即算找到，另带面积比上限防作弊），
    理由是实测出来的，和圆点阵同一类问题：
    网页控件的框高常常只有图高的 4%~6%（900px 高的截图里一个输入框才 45px），
    而模型画的框系统性偏高约 30%——本地化其实是对的，IoU 却卡在 0.5 上下随机翻车。
    那测的是"框画得多紧"，不是"有没有找到这个界面元素"，而这一组要测的恰恰是后者。
    框的紧不紧另由 mean_iou 体现，两个指标一起看。
    """
    gt = []
    for t in targets:
        box = [min(max(float(v), 0.0), 1.0) for v in t["bbox_2d"][:4]]
        if len(box) != 4 or box[0] >= box[2] or box[1] >= box[3]:
            continue   # 退化的框算不出 IoU，直接丢弃而不是让整张图评分失真
        entry = {
            "bbox_2d": [round(v, 4) for v in box],
            "label": str(t["label"]),
            "label_mode": "text",
            "expect_shape": False,
            # 中心点命中，理由见 web_targets_to_gt 的 docstring
            "match": "center",
        }
        # 同一元素的其它合理叫法（抓取脚本的 data-gt-alias），见 benchmark.text_label_ok
        aliases = [str(a) for a in (t.get("aliases") or []) if str(a).strip()]
        if aliases:
            entry["aliases"] = aliases
        gt.append(entry)
    return gt


def _web_builder(sample_id: str):
    """构造某个网页快照的生成器：把抓来的 PNG 复制到 samples 目录，返回 DOM 真值。"""
    def build(path) -> list[dict]:
        entry = web_entry(sample_id)
        src = WEB_SAMPLES_DIR / str(entry.get("file") or f"{sample_id}.png")
        if not src.exists():
            # manifest 还在但 PNG 被删了：强制重抓一次自愈
            entry = web_entry(sample_id, force=True)
            src = WEB_SAMPLES_DIR / str(entry.get("file") or f"{sample_id}.png")
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        if src.resolve() != path.resolve():
            shutil.copyfile(src, path)
        return web_targets_to_gt(entry.get("targets", []))

    return build
