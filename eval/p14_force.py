# -*- coding: utf-8 -*-
"""P1④ 天花板实验：把 gold 强行塞进候选池，看交叉编码器认不认

问题：gold 没进 top5，是「检索没捞到（池/表示层）」还是「捞到了但排序没认」？
做法：候选 = 向量 topN ∪ kw topN ∪ {gold}，直接喂 _rerank_items，打印 gold 名次。
用法: ./venv/Scripts/python.exe eval/p14_force.py [rp=50]
"""
import os
import sys

HERE = r"C:/Users/wulezhi/.boshi"
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, "eval"))
os.chdir(HERE)
os.environ.setdefault("BOSHI_FM_STATS", "0")
import pg_bridge
from cases_a9 import CASES

RP = int(sys.argv[1]) if len(sys.argv) > 1 else 50
SCOPE = os.environ.get("A9_SCOPE", "all")
ASOF = os.environ.get("A9_ASOF")
WHERE = {"created_at": {"$lte": ASOF}} if ASOF else None

CAP = {"fused": []}
_orig_sm = pg_bridge._score_merge


def sm(vec_items, kw_items, top_k, *a, **kw):
    out = _orig_sm(vec_items, kw_items, top_k, *a, **kw)
    CAP["fused"] = out
    return out


pg_bridge._score_merge = sm

print(f"{'gold':10s} {'池内':>5s} {'+gold重排':>9s}  {'rr_score':>8s}")
for kind, gid, anchor, q in [c for c in CASES if c[0] == "paraphrase"]:
    CAP["fused"] = []
    pg_bridge.search_memory(q, top_k=5, fusion="auto", scope=SCOPE, where=WHERE)
    fused = CAP["fused"]
    ids = [str(x["id"])[:8] for x in fused]
    inpool = ids.index(gid) + 1 if gid in ids else None
    gold_item = None
    with pg_bridge._conn().cursor() as cur:
        cur.execute("SELECT id::text, content, meta FROM boshi.memories "
                    "WHERE id::text LIKE %s LIMIT 1", (gid + "%",))
        r = cur.fetchone()
        if r:
            gold_item = pg_bridge._row_to_item((r[0], r[1], r[2], None))
    if not gold_item:
        print(f"{gid:10s}   gold 行不存在")
        continue
    cand = list(fused[:RP])
    if gid not in [str(x["id"])[:8] for x in cand]:
        cand = cand + [gold_item]
    rr = pg_bridge._rerank_items(q, cand, len(cand))
    rids = [str(x["id"])[:8] for x in rr]
    pos = rids.index(gid) + 1 if gid in rids else None
    score = next((x.get("rerank_score") for x in rr if str(x["id"])[:8] == gid), None)
    print(f"{gid:10s} {str(inpool):>5s} {str(pos):>9s}  {str(score):>8s}  n={len(cand)}  {q[:24]}")
