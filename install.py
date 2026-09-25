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


# 插件在 Hermes 进程内 import 伯仕核心，所以这些依赖必须装进 **Hermes 运行时**：
# chromadb（向量库）、onnxruntime + transformers（bge-m3 推理 / tokenizer）。
# ⚠️ Hermes 每次运行时换代（换 venv / 换 python 版本）都会连带卸掉它们——症状是
#    插件**静默失忆**（不召回、不写入、日志还照样显示 registered/activated），
#    只能靠 L2 写入量（source=hermes_plugin）发现。安装脚本须能自愈这一点。
PLUGIN_DEPS = ["chromadb", "onnxruntime", "transformers"]
# 不用 pip show 探测——新版 Hermes venv 由 uv 管理，可能根本没有 pip
_DEPS_PROBE = (
    "import importlib.util as u, sys;"
    "missing=[m for m in %r if u.find_spec(m) is None];"
    "print('missing:' + ','.join(missing));"
    "sys.exit(1 if missing else 0)"
)


def find_hermes_python(hermes_home: Path) -> str:
    """Hermes **运行时**（agent 进程）的 python —— 插件依赖要装这里。

    路径随 Hermes 版本变：2026-09 起是 `hermes-agent/.venv`（uv 建、无 pip），
    更早是 `hermes-agent/venv`；再兜底 glob 扫一层，避免升级后探测失效。
    """
    candidates = [
        hermes_home / "hermes-agent" / ".venv" / "Scripts" / "python.exe",
        hermes_home / "hermes-agent" / ".venv" / "bin" / "python",
        hermes_home / "hermes-agent" / "venv" / "Scripts" / "python.exe",
        hermes_home / "hermes-agent" / "venv" / "bin" / "python",
    ]
    candidates += sorted(hermes_home.glob("hermes-agent/*/Scripts/python.exe"))
    candidates += sorted(hermes_home.glob("hermes-agent/*/bin/python"))
    for c in candidates:
        if Path(c).exists():
            return str(c)
    return sys.executable


def find_mcp_python(hermes_home: Path) -> str:
    """MCP server 用的 python —— 优先伯仕 venv（那里装了 mcp + chromadb）。

    MCP 服务是**独立子进程**（不在 Hermes 进程内 import），与插件依赖目标环境不同：
    装到 Hermes 运行时的 3 个包不含 mcp，所以 MCP 命令必须指向伯仕 venv。
    """
    for c in [BOSHI_VENV / "Scripts" / "python.exe", BOSHI_VENV / "bin" / "python"]:
        if Path(c).exists():
            return str(c)
    return find_hermes_python(hermes_home)


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


def _run(cmd, **kw):
    """跑子进程，永不抛异常（安装脚本要能一路走完并给出结论）。"""
    try:
        return subprocess.run(cmd, capture_output=True, text=True, **kw)
    except Exception:  # noqa: BLE001
        return None


def check_plugin_deps(hermes_python: str) -> list:
    """返回 Hermes 运行时**缺失**的插件依赖（空列表 = 齐全）。"""
    r = _run([hermes_python, "-c", _DEPS_PROBE % (PLUGIN_DEPS,)])
    if r is None:
        return list(PLUGIN_DEPS)
    for line in ((r.stdout or "") + (r.stderr or "")).splitlines():
        if line.startswith("missing:"):
            return [m for m in line[len("missing:"):].split(",") if m]
    return list(PLUGIN_DEPS)


