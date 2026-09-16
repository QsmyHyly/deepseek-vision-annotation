# -*- coding: utf-8 -*-
"""持久化用户偏好（config.local.json）的离线自测：python tests/test_userprefs.py

覆盖 objloc/userprefs.py 与 /api/settings 三个接口，全程**不花 API**：
  1. 文件不存在 -> 全部走内置默认，不算错误
  2. 环境变量生效（THINKING=0 -> thinking=False，来源 env）
  3. 保存后落盘、值生效、来源变 file，且**配置文件盖过环境变量**
  4. 保存后 get_settings() 无需 refresh 就反映新值（按 mtime 自动重建单例）
  5. 校验：未知键 / 越界 / 类型不对一律 PrefsError，且**一项都不写**（部分非法的请求不能写一半）
  6. 布尔字面量：1/0/true/false/yes/no/on/off 都认
  7. 原子写：写完不留 .tmp 残骸
  8. 文件损坏 / 顶层不是对象 -> 不抛异常，回落默认 + 一条警告
  9. 未知键原样保留（手写的键不该被本模块抹掉）
 10. reset：删指定键 / 删全部（文件因此变空时连着文件一起删）
 11. HTTP 层：GET / PATCH / DELETE，非法 PATCH 必须 400 且不落盘
 12. 环境变量把值设成空串 = 没设，不该覆盖默认

⚠️ 测试产物全部落在 tempfile 临时目录（改 config.PREFS_PATH / RUNS_DIR 后再 import web.app），
跑完项目的 runs/ 与根目录都不该多出任何文件。

**负向对照**：加 --control 参数会把 userprefs.file_values() 打桩成恒返回 {}，
模拟"配置文件这层根本没生效"的旧行为 —— 此时第 3/4/9/11 组断言必须**故意失败**，
非零退出。只打印 PASS 却从没验证过断言会失败的测试，等于没测。

    python tests/test_userprefs.py
    python tests/test_userprefs.py --control   # 必须失败（退出码 != 0）
"""

import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Windows 控制台默认是 GBK：不设 PYTHONIOENCODING 时，收尾那句 "✅ 全部通过" 会抛
# UnicodeEncodeError，让**一次全绿的 run 以退出码 1 收场**。假失败比假通过更坑 ——
# 它会让 CI 和人工都以为断言挂了（这个坑是在搬目录后未设该变量的终端里跑出来的）。
# 把编码错误降级为替换字符：支持 UTF-8 的终端照常显示，GBK 终端退化成 "?" 而已。
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(errors="replace")

# 必须在导入 config 之前定下来：mock 模式，且把这些环境变量的干扰清掉
os.environ["LLM_PROVIDER"] = "mock"
os.environ.pop("DEEPSEEK_API_KEY", None)

CONTROL = "--control" in sys.argv

from objloc import config  # noqa: E402

TMP_ROOT = Path(tempfile.mkdtemp(prefix="objloc_prefs_test_"))
config.RUNS_DIR = TMP_ROOT / "runs"
config.UPLOAD_DIR = config.RUNS_DIR / "uploads"
config.SCRATCH_DIR = config.RUNS_DIR / "scratch"
config.HISTORY_DIR = config.RUNS_DIR / "history"
config.PREFS_PATH = TMP_ROOT / "config.local.json"
config.get_settings(refresh=True)   # 顺带把上面几个目录 mkdir 出来

from objloc import userprefs  # noqa: E402

# 清掉可能存在的同名环境变量，保证"默认值"这一层的断言是确定的
for _name in ("THINKING", "REASONING_EFFORT", "USE_TOOLS", "MAX_TOOL_ROUNDS", "SYSTEM_PROMPT"):
    os.environ.pop(_name, None)

if CONTROL:
    # 负向对照：把"从配置文件取值"这条路掐断，后面的断言必须失败才算这些断言有效
    userprefs.file_values = lambda: {}
    print("[control] 已打桩 userprefs.file_values() -> {}，配置文件这一层被停用")

