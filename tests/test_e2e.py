# -*- coding: utf-8 -*-
"""离线端到端自测：视觉标注 + Agent 工具执行 + Web 接口。

强制使用 Mock provider，避免在配置了真实 API Key 的机器上误调真实模型。
"""

import io
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# 必须在导入 config / web.app 之前设置
os.environ["LLM_PROVIDER"] = "mock"
os.environ.pop("DEEPSEEK_API_KEY", None)

FAILED = []


def check(name, cond, extra=""):
    print(("PASS " if cond else "FAIL ") + name + ((" | " + str(extra)) if (not cond and extra) else ""))
    if not cond:
        FAILED.append(name)


# ---------------------------------------------------------------- 1. 标注
from PIL import Image
from objloc.config import RUNS_DIR
from objloc.visualizer import annotate, load_image, render_annotations, summarize

img = Image.new("RGB", (600, 400), (250, 250, 250))
# 坐标约定是 0.0~1.0 相对比例（见 AGENTS.md#4.3）；
# 注意别写成 [100,100,400,300] 这类旧刻度/像素值——normalize_to_unit 会把它当旧刻度整体除以 1000
items = [
    {"bbox_2d": [0.1, 0.1, 0.4, 0.3], "label": "目标 A"},
    {"point_2d": [0.5, 0.2], "label": "点位"},
]
annotated, path = render_annotations(img, items, output_dir=RUNS_DIR, stem="e2e_annotated")
check("annotate saves png", path.exists() and path.stat().st_size > 0, path)
check("annotate keeps size", annotated.size == (600, 400))
check("summary counts", summarize(items) == {"total": 2, "bbox_count": 1, "point_count": 1, "labels": ["目标 A", "点位"]})

# 解析后标注（tuple -> items）
from objloc.parsing import parse_coordinates
from objloc.visualizer import coerce_items

coerced = coerce_items(parse_coordinates('[{"bbox_2d":[0.1,0.2,0.3,0.4],"label":"x"}]'))
check("coerce tuple form", coerced == [{"bbox_2d": [0.1, 0.2, 0.3, 0.4], "label": "x"}], coerced)

# ---------------------------------------------------------------- 2. Agent
from objloc.agent import build_messages, extract_items, run_agent

events = list(run_agent(build_messages("请解析坐标：" + '[{"bbox_2d": [180, 240, 430, 620], "label": "目标 A"}]')))
types = [e["type"] for e in events]
check("agent streams content", "content" in types, types)
check("agent emits tool_call", "tool_call" in types, types)
check("agent emits tool_result", "tool_result" in types, types)
check("agent emits done", "done" in types, types)
tool_result = next(e for e in events if e["type"] == "tool_result")
check("tool executed ok", tool_result["ok"] is True, tool_result)

final = next((e["content"] for e in events if e["type"] == "done"), "")
check("agent has final text", bool(final), final)

# ---------------------------------------------------------------- 2b. 思考模式开关
from objloc.config import REASONING_EFFORTS, thinking_payload
from objloc.providers import MockVisionClient

check("thinking_payload on", thinking_payload(True) == {"thinking": {"type": "enabled"}}, thinking_payload(True))
check("thinking_payload off", thinking_payload(False) == {"thinking": {"type": "disabled"}}, thinking_payload(False))

_msgs = build_messages("请解析坐标：" + '[{"bbox_2d": [0.18, 0.24, 0.43, 0.62], "label": "目标 A"}]')
_on = [e["type"] for e in run_agent(_msgs, thinking=True)]
_off = [e["type"] for e in run_agent(_msgs, thinking=False)]
check("thinking=True streams reasoning", "reasoning" in _on, _on)
check("thinking=False skips reasoning", "reasoning" not in _off, _off)
check("thinking=False still answers", "content" in _off and "done" in _off, _off)

# 思考强度：非法值不会透传；关闭思考时一律不带 effort
_oc = MockVisionClient()
check("effort passed when thinking on", _oc.resolve_thinking(True, "low") == (True, "low"))
check("illegal effort dropped", _oc.resolve_thinking(True, "bogus") == (True, None))
check("no effort when thinking off", _oc.resolve_thinking(False, "high") == (False, None))
check("effort set is closed", REASONING_EFFORTS == frozenset({"low", "medium", "high", "xhigh", "max"}), REASONING_EFFORTS)

