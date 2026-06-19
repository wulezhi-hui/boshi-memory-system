#!/usr/bin/env python3
"""
修复 ChromaDB metadata：从 state.db FTS5 搜索匹配的会话，
为占位符记录补充 source/user_id 等信息
"""
import os, sys, sqlite3, json, time, uuid as uuid_mod
import chromadb
from chromadb.utils.embedding_functions import SentenceTransformerEmbeddingFunction

BOSHI_HOME = os.path.expanduser("~/.boshi")
CHROMA_DIR = os.path.join(BOSHI_HOME, "chroma_db")
MODEL_PATH = os.path.expanduser(
    "~/.cache/huggingface/hub/models--sentence-transformers--all-MiniLM-L6-v2/"
    "snapshots/c9745ed1d9f207416be6d2e6f8de32d1f16199bf"
)

print("=== 1. 扫描 ChromaDB 占位符记录 ===")
client = chromadb.PersistentClient(path=CHROMA_DIR)
ef = SentenceTransformerEmbeddingFunction(model_name=MODEL_PATH)
col = client.get_or_create_collection("boshi_memory", embedding_function=ef)

total = col.count()
print(f"总数: {total}")

BATCH = 200
entries = []
for offset in range(0, total, BATCH):
    limit = min(BATCH, total - offset)
    result = col.get(limit=limit, offset=offset)
    if not result["ids"]:
        continue
    for i in range(len(result["ids"])):
        doc = result["documents"][i] if result.get("documents") else ""
        meta = result["metadatas"][i] if result.get("metadatas") else {}
        is_placeholder = len(meta) <= 2 and meta.get("note") == "repair"
        if is_placeholder and doc and len(doc) > 5:
            entries.append((result["ids"][i], doc, meta))
    if len(entries) >= 2000:
        break  # 先批次试2000条

print(f"待修复: {len(entries)} 条")

print("\n=== 2. 连接 state.db ===")
state_path = os.path.expandvars("%LOCALAPPDATA%/hermes/state.db")
conn = sqlite3.connect(state_path)
c = conn.cursor()

# 读 session 的 platform 信息
session_platform = {}
for sid, title, ct, plat in c.execute("SELECT id, title, created_at, platform FROM sessions"):
    session_platform[sid] = plat or "cli"

print("=== 3. FTS5 匹配并更新 ===")
fixed = 0
errors = []

for idx, (mem_id, doc, meta) in enumerate(entries):
    search_term = doc[:40].replace("'", "''")
    try:
        rows = c.execute(
            "SELECT m.session_id, m.role, m.content "
            "FROM messages_fts f "
            "JOIN messages m ON f.rowid = m.rowid "
            "WHERE messages_fts MATCH ? "
            "LIMIT 1",
            (search_term,)
        ).fetchall()
    except Exception as e:
        errors.append(f"FTS error {idx}: {e}")
        continue
    
    if rows:
        session_id, role, content = rows[0]
        platform = session_platform.get(session_id, "unknown")
        
        # 构建新 metadata
        new_meta = {
            "source": platform,
            "user_id": "lezhi",
            "session_id": session_id,
            "type": "conversation" if "→" in doc else "topic",
            "note": "repaired_from_statedb"
        }
        
        try:
            col.update(
                ids=[mem_id],
                metadatas=[new_meta]
            )
            fixed += 1
        except Exception as e:
            errors.append(f"update error {mem_id}: {e}")
    
    if (idx + 1) % 200 == 0:
        print(f"  {idx+1}/{len(entries)} → 已修复 {fixed} 条")

conn.close()
print(f"\n✅ 修复完成: {fixed}/{len(entries)}")
if errors:
    print(f"⚠️ 错误: {len(errors)} 条")
    for e in errors[:5]:
        print(f"  {e}")

print("\n=== 4. 验证 ===")
result = col.query(query_texts=["ChromaDB"], n_results=3)
for i in range(len(result['ids'][0])):
    doc = result['documents'][0][i] if result.get('documents') else ""
    meta = result['metadatas'][0][i] if result.get('metadatas') else {}
    print(f"  doc={repr(doc[:60])} | meta_keys={list(meta.keys())}")