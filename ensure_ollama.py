#!/usr/bin/env python3
"""Ollama 保活脚本 — 检测 Ollama 是否运行，未运行则启动，并确保 qwen3:8b 可用。

用法:
  python3 ensure_ollama.py          # 单次检测+启动
  python3 ensure_ollama.py --watch  # 持续监控（每60秒检测一次）

返回码:
  0 = Ollama 已就绪
  1 = 启动失败
"""

import os
import sys
import time
import json
import urllib.request
import urllib.error
import subprocess
import logging
from pathlib import Path

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [OLLAMA] %(levelname)s %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("ollama-watchdog")

OLLAMA_URL = "http://localhost:11434"
TAGS_URL = f"{OLLAMA_URL}/api/tags"
PULL_URL = f"{OLLAMA_URL}/api/pull"
MODEL = "qwen3:8b"
OLLAMA_BIN = None

# 自动查找 ollama.exe
_CANDIDATE_PATHS = [
    r"C:\Program Files\Ollama\ollama.exe",
    r"C:\Program Files (x86)\Ollama\ollama.exe",
    os.path.expanduser(r"~\AppData\Local\Programs\Ollama\ollama.exe"),
    os.path.expanduser(r"~\AppData\Local\Ollama\ollama.exe"),
]


def _find_ollama() -> str | None:
    """查找 ollama.exe 路径。"""
    # 先试 PATH
    try:
        result = subprocess.run(
            ["where", "ollama"], capture_output=True, text=True, timeout=5
        )
        if result.returncode == 0:
            path = result.stdout.strip().split("\n")[0].strip()
            if path:
                return path
    except Exception:
        pass

    # 试候选路径
    for p in _CANDIDATE_PATHS:
        if os.path.isfile(p):
            return p

    return None


def is_ollama_running() -> bool:
    """检测 Ollama 服务是否在运行。"""
    try:
        req = urllib.request.Request(TAGS_URL, method="GET")
        with urllib.request.urlopen(req, timeout=3) as resp:
            data = json.loads(resp.read().decode())
            return isinstance(data, dict)
    except (urllib.error.URLError, urllib.error.HTTPError, OSError, json.JSONDecodeError):
        return False


def start_ollama() -> bool:
    """启动 Ollama 后台服务。"""
    global OLLAMA_BIN
    if OLLAMA_BIN is None:
        OLLAMA_BIN = _find_ollama()

    if OLLAMA_BIN is None:
        log.error("❌ 找不到 ollama.exe，请先安装 Ollama")
        return False

    log.info("🚀 启动 Ollama: %s", OLLAMA_BIN)
    try:
        # 后台启动，不阻塞
        proc = subprocess.Popen(
            [OLLAMA_BIN, "serve"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=subprocess.CREATE_NO_WINDOW if hasattr(subprocess, "CREATE_NO_WINDOW") else 0,
        )
        log.info("  PID=%d", proc.pid)
        return True
    except Exception as e:
        log.error("❌ 启动失败: %s", e)
        return False


def wait_for_ollama(timeout: int = 30) -> bool:
    """等待 Ollama 服务就绪。"""
    log.info("⏳ 等待 Ollama 就绪（最长 %ds）...", timeout)
    for i in range(timeout):
        if is_ollama_running():
            log.info("✅ Ollama 已就绪（%ds）", i + 1)
            return True
        time.sleep(1)
    log.error("❌ Ollama 超时未就绪")
    return False


def ensure_model(model: str = MODEL, timeout: int = 120) -> bool:
    """确保指定模型已拉取到本地。"""
    log.info("🔍 检查模型 %s ...", model)
    try:
        req = urllib.request.Request(TAGS_URL, method="GET")
        with urllib.request.urlopen(req, timeout=5) as resp:
            data = json.loads(resp.read().decode())
            models = [m["name"] for m in data.get("models", [])]
            # 匹配模型名（可能带 :latest 后缀）
            for m in models:
                if m.split(":")[0] == model.split(":")[0]:
                    log.info("✅ 模型 %s 已存在", m)
                    return True
    except Exception as e:
        log.warning("  查询模型列表失败: %s", e)

    # 模型不存在，拉取
    log.info("📥 拉取模型 %s（最长 %ds）...", model, timeout)
    try:
        payload = json.dumps({"name": model, "stream": False}).encode()
        req = urllib.request.Request(
            PULL_URL,
            data=payload,
            method="POST",
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            result = json.loads(resp.read().decode())
            if result.get("status") == "success":
                log.info("✅ 模型 %s 拉取完成", model)
                return True
            log.warning("  拉取结果: %s", result.get("status", "unknown"))
            return False
    except Exception as e:
        log.error("❌ 拉取模型失败: %s", e)
        return False


def ensure_ollama_ready() -> bool:
    """完整保活流程：检测 → 启动 → 等待 → 拉模型。"""
    if is_ollama_running():
        log.info("✅ Ollama 已在运行")
        return ensure_model()

    if not start_ollama():
        return False

    if not wait_for_ollama():
        return False

    return ensure_model()


def watch_loop(interval: int = 60):
    """持续监控模式。"""
    log.info("🔄 进入监控模式（每 %ds 检测一次）", interval)
    while True:
        try:
            if not is_ollama_running():
                log.warning("⚠️ Ollama 未运行，尝试启动...")
                ensure_ollama_ready()
            time.sleep(interval)
        except KeyboardInterrupt:
            log.info("👋 监控停止")
            break
        except Exception as e:
            log.error("监控异常: %s", e)
            time.sleep(interval)


if __name__ == "__main__":
    if "--watch" in sys.argv:
        watch_loop()
    else:
        ok = ensure_ollama_ready()
        sys.exit(0 if ok else 1)
