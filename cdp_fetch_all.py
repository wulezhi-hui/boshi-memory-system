#!/usr/bin/env python3
"""用360浏览器CDP打开头条收藏文章并提取正文"""
import json, urllib.request, websocket, time, select, os, re

COOKIE_FILE = r"C:\Users\Administrator\.boshi\360_tt_cookie.txt"
KB_DIR = r"D:\ObsidianVault\虚拟寺院知识库\技术"
os.makedirs(KB_DIR, exist_ok=True)

with open(COOKIE_FILE, 'r') as f:
    COOKIE_STR = f.read().strip()

# AI技术文章列表
articles = [
    ("7649709146966704694", "Github近240万star-3个超神的GitHub开源项目"),
    ("7649751210567680527", "56.2k-Star-这个Skill给Hermes和ClaudeCode装上项目大脑"),
    ("7649612153594511899", "本地加云端免费模型用到你爽"),
    ("7649684171397186098", "全模态API无限期免费谁在掀翻AI时代的成本大山"),
    ("7648395916214288931", "24.6k星-AI的记忆问题被这个团队解决了"),
    ("7648306988731957800", "LLM-context一夜暴涨4000星-开源压缩层让agent少烧60-95%token"),
    ("7638465385704079907", "部署Hermes-Agent后配置SOUL-USER-AGENTS核心文件"),
    ("7646937954216100390", "EasyAIoT把所有摄像头接进来AI实时盯着画面"),
    ("7647336149589000704", "DeepSeek上线专家模式-这套提示词榨干它全部潜能"),
    ("7647040580555407899", "估计90%用户都不知道Hermes是有辅模型的"),
    ("7646310367374737954", "OpenClaw避坑指南先装这10个Skills再谈生产力"),
    ("7646039056186409524", "DeepSeek最强提示词一句话AI自动替你多想五步"),
    ("7646239566382318132", "Hermes接BurpSuite法力无边"),
    ("7646240820873527854", "browser-use-AI-Agent终于能像人一样上网冲浪了"),
]

def sanitize_filename(title):
    name = re.sub(r'[\\/:*?"<>|]', '_', title)[:80]
    return name.strip('_. ')

# 连接360CDP
req = urllib.request.Request("http://localhost:9224/json")
with urllib.request.urlopen(req, timeout=5) as resp:
    pages = json.loads(resp.read().decode())

target = None
for p in pages:
    u = p.get('url','')
    if 'extension' not in u and 'service-worker' not in u and u and 'blank' not in u:
        target = p
        break

if not target:
    print("❌ 无可用页面")
    exit(1)

ws = websocket.create_connection(target['webSocketDebuggerUrl'], timeout=10,
                                  header={"Origin": "http://localhost:9224"})

success = 0
for i, (aid, title) in enumerate(articles):
    print(f"[{i+1}/{len(articles)}] {title[:50]}...")
    url = f"https://www.toutiao.com/article/{aid}/"
    
    ws.send(json.dumps({"id": 1, "method": "Page.navigate", "params": {"url": url}}))
    time.sleep(6)
    
    # 提取正文
    ws.send(json.dumps({"id": 2, "method": "Runtime.evaluate", "params": {
        "expression": "document.body.innerText",
        "returnByValue": True
    }}))
    time.sleep(2)
    
    text_content = ""
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
                    text_content += v
        except:
            break
    
    if text_content and len(text_content) > 200:
        filename = sanitize_filename(title) + ".md"
        filepath = os.path.join(KB_DIR, filename)
        md_content = f"# {title}\n\n> 来源: 今日头条收藏\n> 链接: https://www.toutiao.com/article/{aid}/\n> 抓取时间: 2026-06-11\n\n{text_content}\n"
        with open(filepath, 'w', encoding='utf-8') as f:
            f.write(md_content)
        print(f"  ✅ 已保存 ({len(text_content)}字)")
        success += 1
    else:
        print(f"  ❌ 提取失败 ({len(text_content) if text_content else 0}字)")

ws.close()
print(f"\n✅ 完成! 成功 {success}/{len(articles)} 篇，保存到 {KB_DIR}")