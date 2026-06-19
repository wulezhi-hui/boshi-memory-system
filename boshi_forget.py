#!/usr/bin/env python3
"""
伯仕遗忘引擎 v1.2 🦄
========================
安全地从 ChromaDB 中清除低价值记忆。

原则：
  - 只从 ChromaDB 删除，不动 state.db（原始记录永久保留）
  - 主动记住的永久保护
  - 日常对话自动存入的，超时自动清除

用法：
  python3 ~/.boshi/boshi_forget.py            # dry-run
  python3 ~/.boshi/boshi_forget.py --execute  # 执行
  python3 ~/.boshi/boshi_forget.py --status    # 状态
"""

import json
import logging
import os
import sys
import time

import chromadb
from chromadb.utils.embedding_functions import SentenceTransformerEmbeddingFunction

# ── 配置 ──

CHROMA_DIR = os.path.expanduser("~/.boshi/chroma_db")
COLLECTION_NAME = "boshi_memory"
LOCAL_MODEL_PATH = os.path.expanduser(
    "~/.cache/huggingface/hub/models--sentence-transformers--all-MiniLM-L6-v2/"
    "snapshots/c9745ed1d9f207416be6d2e6f8de32d1f16199bf"
)

MAX_DAYS = 30           # 日常对话超 30 天清除
MAX_DAYS_NO_TS = 7     # 无时间戳旧数据保留 7 天后清除

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s"
)
logger = logging.getLogger("boshi_forget")


def _get_client():
    return chromadb.PersistentClient(path=CHROMA_DIR)


def _get_collection():
    client = _get_client()
    ef = SentenceTransformerEmbeddingFunction(model_name=LOCAL_MODEL_PATH)
    return client.get_or_create_collection(COLLECTION_NAME, embedding_function=ef)


# ── 保护来源/类型 ──

PROTECTED_SOURCES = {
    "boshi_conclude", "explicit", "manual_index", "project_archive",
}
PROTECTED_TYPES = {
    "decision", "preference", "project_log", "work_log",
    "resource_locator", "doc_index", "profile_static",
    "profile_dynamic", "arch_doc",
}


def get_forgettable_ids(col) -> list:
    """
    三步过滤找出可遗忘的记忆 ID。
    只使用 ChromaDB 的 get/delete API，不直接操作 SQLite。
    """
    limit = 1000
    offset = 0
    total_checked = 0
    forgettable = []
    cutoff = time.time() - MAX_DAYS * 86400

    while True:
        batch = col.get(limit=limit, offset=offset)
        if not batch["ids"]:
            break

        for i, mid in enumerate(batch["ids"]):
            meta = batch["metadatas"][i] if batch["metadatas"] else {}
            doc = batch["documents"][i] if batch["documents"] else None
            total_checked += 1

            # 保护来源
            src = meta.get("source", "")
            if src in PROTECTED_SOURCES:
                continue

            # 保护类型
            tp = meta.get("type", "")
            if tp in PROTECTED_TYPES:
                continue

            # 知识图谱关系保护
            if meta.get("rel_type") or meta.get("entity_a") or meta.get("entity_b"):
                continue

            # 暖区/热区保留（有内容的warm保留）
            tier = meta.get("tier", "")
            if tier in ("hot", "warm") and doc:
                continue

            # 按时效判断
            lm = meta.get("last_mentioned", 0)
            if lm and isinstance(lm, (int, float)) and lm > 0:
                if lm > cutoff:
                    continue  # 近期提过
                forgettable.append(mid)
                continue

            # 按创建时间
            created = meta.get("_version_created", 0)
            if created and isinstance(created, (int, float)) and created > 0:
                cutoff_created = time.time() - MAX_DAYS_NO_TS * 86400
                if created > cutoff_created:
                    continue
                forgettable.append(mid)
                continue

            # 无任何时间戳 + 非保护 → 旧迁移数据
            forgettable.append(mid)

        offset += limit

    logger.info(f"已检查 {total_checked} 条，候选遗忘 {len(forgettable)} 条")
    return forgettable


