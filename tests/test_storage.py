# -*- coding: utf-8 -*-
"""历史记录 / 上传落盘 的离线自测：python tests/test_storage.py

覆盖 objloc/storage.py（打标记录的唯一读写入口）与 web 层的上传校验，全程**不花 API**：
  1. run_id 同秒内不重复，且字典序 == 时间序
  2. 写 -> 列表 -> 读单条 -> 删 的完整 round-trip
  3. 模拟重启（重新加载模块、重新扫盘，不走任何内存缓存）后仍能读回全部记录
  4. 保留策略：HISTORY_MAX=3 连写 5 条 -> 只剩最新的 3 条
  5. q 子串过滤 / limit+offset 分页 / total 正确
  6. 非法 run_id（含目录穿越）当作不存在，且**删不掉任何东西**
  7. 损坏的 meta.json 不会炸整张列表，其余记录照常返回
  8. render_thumbnail 产出的合法 PNG 宽度 == 请求宽度、高度按比例
  9. 上传超限 -> 413 且磁盘无残留；伪装成 .png 的文本 -> 400 且磁盘无残留
 10. 源图被删后 resolve_source_path 返回 None 而不是抛异常

⚠️ 测试产物**全部落在 tempfile 临时目录**：本文件在导入 objloc.storage / objloc.web.app
之前就把 objloc.config 上的目录常量改指到临时根目录（storage 与 app 都是**动态**读 config.*，
不在导入期绑定常量，所以改得动）。跑完项目的 runs/ 里一个字节都不该多出来。
"""

import importlib
import io
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# 必须在导入 config / storage / web.app 之前设置：离线 mock，绝不误调真实模型
os.environ["LLM_PROVIDER"] = "mock"
os.environ.pop("DEEPSEEK_API_KEY", None)

from PIL import Image

import objloc.config as config

# ---- 把全部运行产物指到临时目录（这一段决定了本测试不会污染 runs/）----
TMP_ROOT = Path(tempfile.mkdtemp(prefix="objloc_storage_test_"))
config.RUNS_DIR = TMP_ROOT / "runs"
config.UPLOAD_DIR = config.RUNS_DIR / "uploads"
config.SCRATCH_DIR = config.RUNS_DIR / "scratch"
config.HISTORY_DIR = config.RUNS_DIR / "history"
config.get_settings(refresh=True)   # 顺带把上面四个目录 mkdir 出来

from objloc import storage   # noqa: E402

FAILED = []


def check(name, cond, extra=""):
    print(("PASS " if cond else "FAIL ") + name + ((" | " + str(extra)) if (not cond and extra) else ""))
    if not cond:
        FAILED.append(name)


def new_source(name="pic.png", size=(120, 80), image_id="img000000001", filename="原始图.png"):
    """造一张真图落到 uploads/，返回 (路径, storage.make_source 的结果)。"""
    path = config.UPLOAD_DIR / name
    Image.new("RGB", size, (200, 210, 220)).save(path, format="PNG")
    return path, storage.make_source(image_id=image_id, path=path, filename=filename)


def upload_files():
    return sorted(p.name for p in config.UPLOAD_DIR.iterdir() if p.is_file())


ITEMS = [{"bbox_2d": [0.1, 0.2, 0.3, 0.4], "label": "目标 A"},
         {"point_2d": [0.6, 0.7], "label": "点位 B"}]

print("临时根目录:", TMP_ROOT)

# ---------------------------------------------------------------- 1. run_id
ids = [storage.new_run_id() for _ in range(300)]
check("1.1 run_id 同秒内不重复", len(set(ids)) == len(ids), str(len(ids) - len(set(ids))) + " 个重复")
check("1.2 run_id 字典序 == 时间序", ids == sorted(ids), str(ids[:3]) + " ... " + str(ids[-3:]))
check("1.3 run_id 形状合法", all(storage.is_valid_run_id(i) for i in ids), str(ids[:3]))

