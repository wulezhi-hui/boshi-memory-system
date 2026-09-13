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
        if len(sys.argv) < 3:
            _out({"error": "save needs content"})
            return
        content = sys.argv[2]
        topic = sys.argv[3] if len(sys.argv) > 3 else "conversation"
        try:
            _out(save(content=content, topic=topic))
        except Exception as e:
            _out({"error": str(e)})

    elif cmd == "search":
        if len(sys.argv) < 3:
            _out({"error": "search needs query"})
            return
        query = sys.argv[2]
        top_k = int(sys.argv[3]) if len(sys.argv) > 3 else 3
        try:
            _out(search(query=query, top_k=top_k, source="all"))
        except Exception as e:
            _out({"error": str(e)})

    elif cmd == "time_range":
        if len(sys.argv) < 3:
            _out({"error": "time_range needs since"})
            return
        since = float(sys.argv[2])
        # 位置参数（均可选）：
        #   argv[3] = until 或 top_k（根据大小判断）
        #   argv[4] = top_k
        # DSH 插件调法：
        #   time_range <since> <top_k>        → 3 个值，argv[3] 是小整数=top_k
        #   time_range <since> <until> <top_k> → 4 个值，argv[3] 是大数=until
        # 判断标准：Unix 时间戳远大于 1000（1970年1月15日 = 1209600），
        # top_k 最大合理值 < 1000
        until = None
        top_k = 50
        if len(sys.argv) > 3:
            arg3 = sys.argv[3].strip()
            if arg3:
                val3 = float(arg3)
                if val3 < 1000:
                    # 小整数 → 这是 top_k，不是 until
                    top_k = int(val3)
                else:
                    # 大数 → 这是 until
                    until = val3
        if len(sys.argv) > 4:
            arg4 = sys.argv[4].strip()
            if arg4:
                top_k = int(arg4)
        try:
            _out(time_range(since=since, until=until, top_k=top_k))
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
