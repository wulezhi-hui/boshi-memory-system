#!/usr/bin/env python3
"""
从 state.db 补写 ChromaDB 中 metadata 丢失的记录
策略：对 ChromaDB 中 doc 有内容但 metadata 是占位符的记录，
从 state.db 中找到匹配的 session 补充 source/user_id/topic 等 metadata
"""
import os
import sys
import sqlite3
import json
import time
import uuid as uuid_mod
import chromadb
from chromadb.utils.embedding_functions import SentenceTransformerEmbeddingFunction

BOSHI_HOME = os.path.expanduser("~/.boshi")
CHROMA_DIR = os.path.join(BOSHI_HOME, "chroma_db")
MODEL_PATH = os.path.expanduser(
    "~/.cache/huggingface/hub/models--sentence-transformers--all-MiniLM-L6-v2/"
    "snapshots/c9745ed1d9f207416be6d2e6f8de32d1f16199bf"
)

# 1. 连接 ChromaDB
print("=== Phase 1: 分析 ChromaDB 中需要补写的记录 ===")
client = chromadb.PersistentClient(path=CHROMA_DIR)
ef = SentenceTransformerEmbeddingFunction(model_name=MODEL_PATH)
col = client.get_or_create_collection("boshi_memory", embedding_function=ef)

total = col.count()
print(f"ChromaDB 总数: {total}")

# 分批读取所有记录，找出需要补写的
BATCH_SIZE = 200
entries_to_fix = []  # (id, doc, current_meta)

for offset in range(0, total, BATCH_SIZE):
    limit = min(BATCH_SIZE, total - offset)
    result = col.get(limit=limit, offset=offset)
    if not result["ids"]:
        continue
    for i in range(len(result["ids"])):
        doc = result["documents"][i] if result.get("documents") else None
        meta = result["metadatas"][i] if result.get("metadatas") else {}
        mem_id = result["ids"][i]
        
        # 检查 metadata 是否只有占位符（只有 type 和 note）
        is_placeholder = len(meta) <= 2 and meta.get("note") == "repair"
        has_doc = doc and len(doc) > 5
        
        if is_placeholder and has_doc:
            entries_to_fix.append((mem_id, doc, meta))
    
    if len(entries_to_fix) % 500 == 0:
        print(f"  扫描中... 已发现 {len(entries_to_fix)} 条待补写")

print(f"需要补写 metadata: {len(entries_to_fix)} 条")

# 如果不需要补写，退出
if not entries_to_fix:
    print("✅ 没有需要补写的记录")
    sys.exit(0)

# 2. 读取 state.db 建立索引
print("\n=== Phase 2: 建立 state.db 倒排索引 ===")
state_db_path = os.path.expandvars("%LOCALAPPDATA%/hermes/state.db")
conn = sqlite3.connect(state_db_path)
c = conn.cursor()

# 获取所有 session 的信息
sessions = c.execute("SELECT id, title, created_at, platform FROM sessions ORDER BY created_at DESC").fetchall()
print(f"state.db 会话数: {len(sessions)}")

# 建立消息全文索引 (FTS5)
# 对于每个 doc 内容，在 state.db 中找最匹配的会话
# 策略：用 doc 的前 50 个字符作为关键词在 messages 表中搜索
conn2 = sqlite3.connect(state_db_path)
c2 = conn2.cursor()

# 建立 content_hash -> session_id 的映射
# 用 FTS5 搜索
matches_found = 0
for idx, (mem_id, doc, meta) in enumerate(entries_to_fix):
    if idx % 500 == 0:
        print(f"  匹配中... {idx}/{len(entries_to_fix)}")
    
    # 用 doc 内容搜索 state.db
    # 取 doc 前30个字符作为关键词搜索
    search_term = doc[:40].replace("'", "''")
    
    try:
        rows = c2.execute(
            "SELECT m.session_id, m.role, m.content, s.platform "
            "FROM messages_fts f "
            "JOIN messages m ON f.rowid = m.rowid "
            "JOIN sessions s ON m.session_id = s.id "
            "WHERE messages_fts MATCH ? "
            "ORDER BY rank "
            "LIMIT 1",
            (search_term,)
        ).fetchall()
        
        if rows:
            session_id, role, content, platform = rows[0]
            matches_found += 1
    except Exception:
        pass

print(f"FTS 匹配成功: {matches_found}/{len(entries_to_fix)}")

conn2.close()
conn1 = conn
conn1.close()