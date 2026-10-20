# -*- coding: utf-8 -*-
"""A9 候选池尺寸 × 融合式联合扫描（离线，单进程）。

动机：`scratch/a9_recall_probe.py` 显示 paraphrase 的失败分两类
1. 融合丢金标：8b027120(kw=1)、eec4f527(kw=5)、0d28d510(13)、f6c71ce8(10)
   —— 关键词通道名次很好，但 `_score_merge` 只读 kw_abs，绝对覆盖率低就被顶掉。
2. 池子截断：d13aed83(kw=61)、bc695685(kw=73)、89a147e0(27)
   —— 已在 kw 通道里，但 `BOSHI_KW_POOL_CAP=20` 把尾巴切了。

对 (k_vec, k_kw) 网格 × 融合式做联合评估，全部用线上同一套候选/打分代码。

用法: python scratch/a9_pool_scan.py [asof]
"""
import itertools
import json
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
POOL = 15
WV, WK = 0.70, 0.30

c = pg_bridge._conn()
cond, params = pg_bridge._where_sql(dict(WHERE, isLatest=True), "all",
                                    pg_bridge._resolve_profile(), False)

QUERIES = []
for kind, gid, anchor, q in CASES:
    qv = pg_bridge._vec_literal(pg_bridge._ef()([q])[0])
    QUERIES.append({"kind": kind, "gold": gid, "q": q, "qv": qv,
                    "toks": pg_bridge._kw_tokens(q)})

_cache = {}


def pool_for(kv, kk):
    """取 (vec top kv, kw top kk) 并给 kw 独有文档补真实余弦；按 (kv,kk) 缓存。"""
    key = (kv, kk)
    if key in _cache:
        return _cache[key]
    out = []
    for e in QUERIES:
        vit = pg_bridge._vec_channel(c, e["q"], kv, cond, params, v=e["qv"])
        kit = (pg_bridge._kw_channel(c, e["toks"], kk, cond, params)
               if e["toks"] else [])
        seen = {x["id"] for x in vit}
        miss = [str(x["id"]) for x in kit if x["id"] not in seen]
        cmap = pg_bridge._cosine_for_ids(c, e["qv"], miss) if miss else {}
        for it in kit:
            if it["id"] not in seen:
                it["_cos"] = cmap.get(str(it["id"]), 0.0)
        out.append(dict(e, vec=vit, kw=kit))
    _cache[key] = out
    return out


def fuse(vec_items, kw_items, b, weak_only=True):
    qweak = any(x.get("_weak") for x in kw_items)
    boost = b if (qweak or not weak_only) else 0.0
    pool, vids = {}, set()
    for it in vec_items:
        vids.add(it["id"])
        pool[it["id"]] = {"item": it, "v": max(0.0, 1.0 - it["score"]),
                          "k": 0.0, "krel": 0.0}
    for it in kw_items:
        d = pool.get(it["id"])
        if d is None:
            d = pool[it["id"]] = {"item": it, "v": max(0.0, float(it.get("_cos") or 0.0)),
                                  "k": 0.0, "krel": 0.0}
        d["k"] = float(it.get("kw_abs") or 0.0) + boost * float(it.get("kw") or 0.0)
        d["krel"] = float(it.get("kw") or 0.0)
    rated = []
    for d in pool.values():
        f = (WV * d["v"] + WK * d["k"]) / (WV + WK)
        it = d["item"]
        it["_f"] = f
        rated.append(it)
    rated.sort(key=lambda x: -x["_f"])
    return rated[:POOL]


def score(pools, b, weak_only=True):
    st = {"overlap": [0, 0, 0], "paraphrase": [0, 0, 0]}
    rows = []
    for e in pools:
        ids = [str(x["id"])[:8] for x in fuse(e["vec"], e["kw"], b, weak_only)]
        r = ids.index(e["gold"]) + 1 if e["gold"] in ids else None
        rows.append((e["kind"], e["gold"], r))
        s = st[e["kind"]]
        s[0] += (r == 1)
        s[1] += bool(r and r <= 3)
        s[2] += bool(r)
    return st, rows


print("(kv,kk)  b     overlap      para        合计")
results = []
for kv, kk in [(20, 20), (20, 40), (20, 60), (20, 100), (40, 40), (40, 100),
               (10, 20), (10, 40), (60, 60)]:
    pools = pool_for(kv, kk)
    for b in (0.0, 0.4):
        st, rows = score(pools, b)
        ov, pp = st["overlap"], st["paraphrase"]
        tot = [ov[i] + pp[i] for i in range(3)]
        print(f"({kv:2d},{kk:3d}) {b:.1f}  {ov[0]:2d}/{ov[1]:2d}/{ov[2]:2d}   "
              f"{pp[0]:2d}/{pp[1]:2d}/{pp[2]:2d}   {tot[0]:2d}/{tot[1]:2d}/{tot[2]:2d}")
        results.append({"kv": kv, "kk": kk, "b": b, "tot": tot,
                        "ov": ov, "pp": pp,
                        "rows": [[k, g, r] for k, g, r in rows]})

best = max(results, key=lambda x: (x["tot"][0], x["tot"][1], x["tot"][2]))
print(f"\n最佳：(kv={best['kv']}, kk={best['kk']}, b={best['b']}) → {best['tot']} "
      f"ov={best['ov']} pp={best['pp']}")
json.dump(results, open("logs/a9_pool_scan.json", "w"), ensure_ascii=False, indent=1)
print("→ logs/a9_pool_scan.json")
