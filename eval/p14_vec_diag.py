# -*- coding: utf-8 -*-
"""P1④ 表示层诊断：改写型 query 的 gold 在两路里各排第几（不受池上限影响）

目的：把 paraphrase 的失分归因到「表示层(嵌入)」还是「融合侧截断(cap)」。
  · vec_rank  = 全库 is_latest 里比 gold 更近的条数 + 1（精确名次，不截断）
  · kw_rank   = BM25 全序里 gold 的名次（k 给足，不受 _KW_POOL_CAP 影响）
  · in_pool   = gold 是否落在线上真实候选池(top_k=5 → vec 200 / kw 20) 内

用法: ./venv/Scripts/python.exe eval/p14_vec_diag.py [tag]
输出: logs/p14_<tag>.json + 屏幕表格
"""
import json
import os
import sys
import time

HERE = r"C:/Users/wulezhi/.boshi"
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, "eval"))
os.chdir(HERE)
os.environ.setdefault("BOSHI_FM_STATS", "0")
import pg_bridge
from cases_a9 import CASES

tag = sys.argv[1] if len(sys.argv) > 1 else "run"
SCOPE = os.environ.get("A9_SCOPE", "all")
ASOF = os.environ.get("A9_ASOF")
WHERE = {"created_at": {"$lte": ASOF}} if ASOF else None

c = pg_bridge._conn()
cond, params = pg_bridge._where_sql(
    dict(WHERE or {}, isLatest=True), SCOPE, pg_bridge._resolve_profile(), False)
if ASOF:
    tc, tp = pg_bridge._time_bounds(None, None)   # where 已带，走 cond 即可
print("cond:", cond)

# gold 正文
gid_body = {}
with c.cursor() as cur:
    for _, gid, _a, _q in CASES:
        if gid in gid_body:
            continue
        cur.execute("""SELECT content FROM boshi.memories
                       WHERE is_latest AND left(id::text,8)=%s LIMIT 1""", (gid,))
        r = cur.fetchall()
        gid_body[gid] = r[0][0] if r else None

KW_K = 5000
rows = []
seen = set()
for kind, gid, anchor, q in CASES:
    if kind != "paraphrase":
        continue
    qv = pg_bridge._vec_literal(pg_bridge._ef()([q])[0])
    # 精确名次：比 gold 更近的条数
    with c.cursor() as cur:
        cur.execute(f"""SELECT count(*) FROM boshi.memories
                        WHERE {cond} AND embedding <=> %s::vector < (
                          SELECT embedding <=> %s::vector FROM boshi.memories
                          WHERE is_latest AND left(id::text,8)=%s LIMIT 1)""",
                    tuple(params) + (qv, qv, gid))
        vec_rank = cur.fetchone()[0] + 1
    toks = pg_bridge._kw_tokens(q)
    kw_items = pg_bridge._kw_channel(c, toks, KW_K, cond, params)
    kids = [str(x["id"])[:8] for x in kw_items]
    kw_rank = (kids.index(gid) + 1) if gid in kids else None
    rec = {"q": q, "gold": gid, "vec_rank": vec_rank, "kw_rank": kw_rank,
           "in_vec_pool": vec_rank <= 200, "in_kw_pool": bool(kw_rank and kw_rank <= 20),
           "n_kw_hits": len(kw_items)}
    rows.append(rec)

print(f"\n{'gold':9s} {'vec':>6s} {'kw':>6s}  归因  query")
stat = {"vec_miss": 0, "kw_cap_only": 0, "both_far": 0, "in_pool": 0}
for r in rows:
    if r["vec_rank"] <= 200 and r["in_kw_pool"]:
        att, stat["in_pool"] = "池内", stat["in_pool"] + 1
    elif r["vec_rank"] <= 200 and (r["kw_rank"] or 10**9) > 20:
        att, stat["kw_cap_only"] = "kw被cap截断", stat["kw_cap_only"] + 1
    elif r["vec_rank"] > 200 and (r["kw_rank"] or 10**9) <= 20:
        att, stat["kw_cap_only"] = "vec被池截断", stat["kw_cap_only"] + 1
    else:
        att, stat["both_far"] = "两路都远→表示层", stat["both_far"] + 1
    kwt = r["kw_rank"] if r["kw_rank"] is not None else -1
    print(f"{r['gold']:9s} {r['vec_rank']:6d} {kwt:6d}  {att:14s} {r['q'][:34]}")

print("\n归因汇总:", stat, " n =", len(rows))
out = {"tag": tag, "ts": time.strftime("%Y-%m-%d %H:%M:%S"), "asof": ASOF,
       "scope": SCOPE, "stat": stat, "cases": rows}
p = os.path.join(HERE, "logs", "p14_%s.json" % tag)
with open(p, "w", encoding="utf-8") as f:
    json.dump(out, f, ensure_ascii=False, indent=1)
print("写入", p)
