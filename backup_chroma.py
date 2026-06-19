#!/usr/bin/env python3
"""
chroma_db 增量备份 — 供 Hermes cron 调用。

用法：
    python backup_chroma.py                # 正常模式
    python backup_chroma.py --snapshot     # 快照模式（保留7天轮转）

每次执行将 ~/.boshi/chroma_db 增量同步到 D:\\boshi-safeguard\\data\\chroma_db。
使用 robocopy /MIR 只复制差异文件，秒级完成。
"""

import argparse
import os
import shutil
import subprocess
import sys
from datetime import datetime, timedelta

# ── 路径 ──
SRC = os.path.expanduser("~/.boshi/chroma_db")
DST = os.path.join("D:", os.sep, "boshi-safeguard", "data", "chroma_db")
SNAPSHOT_DIR = os.path.join("D:", os.sep, "boshi-safeguard", "snapshots")
MAX_SNAPSHOTS = 7


def backup_chroma() -> tuple:
    """增量备份 chroma_db，返回 (ok: bool, msg: str)"""
    if not os.path.isdir(SRC):
        return False, f"源路径不存在: {SRC}"

    os.makedirs(DST, exist_ok=True)
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    # robocopy 增量镜像（只复制差异文件）
    # 注意：robocopy 输出编码不是 UTF-8（Windows 系统编码如 GBK）
    result = subprocess.run(
        ["robocopy", SRC, DST,
         "/MIR",           # 镜像（增删同步）
         "/NJH", "/NJS",   # 无标题/摘要
         "/NDL",           # 无目录列表
         "/NP",            # 无进度百分比
         "/R:2", "/W:2"],  # 重试2次，等待2秒
        capture_output=True, text=False, timeout=120,
        # text=False → 返回 bytes，避免编码问题
    )
    stdout = result.stdout.decode("utf-8", errors="replace")[:200]
    stderr = result.stderr.decode("utf-8", errors="replace")[:200]

    rc = result.returncode
    if rc < 8:
        copied = "有文件变更" if rc & 1 else "无变更"
        return True, f"[{now}] ✅ chroma_db 增量备份 (robocopy exit={rc}, {copied})"
    else:
        return False, f"[{now}] ⚠️ robocopy 异常 (exit={rc}): {stderr.strip()[-300:]}"


def take_snapshot() -> str:
    """创建时间戳快照（保留最近 MAX_SNAPSHOTS 天）"""
    today = datetime.now().strftime("%Y-%m-%d")
    snap_path = os.path.join(SNAPSHOT_DIR, today, "chroma_db")

    if os.path.isdir(snap_path):
        return f"📸 今日快照已存在: {today}"

    # 先更新静态备份
    ok, msg = backup_chroma()
    if not ok:
        return f"⚠️ 快照失败: {msg}"

    # 复制静态备份作为快照
    os.makedirs(os.path.dirname(snap_path), exist_ok=True)
    shutil.copytree(DST, snap_path, dirs_exist_ok=True)

    # 清理过期快照（保留最近 MAX_SNAPSHOTS 个）
    if os.path.isdir(SNAPSHOT_DIR):
        snapshots = sorted(
            d for d in os.listdir(SNAPSHOT_DIR)
            if os.path.isdir(os.path.join(SNAPSHOT_DIR, d))
        )
        while len(snapshots) > MAX_SNAPSHOTS:
            old = snapshots.pop(0)
            shutil.rmtree(os.path.join(SNAPSHOT_DIR, old), ignore_errors=True)

    return f"📸 快照创建: {today}（保留最近{MAX_SNAPSHOTS}天）"


def main():
    parser = argparse.ArgumentParser(
        description="chroma_db 增量备份"
    )
    parser.add_argument("--snapshot", action="store_true",
                        help="创建时间戳快照（保留7天）")
    args = parser.parse_args()

    if args.snapshot:
        print(take_snapshot())
    else:
        ok, msg = backup_chroma()
        print(msg)
        sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
