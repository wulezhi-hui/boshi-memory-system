# -*- coding: utf-8 -*-
"""P2+ 定向验收：构造"查询里带字面量(端口/版本/开关)"的 gold 集，测字段级匹配是否有效。

A5 的 35 条 gold 里几乎没有字面量查询（扫描显示 ports/versions/flags/files 四类在 auto 下
命中的次数为 0，只有 numbers 触发且净收益 0）——所以 A5 无法评价本特性。
本脚本自动从库里挑出 slots 含 ports/versions/flags 的记忆，用「字面量 + 该条内容里的 2~3 个关键词」
拼成查询（语义弱、字面量强，正是字段级匹配的目标场景），gold = 该条记忆 id。

用法: python eval/a8_field_gold.py  -> logs/a8_field_gold.json
"""
import json
import os
import re
import sys

HERE = r"C:/Users/wulezhi/.boshi"
sys.path.insert(0, HERE)
os.chdir(HERE)

import pg_bridge

STOP = set("""的 了 是 在 我 你 他 就 都 和 也 但 而 与 及 或 这 那 有 没 不 要 可以 一个 什么 怎么 如何
现在 还是 然后 因为 所以 如果 已经 一下 就是 不是 这个 那个 我们 你们 他们 需要 应该 请问
the a an is are was were to of in on at for with and or not be it this that""".split())

CASES = []
c = pg_bridge._conn() if hasattr(pg_bridge, "_conn") else None


def get_conn():
    global c
    if c is None:
        c = pg_bridge._conn()
    return c


def tokens(text, exclude):
    out = []
    for t in re.split(r"[\s，。、；：！？,.;:!?()（）\[\]{}<>|/\\\"'`]+", text):
        t = t.strip("-—–*=#*`~ ")
        if len(t) < 2 or t in STOP or t == exclude or exclude in t:
            continue
        if re.fullmatch(r"[\d.\-–~]+", t):
            continue
        out.append(t)
        if len(out) >= 3:
            break
    return out


for slot, limit in (("ports", 14), ("versions", 14), ("flags", 12)):
    with get_conn().cursor() as cur:
        cur.execute(
            """SELECT id::text, content, slots
                 FROM boshi.memories
                WHERE slots ? %s AND jsonb_array_length(slots->%s) > 0
                  AND length(content) BETWEEN 40 AND 3000
             ORDER BY id DESC LIMIT %s""",
            (slot, slot, limit))
        rows = cur.fetchall()
    for cid, content, slots in rows:
        vals = slots.get(slot) or []
        lit = str(vals[0])
        kws = tokens(content, lit)
        if not kws:
            continue
        q = f"{lit} {' '.join(kws)}"
        CASES.append((q, cid[:8], slot, lit))
    print(f"{slot}: {len([x for x in CASES if x[2] == slot])} 条")

os.environ.pop("BOSHI_FIELD_MATCH", None)


def run(tag):
    st = {"top1": 0, "top3": 0, "top5": 0}
    miss = []
    for q, gold, slot, lit in CASES:
        items = pg_bridge.search_memory(q, top_k=5, fusion="auto", scope="all")
        ids = [str(it["id"])[:8] for it in items]
        rank = (ids.index(gold) + 1) if gold in ids else None
        if rank == 1:
            st["top1"] += 1
        if rank and rank <= 3:
            st["top3"] += 1
        if rank:
            st["top5"] += 1
        else:
            miss.append((q, gold, slot))
    n = len(CASES)
    print(f"{tag:16s} n={n} top1={st['top1']}/{n} top3={st['top3']} top5={st['top5']}")
    return st, miss


base_st, base_miss = run("fm off")
os.environ["BOSHI_FIELD_MATCH"] = "1"
fm_st, fm_miss = run("fm on")
for m in fm_miss[:8]:
    print("  miss:", m)
print("\n逐条转好/变差：")
for (q, gold, slot, lit), b, f in zip(CASES, [], []):
    pass
json.dump({"n": len(CASES), "off": base_st, "on": fm_st,
           "cases": [{"q": q, "gold": g, "slot": s, "lit": l} for q, g, s, l in CASES]},
          open(os.path.join(HERE, "logs", "a8_field_gold.json"), "w", encoding="utf-8"),
          ensure_ascii=False, indent=1)
