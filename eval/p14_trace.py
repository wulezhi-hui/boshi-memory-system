# -*- coding: utf-8 -*-
"""P1④ 逐条解剖：paraphrase 未进 top5 的用例，看 gold 在融合层被谁挤掉

对每条：截住 _score_merge 的输入/输出与 _rerank_items 的输入/输出，
打印 gold 的 (vec分, kw_abs, kw, 名次) 与最终 top5 各条的成分。
用法: ./venv/Scripts/python.exe eval/p14_trace.py [gid8 ...]
"""
import json
import os
import sys

HERE = r"C:/Users/wulezhi/.boshi"
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, "eval"))
os.chdir(HERE)
os.environ.setdefault("BOSHI_FM_STATS", "0")
import pg_bridge
from cases_a9 import CASES

SCOPE = os.environ.get("A9_SCOPE", "all")
ASOF = os.environ.get("A9_ASOF")
WHERE = {"created_at": {"$lte": ASOF}} if ASOF else None

want = set(sys.argv[1:])
cases = [c for c in CASES if c[0] == "paraphrase" and (not want or c[1] in want)]

_orig_sm = pg_bridge._score_merge
_orig_rr = pg_bridge._rerank_items
cap = {}


def sm(vec_items, kw_items, top_k, *a, **kw):
    out = _orig_sm(vec_items, kw_items, top_k, *a, **kw)
    cap["fused"] = out
    cap["nk"] = len(kw_items)
    return out


def rr(query, items, top_k):
    cap["pre_rr"] = list(items)
    out = _orig_rr(query, items, top_k)
    cap["post_rr"] = out
    return out


pg_bridge._score_merge = sm
pg_bridge._rerank_items = rr

for kind, gid, anchor, q in cases:
    cap.clear()
    items = pg_bridge.search_memory(q, top_k=5, fusion="auto", scope=SCOPE,
                                    where=WHERE)
    fused = cap.get("fused") or []
    fids = [str(x["id"])[:8] for x in fused]
    pos = fids.index(gid) + 1 if gid in fids else None
    print("\n" + "=" * 78)
    print(f"{gid}  融合池名次={pos}/{len(fused)}  kw候选={cap.get('nk')}  | {q}")
    if pos:
        g = fused[pos - 1]
        print(f"  GOLD  fused={g.get('fused')} ch={g.get('channels')} "
              f"kw_abs={g.get('kw_abs')} kw={g.get('kw')}")
    # 真实余弦（score 是展示映射后的 0.6~1.0，不能反推）
    ids = [str(x["id"]) for x in fused[:8]]
    cos = {}
    if ids:
        qv = pg_bridge._vec_literal(pg_bridge._ef()([q])[0])
        with pg_bridge._conn().cursor() as cur:
            cur.execute("SELECT id::text, 1-(embedding <=> %s::vector)::float8 "
                        "FROM boshi.memories WHERE id::text = ANY(%s)", (qv, ids))
            cos = {r[0][:8]: round(r[1], 3) for r in cur.fetchall()}
    print("  最终 top5:", [str(x["id"])[:8] for x in items])
    for i, it in enumerate(fused[:8], 1):
        mark = "  <<<GOLD" if str(it["id"])[:8] == gid else ""
        print(f"   {i}. {str(it['id'])[:8]} cos={cos.get(str(it['id'])[:8])} "
              f"fused={it.get('fused')} ch={it.get('channels')} "
              f"kw_abs={it.get('kw_abs')} kw={it.get('kw')}{mark}")
    # 最终名次
    final = [str(x["id"])[:8] for x in items]
    print(f"  → final rank = {final.index(gid)+1 if gid in final else 'MISS'}")
