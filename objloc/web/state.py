# -*- coding: utf-8 -*-
"""web 层的进程内共享状态 —— 只有两个单例，但它们必须全进程唯一。

职责
----
- IMAGES：当前会话登记过的图片（**内存字典**，重启即空，见 AGENTS.md#4.9 的范围说明）；
- REGISTRY：内置工具注册表（/api/tools 展示、/api/detect 执行都用它）。

边界
----
只放"被多个路由模块共享的东西"。路由在 *_api.py / images.py，组装在 app.py。

⚠️ 关键约束是**对象身份**：app.py 会把这两个名字再导出一次
（from .state import IMAGES, REGISTRY），tests/test_storage.py 直接改 web_app.IMAGES 的内容，
必须改到同一份字典 —— 所以任何模块都只能 import 这两个对象，**不许重新赋值**。

旧文件对应关系（等价重构）
--------------------------
本模块 = 拆分前 objloc/web/app.py 顶部的 IMAGES / REGISTRY 两个模块级对象，原样搬出。
"""

from __future__ import annotations

from typing import Any

from objloc.tools import build_default_registry

# 内存中的图片登记表（进程级）。⚠️ 它**仍然**是内存字典：重启后当前图片就要重新上传。
# 打标记录本身已经持久化（runs/history/，见 objloc/storage），重启后可查、可看原图，
# 但「当前选中的图片」这个会话态没有落盘 —— 别把这两件事混为一谈。
IMAGES: dict[str, dict[str, Any]] = {}
REGISTRY = build_default_registry()