# ---------------------------------------------------------------- 3. Web API
from fastapi.testclient import TestClient
from objloc.web.app import app

client = TestClient(app)

r = client.get("/api/health")
check("GET /api/health", r.status_code == 200 and r.json()["provider"] == "mock", r.text[:200])

r = client.get("/api/tools")
check("GET /api/tools", r.status_code == 200 and len(r.json()["tools"]) >= 5, r.text[:200])

r = client.get("/")
check("GET /", r.status_code == 200 and "对比" in r.text, r.status_code)

# 上传
buf = io.BytesIO()
Image.new("RGB", (320, 240), (240, 240, 240)).save(buf, format="PNG")
buf.seek(0)
r = client.post("/api/upload", files={"file": ("t.png", buf, "image/png")})
check("POST /api/upload", r.status_code == 200, r.text[:300])
record = r.json()
check("upload returns size", record.get("width") == 320 and record.get("height") == 240, record)

image_id = record["id"]

# 原图
r = client.get(f"/api/file/{image_id}")
check("GET /api/file", r.status_code == 200 and r.headers["content-type"].startswith("image/"), r.status_code)

# 手动标注
r = client.post("/api/annotate", json={"image_id": image_id, "items": items})
check("POST /api/annotate", r.status_code == 200, r.text[:300])
check("annotate returns url", r.json().get("annotated_url", "").startswith("/api/result/"), r.json())
annotated_url = r.json()["annotated_url"]

r = client.get(annotated_url.split("?")[0])
check("GET /api/result", r.status_code == 200 and len(r.content) > 0, r.status_code)

# 流式识别（mock）
with client.stream("POST", "/api/detect", json={"image_id": image_id, "prompt": "识别", "use_tools": True}) as resp:
    check("POST /api/detect status", resp.status_code == 200, resp.status_code)
    body = "".join(chunk for chunk in resp.iter_text())

check("detect stream has tool_call", '"tool_call"' in body, body[:400])
check("detect stream has annotated", '"annotated"' in body, body[:400])
check("detect stream ends", '"eof"' in body, body[:400])

# 示例
r = client.post("/api/sample")
check("POST /api/sample", r.status_code == 200 and "sample_items" in r.json(), r.text[:200])

# 2c. 工具层坐标归一化（模型能调用的工具必须只认 0.0~1.0）
from objloc.tools.builtin import build_default_registry
from objloc.tool_schema import build_tool
from objloc.tools import builtin as _builtin

_reg = build_default_registry()

# 旧刻度 0~1000 进 -> 0.0~1.0 出
_legacy = '[{"bbox_2d":[180,240,430,620],"label":"a"},{"point_2d":[500,300],"label":"b"}]'
_out = _reg.execute("decode_json_points", {"text": _legacy})
check("tool decode_json_points normalizes legacy",
      _out.raw[0]["bbox_2d"] == [0.18, 0.24, 0.43, 0.62] and _out.raw[1]["point_2d"] == [0.5, 0.3],
      _out.raw)
_bboxes, _bl, _pts, _pl = _reg.execute("parse_coordinates", {"text": _legacy}).raw
check("tool parse_coordinates normalizes legacy", _bboxes == [[0.18, 0.24, 0.43, 0.62]], _bboxes)
_bboxes2, _bl2, _pts2, _pl2 = _reg.execute("extract_coordinates",
                                          {"data": [{"point_2d": [250, 750]}]}).raw
check("tool extract_coordinates normalizes legacy", _pts2 == [[0.25, 0.75]], _pts2)
# 工具结果回传给模型的是 JSON 文本，collect_items 也要能从里面取出归一化坐标
from objloc.agent import collect_items as _collect
_evt = [{"type": "tool_result", "ok": True,
         "content": _reg.execute("parse_coordinates", {"text": _legacy}).content}]
