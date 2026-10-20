# -*- coding: utf-8 -*-
"""验证 P2 升级：① boshi_core.save（MCP/boshi_api 路径）也自动抽槽位 ② 长文本不截断
③ 插件 _slots_meta 对长用户消息可用 ④ BM25 文档仍用原文（未改排序口径）
写一条测试记忆后立刻删除，不留脏数据。"""
import os
import sys

HERE = r"C:/Users/wulezhi/.boshi"
sys.path.insert(0, HERE)
os.chdir(HERE)

import psycopg           # noqa: E402
import boshi_core        # noqa: E402
import pg_bridge         # noqa: E402

LONG = (
    "记忆系统升级自测：218 机器上 llama-server 监听端口 8080，ComfyUI 在 8188，"
    "PE 反推服务 8199，版本 v7.1.3，参数 ctx 长度 32768，阈值 0.85，"
    "模型放在 J:\\models\\Qwen3.6-27B，日志 D:/logs/llama.log。"
) * 12
print("测试正文长度:", len(LONG))

# ① boshi_core.save —— 与 MCP 同一个入口，不带任何 slots
r = boshi_core.save(LONG, topic="test_p2_upgrade")
mid = r["memory_id"]
print("写入 id:", mid)

cur = psycopg.connect("postgresql://postgres@127.0.0.1:5432/hermes_data").cursor()
cur.execute("SELECT length(content), raw_len, slots, source FROM boshi.memories WHERE id=%s", (mid,))
ln, raw_len, slots, source = cur.fetchone()
print("② 原文长度 %d / raw_len %s / source %s" % (ln, raw_len, source))
print("  content 未截断:", ln == len(LONG))
print("  槽位:", {k: (v[:3] if isinstance(v, list) else v) for k, v in (slots or {}).items()})

# ③ 插件侧 _slots_meta（同一 slots.py）对长用户消息
sl = pg_bridge._slots_mod()
meta = sl.to_meta(sl.extract(LONG), len(LONG))
print("③ 插件口径 to_meta keys:", sorted(meta.keys()), "raw_len:", meta["raw_len"])

# ④ BM25 文档口径未变
print("④ _doc_text 用原文:", pg_bridge._doc_text(LONG, slots) == LONG)

# 清理
cur.execute("DELETE FROM boshi.memories WHERE id=%s", (mid,))
cur.connection.commit()
print("已删除测试行，剩余 test_p2_upgrade:", end=" ")
cur.execute("SELECT count(*) FROM boshi.memories WHERE topic='test_p2_upgrade'")
print(cur.fetchone()[0])
