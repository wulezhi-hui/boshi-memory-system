import sqlite3

db_path = r"C:\Users\Administrator\.boshi\tmp_cookies.db"
conn = sqlite3.connect(db_path)
c = conn.cursor()

# cookies 表结构
cols = c.execute("PRAGMA table_info(cookies)").fetchall()
print("cookies 表列:")
for col in cols:
    print(f"  {col}")

# 取一行看实际数据
row = c.execute("SELECT * FROM cookies LIMIT 1").fetchone()
print(f"\n第一行数据 ({len(row)}个字段):")
for i, (col_name, col_type, *_) in enumerate(cols):
    val = row[i]
    if isinstance(val, bytes):
        print(f"  [{i}] {col_name} ({col_type}) = bytes({len(val)})")
    elif val is None:
        print(f"  [{i}] {col_name} ({col_type}) = None")
    else:
        print(f"  [{i}] {col_name} ({col_type}) = {str(val)[:80]}")

# 看看是否有头条 sessionid 的实际数据
print("\n\n查找 sessionid:")
for row2 in c.execute("SELECT * FROM cookies WHERE name='sessionid' LIMIT 1").fetchall():
    for i, (col_name, col_type, *_) in enumerate(cols):
        val = row2[i]
        if val and (isinstance(val, bytes) and len(val) > 0) or (isinstance(val, str) and val.strip()):
            print(f"  {col_name} = {str(val)[:80]}")
    
conn.close()