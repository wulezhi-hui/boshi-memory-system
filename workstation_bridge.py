"""伯仕工作台→微信 广播桥接
检测 converge.db 中工作台(channel=workstation)的新用户消息，
用 Hermes send_message 工具推送到微信。

每次运行时检查最新消息，跳过已发送的（通过 .bridged_msg_ids.txt 去重）。
适合作为 cron 任务运行（间隔 3-5 秒）。
"""
import sqlite3
import os
import sys

DB = os.path.expanduser(r'~/.openclaw/shared/converge.db')
SYNCED_FILE = os.path.expanduser(r'~/.boshi/.bridged_msg_ids.txt')

def load_synced():
    if os.path.exists(SYNCED_FILE):
        with open(SYNCED_FILE) as f:
            return set(line.strip() for line in f if line.strip())
    return set()

def save_synced(msg_id):
    with open(SYNCED_FILE, 'a') as f:
        f.write(str(msg_id) + '\n')

def check_and_bridge():
    """检查 converge.db 中的工作台新消息，返回待发送的消息列表"""
    synced = load_synced()
    
    if not os.path.exists(DB):
        print("DB not found")
        return []
    
    conn = sqlite3.connect(DB)
    c = conn.cursor()
    
    # 获取最新的 workstation 用户消息（没有回复过的）
    rows = c.execute("""
        SELECT id, channel, role, content, timestamp
        FROM conversations
        WHERE channel = 'workstation' AND role = 'user'
        ORDER BY id DESC
        LIMIT 10
    """).fetchall()
    conn.close()
    
    new_messages = []
    for msg_id, channel, role, content, ts in rows:
        if msg_id in synced:
            continue
        new_messages.append({'id': msg_id, 'content': content, 'time': ts})
        save_synced(msg_id)
    
    # 按时间正序返回
    return list(reversed(new_messages))

if __name__ == '__main__':
    msgs = check_and_bridge()
    for m in msgs:
        # 输出格式：每行一条消息，用 || 分隔字段，方便 cron 脚本解析
        print(f"MSG||{m['id']}||{m['content']}||{m['time']}")
    
    if msgs:
        print(f"Bridged: {len(msgs)} messages")
    else:
        print("No new messages")
