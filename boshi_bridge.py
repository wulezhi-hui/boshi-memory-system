#!/usr/bin/env python3
"""
伯仕记忆系统 — DSH 插件桥接层
==============================
为 DSH Cordis 插件提供干净的 JSON 接口（区别于 CLI 的格式化输出和 MCP 的 stdio 协议）。

用法:
  python boshi_bridge.py save "记忆内容" [topic]
  python boshi_bridge.py search "查询" [top_k]
  python boshi_bridge.py profile
  python boshi_bridge.py status

所有输出均为单行 JSON（ensure_ascii=False），供 Node 侧 subprocess 直接解析。
"""
import os
import sys
import json

# 强制 UTF-8 stdout（Windows 默认 GBK，记忆内容含 ✅ 等字符会崩）
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

BOSHI_HOME = os.path.expanduser("~/.boshi")
if BOSHI_HOME not in sys.path:
    sys.path.insert(0, BOSHI_HOME)

from boshi_core import search, save, profile, status, time_range


def _out(obj):
    sys.stdout.write(json.dumps(obj, ensure_ascii=False))
    sys.stdout.flush()


def main():
    if len(sys.argv) < 2:
        _out({"error": "missing command"})
        return

    cmd = sys.argv[1]

    if cmd == "save":
        # 用法: save <content> [topic] [--profile=<name>]
        args = [a for a in sys.argv[2:] if not a.startswith("--")]
        prof = None
        for a in sys.argv[2:]:
            if a.startswith("--profile="):
                prof = a.split("=", 1)[1].strip() or None
        if not args:
            _out({"error": "save needs content"})
            return
        content = args[0]
        topic = args[1] if len(args) > 1 else "conversation"
        try:
            _out(save(content=content, topic=topic, profile=prof))
        except Exception as e:
            _out({"error": str(e)})

    elif cmd == "search":
        # 用法: search <query> [top_k] [--scope=self|all]
        args = [a for a in sys.argv[2:] if not a.startswith("--")]
        scope = None
        for a in sys.argv[2:]:
            if a.startswith("--scope="):
                scope = a.split("=", 1)[1].strip() or None
        if not args:
            _out({"error": "search needs query"})
            return
        query = args[0]
        top_k = int(args[1]) if len(args) > 1 else 3
        try:
            _out(search(query=query, top_k=top_k, source="all", scope=scope))
        except Exception as e:
            _out({"error": str(e)})

    elif cmd == "time_range":
        # 调法（显式标志位，无歧义）：
        #   time_range <since> [--until=<ts>] [--top-k=<n>]
        # since 位置参数必填；until / top_k 用 --key=value 或 --key value
        if len(sys.argv) < 3:
            _out({"error": "time_range needs since"})
            return
        since = float(sys.argv[2])
        until = None
        top_k = 50
        scope = None
        rest = sys.argv[3:]
        i = 0
        while i < len(rest):
            a = rest[i]
            if a.startswith("--until="):
                until = float(a.split("=", 1)[1])
            elif a.startswith("--top-k="):
                top_k = int(float(a.split("=", 1)[1]))
            elif a.startswith("--scope="):
                scope = a.split("=", 1)[1].strip() or None
            elif a == "--until" and i + 1 < len(rest):
                i += 1
                until = float(rest[i])
            elif a == "--top-k" and i + 1 < len(rest):
                i += 1
                top_k = int(float(rest[i]))
            elif a == "--scope" and i + 1 < len(rest):
                i += 1
                scope = rest[i].strip() or None
            else:
                _out({"error": f"unknown argument: {a} (use --until=<ts> / --top-k=<n> / --scope=self|all)"})
                return
            i += 1
        try:
            _out(time_range(since=since, until=until, top_k=top_k, scope=scope))
        except Exception as e:
            _out({"error": str(e)})

    elif cmd == "profile":
        try:
            _out(profile())
        except Exception as e:
            _out({"error": str(e)})

    elif cmd == "status":
        try:
            _out(status())
        except Exception as e:
            _out({"error": str(e)})

    else:
        _out({"error": f"unknown command: {cmd}"})


if __name__ == "__main__":
    main()
