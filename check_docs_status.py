#!/usr/bin/env python3
"""检查 ChromaDB 表结构及数据状态"""
import sqlite3, os

db = os.path.expanduser('~/.boshi/chroma_db/chroma.sqlite3')
c = sqlite3.connect(db).cursor()

# 0. 表结构
print("=== embeddings 表结构 ===")
cols = c.execute("PRAGMA table_info(embeddings)").fetchall()
for col_id, name, dtype, notnull, default, pk in cols:
    print(f"  {col_id}: {name} ({dtype}) pk={pk}")

print()
print("=== embedding_metadata 表结构 ===")
cols = c.execute("PRAGMA table_info(embedding_metadata)").fetchall()
for col_id, name, dtype, notnull, default, pk in cols:
    print(f"  {col_id}: {name} ({dtype}) pk={pk}")

# 1. type 分布
print("\n=== type 分布 ===")
types = c.execute("SELECT string_value, COUNT(*) FROM embedding_metadata WHERE key='type' GROUP BY string_value ORDER BY COUNT(*) DESC").fetchall()
for t, cnt in types:
    print(f"  {t}: {cnt}")

# 2. source 分布
print("\n=== source 分布 ===")
sources = c.execute("SELECT string_value, COUNT(*) FROM embedding_metadata WHERE key='source' GROUP BY string_value ORDER BY COUNT(*) DESC").fetchall()
for s, cnt in sources:
    print(f"  {s}: {cnt}")

# 3. 记录总数和 embedding 情况
total = c.execute("SELECT COUNT(*) FROM embeddings").fetchone()[0]
print(f"\nembeddings 总数: {total}")

# 4. 看有没有 work_log 类型的记录
wl = c.execute("SELECT COUNT(*) FROM embedding_metadata WHERE key='type' AND string_value='work_log'").fetchone()[0]
print(f"work_log: {wl}")

# 5. 看 project_log
pl = c.execute("SELECT COUNT(*) FROM embedding_metadata WHERE key='type' AND string_value='project_log'").fetchone()[0]
print(f"project_log: {pl}")

# 6. 看 entity_extracted
ee = c.execute("SELECT COUNT(*) FROM embedding_metadata WHERE key='type' AND string_value='entity_extracted'").fetchone()[0]
print(f"entity_extracted: {ee}")

# 7. 看 relation 
rel = c.execute("SELECT COUNT(*) FROM embedding_metadata WHERE key='type' AND string_value='relation'").fetchone()[0]
print(f"relation: {rel}")

c.close()