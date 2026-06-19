#!/usr/bin/env python3
"""
清理 ChromaDB 旧库备份 + hot.json 残留 + 优化搜索索引
"""
import os
import shutil
import sqlite3
import time
import json

# ========== 1. 清理旧备份目录 ==========
to_clean = [
    "chroma_db_pre_repair",
    "chroma_db_repair_bak",
    "chroma_db_repair_bak2",
    "chroma_db_v2",
]

BOSHI_HOME = os.path.expanduser("~/.boshi")
for d in to_clean:
    path = os.path.join(BOSHI_HOME, d)
    if os.path.exists(path):
        try:
            shutil.rmtree(path)
            print(f"✅ 已删除 {d}")
        except Exception as e:
            print(f"⚠️ 无法删除 {d}: {e}")
    else:
        print(f"  {d} 不存在，跳过")

# ========== 2. hot.json 冻结 ==========
memory_dir = os.path.join(BOSHI_HOME, "memory")
hot_files = [
    ("hot.json", "hot.json.migrated"),
    ("hot.json.bak", None),
]

for src, dst in hot_files:
    src_path = os.path.join(memory_dir, src)
    if os.path.exists(src_path):
        if dst:
            dst_path = os.path.join(memory_dir, dst)
            if not os.path.exists(dst_path):
                os.rename(src_path, dst_path)
                print(f"✅ 已冻结: {src} → {dst}")
            else:
                os.remove(src_path)
                print(f"✅ {dst} 已存在，删除 {src}")
        else:
            os.remove(src_path)
            print(f"✅ 已删除: {src}")

# ========== 3. 索引优化：验证新库能正常搜索 ==========
print("\n=== 索引优化 ===")
import chromadb
from chromadb.utils.embedding_functions import SentenceTransformerEmbeddingFunction

CHROMA_DIR = os.path.expanduser("~/.boshi/chroma_db")
MODEL_PATH = os.path.expanduser(
    "~/.cache/huggingface/hub/models--sentence-transformers--all-MiniLM-L6-v2/"
    "snapshots/c9745ed1d9f207416be6d2e6f8de32d1f16199bf"
)

client = chromadb.PersistentClient(path=CHROMA_DIR)
ef = SentenceTransformerEmbeddingFunction(model_name=MODEL_PATH)
col = client.get_or_create_collection("boshi_memory", embedding_function=ef)

total = col.count()
print(f"新库总数: {total}")

# 测试多组搜索词
test_queries = [
    "记忆系统 架构 技术文档",
    "工作日志 项目",
    "Hermes 配置 插件",
    "ChromaDB 恢复 修复",
    "乐之 偏好 用户",
]

for q in test_queries:
    try:
        result = col.query(query_texts=[q], n_results=3)
        hits = len(result['ids'][0])
        if hits > 0:
            scores = result['distances'][0] if result.get('distances') else [0]
            print(f"  ✅ [{q}] → {hits}条, 最佳匹配度={scores[0]:.4f}")
        else:
            print(f"  ⚠️ [{q}] → 0条")
    except Exception as e:
        print(f"  ❌ [{q}] → {e}")

# 4. 检查有多少干净的 conversation_turn 和 type=work_log
print("\n=== 有效数据统计 ===")
# 通过 query 来大致统计
for q in ["conversation_turn conversation", "work_log 工作日志", "fact 事实", "project 项目"]:
    result = col.query(query_texts=[q], n_results=20)
    docs_with_meta = 0
    for i in range(len(result['ids'][0])):
        meta = result['metadatas'][0][i] if result.get('metadatas') else {}
        if meta and len(meta.keys()) > 1:
            docs_with_meta += 1
    print(f"  [{q}] 总{len(result['ids'][0])}条, 有meta信息{docs_with_meta}条")

print("\n✅ 清理和优化完成")