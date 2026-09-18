# -*- coding: utf-8 -*-
"""工具注册表与执行框架 —— 实现已上游到 qsmy_deepseek-locator（0.2.0），这里只做转出。

注册、schema 生成、执行、异常回填、上下文注入（不暴露给模型的参数由运行上下文提供）
这一整套搬进了库；本文件保留原模块路径与全部名字，调用方一行都不用改。
"""

from __future__ import annotations

from qsmy_deepseek_locator.tools.registry import Tool, ToolRegistry, ToolResult

__all__ = ["Tool", "ToolRegistry", "ToolResult"]
