"""把伯仕的回复自动写入汇聚数据库"""
import sqlite3, os, sys

DB_PATH = os.path.expanduser("~/.openclaw/shared/converge.db")

def write(channel, role, content):
    try:
        conn = sqlite3.connect(DB_PATH)
        conn.execute("INSERT INTO conversations (channel, role, content) VALUES (?, ?, ?)", (channel, role, content[:10000]))
        conn.commit()
        conn.close()
        return True
    except:
        return False