# ---------------------------------------------------------------- 2. round-trip
src_path, source = new_source()
annotated = Image.new("RGB", (120, 80), (255, 255, 255))
meta = storage.record_run(kind="detect", source=source, items=ITEMS, prompt="识别主要目标",
                          thinking=True, reasoning_effort=None, model="deepseek-flash",
                          duration_ms=4900, accuracy=None, warnings=["旧刻度换算"],
                          notice=None, annotated=annotated)
run_id = meta["run_id"]
run_path = config.HISTORY_DIR / run_id
check("2.1 目录与两个文件都写出来了",
      run_path.is_dir() and (run_path / "meta.json").is_file() and (run_path / "annotated.png").is_file(),
      str(run_path))
disk = json.loads((run_path / "meta.json").read_text(encoding="utf-8"))
check("2.2 schema 字段齐全",
      disk["schema"] == 1 and disk["kind"] == "detect" and disk["duration_ms"] == 4900
      and disk["summary"]["total"] == 2 and disk["warnings"] == ["旧刻度换算"]
      and disk["source"]["filename"] == "原始图.png" and len(disk["source"]["sha256"]) == 64,
      str(disk))
# source.path 一律「相对项目根 + 正斜杠」；本测试的临时目录在项目外，落回绝对路径也是对的，
# 所以这里断言的是「正斜杠 + 能解析回原文件」这两条真正的不变量
check("2.3 source.path 用正斜杠且能解析回原文件",
      chr(92) not in disk["source"]["path"]
      and storage.resolve_source_path(disk) == src_path.resolve(),
      str((disk["source"]["path"], str(src_path))))

records, total = storage.list_runs()
check("2.4 列表能读到刚写的这条", total == 1 and len(records) == 1, str((total, len(records))))
check("2.5 列表项不含 items", "items" not in records[0], str(list(records[0].keys())))
check("2.6 列表项带 n_items 与三个 URL",
      records[0]["n_items"] == 2
      and records[0]["annotated_url"] == "/api/history/" + run_id + "/annotated"
      and records[0]["source_url"] == "/api/history/" + run_id + "/source"
      and records[0]["thumb_url"] == "/api/history/" + run_id + "/annotated?w=160",
      str(records[0]))
check("2.7 列表项 source 只带四个子字段",
      set(records[0]["source"]) == {"image_id", "filename", "width", "height"}, str(records[0]["source"]))

one = storage.load_run(run_id)
check("2.8 单条记录带全文 items", one is not None and one["items"] == ITEMS, str(one))
check("2.9 单条记录带 url 字段", one["annotated_url"].endswith("/annotated"), str(one.get("annotated_url")))
check("2.10 delete 返回 True", storage.delete_run(run_id) is True)
check("2.11 删掉后读不回来", storage.load_run(run_id) is None)
check("2.12 重复删除返回 False", storage.delete_run(run_id) is False)

# ---------------------------------------------------------------- 3. 模拟重启
kept = []
for i in range(4):
    _p, _s = new_source("restart_" + str(i) + ".png", image_id="imgrestart" + str(i).zfill(3))
    kept.append(storage.record_run(kind="annotate", source=_s, items=ITEMS[:1],
                                   prompt="重启前第 " + str(i) + " 条", annotated=annotated)["run_id"])
before, before_total = storage.list_runs()
# 重新加载模块 = 丢掉全部模块级状态（相当于进程重启后重新扫盘）
storage = importlib.reload(storage)
after, after_total = storage.list_runs()
check("3.1 重载后 total 一致", before_total == after_total == 4, str((before_total, after_total)))
check("3.2 重载后每条都读得回来",
      sorted(r["run_id"] for r in after) == sorted(kept), str([r["run_id"] for r in after]))
check("3.3 重载后仍是 run_id 倒序", [r["run_id"] for r in after] == sorted(kept, reverse=True),
      str([r["run_id"] for r in after]))

