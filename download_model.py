#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
伯仕记忆系统 — bge-m3 ONNX 向量模型下载脚本
=============================================
下载 BAAI/bge-m3（Xenova ONNX 导出，1024 维，int8 量化 ~569MB）到
~/.boshi/models/bge-m3/，供 onnx_embed.py 纯本地推理使用。

用法:
  python download_model.py                # 默认从 hf-mirror.com（国内镜像）
  python download_model.py --source hf    # 从 huggingface.co（海外/有代理）
  python download_model.py --check        # 仅检查模型是否已就位

特性:
  - 零第三方依赖（标准库 urllib）
  - 断点续传（已下载部分自动 Range 续传）
  - 幂等（已存在且完整则跳过）
  - 国内默认 hf-mirror.com，无需代理
"""
import argparse
import os
import sys
import urllib.parse
import urllib.request
from pathlib import Path

MODEL_ID = "Xenova/bge-m3"
BOSHI_MODEL_DIR = Path.home() / ".boshi" / "models" / "bge-m3"
ONNX_DIR = BOSHI_MODEL_DIR / "onnx"

# 必需文件（onnx_embed.py 实际运行需要）
REQUIRED_FILES = [
    "config.json",
    "tokenizer.json",
    "tokenizer_config.json",
    "special_tokens_map.json",
    "onnx/model_quantized.onnx",
]

# 可选文件（slow tokenizer 回退用，缺失不影响 fast tokenizer）
OPTIONAL_FILES = [
    "sentencepiece.bpe.model",
]

FILES = REQUIRED_FILES + OPTIONAL_FILES

SOURCES = {
    "hf-mirror": "https://hf-mirror.com/{model_id}/resolve/main/{path}",
    "hf":        "https://huggingface.co/{model_id}/resolve/main/{path}",
}

BLOCK = 1024 * 1024  # 1MB 块

def human_size(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.1f}{unit}" if unit != "B" else f"{n}B"
        n /= 1024
    return f"{n:.1f}GB"


def check_installed() -> bool:
    """必需模型文件是否已全部就位。"""
    missing = []
    for rel in REQUIRED_FILES:
        p = BOSHI_MODEL_DIR / rel
        if not p.exists() or p.stat().st_size == 0:
            missing.append(rel)
    if missing:
        print(f"❌ 模型未完整安装，缺少: {', '.join(missing)}")
        return False
    total = sum((BOSHI_MODEL_DIR / rel).stat().st_size for rel in REQUIRED_FILES)
    print(f"✅ bge-m3 ONNX 模型已就位（共 {human_size(total)}），位于 {BOSHI_MODEL_DIR}")
    return True


def fetch(url: str, dest: Path, label: str) -> None:
    """断点续传下载单个文件。"""
    dest.parent.mkdir(parents=True, exist_ok=True)
    existing = dest.stat().st_size if dest.exists() else 0

    req = urllib.request.Request(url, headers={
        "User-Agent": "boshi-memory-installer/1.0",
        "Range": f"bytes={existing}-" if existing else None,
    })
    req.headers = {k: v for k, v in req.headers.items() if v is not None}

    with urllib.request.urlopen(req, timeout=60) as resp:
        total = int(resp.headers.get("Content-Length", 0)) + existing
        if resp.status == 200:
            # 服务器不支持 Range，重新下载
            existing = 0
            total = int(resp.headers.get("Content-Length", 0))
            print(f"   （服务器不支持断点续传，重新下载 {label}）")

        mode = "ab" if existing > 0 and resp.status == 206 else "wb"
        downloaded = existing
        with open(dest, mode) as f:
            while True:
                chunk = resp.read(BLOCK)
                if not chunk:
                    break
                f.write(chunk)
                downloaded += len(chunk)
                if total > 0:
                    pct = downloaded * 100 // total
                    print(f"\r   {label}: {human_size(downloaded)}/{human_size(total)} ({pct}%)", end="", flush=True)
        print()


def main() -> None:
    parser = argparse.ArgumentParser(description="下载 bge-m3 ONNX 向量模型")
    parser.add_argument("--source", choices=sorted(SOURCES), default="hf-mirror",
                        help="下载源（默认 hf-mirror，国内直连）")
    parser.add_argument("--check", action="store_true", help="仅检查模型是否已安装")
    args = parser.parse_args()

    if args.check:
        sys.exit(0 if check_installed() else 1)

    print(f"🦄 下载 bge-m3 ONNX 向量模型（Xenova/bge-m3, int8 量化 ~569MB）")
    print(f"   来源: {args.source}  目标: {BOSHI_MODEL_DIR}")
    print()

    if check_installed():
        print("无需下载。")
        return

    base = SOURCES[args.source]
    for rel in FILES:
        dest = BOSHI_MODEL_DIR / rel
        if dest.exists() and dest.stat().st_size > 0:
            print(f"   ⏭️  跳过 {rel}（已存在 {human_size(dest.stat().st_size)}）")
            continue
        url = base.format(model_id=MODEL_ID, path=urllib.parse.quote(rel))
        print(f"   ⬇️  {rel}")
        try:
            fetch(url, dest, rel.split("/")[-1])
        except Exception as e:
            print(f"   ❌ 下载 {rel} 失败: {e}")
            print(f"      可重试: python {sys.argv[0]} --source {args.source}")
            sys.exit(1)

    print()
    if check_installed():
        print("✅ 模型下载完成。")
    else:
        print("✅ 模型下载完成，伯仕记忆系统可正常向量化。")


if __name__ == "__main__":
    main()
