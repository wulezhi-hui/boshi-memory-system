# -*- coding: utf-8 -*-
"""A9 失败归因：每条 gold 在两个通道的**全深度**名次 + 融合名次。

目的：区分「通道里根本没有」与「通道有但被 pool/cap 截断」，再决定池结构怎么改。
用法: venv/Scripts/python.exe scratch/a9_pool_probe.py
"""
import json, os, sys

HERE = r"C:/Users/wulezhi/.boshi"
sys.path.insert(0, HERE); sys.path.insert(0, os.path.join(HERE, "eval"))
os.chdir(HERE)
os.environ.setdefault("BOSHI_FM_STATS", "0")
import pg_bridge
from cases_a9 import CASES

ASOF = 1792461898.0
WHERE = {"created_at": {"$lte": ASOF}}
SCOPE = "all"

c = pg_bridge._conn()
cond, params = pg_bridge._where_sql({**WHERE, "isLatest": True}, SCOPE, "lezhi", False)

DEEP = 300
rows = []
for kind, gid, anchor, q in CASES:
    qv = pg_bridge._vec_literal(pg_bridge._ef()([q])[0])
    # 向量通道全深度
    with c.cursor() as cur:
        cur.execute(
            f"SELECT left(id::text,8), embedding <=> %s::vector AS d FROM boshi.memories "
            f"WHERE {cond} ORDER BY d LIMIT %s", (qv, *params, DEEP))
        vr = cur.fetchall()
    vrank = next((i + 1 for i, (g, _) in enumerate(vr) if g == gid), None)
    vcos = next((1.0 - d for g, d in vr if g == gid), None)
    # 关键词通道全深度
    toks = pg_bridge._kw_tokens(q)
    kw_items = pg_bridge._kw_channel(c, toks, DEEP, cond, params)
    krank = next((i + 1 for i, it in enumerate(kw_items) if str(it["id"])[:8] == gid), None)
    kwabs = next((it.get("kw_abs") for it in kw_items if str(it["id"])[:8] == gid), None)
    # 融合名次（线上口径 top_k=5）
    hit = pg_bridge.search_memory(q, top_k=5, fusion="auto", scope=SCOPE, where=WHERE)
    ids = [str(x["id"])[:8] for x in (hit or [])]
    frank = ids.index(gid) + 1 if gid in ids else None
    rows.append(dict(kind=kind, gid=gid, q=q, vrank=vrank, vcos=round(vcos, 4) if vcos else None,
                     krank=krank, kwabs=round(kwabs, 4) if kwabs is not None else None,
                     frank=frank, kw_chan_len=len(kw_items)))

print(f"{'kind':11s} {'gold':10s} {'fused':>5s} {'vec_r':>6s} {'cos':>7s} {'kw_r':>5s} {'kw_abs':>7s}  q")
for r in rows:
    if r["frank"] and r["frank"] <= 5 and r["kind"] == "overlap":
        continue   # overlap 已中的不看
    print(f"{r['kind']:11s} {r['gid']:10s} {str(r['frank']):>5s} {str(r['vrank']):>6s} "
          f"{str(r['vcos']):>7s} {str(r['krank']):>5s} {str(r['kwabs']):>7s}  {r['q']}")

json.dump(rows, open(f"{HERE}/logs/a9_pool_probe.json", "w"), ensure_ascii=False, indent=1)
print("\n→ logs/a9_pool_probe.json")