# ---------------------------------------------------------------- 4. 保留策略
os.environ["HISTORY_MAX"] = "3"
config.get_settings(refresh=True)
check("4.0 配置读到 HISTORY_MAX=3", storage.history_max() == 3, str(storage.history_max()))
written = []
for i in range(5):
    _p, _s = new_source("retention_" + str(i) + ".png", image_id="imgretention" + str(i).zfill(3))
    written.append(storage.record_run(kind="annotate", source=_s, items=ITEMS[:1],
                                      prompt="保留策略第 " + str(i) + " 条",
                                      annotated=annotated)["run_id"])
left, left_total = storage.list_runs(limit=0)
check("4.1 连写 5 条只剩 3 条", left_total == 3, str(left_total))
check("4.2 留下的是最新的 3 条", sorted(r["run_id"] for r in left) == sorted(written[-3:]),
      "left=" + str(sorted(r["run_id"] for r in left)) + " written=" + str(written))
check("4.3 磁盘上确实只剩 3 个目录",
      len([d for d in config.HISTORY_DIR.iterdir() if d.is_dir()]) == 3,
      str([d.name for d in config.HISTORY_DIR.iterdir()]))
os.environ.pop("HISTORY_MAX", None)
config.get_settings(refresh=True)
check("4.4 清空返回条数", storage.clear_runs() == 3, "clear_runs != 3")
check("4.5 清空后列表为空", storage.list_runs(limit=0)[1] == 0)

# ---------------------------------------------------------------- 5. 过滤与分页
for i in range(5):
    # 第 3 条给一个独有的原始文件名，用来验证 q 也会过滤 filename（其余都叫「原始图.png」）
    _p, _s = new_source("page_" + str(i) + ".png", image_id="imgpage" + str(i).zfill(3),
                        filename="unique-name.png" if i == 3 else "原始图.png")
    prompt = "ALPHA 目标" if i % 2 == 0 else "beta 目标"
    storage.record_run(kind="annotate", source=_s, items=ITEMS, prompt=prompt, annotated=annotated)
page1, total1 = storage.list_runs(limit=2, offset=0)
page2, total2 = storage.list_runs(limit=2, offset=4)
check("5.1 total 是过滤后的总数而不是本页条数", total1 == 5 and len(page1) == 2, str((total1, len(page1))))
check("5.2 offset 到底只剩 1 条", len(page2) == 1 and total2 == 5, str((len(page2), total2)))
check("5.3 两页不重叠", not ({r["run_id"] for r in page1} & {r["run_id"] for r in page2}))
alpha, alpha_total = storage.list_runs(q="alpha")
check("5.4 q 过滤 prompt（大小写不敏感）",
      alpha_total == 3 and all("ALPHA" in r["prompt"] for r in alpha), str(alpha_total))
file_hit, file_total = storage.list_runs(q="UNIQUE-NAME")
check("5.5 q 过滤文件名（大小写不敏感）",
      file_total == 1 and file_hit[0]["source"]["filename"] == "unique-name.png", str(file_total))
none_hit, none_total = storage.list_runs(q="不存在的关键词")
check("5.6 匹配不到时 total=0", none_total == 0 and len(none_hit) == 0, str(none_total))

# ---------------------------------------------------------------- 6. 非法 run_id（防目录穿越）
victim, victim_total = storage.list_runs(limit=1)
victim_id = victim[0]["run_id"]
victim_meta = config.HISTORY_DIR / victim_id / "meta.json"
outside = TMP_ROOT / "outside"
outside.mkdir(exist_ok=True)
secret = outside / "secret.txt"
secret.write_text("别删我", encoding="utf-8")

