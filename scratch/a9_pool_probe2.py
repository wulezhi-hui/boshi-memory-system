# -*- coding: utf-8 -*-
"""A9 失败归因 v2：把线上真实的融合池（pool_k=20 / k_kw=20）复现出来，
看 gold 是「不在池里」（召回问题）还是「在池里但排不进 top5」（排序问题）。

用法: venv/Scripts/python.exe scratch/a9_pool_probe2.py
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

TOP_K = 5
POOL = int(TOP_K) * max(1, pg_bridge._FM_POOL)
K_EACH = int(min(max(TOP_K * 4, 20), 100))
K_KW = min(K_EACH, pg_bridge._KW_POOL_CAP) if pg_bridge._KW_POOL_CAP > 0 else K_EACH
print(f"POOL={POOL} K_EACH={K_EACH} K_KW={K_KW} FM_POOL={pg_bridge._FM_POOL}")

rows = []
for kind, gid, anchor, q in CASES:
    qv = pg_bridge._vec_literal(pg_bridge._ef()([q])[0])
    vec_items = pg_bridge._vec_channel(c, q, max(K_EACH, POOL), cond, params, v=qv)
    toks = pg_bridge._kw_tokens(q)
    kw_items = pg_bridge._kw_channel(c, toks, K_KW, cond, params)
    seen = {x["id"] for x in vec_items}
    miss = [str(x["id"]) for x in kw_items if x["id"] not in seen]
    cmap = pg_bridge._cosine_for_ids(c, qv, miss)
    for it in kw_items:
        if it["id"] not in seen:
            it["_cos"] = cmap.get(str(it["id"]), 0.0)
    merged = pg_bridge._score_merge(list(vec_items), list(kw_items), POOL)
    mids = [str(x["id"])[:8] for x in merged]
    # 真实前 5（线上输出）
    top5 = [str(x["id"])[:8] for x in
            pg_bridge.search_memory(q, top_k=TOP_K, fusion="auto", scope=SCOPE, where=WHERE) or []]

    def rank(lst):
        return lst.index(gid) + 1 if gid in lst else None

    vrank = next((i + 1 for i, it in enumerate(vec_items) if str(it["id"])[:8] == gid), None)
    krank = next((i + 1 for i, it in enumerate(kw_items) if str(it["id"])[:8] == gid), None)
    f20 = rank(mids)
    f5 = rank(top5)
    rec = merged[f20 - 1] if f20 else None
    cos = (1.0 - rec["score"] - 0.0) if False else None
    rows.append(dict(kind=kind, gid=gid, q=q, vrank=vrank, krank=krank,
                     f20=f20, f5=f5, pool_n=len(merged),
                     fused=(rec.get("fused") if rec else None),
                     ch=(rec.get("channels") if rec else None)))

miss_pool = [r for r in rows if r["kind"] == "paraphrase" and r["f20"] is None]
in_pool = [r for r in rows if r["kind"] == "paraphrase" and r["f20"] and not r["f5"]]
print(f"\nparaphrase 20 条：池内排不进 top5 = {len(in_pool)}，根本不在池里 = {len(miss_pool)}")
print(f"\n{'kind':11s} {'gold':9s} {'f5':>4s} {'f20':>4s} {'vec':>4s} {'kw':>4s} {'fused':>7s}  {'ch':16s} q")
for r in rows:
    if r["kind"] == "paraphrase" or (r["kind"] == "overlap" and not r["f5"]):
        print(f"{r['kind']:11s} {r['gid']:9s} {str(r['f5']):>4s} {str(r['f20']):>4s} "
              f"{str(r['vrank']):>4s} {str(r['krank']):>4s} {str(r['fused']):>7s}  "
              f"{str(r['ch']):16s} {r['q']}")

json.dump(rows, open(f"{HERE}/logs/a9_pool_probe2.json", "w"), ensure_ascii=False, indent=1)
