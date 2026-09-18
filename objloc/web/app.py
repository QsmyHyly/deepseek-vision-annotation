"""Web 服务：流式识别 + 打标前后对比页面。

接口一览：
    GET  /                     对比页面（web/static/index.html）
    GET  /api/health           运行状态（provider / 是否有 Key / 工具列表）
    GET  /api/tools            已注册工具清单
    POST /api/shutdown         关闭本服务（页面右上角「关闭服务」按钮走这里；**仅限本机**）
    GET  /api/settings         当前生效的用户偏好（值 / 来源 / 可选范围），持久化在 config.local.json
    PATCH  /api/settings       按 key 保存若干项偏好（部分更新），保存后立即生效，无需重启
    DELETE /api/settings       删掉偏好键 -> 回落到 环境变量 > 内置默认
    POST /api/upload           上传图片或提交图片 URL -> 返回 image_id
    POST /api/detect           SSE 流式识别（流式输出 + 工具执行），结束前给出标注图
                               请求体可带 thinking: true/false 按次开关思考模式
    POST /api/annotate         本地标注：给定坐标 JSON -> 返回标注图（离线可用）
    POST /api/sample           载入内置示例图与示例坐标，便于零配置体验
    GET  /api/samples          内置测试图目录（按测试目的分组，见 objloc/samples/）
    GET  /api/samples/{id}/image  测试图原图（缺失时现场生成）
    POST /api/samples/{id}/load   把测试图设为当前图片，并回传真值（可自动算准确率）
    GET  /api/file/{image_id}  原图（内存表里没有时按 source.image_id 从历史记录反查）
    GET  /api/result/{name}    标注结果图（旧路由，向后兼容：查 runs/scratch/ 与 runs/ 根）
    GET  /api/history          历史记录列表（按 created_at 倒序，不含 items）
    GET  /api/history/{run_id} 单条完整记录（= meta.json + url 字段）
    DELETE /api/history/{run_id}  删一条记录
    DELETE /api/history        清空全部记录
    POST /api/history/gc       清理孤儿源图（只在被显式调用时执行）
    GET  /api/history/{run_id}/annotated[?w=160]  标注图 / 现场缩略图
    GET  /api/history/{run_id}/source             源图

存储布局、meta.json schema 与上述历史接口的字段口径见
@doc AGENTS.md#4.9-上传--结果存储--历史记录
（该文档解决「打标记录存哪、存什么、怎么读回来」的问题；磁盘读写全在 objloc/storage/，
本模块不自己拼 history/ 路径。）

坐标约定见 AGENTS.md#4.3：一律 0.0~1.0 的相对比例；若模型给出 0~1000 旧刻度，
这里会自动换算并向前端推送 warning（见 objloc/parsing.py: normalize_to_unit）。

## 包内职责划分（原本是一个 814 行的单文件）

| 模块 | 一句话职责 | 原文件对应段落 |
|---|---|---|
| `state.py` | 进程内共享状态（IMAGES / REGISTRY） | 顶部两个模块级对象 |
| `settings_api.py` | 用户偏好路由（/api/settings 三个接口） | 「用户偏好」 |
| `images.py` | 图片登记与图片资源路由（上传 / 取回 / 内置测试图 / 示例图） | 「内置测试图」「图片登记」「内置示例」 |
| `history_api.py` | 历史记录路由（列表 / 单条 / 删 / 清空 / GC / 图） | 「历史记录」 |
| `detect_api.py` | 识别与标注路由（SSE 流式 / 本地标注） | 「标注」「流式识别」 |

⚠️ 拆分按**资源**分组，不按行数平均切：每个模块对应一类 URL 前缀，
改动某个接口只需要动一个文件。路由函数体是从原文件**逐行搬移**的，
只有装饰器由 `@app.*` 机械改成 `@router.*`；SSE 事件字段与响应结构未变。
"""

from __future__ import annotations

import threading

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from objloc.config import WEB_STATIC_DIR, get_settings

from . import detect_api, history_api, images, settings_api
# IMAGES / REGISTRY 在这里再导出一次：对外（含 tests/test_storage.py）读 web_app.IMAGES
# 必须拿到 state 里那同一个字典，所以只 import、不重新赋值。
from .state import IMAGES, REGISTRY  # noqa: F401

# 路由处理函数与私有辅助一并转出：拆分前它们都是 objloc.web.app 的模块属性
# （原文件里 @app.get 装饰的就是这些同名函数；tests/test_userprefs.py 还依赖
#  web_app._pick_detail），保持可用，避免「内部重构」意外变成对外破坏。
# 不转 json / Path / FileResponse 这类 import 顺带进来的 name —— 它们从来不是本模块的接口。
from .detect_api import (  # noqa: F401
    _pick_detail,
    _pick_int,
    _sample_accuracy,
    _sse,
    annotate_api,
    detect,
)
from .history_api import (  # noqa: F401
    history_annotated,
    history_clear,
    history_delete,
    history_gc,
    history_get,
    history_list,
    history_source,
)
from .images import (  # noqa: F401
    ALLOWED_SUFFIXES,
    SAMPLE_ITEMS,
    _UPLOAD_CHUNK,
    _register_image,
    _source_meta,
    _stream_to_temp,
    get_file,
    get_result,
    list_samples,
    load_sample,
    sample,
    sample_image,
    upload,
    upload_url,
)
from .settings_api import read_settings, reset_settings, update_settings  # noqa: F401