BS = chr(92)
BAD_IDS = ["../../etc/passwd", "..%2f..", "..", ".", "", "abc",
           "20260916-163512-A1B2C3", "20260916-163512-a1b2c", "2026-09-16-163512-a1b2c3",
           "20260916-163512-a1b2c3/../../outside",
           "20260916-163512-a1b2c3" + BS + ".." + BS + ".." + BS + "outside",
           ".." + BS + ".." + BS + "outside" + BS + "secret.txt"]
bad_ok = True
for bad in BAD_IDS:
    ok_one = (storage.load_run(bad) is None
              and storage.delete_run(bad) is False
              and storage.annotated_path(bad) is None
              and not storage.is_valid_run_id(bad))
    if not ok_one:
        print("     非法 run_id 未被拦住:", repr(bad))
    bad_ok = bad_ok and ok_one
check("6.1 非法 run_id 一律当作不存在", bad_ok)
check("6.2 穿越也没删掉项目外的文件", secret.is_file() and secret.read_text(encoding="utf-8") == "别删我")
check("6.3 穿越也没删掉历史记录里的任何一条",
      victim_meta.is_file() and storage.list_runs(limit=0)[1] == victim_total, str(victim_total))
check("6.4 历史目录里没有多出多余条目",
      len(list(config.HISTORY_DIR.iterdir())) == victim_total,
      str([d.name for d in config.HISTORY_DIR.iterdir()]))

# ---------------------------------------------------------------- 7. 坏记录
good_before = storage.list_runs(limit=0)[1]
broken_ids = []
payloads = [json.dumps({"schema": 1, "run_id": "x", "created_at": "t"})[:12],  # 截断
            "{ 这不是 JSON",                                                    # 非法 JSON
            "[]",                                                               # 不是对象
            "{}"]                                                               # 缺字段
for payload in payloads:
    rid = storage.new_run_id()
    broken_ids.append(rid)
    (storage.run_dir(rid) / "meta.json").write_text(payload, encoding="utf-8")
after_broken, after_total = storage.list_runs(limit=0)
check("7.1 坏记录不会让 list_runs 崩", after_total == good_before, str((good_before, after_total)))
check("7.2 坏记录被计入 broken 计数", after_broken.broken == 4, str(after_broken.broken))
check("7.3 坏记录不会被当成有效记录返回",
      not ({r["run_id"] for r in after_broken} & set(broken_ids)), str(broken_ids))
check("7.4 好记录照常读得回来", storage.load_run(victim_id) is not None)
check("7.5 读坏记录返回 None", all(storage.load_run(b) is None for b in broken_ids))
for rid in broken_ids:
    shutil.rmtree(config.HISTORY_DIR / rid, ignore_errors=True)

# ---------------------------------------------------------------- 8. 缩略图
thumb_src = config.UPLOAD_DIR / "thumb.png"
Image.new("RGB", (400, 300), (30, 60, 90)).save(thumb_src, format="PNG")
data = storage.render_thumbnail(thumb_src, 160)
check("8.1 是合法 PNG 字节", data[:8] == bytes([137, 80, 78, 71, 13, 10, 26, 10]), str(data[:8]))
with Image.open(io.BytesIO(data)) as thumb:
    thumb_size = thumb.size
check("8.2 宽度 == 请求宽度", thumb_size[0] == 160, str(thumb_size))
check("8.3 高度按比例（300/400*160=120）", thumb_size[1] == 120, str(thumb_size))
cached = storage.render_thumbnail(thumb_src, 160)
check("8.4 重复调用命中缓存（内容一致）", cached == data)
Image.new("RGB", (400, 300), (200, 20, 20)).save(thumb_src, format="PNG")   # 覆盖源文件
fresh = storage.render_thumbnail(thumb_src, 160)
check("8.5 源文件变了缓存必须失效（缓存键含 mtime）", fresh != data)
check("8.6 不落盘：uploads/ 里没多出缩略图文件",
      not [n for n in upload_files() if "thumb" in n and n != "thumb.png"], str(upload_files()))