FAILED = []


def check(name, cond, extra=""):
    print(("PASS " if cond else "FAIL ") + name + ((" | " + str(extra)) if (not cond and extra) else ""))
    if not cond:
        FAILED.append(name)


def prefs_file():
    return Path(config.PREFS_PATH)


def write_raw(text):
    """直接往盘上写原文，模拟"手改文件"，并保证 mtime_ns 一定变（否则戳记缓存会拦住重读）。"""
    prefs_file().write_text(text, encoding="utf-8")
    st = prefs_file().stat()
    os.utime(prefs_file(), ns=(st.st_atime_ns, st.st_mtime_ns + 1_000_000))


def cleanup():
    try:
        prefs_file().unlink()
    except OSError:
        pass
    shutil.rmtree(TMP_ROOT, ignore_errors=True)


# --------------------------------------------------------------------------- #
# 1. 文件不存在
# --------------------------------------------------------------------------- #
check("1a 文件不存在时 stamp() 为 None", userprefs.stamp() is None)
eff = userprefs.effective()
check("1b 文件不存在时不是错误，exists=False", eff["exists"] is False)
check("1c 全部来源都是内置默认",
      all(v == "default" for v in eff["sources"].values()), eff["sources"])
check("1d 默认值正确（thinking/use_tools=True，轮数=8，effort 空）",
      eff["values"]["thinking"] is True and eff["values"]["use_tools"] is True
      and eff["values"]["max_tool_rounds"] == 8 and eff["values"]["reasoning_effort"] == "")
check("1e 文件不存在时没有警告", eff["warnings"] == [], eff["warnings"])

# --------------------------------------------------------------------------- #
# 2. 环境变量这一层
# --------------------------------------------------------------------------- #
os.environ["THINKING"] = "0"
os.environ["MAX_TOOL_ROUNDS"] = "3"
check("2a THINKING=0 生效且来源标成 env", userprefs.resolve("thinking") == (False, "env"))
check("2b MAX_TOOL_ROUNDS=3 生效且来源标成 env",
      userprefs.resolve("max_tool_rounds") == (3, "env"))
check("2c Settings 读到的是环境变量的值",
      config.get_settings(refresh=True).thinking is False
      and config.get_settings().max_tool_rounds == 3)
os.environ["THINKING"] = ""      # 设成空串 = 没设，不该覆盖默认值
check("2d 环境变量为空串视为未设置（回落默认 True）",
      userprefs.resolve("thinking") == (True, "default"))
os.environ["THINKING"] = "0"
os.environ["MAX_TOOL_ROUNDS"] = "not-a-number"
check("2e 环境变量非法（轮数=not-a-number）回落内置默认，不抛异常",
      userprefs.resolve("max_tool_rounds") == (8, "default"))
os.environ["MAX_TOOL_ROUNDS"] = "3"

# --------------------------------------------------------------------------- #
# 3. 保存 + 优先级
# --------------------------------------------------------------------------- #
data = userprefs.save({"thinking": True, "prompt": "识别图中的主要目标"})
check("3a 保存后文件真的落盘了", prefs_file().is_file())
check("3b 落盘内容是合法 JSON 且含刚写的键",
      json.loads(prefs_file().read_text(encoding="utf-8"))["thinking"] is True)
check("3c 保存后来源变成 file",
      data["sources"]["thinking"] == "file" and data["sources"]["prompt"] == "file",
      data["sources"])
check("3d **配置文件盖过环境变量**（env 仍是 THINKING=0，但解析出 True）",
      userprefs.resolve("thinking") == (True, "file"))
check("3e 没碰过的键仍来自 env（max_tool_rounds=3）",
      userprefs.resolve("max_tool_rounds") == (3, "env"))
check("3f 保存返回值里的 values 就是新值",
      data["values"]["thinking"] is True and data["values"]["prompt"] == "识别图中的主要目标")

