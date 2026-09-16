# -*- coding: utf-8 -*-
import os
import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

"""快速自测脚本（不依赖网络/API Key）：python _selftest.py"""
import json
import sys

from objloc.parsing import (
    COORD_MAX,
    LEGACY_SCALE,
    decode_json_points,
    normalize_to_unit,
    parse_coordinates,
    to_items,
)


def check(name, cond):
    print(("PASS " if cond else "FAIL ") + name)
    if not cond:
        sys.exit(1)


# 1. 夹杂说明文字 + 中文
t1 = '图中示例坐标如下：[{"bbox_2d": [180, 240, 430, 620], "label": "目标 A"}] 请解析'
check("prose-wrapped JSON", decode_json_points(t1) == [{"bbox_2d": [180, 240, 430, 620], "label": "目标 A"}])

# 2. 代码块
t2 = '```json\n[{"point_2d": [1, 2], "label": "x"}]\n```'
check("fenced JSON", decode_json_points(t2) == [{"point_2d": [1, 2], "label": "x"}])

# 3. 纯 JSON
check("plain JSON", decode_json_points('[{"bbox_2d": [0, 0, 10, 10]}]') == [{"bbox_2d": [0, 0, 10, 10]}])

# 4. 非法
check("invalid -> []", decode_json_points("hello world") == [])

# 5. parse_coordinates -> to_items 往返
res = parse_coordinates(t1)
items = to_items(res)
check("roundtrip", items == [{"bbox_2d": [180, 240, 430, 620], "label": "目标 A"}])

# 6. 字符串内包含括号/转义
t6 = '[{"bbox_2d": [1, 2, 3, 4], "label": "a[b]{c}\\"d"}]'
check("escaped brackets", decode_json_points("prefix " + t6 + " suffix")[0]["label"] == 'a[b]{c}"d')

# 7. 坐标刻度：新约定 0.0~1.0 原样通过
keep, converted = normalize_to_unit([{"bbox_2d": [0.18, 0.24, 0.43, 0.62]}])
check("unit coords untouched", converted is False and keep[0]["bbox_2d"] == [0.18, 0.24, 0.43, 0.62])

# 8. 坐标刻度：旧刻度 0~1000 整体换算成 0.0~1.0
conv, converted = normalize_to_unit([{"bbox_2d": [180, 240, 430, 620], "label": "a"},
                                     {"point_2d": [640, 200], "label": "b"}])
check("legacy scale converted", converted is True
      and conv[0]["bbox_2d"] == [round(180 / LEGACY_SCALE, 6), round(240 / LEGACY_SCALE, 6),
                                 round(430 / LEGACY_SCALE, 6), round(620 / LEGACY_SCALE, 6)]
      and conv[1]["point_2d"] == [0.64, 0.2])

# 9. 坐标刻度：像素坐标（>1000）不做换算，交给 range 检查报警
raw, converted = normalize_to_unit([{"bbox_2d": [180, 240, 4300, 6200]}])
check("pixel coords not converted", converted is False and raw[0]["bbox_2d"][3] == 6200)

check("coord max is 1.0", COORD_MAX == 1.0)

print("\nALL SELFTEST PASSED")
