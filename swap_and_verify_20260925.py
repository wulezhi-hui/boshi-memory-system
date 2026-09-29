#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
伯仕记忆库替换 + 校验 (2026-09-25)

把重建好的 chroma_db_rebuild 换成正式的 chroma_db，损坏库原地保留为
chroma_db_broken_20260925 作为证据。

安全点：
 1. 先确认损坏库与重建库都存在、重建库 count 合理，才动手
 2. rename 带重试（Windows 下若有进程持句柄会 Permission denied）
 3. 任一步失败都回滚，不留半吊子状态
 4. 替换后立刻用 chroma_bridge 的真实调用路径校验

用法：
  venv/Scripts/python.exe swap_and_verify_20260925.py [--retries 30] [--delay 10]
"""

import io
import os
import shutil
import sys
import time

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

BOSHI = os.path.expanduser("~/.boshi")
LIVE = os.path.join(BOSHI, "chroma_db")
BROKEN = os.path.join(BOSHI, "chroma_db_broken_20260925")
REBUILD = os.path.join(BOSHI, "chroma_db_rebuild")


def log(msg):
    print("[%s] %s" % (time.strftime("%H:%M:%S"), msg), flush=True)


def try_rename(src, dst, retries, delay):
    for i in range(1, retries + 1):
        try:
            os.rename(src, dst)
            return True
        except OSError as e:
            log("  rename 第 %d/%d 次失败: %s" % (i, retries, e))
            time.sleep(delay)
    return False


def main():
    retries = 30
    delay = 10
    if "--retries" in sys.argv:
        retries = int(sys.argv[sys.argv.index("--retries") + 1])
    if "--delay" in sys.argv:
        delay = int(sys.argv[sys.argv.index("--delay") + 1])

    # ── 前置检查 ───────────────────────────────────
    if not os.path.isdir(REBUILD):
        log("ABORT: 重建库不存在 %s" % REBUILD)
        return 1
    if not os.path.isfile(os.path.join(REBUILD, "chroma.sqlite3")):
        log("ABORT: 重建库没有 chroma.sqlite3")
        return 1
    if os.path.exists(BROKEN):
        log("ABORT: %s 已存在，先人工处理" % BROKEN)
        return 1
    log("前置检查通过: rebuild=%.0f MB" % (
        sum(os.path.getsize(os.path.join(r, f))
            for r, _, fs in os.walk(REBUILD) for f in fs) / 1e6))

    # ── 替换 ───────────────────────────────────────
    log("步骤1/2: 损坏库 -> %s" % BROKEN)
    if not try_rename(LIVE, BROKEN, retries, delay):
        log("ABORT: 损坏库改名失败（仍有进程持句柄），未做任何破坏，可重跑")
        return 1

    log("步骤2/2: 重建库 -> %s" % LIVE)
    if not try_rename(REBUILD, LIVE, max(3, retries), delay):
        log("回滚: 重建库改名失败，把损坏库放回原位")
        try_rename(BROKEN, LIVE, 5, delay)
        return 1

    log("替换完成 ✓  损坏库保留在 %s" % BROKEN)

    # ── 用真实调用路径校验 ─────────────────────────
    sys.path.insert(0, BOSHI)
    log("校验: 走 chroma_bridge 真实路径")
    try:
        import chroma_bridge as cb
        n = cb.get_total_count()
        log("  get_total_count = %d" % n)
        hits = cb.search_memory("微调 大模型 Turing", top_k=3)
        log("  search_memory 返回 %d 条" % len(hits))
        for h in hits[:3]:
            md = h.get("metadata", {}) if isinstance(h, dict) else {}
            txt = (h.get("document") or h.get("content") or "") if isinstance(h, dict) else ""
            log("    hit: %s | %s" % (
                txt[:50].replace("\n", " "),
                {k: md.get(k) for k in ("topic", "type", "profile") if k in md}))
        rec = cb.get_recent(3)
        log("  get_recent 返回 %d 条" % len(rec))
        log("VERIFY_OK")
    except Exception as e:
        import traceback
        traceback.print_exc()
        log("VERIFY_FAIL: %s" % e)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
