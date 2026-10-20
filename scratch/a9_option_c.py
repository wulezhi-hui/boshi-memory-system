# -*- coding: utf-8 -*-
"""选项 C 影子：coord 退化修复（保留原始 BM25）+ 弱证据降权。

为什么还要 C：coord 修复后，纯中文 query 的关键词通道靠 **bigram 证据**活着
（主 token 全 df=0，只剩 2 字切片参与 BM25）。bigram 证据比整词证据弱得多，
但 `_score_merge` 仍然按 wk=0.55 等权融合，于是 paraphrase 组出现了「金标被
bigram 噪声挤出 top5」的反向样本（56d59b74）。

C 的做法：`_kw_channel` 在 main_hit_n==0 时给结果打 `_weak=True`；
`_score_merge` 见到弱证据就把 wk 从 0.55 降到 0.30（wv 0.45→0.70），
即"关键词通道降级参与"，而不是"闭嘴"或"等权"。

对比四列：orig(现状) / fix / fix+C / vector
A5（35 条带锚点）做回归，要求 fix+C 与之逐条一致。
"""
import math
import os
import sys

HERE = r"C:/Users/wulezhi/.boshi"
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, "eval"))
os.chdir(HERE)
import pg_bridge
from cases import CASES as A5_CASES
from cases_a9 import CASES as A9_CASES

ORIG_KW = pg_bridge._kw_channel
ORIG_MERGE = pg_bridge._score_merge
WEAK_WV, WEAK_WK = 0.70, 0.30


def patched_kw_channel(c, toks, k, cond, params):
    """原 _kw_channel 复制：coord 只在有主 token 命中时施加；并打 _weak 标记。"""
    if not toks:
        return []
    ent = pg_bridge._kw_index(c, cond, params)
    docs, postings = ent["docs"], ent["postings"]
    if not docs:
        return []
    n = len(docs)
    avgdl = ent["avgdl"] or 1.0
    k1, b = pg_bridge._KW_BM25_K1, pg_bridge._KW_BM25_B
    scores, hits, prim = {}, {}, []
    widf_total = 0.0
    main_hit_n = 0
    for tok, w in toks:
        pl = postings.get(tok)
        df = len(pl) if pl else 0
        idf = math.log(1.0 + (n - df + 0.5) / (df + 0.5))
        if w >= 2.5:
            widf_total += w * idf
            prim.append((tok, w, idf))
            if df:
                main_hit_n += 1
        if not pl:
            continue
        for idx, tf in pl:
            dl = docs[idx][3]
            denom = tf + k1 * (1.0 - b + b * dl / avgdl)
            scores[idx] = scores.get(idx, 0.0) + w * idf * (tf * (k1 + 1.0)) / denom
            if w >= 2.5:
                d = hits.setdefault(idx, {"hit": 0.0, "cov": 0.0})
                d["hit"] += w * idf
                d["cov"] += w
    if not scores:
        return []
    if main_hit_n:
        pr = {i: (hits.get(i) or {}).get("cov", 0.0) for i in scores}
        denom_w = sum(w for _, w in toks if w >= 2.5) or 1.0
        scores = {i: s * (pr[i] / denom_w) for i, s in scores.items()}
    ranked = sorted(scores.items(), key=lambda x: -x[1])[:int(k)]
    if not ranked:
        return []
    top = ranked[0][1] or 1.0
    weak = main_hit_n == 0
    out = []
    for idx, s in ranked:
        rel = s / top
        if rel < pg_bridge._KW_MIN:
            continue
        rid, content, meta, _dl, dtxt = docs[idx]
        it = pg_bridge._row_to_item((rid, content, meta, None))
        it["kw"] = round(rel, 3)
        it["kw_abs"] = round(pg_bridge._kw_abs(dtxt, prim, widf_total, _dl, avgdl), 3)
        it["_weak"] = weak
        out.append(it)
    return out


