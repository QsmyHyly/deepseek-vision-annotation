# -*- coding: utf-8 -*-
"""工具 schema 生成 —— 实现已上游到 qsmy-deepseek-locator（0.2.0），这里只做转出。

schema 从**函数签名 + Annotated 注解**推导，名称/描述/参数结构都只在函数定义处维护一份。
这条规则三边共用（本项目 / 库 / 安卓 App），所以实现搬进了库。

⚠️ 记牢那条老约定：**只有 docstring 的首行会进模型**，面向模型的约束必须写在首行里。
"""

from __future__ import annotations

from qsmy_deepseek_locator.tool_schema import TOOLS, build_tool

__all__ = ["TOOLS", "build_tool"]
