#!/usr/bin/env bash
# ============================================================================
# 人参素材归类 - 一键运行器 (同事友好)
# 前置: 已跑过 bash scripts/setup.sh
# 用法:  bash scripts/run.sh
# 说明: 会自动校验 config.json 的目录是否有效, 无效则提示你填, 有效则全流程跑
# ============================================================================
set -e
HERE="$(cd "$(dirname "$0")" && pwd)"
TOOL_DIR="$(dirname "$HERE")"
CFG="$TOOL_DIR/config.json"
VENV="$TOOL_DIR/.venv-ginseng"

[ -d "$VENV" ] || { echo "✗ 还没初始化环境, 先跑: bash scripts/setup.sh"; exit 1; }
. "$VENV/bin/activate"
cd "$TOOL_DIR"

# 校验配置
PARSE() { python -c "import json,sys;print(json.load(open('$CFG')).get('$1',''))" 2>/dev/null; }
if [ ! -f "$CFG" ]; then
  echo "✗ 缺少 config.json, 先生成并填写:"
  echo "   python scripts/pipeline.py init-config"
  echo "   然后编辑 config.json 的 photo_dirs / video_dir / out_root"
  exit 1
fi
echo "==> 预检..."
python scripts/pipeline.py check || true

echo "==> 校验素材目录..."
BAD=0
for d in $(python -c "import json;print(' '.join(json.load(open('$CFG')).get('photo_dirs',[])))"); do
  [ -d "$d" ] || { echo "   ✗ 照片目录不存在: $d"; BAD=1; }
done
VDIR=$(PARSE video_dir)
[ -z "$VDIR" ] || [ -d "$VDIR" ] || { [ -n "$VDIR" ] && { echo "   ✗ 视频目录不存在: $VDIR"; BAD=1; }; }
if [ "$BAD" = "1" ]; then
  echo "→ 请修正 config.json 后重跑 (photo_dirs/video_dir/out_root)。"
  echo "   可用: python scripts/pipeline.py init-config 重新生成模板"
  exit 1
fi

echo "==> 开始全流程: manifest -> classify -> group -> apply (copy, 不动原文件)"
python scripts/pipeline.py all --config "$CFG"
echo
echo "============================================================"
echo " 完成。请核对报告: $(python -c "import json;print(json.load(open('$CFG')).get('cache_dir'))")/核对报告.csv"
echo " 确认无误后可改用 --mode move 删源:"
echo "   python scripts/pipeline.py apply --config $CFG --mode move"
echo "============================================================"
