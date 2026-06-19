"""从 360 浏览器 CDP 获取头条 Cookie"""
import websocket, json, urllib.request

# 获取 WS URL
req = urllib.request.Request("http://localhost:9224/json/version")
with urllib.request.urlopen(req, timeout=5) as resp:
    ver = json.loads(resp.read().decode())
    ws_url = ver['webSocketDebuggerUrl']

# 连接
ws = websocket.create_connection(ws_url, timeout=10)

# 获取所有 Cookie
ws.send(json.dumps({"id": 1, "method": "Network.getAllCookies"}))
resp = ws.recv()
data = json.loads(resp)
cookies = data.get('result', {}).get('cookies', [])

# 筛选头条
tt_cookies = [c for c in cookies if 'toutiao' in c.get('domain', '') or 'bytedance' in c.get('domain', '')]

cookie_parts = []
for c in tt_cookies:
    cookie_parts.append(f"{c['name']}={c['value']}")
    if c['name'] in ['sessionid', 'sid_tt', 'uid_tt_ss', 'passport_csrf_token', 'tt_scid']:
        print(f"✅ {c['name']} = {c['value'][:50]}...")

cookie_str = "; ".join(cookie_parts)
with open(r"C:\Users\Administrator\.boshi\360_tt_cookie.txt", "w") as f:
    f.write(cookie_str)

print(f"\n共 {len(tt_cookies)} 个 Cookie")
print(f"长度: {len(cookie_str)}")
print("✅ 已保存")

ws.close()