def install_hermes_deps(hermes_home: Path, *, force: bool = False) -> bool:
    """把伯仕的**进程内依赖**装进 Hermes 运行时 venv（幂等）。

    可在 Hermes 每次升级后单独执行：`python install.py --fix-runtime`
    （升级会换 venv / python 版本，连带卸掉这些包 → 插件静默失忆）。
    返回 True 表示依赖齐全。
    """
    print("[2b/6] 检查 Hermes 运行时依赖（插件进程内 import 用）...")
    hermes_python = find_hermes_python(hermes_home)
    if not Path(hermes_python).exists():
        print("   ⚠️ 未找到 Hermes 运行时 python，跳过（插件将无法召回，请手工确认）")
        return False
    print(f"   目标环境: {hermes_python}")
    # 回退到伯仕 venv = 没找到 Hermes 运行时：那里天然有依赖，不能当“齐全”，否则假绿灯
    try:
        if Path(hermes_python).resolve().is_relative_to(BOSHI_DIR.resolve()):
            print("   ⚠️ 未定位到 Hermes 运行时（回退到伯仕 venv）——无法确认插件依赖")
            print("   请用 --home 指定 HERMES_HOME，或确认 hermes-agent/.venv 是否存在")
            return False
    except Exception:  # noqa: BLE001
        pass

    missing = list(PLUGIN_DEPS) if force else check_plugin_deps(hermes_python)
    if not missing:
        print("   ✅ 依赖齐全，无需安装")
        return True
    print(f"   ℹ️ 缺失: {', '.join(missing)} → 开始安装（首次约几百 MB，稍慢）")

    # 新版 Hermes venv 由 uv 管理、可能没有 pip → 优先 uv；退化 pip；再退化 ensurepip
    installed = False
    uv = shutil.which("uv")
    if uv:
        r = _run([uv, "pip", "install", "--python", hermes_python] + missing)
        installed = bool(r and r.returncode == 0)
        if not installed and r is not None:
            print("   ⚠️ uv 安装失败，改用 pip：" + (r.stderr or "").strip()[:200])
    if not installed:
        r = _run([hermes_python, "-m", "pip", "install"] + missing)
        installed = bool(r and r.returncode == 0)
    if not installed:
        print("   ℹ️ 目标环境无 pip，尝试 ensurepip 引导...")
        _run([hermes_python, "-m", "ensurepip", "--upgrade"])
        r = _run([hermes_python, "-m", "pip", "install"] + missing)
        installed = bool(r and r.returncode == 0)

    left = check_plugin_deps(hermes_python)
    if not left:
        print("   ✅ Hermes 运行时依赖已补齐（插件可直接 import）")
        print("   ⚠️ 需**重启 Hermes（gateway）**才生效：运行中进程不会加载新装模块")
        return True
    print("   ❌ 仍有缺失: " + ", ".join(left))
    print(f'   手工排查可试: uv pip install --python "{hermes_python}" {" ".join(left)}')
    return False


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
    parser.add_argument("--fix-runtime", action="store_true",
                        help="只做一件事：检测并补齐 Hermes 运行时依赖（Hermes 升级后跑这个）")
    parser.add_argument("--force-deps", action="store_true",
                        help="忽略检测结果，强制重装运行时依赖")
    args = parser.parse_args()

    hermes_home = Path(args.home) if args.home else get_hermes_home()

    # 单点自愈入口：Hermes 升级会换 venv/python，连带卸掉插件的进程内依赖
    if args.fix_runtime:
        print(f"🦄 伯仕运行时依赖自愈 | HERMES_HOME = {hermes_home}")
        ok = install_hermes_deps(hermes_home, force=args.force_deps)
        print()
        if ok:
            print("✅ 依赖齐全 —— **重启 Hermes（gateway）后生效**")
            print("   验证：重启后看 L2 写入量（source=hermes_plugin）是否恢复")
        else:
            print("❌ 未补齐，见上方提示（插件会继续静默失忆：不召回、不写入、无报错）")
        return

    mcp_command = find_mcp_python(hermes_home)
    print(f"🦄 伯仕记忆系统安装脚本 | HERMES_HOME = {hermes_home}")
    print(f"   插件依赖目标 = {find_hermes_python(hermes_home)}")
    print(f"   MCP command  = {mcp_command}")

    deploy_code()
    if not args.no_deps:
        install_deps()
        install_hermes_deps(hermes_home, force=args.force_deps)
    if not args.no_model:
        install_model()
    install_plugin(hermes_home)
    configure_config(hermes_home, mcp_command)
    install_skill(hermes_home)

    print()
    print("=" * 52)
    print("✅ 安装完成！重启 Hermes 后生效：")
    print(f"   插件方式: memory.provider = boshi（每轮自动召回/存储）")
    print(f"   MCP 方式 : mcp_servers.boshi（9 个 boshi_* 工具）")
    print()
    print("验证命令:")
    print("   hermes memory status          # 应显示 Provider: boshi")
    print("   hermes mcp test boshi         # 应显示连接成功")
    print("=" * 52)


if __name__ == "__main__":
    main()
