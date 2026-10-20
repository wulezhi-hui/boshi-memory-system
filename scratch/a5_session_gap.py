# -*- coding: utf-8 -*-
"""A5 分析：auto 未命中的用例里，有多少 gold 与某个 top5 命中「同 session」。
若占比高 → 说明"用户提问命中、结论没被召回"是主因，可用同 session 扩展修。"""
import json
import psycopg

c = psycopg.connect("postgresql://postgres@127.0.0.1:5432/hermes_data")
cur = c.cursor()

d = json.load(open(r"C:/Users/wulezhi/.boshi/logs/a5_p2_upgrade.json", encoding="utf-8"))
miss = [x for x in d["cases"] if (x.get("auto") or {}).get("rank") != 1]
print("auto 未 top1 的用例:", len(miss))

same_sess = 0
detail = []
for x in miss:
    gid = x["gold"]
    cur.execute("SELECT session_id::text, role, topic FROM boshi.memories WHERE left(id::text,8)=%s", (gid,))
    g = cur.fetchone()
    if not g:
        continue
    gsid, grole, gtopic = g
    hits = (x.get("auto") or {}).get("ids") or []
    if gsid:
        cur.execute("""SELECT count(*) FROM boshi.memories
                       WHERE is_latest AND session_id::text=%s AND left(id::text,8) = ANY(%s)""",
                    (gsid, hits))
        k = cur.fetchone()[0]
    else:
        k = 0
    if k:
        same_sess += 1
    detail.append((x["q"][:34], grole, gtopic, bool(gsid), k))

print("gold 有 session_id 且 top5 里有同 session 伙伴:", same_sess)
print("--- 明细 (query, gold_role, gold_topic, 有sid, 同session命中数) ---")
for t in detail:
    print(t)