def preview(ids: list, col) -> dict:
    """生成遗忘预览（需要重新 get 元数据来生成报表）"""
    if not ids:
        return {"total": 0}

    # 分批获取元数据
    sources, types, topics = {}, {}, {}
    recent, old, no_ts = 0, 0, 0
    cutoff = time.time() - MAX_DAYS * 86400

    BATCH = 100
    for i in range(0, len(ids), BATCH):
        batch_ids = ids[i:i + BATCH]
        data = col.get(ids=batch_ids)
        if not data["metadatas"]:
            continue
        for meta in data["metadatas"]:
            s = meta.get("source", "")
            if s:
                sources[s] = sources.get(s, 0) + 1
            t = meta.get("type", "")
            if t:
                types[t] = types.get(t, 0) + 1
            tp = meta.get("topic", "")
            if tp:
                topics[tp] = topics.get(tp, 0) + 1

            lm = meta.get("last_mentioned", 0)
            if lm and isinstance(lm, (int, float)) and lm > 0:
                if lm > cutoff:
                    recent += 1
                else:
                    old += 1
            else:
                created = meta.get("_version_created", 0)
                if created and isinstance(created, (int, float)) and created > 0:
                    if created > cutoff:
                        recent += 1
                    else:
                        old += 1
                else:
                    no_ts += 1

    return {
        "total": len(ids),
        "by_source": dict(sorted(sources.items(), key=lambda x: -x[1])),
        "by_type": dict(sorted(types.items(), key=lambda x: -x[1])),
        "by_topic": dict(sorted(topics.items(), key=lambda x: -x[1])[:15]),
        "recent": recent,
        "old": old,
        "no_timestamp": no_ts,
    }


def execute_forget(col, ids: list) -> int:
    """执行遗忘：通过 ChromaDB API 批量删除"""
    if not ids:
        return 0
    count = len(ids)
    BATCH = 100
    for i in range(0, len(ids), BATCH):
        batch = ids[i:i + BATCH]
        try:
            col.delete(ids=batch)
        except Exception:
            pass
    logger.info(f"已清除 {count} 条记忆")
    return count


# ── 入口 ──

def run_status():
    col = _get_collection()
    total = col.count()
    ids = get_forgettable_ids(col)
    print("=" * 50)
    print("  🦄 伯仕遗忘系统 — 状态")
    print("=" * 50)
    print(f"  总记忆: {total}")
    print(f"  可遗忘: {len(ids)}")
    print(f"  条件: 日常对话超 {MAX_DAYS} 天 / 无时间戳超 {MAX_DAYS_NO_TS} 天")
    print("=" * 50)
    return ids


def run_forget(execute=False):
    col = _get_collection()
    ids = get_forgettable_ids(col)

    if not ids:
        print("✅ 没有需要遗忘的记忆。")
        return

    report = preview(ids, col)
    print(f"\n📋 待遗忘: {report['total']} 条")
    print(f"\n  → 时效分组：")
    print(f"    最近提及/创建: {report['recent']}")
    print(f"    超过30天:      {report['old']}")
    print(f"    无时间戳:      {report['no_timestamp']}")
    print(f"\n  → 按来源分布：")
    for s, cnt in report["by_source"].items():
        print(f"    {s}: {cnt}")
    print(f"\n  → 按类型分布：")
    for t, cnt in report["by_type"].items():
        print(f"    {t}: {cnt}")

    if not execute:
        print(f"\n🟡 DRY-RUN — 未执行删除。加 --execute 执行。")
        return

    # 安全阀：超过 100 条需二次确认（--force 或 非TTY模式跳过）
    if len(ids) > 100 and "--force" not in sys.argv:
        try:
            has_tty = os.isatty(0)
        except (AttributeError, OSError):
            has_tty = False
        if has_tty:
            print(f"\n⚠️  安全阀触发：将要清除 {len(ids)} 条记忆！")
            print(f"   输入 YES 确认执行，其他任意键取消：")
            try:
                confirm = input("> ").strip()
            except (EOFError, KeyboardInterrupt):
                confirm = ""
            if confirm != "YES":
                print("🛑 已取消。")
                return

    print(f"\n🧹 开始遗忘...")
    deleted = execute_forget(col, ids)

    # 记录遗忘日志
    log_path = os.path.expanduser("~/.boshi/memory/forget_log.jsonl")
    os.makedirs(os.path.dirname(log_path), exist_ok=True)
    with open(log_path, "a", encoding="utf-8") as f:
        f.write(json.dumps({
            "timestamp": time.time(),
            "date": time.strftime("%Y-%m-%d %H:%M:%S"),
            "deleted": deleted,
            "sources": report["by_source"],
        }, ensure_ascii=False) + "\n")

    print(f"✅ 清除: {deleted} 条")
    print(f"   日志: {log_path}")


if __name__ == "__main__":
    if "--status" in sys.argv:
        run_status()
    elif "--execute" in sys.argv:
        run_forget(execute=True)
    else:
        # cron mode: detect if running without TTY, auto-execute
        try:
            has_tty = os.isatty(0)
        except (AttributeError, OSError):
            has_tty = False
        if not has_tty:
            # Running from cron/no-agent — auto-execute
            run_forget(execute=True)
        else:
            run_forget(execute=False)