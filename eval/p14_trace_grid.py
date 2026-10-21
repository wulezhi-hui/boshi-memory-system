# -*- coding: utf-8 -*-
"""用 BOSHI_RERANK_TRACE 采样离线复算混合参数（生产池深，不再受 DEPTH 影响）。

为什么不用 p14_blend：那里用 top_k=DEPTH 采集，pool_k/k_each/_field_match 的
候选深度都跟着 top_k 变，采到的名次搬不回生产路径（实测离线预测 ov 12/18/20，
线上只有 12/17/18）。这里改成「生产 top_k=5 跑一遍、在 _rerank_items 内部
把融合序 + 原始重排分整串存下来」，名次就是线上名次。

用法: BOSHI_RERANK_TRACE=logs/trace_rr ./venv/Scripts/python.exe eval/p14_trace_grid.py
"""
import itertools
import json
import os
import sys

HERE = r"C:/Users/wulezhi/.boshi"
sys.path.insert(0, os.path.join(HERE, "eval"))
os.chdir(HERE)
from cases_a9 import CASES as A9          # noqa: E402
from cases import CASES as A5             # noqa: E402

TRACE = os.environ.get("BOSHI_RERANK_TRACE", "logs/trace_rr")
path = os.path.join(HERE, TRACE, "trace.jsonl")

recs = []
with open(path, encoding="utf-8") as f:
    for line in f:
        line = line.strip()
        if line:
            recs.append(json.loads(line))
print("trace 记录数 =", len(recs))

by_q = {}
for r in recs:
    by_q.setdefault(r["q"], []).append(r)

cases = []
for kind, gid, _a, q in A9:
    cases.append((kind, gid, q))
for q, gid, _a in A5:
    cases.append(("A5", gid, q))

rows, unmatched = [], 0
for kind, gid, q in cases:
    bucket = by_q.get(q)
    if not bucket:
        unmatched += 1
        continue
    r = bucket.pop(0)
    fu, rs = r["fu"], r["rs"]
    # 重排名次：分高的在前；无分的（None）排最后
    rr_order = sorted(range(len(fu)),
                      key=lambda i: (-(rs[i] if rs[i] is not None else -1e9), i))
    rr = [fu[i] for i in rr_order]
    rows.append({"kind": kind, "gold": gid, "q": q, "fu": fu, "rr": rr,
                 "rs": [rs[i] for i in rr_order]})
print("匹配上 %d 条，未匹配 %d 条" % (len(rows), unmatched))


def hits(order, gold):
    for p, x in enumerate(order[:5]):
        if x == gold:
            return p + 1
    return None


def stat(fn):
    st = {k: {"top1": 0, "top3": 0, "top5": 0, "n": 0}
          for k in ("overlap", "paraphrase", "A5")}
    for r in rows:
        order = fn(r)
        p = hits(order, r["gold"])
        s = st[r["kind"]]
        s["n"] += 1
        if p:
            if p == 1:
                s["top1"] += 1
            if p <= 3:
                s["top3"] += 1
            s["top5"] += 1
    return st


def fmt(st):
    return "ov=%s para=%s A5=%s" % tuple(
        tuple(st[k][x] for x in ("top1", "top3", "top5"))
        for k in ("overlap", "paraphrase", "A5"))


def rrf(w_rk, w_fu, K, head):
    def f(r):
        rr, fu = r["rr"], r["fu"]
        key = {}
        for i, x in enumerate(rr):
            key[x] = key.get(x, 0.0) + w_rk / (K + i + 1.0)
        h = fu[:head] if head > 0 else fu
        for i, x in enumerate(h):
            key[x] = key.get(x, 0.0) + w_fu / (K + i + 1.0)
        return [x for x, _ in sorted(key.items(), key=lambda kv: -kv[1])]
    return f


print("\n== 纯基线（生产池深，离线复算）==")
print("  rerank_only  ", fmt(stat(lambda r: r["rr"])))
print("  fused_only   ", fmt(stat(lambda r: r["fu"])))

print("\n== 病例分型（gold 在两侧的位次）==")
cls = {"both": 0, "rr_only": 0, "fu_only": 0, "neither": 0}
for r in rows:
    pr, pf = hits(r["rr"], r["gold"]), hits(r["fu"], r["gold"])
    if pr and pf:
        cls["both"] += 1
    elif pr:
        cls["rr_only"] += 1
    elif pf:
        cls["fu_only"] += 1
    else:
        cls["neither"] += 1
print("  两侧都在前5:", cls["both"], " 只有重排:", cls["rr_only"],
      " 只有融合:", cls["fu_only"], " 两侧都没:", cls["neither"])

print("\n== gold 只在融合序前5（重排打掉）的用例 ==")
for r in rows:
    if hits(r["fu"], r["gold"]) and not hits(r["rr"], r["gold"]) and r["rs"]:
        top1 = r["rs"][0] or -99
        top5 = r["rs"][4] if len(r["rs"]) > 4 and r["rs"][4] is not None else top1
        gi = r["rr"].index(r["gold"]) if r["gold"] in r["rr"] else None
        gs = r["rs"][gi] if gi is not None and gi < len(r["rs"]) else None
        print("  %-4s %-9s gold_fu=%s gold_rr=%s gold_rr_score=%s | top1=%.3f gap=%.3f" % (
            r["kind"][:4], r["gold"], hits(r["fu"], r["gold"]), gi, gs, top1, top1 - top5))

print("\n== gold 只在重排前5（融合序没有）的用例 ==")
for r in rows:
    if hits(r["rr"], r["gold"]) and not hits(r["fu"], r["gold"]) and r["rs"]:
        top1 = r["rs"][0] or -99
        top5 = r["rs"][4] if len(r["rs"]) > 4 and r["rs"][4] is not None else top1
        print("  %-4s %-9s gold_rr=%s gold_fu=%s | top1=%.3f gap=%.3f" % (
            r["kind"][:4], r["gold"], hits(r["rr"], r["gold"]),
            r["fu"].index(r["gold"]) + 1 if r["gold"] in r["fu"] else None,
            top1, top1 - top5))

print("\n== 网格（生产口径离线复算，按 para.top5 排序）==")
g = []
for w_rk, w_fu, K, head in itertools.product(
        (1.0, 1.2, 1.5), (0.3, 0.5, 0.8, 1.0), (5, 10, 20), (1, 5, 20)):
    g.append((stat(rrf(w_rk, w_fu, K, head)), w_rk, w_fu, K, head))
g.sort(key=lambda t: (t[0]["paraphrase"]["top5"], t[0]["paraphrase"]["top3"],
                      t[0]["overlap"]["top5"], t[0]["A5"]["top5"]), reverse=True)
for st, w_rk, w_fu, K, head in g[:12]:
    print("  rk=%.1f fu=%.1f K=%2d head=%2d | %s" % (w_rk, w_fu, K, head, fmt(st)))

out = os.path.join(HERE, "logs", "p14_trace_grid.json")
with open(out, "w", encoding="utf-8") as f:
    json.dump([{"stat": s, "w_rk": a, "w_fu": b, "k": c, "head": d}
               for s, a, b, c, d in g], f, ensure_ascii=False, indent=1)
print("\n写入 logs/p14_trace_grid.json")
