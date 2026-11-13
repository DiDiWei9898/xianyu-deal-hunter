# 打包成单文件 exe（Windows）
# 用法：powershell -ExecutionPolicy Bypass -File build\build_exe.ps1

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
Set-Location -LiteralPath $root

Write-Host "1/4 检查 Python"
python --version
if ($LASTEXITCODE -ne 0) { throw "未找到 python，请先安装 Python 3.9+" }

Write-Host "2/4 安装 PyInstaller"
python -m pip install --upgrade pyinstaller
if ($LASTEXITCODE -ne 0) { throw "PyInstaller 安装失败" }

Write-Host "3/4 打包"
python -m PyInstaller --noconfirm --clean --onefile `
  --name XianyuDealHunter `
  --add-data "app\static;app\static" `
  --hidden-import app.sources.mock `
  --hidden-import app.sources.http_json `
  --hidden-import app.sources.manual `
  run.py
if ($LASTEXITCODE -ne 0) { throw "打包失败" }

Write-Host "4/4 复制插件目录"
New-Item -ItemType Directory -Force -Path "dist\plugins" | Out-Null
Copy-Item -Path "plugins\*.py" -Destination "dist\plugins\" -Force

Write-Host ""
Write-Host "完成：dist\XianyuDealHunter.exe"
Write-Host "首次运行会在 exe 同级目录创建 data\（数据库）"
