# skill-router 一键安装（Windows PowerShell）
#
#   powershell -ExecutionPolicy Bypass -File install.ps1
#
# 真正干活的逻辑在 install.py —— 这里只负责找到 Python 并把控制台切到 UTF-8。

$ErrorActionPreference = "Continue"
try { $null = chcp 65001 } catch {}
try { [Console]::OutputEncoding = [System.Text.Encoding]::UTF8 } catch {}

$dir = if ($PSScriptRoot) { $PSScriptRoot } else { (Get-Location).Path }

$py = $null
foreach ($c in @("python", "python3", "py")) {
    if (Get-Command $c -ErrorAction SilentlyContinue) { $py = $c; break }
}

if (-not $py) {
    Write-Host ""
    Write-Host "  没找到 Python。先装 3.10 或更高版本：" -ForegroundColor Red
    Write-Host "  https://www.python.org/downloads/" -ForegroundColor Red
    Write-Host "  安装时勾选 Add Python to PATH" -ForegroundColor Red
    Write-Host ""
    exit 1
}

& $py "$dir\install.py"
exit $LASTEXITCODE