# ---------------------------------------------------------------- 10. 源图丢了
lost_path, lost_source = new_source("lost.png", image_id="imglost000001")
lost = storage.record_run(kind="annotate", source=lost_source, items=ITEMS[:1], annotated=annotated)
lost_path.unlink()
check("10.1 源图被删后 resolve_source_path 返回 None（不抛异常）",
      storage.resolve_source_path(storage.load_run(lost["run_id"])) is None)
check("10.2 meta 结构不对时也返回 None",
      storage.resolve_source_path(None) is None and storage.resolve_source_path({}) is None
      and storage.resolve_source_path({"source": None}) is None)
check("10.3 按 image_id 反查失败也返回 None", storage.find_source_by_image_id("imglost000001") is None)

# ---------------------------------------------------------------- 9. 上传（HTTP 层）
os.environ["MAX_UPLOAD_MB"] = "1"
config.get_settings(refresh=True)
storage.clear_runs()

from fastapi.testclient import TestClient   # noqa: E402
from objloc.web import app as web_app       # noqa: E402
from objloc.web.app import app              # noqa: E402

client = TestClient(app)
files_before = upload_files()

# 9.1 超限：413 + 无残留
r = client.post("/api/upload", files={"file": ("big.png", b"x" * (1024 * 1024 + 4096), "image/png")})
check("9.1 超限返回 413", r.status_code == 413, str(r.status_code))
check("9.2 超限后磁盘无残留（半截临时文件已删）", upload_files() == files_before, str(upload_files()))

# 9.3 伪装成 .png 的文本：400 + 无残留
r = client.post("/api/upload", files={"file": ("fake.png", b"this is not an image at all", "image/png")})
check("9.3 假 PNG 返回 400", r.status_code == 400, str((r.status_code, r.text[:120])))
check("9.4 假 PNG 落盘后已删干净", upload_files() == files_before, str(upload_files()))

# 9.5 后缀白名单仍然拦在第一道
r = client.post("/api/upload", files={"file": ("a.exe", b"MZ", "application/octet-stream")})
check("9.5 非白名单后缀 400", r.status_code == 400, str(r.status_code))

# 9.6 正常上传：统一转 PNG 落 uploads/<image_id>.png，原始文件名与 sha256 都记下来
buf = io.BytesIO()
Image.new("RGB", (320, 240), (240, 240, 240)).save(buf, format="JPEG")
r = client.post("/api/upload", files={"file": ("我的照片.jpg", buf.getvalue(), "image/jpeg")})
check("9.6 正常上传 200", r.status_code == 200, r.text[:200])
rec = r.json()
check("9.7 统一转 PNG 且文件名是 <image_id>.png", rec["path"].endswith(rec["id"] + ".png"), rec["path"])
check("9.8 原始文件名保留、尺寸与 sha256 都在",
      rec["filename"] == "我的照片.jpg" and rec["width"] == 320 and rec["height"] == 240
      and len(rec["sha256"]) == 64, str(rec))

# 9.9 标注落历史记录 + 三个 URL 都能取到
r = client.post("/api/annotate", json={"image_id": rec["id"], "items": ITEMS})
check("9.9 POST /api/annotate 200", r.status_code == 200, r.text[:200])
body = r.json()
hd = body.get("run_id")
check("9.10 响应带回 run_id 且 annotated_url 指向历史记录",
      bool(hd) and body["annotated_url"].startswith("/api/history/" + str(hd) + "/annotated"),
      str(body.get("annotated_url")))
run_id2 = body["run_id"]
listing = client.get("/api/history").json()
check("9.11 GET /api/history 列表里有这条",
      any(x["run_id"] == run_id2 for x in listing["records"]), str(listing.get("total")))
item = next(x for x in listing["records"] if x["run_id"] == run_id2)
check("9.12 三个 URL 都返回 200 且缩略图是 PNG",
      client.get(item["annotated_url"]).status_code == 200
      and client.get(item["source_url"]).status_code == 200
      and client.get(item["thumb_url"]).status_code == 200
      and client.get(item["thumb_url"]).headers["content-type"].startswith("image/png"),
      str(item["thumb_url"]))
