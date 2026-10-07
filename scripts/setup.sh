#!/usr/bin/env bash
# ============================================================================
# 人参素材归类工具 - 一键初始化环境 (macOS + Homebrew)
# 用法:  bash scripts/setup.sh
# 做: 建 Python venv、装 rawpy/Pillow/numpy、检查/安装 ffmpeg 与 ollama、拉取识别模型、生成 config.json
# ============================================================================
set -e
HERE="$(cd "$(dirname "$0")" && pwd)"
TOOL_DIR="$(dirname "$HERE")"
VENV="$TOOL_DIR/.venv-ginseng"
PYTHON_BIN="$(command -v python3)"
# 镜像: 国内慢时用清华源 (可 export PIP_INDEX_URL=... 覆盖)
MIRROR="${PIP_INDEX_URL:-https://pypi.tuna.tsinghua.edu.cn/simple}"
MODEL="qwen3.6:35b"

echo "==> 目录: $TOOL_DIR"
[ -z "$PYTHON_BIN" ] && { echo "✗ 未找到 python3"; exit 1; }
echo "==> python3: $PYTHON_BIN ($($PYTHON_BIN --version))"

# 1) venv
if [ ! -d "$VENV" ]; then
  echo "==> 创建 venv: $VENV"
  "$PYTHON_BIN" -m venv "$VENV"
fi
. "$VENV/bin/activate"
echo "==> 升级 pip"
pip install --upgrade pip -q

# 2) Python 库 (rawpy/Pillow/numpy/cv2) —— 幂等: 已装则跳过, 不再重复装
if python -c "import rawpy, PIL, numpy, cv2" >/dev/null 2>&1; then
  echo "==> rawpy/Pillow/numpy/opencv 已就绪, 跳过安装"
else
  echo "==> 安装 rawpy pillow numpy opencv-python-headless (镜像: $MIRROR)"
  pip install -i "$MIRROR" rawpy pillow numpy opencv-python-headless
fi

# 3) ffmpeg / ffprobe
if ! command -v ffmpeg >/dev/null 2>&1 && [ ! -x /opt/homebrew/bin/ffmpeg ]; then
  echo "==> 安装 ffmpeg (brew)"
  brew install ffmpeg
else
  echo "==> ffmpeg 已就绪"
fi

# 4) ollama
if ! command -v ollama >/dev/null 2>&1 && [ ! -x /opt/homebrew/bin/ollama ]; then
  echo "==> 安装 ollama (brew)"
  brew install ollama
  brew services start ollama
fi
echo "==> 确保 ollama 在运行"
curl -s -o /dev/null -w "%{http_code}" http://127.0.0.1:11434/api/tags 2>/dev/null | grep -q 200 || {
  echo "   ollama 未运行, 尝试启动..."; brew services start ollama; sleep 3; }
echo "==> 拉取识别模型 $MODEL (首次较长)"
ollama pull "$MODEL"

# 5) 生成 config.json
if [ ! -f "$TOOL_DIR/config.json" ]; then
  echo "==> 生成配置模板 config.json"
  (cd "$TOOL_DIR" && python scripts/pipeline.py init-config)
  echo "   请编辑 config.json: 设置 photo_dirs / video_dir / out_root 等"
else
  echo "==> config.json 已存在, 保留"
fi

echo
echo "============================================================"
echo " 环境就绪。运行示例:"
echo "   cd $TOOL_DIR && . .venv-ginseng/bin/activate"
echo "   python scripts/pipeline.py check              # 预检"
echo "   python scripts/pipeline.py all --config config.json"
echo "   # 或分步: manifest -> classify -> reflect -> group -> apply"
echo "============================================================"
