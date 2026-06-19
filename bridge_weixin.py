#!/usr/bin/env python3
"""
三友微信 <-> 伯仕工作台 桥接器 v3.1

双向桥接：
- 微信->工作台: 轮询 converge.db 的 weixin 消息，通过 HTTP POST /push_history 推送到工作台
- 工作台->微信: 轮询 converge.db 的 workstation 消息，通过 subprocess 调用 hermes send --to weixin

数据库路径：~/.openclaw/shared/converge.db
工作台推送API: http://127.0.0.1:7681/push_history

v3.1: 去重保活 + 异常自愈，启动时检查重复
"""

import sqlite3
import time
import subprocess
import urllib.request
import urllib.parse
import json
import sys
import os
import hashlib
from datetime import datetime

CONVERGE_DB = "C:/Users/Administrator/.openclaw/shared/converge.db"
WORKSTATION_URL = "http://127.0.0.1:7681/push_history"
POLL_INTERVAL = 1.5  # 1.5秒轮询，接近实时
PID_FILE = os.path.expanduser("~/.boshi/.bridge_weixin.pid")
SYNCED_FILE = os.path.expanduser("~/.boshi/.bridged_msg_ids.txt")

processed = set()

def is_process_alive(pid):
    """检查 PID 是否存活（Windows 兼容）"""
    try:
        result = subprocess.run(
            ['wmic', 'process', 'where', f'ProcessId={pid}', 'get', 'ProcessId'],
            capture_output=True, text=True, timeout=5
        )
        return str(pid) in result.stdout
    except Exception:
        return False

def check_already_running():
    """检查 PID 文件，如果已有活着的桥接实例则退出"""
    if os.path.exists(PID_FILE):
        try:
            with open(PID_FILE) as f:
                old_pid = int(f.read().strip())
            if is_process_alive(old_pid) and old_pid != os.getpid():
                print(f"[{datetime.now().strftime('%H:%M:%S')}] ⚠ bridge_weixin 已在运行(PID={old_pid})，退出")
                print(f"    调用者: {sys.executable} {' '.join(sys.argv)}")
                sys.exit(0)
        except (ValueError, OSError):
            pass

def get_db():
    """连接到汇聚数据库"""
    conn = sqlite3.connect(CONVERGE_DB)
    conn.row_factory = sqlite3.Row
    return conn

def get_latest_messages(conn, last_id=0):
    """获取所有频道的最新消息"""
    cursor = conn.execute(
        "SELECT id, channel, role, content, timestamp FROM conversations WHERE id > ? ORDER BY id ASC",
        (last_id,)
    )
    rows = cursor.fetchall()
    return [dict(r) for r in rows]

def push_to_workstation(msg):
    """通过 HTTP POST 推送单条消息到工作台"""
    try:
        data = json.dumps({
            "role": msg["role"],
            "content": msg["content"]
        }).encode("utf-8")
        req = urllib.request.Request(
            WORKSTATION_URL,
            data=data,
            headers={"Content-Type": "application/json"},
            method="POST"
        )
        resp = urllib.request.urlopen(req, timeout=3)
        return resp.status == 200
    except Exception as e:
        # 工作台可能不在线，静默忽略连接错误
        if "Connection refused" not in str(e) and "404" not in str(e):
            print(f"[{datetime.now().strftime('%H:%M:%S')}] ⚠ push_to_workstation: {e}", flush=True)
        return False

def load_synced():
    if os.path.exists(SYNCED_FILE):
        with open(SYNCED_FILE) as f:
            return set(line.strip() for line in f if line.strip())
    return set()

def save_synced(msg_id):
    with open(SYNCED_FILE, 'a') as f:
        f.write(str(msg_id) + '\n')

def send_to_weixin(text):
    """通过 hermes send 发送消息到微信"""
    try:
        safe_text = text.replace('"', '\\"').replace('`', '\\`')
        result = subprocess.run(
            ['hermes', 'send', '--to', 'weixin', safe_text],
            capture_output=True,
            text=True,
            timeout=10
        )
        return result.returncode == 0
    except Exception as e:
        print(f"[{datetime.now().strftime('%H:%M:%S')}] ⚠ send_to_weixin: {e}", flush=True)
        return False

def write_pid():
    """写入 PID 文件，避免重复启动"""
    pid = os.getpid()
    os.makedirs(os.path.dirname(PID_FILE), exist_ok=True)
    with open(PID_FILE, 'w') as f:
        f.write(str(pid))
    print(f"   PID: {pid} (已写入 {PID_FILE})", flush=True)

def cleanup_pid():
    """退出时清理 PID 文件"""
    if os.path.exists(PID_FILE):
        try:
            # 只清理自己写入的 PID
            with open(PID_FILE) as f:
                saved_pid = int(f.read().strip())
            if saved_pid == os.getpid():
                os.remove(PID_FILE)
                print(f"   PID文件已清理", flush=True)
        except Exception as e:
            print(f"   PID清理失败: {e}", flush=True)

def main():
    print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] 🔄 双向桥接启动中...", flush=True)
    print(f"   监控: {CONVERGE_DB}", flush=True)
    print(f"   间隔: {POLL_INTERVAL}s", flush=True)
    
    # 检查是否已在运行
    check_already_running()
    
    # 写 PID 文件
    write_pid()
    
    # 确保数据库存在
    if not os.path.exists(CONVERGE_DB):
        print(f"❌ 数据库不存在: {CONVERGE_DB}", flush=True)
        cleanup_pid()
        sys.exit(1)
    
    # 加载历史已处理 ID
    synced = load_synced()
    processed.update(synced)
    
    # 初始：获取当前最大ID
    conn = get_db()
    last_processed_id = conn.execute("SELECT COALESCE(MAX(id), 0) FROM conversations").fetchone()[0]
    conn.close()
    
    print(f"   起始ID: {last_processed_id}  (已去重: {len(processed)}条)", flush=True)
    
    try:
        while True:
            try:
                conn = get_db()
                new_messages = get_latest_messages(conn, last_id=last_processed_id)
                conn.close()
                
                if new_messages:
                    for msg in new_messages:
                        channel = msg["channel"]
                        content = msg["content"]
                        role = msg["role"]
                        msg_id = msg["id"]
                        
                        # 去重
                        if msg_id in processed:
                            continue
                        processed.add(msg_id)
                        save_synced(msg_id)
                        
                        if channel == "workstation":
                            print(f"[{datetime.now().strftime('%H:%M:%S')}] →微信: {content[:60]}...", flush=True)
                            send_to_weixin(content)
                        else:
                            print(f"[{datetime.now().strftime('%H:%M:%S')}] →工作台: {content[:60]}...", flush=True)
                            push_to_workstation(msg)
                    
                    last_processed_id = new_messages[-1]["id"]
            except Exception as inner_e:
                # 单次轮询异常不退出，等待后重试
                print(f"[{datetime.now().strftime('%H:%M:%S')}] ⚠ 轮询异常(自动恢复): {inner_e}", flush=True)
                time.sleep(3)
            
            time.sleep(POLL_INTERVAL)
            
    except KeyboardInterrupt:
        print(f"\n[{datetime.now().strftime('%H:%M:%S')}] 🛑 桥接停止", flush=True)
    finally:
        cleanup_pid()

if __name__ == "__main__":
    main()
