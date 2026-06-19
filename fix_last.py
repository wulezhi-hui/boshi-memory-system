#!/usr/bin/env python3
import json, urllib.request, websocket, time, select

req = urllib.request.Request("http://localhost:9224/json")
with urllib.request.urlopen(req, timeout=5) as resp:
    pages = json.loads(resp.read().decode())
for p in pages:
    u = p.get('url','')
    if 'extension' not in u and 'service-worker' not in u and u and 'blank' not in u:
        ws = websocket.create_connection(p['webSocketDebuggerUrl'], timeout=15, header={"Origin": "http://localhost:9224"})
        break

ws.send(json.dumps({"id": 1, "method": "Page.navigate", "params": {"url": "https://www.toutiao.com/article/7646039056186409524/"}}))
time.sleep(6)

ws.send(json.dumps({"id": 2, "method": "Runtime.evaluate", "params": {
    "expression": "document.body.innerText",
    "returnByValue": True
}}))
time.sleep(2)

text = ""
while True:
    ready = select.select([ws.sock], [], [], 3)[0]
    if not ready:
        break
    try:
        msg = ws.recv()
        data = json.loads(msg)
        if 'result' in data and 'result' in data.get('result', {}):
            v = data['result']['result'].get('value', '')
            if v:
                text += v
    except:
        break
ws.close()

if text and len(text) > 200:
    text_clean = text.encode('utf-8', errors='replace').decode('utf-8')
    title = "DeepSeek最强提示词：只要一句话，AI自动替你\u201c多想五步\u201d"
    md = f"# {title}\n\n> 来源: 今日头条收藏\n> 链接: https://www.toutiao.com/article/7646039056186409524/\n> 抓取时间: 2026-06-11\n\n{text_clean}\n"
    path = r"D:\ObsidianVault\虚拟寺院知识库\技术\DeepSeek最强提示词一句话AI自动替你多想五步.md"
    with open(path, 'w', encoding='utf-8') as f:
        f.write(md)
    print(f"OK saved ({len(text_clean)} chars)")
else:
    print(f"FAILED ({len(text)} chars)")