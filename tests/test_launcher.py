# -*- coding: utf-8 -*-
"""启动脚本自测：双击 start-web.cmd 到底能不能跑起来。

**为什么必须有这个文件**：2026-09-18 上线启动脚本时踩了一个"验了但等于没验"的坑 ——
start-web.ps1 存成了 **UTF-8 无 BOM**，而双击 .cmd 走的是 **Windows PowerShell 5.1**，
5.1 读无 BOM 的 .ps1 会按系统 ANSI（简体中文 Windows 上是 GBK）解码，中文注释的字节被解坏，
把旁边的引号吃掉，整份脚本报了 "The string is missing the terminator" 直接解析失败 ——
窗口一闪而过，浏览器当然不弹。而当时所有"验证通过"都是拿 **PowerShell 7（pwsh）** 跑的，
PS7 默认按 UTF-8 读，永远是对的。**验错了执行环境，等于没验。**

所以这里不做语法检查的复述，而是直接拿**真正的 5.1** 去解析，并且配一条负向对照：
把 BOM 去掉之后，同一个检查**必须报错**。没有这条对照，这个测试在文件本来就有 BOM 时
自动成立、在没 BOM 时也可能因为别的原因蒙混过关，等于空气。

不需要 API、不需要服务；只需要本机有 Windows PowerShell（Windows 自带）。
非 Windows 或找不到 powershell.exe 时打印 SKIP 并以 0 退出。

跑法：python tests\test_launcher.py
"""

import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FAILED = []


def check(name, cond, extra=""):
    print(("PASS " if cond else "FAIL ") + name + ((" | " + str(extra)) if (not cond and extra) else ""))
    if not cond:
        FAILED.append(name)


# ---------------------------------------------------------------- 找 5.1
def find_powershell51():
    """定位 **Windows PowerShell 5.1**（不是 pwsh）。

    刻意不写死 System32 路径：环境变量优先，再退回默认位置。找不到就 None。
    """
    cand = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "WindowsPowerShell" / "v1.0" / "powershell.exe"
    return cand if cand.exists() else None


PS51 = find_powershell51()
if PS51 is None:
    print("SKIP 本机没有 Windows PowerShell 5.1（非 Windows 或未安装）")
    print()
    print("LAUNCHER TESTS SKIPPED")
    sys.exit(0)


# ---------------------------------------------------------------- 解析探针
PS51_BOM = b"\xef\xbb\xbf"


