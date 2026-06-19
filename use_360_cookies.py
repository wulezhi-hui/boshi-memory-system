"""直接从 360 Cookie 数据库读取头条 Cookie 并调 API"""
import sqlite3, urllib.request, json, os

db_path = r"C:\Users\Administrator\.boshi\360_cookies.db"
conn = sqlite3.connect(db_path)
c = conn.cursor()

rows = c.execute(
    "SELECT host_key, name, value, encrypted_value FROM cookies "
    "WHERE (host_key LIKE '%toutiao%' OR host_key LIKE '%bytedance%')"
).fetchall()

# 用 value 列（可能有值的）或忽略加密的
cookie_parts = []
for r in rows:
    host, name, val, enc_val = r
    if val and val.strip():
        cookie_parts.append(f"{name}={val}")

cookie_str = "; ".join(cookie_parts)
print(f"明文 Cookie 条目: {len(cookie_parts)}")

# 先看看有哪些关键的
for r in rows:
    if r[1] in ['sessionid', 'sid_tt', 'uid_tt_ss', 'passport_csrf_token']:
        val = r[2] or r[3][:20] if isinstance(r[3], bytes) else ''
        print(f"  {r[1]}: {str(r[2])[:40]}")

# 用 Cookie 试试调头条 API
headers = {
    "Cookie": cookie_str,
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/132.0.0.0 Safari/537.36",
    "Referer": "https://www.toutiao.com/",
}

# 1. 验证是否登录：用户信息 API
url = "https://www.toutiao.com/toutiao/c/user/article/?page_type=1&count=5"
req = urllib.request.Request(url, headers=headers)
try:
    with urllib.request.urlopen(req, timeout=10) as resp:
        data = json.loads(resp.read().decode())
        print(f"\n✅ 用户文章API: login_status={data.get('login_status')}, data条数={len(data.get('data',[]))}")
        if data.get('data'):
            for a in data['data'][:3]:
                print(f"  📝 {a.get('title','')[:60]}")
except Exception as e:
    print(f"\n❌ 用户文章API: {e}")

conn.close()