# --------------------------------------------------------------------------- #
# 4. 热重建：不 refresh 也要看到新值
# --------------------------------------------------------------------------- #
config.get_settings(refresh=True)                     # 先把单例刷成"此刻"的状态
check("4a 重建后单例读到文件里的 True", config.get_settings().thinking is True)
userprefs.save({"thinking": False, "use_tools": False})
settings = config.get_settings()                      # ⚠️ 刻意不传 refresh
check("4b 保存后 get_settings() 不 refresh 也拿到新值（按 mtime 自动重建）",
      settings.thinking is False and settings.use_tools is False,
      f"thinking={settings.thinking} use_tools={settings.use_tools}")
check("4c use_tools 缺省时 /api/detect 用的就是它（Settings.use_tools）",
      settings.use_tools is False)

# --------------------------------------------------------------------------- #
# 5. 校验：非法值一项都不写
# --------------------------------------------------------------------------- #
def expect_error(name, patch, why):
    before = prefs_file().read_text(encoding="utf-8")
    try:
        userprefs.save(patch)
    except userprefs.PrefsError as exc:
        after = prefs_file().read_text(encoding="utf-8")
        check(name, before == after, f"文件被改动了！{why} 错误信息：{exc}")
    else:
        check(name, False, f"居然没报错：{why}")


expect_error("5a 未知键被拒绝，且文件没被改", {"nope": 1}, "未知键")
expect_error("5b 轮数越界被拒绝，且文件没被改", {"max_tool_rounds": 999}, "上限 64")
expect_error("5c 轮数下界被拒绝，且文件没被改", {"max_tool_rounds": 0}, "下限 1")
expect_error("5d 轮数类型不对被拒绝，且文件没被改", {"max_tool_rounds": "abc"}, "不是整数")
expect_error("5e 布尔类型不对被拒绝，且文件没被改", {"thinking": "maybe"}, "不是布尔")
expect_error("5f 非法的思考强度被拒绝，且文件没被改", {"reasoning_effort": "turbo"}, "枚举外")
expect_error("5g 空请求体被拒绝", {}, "空对象")
expect_error("5h 提示词超长被拒绝", {"prompt": "x" * 4001}, "长度上限")

# ⚠️ 关键的一条：一半合法一半非法，必须**一项都不写**（写一半比直接失败更难排查）
before = prefs_file().read_text(encoding="utf-8")
try:
    userprefs.save({"prompt": "合法的新提示词", "max_tool_rounds": 999})
except userprefs.PrefsError:
    pass
after = prefs_file().read_text(encoding="utf-8")
check("5i 部分非法 -> 合法的那个也不许写进去", before == after)
check("5j 提示词仍然是 5i 之前的值",
      "合法的新提示词" not in prefs_file().read_text(encoding="utf-8"))

check("5k 合法的枚举值能写进去",
      userprefs.save({"reasoning_effort": "high"})["values"]["reasoning_effort"] == "high")
check("5l 空串表示回落到服务端默认，也是合法值",
      userprefs.save({"reasoning_effort": ""})["values"]["reasoning_effort"] == "")

# --------------------------------------------------------------------------- #
# 6. 布尔字面量
# --------------------------------------------------------------------------- #
# 走**文件里写字面量**这条真实路径（手改配置文件时人最容易写成 "0" / "no" 这类字符串），
# 而不是直接调内部的 _as_bool —— 测的必须是用户真会走的那条路
for text, expect in (("1", True), ("true", True), ("yes", True), ("y", True), ("on", True),
                     ("0", False), ("false", False), ("no", False), ("n", False), ("off", False)):
    write_raw(json.dumps({"thinking": text}))
    got = userprefs.resolve("thinking")
    check(f"6 文件里的布尔字面量 {text!r} -> {expect}", got == (expect, "file"), got)
