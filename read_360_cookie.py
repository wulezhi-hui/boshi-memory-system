import sqlite3, os, json

db_path = r"C:\Users\Administrator\.boshi\360_cookies.db"
conn = sqlite3.connect(db_path)
c = conn.cursor()

# 查表结构
tables = c.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
print(f"Tables: {[t[0] for t in tables]}")

for tbl in [t[0] for t in tables]:
    # 看看有哪些列
    cols = [col[1] for col in c.execute(f"PRAGMA table_info({tbl})").fetchall()]
    print(f"\n{tbl} 列: {cols}")

    if 'host_key' in cols or 'hostKey' in cols or 'host' in cols:
        host_col = 'host_key' if 'host_key' in cols else ('hostKey' if 'hostKey' in cols else 'host')
        name_col = 'name'
        val_col = 'value' if 'value' in cols else ('encrypted_value' if 'encrypted_value' in cols else None)
        
        # 找头条相关
        rows = c.execute(
            f"SELECT {host_col}, {name_col}, {val_col} FROM {tbl} "
            f"WHERE ({host_col} LIKE '%toutiao%' OR {host_col} LIKE '%bytedance%') "
            f"AND ({val_col} IS NOT NULL AND {val_col} != '')"
        ).fetchall()
        
        if rows:
            print(f"\n✅ 找到 {len(rows)} 个头条 Cookie (有值的):")
            cookie_parts = []
            for r in rows:
                val = r[2]
                if isinstance(val, bytes):
                    # encrypted_value 是加密的，跳过
                    continue
                cookie_parts.append(f"{r[1]}={val}")
                print(f"  {r[0]:40s} | {r[1]:30s} | {str(val)[:60]}")
            
            if cookie_parts:
                cookie_str = "; ".join(cookie_parts)
                print(f"\nCookie 字符串长度: {len(cookie_str)}")
                
                # 保存 Cookie 文件
                with open(r"C:\Users\Administrator\.boshi\360_tt_cookie.txt", "w") as f:
                    f.write(cookie_str)
                print("✅ Cookie 已保存到 360_tt_cookie.txt")

conn.close()