check("9.13 缩略图宽度就是 160",
      Image.open(io.BytesIO(client.get(item["thumb_url"]).content)).size[0] == 160)
check("9.14 单条记录接口带全文 items", client.get("/api/history/" + run_id2).json()["items"] == ITEMS)
check("9.15 非法 run_id 走 HTTP 也是 404（且删不掉东西）",
      client.delete("/api/history/..%2f..").status_code == 404
      and client.get("/api/history/" + run_id2).status_code == 200)

# 9.16 gc：只清孤儿源图，被 history 引用的和内存表里的都留着
orphan = config.UPLOAD_DIR / "orphan.png"
Image.new("RGB", (10, 10), (0, 0, 0)).save(orphan, format="PNG")
gc = client.post("/api/history/gc").json()
check("9.16 gc 删掉了孤儿源图", gc["removed"] >= 1 and gc["freed_bytes"] > 0 and not orphan.exists(), str(gc))
check("9.17 gc 没删掉正在用的源图", Path(rec["path"]).is_file(), rec["path"])

# 9.18 /api/file/{image_id}：内存表里没有时从历史记录反查源图
saved = web_app.IMAGES.pop(rec["id"])
r_file = client.get("/api/file/" + rec["id"])
web_app.IMAGES[rec["id"]] = saved
check("9.18 内存表里没有时从历史记录反查源图", r_file.status_code == 200, str(r_file.status_code))
check("9.19 找不到的 image_id 仍然 404", client.get("/api/file/nosuchimage").status_code == 404)

# 9.20 清空历史记录
cleared = client.delete("/api/history").json()["removed"]
check("9.20 DELETE /api/history 清空",
      cleared >= 1 and client.get("/api/history").json()["total"] == 0, str(cleared))

# ---------------------------------------------------------------- 11. 删除被占用：不许静默地"部分成功"
# 来历（本机实测踩过）：一次性删二十来条记录时，偶发有几条 shutil.rmtree 抛 WinError 32
# （文件正被杀软扫描 / 被索引器或读句柄占着），而 clear_runs 原先是 `except OSError: pass` ——
# 接口照样 200、界面照样写"已清空 22 条"，盘上却剩 5 条。删不掉的必须**如实反映在计数里**。
import contextlib as _ctx   # noqa: E402
import io as _io            # noqa: E402
import shutil as _shutil    # noqa: E402


def _seed(n):
    """写 n 条记录，返回 run_id 列表（走 storage.record_run，与真实落盘路径完全一致）。"""
    ids = []
    for i in range(n):
        meta = storage.record_run(
            kind="annotate", source=new_source(image_id="imgdel%06d" % i)[1],
            prompt="删除自检 %d" % i, items=ITEMS, thinking=False,
            reasoning_effort=None, model="mock", duration_ms=None,
        )
        ids.append(meta["run_id"])
    return ids


_real_rmtree = _shutil.rmtree
storage.clear_runs()
seeded = _seed(4)
check("11.0 造出 4 条记录备用", storage.list_runs(limit=0)[1] == 4, str(seeded))

# (a) 瞬时占用：第一次 rmtree 抛 OSError，重试应当把它删掉 —— 不许因此少删一条
_calls = {"n": 0}


def _flaky_rmtree(path, *a, **kw):
    if _calls["n"] == 0:
        _calls["n"] += 1
        raise PermissionError(32, "The process cannot access the file（模拟瞬时占用）")
    return _real_rmtree(path, *a, **kw)


_err = _io.StringIO()
with _ctx.redirect_stderr(_err):
    _shutil.rmtree = _flaky_rmtree
    try:
        removed_a = storage.clear_runs()
    finally:
        _shutil.rmtree = _real_rmtree
