#!/usr/bin/env python3
"""
ChromaDB 修复 v2 — 用 ChromaDB API 读取现有数据，迁移到新 collection
避免直接操作 SQLite 内部表结构
"""
import os
import sys
import shutil
import time
import json
import uuid as uuid_mod

CHROMA_DIR = os.path.expanduser("~/.boshi/chroma_db")
REPAIR_DIR = os.path.expanduser("~/.boshi/chroma_db_v2")
BACKUP_DIR = os.path.expanduser("~/.boshi/chroma_db_pre_repair")

LOCAL_MODEL_PATH = os.path.expanduser(
    "~/.cache/huggingface/hub/models--sentence-transformers--all-MiniLM-L6-v2/"
    "snapshots/c9745ed1d9f207416be6d2e6f8de32d1f16199bf"
)

# 确保有备份
if not os.path.exists(BACKUP_DIR):
    shutil.copytree(CHROMA_DIR, BACKUP_DIR)
    print(f"✅ 已备份到 {BACKUP_DIR}")
else:
    print(f"⚠️ 使用现有备份: {BACKUP_DIR}")

import chromadb
from chromadb.utils.embedding_functions import SentenceTransformerEmbeddingFunction

# 1. 连接旧库，读取所有数据
print("=== 读取旧库 ===")
client_old = chromadb.PersistentClient(path=CHROMA_DIR)
ef = SentenceTransformerEmbeddingFunction(model_name=LOCAL_MODEL_PATH)
try:
    col_old = client_old.get_collection("boshi_memory", embedding_function=ef)
    total = col_old.count()
    print(f"旧库总数: {total}")
except Exception as e:
    print(f"❌ 打开旧库失败: {e}")
    sys.exit(1)

# 分批读取所有数据
BATCH_READ = 500
all_docs = []
all_metadatas = []
all_ids = []

for offset in range(0, total, BATCH_READ):
    limit = min(BATCH_READ, total - offset)
    try:
        result = col_old.get(limit=limit, offset=offset)
        if result["ids"]:
            for i in range(len(result["ids"])):
                doc = result["documents"][i] if result.get("documents") and result["documents"][i] else ""
                meta = result["metadatas"][i] if result.get("metadatas") and result["metadatas"][i] else {}
                
                # 清理损坏的 metadata：去掉所有 float 值（它们是被错误写入的时间戳）
                clean_meta = {}
                for k, v in meta.items():
                    if isinstance(v, (int, float)):
                        continue  # 跳过所有数值 — 都是损坏的
                    if k in ("chroma:document",):
                        continue  # 内部字段
                    if v is not None and v != "":
                        clean_meta[k] = v
                
                # 如果 metadata 全空了，加一个占位 type 保持可检索
                if not clean_meta:
                    # 从文档内容推断类型
                    if doc:
                        if doc.startswith("话题:"):
                            clean_meta = {"type": "topic", "note": "repair"}
                        elif "→" in doc:
                            clean_meta = {"type": "conversation", "note": "repair"}
                        else:
                            clean_meta = {"type": "text", "note": "repair"}
                    else:
                        clean_meta = {"type": "unknown", "note": "repair"}
                
                all_docs.append(doc)
                all_metadatas.append(clean_meta)
                all_ids.append(result["ids"][i])
    except Exception as e:
        print(f"  ⚠️ batch offset={offset} 读取失败: {e}")
    
    if (offset + limit) % 1000 == 0:
        print(f"  已读取: {offset + limit}/{total}")

print(f"\n读取完成: {len(all_docs)} 条")
clean_count = sum(1 for m in all_metadatas if m)
print(f"有干净 metadata: {clean_count}")

# 2. 创建新库并写入
print("\n=== 创建新库 ===")
if os.path.exists(REPAIR_DIR):
    shutil.rmtree(REPAIR_DIR)
os.makedirs(REPAIR_DIR)

client_new = chromadb.PersistentClient(path=REPAIR_DIR)
col_new = client_new.create_collection("boshi_memory", embedding_function=ef)

# 分批写入新库
BATCH_WRITE = 30
BATCH_SLEEP = 1

total_written = 0
errors = []

for i in range(0, len(all_docs), BATCH_WRITE):
    batch_end = min(i + BATCH_WRITE, len(all_docs))
    batch_docs = all_docs[i:batch_end]
    batch_metas = all_metadatas[i:batch_end]
    batch_ids = all_ids[i:batch_end]
    
    # 生成新的 UUID 避免冲突
    new_ids = [str(uuid_mod.uuid4()) for _ in batch_ids]
    
    try:
        col_new.add(
            documents=batch_docs,
            metadatas=batch_metas,
            ids=new_ids
        )
        total_written += len(batch_docs)
    except Exception as e:
        errors.append(f"batch {i}: {e}")
        # 尝试逐条写入
        for j in range(len(batch_docs)):
            try:
                col_new.add(
                    documents=[batch_docs[j]],
                    metadatas=[batch_metas[j]] if batch_metas[j] else [{}],
                    ids=[str(uuid_mod.uuid4())]
                )
                total_written += 1
            except Exception as e2:
                errors.append(f"single {i+j}: {e2}")
    
    if (i + BATCH_WRITE) % 300 == 0:
        print(f"  已写入: {total_written}/{len(all_docs)}")
    
    time.sleep(BATCH_SLEEP)

print(f"\n写入完成: {total_written}/{len(all_docs)}")
if errors:
    print(f"⚠️ 错误数: {len(errors)}")
    for e in errors[:5]:
        print(f"  {e}")

# 3. 验证
print("\n=== 验证新库 ===")
try:
    test = col_new.query(query_texts=["记忆系统 工作日志 架构"], n_results=5)
    print(f"查询结果: {len(test['ids'][0])} 条")
    for i in range(len(test['ids'][0])):
        doc = test['documents'][0][i] or ""
        meta = test['metadatas'][0][i] or {}
        print(f"  [{i}] doc={repr(doc[:80])}")
        print(f"      meta={dict(list(meta.items())[:4])}")
except Exception as e:
    print(f"❌ 验证失败: {e}")

# 4. 统计最终状态
print(f"\n=== 统计 ===")
print(f"新库总数: {col_new.count()}")
print(f"旧库备份: {BACKUP_DIR}")
print(f"新库位置: {REPAIR_DIR}")
print(f"\n确认后执行切换:")
print(f"  mv {CHROMA_DIR} {CHROMA_DIR}.old")
print(f"  mv {REPAIR_DIR} {CHROMA_DIR}")