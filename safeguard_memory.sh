#!/bin/bash
# ==============================================================
# 伯仕记忆系统升级安全脚本 v1.0
# 在 Hermes 升级前执行：备份记忆系统快照 + 验证完整性
# 在 Hermes 升级后执行：验证完整性 + 恢复被覆写的文件
# ==============================================================
set -e

BOSHI_HOME="$HOME/.boshi"
GUARD="$BOSHI_HOME/memory/integrity_guard.py"
SNAPSHOT_LABEL="${1:-pre-upgrade}"

echo "========================================"
echo " 伯仕记忆系统升级安全脚本"
echo "========================================"

case "${SNAPSHOT_LABEL}" in
  pre-upgrade|PRE*|pre*)
    echo ""
    echo "[1/3] 生成完整性基准 manifest..."
    python "$GUARD" manifest > /dev/null 2>&1

    echo "[2/3] 创建快照..."
    python "$GUARD" snapshot "pre-upgrade"

    echo "[3/3] 验证快照完整性..."
    python "$GUARD" check
    echo ""
    echo "✅ 升级前安全准备完成，可以开始升级"
    echo "   恢复命令: bash $0 restore"
    ;;

  restore|RESTORE|restore*)
    echo ""
    echo "[1/2] 验证当前状态..."
    python "$GUARD" check || true

    echo "[2/2] 从最近快照恢复..."
    python "$GUARD" restore
    echo ""
    echo "✅ 恢复完成，建议重启 Hermes 使变更生效"
    ;;

  check|CHECK*)
    python "$GUARD" check
    ;;

  *)
    echo "用法:"
    echo "  升级前: bash safeguard_memory.sh pre-upgrade"
    echo "  升级后: bash safeguard_memory.sh restore"
    echo "  巡检:   bash safeguard_memory.sh check"
    ;;
esac
