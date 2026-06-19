#!/usr/bin/env python3
"""通过 Chrome CDP 协议获取头条 Cookie，然后注入 Camofox 访问收藏页"""
import urllib.request, json, time

# 1. 用 Chrome 真实用户数据启动 CDP
import subprocess, os

chrome = r"C:\Users\Administrator\AppData\Local\Google\Chrome\Application\chrome.exe"
user_data = r"C:\Users\Administrator\AppData\Local\Google\Chrome\User Data"

# 先杀干净
os.system("taskkill /F /IM chrome.exe 2>nul")
time.sleep(3)

proc = subprocess.Popen(
    [chrome, "--remote-debugging-port=9222", f"--user-data-dir={user_data}",
     "--no-first-run", "--no-default-browser-check",
     "--new-window", "https://www.toutiao.com"],
    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
)
print(f"Chrome started PID: {proc.pid}")

# 2. 等待 CDP 就绪
for i in range(10):
    time.sleep(2)
    try:
        req = urllib.request.Request("http://localhost:9222/json/version")
        with urllib.request.urlopen(req, timeout=5) as resp:
            d = json.loads(resp.read().decode())
            ws_url = d.get("webSocketDebuggerUrl", "")
            print(f"✅ CDP Ready! {d.get('Browser','')[:50]}")
            break
    except:
        print(f"Waiting... {i+1}")
else:
    print("❌ CDP 启动失败")
    exit(1)

# 3. 创建一个新页面
req = urllib.request.Request("http://localhost:9222/json/new")
with urllib.request.urlopen(req, timeout=5) as resp:
    page = json.loads(resp.read().decode())
    page_id = page["id"]
    ws_url = page["webSocketDebuggerUrl"]
    print(f"✅ 新页面创建: {page_id}")

# 4. 用 CDP 协议获取 Cookie
# 通过 Puppeteer-like WebSocket 调用
import websocket
import base64
import json as json_mod

def cdp_send(ws, cmd_id, method, params=None):
    msg = {"id": cmd_id, "method": method}
    if params:
        msg["params"] = params
    ws.send(json_mod.dumps(msg))
    while True:
        resp = json_mod.loads(ws.recv())
        if resp.get("id") == cmd_id:
            return resp.get("result", {})

ws = websocket.create_connection(ws_url)

# 先导航到头条
cdp_send(ws, 1, "Page.enable")
cdp_send(ws, 2, "Page.navigate", {"url": "https://www.toutiao.com"})
time.sleep(5)

# 获取所有 Cookie
cookies_result = cdp_send(ws, 3, "Network.getAllCookies")
cookies = cookies_result.get("cookies", [])

# 筛选头条相关
tt_cookies = [c for c in cookies if "toutiao" in c.get("domain", "") or "bytedance" in c.get("domain", "")]
print(f"\n头条相关 Cookie: {len(tt_cookies)} 条")

cookie_parts = []
for c in tt_cookies:
    cookie_parts.append(f"{c['name']}={c['value']}")
    if c['name'] in ['sessionid', 'sid_tt', 'uid_tt_ss', 'passport_csrf_token']:
        print(f"  ✅ {c['name']} = {c['value'][:40]}...")

cookie_str = "; ".join(cookie_parts)
print(f"\nCookie 长度: {len(cookie_str)}")

# 保存到文件供后续使用
with open(r"C:\Users\Administrator\.boshi\tt_cookie.txt", "w") as f:
    f.write(cookie_str)
print("✅ Cookie 已保存到 .boshi/tt_cookie.txt")

# 关闭页面
cdp_send(ws, 4, "Page.close")
ws.close()
print("✅ 完成")