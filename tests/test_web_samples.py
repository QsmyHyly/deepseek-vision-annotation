# -*- coding: utf-8 -*-
"""第 ⑤ 组「网页截图」测试图离线自测：真值与 PNG 是否同源、能否被自动打分。

为什么要单独一个测试文件：这一组是本项目唯一**真值不由 Python 生成**的图——
图片来自 Playwright 渲染，真值来自浏览器 DOM。两个来源分居两处，
一旦页面改了却忘了重抓，或者 CATALOG 里声明的尺寸变了，评测会**安静地**给出错误结论
（图片照样显示，指标照样算，只是算的不是回事）。所以这里逐条把它们钉死。

不需要 API、不需要服务在跑；需要本机有 node + Playwright 才能重抓，
两者都缺时本测试打印 SKIP 并以 0 退出（不假装通过）。
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PIL import Image  # noqa: E402

from objloc import benchmark, samples as samples_mod  # noqa: E402

FAILED = []


def check(name, cond, extra=""):
    print(("PASS " if cond else "FAIL ") + name + ((" | " + str(extra)) if (not cond and extra) else ""))
    if not cond:
        FAILED.append(name)


def skip(reason):
    print("SKIP " + reason)
    print()
    print("WEB SAMPLE TESTS SKIPPED")
    sys.exit(0)


if not samples_mod.WEB_CAPTURE_SCRIPT.exists():
    skip(f"缺少抓取脚本 {samples_mod.WEB_CAPTURE_SCRIPT}")

web_items = [it for it in samples_mod.CATALOG if it.group.startswith("⑤")]
if not web_items:
    check("网页截图组存在于目录", False)
else:
    check("网页截图组存在于目录", True)
    check("网页截图组规模", len(web_items) >= 3, len(web_items))
    check("网页截图 id 唯一", len({it.id for it in web_items}) == len(web_items))

for item in web_items:
    # ensure() 会在 PNG / manifest 缺失时现场重跑抓取，所以这里同时验证了"可自愈"
    try:
        _item, path = samples_mod.ensure(item.id)
    except Exception as exc:  # noqa: BLE001
        check(f"{item.id} 能生成图片", False, exc)
        continue

    check(f"{item.id} 图片已落盘", path.exists(), path)
    if not path.exists():
        continue

    real = Image.open(path).size
    check(f"{item.id} 声明的尺寸 == 实际 PNG 尺寸", (item.width, item.height) == real,
          f"声明 {item.width}x{item.height} / 实际 {real[0]}x{real[1]}")

    entry = samples_mod.web_entry(item.id)
    check(f"{item.id} manifest 尺寸 == 实际 PNG 尺寸",
          (entry.get("width"), entry.get("height")) == real,
          f"manifest {(entry.get('width'), entry.get('height'))} / 实际 {real}")

    gt = samples_mod.ground_truth(item.id)
    check(f"{item.id} 有真值", len(gt) > 0, len(gt))
    check(f"{item.id} 真值数量在 5~9", 5 <= len(gt) <= 9, len(gt))
    check(f"{item.id} 真值标签唯一",
          len({g["label"] for g in gt}) == len(gt),
          [g["label"] for g in gt])

    bad_box, bad_label, bad_mode = [], [], []
    for g in gt:
        x1, y1, x2, y2 = g["bbox_2d"]
        if not (0.0 <= x1 < x2 <= 1.0 and 0.0 <= y1 < y2 <= 1.0):
            bad_box.append(g["label"])
        if not str(g["label"]).strip():
            bad_label.append(g)
        if g.get("label_mode") != "text":
            bad_mode.append(g["label"])
    check(f"{item.id} 真值框都在 [0,1] 且 x1<x2,y1<y2", not bad_box, bad_box)
    check(f"{item.id} 真值标签非空", not bad_label, bad_label)
    # 网页元素的标签是界面文本名，没有颜色/形状；漏了这个标记标签准确率会恒为 0
    check(f"{item.id} 真值全部标了 label_mode=text", not bad_mode, bad_mode)

    # 端到端：拿真值当"完美预测"喂给打分器，应当满分。
    # 这是本组能否被自动评测的判定性证据——它一眼能看出 label_mode 有没有生效。
    perfect = [{"bbox_2d": list(g["bbox_2d"]), "label": g["label"]} for g in gt]
    report = samples_mod.accuracy(item.id, perfect, real[0], real[1])
    check(f"{item.id} 完美预测检出率 1.0", report and report["detection_rate"] == 1.0,
          report and report["detection_rate"])
    check(f"{item.id} 完美预测标签准确率 1.0", report and report["label_accuracy"] == 1.0,
          report and report["label_accuracy"])
    check(f"{item.id} 完美预测平均 IoU 1.0", report and report["mean_iou"] >= 0.999,
          report and report["mean_iou"])

    # 打分器认的坐标空间应是 normalized（真值本身就是归一化的）
    check(f"{item.id} 坐标空间判定为 normalized", report and report["coord_space"] == "normalized",
          report and report["coord_space"])

# ---- web_targets_to_gt 的边界处理 ----
degenerate = samples_mod.web_targets_to_gt([
    {"label": "正常", "bbox_2d": [0.1, 0.1, 0.2, 0.2]},
    {"label": "零宽", "bbox_2d": [0.3, 0.1, 0.3, 0.2]},
    {"label": "越界", "bbox_2d": [-0.5, 0.1, 1.8, 0.2]},
    {"label": "翻转", "bbox_2d": [0.5, 0.5, 0.2, 0.2]},
])
check("退化框被丢弃（零宽/翻转）", [g["label"] for g in degenerate] == ["正常", "越界"],
      [g["label"] for g in degenerate])
check("越界坐标被夹回 [0,1]", degenerate[1]["bbox_2d"] == [0.0, 0.1, 1.0, 0.2],
      degenerate[1]["bbox_2d"])

# ---- 别名（data-gt-alias）：不能丢，也不能凭空多出空列表 ----
alias_gt = samples_mod.web_targets_to_gt([
    {"label": "搜索商品", "bbox_2d": [0.1, 0.1, 0.2, 0.2], "aliases": ["搜索框", "搜索栏", " "]},
    {"label": "总销售额", "bbox_2d": [0.3, 0.3, 0.4, 0.4]},
    {"label": "订单量", "bbox_2d": [0.5, 0.5, 0.6, 0.6], "aliases": []},
])
check("别名被保留且去掉空白项", alias_gt[0].get("aliases") == ["搜索框", "搜索栏"],
      alias_gt[0].get("aliases"))
check("没有别名的真值不带 aliases 字段", "aliases" not in alias_gt[1], alias_gt[1])
check("空别名列表不写进真值", "aliases" not in alias_gt[2], alias_gt[2])
check("别名参与标签判定",
      benchmark.text_label_ok(alias_gt[0]["label"], "搜索框", aliases=alias_gt[0]["aliases"]))
check("别名不兜错误答案",
      not benchmark.text_label_ok(alias_gt[0]["label"], "购物车", aliases=alias_gt[0]["aliases"]))

# ---- 目录声明与 manifest 一一对应（防止加了图却忘了登记 / 反过来）----
manifest_ids = {s["id"] for s in samples_mod.capture_web_samples().get("samples", [])}
catalog_ids = {it.id for it in web_items}
check("manifest 与 CATALOG 的 id 集合一致", manifest_ids == catalog_ids,
      f"仅在 manifest: {manifest_ids - catalog_ids} / 仅在 CATALOG: {catalog_ids - manifest_ids}")

print()
if FAILED:
    print("FAILED:", FAILED)
    sys.exit(1)
print("ALL WEB SAMPLE TESTS PASSED")
