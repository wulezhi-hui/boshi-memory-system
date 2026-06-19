"""测试汇聚数据库写入"""
import sqlite3, os

db = os.path.expanduser("~/.openclaw/shared/converge.db")
conn = sqlite3.connect(db)
conn.execute(
    "INSERT INTO conversations (channel, role, content) VALUES (?, ?, ?)",
    ("weixin", "assistant", "伯仕自动写入汇聚数据库 ✅")
)
conn.commit()
count = conn.execute("SELECT COUNT(*) FROM conversations").fetchone()[0]
print(f"写入成功，当前共 {count} 条记录")

rows = conn.execute("SELECT channel, role, content, timestamp FROM conversations ORDER BY id DESC LIMIT 3").fetchall()
for r in reversed(rows):
    print(f"  [{r[0]}] {r[1]}: {r[2][:50]} @ {r[3]}")
conn.close()
