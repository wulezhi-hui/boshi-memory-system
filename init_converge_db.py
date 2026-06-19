"""初始化汇聚数据库"""
import sqlite3, os

db_path = os.path.expanduser("~/.openclaw/shared/converge.db")
os.makedirs(os.path.dirname(db_path), exist_ok=True)

conn = sqlite3.connect(db_path)
conn.executescript("""
    CREATE TABLE IF NOT EXISTS conversations (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        channel TEXT NOT NULL DEFAULT 'unknown',
        role TEXT NOT NULL,
        content TEXT NOT NULL,
        timestamp TEXT NOT NULL DEFAULT (datetime('now','localtime'))
    );
    CREATE INDEX IF NOT EXISTS idx_conv_time ON conversations(timestamp);
""")
conn.execute("INSERT INTO conversations (channel, role, content) VALUES (?, ?, ?)",
             ('system', 'system', '汇聚数据库已创建'))
conn.commit()

count = conn.execute("SELECT COUNT(*) FROM conversations").fetchone()[0]
print(f"OK: {count} records")
conn.close()
