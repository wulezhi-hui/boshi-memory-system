"""检查 converge.db 结构和最新内容"""
import sqlite3
import os

DB = os.path.expanduser(r'~/.openclaw/shared/converge.db')
print(f"DB exists: {os.path.exists(DB)}")
print(f"DB size: {os.path.getsize(DB)} bytes")

conn = sqlite3.connect(DB)
c = conn.cursor()
c.execute("SELECT name FROM sqlite_master WHERE type='table'")
tables = [t[0] for t in c.fetchall()]
print(f"Tables: {tables}")

for t in tables:
    c.execute(f"SELECT sql FROM sqlite_master WHERE name='{t}'")
    print(f"\n=== {t} ===")
    print(f"Schema: {c.fetchone()[0]}")
    c.execute(f"SELECT * FROM {t} ORDER BY rowid DESC LIMIT 5")
    cols = [d[0] for d in c.description]
    print(f"Columns: {cols}")
    for r in c.fetchall():
        print(f"  {dict(zip(cols, r))}")

conn.close()
