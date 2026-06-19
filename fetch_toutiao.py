import sqlite3, urllib.request, json

db_path = r"C:\Users\Administrator\.boshi\tmp_cookies.db"
conn = sqlite3.connect(db_path)
c = conn.cursor()

rows = c.execute(
    "SELECT host_key, name, value FROM cookies "
    "WHERE host_key LIKE '%toutiao%' OR host_key LIKE '%bytedance%'"
).fetchall()

# 构建 Cookie 字符串（只取有值的）
cookie_parts = []
for r in rows:
    name, value = r[1], r[2]
    if value and value.strip():
        cookie_parts.append(f"{name}={value}")

cookie_str = "; ".join(cookie_parts)
print(f"Cookie 条目: {len(cookie_parts)}")
print(f"Cookie 长度: {len(cookie_str)}")

# 验证关键 cookie
key_names = ['sessionid', 'sid_tt', 'uid_tt_ss', 'passport_csrf_token', 'tt_scid']
for key in key_names:
    for r in rows:
        if r[1] == key and r[2]:
            print(f"  ✅ {key} = {r[2][:40]}...")
            break
    else:
        print(f"  ❌ {key} missing!")

# ===== 尝试访问头条 API =====
headers = {
    "Cookie": cookie_str,
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/134.0.0.0 Safari/537.36",
    "Referer": "https://www.toutiao.com/",
    "Accept": "application/json, text/plain, */*",
}

# 1️⃣ 试试用户信息 API（确认登录）
url = "https://www.toutiao.com/toutiao/c/user/article/?page_type=1&count=20"
req = urllib.request.Request(url, headers=headers)
try:
    with urllib.request.urlopen(req, timeout=15) as resp:
        data = resp.read().decode()
        obj = json.loads(data)
        print(f"\n✅ 用户文章API: 成功! HTTP {resp.status}")
        print(f"   返回: {json.dumps(obj, ensure_ascii=False)[:300]}")
except Exception as e:
    print(f"\n❌ 用户文章API: {e}")

# 2️⃣ 试试头条号管理后台 API
url2 = "https://mp.toutiao.com/profile_v4/open/article/list?page=1&page_size=20&type=1&is_og=True&format=json"
req2 = urllib.request.Request(url2, headers=headers)
try:
    with urllib.request.urlopen(req2, timeout=15) as resp:
        data2 = resp.read().decode()
        print(f"\n✅ 头条号文章列表API: 成功! HTTP {resp.status}")
        print(f"   返回前300字: {data2[:300]}")
except Exception as e:
    print(f"\n❌ 头条号文章列表API: {e}")

# 3️⃣ 试试收藏列表 API
url3 = "https://www.toutiao.com/toutiao/c/user/favorites/?count=20"
req3 = urllib.request.Request(url3, headers=headers)
try:
    with urllib.request.urlopen(req3, timeout=15) as resp:
        data3 = resp.read().decode()
        print(f"\n✅ 收藏API: 成功! HTTP {resp.status}")
        print(f"   返回前300字: {data3[:300]}")
except Exception as e:
    print(f"\n❌ 收藏API: {e}")

conn.close()