# -*- coding: utf-8 -*-
"""融合策略离线扫参：一次取深候选，然后在内存里跑多种融合，比 top1/top3/top5。

A9（40 条，纯中文，分 overlap / paraphrase）× A5（35 条带锚点）双回归。
用法: venv/Scripts/python.exe eval/sweep_fuse.py
"""
import json, math, os, sys

HERE = r"C:/Users/wulezhi/.boshi"
sys.path.insert(0, HERE); sys.path.insert(0, os.path.join(HERE, "eval"))
os.chdir(HERE)
os.environ.setdefault("BOSHI_FM_STATS", "0")
import pg_bridge
from cases import CASES as A5
from cases_a9 import CASES as A9

ASOF = 1792461898.0
SCOPE = "all"
DEEP = 60

c = pg_bridge._conn()


def gather(cases, asof=None):
    where = {"created_at": {"$lte": asof}} if asof else None
    w = dict(where or {}); w["isLatest"] = True
    cond, params = pg_bridge._where_sql(w, SCOPE, "lezhi", False)
    out = []
    for row in cases:
        if len(row) == 4:
            kind, gid, anchor, q = row
        else:
            kind, q, gid = "a5", row[0], row[1]
        qv = pg_bridge._vec_literal(pg_bridge._ef()([q])[0])
        vec = pg_bridge._vec_channel(c, q, DEEP, cond, params, v=qv)
        toks = pg_bridge._kw_tokens(q)
        kw = pg_bridge._kw_channel(c, toks, DEEP, cond, params)
        seen = {x["id"] for x in vec}
        miss = [str(x["id"]) for x in kw if x["id"] not in seen]
        cmap = pg_bridge._cosine_for_ids(c, qv, miss)
        for it in kw:
            if it["id"] not in seen:
                it["_cos"] = cmap.get(str(it["id"]), 0.0)
        out.append(dict(kind=kind, q=q, gold=gid,
                        vec=[(str(it["id"])[:8], max(0.0, 1.0 - it["score"])) for it in vec],
                        kw=[(str(it["id"])[:8], float(it.get("kw_abs") or 0.0), float(it.get("kw") or 0.0),
                             bool(it.get("_weak")), float(it.get("_cos") or 0.0)) for it in kw]))
    return out


def fuse(d, mode, depth_v=None, depth_k=None):
    """返回按分排序的 id 列表（全部候选，不截断便于看名次）。"""
    V = d["vec"][:depth_v] if depth_v else d["vec"]
    K = d["kw"][:depth_k] if depth_k else d["kw"]
    qweak = any(x[3] for x in K)
    pool = {}
    for i, (iid, v) in enumerate(V):
        pool.setdefault(iid, {"v": v, "k": 0.0, "rv": i + 1, "rk": None})
    for i, (iid, ka, kr, wk_, cos) in enumerate(K):
        p = pool.setdefault(iid, {"v": 0.0, "k": 0.0, "rv": None, "rk": None})
        if p["v"] == 0.0:
            p["v"] = cos
        p["k"] = ka + (0.4 * kr if qweak else 0.0)
        p["rk"] = i + 1

    if mode == "base":
        wv, wk = (0.70, 0.30) if qweak else (0.45, 0.55)
        key = lambda p: (wv * p["v"] + wk * p["k"]) / (wv + wk)
    elif mode == "base_eq":            # 不降权，等权
        key = lambda p: 0.5 * p["v"] + 0.5 * p["k"]
    elif mode == "vec_heavy":
        wv, wk = (0.80, 0.20) if qweak else (0.65, 0.35)
        key = lambda p: (wv * p["v"] + wk * p["k"]) / (wv + wk)
    elif mode == "norm":               # 池内 min-max 归一后加权
        vs = [p["v"] for p in pool.values()]; ks = [p["k"] for p in pool.values()]
        vlo, vhi = min(vs), max(vs); klo, khi = min(ks), max(ks)
        dv = (vhi - vlo) or 1.0; dk = (khi - klo) or 1.0
        wv, wk = (0.55, 0.45) if qweak else (0.50, 0.50)
        key = lambda p: (wv * (p["v"] - vlo) / dv + wk * (p["k"] - klo) / dk) / (wv + wk)
    elif mode == "rrf":
        key = lambda p: (1.0 / (60 + p["rv"]) if p["rv"] else 0.0) + \
                        (1.0 / (60 + p["rk"]) if p["rk"] else 0.0)
    elif mode == "quota":              # 融合序 + 通道名次奖励（各通道前 5 加分）
        wv, wk = (0.70, 0.30) if qweak else (0.45, 0.55)
        key = lambda p: (wv * p["v"] + wk * p["k"]) / (wv + wk) \
                        + (0.10 if p["rv"] and p["rv"] <= 5 else 0.0) \
                        + (0.15 if p["rk"] and p["rk"] <= 3 else 0.0)
    elif mode == "quota2":             # 温和版奖励
        wv, wk = (0.70, 0.30) if qweak else (0.45, 0.55)
        key = lambda p: (wv * p["v"] + wk * p["k"]) / (wv + wk) \
                        + (0.05 if p["rv"] and p["rv"] <= 5 else 0.0) \
                        + (0.08 if p["rk"] and p["rk"] <= 3 else 0.0)
    elif mode in ("adaptive", "adaptive2", "adaptive3"):
        anc = pg_bridge._has_anchor(d["q"])
        bv, bk = 0.0, 0.0
        if anc:                                   # 有锚点：字面可靠 → 给通道名次奖励
            bv, bk = (0.05, 0.08) if mode == "adaptive3" else (0.10, 0.15)
            wv, wk = (0.70, 0.30) if qweak else (0.45, 0.55)
        else:                                     # 纯中文：嵌入可靠 → 加重向量
            if mode == "adaptive":
                wv, wk = (0.80, 0.20) if qweak else (0.65, 0.35)
            else:
                wv, wk = (0.70, 0.30) if qweak else (0.45, 0.55)
        key = lambda p: (wv * p["v"] + wk * p["k"]) / (wv + wk) \
                        + (bv if p["rv"] and p["rv"] <= 5 else 0.0) \
                        + (bk if p["rk"] and p["rk"] <= 3 else 0.0)
    else:
        raise ValueError(mode)
    return [iid for iid, _ in sorted(pool.items(), key=lambda kv: -key(kv[1]))]