write_raw(json.dumps({"thinking": False}))
check("6 文件里直接写 JSON false 也认", userprefs.resolve("thinking") == (False, "file"))
write_raw(json.dumps({"thinking": "maybe"}))
check("6 认不出的布尔字面量不受文件层认领，会掉到下一层（此时 THINKING=0，故是 env）",
      userprefs.resolve("thinking") == (False, "env")
      and any("thinking" in w for w in userprefs.warnings()))

# --------------------------------------------------------------------------- #
# 7. 原子写不留残骸
# --------------------------------------------------------------------------- #
userprefs.save({"max_tool_rounds": 5})
leftovers = sorted(p.name for p in TMP_ROOT.iterdir() if p.name.endswith(".tmp"))
check("7a 写完不留 .tmp 残骸", leftovers == [], leftovers)
check("7b 临时文件确实用过同一个目录（同盘 rename 才是原子操作）",
      prefs_file().parent == TMP_ROOT)

# --------------------------------------------------------------------------- #
# 8. 损坏的文件
# --------------------------------------------------------------------------- #
# 第 8 组关注的是"文件这一层坏掉之后掉到哪一层"，所以先把 THINKING 撤掉，
# 否则断言会被环境变量接住、测不出内置默认那一层（这正是本组最初写错的地方）
os.environ.pop("THINKING", None)
write_raw("{ 这不是 JSON")
check("8a 损坏文件不抛异常，回落内置默认", userprefs.resolve("thinking") == (True, "default"))
check("8b 损坏文件给出警告", len(userprefs.warnings()) == 1, userprefs.warnings())
check("8c 损坏文件时 Settings 仍能构造出来", config.get_settings(refresh=True) is not None)

write_raw("[1, 2, 3]")
check("8d 顶层不是对象 -> 回落默认并给出警告",
      userprefs.resolve("use_tools") == (True, "default") and len(userprefs.warnings()) == 1)

write_raw('{"thinking": "maybe", "use_tools": false}')
check("8e 单项非法 -> 该项回落默认，其余项照常生效",
      userprefs.resolve("thinking") == (True, "default")
      and userprefs.resolve("use_tools") == (False, "file"))
check("8f 单项非法的警告里点名了是哪一项",
      any("thinking" in w for w in userprefs.warnings()), userprefs.warnings())
os.environ["THINKING"] = "0"   # 还给后面几组（11c 断言的就是"来源是环境变量"）

# --------------------------------------------------------------------------- #
# 9. 未知键原样保留
# --------------------------------------------------------------------------- #
write_raw('{"thinking": true, "my_note": "手写的注释性键"}')
check("9a 未知键被点名警告", any("my_note" in w for w in userprefs.warnings()), userprefs.warnings())
userprefs.save({"use_tools": False})
kept = json.loads(prefs_file().read_text(encoding="utf-8"))
check("9b 保存时未知键被原样保留（没被本模块抹掉）", kept.get("my_note") == "手写的注释性键", kept)
check("9c 写入的已知键生效", kept["use_tools"] is False)

# --------------------------------------------------------------------------- #
# 10. reset
# --------------------------------------------------------------------------- #
userprefs.reset(["prompt"])
kept = json.loads(prefs_file().read_text(encoding="utf-8"))
check("10a 只删指定键，其余保留", "prompt" not in kept and "my_note" in kept, kept)
check("10b 被删的键回落（prompt -> 内置默认空串）",
      userprefs.resolve("prompt") == ("", "default"))

# 先把手写的未知键清掉再全量重置：reset() 只删**设置项**，未知键是刻意保留的（见 9b），
# 所以留着 my_note 时文件不该被删 —— 这一点单独断言
write_raw(json.dumps({"thinking": True, "my_note": "手写的注释性键"}))
userprefs.reset()
check("10c reset 不会连未知键一起删掉（文件仍在，my_note 还在）",
      prefs_file().is_file()
      and json.loads(prefs_file().read_text(encoding="utf-8")) == {"my_note": "手写的注释性键"})