# 再补上原本顺带可见的**项目内**名字（模块别名与工具函数），理由同上：
# 拆分前 web_app.storage / web_app.normalize_to_unit 这类写法能取到，就别让重构把它们弄没了。
from objloc import config, samples as samples_mod, storage, userprefs  # noqa: F401
from objloc.agent import build_messages, collect_items, run_agent  # noqa: F401
from objloc.config import IMAGE_DETAILS  # noqa: F401
from objloc.parsing import (  # noqa: F401
    LEGACY_SCALE_NOTICE,
    check_coordinate_range,
    format_coordinate_warnings,
    normalize_to_unit,
)
from objloc.tools import build_default_registry  # noqa: F401
from objloc.visualizer import (  # noqa: F401
    coerce_items,
    image_to_data_url_from_source,
    load_image,
    render_annotations,
    resolve_font,
    summarize,
)

app = FastAPI(title="DeepSeek 物体定位演示", version="2.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# --------------------------------------------------------------------------- #
# 页面与静态资源
# --------------------------------------------------------------------------- #
@app.get("/")
def index() -> FileResponse:
    return FileResponse(WEB_STATIC_DIR / "index.html")


@app.get("/api/health")
def health() -> dict:
    settings = get_settings()
    return {
        "ok": True,
        "provider": settings.resolved_provider(),
        "has_api_key": settings.has_api_key,
        "model": settings.model,
        "base_url": settings.base_url,
        "tools": REGISTRY.names(),
        # 下面五项是「识别参数」的当前生效默认值（来自 config.local.json / 环境变量 / 内置默认，
        # 谁定的见 GET /api/settings 的 sources）。按次覆盖走 /api/detect 的同名字段。
        "max_tool_rounds": settings.max_tool_rounds,
        "use_tools": settings.use_tools,
        "thinking": settings.thinking,
        "reasoning_effort": settings.reasoning_effort or "server-default",
        "image_detail": settings.image_detail or "server-default",
    }


@app.get("/api/tools")
def tools() -> dict:
    return {"tools": REGISTRY.describe()}


# --------------------------------------------------------------------------- #
# 进程控制：关闭服务
# --------------------------------------------------------------------------- #
# 为什么需要这个接口：服务通常由 start-web.ps1 用**隐藏窗口**拉起来，
# 没有控制台可以按 Ctrl+C —— 用户关掉浏览器页面后，python 进程还挂在那儿占着端口，
# 下次启动就会撞上"端口已被占用"。页面右上角那个按钮就是这件事的出口。
def _request_shutdown(server) -> None:
    """把 should_exit 置上，让 uvicorn 走优雅退出（收尾在跑的连接、关监听套接字）。"""
    print("[web] 收到关闭请求（POST /api/shutdown），正在退出…")
    server.should_exit = True


@app.post("/api/shutdown")
def shutdown(request: Request) -> dict:
    """关闭本服务。

    ⚠️ **只允许本机调用**：它会把整个进程停掉，而 WEB_HOST 是可以配成 0.0.0.0 的，
    那种部署下不该让局域网里任何人一个请求就把服务关掉。
    判断用 request.client.host（TCP 对端地址），不看任何可伪造的请求头。
    """
    client_host = request.client.host if request.client else ""
    if client_host not in ("127.0.0.1", "::1", "localhost"):
        raise HTTPException(status_code=403, detail=f"只允许从本机调用（当前来源 {client_host!r}）")

    server = getattr(app.state, "uvicorn_server", None)
    if server is None:
        # 用 uvicorn CLI、或测试用的 TestClient 起的进程里没有这个句柄。
        # 这时候要**如实回 503**，不能假装已关闭 —— 前端收到 200 就会显示"已关闭"，
        # 而进程其实还活着，人会在那儿一直等一个永远不会发生的结果。
        raise HTTPException(
            status_code=503,
            detail="当前进程不是由 objloc.web.app 启动的（缺少 uvicorn Server 句柄），无法远程关闭",
        )

    # 延迟一点点再置 should_exit：立刻置的话这次响应可能还没写回客户端，
    # 前端只会看到"连接被重置"，分不清是关成功了还是崩了。
    threading.Timer(0.3, _request_shutdown, args=(server,)).start()
    return {"ok": True, "message": "服务正在关闭"}


# 路由按资源分组挂在各自模块里（文件头写了各自的职责与边界），这里只负责组装。
# 挂载顺序与原单文件一致：偏好 -> 图片 -> 历史 -> 识别；路径互不重叠，顺序不影响匹配。
app.include_router(settings_api.router)
app.include_router(images.router)
app.include_router(history_api.router)
app.include_router(detect_api.router)

# 静态资源（放在最后，避免覆盖 API 路由）
app.mount("/static", StaticFiles(directory=str(WEB_STATIC_DIR)), name="static")



def main() -> None:
    """python -m web.app 或 python web/app.py 直接启动。"""
    import uvicorn

    settings = get_settings()
    # 自己构造 Server 而不是调 uvicorn.run(...)：uvicorn.run 内部才创建 Server 实例，
    # 外面拿不到句柄，而「关闭服务」按钮（POST /api/shutdown）需要它才能优雅退出。
    # 除了这一处，两者行为完全一样。
    config = uvicorn.Config(app, host=settings.host, port=settings.port, log_level="info")
    server = uvicorn.Server(config)
    app.state.uvicorn_server = server
    print(f"[web] provider={settings.resolved_provider()} model={settings.model}")
    print(f"[web] http://{settings.host}:{settings.port}")
    server.run()


if __name__ == "__main__":
    main()
