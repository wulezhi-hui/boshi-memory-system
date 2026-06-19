#!/usr/bin/env python3
"""
修复 ChromaDB 索引损坏 — 从 FTS + metadata 表中恢复数据到新 collection
"""
import os
import sys
import sqlite3
import shutil
import uuid as uuid_mod
from datetime import datetime, timezone

CHROMA_DIR = os.path.expanduser("~/.boshi/chroma_db")
DB_PATH = os.path.join(CHROMA_DIR, "chroma.sqlite3")

# 1. 先备份
BACKUP_DIR = os.path.expanduser("~/.boshi/chroma_db_repair_bak")
if not os.path.exists(BACKUP_DIR):
    shutil.copytree(CHROMA_DIR, BACKUP_DIR)
    print(f"✅ 已备份到 {BACKUP_DIR}")
else:
    print("⚠️ 备份已存在，跳过")

# 2. 读取所有 FTS 文档内容
conn = sqlite3.connect(DB_PATH)
c = conn.cursor()

# 获取 rowid -> document 内容
fts_rows = c.execute("SELECT rowid, c0 FROM embedding_fulltext_search_content").fetchall()
print(f"FTS 文档数: {len(fts_rows)}")

# 获取每个 id 的 metadata
# 先查所有 embedding id
embed_ids = [r[0] for r in c.execute("SELECT id FROM embeddings ORDER BY id").fetchall()]
print(f"嵌入记录数: {len(embed_ids)}")

# 获取所有 metadata
all_meta = {}
meta_rows = c.execute("SELECT id, key, string_value, float_value, int_value FROM embedding_metadata").fetchall()
for mid, key, sv, fv, iv in meta_rows:
    if mid not in all_meta:
        all_meta[mid] = {}
    # 优先取 string_value，再取 float/int
    if sv is not None:
        all_meta[mid][key] = sv
    elif iv is not None:
        all_meta[mid][key] = iv
    elif fv is not None:
        all_meta[mid][key] = fv

print(f"metadata 记录数: {len(meta_rows)}")
print(f"有 metadata 的 id 数: {len(all_meta)}")

# 3. 获取 embeddings 向量
print("\n导出向量...")
embeddings_data = {}
emb_rows = c.execute("SELECT id, embedding FROM embeddings").fetchall()
for eid, emb in emb_rows:
    embeddings_data[eid] = emb
print(f"向量数: {len(embeddings_data)}")

conn.close()

# 4. 用 ChromaDB 重新创建并写入
import chromadb
from chromadb.utils.embedding_functions import SentenceTransformerEmbeddingFunction

LOCAL_MODEL_PATH = os.path.expanduser(
    "~/.cache/huggingface/hub/models--sentence-transformers--all-MiniLM-L6-v2/"
    "snapshots/c9745ed1d9f207416be6d2e6f8de32d1f16199bf"
)

# 清空旧的 chroma_db 目录
REPAIR_DIR = os.path.expanduser("~/.boshi/chroma_db_repair")
if os.path.exists(REPAIR_DIR):
    shutil.rmtree(REPAIR_DIR)
os.makedirs(REPAIR_DIR, exist_ok=True)

client = chromadb.PersistentClient(path=REPAIR_DIR)
ef = SentenceTransformerEmbeddingFunction(model_name=LOCAL_MODEL_PATH)
col = client.create_collection("boshi_memory", embedding_function=ef)

# 分批写入
BATCH_SIZE = 30
BATCH_INTERVAL = 2

# 构建倒排索引：rowid -> document
fts_map = {rowid: doc for rowid, doc in fts_rows}

# 合并数据
recovered = 0
failed = 0
total = len(embed_ids)

# 分批处理
batches = []
current_batch_docs = []
current_batch_metas = []
current_batch_ids = []
current_batch_embeddings = []

for i, eid in enumerate(embed_ids):
    doc = fts_map.get(eid, "")
    if not doc:
        doc = ""
    
    meta = all_meta.get(eid, {})
    # 清理 chroma:document 内部字段
    clean_meta = {}
    for k, v in meta.items():
        if k == 'chroma:document':
            continue
        if isinstance(v, float) or isinstance(v, int):
            continue  # 跳过损坏的 float 值
        clean_meta[k] = v
    
    mem_id = str(uuid_mod.uuid4())
    
    current_batch_docs.append(doc)
    current_batch_metas.append(clean_meta)
    current_batch_ids.append(mem_id)
    current_batch_embeddings.append(embeddings_data.get(eid))
    
    if len(current_batch_docs) >= BATCH_SIZE or i == total - 1:
        try:
            col.add(
                documents=current_batch_docs,
                metadatas=current_batch_metas,
                ids=current_batch_ids,
                embeddings=current_batch_embeddings
            )
            recovered += len(current_batch_docs)
            if (recovered % 200) == 0:
                print(f"  已恢复 {recovered}/{total}")
        except Exception as ex:
            print(f"  ❌ 批次失败: {ex}")
            failed += len(current_batch_docs)
        
        current_batch_docs = []
        current_batch_metas = []
        current_batch_ids = []
        current_batch_embeddings = []
        
        import time
        time.sleep(BATCH_INTERVAL)

print(f"\n✅ 恢复完成: {recovered} 成功, {failed} 失败, 共 {total}")

# 5. 验证
print("\n=== 验证新库 ===")
try:
    result = col.query(query_texts=["记忆系统"], n_results=3)
    for i in range(len(result['ids'][0])):
        doc = result['documents'][0][i]
        print(f"  [{i}] doc={repr(doc[:80]) if doc else 'None'}")
        meta = result['metadatas'][0][i]
        print(f"      meta keys={list(meta.keys())[:5]}")
except Exception as ex:
    print(f"❌ 验证失败: {ex}")

print(f"\n修复后的 ChromaDB: {REPAIR_DIR}")
print("确认无误后，执行以下命令切换：")
print(f"  mv {CHROMA_DIR} {CHROMA_DIR}.bak.old")
print(f"  mv {REPAIR_DIR} {CHROMA_DIR}")