MODES = ["base", "base_eq", "vec_heavy", "norm", "rrf", "quota", "quota2",
         "adaptive", "adaptive2", "adaptive3"]
DEPTHS = [(None, None), (DEEP, DEEP)]
CACHE = os.path.join(HERE, "logs", "sweep_fuse_cache.json")


def cached_gather(name, cases, asof):
    if os.path.exists(CACHE):
        blob = json.load(open(CACHE, encoding="utf-8"))
        if name in blob and len(blob[name]) == len(cases):
            print(f"  ({name} 用缓存 {CACHE})")
            return blob[name]
    data = gather(cases, asof)
    blob = json.load(open(CACHE, encoding="utf-8")) if os.path.exists(CACHE) else {}
    blob[name] = data
    os.makedirs(os.path.dirname(CACHE), exist_ok=True)
    json.dump(blob, open(CACHE, "w", encoding="utf-8"), ensure_ascii=False)
    return data


for name, cases, asof in (("A9", A9, ASOF), ("A5", A5, None)):
    data = cached_gather(name, cases, asof)
    print(f"\n===== {name} ({len(data)} 条) =====")
    # 通道可达上限
    for label, acc in (("vec@60", lambda d: any(x[0] == d["gold"] for x in d["vec"])),
                       ("kw@60", lambda d: any(x[0] == d["gold"] for x in d["kw"])),
                       ("union@60", lambda d: any(x[0] == d["gold"] for x in d["vec"] + d["kw"]))):
        if name == "A9":
            for kind in ("overlap", "paraphrase"):
                sub = [d for d in data if d["kind"] == kind]
                print(f"  {label} {kind:11s} 召回 {sum(1 for d in sub if acc(d))}/{len(sub)}")
        else:
            print(f"  {label} 召回 {sum(1 for d in data if acc(d))}/{len(data)}")
    hdr = f"  {'mode':10s}" + "".join(f"{str(dv)+'/'+str(dk):>12s}" for dv, dk in DEPTHS)
    print(hdr)
    for mode in MODES:
        cells = []
        for dv, dk in DEPTHS:
            if name == "A9":
                r = {}
                for kind in ("overlap", "paraphrase"):
                    sub = [d for d in data if d["kind"] == kind]
                    t = [0, 0, 0]
                    for d in sub:
                        ids = fuse(d, mode, dv, dk)
                        if d["gold"] in ids[:20]:
                            rk = ids.index(d["gold"]) + 1
                            for j, cut in enumerate((1, 3, 5)):
                                t[j] += rk <= cut
                    r[kind] = t
                cells.append(f"o{r['overlap'][0]}/{r['overlap'][2]}p{r['paraphrase'][0]}/{r['paraphrase'][2]}")
            else:
                t = [0, 0, 0]
                for d in data:
                    ids = fuse(d, mode, dv, dk)
                    if d["gold"] in ids[:20]:
                        rk = ids.index(d["gold"]) + 1
                        for j, cut in enumerate((1, 3, 5)):
                            t[j] += rk <= cut
                cells.append(f"{t[0]}/{t[1]}/{t[2]}")
        print(f"  {mode:10s}" + "".join(f"{x:>12s}" for x in cells))
print("\n列头 = 向量深度/关键词深度；A9 单元格 = overlap top1/top5 · paraphrase top1/top5；A5 = top1/top3/top5")
