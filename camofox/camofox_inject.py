#!/usr/bin/env python3
"""Camofox 反检测注入工具
在已有 tab 上执行反检测 JS 注入。
用法:
  python3 camofox_inject.py <tab_id>    # 注入到指定 tab
  python3 camofox_inject.py             # 自动找第一个活跃 tab 注入
"""
import sys, json, urllib.request, os

CAMOFOX_URL = "http://localhost:9377"
STEALTH_JS = os.path.expanduser("~/.boshi/camofox/stealth_inject.js")

def load_stealth_script():
    with open(STEALTH_JS, 'r', encoding='utf-8') as f:
        code = f.read()
    # 压缩成单行
    return ' '.join(line.strip() for line in code.split('\n') if line.strip() and not line.strip().startswith('//'))

def get_active_tabs():
    req = urllib.request.Request(f"{CAMOFOX_URL}/tabs")
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return json.loads(resp.read().decode())
    except:
        # 试试 GET /tabs?userId=lezhi
        req = urllib.request.Request(f"{CAMOFOX_URL}/tabs?userId=lezhi")
        try:
            with urllib.request.urlopen(req, timeout=5) as resp:
                return json.loads(resp.read().decode())
        except:
            return []

def inject(tab_id):
    script = load_stealth_script()
    payload = json.dumps({
        "userId": "lezhi",
        "expression": script
    }).encode()
    req = urllib.request.Request(
        f"{CAMOFOX_URL}/tabs/{tab_id}/evaluate",
        data=payload,
        method="POST",
        headers={"Content-Type": "application/json"}
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            result = json.loads(resp.read().decode())
            if result.get("ok"):
                print(f"✅ 反检测已注入到 tab {tab_id}")
                return True
            else:
                print(f"⚠️ 注入返回: {result}")
                return False
    except Exception as e:
        print(f"❌ 注入失败: {e}")
        return False

if __name__ == "__main__":
    if len(sys.argv) > 1:
        tab_id = sys.argv[1]
        inject(tab_id)
    else:
        tabs = get_active_tabs()
        if not tabs:
            print("❌ 没有活跃的 Camofox tabs")
            sys.exit(1)
        
        if isinstance(tabs, list):
            count = 0
            for tab in tabs:
                tid = tab.get("tabId") or tab.get("id")
                if tid:
                    if inject(tid):
                        count += 1
            print(f"已完成 {count}/{len(tabs)} 个 tab 的反检测注入")
        elif isinstance(tabs, dict):
            tid = tabs.get("tabId") or tabs.get("id")
            if tid:
                inject(tid)