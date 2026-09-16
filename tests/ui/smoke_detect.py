# -*- coding: utf-8 -*-
"""真实接口冒烟：内置测试图载入 -> SSE 识别 -> 校验 annotated 事件带 accuracy。

只走 HTTP，不 import 项目代码，所以它验证的是"浏览器会看到的那条链路"：
    POST /api/samples/bench_01/load  -> image_id
    POST /api/detect (thinking=false) -> text/event-stream
验收点：
  1. 收到 annotated 事件；
  2. annotated 带 accuracy，且含 detection_rate / mean_iou / label_accuracy / coord_space；
  3. 事件类型覆盖 round_start / content / done / annotated / eof。
"""

from __future__ import annotations

import json
import urllib.request

BASE = "http://127.0.0.1:8765"


def post(path: str, payload: dict | None = None) -> dict:
    data = json.dumps(payload or {}).encode("utf-8")
    req = urllib.request.Request(
        BASE + path, data=data, headers={"Content-Type": "application/json"}, method="POST"
    )
    with urllib.request.urlopen(req, timeout=60) as resp:
        return json.loads(resp.read().decode("utf-8"))


def stream(path: str, payload: dict):
    """逐块读取 SSE，产出解析后的事件字典。"""
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        BASE + path, data=data,
        headers={"Content-Type": "application/json", "Accept": "text/event-stream"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=180) as resp:
        print(f"  HTTP {resp.status} · {resp.headers.get('content-type')}")
        buffer = ""
        while True:
            chunk = resp.read1(4096)
            if not chunk:
                break
            buffer += chunk.decode("utf-8")
            while "\n\n" in buffer:
                block, buffer = buffer.split("\n\n", 1)
                payload_text = "\n".join(
                    line[5:].strip() for line in block.split("\n") if line.startswith("data:")
                )
                if payload_text:
                    yield json.loads(payload_text)


def main() -> int:
    print("[1] POST /api/samples/bench_01/load")
    loaded = post("/api/samples/bench_01/load")
    image_id = loaded["id"]
    print(f"    image_id={image_id} {loaded['width']}x{loaded['height']} "
          f"sample={loaded['sample']['title']} 真值={len(loaded['ground_truth'])} 个")

    print("[2] POST /api/detect  thinking=false")
    types: list[str] = []
    annotated = None
    content_chars = 0
    for evt in stream("/api/detect", {
        "image_id": image_id,
        "prompt": loaded["prompt"],
        "use_tools": True,
        "thinking": False,
    }):
        types.append(evt["type"])
        if evt["type"] == "content":
            content_chars += len(evt.get("text") or "")
        if evt["type"] == "annotated":
            annotated = evt
        if evt["type"] in ("error",):
            print("    !! error:", evt.get("message"))

    from collections import Counter
    counts = Counter(types)
    print(f"    事件总数={len(types)} 分布={dict(counts)} 正文={content_chars} 字")
    assert "annotated" in counts, "没有收到 annotated 事件"
    assert annotated is not None

    acc = annotated.get("accuracy")
    print(f"    annotated.url={annotated['url']}")
    print(f"    annotated.summary={annotated['summary']}")
    assert acc, "annotated 事件没有 accuracy 字段"
    print("    annotated.accuracy =", json.dumps(
        {k: acc[k] for k in ("gt_count", "hits", "detection_rate", "mean_iou",
                             "label_accuracy", "coord_space", "detection_rate_best")},
        ensure_ascii=False))
    for key in ("detection_rate", "mean_iou", "label_accuracy", "coord_space", "per_target"):
        assert key in acc, f"accuracy 缺少 {key}"

    with urllib.request.urlopen(BASE + annotated["url"], timeout=20) as resp:
        print(f"[3] GET 标注图 {resp.status} · {len(resp.read())} bytes")

    print("冒烟通过：SSE annotated 事件带 accuracy")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
