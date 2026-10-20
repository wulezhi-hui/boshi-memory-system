# -*- coding: utf-8 -*-
"""弱证据融合：把「关键词通道名次」这个免费信号用起来（离线网格）。

诊断（scratch/a9_recall_probe.py）：paraphrase 的 11 条里，有 4 条在关键词通道
已经排到 top-5（8b027120=1、eec4f527=5、b74d2c87=6、f6c71ce8=10）却在融合后
不见踪影。原因：`_score_merge` 只读 `kw_abs`（绝对 idf 覆盖率），而这些 gold
的**相对**名次很好、绝对覆盖率低（它只有几个 bigram 对上），于是输给「向量近
但字面完全不沾」的候选。

本探针离线复刻线上候选池（vec top20 / kw top20 → 池 15），比较几种用上
「名次」的融合式。候选池与线上一致，结论才可迁移。

用法: python scratch/a9_fuse_rank_scan.py [asof]
"""
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
POOL = 15          # 线上 pool_k = top_k(5) × _FM_POOL(3)
WV, WK = 0.70, 0.30

c = pg_bridge._conn()
cond, params = pg_bridge._where_sql(dict(WHERE, isLatest=True), "all",
                                    pg_bridge._resolve_profile(), False)


def fuse(vec_items, kw_items, mode, p: dict):
    """复刻 _score_merge 的池构建，换不同的 k 值口径。"""
    pool = {}
    for it in vec_items:
        pool.setdefault(it["id"], {"item": it, "v": max(0.0, 1.0 - it["score"]),
                                   "k": 0.0, "krel": 0.0, "kr": None,
                                   "invec": False})["v"] = \
            max(0.0, 1.0 - it["score"])
    for it in vec_items:
        pool[it["id"]]["invec"] = True
    for r, it in enumerate(kw_items, 1):
        d = pool.setdefault(it["id"], {"item": it, "v": 0.0, "k": 0.0,
                                       "krel": 0.0, "kr": None, "invec": False})
        if it["id"] not in {x["id"] for x in vec_items}:
            d["v"] = max(0.0, float(it.get("_cos") or 0.0))
            d["item"] = it
        d["k"] = float(it.get("kw_abs") or 0.0)
        d["krel"] = float(it.get("kw") or 0.0)
        d["kr"] = r
    out = []
    qweak = any(x.get("_weak") for x in kw_items)
    for d in pool.values():
        kabs, krel, kr = d["k"], d["krel"], d["kr"]
        if mode == "base":
            k = kabs
        elif mode == "max_rel":
            k = max(kabs, p["b"] * krel)
        elif mode == "max_rel_weak":
            k = max(kabs, p["b"] * krel) if qweak else kabs
        elif mode == "blend":
            k = kabs + p["b"] * krel
        elif mode == "blend_weak":
            k = kabs + (p["b"] * krel if qweak else 0.0)
        elif mode == "blend_topk":
            k = kabs + (p["b"] * krel if (kr and kr <= p["m"]) else 0.0)
        elif mode == "blend_kwonly":
            # 只补「向量通道完全没进池」的候选：它们本来就拿不到向量名次
            k = kabs + (p["b"] * krel if (kr and not d["invec"]) else 0.0)
        elif mode == "rank_bonus":
            k = kabs + (p["b"] / kr if kr else 0.0)
        elif mode == "rel_topk":
            # 关键词通道名次进 top-M 才拿相对分，避免长尾相对分虚高
            k = max(kabs, p["b"] * krel) if (kr and kr <= p["m"]) else kabs
        else:
            raise SystemExit(mode)
        f = (WV * d["v"] + WK * k) / (WV + WK)
        it = d["item"]
        it["_f"] = f
        out.append(it)
    out.sort(key=lambda x: -x["_f"])
    return out[:POOL]


