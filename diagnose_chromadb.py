#!/usr/bin/env python3
"""
ChromaDB 原地修复 — 只修 metadata 的 string_value，不重建向量索引
ChromaDB 的 metadata 存在 embedding_metadata 表中，string_value 被写入了 float_value
文档内容存储在 embedding_fulltext_search_content 中（通过 FTS5 关联）
"""
import os
import sqlite3
import shutil
from datetime import datetime

CHROMA_DIR = os.path.expanduser("~/.boshi/chroma_db")
DB_PATH = os.path.join(CHROMA_DIR, "chroma.sqlite3")

# 1. 备份
BACKUP_DIR = os.path.expanduser("~/.boshi/chroma_db_repair_bak2")
if os.path.exists(BACKUP_DIR):
    shutil.rmtree(BACKUP_DIR)
shutil.copytree(CHROMA_DIR, BACKUP_DIR)
print(f"✅ 已备份到 {BACKUP_DIR}")

conn = sqlite3.connect(DB_PATH)
c = conn.cursor()

# 2. 分析损坏模式
# 大部分 metadata 的 string_value = None, float_value = 时间戳
# 正常记录的 string_value 存了正确的值
# 修复策略：对于已知应该是 string 的 key，如果 string_value 为 None 且 float_value 非 None
# 把这个 float_value 当作错误数据丢弃（因为它是时间戳，不是有效值）

# 先分析每个 key 应该是什么类型
key_info = c.execute("""
    SELECT key,
        COUNT(*) as total,
        SUM(CASE WHEN string_value IS NOT NULL THEN 1 ELSE 0 END) as str_count,
        SUM(CASE WHEN float_value IS NOT NULL THEN 1 ELSE 0 END) as float_count,
        SUM(CASE WHEN int_value IS NOT NULL THEN 1 ELSE 0 END) as int_count
    FROM embedding_metadata
    GROUP BY key
    ORDER BY total DESC
""").fetchall()

string_keys = set()
for key, total, str_cnt, float_cnt, int_cnt in key_info:
    if str_cnt > 0 and float_cnt > 0:
        print(f"  {key}: total={total}, str={str_cnt}, float={float_cnt} → 混合类型，保留现有的 str")
    elif str_cnt > 0 and float_cnt == 0:
        string_keys.add(key)
        print(f"  {key}: total={total}, str={str_cnt}, float={float_cnt} → 纯 string")
    elif str_cnt == 0 and float_cnt > 0:
        # 全部损坏
        print(f"  {key}: total={total}, str={str_cnt}, float={float_cnt} → ⚠️ 完全损坏，无法恢复")
    elif int_cnt > 0:
        print(f"  {key}: total={total}, int={int_cnt} → 整数类型")

# 3. 对于混合类型的 key，看看损坏的比例
print("\n=== 混合 key 的损坏比例 ===")
for key, total, str_cnt, float_cnt, int_cnt in key_info:
    if str_cnt > 0 and float_cnt > 0:
        float_only = c.execute(
            "SELECT COUNT(*) FROM embedding_metadata WHERE key=? AND string_value IS NULL AND float_value IS NOT NULL",
            (key,)
        ).fetchone()[0]
        print(f"  {key}: {float_only}/{total} 损坏 (float_only)")

# 4. 看看有没有 metadata 被完全正确写入的记录（source, user_id, topic 都有值）
print("\n=== 完全正常的记录数 ===")
good = c.execute("""
    SELECT COUNT(DISTINCT m1.id)
    FROM embedding_metadata m1
    JOIN embedding_metadata m2 ON m1.id = m2.id AND m2.key='source' AND m2.string_value IS NOT NULL
    JOIN embedding_metadata m3 ON m1.id = m3.id AND m3.key='type' AND m3.string_value IS NOT NULL
    WHERE m1.key='user_id' AND m1.string_value IS NOT NULL
""").fetchone()[0]
print(f"  有 source+type+user_id 的正常记录: {good}")

# 5. 尝试恢复 — 从 FTS 读取内容，创建新 metadata
# 对于每个 embedding_id，从 embedding_fulltext_search_content 获取文档
# 然后用 chroma:document 的 string_value（如果存在）或构造新的 metadata

# 获取每个 embedding_id 对应的文档内容 (通过 rowid)
print("\n=== 查看 FTS 关联 ===")
fts_meta = c.execute("""
    SELECT em.id, em.string_value, fts.c0
    FROM embedding_metadata em
    JOIN embedding_fulltext_search_content fts ON em.id = fts.rowid
    WHERE em.key = 'chroma:document' AND em.string_value IS NOT NULL
    LIMIT 3
""").fetchall()
for fm in fts_meta:
    print(f"  id={fm[0]}: chroma:doc={repr(fm[1][:60])}, fts={repr(fm[2][:60])}")

# 看看 chroma:document 的 string_value 和 FTS 内容的关系
print("\n=== 确认修复策略 ===")
print("FTS 文档内容完整，但 metadata 的字段名对应的值被时间戳覆盖了")
print("这些值（name, category, heat 等）是从 hot.json 迁移来的话题数据")
print("原始话题内容在 FTS 中有保存")
print("metadata 中的 name/topic 等信息可以从 FTS 文档里重建")
print()
print("修复方案：直接重建 ChromaDB collection")
print("从 FTS + 残存的正常 metadata 中恢复")

conn.close()
print("\n✅ 诊断完成")