#!/bin/bash
# 动态压缩阈值补丁恢复脚本
# Hermes升级后执行此脚本恢复动态threshold逻辑
# 用法: bash ~/.boshi/apply_dynamic_compression_patches.sh

HERMES_DIR="/c/Users/Administrator/AppData/Local/hermes/hermes-agent"
PATCH_DIR="/c/Users/Administrator/.boshi"

echo "=== 动态压缩阈值补丁恢复 ==="

cd "$HERMES_DIR" || { echo "❌ Hermes目录不存在"; exit 1; }

if git apply --check "$PATCH_DIR/dynamic_compression_threshold.patch" 2>/dev/null; then
    git apply "$PATCH_DIR/dynamic_compression_threshold.patch"
    echo "✅ 补丁应用成功"
else
    echo "⚠️  补丁无法直接应用（代码可能有变更），请手动检查"
    echo "   补丁文件: $PATCH_DIR/dynamic_compression_threshold.patch"
    echo "   手动方式：在agent_init.py的 _ctx = getattr(...) 后加："
    echo '     try:'
    echo '       from pathlib import Path; _boshi = Path.home() / ".boshi"; import sys; sys.path.insert(0, str(_boshi))'
    echo '       from dynamic_threshold import get_dynamic_threshold'
    echo '       _dyn = get_dynamic_threshold(_ctx)'
    echo '       if _dyn is not None and _dyn != compression_threshold:'
    echo '         agent.context_compressor.threshold_percent = _dyn'
    echo '         agent.context_compressor.threshold_tokens = max(int(_ctx * _dyn), MINIMUM_CONTEXT_LENGTH)'
    echo '         compression_threshold = _dyn'
    echo '     except Exception: pass'
    echo ''
    echo '   在context_compressor.py的 self.context_length = context_length 后加：'
    echo '     try:'
    echo '       from pathlib import Path; _boshi = Path.home() / ".boshi"; import sys; sys.path.insert(0, str(_boshi))'
    echo '       from dynamic_threshold import get_dynamic_threshold'
    echo '       _dyn = get_dynamic_threshold(context_length)'
    echo '       if _dyn is not None: self.threshold_percent = _dyn'
    echo '     except Exception: pass'
fi

echo ""
echo "=== 阈值策略（修改 ~/.boshi/dynamic_threshold.yaml 自定义）==="
cat "$PATCH_DIR/dynamic_threshold.yaml" 2>/dev/null | grep -E "max_context|threshold:" | paste - - | sed 's/.*max_context: *\([0-9]*\).*threshold: *\([0-9.]*\).*/  ≤\1 ctx → \2 压缩/'