VARIANTS = [
    ("base", {}),
    ("blend_weak", {"b": 0.3}),
    ("blend_topk", {"b": 0.4, "m": 3}), ("blend_topk", {"b": 0.4, "m": 5}),
    ("blend_topk", {"b": 0.6, "m": 5}), ("blend_topk", {"b": 0.8, "m": 5}),
    ("blend_kwonly", {"b": 0.3}), ("blend_kwonly", {"b": 0.5}),
    ("blend_kwonly", {"b": 0.8}), ("blend_weak", {"b": 0.4}),
    ("blend_weak", {"b": 0.5}), ("blend_weak", {"b": 0.6}),
    ("max_rel_weak", {"b": 0.5}), ("max_rel_weak", {"b": 0.7}),
    ("max_rel", {"b": 0.5}), ("max_rel", {"b": 0.7}), ("max_rel", {"b": 0.9}),
    ("blend", {"b": 0.2}), ("blend", {"b": 0.4}),
    ("rank_bonus", {"b": 0.3}), ("rank_bonus", {"b": 0.6}),
    ("rel_topk", {"b": 0.8, "m": 3}), ("rel_topk", {"b": 0.9, "m": 5}),
    ("rel_topk", {"b": 0.9, "m": 10}),
]

# 预取候选池（每 query 只算一次，各变体共用）
cache = []
for kind, gid, anchor, q in CASES:
    qv = pg_bridge._vec_literal(pg_bridge._ef()([q])[0])
    vit = pg_bridge._vec_channel(c, q, 20, cond, params, v=qv)
    toks = pg_bridge._kw_tokens(q)
    kit = pg_bridge._kw_channel(c, toks, 20, cond, params) if toks else []
    seen = {x["id"] for x in vit}
    miss = [str(x["id"]) for x in kit if x["id"] not in seen]
    cmap = pg_bridge._cosine_for_ids(c, qv, miss) if miss else {}
    for it in kit:
        if it["id"] not in seen:
            it["_cos"] = cmap.get(str(it["id"]), 0.0)
    cache.append({"kind": kind, "gold": gid, "q": q,
                  "vec": vit, "kw": kit})

print(f"{'variant':22s} {'overlap':>10s} {'para':>10s} {'合计':>10s}")
ranks = {}
best = None
for mode, p in VARIANTS:
    st = {"overlap": [0, 0, 0], "paraphrase": [0, 0, 0]}
    det = []
    for e in cache:
        ranked = fuse(e["vec"], e["kw"], mode, p)
        ids = [str(x["id"])[:8] for x in ranked]
        r = ids.index(e["gold"]) + 1 if e["gold"] in ids else None
        s = st[e["kind"]]
        if r == 1:
            s[0] += 1
        if r and r <= 3:
            s[1] += 1
        if r:
            s[2] += 1
        det.append((e["gold"], e["kind"], r))
        ranks.setdefault(mode + (f" {p}" if p else ""), {})[(e["gold"], e["kind"])] = r
    ov, pp = st["overlap"], st["paraphrase"]
    tot = [ov[i] + pp[i] for i in range(3)]
    name = mode + (f" {p}" if p else "")
    print(f"{name:22s} {ov[0]:2d}/{ov[1]:2d}/{ov[2]:2d}  {pp[0]:2d}/{pp[1]:2d}/{pp[2]:2d}  "
          f"{tot[0]:2d}/{tot[1]:2d}/{tot[2]:2d}")
    key = (tot[0], tot[1], tot[2])
    if best is None or key > best[0]:
        best = (key, name, det)

print(f"\n最佳：{best[1]} → {best[0]}")
print("逐条（<5 名才算改善）:")
for g, k, r in best[2]:
    print(f"  {k[:4]} {g} {r}")

print("\n=== 与 base 的 overlap 名次差异（找退化源）===")
b = ranks.get("base", {})
for name in ("blend_weak {'b': 0.3}", "blend_topk {'b': 0.4, 'm': 5}",
             "blend_kwonly {'b': 0.5}"):
    rr = ranks.get(name)
    if not rr:
        continue
    diffs = [(g, b[k], rr[k]) for k in rr
             if k[1] == "overlap" and b.get(k) != rr[k] for g in [k[0]]]
    print(f"{name}: {[(g, f'{x}->{y}') for g, x, y in diffs] or 'overlap 名次不变'}")
