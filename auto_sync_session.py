#!/usr/bin/env python3
"""
自动同步最新微信对话到 converge.db + 工作台

每30秒运行一次（作为 cronjob）：
1. 导出当前 Hermes weixin session
2. 找到所有 user/assistant 对话消息
3. 写入 converge.db
4. 推送到工作台（通过 /push_history）

追踪文件：~/.boshi/.sync_state.json 记录上次同步到的消息 ID
"""

import subprocess
import sqlite3
import json
import sys
import os
import urllib.request
from datetime import datetime
import re

DB = "C:/Users/Administrator/.openclaw/shared/converge.db"
WORKSTATION_URL = "http://127.0.0.1:7681/push_history"
STATE_FILE = os.path.expanduser("~/.boshi/.sync_state.json")

# 当前 weixin session ID
SESSION_ID = "20260520_104709_fbb92682"


def get_session_messages():
    """导出当前 session 并返回所有对话消息（按 id 排序）"""
    try:
        result = subprocess.run(
            ["hermes", "sessions", "export", "--session-id", SESSION_ID, "-"],
            capture_output=True, text=True, timeout=15
        )
        if result.returncode != 0:
            print(f"export failed: rc={result.returncode}", file=sys.stderr)
            return []
        
        data = json.loads(result.stdout)
        messages = data.get("messages", [])
        
        # 过滤：只保留 user 和 assistant 且有 content 的
        conversations = []
        for m in messages:
            role = m.get("role", "")
            content = m.get("content", "")
            msg_id = m.get("id", 0)
            
            if role in ("user", "assistant") and content:
                # 跳过只有工具调用的 assistant 消息（content 为空但有 tool_calls）
                conversations.append({
                    "id": msg_id,
                    "role": role,
                    "content": content,
                    "timestamp": m.get("timestamp", "")
                })
        
        return conversations
    except Exception as e:
        print(f"get_messages error: {e}", file=sys.stderr)
        return []


def load_state():
    """加载同步状态"""
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r") as f:
                return json.load(f)
        except:
            pass
    return {"last_msg_id": 0}


def save_state(state):
    """保存同步状态"""
    os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
    with open(STATE_FILE, "w") as f:
        json.dump(state, f)


def sync_to_converge_db(messages):
    """将新消息写入 converge.db 并返回推送到工作台的消息"""
    if not messages:
        return []
    
    conn = sqlite3.connect(DB)
    synced = []
    try:
        for msg in messages:
            content = msg["content"].strip()
            role = msg["role"]
            if not content:
                continue
            
            # 去重：检查 role+content 是否已存在（检查最近20条）
            existing = conn.execute(
                """SELECT id FROM conversations 
                   WHERE channel='weixin' AND role=? AND content=?
                   ORDER BY id DESC LIMIT 1""",
                (role, content[:200])
            ).fetchone()
            
            if existing:
                continue
            
            ts = msg.get("timestamp", "")
            if isinstance(ts, (int, float)):
                ts = datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S")
            elif not ts:
                ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            
            conn.execute(
                "INSERT INTO conversations (channel, role, content, timestamp) VALUES (?, ?, ?, ?)",
                ("weixin", role, content, ts)
            )
            synced.append(msg)
        
        conn.commit()
    finally:
        conn.close()
    
    return synced


def push_to_workstation(messages):
    """推送到工作台"""
    ok_count = 0
    for msg in messages:
        try:
            data = json.dumps({
                "role": msg["role"],
                "content": msg["content"].strip()
            }).encode("utf-8")
            req = urllib.request.Request(
                WORKSTATION_URL,
                data=data,
                headers={"Content-Type": "application/json"},
                method="POST"
            )
            resp = urllib.request.urlopen(req, timeout=3)
            if resp.status == 200:
                ok_count += 1
        except Exception as e:
            if "Connection refused" not in str(e):
                print(f"push warn: {e}", file=sys.stderr)
    
    return ok_count


def main():
    state = load_state()
    last_msg_id = state.get("last_msg_id", 0)
    
    # 1. 获取所有对话消息
    conversations = get_session_messages()
    if not conversations:
        # 没有找到消息，静默退出
        return
    
    # 2. 过滤出新消息（id > last_msg_id）
    new_messages = [m for m in conversations if m["id"] > last_msg_id]
    if not new_messages:
        return
    
    # 3. 写入数据库
    synced = sync_to_converge_db(new_messages)
    
    # 4. 推送到工作台
    if synced:
        ok = push_to_workstation(synced)
        print(f"synced {len(synced)} msgs (id {synced[0]['id']}..{synced[-1]['id']}), ws_ok={ok}", flush=True)
    
    # 5. 更新状态
    state["last_msg_id"] = new_messages[-1]["id"]
    save_state(state)


if __name__ == "__main__":
    main()
