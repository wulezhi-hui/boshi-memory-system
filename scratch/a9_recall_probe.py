# -*- coding: utf-8 -*-
"""A9 paraphrase 短板定位：是「召回不到」还是「排不上来」。

对每条 query 分别看两路深池（各 200）里 gold 名次：
  vec_rank   = 纯向量深池名次（None = 200 内都没有）
  kw_rank    = 纯关键词深池名次
  fuse_rank  = 现行 auto 的实排名（top5 内）

若 vec_rank 远超 top5 但池内存在 → 排序/融合问题（可救）
若 vec_rank None 且 kw_rank None → 真召回缺失（要改表示/切块，不是调权）

用法: python scratch/a9_recall_probe.py [asof]
"""
import os
import sys

HERE = r"C:/Users/wulezhi/.boshi"
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, "eval"))
os.chdir(HERE)
import pg_bridge
from cases_a9 import CASES

ASOF = sys.argv[1] if len(sys.argv) > 1 else "1792461898"
WHERE = {"created_at": {"$lte": ASOF}}
DEEP = 200

c = pg_bridge._conn()
cond, params = pg_bridge._where_sql(dict(WHERE, isLatest=True), "all",
                                    pg_bridge._resolve_profile(), False)

print(f"{'kind':10s} {'gold':9s} {'vec':>5s} {'kw':>5s} {'fuse':>5s}  query")
bucket = {"recall_dead": [], "vec_deep_only": [], "kw_only": [], "near": []}
for kind, gid, anchor, q in CASES:
    qv = pg_bridge._vec_literal(pg_bridge._ef()([q])[0])
    vit = pg_bridge._vec_channel(c, q, DEEP, cond, params, v=qv)
    vids = [str(x["id"])[:8] for x in vit]
    vrank = vids.index(gid) + 1 if gid in vids else None
    toks = pg_bridge._kw_tokens(q)
    kit = pg_bridge._kw_channel(c, toks, DEEP, cond, params) if toks else []
    kids = [str(x["id"])[:8] for x in kit]
    krank = kids.index(gid) + 1 if gid in kids else None
    fit = pg_bridge.search_memory(q, top_k=5, fusion="auto", scope="all", where=WHERE)
    fids = [str(x["id"])[:8] for x in fit]
    frank = fids.index(gid) + 1 if gid in fids else None

    if vrank is None and krank is None:
        tag = "recall_dead"
    elif vrank and vrank <= 20 and not frank:
        tag = "near"
    elif vrank is None or vrank > 20:
        tag = "vec_deep_only" if krank is None else "kw_only"
    else:
        tag = "near"
    bucket[tag].append((gid, kind, vrank, krank, frank))
    print(f"{kind:10s} {gid:9s} {str(vrank or '-'):>5s} {str(krank or '-'):>5s} "
          f"{str(frank or '-'):>5s}  {q[:34]}")

print("\n=== 分桶 ===")
for k, v in bucket.items():
    print(f"{k:14s} {len(v):2d}  " + " ".join(f"{g}({kind[:4]})" for g, kind, *_ in v))

pp = [x for x in bucket["recall_dead"] + bucket["vec_deep_only"] + bucket["kw_only"]
      + bucket["near"] if x[1] == "paraphrase"]
print(f"\nparaphrase 共 {sum(1 for x in CASES if True)}...")
for tag, v in bucket.items():
    p = [x for x in v if x[1] == "paraphrase"]
    if p:
        print(f"  paraphrase in {tag}: {len(p)} -> " +
              " ".join(f"{g}(v={vr},k={kr},f={fr})" for g, _, vr, kr, fr in p))