def ps51_parse(script: Path):
    """让 5.1 解析一个脚本，返回 (是否解析通过, 首条错误信息)。

    探针写成临时 .ps1 再 -File 调用，而不是 -Command 拼字符串 ——
    后者要在 cmd / PowerShell / Python 三层之间转义，踩过一次引号被吃掉的坑。
    探针自身也带 BOM，免得它自己先被解码坏掉。
    """
    with tempfile.TemporaryDirectory() as td:
        probe = Path(td) / "probe.ps1"
        probe.write_bytes(PS51_BOM + (
            "$e = $null\n"
            "[void][System.Management.Automation.Language.Parser]::ParseFile("
            "'" + str(script).replace("'", "''") + "', [ref]$null, [ref]$e)\n"
            "if ($e.Count -gt 0) { $e[0].Message; exit 3 }\n"
            "'OK'\n"
        ).encode("utf-8"))
        r = subprocess.run(
            [str(PS51), "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(probe)],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=120,
        )
        return r.returncode == 0, (r.stdout + r.stderr).strip()


def read_lnk(lnk: Path):
    """用 WScript.Shell 把 .lnk 读回来，返回 (目标, 工作目录)。

    ⚠️ 不能在 .lnk 的字节里找绝对路径字符串：同盘文件 Windows 往往只存**相对路径**
    （第一版就是这么写的，于是"目标对不对"那条断言在正确的 .lnk 上也会失败）。
    读 .lnk 真值的唯一可靠方式就是 COM。
    结果落成 UTF-8 文件再交给 Python 读，避开 5.1 控制台那段 OEM 编码。
    """
    with tempfile.TemporaryDirectory() as td:
        probe = Path(td) / "readlnk.ps1"
        out = Path(td) / "out.txt"
        probe.write_bytes(PS51_BOM + (
            "$sh = New-Object -ComObject WScript.Shell\n"
            "$l = $sh.CreateShortcut('" + str(lnk).replace("'", "''") + "')\n"
            "[System.IO.File]::WriteAllLines('" + str(out).replace("'", "''") + "',\n"
            "    @($l.TargetPath, $l.WorkingDirectory), (New-Object System.Text.UTF8Encoding($false)))\n"
        ).encode("utf-8"))
        subprocess.run([str(PS51), "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(probe)],
                       capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=120)
        if not out.exists():
            return None, None
        lines = out.read_text(encoding="utf-8").splitlines()
        return (lines[0] if lines else None), (lines[1] if len(lines) > 1 else None)


print("用 " + str(PS51) + " 检查（双击 .cmd 走的就是它）")
print()

# ---------------------------------------------------------------- 1. BOM
ps1_files = sorted(p for p in ROOT.rglob("*.ps1") if ".git" not in p.parts)
check("仓库里找得到 .ps1", len(ps1_files) > 0, f"{len(ps1_files)} 个")

for p in ps1_files:
    head = p.read_bytes()[:3]
    check(f"{p.relative_to(ROOT)} 带 UTF-8 BOM", head == PS51_BOM,
          "头三字节 " + head.hex(" ") + "（应为 ef bb bf）—— 没有它 5.1 会按 ANSI 解码，"
          "中文注释能把引号吃掉")

# ---------------------------------------------------------------- 2. 5.1 真解析
for p in ps1_files:
    ok, msg = ps51_parse(p)
    check(f"{p.relative_to(ROOT)} 能被 5.1 解析", ok, msg.splitlines()[0] if msg else "")

# ---------------------------------------------------------------- 3. 负向对照
# 把 BOM 去掉、其余一个字节都不动，5.1 必须解析失败。
# 不成立就说明上面那条断言测的不是 BOM 这件事（比如它本来就因为别的原因过不了，
# 或者这个版本的 PS 恰好按 UTF-8 读），那这条测试就是空气。
target = ROOT / "start-web.ps1"
if target.exists():
    with tempfile.TemporaryDirectory() as td:
        stripped = Path(td) / "start-web-nobom.ps1"
        raw = target.read_bytes()
        stripped.write_bytes(raw[3:] if raw[:3] == PS51_BOM else raw)
        ok_nobom, msg_nobom = ps51_parse(stripped)
        check("负向对照：去掉 BOM 后 5.1 必须解析失败", not ok_nobom,
              "去掉 BOM 竟然还能解析 —— 那上面那条 BOM 断言就没有意义了")
        if not ok_nobom:
            check("负向对照的报错确实是编码引起的（引号被吃）",
                  "terminator" in msg_nobom.lower() or "string" in msg_nobom.lower(),
                  msg_nobom.splitlines()[0] if msg_nobom else "")

        # 再对照组：同一份内容、只把 BOM 加回去，必须又能解析 —— 证明差异只来自 BOM。
        restored = Path(td) / "start-web-bom.ps1"
        restored.write_bytes(PS51_BOM + stripped.read_bytes())
        ok_restored, msg_restored = ps51_parse(restored)
        check("对照：同一份内容加回 BOM 即可解析", ok_restored,
              msg_restored.splitlines()[0] if msg_restored else "")

# ---------------------------------------------------------------- 4. cmd 必须纯 ASCII
# cmd.exe 即使 chcp 65001 也会搞坏含 UTF-8 中文的批处理文件，所以 .cmd 一律不许有非 ASCII 字节。
for cmd_file in sorted(p for p in ROOT.glob("*.cmd") if ".git" not in p.parts):
    raw = cmd_file.read_bytes()
    bad = [i for i, b in enumerate(raw) if b > 0x7F]
    check(f"{cmd_file.name} 是纯 ASCII", not bad,
          f"第 {bad[0]} 字节是 {raw[bad[0]]:#x}" if bad else "")

# 负向对照：把任意一个字节改成非 ASCII，上面那条必须能发现（证明它测的是字节，不是文件名）。
probe_cmd = Path(tempfile.mkdtemp()) / "probe.cmd"
probe_cmd.write_bytes(b"@echo off\nrem ok\n")
check("负向对照：纯 ASCII 的 cmd 通过", not [b for b in probe_cmd.read_bytes() if b > 0x7F])
probe_cmd.write_bytes("@echo off\nrem \u4e2d\n".encode("utf-8"))
check("负向对照：含中文的 cmd 被判为非 ASCII",
      bool([b for b in probe_cmd.read_bytes() if b > 0x7F]))
shutil.rmtree(probe_cmd.parent, ignore_errors=True)

# ---------------------------------------------------------------- 5. .cmd 转发参数
# 双击时不带参数；但控制台里要能 "start-web.cmd -NoBrowser"（自动化自检就靠它，
# 否则每跑一次验证就弹一次浏览器）。没有 %* 的话这个用法会静默变成"开浏览器"。
cmd_text = (ROOT / "start-web.cmd").read_text(encoding="ascii")
check("start-web.cmd 转发参数（%*）", "%*" in cmd_text,
      "没有 %* 时 start-web.cmd -NoBrowser 会被忽略、照常弹浏览器")

# ---------------------------------------------------------------- 6. 快捷方式生成器
# 别只检查脚本里"有没有那行字"——真跑一遍，看它到底造不造得出 .lnk、目标对不对。
inst = ROOT / "install-shortcut.ps1"
check("install-shortcut.ps1 存在", inst.exists())
if inst.exists():
    with tempfile.TemporaryDirectory() as td:
        r = subprocess.run(
            [str(PS51), "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(inst),
             "-OutDir", td, "-Name", "probe-launcher"],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=120,
        )
        lnk = Path(td) / "probe-launcher.lnk"
        check("install-shortcut.ps1 真的生成了 .lnk", lnk.exists(),
              (r.stdout + r.stderr).strip()[:200])
        if lnk.exists():
            got_target, got_wd = read_lnk(lnk)
            check(".lnk 的目标是 start-web.cmd",
                  got_target is not None and Path(got_target) == ROOT / "start-web.cmd",
                  "读到 " + str(got_target))
            # 工作目录必须是项目根：双击后 cmd 的 cwd 由它决定，日志与相对路径都从这里长出来。
            check(".lnk 的工作目录是项目根",
                  got_wd is not None and Path(got_wd) == ROOT, "读到 " + str(got_wd))
            # 负向对照：目标不能是别的文件 —— 证明上面那条不是恒真（比如改成"路径非空"就恒真了）。
            check("负向对照：.lnk 目标不是无关文件",
                  got_target is not None and Path(got_target) != ROOT / "AGENTS.md")

# ---------------------------------------------------------------- 7. -NoBrowser 开关
launcher = (ROOT / "start-web.ps1").read_text(encoding="utf-8-sig")
check("start-web.ps1 声明了 -NoBrowser", "[switch] $NoBrowser" in launcher)
check("开浏览器只在 Open-Browser 里做（没有游离的 Start-Process $url）",
      launcher.count("Start-Process $url") == 1,
      f"出现 {launcher.count('Start-Process $url')} 次")
check("Open-Browser 受 -NoBrowser 短路", "if ($NoBrowser)" in launcher)
check("两份入口都调用 Open-Browser", launcher.count("Open-Browser") == 3,
      "应为 1 个函数定义 + 2 个调用点，实际 " + str(launcher.count("Open-Browser")))

# ---------------------------------------------------------------- 8. requirements.txt 必须带 BOM
# 与上面 .ps1 是**同一类坑**，只是读它的程序从 PowerShell 5.1 换成了 pip：
# pip 的 auto_decode 先找 BOM，找不到就退回 locale.getpreferredencoding(False) ——
# 简体中文 Windows 上是 GBK。于是 UTF-8 **无 BOM** 的 requirements.txt 只要含中文注释，
# `pip install -r requirements.txt` 就抛 UnicodeDecodeError(gbk) 直接崩，一行依赖都装不上。
# 这个坑 2026-09-18 才被发现：README 里那条手工安装路径**一直是坏的**，却没人知道 ——
# 因为本机开发一律走 start-web.cmd（它把库源码挂上 PYTHONPATH），没人真去跑一次 pip install。
# 又一次「兜底把坑盖住了」，和文件开头那条 .ps1 的教训同源。
req = ROOT / "requirements.txt"
check("requirements.txt 存在", req.exists())
if req.exists():
    raw = req.read_bytes()
    check("requirements.txt 带 UTF-8 BOM（否则 pip 在中文 Windows 上按 GBK 读会崩）",
          raw[:3] == PS51_BOM,
          "前 3 字节是 " + " ".join(f"{b:02x}" for b in raw[:3]) + "，应为 ef bb bf")

    # 负向对照：证明这条断言测的是 BOM 本身，而不是「这个文件恰好能读」。
    tmpdir = Path(tempfile.mkdtemp())
    try:
        probe = tmpdir / "req_nobom.txt"
        probe.write_bytes(raw[3:] if raw[:3] == PS51_BOM else raw)  # 去掉 BOM，其余一字不动
        check("负向对照：去掉 BOM 后不再被认作带 BOM", probe.read_bytes()[:3] != PS51_BOM)
        probe.write_bytes(PS51_BOM + probe.read_bytes())           # 再原样加回去
        check("对照：同一份内容加回 BOM 后又成立", probe.read_bytes()[:3] == PS51_BOM)
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)

    # 顺带锁住依赖下限：写低了会装到缺 to_items 的 0.2.0，objloc.parsing 直接 ImportError
    # （整个包 import 不了）。详见 AGENTS.md §0 与库仓 README §13。
    check("qsmy-deepseek-locator 下限 >=0.2.1",
          "qsmy-deepseek-locator>=0.2.1" in req.read_text(encoding="utf-8-sig"),
          "写更低的下限会装到 PyPI 上缺 to_items 的 0.2.0")

print()
if FAILED:
    print("FAILED:", FAILED)
    sys.exit(1)
print("ALL LAUNCHER TESTS PASSED")
