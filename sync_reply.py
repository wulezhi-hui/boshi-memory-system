#!/usr/bin/env python3
"""
自动同步：伯仕每次回话后调用，把微信对话同步到工作台

功能：
1. 写入 converge.db（完整记录）
2. 通过 /push_history API 直接推送到工作台（即时，不等轮询）

用法：
  python sync_reply.py <role> <content>
  
  role: user 或 assistant
  content: 消息内容
"""

import sqlite3
import sys
import os
import urllib.request
import json
from datetime import datetime

DB = "C:/Users/Administrator/.openclaw/shared/converge.db"
WORKSTATION_URL = "http://127.0.0.1:7681/push_history"


def sync(role, content):
    """写入一条消息到 converge.db 并推送到工作台"""
    if not content or not role:
        return False
    
    # 1. 写入数据库
    msg_id = None
    conn = sqlite3.connect(DB)
    try:
        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        conn.execute(
            "INSERT INTO conversations (channel, role, content, timestamp) VALUES (?, ?, ?, ?)",
            ("weixin", role, content.strip(), ts)
        )
        conn.commit()
        msg_id = conn.execute("SELECT MAX(id) FROM conversations").fetchone()[0]
    except Exception as e:
        print(f"DB ERROR: {e}", file=sys.stderr)
        return False
    finally:
        conn.close()
    
    # 2. 推送到工作台
    try:
        data = json.dumps({
            "role": role,
            "content": content.strip()
        }).encode("utf-8")
        req = urllib.request.Request(
            WORKSTATION_URL,
            data=data,
            headers={"Content-Type": "application/json"},
            method="POST"
        )
        resp = urllib.request.urlopen(req, timeout=3)
        ws_ok = resp.status == 200
    except Exception as e:
        ws_ok = False
        # 工作台可能不在线，静默
        if "Connection refused" not in str(e):
            print(f"WS PUSH WARN: {e}", file=sys.stderr)
    
    print(f"OK: sync ID={msg_id} role={role} len={len(content)} ws_push={ws_ok}")
    return True


if __name__ == "__main__":
    if len(sys.argv) < 3:
        print(f"用法: python {sys.argv[0]} <role> <content>")
        sys.exit(1)
    
    role = sys.argv[1]
    content = " ".join(sys.argv[2:])
    
    if role not in ("user", "assistant"):
        print(f"无效 role: {role}，应为 user 或 assistant", file=sys.stderr)
        sys.exit(1)
    
    success = sync(role, content)
    sys.exit(0 if success else 1)
