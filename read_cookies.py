import sqlite3

db_path = r"C:\Users\Administrator\.boshi\tmp_cookies.db"
conn = sqlite3.connect(db_path)
c = conn.cursor()

# 查表结构
tables = c.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
print(f"Tables: {[t[0] for t in tables]}")

# 查头条cookie
for tbl in [t[0] for t in tables]:
    cols = [col[1] for col in c.execute(f"PRAGMA table_info({tbl})").fetchall()]
    
    if 'host_key' in cols:
        rows = c.execute(
            f"SELECT host_key, name, value FROM {tbl} "
            f"WHERE host_key LIKE '%toutiao%' OR host_key LIKE '%bytedance%'"
        ).fetchall()
        print(f"\n头条 Cookie: {len(rows)} 条")
        for r in rows:
            print(f"  {r[0]:40s} | {r[1]:30s} | {r[2][:60]}")
    else:
        # 不一定叫 host_key，看看有什么列
        print(f"\n{tbl} 列: {cols}")
        sample = c.execute(f"SELECT * FROM {tbl} LIMIT 1").fetchone()
        if sample:
            print(f"  示例: {sample[:3]}")

conn.close()