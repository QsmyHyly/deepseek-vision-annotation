# DeepSeek 物体定位 · 打标对比演示台 —— 网页服务启动器
#
# 写法参考 ~/.dsh/desktop-launcher/start-dsh-web.cmd + launcher.ps1：
# 先探一下服务在不在 -> 在就直接开浏览器（不重复起进程）；不在就拉起来、等到就绪再开浏览器。
#
# 四件事是本项目特有的，都是踩过才写进来的：
#
# 0. **依赖库不一定是 pip 装过的**。本仓库依赖 qsmy-deepseek-locator，而按本机约定它常常是
#    「源码就在旁边的目录、没 pip install」（AGENTS.md 里的测试命令都带 PYTHONPATH）。
#    少这一步，脚本会在 60 秒后抛一句看不懂的 ModuleNotFoundError ——
#    本脚本的第一版就是这样挂的，所以下面先探一次 import，探不通就把旁边的源码挂上。
# 1. **必须先刷 API Key**。已打开的终端读不到新设置的用户级环境变量，这是老坑
#    （AGENTS.md §2 与 §6.1）。不刷就会静默降级成离线 Mock —— 页面照样能点，
#    但模型是假的，最容易让人以为「识别效果怎么这么差」。
# 2. **服务用隐藏窗口拉起**，日志重定向到 runs/web.out.log / web.err.log。
#    因此**没有控制台可以按 Ctrl+C**：关服务请用网页右上角的「关闭服务」按钮
#    （走 POST /api/shutdown，后端只接受本机调用），或者下面的 Stop-Process。
# 3. 端口被别的进程占着时**先如实报出来**，而不是干等 60 秒再报一句「启动失败」。
#    最常见的情形就是「上次那个 python 没关干净，还占着 8765」，报出来一眼就懂。
#
# 双击 start-web.cmd 即可；也可以直接右键本文件 -> 使用 PowerShell 运行。
#
# 参数：
#   -NoBrowser   只把服务拉起来，**不打开浏览器**。给自动化 / 自检用 ——
#                否则每跑一次自检就弹一个浏览器窗口，正在用电脑的人会被反复打断。
#                服务照常起来、照常等到就绪，只是最后那一下 Start-Process 跳过。

param(
    [switch] $NoBrowser
)

$ErrorActionPreference = 'SilentlyContinue'

$root    = $PSScriptRoot
$port    = if ($env:WEB_PORT) { $env:WEB_PORT } else { '8765' }
$url     = "http://127.0.0.1:$port"
$runsDir = Join-Path $root 'runs'
$outLog  = Join-Path $runsDir 'web.out.log'
$errLog  = Join-Path $runsDir 'web.err.log'

function Test-WebUrl {
    try {
        $resp = Invoke-WebRequest -UseBasicParsing -Uri $url -TimeoutSec 2
        return ($resp.StatusCode -ge 200) -and ($resp.StatusCode -lt 500)
    } catch {
        return $false
    }
}

function Open-Browser {
    if ($NoBrowser) {
        Write-Host "  [--] -NoBrowser：不打开浏览器（服务已就绪，自己去 $url 看）" -ForegroundColor DarkGray
        return
    }
    Start-Process $url
}

function Fail-AndHold([string[]] $lines) {
    foreach ($l in $lines) { Write-Host $l }
    Read-Host "  按回车退出"
    exit 1
}

Write-Host ""
Write-Host "  DeepSeek 物体定位 · 打标对比演示台" -ForegroundColor Cyan
Write-Host "  ------------------------------------" -ForegroundColor DarkGray

# ---- 已经在跑就不重复起：直接开浏览器，并把「怎么关」一并说清 ----
if (Test-WebUrl) {
    Write-Host "  [ok] 服务已经在跑：$url" -ForegroundColor Green
    Write-Host "       关闭服务：网页右上角的「关闭服务」按钮" -ForegroundColor DarkGray
    Open-Browser
    exit 0
}

# ---- 1) 找 python ----
$python = (Get-Command python -ErrorAction SilentlyContinue).Source
if ([string]::IsNullOrWhiteSpace($python)) {
    Fail-AndHold @(
        "  [x] 找不到 python 命令。请先确保 python 在 PATH 里。"
    )
}

