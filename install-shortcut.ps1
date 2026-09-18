# 生成一个"双击即用"的快捷方式（.lnk），避开文件关联这道坎。
#
# 为什么需要它：.ps1 在本机关联到了 VSCode，双击会打开编辑器而不是运行；
# 直接双击 main.py 也不对（它是个 CLI，不给参数只会打印用法）。
# .lnk 是唯一不受关联影响的入口 —— 双击就是"用它的目标去启动"，没有中间商。
#
# 本文件对应桌面上的 DeepSeek-Harness.lnk（那个指向 ~/.dsh/desktop-launcher/
# start-dsh-web.cmd）。这里照同一套路做，只是把目标换成项目里的 start-web.cmd。
#
# 跑法（在项目根目录，用 Windows PowerShell 跑一次即可）：
#     powershell -NoProfile -ExecutionPolicy Bypass -File .\install-shortcut.ps1
#     ... -Desktop      同时在桌面也放一个
#     ... -Name "XXX"   换个名字
#
# ⚠️ 本文件自己带 UTF-8 BOM，**不要去掉** —— 理由同 start-web.ps1，见 tests/test_launcher.py。

param(
    [string] $Name = '启动演示台',
    [switch] $Desktop,
    [string] $Icon = '',
    # 生成到哪里；默认项目根。留这个参数是为了让 tests/test_launcher.py 能生成到
    # 临时目录去验证『真的能造出 .lnk 且目标正确』，而不是只检查脚本里有没有那行字。
    [string] $OutDir = ''
)

$ErrorActionPreference = 'Stop'

$root   = $PSScriptRoot
$target = Join-Path $root 'start-web.cmd'

if (-not (Test-Path -LiteralPath $target)) {
    Write-Host "找不到 $target，这个脚本必须放在项目根目录（与 start-web.cmd 同级）。" -ForegroundColor Red
    exit 1
}

if (-not $Icon) {
    # 没有自带图标，就用 PowerShell 自己的 —— 比通用窗口图标更像"能跑的脚本"。
    $Icon = Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\powershell.exe'
}

function New-LauncherLink([string] $linkPath) {
    $sh = New-Object -ComObject WScript.Shell
    $lnk = $sh.CreateShortcut($linkPath)
    $lnk.TargetPath       = $target
    # 工作目录设成项目根：双击后 cmd 的 cwd 由它决定，日志/相对路径都从这里长出来。
    $lnk.WorkingDirectory = $root
    $lnk.IconLocation     = $Icon
    $lnk.Description      = '启动 DeepSeek 物体定位演示台（起服务 + 开浏览器）'
    $lnk.Save()
    return $linkPath
}

$outDir = if ($OutDir) { $OutDir } else { $root }
if (-not (Test-Path -LiteralPath $outDir)) { New-Item -ItemType Directory -Path $outDir -Force | Out-Null }

$made = @()
$made += New-LauncherLink (Join-Path $outDir ($Name + '.lnk'))
if ($Desktop) {
    $desk = [Environment]::GetFolderPath('Desktop')
    $made += New-LauncherLink (Join-Path $desk ($Name + '.lnk'))
}

Write-Host ''
Write-Host '已生成快捷方式：' -ForegroundColor Green
foreach ($m in $made) { Write-Host ("  " + $m) }
Write-Host ''
Write-Host ("  目标: " + $target)
Write-Host ''
Write-Host '双击它就是起服务（隐藏窗口）+ 等就绪 + 开浏览器。' -ForegroundColor DarkGray
Write-Host '关服务走网页右上角的「关闭服务」按钮 —— 服务是隐藏窗口，没有控制台可按 Ctrl+C。' -ForegroundColor DarkGray
