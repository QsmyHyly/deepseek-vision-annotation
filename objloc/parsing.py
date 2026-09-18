"""
坐标点解析模块。

模型输出的坐标 JSON 格式示例（bbox_2d 为边界框，point_2d 为点）：

```json
[
    {"bbox_2d": [x1, y1, x2, y2], "label": "食物名称"},
    {"point_2d": [x, y], "label": "食物名称"}
]
```

字段说明：
- bbox_2d: 边界框，[x1, y1, x2, y2]（左上角 x1, y1，右下角 x2, y2）。
- point_2d: 点坐标，[x, y]。
- label: 目标名称。
- 坐标均为 0.0~1.0 的小数（占宽/高的相对比例）。

decode_json_points 负责把模型输出文本解析为上述列表对象；extract_coordinates 再把该对象拆分为 bbox 与 point 两组坐标及标签。
"""

import ast
import json
from typing import Annotated

# to_items 的规则本体在库的 qsmy_deepseek_locator.parsing（0.2.0 上游化），这里转出同名符号 ——
# 本仓库的 objloc/visualizer.py 与 tests/test_parser.py 都按 objloc.parsing.to_items 引用它。
# 为什么特别说明：这份实现原先在本仓库和库里各有一份，**长得像但语义不同** ——
# 本仓库这份认「工具返回的成对列表」，库里那份（to_dict_items）只认 dict。
# 上游化 collect_items 时带走了严格的那份，宽松分支留在本仓库没跟过去，
# 于是工具模式下收集不到坐标（2026-09-18，两个仓库的 e2e 同时红）。现在只留库里的那一份。
from qsmy_deepseek_locator.parsing import to_items  # noqa: F401

# data 参数的 JSON schema（嵌套较深，抽为常量以保持函数签名简洁）
_COORDINATE_LIST_SCHEMA = {
    "type": "array",
    "description": "坐标列表对象，元素为含 bbox_2d 或 point_2d 的字典。",
    "items": {
        "type": "object",
        "properties": {
            "bbox_2d": {
                "type": "array",
                "items": {"type": "number"},
                "description": "边界框 [x1, y1, x2, y2]，0.0~1.0 的相对比例。",
            },
            "point_2d": {
                "type": "array",
                "items": {"type": "number"},
                "description": "点坐标 [x, y]，0.0~1.0 的相对比例。",
            },
            "label": {"type": "string", "description": "目标名称。"},
        },
    },
}


def _strip_code_fence(text: str) -> str:
    """去掉 ```json ... ``` 代码块标记，返回代码块内容。"""
    if "```" in text:
        parts = text.split("```")
        if len(parts) >= 3:
            block = parts[1]
            head = block.lstrip()
            if head[:4].lower() == "json":
                block = head[4:]
            return block.strip()
    return text.strip()


def _extract_json_block(text: str) -> str | None:
    """从夹杂说明文字的文本中，扫描出第一个完整的 JSON 数组/对象。

    使用括号配对扫描，能正确处理字符串内的括号与转义。
    """
    start = None
    opener = ""
    closer = ""
    for index, char in enumerate(text):
        if char in "[{":
            start = index
            opener = char
            closer = "]" if char == "[" else "}"
            break
    if start is None:
        return None

    depth = 0
    in_string = False
    escaped = False
    for index in range(start, len(text)):
        char = text[index]
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == opener:
            depth += 1
        elif char == closer:
            depth -= 1
            if depth == 0:
                return text[start:index + 1]
    return None


def decode_json_points(text: Annotated[str, "包含坐标 JSON 的文本，可带 ```json 代码块标记。"]):
    """从模型输出文本中解析出坐标列表对象。

    依次尝试：直接解析 -> 去掉代码块标记 -> 从说明文字中扫描 JSON 片段 -> Python 字面量。
    解析失败时返回空列表。返回元素形如：
    [
        {"bbox_2d": [x1, y1, x2, y2], "label": "食物名称"},
        {"point_2d": [x, y], "label": "食物名称"}
    ]
    """
    if not text or not isinstance(text, str):
        return []

    candidates: list[str] = []
    stripped = text.strip()
    candidates.append(stripped)

    unfenced = _strip_code_fence(text)
    if unfenced and unfenced not in candidates:
        candidates.append(unfenced)

    extracted = _extract_json_block(unfenced or stripped)
    if extracted and extracted not in candidates:
        candidates.append(extracted)

    for candidate in candidates:
        try:
            return json.loads(candidate)
        except Exception:  # noqa: BLE001 - 继续尝试下一个候选
            continue

    # 最后兜底：Python 字面量（容忍单引号 / True / None 等）
    try:
        return ast.literal_eval(extracted or unfenced or stripped)
    except Exception:  # noqa: BLE001
        return []


