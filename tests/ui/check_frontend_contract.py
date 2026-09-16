# -*- coding: utf-8 -*-
"""前端契约自检：index.html 里 JS 引用的 id 与 HTML 声明的 id 必须双向一致。

为什么要有这个脚本
------------------
页面是单文件、零构建，没有编译器帮忙查拼写错误：$("btnDetct") 这类手滑在浏览器里
只会表现为"某个按钮点了没反应"，很难排查。这个脚本做三件事：

  1. 正向：JS 里每个 $("x") / getElementById("x") / querySelector("#x") 引用的 id，
     都必须在 HTML 里真实存在（缺失数必须为 0）；
  2. 反向：HTML 里每个 id 都必须能被取到——被 JS 字符串字面量、HTML 的 idref 属性
     （for / aria-controls / aria-labelledby …）、或 CSS 的 #id 选择器引用过；
     否则它就是死元素（"永远取不到"）；
  3. 附加：JS 里用到的 [data-*] 选择器钩子必须在 HTML 里存在对应属性。

用法：python tests/ui/check_frontend_contract.py [html路径]

配套的浏览器端自检见同目录 ui_check.mjs（离线，需要本地服务在跑）；
会花 API 的两支是 ui_check_detect.mjs 与 smoke_detect.py。
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]  # tests/ui/ -> 项目根
DEFAULT_HTML = ROOT / "objloc" / "web" / "static" / "index.html"


def extract_block(html: str, tag: str) -> str:
    """取出最后一个 <tag>…</tag> 的内容（页面只有一个 style / script 块）。"""
    matches = re.findall(rf"<{tag}[^>]*>(.*?)</{tag}>", html, re.S)
    return matches[-1] if matches else ""


def main() -> int:
    path = Path(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_HTML
    html = path.read_text(encoding="utf-8")
    js = extract_block(html, "script")
    css = extract_block(html, "style")

    declared = re.findall(r'\bid="([^"]+)"', html)
    declared_set = set(declared)
    dupes = sorted({i for i in declared if declared.count(i) > 1})

    # ---- 1. JS 直接引用的 id ----
    direct: set[str] = set()
    direct |= set(re.findall(r'\$\("([^"]+)"\)', js))
    direct |= set(re.findall(r'getElementById\(\s*"([^"]+)"', js))
    direct |= set(re.findall(r'querySelector\(\s*"#([A-Za-z][\w-]*)"', js))
    missing = sorted(i for i in direct if i not in declared_set)

    # ---- 2. 反向：谁引用了这个 id ----
    js_literals = set(re.findall(r'"([^"\n]*)"', js)) | set(re.findall(r"'([^'\n]*)'", js))
    idrefs: set[str] = set()
    for attr in ("for", "aria-controls", "aria-labelledby", "aria-describedby", "list", "form"):
        for value in re.findall(rf'\b{attr}="([^"]+)"', html):
            idrefs |= set(value.split())
    idrefs |= set(re.findall(r'href="#([^"]+)"', html))
    css_refs = set(re.findall(r"#([A-Za-z][\w-]*)", css))

    unreachable = sorted(
        i for i in declared_set
        if i not in js_literals and i not in idrefs and i not in css_refs
    )

    # ---- 3. 附加：JS 用到的 data-* 选择器钩子 ----
    hooks = sorted(set(re.findall(r'\[(data-[\w-]+)\]', js)))
    html_attrs = set(re.findall(r'\b(data-[\w-]+)=', html))
    missing_hooks = [h for h in hooks if h not in html_attrs]

    print(f"文件: {path}")
    print(f"HTML 声明的 id 数: {len(declared_set)}（重复声明: {dupes or '无'}）")
    print(f"JS 直接引用的 id 数: {len(direct)}")
    print(f"缺失的 id（JS 引用了但 HTML 没有）: {len(missing)} {missing}")
    print(f"永远取不到的 id（HTML 有但没人引用）: {len(unreachable)} {unreachable}")
    print(f"JS 用到的 data-* 钩子: {len(hooks)}，缺失: {len(missing_hooks)} {missing_hooks}")

    ok = not missing and not unreachable and not missing_hooks and not dupes
    print("结论:", "全部通过" if ok else "存在问题")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