def patched_score_merge(vec_items, kw_items, top_k, wv=0.45, wk=0.55):
    """原 _score_merge 复制：若关键词结果整体是弱证据，降权。"""
    if kw_items and kw_items[0].get("_weak") and (wv, wk) == (0.45, 0.55):
        wv, wk = WEAK_WV, WEAK_WK
    wsum = (wv + wk) or 1.0
    pool = {}
    for it in vec_items:
        pool.setdefault(it["id"], {"item": it, "v": max(0.0, 1.0 - it["score"]),
                                   "k": 0.0, "ch": []})["ch"].append("vec")
    for it in kw_items:
        d = pool.setdefault(it["id"], {"item": it, "v": 0.0, "k": 0.0, "ch": []})
        if d["v"] == 0.0 and it.get("_cos"):
            d["v"] = max(0.0, float(it["_cos"]))
            d["item"] = it
        d["k"] = float(it.get("kw_abs") or 0.0)
        d["ch"].append("kw")
    out = []
    for d in pool.values():
        f = (wv * d["v"] + wk * d["k"]) / wsum
        it = d["item"]
        it["score"] = round(1.0 - (0.60 + 0.40 * min(f, 1.0)), 6)
        it["channels"] = d["ch"]
        it["fused"] = round(f, 4)
        out.append(it)
    out.sort(key=lambda x: x["score"])
    return out[:int(top_k)]


def set_mode(mode):
    pg_bridge._kw_channel = patched_kw_channel if mode in ("fix", "fixC") else ORIG_KW
    pg_bridge._score_merge = patched_score_merge if mode == "fixC" else ORIG_MERGE


pg_bridge.search_memory("预热 嵌入 模型", top_k=3, fusion="vector", scope="all")

MODES = ("orig", "fix", "fixC", "vector")
res = {}
for kind, gid, anchor, q in A9_CASES:
    for mode in MODES:
        set_mode(mode if mode != "vector" else "orig")
        fusion = "vector" if mode == "vector" else "score"
        items = pg_bridge.search_memory(q, top_k=5, fusion=fusion, scope="all")
        ids = [str(it["id"])[:8] for it in items]
        res.setdefault((kind, q), {})[mode] = (ids.index(gid) + 1) if gid in ids else None
set_mode("orig")

print("=== A9（40 条纯中文，gold 名次）===")
print(f"{'组':11s} {'口径':6s} " + "  ".join(f"{m:>8s}" for m in MODES))
for kind in ("overlap", "paraphrase"):
    sub = {q: v for (k, q), v in res.items() if k == kind}
    for label, fn in (("top1", lambda r: r == 1), ("top3", lambda r: r and r <= 3),
                      ("top5", lambda r: bool(r))):
        cells = [sum(1 for v in sub.values() if fn(v[m])) for m in MODES]
        print(f"{kind:11s} {label:6s} " + "  ".join(f"{c:>8d}" for c in cells))

print("\n=== 逐条差异（fix vs fixC）===")
for (kind, q), v in res.items():
    if v["fix"] != v["fixC"]:
        print(f"  {kind:10s} fix={v['fix']} fixC={v['fixC']} vec={v['vector']}  {q[:32]}")

print("\n=== A5 回归：fixC 与现状逐条（auto/score/rrf）===")
bad = 0
for q, gid, anchor in A5_CASES:
    for fusion in ("auto", "score", "rrf"):
        got = []
        for mode in ("orig", "fixC"):
            set_mode(mode)
            items = pg_bridge.search_memory(q, top_k=5, fusion=fusion, scope="all")
            ids = [str(it["id"])[:8] for it in items]
            got.append((ids.index(gid) + 1) if gid in ids else None)
        if got[0] != got[1]:
            bad += 1
            print(f"  变化 {fusion} {got[0]} → {got[1]}  {q[:34]}")
set_mode("orig")
print(f"  A5 名次变化: {bad}/{len(A5_CASES)*3}")