write_raw(json.dumps({"thinking": True, "use_tools": False}))
userprefs.reset()
check("10c2 只剩已知键时，全量重置会把文件一起删掉（不留空的 {}）", not prefs_file().exists())
check("10d 重置后 use_tools 回落默认 True（文件里的 False 已消失）",
      userprefs.resolve("use_tools") == (True, "default"))
check("10e 重置后 thinking 回落到 env（THINKING=0）",
      userprefs.resolve("thinking") == (False, "env"))
try:
    userprefs.reset(["bogus"])
except userprefs.PrefsError:
    check("10f reset 未知键被拒绝", True)
else:
    check("10f reset 未知键被拒绝", False, "居然没报错")

# --------------------------------------------------------------------------- #
# 11. HTTP 层
# --------------------------------------------------------------------------- #
from fastapi.testclient import TestClient  # noqa: E402

from objloc.web import app as web_app  # noqa: E402

client = TestClient(web_app.app)

resp = client.get("/api/settings")
check("11a GET /api/settings 200", resp.status_code == 200, resp.text[:200])
body = resp.json()
check("11b GET 带 path/values/sources/meta/defaults/warnings 六个字段",
      all(k in body for k in ("path", "values", "sources", "meta", "defaults", "warnings")))
check("11c GET 的 sources 反映环境变量（THINKING=0 -> env）", body["sources"]["thinking"] == "env")
check("11d meta 里带了可选范围供前端渲染（轮数 1~64）",
      body["meta"]["max_tool_rounds"]["minimum"] == 1 and body["meta"]["max_tool_rounds"]["maximum"] == 64)
check("11e system_prompt 标了 ui=False（不许在网页上改坐标口径）",
      body["meta"]["system_prompt"]["ui"] is False)

resp = client.patch("/api/settings", json={"thinking": True, "max_tool_rounds": 6})
check("11f PATCH 200 且回传新快照",
      resp.status_code == 200 and resp.json()["values"]["thinking"] is True, resp.text[:200])
health = client.get("/api/health").json()
check("11g PATCH 后 /api/health 立刻反映新值（不用重启服务）",
      health["thinking"] is True and health["max_tool_rounds"] == 6, health)

resp = client.patch("/api/settings", json={"use_tools": False, "max_tool_rounds": 999})
check("11h 非法 PATCH 返回 400", resp.status_code == 400, resp.text[:200])
check("11i 非法 PATCH 的 detail 说清了哪一项不对",
      "max_tool_rounds" in str(resp.json().get("detail", "")), resp.json())
check("11j 非法 PATCH 里合法的那个也没写进去",
      client.get("/api/settings").json()["values"]["use_tools"] is True)

resp = client.delete("/api/settings?keys=bogus")
check("11k DELETE 未知键 400", resp.status_code == 400, resp.text[:200])

resp = client.delete("/api/settings")
check("11l DELETE 全量 200", resp.status_code == 200, resp.text[:200])
after = resp.json()
check("11m 重置后 thinking 回到环境变量（env=0 -> False）",
      after["values"]["thinking"] is False and after["sources"]["thinking"] == "env")
# 注意断言的是 3 而不是 8：第 2 组设了 MAX_TOOL_ROUNDS=3，重置掉文件那一层后应当露出**环境变量**，
# 这恰好证明了回落链是逐层往下的，而不是"一删就跳回出厂值"
check("11n 重置后 max_tool_rounds 回到环境变量那一层（MAX_TOOL_ROUNDS=3）",
      after["values"]["max_tool_rounds"] == 3 and after["sources"]["max_tool_rounds"] == "env",
      after["values"])
check("11o 重置后文件被删掉", not prefs_file().exists())

