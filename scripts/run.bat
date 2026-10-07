@echo off
chcp 936 >nul
rem ===========================================================================
rem  人参素材按标签归类 · 一键运行（Windows 版，对应 macOS 的 scripts/run.sh）
rem  前置：先跑过 scripts\setup_windows.ps1，并编辑好 config.json
rem ===========================================================================
setlocal
set PYTHONUTF8=1
set PYTHONIOENCODING=utf-8
cd /d "%~dp0.."

set "PY=%CD%\.venv-ginseng-win\Scripts\python.exe"
if not exist "%PY%" (
  echo [提示] 没找到 .venv-ginseng-win，改用系统 python。建议先跑 scripts\setup_windows.ps1
  set "PY=python"
)

if not exist "config.json" (
  echo [X] 缺少 config.json。先跑 setup_windows.ps1 生成，或从 config.windows.example.json 复制一份
  exit /b 1
)

echo ==^> 预检...
"%PY%" scripts\pipeline.py check --config config.json

echo.
echo ==^> 全流程: manifest 到 classify 到 group 到 apply（copy，不动原文件）
"%PY%" scripts\pipeline.py all --config config.json
if errorlevel 1 (
  echo [X] 流程中断，看上面报错
  exit /b 1
)

echo.
echo ============================================================
echo  完成。**先看核对报告再决定是否 move**：
echo    cache\核对报告.csv
echo  确认无误后删源落盘：
echo    "%PY%" scripts\pipeline.py apply --config config.json --mode move
echo ============================================================
endlocal
