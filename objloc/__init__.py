"""objloc —— DeepSeek 物体定位演示软件的核心包。

模块划分：
    config       集中配置
    providers    模型客户端（流式事件协议 + OpenAI 兼容 + 离线 Mock）
    client       客户端门面（stream_chat / infer / 旧签名兼容）
    parsing      坐标 JSON 解析
    tool_schema  由函数签名生成工具 JSON Schema
    tools        工具执行框架（注册 / 执行 / 上下文注入）
    visualizer   图像加载与标注绘制
    agent        Agent 主循环（流式输出 + 工具执行）
    web          FastAPI 服务与对比页面
"""

__version__ = "2.0.0"

__all__ = ["__version__"]
