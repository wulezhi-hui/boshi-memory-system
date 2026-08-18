#!/bin/bash
# 伯仕记忆系统 v6.2 — Ubuntu/Linux 一键安装脚本（双轨接入：插件 + MCP）
# 用法: curl -sL https://raw.githubusercontent.com/wulezhi-hui/boshi-memory-system/main/install.sh | bash
#
# 说明：
# - 仓库不包含 ~569MB 的 bge-m3 ONNX 模型，安装时调用 download_model.py
#   （默认 hf-mirror.com 国内镜像，可设 BOSHI_MODEL_SOURCE=hf 切换官方源）
# - 如果目标机器有代理，请提前设置 http_proxy/https_proxy 环境变量

set -e

REPO_URL="https://github.com/wulezhi-hui/boshi-memory-system.git"
INSTALL_DIR="$HOME/.boshi"
# Hermes 配置目录：用户显式设置优先，否则探测 ~/.hermes / ~/.config/hermes
if [ -z "${HERMES_DIR:-}" ]; then
    if [ -d "$HOME/.hermes" ]; then
        HERMES_DIR="$HOME/.hermes"
    elif [ -d "$HOME/.config/hermes" ]; then
        HERMES_DIR="$HOME/.config/hermes"
    else
        HERMES_DIR="$HOME/.hermes"
    fi
fi
export HERMES_DIR

echo "🦄 伯仕记忆系统 v6.2 安装脚本（bge-m3 ONNX 向量模型）"
echo "========================================================"

# 0. 检查依赖
echo "[1/7] 检查 Python3 / pip..."
python3 --version > /dev/null 2>&1 || { echo "❌ 未找到 python3，请先安装"; exit 1; }
pip3 --version > /dev/null 2>&1 || { echo "❌ 未找到 pip3，请先安装"; exit 1; }

# 1. 克隆/更新仓库
echo "[2/7] 克隆仓库到 $INSTALL_DIR..."
if [ -d "$INSTALL_DIR/.git" ]; then
    cd "$INSTALL_DIR"
    git pull --ff-only
else
    rm -rf "$INSTALL_DIR"
    git clone "$REPO_URL" "$INSTALL_DIR"
fi

# 2. 安装 Python 依赖（含 bge-m3 ONNX 推理所需）
echo "[3/7] 安装 Python 依赖..."
pip3 install --user chromadb "mcp>=2.0.0" onnxruntime transformers pyyaml 2>/dev/null \
    || pip3 install chromadb "mcp>=2.0.0" onnxruntime transformers pyyaml

# 3. 下载 bge-m3 ONNX 向量模型（~569MB，断点续传）
echo "[4/7] 下载 bge-m3 ONNX 向量模型（int8 量化 ~569MB，请耐心等待）..."
cd "$INSTALL_DIR"
if python3 download_model.py --check; then
    echo "   ✅ 模型已就位，跳过下载"
else
    SOURCE_ARG=""
    if [ -n "${BOSHI_MODEL_SOURCE:-}" ]; then
        SOURCE_ARG="--source $BOSHI_MODEL_SOURCE"
    fi
    python3 download_model.py $SOURCE_ARG
fi

# 4. 安装 Memory Provider 插件（插件方式接入）
echo "[5/7] 安装 Memory Provider 插件..."
mkdir -p "$HERMES_DIR/plugins"
if [ -d "$INSTALL_DIR/plugins/boshi" ]; then
    rm -rf "$HERMES_DIR/plugins/boshi"
    cp -r "$INSTALL_DIR/plugins/boshi" "$HERMES_DIR/plugins/boshi"
    echo "   ✅ 插件已复制到 $HERMES_DIR/plugins/boshi"
else
    echo "   ⚠️ 仓库中未找到 plugins/boshi，跳过插件安装"
fi

# 5. 安装 Skill
echo "[6/7] 安装 boshi-memory skill..."
mkdir -p "$HERMES_DIR/skills"
if [ -d "$INSTALL_DIR/skills/boshi-memory" ]; then
    rm -rf "$HERMES_DIR/skills/boshi-memory"
    cp -r "$INSTALL_DIR/skills/boshi-memory" "$HERMES_DIR/skills/"
    echo "   ✅ Skill 已复制到 $HERMES_DIR/skills/boshi-memory"
else
    echo "   ⚠️ 仓库中未找到 skills/boshi-memory，跳过"
fi

# 6. 配置 Hermes config.yaml（双轨：插件 + MCP）
echo "[7/7] 配置 Hermes..."
CONFIG_FILE="$HERMES_DIR/config.yaml"

if [ -f "$CONFIG_FILE" ]; then
    cp "$CONFIG_FILE" "$CONFIG_FILE.bak.$(date +%Y%m%d_%H%M%S)"
fi

if [ ! -f "$CONFIG_FILE" ]; then
    mkdir -p "$HERMES_DIR"
    cat > "$CONFIG_FILE" << 'CONF'
model:
  default: auto
agent:
  tool_use_enforcement: normal
CONF
fi

python3 - "$HERMES_DIR" << 'PY'
import os, sys, yaml
config_path = os.path.join(sys.argv[1], "config.yaml")
with open(config_path, "r") as f:
    data = yaml.safe_load(f) or {}

# 插件方式：memory.provider = boshi
data.setdefault("memory", {})
data["memory"]["provider"] = "boshi"
data["memory"]["memory_enabled"] = True
data["memory"]["user_profile_enabled"] = True

# MCP 方式：mcp_servers.boshi
data.setdefault("mcp_servers", {})
data["mcp_servers"].setdefault("boshi", {})
data["mcp_servers"]["boshi"]["enabled"] = True
data["mcp_servers"]["boshi"]["command"] = "python3"
data["mcp_servers"]["boshi"]["args"] = [os.path.expanduser("~/.boshi/boshi_mcp_server.py")]

with open(config_path, "w") as f:
    yaml.dump(data, f, default_flow_style=False, sort_keys=False, allow_unicode=True)
print("   ✅ Hermes 配置已写入", config_path)
PY

echo ""
echo "========================================================"
echo "✅ 伯仕记忆系统安装完成！"
echo ""
echo "📂 安装位置: $INSTALL_DIR"
echo "🧠 bge-m3 ONNX 模型: $INSTALL_DIR/models/bge-m3/（int8 量化）"
echo "🔌 插件方式: $HERMES_DIR/plugins/boshi（memory.provider = boshi）"
echo "🔗 MCP 方式 : mcp_servers.boshi（8 个 boshi_* 工具）"
echo ""
echo "下一步："
echo "   1. 在 Hermes 配置确认：hermes memory status / hermes mcp list"
echo "   2. 重启 Hermes：hermes gateway run（或 CLI 重新打开）"
echo ""
echo "🦄 若有疑问，随时呼唤伯仕。"
echo ""
