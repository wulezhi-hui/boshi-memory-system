#!/bin/bash
# 伯仕记忆系统 v6.0 — Ubuntu 一键安装脚本
# 用法: curl -sL https://raw.githubusercontent.com/wulezhi-hui/boshi-memory-system/main/install.sh | bash

set -e

REPO_URL="https://github.com/wulezhi-hui/boshi-memory-system.git"
INSTALL_DIR="$HOME/.boshi"
HERMES_DIR="${HERMES_DIR:-$HOME/.config/hermes}"

echo "🦄 伯仕记忆系统 v6.0 安装脚本"
echo "================================"

# 0. 检查依赖
echo "[1/6] 检查 Python3..."
python3 --version >/dev/null 2>&1 || { echo "❌ 未找到 python3，请先安装"; exit 1; }

echo "[2/6] 检查 pip..."
pip3 --version >/dev/null 2>&1 || { echo "❌ 未找到 pip3，请先安装"; exit 1; }

# 1. 克隆/更新仓库
echo "[3/6] 克隆仓库到 $INSTALL_DIR..."
if [ -d "$INSTALL_DIR/.git" ]; then
    cd "$INSTALL_DIR"
    git pull --ff-only
else
    rm -rf "$INSTALL_DIR"
    git clone "$REPO_URL" "$INSTALL_DIR"
fi

# 2. 安装 Python 依赖
echo "[4/6] 安装 Python 依赖..."
pip3 install --user chromadb openai pydantic 2>/dev/null || pip3 install chromadb openai pydantic

# 3. 安装 Skill
echo "[5/6] 安装 boshi-memory skill..."
mkdir -p "$HERMES_DIR/skills"
if [ -d "$INSTALL_DIR/skills/boshi-memory" ]; then
    rm -rf "$HERMES_DIR/skills/boshi-memory"
    cp -r "$INSTALL_DIR/skills/boshi-memory" "$HERMES_DIR/skills/"
    echo "   ✅ Skill 已复制到 $HERMES_DIR/skills/boshi-memory"
fi

# 4. 配置 Hermes config.yaml
echo "[6/6] 配置 Hermes..."
CONFIG_FILE="$HERMES_DIR/config.yaml"

# 备份原配置
if [ -f "$CONFIG_FILE" ]; then
    cp "$CONFIG_FILE" "$CONFIG_FILE.bak.$(date +%Y%m%d_%H%M%S)"
fi

# 如果 config 不存在，创建基础结构
if [ ! -f "$CONFIG_FILE" ]; then
    mkdir -p "$HERMES_DIR"
    cat > "$CONFIG_FILE" << 'CONFIGEOF'
model:
  default: auto
agent:
  tool_use_enforcement: normal
CONFIGEOF
fi

# 用 Python 安全地注入记忆系统配置
python3 << 'PYEOF'
import yaml, os, sys

config_path = os.path.expanduser("~/.config/hermes/config.yaml")
if not os.path.exists(config_path):
    os.makedirs(os.path.dirname(config_path), exist_ok=True)
    data = {}
else:
    with open(config_path, 'r') as f:
        data = yaml.safe_load(f) or {}

# 确保 memory 节存在
data.setdefault('memory', {})
data['memory']['provider'] = 'boshi'
data['memory']['enabled'] = True

# 确保 mcp_servers 节存在
data.setdefault('mcp_servers', {})
data['mcp_servers'].setdefault('boshi', {})
data['mcp_servers']['boshi']['enabled'] = True
data['mcp_servers']['boshi']['command'] = 'python3'
data['mcp_servers']['boshi']['args'] = [os.path.expanduser('~/.boshi/boshi_mcp_server.py')]

with open(config_path, 'w') as f:
    yaml.dump(data, f, default_flow_style=False, sort_keys=False, allow_unicode=True)

print("   ✅ 配置已写入", config_path)
PYEOF

# 5. 提示重启
echo ""
echo "================================"
echo "✅ 伯仕记忆系统安装完成！"
echo ""
echo "接下来请重启 Hermes Gateway："
echo "   hermes gateway run"
echo ""
echo "或直接对 Hermes 说："
echo "   '重启 gateway'"
echo ""
echo "📂 安装位置: $INSTALL_DIR"
echo "📂 Skill 位置: $HERMES_DIR/skills/boshi-memory"
echo "📝 配置备份: $CONFIG_FILE.bak.*"
