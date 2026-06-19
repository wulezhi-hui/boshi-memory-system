"""双向桥接 — 微信↔工作台 实时同步
1. 工作台→微信：检测 converge.db 的工作台消息，推送到微信（现有功能）
2. 微信→工作台：检测 converge.db 的微信消息/回复，推送到工作台左边栏
"""
import sqlite3, os, time, sys, subprocess, urllib.request, urllib.error, json

DB = os.path.expanduser(r'~/.openclaw/shared/converge.db')
SYNCED_FILE = os.path.expanduser(r'~/.boshi/.bridged_msg_ids.txt')
WORKSTATION_URL = 'http://127.0.0.1:7681/push_history'

def load_synced():
    if os.path.exists(SYNCED_FILE):
        with open(SYNCED_FILE) as f:
            return set(line.strip() for line in f if line.strip())
    return set()

def save_synced(msg_id):
    with open(SYNCED_FILE, 'a') as f:
        f.write(str(msg_id) + '\n')

def log(msg):
    ts = time.strftime('%Y-%m-%d %H:%M:%S')
    print(f"[{ts}] {msg}")
    sys.stdout.flush()

def send_to_wechat(content, ts):
    """工作台→微信"""
    text = f'〔工作台〕{content}'[:2000]
    cmd = [sys.executable, '-m', 'hermes_cli.main', 'send', '--to', 'weixin', text]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=15,
                          encoding='utf-8', errors='replace')
        if r.returncode == 0:
            log(f"→微信: {content[:50]}...")
        else:
            log(f"→微信 失败: {r.stderr[:200]}")
    except subprocess.TimeoutExpired:
        log("→微信 超时")
    except Exception as e:
        log(f"→微信 异常: {e}")

def push_to_workstation(role, content, ts):
    """微信→工作台"""
    data = json.dumps({"role": role, "content": content}).encode('utf-8')
    try:
        req = urllib.request.Request(
            WORKSTATION_URL, data=data,
            headers={'Content-Type': 'application/json'}
        )
        resp = urllib.request.urlopen(req, timeout=5)
        result = json.loads(resp.read().decode())
        if result.get("ok"):
            log(f"→工作台: {content[:50]}...")
        else:
            log(f"→工作台 失败: {result}")
    except Exception as e:
        log(f"→工作台 异常: {e}")

def bridge_loop():
    log("🔄 双向桥接启动中...")
    log(f"   监控: {DB}")
    
    while True:
        try:
            if not os.path.exists(DB):
                time.sleep(2)
                continue
            
            synced = load_synced()
            conn = sqlite3.connect(DB)
            c = conn.cursor()
            
            # 获取最新的消息（按时间倒序）
            rows = c.execute("""
                SELECT id, channel, role, content, timestamp
                FROM conversations
                ORDER BY id DESC
                LIMIT 20
            """).fetchall()
            conn.close()
            
            for msg_id, channel, role, content, ts in reversed(rows):
                s_id = str(msg_id)
                if s_id in synced:
                    continue
                
                if channel == 'workstation' and role == 'user':
                    # 工作台→微信
                    send_to_wechat(content, ts)
                elif channel == 'weixin':
                    # 微信→工作台（用户消息和AI回复都推）
                    push_to_workstation(role, content, ts)
                
                save_synced(s_id)
        
        except Exception as e:
            log(f"⚠️ 错误: {e}")
        
        time.sleep(2)

if __name__ == '__main__':
    bridge_loop()
