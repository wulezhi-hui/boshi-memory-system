"""将当前微信对话写入 converge.db 并推送到工作台"""
import sqlite3, os, sys, json, urllib.request

DB = os.path.expanduser(r'~/.openclaw/shared/converge.db')
WORKSTATION_URL = 'http://127.0.0.1:7681/push_history'

def write_msg(role, content):
    """写入 converge.db"""
    conn = sqlite3.connect(DB)
    conn.execute("INSERT INTO conversations (channel, role, content) VALUES (?, ?, ?)",
                 ('weixin', role, content[:10000]))
    conn.commit()
    conn.close()
    print(f"写入 converge.db: {role} | {content[:50]}...")

def push_to_workstation(role, content):
    """推送到工作台"""
    data = json.dumps({"role": role, "content": content}).encode('utf-8')
    try:
        req = urllib.request.Request(
            WORKSTATION_URL, data=data,
            headers={'Content-Type': 'application/json'}
        )
        resp = urllib.request.urlopen(req, timeout=5)
        result = json.loads(resp.read().decode())
        if result.get("ok"):
            print(f"推送到工作台: {content[:50]}...")
        else:
            print(f"推送失败: {result}")
    except Exception as e:
        print(f"推送异常: {e}")

if __name__ == '__main__':
    if len(sys.argv) < 3:
        print("用法: python weixin_sync.py <role> <content>")
        sys.exit(1)
    role = sys.argv[1]
    content = sys.argv[2]
    write_msg(role, content)
    push_to_workstation(role, content)
