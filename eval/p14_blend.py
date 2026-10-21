# -*- coding: utf-8 -*-
"""P1④ 融合序与重排序的 RRF 混合（不换模型，只改聚合方式）

动机：实测重排会「救回来一批、同时打掉一批」——
  · 8b027120 融合序第 1，重排后掉到 30（交叉编码器把「多模型协作注解」判成不相关）
  · c0abea75 融合序 50，重排后进榜
单纯「重排覆盖融合序」等于把前者的收益扔了。改成两路名次做 RRF，兼顾两边。

做法：同一 query 跑两遍（关重排 / 开重排），拿两串 id 序，离线按
      score = w_rk/(K+rk_rerank) + w_fu/(K+rk_fusion)   重排，再算 top1/3/5。
用法: BOSHI_RERANK_POOL=100 ./venv/Scripts/python.exe eval/p14_blend.py <tag>
输出: logs/p14_blend_<tag>.json
"""
import itertools
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
from cases_a9 import CASES as A9
from cases import CASES as A5

tag = sys.argv[1] if len(sys.argv) > 1 else "blend"
SCOPE = os.environ.get("A9_SCOPE", "all")
ASOF = os.environ.get("A9_ASOF")
WHERE = {"created_at": {"$lte": ASOF}} if ASOF else None
DEPTH = int(os.environ.get("BLEND_DEPTH", "60"))


def _ids(q):
    items = pg_bridge.search_memory(q, top_k=DEPTH, fusion="auto",
                                    scope=SCOPE, where=WHERE)
    return ([str(it["id"])[:8] for it in items],
            [it.get("rerank_score") for it in items])


_rerank_on = pg_bridge._RERANK_ON
rows = []
t0 = time.time()
for kind, gid, _a, q in A9:
    pg_bridge._RERANK_ON = False
    fu, _ = _ids(q)
    pg_bridge._RERANK_ON = _rerank_on
    rr, rs = _ids(q)
    rows.append({"kind": kind, "gold": gid, "q": q, "fu": fu, "rr": rr, "rs": rs})
for q, gid, _a in A5:
    pg_bridge._RERANK_ON = False
    fu, _ = _ids(q)
    pg_bridge._RERANK_ON = _rerank_on
    rr, rs = _ids(q)
    rows.append({"kind": "A5", "gold": gid, "q": q, "fu": fu, "rr": rr, "rs": rs})
    print("...%d/%d %.0fs" % (len(rows), len(A9) + len(A5), time.time() - t0),
          flush=True)


def stats(w_rk, w_fu, K):
    st = {k: {"top1": 0, "top3": 0, "top5": 0, "n": 0}
          for k in ("overlap", "paraphrase", "A5")}
    for r in rows:
        sc = {}
        for i, x in enumerate(r["rr"]):
            sc[x] = sc.get(x, 0.0) + w_rk / (K + i + 1)
        for i, x in enumerate(r["fu"]):
            sc[x] = sc.get(x, 0.0) + w_fu / (K + i + 1)
        order = [x for x, _ in sorted(sc.items(), key=lambda kv: -kv[1])][:5]
        s = st[r["kind"]]
        s["n"] += 1
        if r["gold"] in order:
            pos = order.index(r["gold"]) + 1
            if pos == 1:
                s["top1"] += 1
            if pos <= 3:
                s["top3"] += 1
            s["top5"] += 1
    return st


def _pure(order_key):
    st = {k: {"top1": 0, "top3": 0, "top5": 0, "n": 0}
          for k in ("overlap", "paraphrase", "A5")}
    for r in rows:
        order = r[order_key][:5]
        s = st[r["kind"]]
        s["n"] += 1
        if r["gold"] in order:
            pos = order.index(r["gold"]) + 1
            if pos == 1:
                s["top1"] += 1
            if pos <= 3:
                s["top3"] += 1
            s["top5"] += 1
    return st


base = {"fused_only": _pure("fu"), "rerank_only": _pure("rr")}
grid = []
for w_rk, w_fu, K in itertools.product((1.0, 0.7, 0.5, 0.4, 0.3), (0.3, 0.5, 0.7, 1.0), (10, 60)):
    st = stats(w_rk, w_fu, K)
    grid.append({"w_rerank": w_rk, "w_fused": w_fu, "K": K, "stat": st})

def _key(g):
    s = g["stat"]
    return (s["paraphrase"]["top5"], s["paraphrase"]["top3"], s["paraphrase"]["top1"],
            s["A5"]["top1"], s["overlap"]["top5"])
grid.sort(key=_key, reverse=True)

print("\n== 纯基线 ==")
for k, v in base.items():
    print("  %-11s ov=%s para=%s A5=%s" % (
        k, tuple(v["overlap"][x] for x in ("top1", "top3", "top5")),
        tuple(v["paraphrase"][x] for x in ("top1", "top3", "top5")),
        tuple(v["A5"][x] for x in ("top1", "top3", "top5"))))
print("\n== RRF 混合（按 para.top5 排序，前 8）==")
for g in grid[:8]:
    s = g["stat"]
    print("  w_rk=%.1f w_fu=%.1f K=%d | ov=%s para=%s A5=%s" % (
        g["w_rerank"], g["w_fused"], g["K"],
        tuple(s["overlap"][x] for x in ("top1", "top3", "top5")),
        tuple(s["paraphrase"][x] for x in ("top1", "top3", "top5")),
        tuple(s["A5"][x] for x in ("top1", "top3", "top5"))))

out = {"tag": tag, "ts": time.strftime("%Y-%m-%d %H:%M:%S"), "asof": ASOF,
       "depth": DEPTH, "base": base, "grid": grid, "rows": rows}
with open(os.path.join(HERE, "logs", "p14_blend_%s.json" % tag), "w",
          encoding="utf-8") as f:
    json.dump(out, f, ensure_ascii=False, indent=1)
print("\n写入 logs/p14_blend_%s.json" % tag)
