"""用360头条Cookie获取收藏文章"""
import urllib.request, json

# 读 Cookie
with open(r"C:\Users\Administrator\.boshi\360_tt_cookie.txt", "r") as f:
    cookie_str = f.read().strip()

headers = {
    "Cookie": cookie_str,
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/132.0.0.0 Safari/537.36",
    "Referer": "https://www.toutiao.com/",
    "Accept": "application/json, text/plain, */*",
}

# 1. 用户已发布文章列表
urls = [
    ("用户文章", "https://www.toutiao.com/toutiao/c/user/article/?page_type=1&count=30"),
    ("收藏列表", "https://www.toutiao.com/toutiao/c/user/favorites/?count=30"),
    ("头条号文章", "https://mp.toutiao.com/profile_v4/open/article/list?page=1&page_size=30&type=1&format=json"),
    ("我的收藏v2", "https://www.toutiao.com/api/pc/user/favorites?count=30"),
]

for name, url in urls:
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            text = resp.read().decode('utf-8', errors='replace')
            try:
                data = json.loads(text)
                print(f"\n✅ {name} 成功!")
                if isinstance(data, dict):
                    print(f"   键: {list(data.keys())[:10]}")
                    for k in ['login_status', 'has_more', 'message', 'data', 'code']:
                        if k in data:
                            v = data[k]
                            if isinstance(v, list):
                                print(f"   {k}: 列表({len(v)}条)")
                                if v and len(v) > 0:
                                    sample = v[0]
                                    if isinstance(sample, dict):
                                        print(f"   首项键: {list(sample.keys())[:10]}")
                            else:
                                print(f"   {k}: {v}")
                elif isinstance(data, list):
                    print(f"   列表({len(data)}条)")
            except:
                print(f"   (非JSON) 前200字: {text[:200]}")
    except urllib.error.HTTPError as e:
        print(f"\n❌ {name}: HTTP {e.code}")
    except Exception as e:
        print(f"\n❌ {name}: {e}")

# 2. 试试头条号创作平台列表（之前成功用过）
print("\n\n=== 头条号文章管理 ===")
url4 = "https://mp.toutiao.com/profile_v4/open/article/list?page=1&page_size=20"
req4 = urllib.request.Request(url4, headers={
    **headers, 
    "Referer": "https://mp.toutiao.com/",
    "X-Requested-With": "XMLHttpRequest"
})
try:
    with urllib.request.urlopen(req4, timeout=10) as resp:
        text = resp.read().decode('utf-8', errors='replace')
        if text.startswith('<'):
            # HTML 响应
            import re
            scripts = re.findall(r'window\._initialState\s*=\s*({.*?});', text, re.DOTALL)
            if scripts:
                data = json.loads(scripts[0])
                print(f"✅ 找到 initial_state, 键: {list(data.keys())[:10]}")
            else:
                print(f"✅ 返回HTML ({len(text)}字符)")
        else:
            data = json.loads(text)
            print(f"✅ 成功! 键: {list(data.keys())[:10]}")
            if 'data' in data and isinstance(data['data'], dict):
                for k, v in data['data'].items():
                    if isinstance(v, list):
                        print(f"   {k}: {len(v)}条")
except Exception as e:
    print(f"❌ {e}")