def extract_coordinates(data: Annotated[list, _COORDINATE_LIST_SCHEMA]):
    """将坐标列表对象拆分为 bbox 与 point 两组数据。

    Args:
        data: decode_json_points 返回的列表对象。

    Returns:
        bboxes: 边界框坐标列表，元素为 [x1, y1, x2, y2]
        bbox_labels: 与 bboxes 一一对应的标签
        points: 点坐标列表，元素为 [x, y]
        point_labels: 与 points 一一对应的标签
    """
    bboxes = []
    bbox_labels = []
    points = []
    point_labels = []

    if isinstance(data, dict):
        data = [data]
    if not isinstance(data, list):
        data = []

    for item in data:
        if not isinstance(item, dict):
            continue
        if "bbox_2d" in item:
            bboxes.append(item["bbox_2d"])
            label = item.get("label", f"bbox_{len(bboxes)}")
            bbox_labels.append(label)

        if "point_2d" in item:
            points.append(item["point_2d"])
            label = item.get("label", f"point_{len(points)}")
            point_labels.append(label)

    return bboxes, bbox_labels, points, point_labels


def parse_coordinates(text: Annotated[str, "包含坐标 JSON 的文本，可带 ```json 代码块标记。"]):
    """解析模型输出文本并直接拆分为 bbox / point 两组数据。"""
    return extract_coordinates(decode_json_points(text))


# to_items 的定义已上游进库，见文件头的 import（旧实现逐字搬到了
# qsmy_deepseek_locator.parsing.to_items，并补上了「工具只回坐标不回标签时不要整批丢掉」
# 的处理）。这里不再保留副本。


# --------------------------------------------------------------------------- #
# 坐标合法性检查与历史刻度兼容
# --------------------------------------------------------------------------- #
# 当前约定：0.0~1.0 的小数，表示占宽/高的比例。
COORD_MIN, COORD_MAX = 0.0, 1.0

# 历史刻度：0~1000 的整数归一化值（x = 像素x / 图宽 × 1000）。
# 实测 deepseek-flash 在物体定位时偶尔仍会沿用这个旧刻度（训练先验 / 历史提示词惯性），
# 它与 0.0~1.0 是同一坐标空间的 1000 倍线性缩放，可以无损换算回来，
# 因此这里做一次显式兜底，避免旧刻度被当成 0.0~1.0 直接画到图片左上角。
#
# 为什么会有这个刻度：它不是本项目的发明，而是视觉大模型圈子里的一套通用约定
# （Qwen2-VL / Qwen3-VL 等都用 0~1000，但 Qwen2.5-VL 中途换成了绝对像素，可见各家仍在摇摆）。
# 归一化本身则比检测早得多——1977 年 SIGGRAPH 的 Core 报告提出 NDC（Normalized Device
# Coordinates），1985 年 GKS 成为 ISO 7942 后把 NDC 写进国际标准，取值范围就是 [0,1]；
# 动机是"同一份图形描述，换设备换分辨率都不用改"。检测圈里把 bbox 归一化变成事实标准的是
# YOLO（2015，标签写成 cx cy w h 全部除以图宽高），其后 DETR（2020）直接回归归一化 cxcywh。
# 对齐到本项目：0~1 的价值不在于"数学上更优雅"，而在于"把设备/分辨率的自由度从数据里挤出去"，
# 这正是 4.3 节实测"服务端会缩放图片且不回传尺寸、像素不可复现、只有相对比例是不变量"的同一件事。
# 参考：https://en.wikipedia.org/wiki/Graphical_Kernel_System 、 https://docs.ultralytics.com/datasets/detect/
LEGACY_SCALE = 1000.0