left_a = storage.list_runs(limit=0)[1]
check("11.1 瞬时被占用会重试，4 条最终全删掉（不因此少删）",
      removed_a == 4 and left_a == 0, "removed=%s 重试触发=%s 剩=%s" % (removed_a, _calls["n"], left_a))
check("11.2 重试成功时不该往 stderr 喷警告", _err.getvalue() == "", _err.getvalue()[:120])

# (b) 一直删不掉：必须**少报**并在 stderr 留证据，绝不谎报"全删了"
_seeded2 = _seed(3)
_err2 = _io.StringIO()


def _always_fail(path, *a, **kw):
    raise PermissionError(32, "一直被占用")


with _ctx.redirect_stderr(_err2):
    _shutil.rmtree = _always_fail
    try:
        removed_b = storage.clear_runs()
        single_ok = storage.delete_run(_seeded2[0])
    finally:
        _shutil.rmtree = _real_rmtree
left_b = storage.list_runs(limit=0)[1]
check("11.3 一直删不掉时返回 0 而不是谎报 3",
      removed_b == 0 and left_b == 3, "removed=%s 剩=%s" % (removed_b, left_b))
check("11.4 删不掉时 stderr 有可查的证据（不是静默跳过）",
      "删除失败" in _err2.getvalue(), _err2.getvalue()[:160])
check("11.5 逐条删除同一口径：删不掉返回 False（HTTP 层据此回 404 而不是 200）",
      single_ok is False, str(single_ok))
# 负向对照：撤掉"占用"桩，同样这三条必须立刻删得掉 —— 证明上面失败的是占用，不是 clear_runs 本身。
check("11.6 撤掉占用桩后同样的三条立刻删得掉，计数回到 3",
      storage.clear_runs() == 3 and storage.list_runs(limit=0)[1] == 0)

# (c) 负向对照：把改动前的写法（不重试、静默 except OSError: pass）装回来，
# 同一个"瞬时占用"必须让它**真的少删一条** —— 否则说明 11.1 / 11.4 测的不是这回事。
def _legacy_clear():
    """复刻改动前的 clear_runs：不重试、不记证据、静默跳过删不掉的。"""
    removed = 0
    for entry in list(storage.history_dir().iterdir()):
        if not entry.is_dir() or not storage.is_valid_run_id(entry.name):
            continue
        try:
            _shutil.rmtree(entry)
            removed += 1
        except OSError:
            pass
    return removed


_seed(3)
_calls2 = {"n": 0}


def _fail_once_again(path, *a, **kw):
    if _calls2["n"] == 0:
        _calls2["n"] += 1
        raise PermissionError(32, "瞬时占用")
    return _real_rmtree(path, *a, **kw)


_err3 = _io.StringIO()
with _ctx.redirect_stderr(_err3):
    _shutil.rmtree = _fail_once_again
    try:
        legacy_removed = _legacy_clear()
    finally:
        _shutil.rmtree = _real_rmtree
legacy_left = storage.list_runs(limit=0)[1]
check("11.7 负向对照：老写法遇到同一个瞬时占用会真的少删一条、且不留任何证据",
      legacy_removed == 2 and legacy_left == 1 and _err3.getvalue() == "",
      "removed=%s 剩=%s stderr=%r" % (legacy_removed, legacy_left, _err3.getvalue()[:80]))
check("11.8 新写法在同样场景下多删的正是老写法漏掉的那条（11.1 复现）",
      storage.clear_runs() == 1 and storage.list_runs(limit=0)[1] == 0)

os.environ.pop("MAX_UPLOAD_MB", None)
config.get_settings(refresh=True)

print()
print("临时目录:", TMP_ROOT)
print("临时 runs/ 下的条目:", sorted(p.name for p in config.RUNS_DIR.iterdir()))

if FAILED:
    print("FAILED:", FAILED)
    sys.exit(1)
print("ALL STORAGE TESTS PASSED")