# ---- 2) 依赖库在哪 ----
# 先探一次 import：装过就直接用，没装就找旁边的源码 checkout。
& $python -c "import qsmy_deepseek_locator" 2>$null | Out-Null
if ($LASTEXITCODE -ne 0) {
    $libSrc = Join-Path (Split-Path $root -Parent) 'qsmy-deepseek-locator\src'
    if (Test-Path (Join-Path $libSrc 'qsmy_deepseek_locator')) {
        $env:PYTHONPATH = if ($env:PYTHONPATH) { "$libSrc;$env:PYTHONPATH" } else { $libSrc }
        Write-Host "  [ok] 本机没装库，已把旁边的源码挂上 PYTHONPATH：" -ForegroundColor DarkGray
        Write-Host "       $libSrc" -ForegroundColor DarkGray
    } else {
        Fail-AndHold @(
            "  [x] 依赖库 qsmy_deepseek_locator 不可用：import 不成功，旁边也没找到源码。"
            "      找过：$libSrc"
            "      两种修法（任选其一）："
            "        pip install -r requirements.txt   # 从 PyPI 装（需 >= 0.2.0）"
            "        或把源码仓库放到上一级目录，路径就是上面那个"
            ""
            "      注：本机开发时走的通常是后者 —— 直接挂旁边的源码，"
            "          改完库立刻见效，不用重新安装。"
        )
    }
}

# ---- 3) 刷新 API Key ----
$key = [Environment]::GetEnvironmentVariable('DEEPSEEK_API_KEY', 'User')
if ([string]::IsNullOrWhiteSpace($key)) {
    Write-Host "  [!] 没读到 DEEPSEEK_API_KEY —— 将以**离线 Mock** 启动。" -ForegroundColor Yellow
    Write-Host "      页面仍可完整演示（流式 / 工具 / 打标对比 / 真值打分），但不调真实模型。" -ForegroundColor DarkGray
} else {
    $env:DEEPSEEK_API_KEY = $key
    Write-Host "  [ok] 已从用户级环境变量刷新 API Key（长度 $($key.Length)）"
}

# ---- 4) 端口占着就直说 ----
$busy = Get-NetTCPConnection -LocalPort ([int]$port) -State Listen -ErrorAction SilentlyContinue
if ($busy) {
    $holder = @($busy)[0].OwningProcess
    Fail-AndHold @(
        "  [!] 端口 $port 已被进程 $holder 占用，但它不响应 HTTP。"
        "      多半是上一轮的 python 没关干净。先停掉再重试："
        "      Stop-Process -Id $holder -Force"
        "      （正常关服务请用网页右上角的「关闭服务」按钮）"
    )
}

# ---- 5) 隐藏窗口拉起服务 ----
New-Item -ItemType Directory -Force -Path $runsDir | Out-Null
# 先清掉旧日志：失败时下面要打印「日志的最后 20 行」，留着一万年前的日志会把人带沟里。
Remove-Item -Force $outLog, $errLog -ErrorAction SilentlyContinue
$proc = Start-Process -FilePath $python -ArgumentList @("-m", "objloc.web.app") -WorkingDirectory $root -WindowStyle Hidden -RedirectStandardOutput $outLog -RedirectStandardError $errLog -PassThru
Write-Host "  [..] 正在启动（python pid=$($proc.Id)，最多等 60 秒）…"

# ---- 6) 等就绪 ----
$deadline = (Get-Date).AddSeconds(60)
while ((Get-Date) -lt $deadline) {
    Start-Sleep -Milliseconds 400
    if (Test-WebUrl) {
        Write-Host "  [ok] 启动完成：$url" -ForegroundColor Green
        Write-Host "       关闭服务：网页右上角的「关闭服务」按钮（python pid=$($proc.Id)）" -ForegroundColor DarkGray
        Write-Host "       日志：runs\web.out.log / runs\web.err.log" -ForegroundColor DarkGray
        Open-Browser
        exit 0
    }
}

# ---- 7) 超时：把日志尾巴摊出来，别只报一句「启动失败」 ----
Write-Host "  [x] 60 秒内没就绪：$url" -ForegroundColor Red
Write-Host "      这个窗口可以关掉，但那个 python 进程可能还在（pid=$($proc.Id)）。" -ForegroundColor DarkGray
foreach ($f in @($errLog, $outLog)) {
    if (Test-Path $f) {
        Write-Host ""
        Write-Host "      ---- $f 的最后 20 行 ----" -ForegroundColor DarkGray
        Get-Content $f -Tail 20 | ForEach-Object { Write-Host "      $_" }
    }
}
Read-Host "  按回车退出"
exit 1
