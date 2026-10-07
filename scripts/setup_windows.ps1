# ============================================================================
#  人参素材按标签归类 · Windows 10 一键装环境（对应 macOS 的 scripts/setup.sh）
#
#  用法：powershell -ExecutionPolicy Bypass -File scripts\setup_windows.ps1
#
#  做五件事：
#    1) 建 .venv-ginseng-win + 装 rawpy/pillow/numpy/opencv
#    2) 检查 ffmpeg
#    3) 检查 / 安装 ollama，并拉识别模型
#    4) 生成 config.json（要你自己填目录）
#    5) 预检
# ============================================================================
$ErrorActionPreference = "Stop"
$Here = Split-Path -Parent $MyInvocation.MyCommand.Path
$Tool = Split-Path -Parent $Here
Set-Location $Tool
Write-Host "=== 目录: $Tool ===" -ForegroundColor Cyan

$Model = "qwen3.6:35b"      # 和你 Mac 上 `ollama list` 显示的名字保持一致

# ---------- 1) Python + venv ----------
$py = $null
foreach ($cand in @("py -3", "python", "python3")) {
    & cmd /c "$cand -c ""import sys"" " 2>$null
    if ($LASTEXITCODE -eq 0) { $py = $cand; break }
}
if (-not $py) {
    Write-Host "✗ 没找到 Python。装 64 位 Python 3.10~3.12: https://www.python.org/downloads/windows/" -ForegroundColor Red
    exit 1
}
$Venv = Join-Path $Tool ".venv-ginseng-win"
$VenvPy = Join-Path $Venv "Scripts\python.exe"
if (-not (Test-Path $VenvPy)) {
    Write-Host "==> 建虚拟环境 $Venv"
    & cmd /c "$py -m venv ""$Venv"""
}
Write-Host "==> 装依赖（rawpy / pillow / numpy / opencv）"
& $VenvPy -m pip install --upgrade pip -q
& $VenvPy -m pip install -i https://pypi.tuna.tsinghua.edu.cn/simple -r requirements.txt
if ($LASTEXITCODE -ne 0) { & $VenvPy -m pip install -r requirements.txt }

# ---------- 2) ffmpeg ----------
$ff = Get-Command ffmpeg -ErrorAction SilentlyContinue
if ($ff) {
    Write-Host "==> ffmpeg: $($ff.Source)" -ForegroundColor Green
    $ffprobe = (Get-Command ffprobe -ErrorAction SilentlyContinue).Source
    Write-Host "==> ffprobe: $ffprobe" -ForegroundColor Green
} else {
    Write-Host "✗ 没找到 ffmpeg —— winget install Gyan.FFmpeg  （装完重开终端）" -ForegroundColor Yellow
}

# ---------- 3) ollama ----------
$ol = Get-Command ollama -ErrorAction SilentlyContinue
if (-not $ol) {
    Write-Host "==> 装 ollama"
    try { winget install --id Ollama.Ollama -e --accept-source-agreements --accept-package-agreements }
    catch { Write-Host "  winget 装失败，手动下：https://ollama.com/download/windows" -ForegroundColor Yellow }
} else {
    Write-Host "==> ollama 已就绪" -ForegroundColor Green
}
Write-Host "==> 拉识别模型 $Model（首次几十分钟，看网速）"
& ollama pull $Model

# ---------- 4) config ----------
if (-not (Test-Path (Join-Path $Tool "config.json"))) {
    Copy-Item (Join-Path $Tool "config.windows.example.json") (Join-Path $Tool "config.json")
    Write-Host "==> 已生成 config.json —— **请先编辑它**（photo_dirs / video_dir / out_root / ffmpeg）" -ForegroundColor Yellow
    Write-Host "    注意 JSON 里 Windows 路径要写双反斜杠：D:\\素材\\标本" -ForegroundColor Yellow
} else {
    Write-Host "==> config.json 已存在，保留"
}

# ---------- 5) 预检 ----------
Write-Host ""
Write-Host "==> 预检" -ForegroundColor Cyan
& $VenvPy scripts\pipeline.py check --config config.json

Write-Host ""
Write-Host "============================================================" -ForegroundColor Cyan
Write-Host " 完成。日常用法：双击 scripts\run.bat，或："
Write-Host "   .venv-ginseng-win\Scripts\python.exe scripts\pipeline.py all --config config.json"
Write-Host "============================================================" -ForegroundColor Cyan