LEGACY_SCALE_NOTICE = (
    "检测到模型输出的是 0~1000 旧刻度坐标，已自动除以 1000 换算为 0.0~1.0。"
    "若频繁出现，请检查系统提示词是否要求了 0.0~1.0 的小数。"
)

_COORD_FIELDS = ("bbox_2d", "point_2d")


def iter_coord_values(items):
    """遍历所有坐标对象里的数值（非数值原样产出，由调用方决定怎么处理）。"""
    for item in items or []:
        if not isinstance(item, dict):
            continue
        for field_name in _COORD_FIELDS:
            coords = item.get(field_name)
            if isinstance(coords, (list, tuple)):
                for value in coords:
                    yield value


def normalize_to_unit(items) -> tuple[list, bool]:
    """把可能的 0~1000 旧刻度坐标整体换算成 0.0~1.0，返回 (新列表, 是否换算过)。

    判定在**整批**坐标上统一进行，避免出现一半被换算、一半没换算的错位：

    - 最大值 <= 1.0                  -> 已是新约定，原样返回；
    - 最大值 <= 1000 且存在 > 1.0 的值 -> 判定为旧刻度，整体除以 1000；
    - 存在 > 1000 的值                -> 不做换算（很可能是像素坐标），
      交给 check_coordinate_range 报警，由调用方判断。

    注意：像素坐标在“小图”上也会落在 0~1000 内，与旧刻度无法从数值上区分，
    这种歧义无法自动消除，只能靠提示词约束 + 上层告警提示。
    """
    values = []
    for value in iter_coord_values(items):
        try:
            values.append(float(value))
        except (TypeError, ValueError):
            continue
    if not values:
        return list(items or []), False
    top = max(values)
    if top <= COORD_MAX or top > LEGACY_SCALE:
        return list(items or []), False

    converted: list = []
    for item in items or []:
        if not isinstance(item, dict):
            converted.append(item)
            continue
        new_item = dict(item)
        for field_name in _COORD_FIELDS:
            coords = item.get(field_name)
            if not isinstance(coords, (list, tuple)):
                continue
            new_coords = []
            for value in coords:
                try:
                    new_coords.append(round(float(value) / LEGACY_SCALE, 6))
                except (TypeError, ValueError):
                    new_coords.append(value)
            new_item[field_name] = new_coords
        converted.append(new_item)
    return converted, True


def check_coordinate_range(items, lo: float = COORD_MIN, hi: float = COORD_MAX) -> list[dict]:
    """检查坐标是否越界，返回警告列表。

    背景：DeepSeek 会在进入模型前缩放图片，且不回传缩放后的尺寸，调用方无法还原像素。
    因此模型若输出了像素坐标（而非 0.0~1.0 的相对比例），数值会越界。
    这里做一次显式检查，方便上层提示用户 / 记录日志，而不是默默画错。
    """
    warnings: list[dict] = []
    for index, item in enumerate(items):
        if not isinstance(item, dict):
            continue
        label = item.get("label", f"#{index}")
        for field_name in _COORD_FIELDS:
            coords = item.get(field_name)
            if not isinstance(coords, (list, tuple)):
                continue
            out_of_range = []
            for value in coords:
                try:
                    number = float(value)
                except (TypeError, ValueError):
                    out_of_range.append(value)
                    continue
                if number < lo or number > hi:
                    out_of_range.append(value)
            if out_of_range:
                warnings.append({
                    "index": index,
                    "label": label,
                    "field": field_name,
                    "coords": list(coords),
                    "out_of_range": out_of_range,
                })
    return warnings


def format_coordinate_warnings(warnings: list[dict]) -> str:
    """把坐标告警转成一句给用户/模型看的提示。"""
    if not warnings:
        return ""
    parts = [
        f"{w['label']} 的 {w['field']} {w['coords']}（越界值 {w['out_of_range']}）"
        for w in warnings[:3]
    ]
    more = "" if len(warnings) <= 3 else f"，另有 {len(warnings) - 3} 处"
    return ("检测到坐标超出 0.0~1.0 相对范围：" + "；".join(parts) + more +
            "。模型可能输出了像素坐标或 0~1000 旧刻度（图片会被 DeepSeek 缩放，像素值不可靠），"
            "建议检查提示词或改为 0.0~1.0 的相对比例。")