_got = _collect(_evt, "")
check("collect_items reads tool result as unit coords",
      _got and _got[0]["bbox_2d"] == [0.18, 0.24, 0.43, 0.62], _got)

# 已经是 0.0~1.0 -> 原样不动
_unit = '[{"bbox_2d":[0.18,0.24,0.43,0.62],"label":"a"}]'
_out2 = _reg.execute("decode_json_points", {"text": _unit}).raw
check("tool keeps unit scale", _out2 == [{"bbox_2d": [0.18, 0.24, 0.43, 0.62], "label": "a"}], _out2)

# 像素坐标（>1000）不做换算，留给 range 检查报警
_px = '[{"bbox_2d":[180,240,4300,6200],"label":"a"}]'
_out3 = _reg.execute("decode_json_points", {"text": _px}).raw
check("tool leaves pixel scale alone", _out3[0]["bbox_2d"] == [180, 240, 4300, 6200], _out3)

# 工具描述里必须写明坐标约定（模型是从 schema 认识工具的）
for _name in ("decode_json_points", "parse_coordinates", "extract_coordinates", "annotate_image"):
    _desc = build_tool(getattr(_builtin, _name))["function"]["description"]
    check("schema states 0.0~1.0: " + _name, "0.0~1.0" in _desc, _desc)

# 解析核心本身必须保持"原样解析"，否则坐标空间探针失效
from objloc.parsing import decode_json_points as _raw_decode
check("parsing core stays raw (probe relies on it)",
      _raw_decode(_legacy)[0]["bbox_2d"] == [180, 240, 430, 620])

# 标注工具端到端：旧刻度也能画对位置（annotate 内部兜底）
from objloc.samples import ensure as _ensure_sample
_sample_png = _ensure_sample("marker_900")[1]
_src = _reg.execute("annotate_image", {"items": _legacy},
                    context={"source": str(_sample_png)})
check("tool annotate_image ok with legacy scale", _src.ok, _src.content)
# 追加用例：center_hit 与 expect_shape
from objloc.benchmark import center_hit, evaluate_sample
from objloc.samples import markers_to_gt

gt_tiny = [{"bbox_2d": [0.45, 0.45, 0.55, 0.55], "label": "红色", "kind": "circle",
            "color_name": "红色", "expect_shape": False, "match": "center"}]
# 中心命中、框略大（面积比 7.8 < 25）-> 命中
check("center_hit loose box", center_hit([0.36, 0.36, 0.64, 0.64], gt_tiny[0]["bbox_2d"]))
# 中心没盖住 -> 不命中
check("center_hit off center", not center_hit([0.0, 0.0, 0.2, 0.2], gt_tiny[0]["bbox_2d"]))
# 框住整图 -> 不算命中（防作弊）
check("center_hit full frame rejected", not center_hit([0.0, 0.0, 1.0, 1.0], gt_tiny[0]["bbox_2d"]))
# 只报颜色名，标签也算对（expect_shape=False）
rep = evaluate_sample([{"bbox_2d": [0.36, 0.36, 0.64, 0.64], "label": "红色"}], gt_tiny)
check("expect_shape=False label ok", rep["label_accuracy"] == 1.0, rep)
check("expect_shape=False hit", rep["detection_rate"] == 1.0, rep)
# 经典形状仍然要求形状词
rep2 = evaluate_sample([{"bbox_2d": [0.0, 0.0, 1.0, 1.0], "label": "红色"}],
                        [{"bbox_2d": [0.0, 0.0, 1.0, 1.0], "label": "红色矩形",
                          "kind": "rect", "color_name": "红色"}])
check("shape still enforced", rep2["label_accuracy"] == 0.0, rep2)
# 圆点真值不再带"圆形"后缀
mg = markers_to_gt([{"label": "红色", "cx": 50, "cy": 50, "radius": 10}], 100, 100)
check("marker gt label is color only", mg[0]["label"] == "红色", mg)
check("marker gt declares center match", mg[0]["match"] == "center", mg)

print()
if FAILED:
    print("FAILED:", FAILED)
    sys.exit(1)
print("ALL E2E TESTS PASSED")
