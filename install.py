#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
伯仕记忆系统 — 跨平台一键安装脚本（Windows / Linux / macOS）
=============================================================
安装内容（双轨接入 Hermes）：
  1. 部署代码到 ~/.boshi（克隆仓库 / 已存在则跳过）
  2. 安装 Python 依赖（chromadb / mcp / onnxruntime / transformers）
  3. 下载 bge-m3 ONNX 向量模型（~569MB，断点续传，默认 hf-mirror 国内镜像）
  4. 【插件方式】复制 plugins/boshi → $HERMES_HOME/plugins/boshi/
     并写入 config.yaml: memory.provider = boshi
  5. 【MCP 方式】写入 config.yaml: mcp_servers.boshi → boshi_mcp_server.py
  6. 复制 skills/boshi-memory → $HERMES_HOME/skills/

用法:
  python install.py                # 本机默认安装
  python install.py --home PATH    # 指定 HERMES_HOME（默认自动探测）
  python install.py --no-deps      # 跳过依赖安装
  python install.py --no-model     # 跳过模型下载
"""
import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path

REPO_URL = "https://github.com/wulezhi-hui/boshi-memory-system.git"
BOSHI_DIR = Path.home() / ".boshi"
BOSHI_VENV = BOSHI_DIR / "venv"  # 伯仕独立 venv，依赖均安装于此


def get_hermes_home() -> Path:
    """探测 Hermes 配置目录（HERMES_HOME）。"""
    env = os.environ.get("HERMES_HOME")
    if env:
        return Path(env)
    if os.name == "nt":
        # Windows: %LOCALAPPDATA%\\hermes
        return Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local")) / "hermes"
    # Linux/macOS: ~/.hermes（旧版可能 ~/.config/hermes）
    home_hermes = Path.home() / ".hermes"
    if home_hermes.is_dir():
        return home_hermes
    return Path.home() / ".config" / "hermes"


def find_hermes_python(hermes_home: Path) -> str:
    """优先返回 Hermes 自带 venv 的 python（MCP server 依赖装在那里最稳）。"""
    candidates = [
        hermes_home / "hermes-agent" / "venv" / "Scripts" / "python.exe",  # Windows
        hermes_home / "hermes-agent" / "venv" / "bin" / "python",          # Linux/macOS
    ]
    for c in candidates:
        if c.exists():
            return str(c)
    return sys.executable


def deploy_code() -> None:
    """克隆/更新仓库到 ~/.boshi。"""
    print("[1/6] 部署代码到", BOSHI_DIR)
    if (BOSHI_DIR / ".git").exists():
        subprocess.run(["git", "pull", "--ff-only"], cwd=str(BOSHI_DIR), check=False)
        print("   ✅ 已是最新（git pull）")
    else:
        shutil.rmtree(BOSHI_DIR, ignore_errors=True)
        BOSHI_DIR.mkdir(parents=True, exist_ok=True)
        subprocess.run(["git", "clone", REPO_URL, str(BOSHI_DIR)], check=True)
        print("   ✅ 已克隆仓库")


def install_deps() -> None:
    """安装 Python 依赖到伯仕 venv。"""
    print("[2/6] 安装 Python 依赖到 ~/.boshi/venv...")
    deps = ["chromadb", "mcp>=2.0.0", "onnxruntime", "transformers", "pyyaml"]
    pip = [str(BOSHI_VENV / "Scripts" / "pip.exe"), "install"]
    for d in deps:
        subprocess.run(pip + [d], check=False)
    print("   ✅ 伯仕 venv 依赖安装完成")


def install_hermes_deps(hermes_home: Path) -> None:
    """额外安装到 Hermes venv（让插件直接 import 可用）。"""
    print("[2b/6] 检查 Hermes venv 依赖（可选）...")
    hermes_python = find_hermes_python(hermes_home)
    if not hermes_python or not Path(hermes_python).exists():
        print("   ⚠️ 未找到 Hermes venv，跳过（插件将通过 fallback 使用伯仕 venv）")
        return

    # 检查是否已安装
    result = subprocess.run([hermes_python, "-m", "pip", "show", "chromadb"],
                          capture_output=True, text=True)
    if result.returncode == 0:
        print("   ✅ Hermes venv 已有 chromadb，无需额外安装")
        return

    # 安装
    deps = ["chromadb", "mcp>=2.0.0", "onnxruntime", "transformers", "pyyaml"]
    pip = [hermes_python, "-m", "pip", "install"]
    print("   ℹ️ 正在安装到 Hermes venv（首次启动会稍慢）...")
    for d in deps:
        subprocess.run(pip + [d], check=False)
    print("   ✅ Hermes venv 依赖安装完成（插件可直接 import，无需 fallback）")


def install_model() -> None:
    """下载 bge-m3 ONNX 向量模型（调用仓库自带 download_model.py）。"""
    print("[3/6] 下载 bge-m3 ONNX 向量模型（~569MB，断点续传）...")
    script = BOSHI_DIR / "download_model.py"
    if not script.exists():
        print("   ⚠️ 仓库缺少 download_model.py，跳过模型下载（首次向量化会失败！）")
        return
    subprocess.run([sys.executable, str(script), "--check"], cwd=str(BOSHI_DIR))
    if not (BOSHI_DIR / "models" / "bge-m3" / "onnx" / "model_quantized.onnx").exists():
        subprocess.run([sys.executable, str(script)], cwd=str(BOSHI_DIR))
    print("   ✅ 模型就绪")


def install_plugin(hermes_home: Path) -> None:
    """插件方式：复制 plugins/boshi 并配置 memory.provider=boshi。"""
    print("[4/6] 安装 Memory Provider 插件（插件方式）...")
    src = BOSHI_DIR / "plugins" / "boshi"
    dst = hermes_home / "plugins" / "boshi"
    if not src.exists():
        print("   ⚠️ 仓库缺少 plugins/boshi，跳过插件安装")
        return
    dst.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src / "__init__.py", dst / "__init__.py")
    print(f"   ✅ 插件已复制到 {dst}")


def configure_config(hermes_home: Path, mcp_command: str) -> None:
    """写入 config.yaml：memory.provider=boshi（插件）+ mcp_servers.boshi（MCP）。"""
    print("[5/6] 配置 Hermes config.yaml（双轨）...")
    config_path = hermes_home / "config.yaml"
    if config_path.exists():
        shutil.copy2(config_path, config_path.with_suffix(".yaml.bak"))
        print(f"   ℹ️ 已备份原配置到 {config_path}.bak")

    try:
        import yaml
    except ImportError:
        subprocess.run([sys.executable, "-m", "pip", "install", "pyyaml"], check=False)
        import yaml  # noqa: F401

    if config_path.exists():
        with open(config_path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
    else:
        data = {}

    # 插件方式：memory.provider = boshi
    data.setdefault("memory", {})
    data["memory"]["provider"] = "boshi"
    data["memory"]["memory_enabled"] = True
    data["memory"]["user_profile_enabled"] = True

    # MCP 方式：mcp_servers.boshi（command 用 Hermes venv python 优先）
    data.setdefault("mcp_servers", {})
    data["mcp_servers"].setdefault("boshi", {})
    data["mcp_servers"]["boshi"]["enabled"] = True
    data["mcp_servers"]["boshi"]["command"] = mcp_command
    data["mcp_servers"]["boshi"]["args"] = [str(BOSHI_DIR / "boshi_mcp_server.py")]

    config_path.parent.mkdir(parents=True, exist_ok=True)
    with open(config_path, "w", encoding="utf-8") as f:
        yaml.dump(data, f, default_flow_style=False, sort_keys=False, allow_unicode=True)
    print(f"   ✅ 配置已写入 {config_path}")


def install_skill(hermes_home: Path) -> None:
    """复制 boshi-memory skill。"""
    print("[6/6] 安装 boshi-memory skill...")
    src = BOSHI_DIR / "skills" / "boshi-memory"
    dst = hermes_home / "skills" / "boshi-memory"
    if src.exists():
        shutil.rmtree(dst, ignore_errors=True)
        shutil.copytree(src, dst)
        print(f"   ✅ Skill 已复制到 {dst}")
    else:
        print("   ⚠️ 仓库缺少 skills/boshi-memory，跳过")


def main() -> None:
    parser = argparse.ArgumentParser(description="伯仕记忆系统安装脚本")
    parser.add_argument("--home", default=None, help="HERMES_HOME 路径（默认自动探测）")
    parser.add_argument("--no-deps", action="store_true", help="跳过依赖安装")
    parser.add_argument("--no-model", action="store_true", help="跳过模型下载")
    args = parser.parse_args()

    hermes_home = Path(args.home) if args.home else get_hermes_home()
    mcp_command = find_hermes_python(hermes_home)
    print(f"🦄 伯仕记忆系统安装脚本 | HERMES_HOME = {hermes_home}")
    print(f"   MCP command = {mcp_command}")

    deploy_code()
    if not args.no_deps:
        install_deps()
        install_hermes_deps(hermes_home)  # 额外安装到 Hermes venv，确保插件直接可用
    if not args.no_model:
        install_model()
    install_plugin(hermes_home)
    configure_config(hermes_home, mcp_command)
    install_skill(hermes_home)

    print()
    print("=" * 52)
    print("✅ 安装完成！重启 Hermes 后生效：")
    print(f"   插件方式: memory.provider = boshi（每轮自动召回/存储）")
    print(f"   MCP 方式 : mcp_servers.boshi（8 个 boshi_* 工具）")
    print()
    print("验证命令:")
    print("   hermes memory status          # 应显示 Provider: boshi")
    print("   hermes mcp test boshi         # 应显示连接成功")
    print("=" * 52)


if __name__ == "__main__":
    main()
