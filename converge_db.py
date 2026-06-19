"""汇聚数据库工具函数 — 三端对话汇集存储"""
import sqlite3, os, json
from datetime import datetime

DB_PATH = os.path.expanduser("~/.openclaw/shared/converge.db")


def get_conn():
    """获取数据库连接"""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def write_message(channel, role, content):
    """写入一条消息到汇聚数据库"""
    try:
        conn = get_conn()
        conn.execute(
            "INSERT INTO conversations (channel, role, content) VALUES (?, ?, ?)",
            (channel, role, content[:10000])
        )
        conn.commit()
        conn.close()
        return True
    except Exception as e:
        print(f"[汇聚DB] 写入失败: {e}")
        return False


def read_recent(limit=80, channels=None):
    """读取最近对话，支持按通道过滤"""
    try:
        conn = get_conn()
        if channels:
            placeholders = ",".join("?" for _ in channels)
            rows = conn.execute(
                f"SELECT channel, role, content, timestamp FROM conversations "
                f"WHERE channel IN ({placeholders}) ORDER BY timestamp DESC LIMIT ?",
                channels + [limit]
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT channel, role, content, timestamp FROM conversations "
                "ORDER BY timestamp DESC LIMIT ?", (limit,)
            ).fetchall()
        conn.close()
        # 反转成时间正序
        result = []
        for r in reversed(rows):
            result.append({
                "role": r["role"],
                "content": r["content"],
                "timestamp": r["timestamp"],
                "channel": r["channel"],
            })
        return result
    except Exception as e:
        print(f"[汇聚DB] 读取失败: {e}")
        return []


def read_recent_messages(limit=80, channels=None, for_hermes=False):
    """以 Hermes messages 格式读取最近对话"""
    rows = read_recent(limit, channels)
    if for_hermes:
        # 去掉时间戳和通道前缀，返回精简格式
        msgs = []
        for r in rows:
            content = r["content"]
            # 去掉 [weixin] / [workstation] 前缀
            if "[weixin]" in content or "[workstation]" in content or "[terminal]" in content:
                content = content.split("] ", 1)[-1] if "] " in content else content
            msgs.append({"role": r["role"], "content": content})
        return msgs
    return rows


if __name__ == "__main__":
    # 测试
    write_message("test", "user", "这是一条测试消息")
    write_message("test", "assistant", "测试回复")
    print("最近5条:")
    for r in read_recent(5):
        print(f"  [{r['channel']}] {r['role']}: {r['content'][:50]}")
    print(f"\nHermes 格式:")
    for m in read_recent_messages(5, for_hermes=True):
        print(f"  {m['role']}: {m['content'][:50]}")