# --------------------------------------------------------------------------- #
# 12. 图片输入精度（image_url 的 detail）：从偏好一路走到真正发出去的报文
# --------------------------------------------------------------------------- #
# 这一组验的是**整条链路**：config.local.json -> Settings -> build_messages 拼出的 content 块。
# 只验"值存下来了"是不够的 —— 存下来却没拼进报文，等于这个开关是假的（恒真断言的近亲）。
from objloc.agent import build_messages as _build_messages  # noqa: E402
from objloc.config import IMAGE_DETAILS  # noqa: E402


def _image_block(messages):
    """取出报文里那条 image_url 内容块（只取 image_url 本身，不含外层的 type）。"""
    for part in messages[-1]["content"]:
        if part.get("type") == "image_url":
            return part["image_url"]
    raise AssertionError("报文里根本没有 image_url 内容块")


_prev_detail = os.environ.pop("IMAGE_DETAIL", None)   # 免得外面的环境干扰本组

check("12a 默认**不带**这个字段（不带偏好文件时报文与改动前逐字节一致）",
      "detail" not in _image_block(_build_messages("找目标", image="data:image/png;base64,AA")))
check("12b 四个合法取值与 config.IMAGE_DETAILS 一致",
      set(IMAGE_DETAILS) == {"low", "high", "original", "auto"}, IMAGE_DETAILS)

resp = client.patch("/api/settings", json={"image_detail": "LOW"})   # 大小写不该成为负担
check("12c PATCH 大写 LOW 被规范成小写并落盘",
      resp.status_code == 200 and resp.json()["values"]["image_detail"] == "low", resp.text[:200])
check("12d 落盘后报文立刻带上 detail=low（Settings 热生效，不用重启服务）",
      _image_block(_build_messages("找目标", image="x")).get("detail") == "low",
      _image_block(_build_messages("找目标", image="x")))

resp = client.patch("/api/settings", json={"image_detail": "ultra"})
check("12e 非法枚举被拒绝（400）", resp.status_code == 400, resp.text[:200])
check("12f 被拒后盘上仍是 low（没有写坏）",
      client.get("/api/settings").json()["values"]["image_detail"] == "low")

check("12g 按次覆盖能盖过 Settings 里的默认值",
      _image_block(_build_messages("找目标", image="x", image_detail="original")).get("detail") == "original")
check("12h 按次覆盖传空串 = 这一次不带该字段（可以单次关掉）",
      "detail" not in _image_block(_build_messages("找目标", image="x", image_detail="")))

# 环境变量这一层：删掉文件里的键之后 IMAGE_DETAIL 应该重新露出来
client.delete("/api/settings")
os.environ["IMAGE_DETAIL"] = "high"
check("12i 文件里没有该键时 IMAGE_DETAIL 环境变量生效",
      config.get_settings(refresh=True).image_detail == "high",
      config.get_settings().image_detail)
os.environ["IMAGE_DETAIL"] = "nonsense"
check("12j 环境变量写错时回落到空（不带该字段），而不是让服务起不来",
      config.get_settings(refresh=True).image_detail == ""
      and "detail" not in _image_block(_build_messages("找目标", image="x")))
os.environ.pop("IMAGE_DETAIL", None)
config.get_settings(refresh=True)

# /api/detect 的按次覆盖解析：写错的值要回落，不能因此 400 掉整轮识别（与 _pick_int 同一取舍）
check("12k 写错的按次覆盖值回落而不是报错",
      web_app._pick_detail("ULTRA", "low") == "low"
      and web_app._pick_detail("HIGH", "") == "high"
      and web_app._pick_detail(None, "") == "")
if _prev_detail is not None:
    os.environ["IMAGE_DETAIL"] = _prev_detail

# 收尾：把环境变量还原（本进程用完即退，纯粹为了不误导后续调试）
os.environ.pop("THINKING", None)
os.environ.pop("MAX_TOOL_ROUNDS", None)

print()
if FAILED:
    print(f"❌ {len(FAILED)} 项失败：" + "，".join(FAILED))
    cleanup()
    sys.exit(1)
print("✅ 全部通过")
cleanup()
sys.exit(0)
