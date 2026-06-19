#!/bin/bash
# 伯仕记忆系统 v6.1 — Ubuntu 一键安装脚本（自动下载 ONNX 模型，不打包）
# 用法: curl -sL https://raw.githubusercontent.com/wulezhi-hui/boshi-memory-system/main/install.sh | bash
#
# 说明：
# - 仓库本身不包含 86MB 的 model.onnx（太大了）
# - 安装时会利用 ChromaDB 内置机制自动下载到 ~/.boshi/models/
# - 如果目标机器有代理，请提前设置 http_proxy/https_proxy 环境变量

set -e

REPO_URL="https://github.com/wulezhi-hui/boshi-memory-system.git"
INSTALL_DIR="$HOME/.boshi"
HERMES_DIR="${HERMES_DIR:-$HOME/.config/hermes}"
MODEL_DIR="$INSTALL_DIR/models/all-MiniLM-L6-v2"

echo "🦄 伯仕记忆系统 v6.1 安装脚本（自动下载 ONNX 模型）"
echo "================================================"

# 0. 检查依赖
echo "[1/6] 检查 Python3..."
python3 --version > /dev/null 2>&1 || { echo "❌ 未找到 python3，请先安装"; exit 1; }

echo "[2/6] 检查 pip..."
pip3 --version > /dev/null 2>&1 || { echo "❌ 未找到 pip3，请先安装"; exit 1; }

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
pip3 install --user chromadb 2>/dev/null || pip3 install chromadb

# 3. 预下载 ONNX 向量模型
# 如果模型目录已存在，跳过；否则让 ChromaDB 自动下载一次
echo "[5/6] 准备 ONNX 向量模型（自动下载）..."
python3 << 'PY'
import os, shutil
from pathlib import Path

model_dir = Path(os.path.expanduser("~/.boshi/models/all-MiniLM-L6-v2"))

# 如果模型已经就位，跳过
if (model_dir / "onnx" / "model.onnx").exists():
    print(f"   ℹ️ ONNX 模型已存在于 {model_dir}")
    print(f"   来源: 本地副本")
    exit(0)

# 否则，触发 ChromaDB 内置下载机制
print(f"   ⬇️ 正在下载 all-MiniLM-L6-v2 ONNX 模型（约 86MB，请稍候）...")
try:
    from chromadb.utils.embedding_functions.onnx_mini_lm_l6_v2 import ONNXMiniLM_L6_V2
    ef = ONNXMiniLM_L6_V2()  # 首次实例化会自动下载

    # 下载完成后，ChromaDB 把它放在 ~/.cache/chroma/onnx_models/...
    cache_dir = Path(os.path.expanduser("~/.cache/chroma/onnx_models/all-MiniLM-L6-v2"))
    if cache_dir.exists():
        model_dir.mkdir(parents=True, exist_ok=True)
        target = model_dir / "onnx"
        if not target.exists():
            shutil.copytree(cache_dir, target)
            print(f"   ✅ 模型已下载并复制到 {target}")
        else:
            print(f"   ℹ️ 模型目录已存在，跳过复制")
    else:
        print(f"   ⚠️ ChromaDB 下载路径未找到，将在首次查询时自动重试")
except Exception as e:
    print(f"   ⚠️ 模型下载遇到问题: {e}")
    print(f"   伯仕记忆系统仍可运行，首次调用向量化时将再次尝试自动下载。")
PY

# 4. 安装 Skill
echo "[6/6] 安装 boshi-memory skill..."
mkdir -p "$HERMES_DIR/skills"
if [ -d "$INSTALL_DIR/skills/boshi-memory" ]; then
    rm -rf "$HERMES_DIR/skills/boshi-memory"
    cp -r "$INSTALL_DIR/skills/boshi-memory" "$HERMES_DIR/skills/"
    echo "   ✅ Skill 已复制到 $HERMES_DIR/skills/boshi-memory"
else
    echo "   ⚠️ 仓库中未找到 skills/boshi-memory，跳过"
fi

# 5. 配置 Hermes config.yaml
echo ""
echo "🔧 配置 Hermes..."
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

python3 << 'PY'
import yaml, os
config_path = os.path.expanduser("~/.config/hermes/config.yaml")
if not os.path.exists(config_path):
    os.makedirs(os.path.dirname(config_path), exist_ok=True)
    data = {}
else:
    with open(config_path, 'r') as f:
        data = yaml.safe_load(f) or {}

data.setdefault('memory', {})
data['memory']['provider'] = 'boshi'
data['memory']['enabled'] = True

data.setdefault('mcp_servers', {})
data['mcp_servers'].setdefault('boshi', {})
data['mcp_servers']['boshi']['enabled'] = True
data['mcp_servers']['boshi']['command'] = 'python3'
data['mcp_servers']['boshi']['args'] = [os.path.expanduser('~/.boshi/boshi_mcp_server.py')]

with open(config_path, 'w') as f:
    yaml.dump(data, f, default_flow_style=False, sort_keys=False, allow_unicode=True)
print("   ✅ Hermes 配置已写入", config_path)
PY

echo ""
echo "================================================"
echo "✅ 伯仕记忆系统安装完成！"
echo ""
echo "📂 安装位置: $INSTALL_DIR"
echo "🧠 ONNX 模型: $MODEL_DIR/onnx/（首次使用自动下载）"
echo "📂 Skill 位置: $HERMES_DIR/skills/boshi-memory（如有）"
echo ""
echo "下一步："
echo "   重启 Hermes Gateway："
echo "      hermes gateway run"
echo ""
echo "🦄 若有疑问，随时呼唤伯仕。"
echo ""
