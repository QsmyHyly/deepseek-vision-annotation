# -*- coding: utf-8 -*-
"""离线自测：@doc 交叉引用的一致性。

为什么需要它
------------
项目约定「文档就近写在代码里；凡另有独立文档，代码必须在就近位置用 @doc 引用」，
并且「文档一旦挪动锚点，必须同步全仓库的 @doc 引用」。全靠人记就会漏，所以固化成测试。

检查三件事：
  1. 每个 @doc 指向的文件必须真实存在（改名/挪目录后最容易断的就是这里）；
  2. docs/ 下的每份文档都必须被至少一处代码 @doc 引用（否则就是孤儿文档）；
  3. 锚点（如果有）应当能在目标文档里对上一个标题——按"忽略标点与大小写"的宽松口径比对。

第 3 条只告警不失败：本项目用的锚点是**人类可读标签**（如 #4.3-坐标约定），
而 GitHub 自动 slug 会去掉点号、生成 #43-坐标约定曾踩坑，两者本就不等价。
约定见 AGENTS.md#4.8-文档与注释约定。

用法：python tests/test_docrefs.py
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SKIP_PARTS = {".git", "runs", "__pycache__", "node_modules", ".venv"}

# 锚点里可能出现的中文标点与引号，遇到即截断（它们属于正文，不属于锚点）
STOP = "\"'\u3000\uff09\u3002\uff0c\u3001<\uff1b\uff1a"
# 目标必须是"像路径的东西"（纯 ASCII 路径字符）。这条限制是必要的：本文件与 AGENTS.md 的正文里
# 也会出现「@doc 引用」这样的行文，若只按空档切分，脚本会把自己的说明文字当成引用去查文件。
REF_RE = re.compile(r"@doc\s+(?P<target>[A-Za-z0-9_./\\-]+)(?:#(?P<anchor>[^\s" + re.escape(STOP) + r"]+))?")


def loose(text: str) -> str:
    """宽松归一化：只保留文字与数字，忽略标点、空格、大小写。"""
    return re.sub(r"[^0-9a-z\u4e00-\u9fff]", "", text.lower())


def collect_headings(path: Path) -> list[str]:
    body = path.read_text(encoding="utf-8", errors="ignore")
    return re.findall(r"^#{1,6}\s+(.+?)\s*$", body, re.M)


def iter_sources():
    for pattern in ("*.py", "*.mjs", "*.html", "*.js", "*.md"):
        for f in ROOT.rglob(pattern):
            if any(p in SKIP_PARTS for p in f.parts):
                continue
            # docs/ 下面是被引用的文档本身，文件头有 `@doc <本文件>#<锚点>` 的格式说明，不是真引用
            if (ROOT / "docs") in f.parents:
                continue
            yield f


def main() -> int:
    failures: list[str] = []
    warnings: list[str] = []
    referenced: set[Path] = set()
    total = 0

    for src in iter_sources():
        text = src.read_text(encoding="utf-8", errors="ignore")
        for m in REF_RE.finditer(text):
            target, anchor = m.group("target"), m.group("anchor")
            total += 1
            path = (src.parent / target)
            if not path.exists():
                path = ROOT / target
            if not path.exists():
                failures.append(f"{src.relative_to(ROOT)} -> 目标文件不存在: {target}")
                continue
            path = path.resolve()
            referenced.add(path)
            if anchor and anchor != "<锚点>":
                heads = collect_headings(path)
                if not any(loose(anchor) in loose(h) or loose(h) in loose(anchor) for h in heads):
                    warnings.append(f"{src.relative_to(ROOT)} -> {target}#{anchor} 对不上任何标题")

    docs_dir = ROOT / "docs"
    if docs_dir.is_dir():
        for doc in sorted(docs_dir.glob("*.md")):
            if doc.resolve() not in referenced:
                failures.append(f"孤儿文档（没有任何代码 @doc 引用它）: docs/{doc.name}")

    print(f"@doc 引用总数: {total}")
    print(f"被引用的文档: {len({p for p in referenced})} 个")
    for w in warnings:
        print("WARN " + w)
    for f in failures:
        print("FAIL " + f)

    if failures:
        print(f"\n{len(failures)} 项失败")
        return 1
    print(f"\nDOCREF TESTS PASSED（{len(warnings)} 条锚点告警，属人类可读锚点的预期差异）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
