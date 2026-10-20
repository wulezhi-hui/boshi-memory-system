# -*- coding: utf-8 -*-
"""P3 前置测量：纯中文 query 要不要放开关键词通道？

auto 模式的门槛是 _has_anchor（含型号/端口/版本号/数字才走 BM25），原因是
BM25 不辨否定，实测把「不是佛教微调，是通用的」顶到第一。现在 P2 写入侧槽位
已落地、P2+ 字段级匹配已上线，前提变了 —— 用 A5 的 35 条真实 query 直接测：

  auto / rrf / score / vector 四档，按「纯中文」与「带锚点」分两组看 top1/3/5，
  并逐条列出 auto→score 的名次变化（转好/变差/不变/均未中）。

用法: python a5_fusion_modes.py [tag]
结果写 ~/.boshi/logs/a5_fusion_<tag>.json
"""
import json
import os
import sys
import time

HERE = r"C:/Users/wulezhi/.boshi"
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, "eval"))
os.chdir(HERE)
import pg_bridge
from cases import CASES

tag = sys.argv[1] if len(sys.argv) > 1 else "run"
MODES = ("auto", "score", "rrf", "vector")

pg_bridge.search_memory("预热 嵌入 模型", top_k=3, fusion="vector",
                        scope=os.environ.get("A5_SCOPE", "all"))

rows = []
stat = {m: {"top1": 0, "top3": 0, "top5": 0} for m in MODES}
grp = {}
for q, gold, anchor in CASES:
    anchored = pg_bridge._has_anchor(q)
    key = "anchor" if anchored else "chinese"
    g = grp.setdefault(key, {"n": 0, **{m: 0 for m in MODES}})
    g["n"] += 1
    rec = {"q": q, "gold": gold, "anchor": anchor, "has_anchor": anchored}
    for mode in MODES:
        t0 = time.time()
        items = pg_bridge.search_memory(q, top_k=5, fusion=mode,
                                        scope=os.environ.get("A5_SCOPE", "all"))
        ms = (time.time() - t0) * 1000
        ids = [str(it["id"])[:8] for it in items]
        rank = (ids.index(gold) + 1) if gold in ids else None
        rec[mode] = {"rank": rank, "ms": round(ms), "ids": ids}
        if rank == 1:
            stat[mode]["top1"] += 1
            g[mode] += 1
        if rank and rank <= 3:
            stat[mode]["top3"] += 1
        if rank:
            stat[mode]["top5"] += 1
    a, s = rec["auto"]["rank"], rec["score"]["rank"]
    if a == s:
        rec["delta"] = "same"
    elif s == 1 and a != 1:
        rec["delta"] = "better"
    elif a == 1 and s != 1:
        rec["delta"] = "worse"
    elif a and s and s < a:
        rec["delta"] = "up"
    elif a and s and s > a:
        rec["delta"] = "down"
    elif a is None and s:
        rec["delta"] = "better"
    elif a and s is None:
        rec["delta"] = "worse"
    else:
        rec["delta"] = "neither"
    rows.append(rec)

n = len(CASES)
print("=== 总览 (n=%d) ===" % n)
for m in MODES:
    st = stat[m]
    print(f"{m:6s} top1={st['top1']:2d}/{n} ({st['top1']/n:.1%})  "
          f"top3={st['top3']:2d}  top5={st['top5']:2d}")

print("\n=== 分组 ===")
for key, label in (("chinese", "纯中文(无锚点)"), ("anchor", "带锚点")):
    g = grp.get(key)
    if not g:
        continue
    print(f"{label} n={g['n']}: " + "  ".join(f"{m} top1={g[m]}/{g['n']}" for m in MODES))

print("\n=== auto → score 逐条变化 ===")
order = {"better": 0, "up": 1, "same": 2, "neither": 3, "down": 4, "worse": 5}
for r in sorted(rows, key=lambda r: (order[r["delta"]], r["q"])):
    if r["delta"] == "same":
        continue
    print(f"[{r['delta']:6s}] auto={r['auto']['rank']} score={r['score']['rank']} "
          f"{'锚' if r['has_anchor'] else '中'}  {r['q'][:44]}")

cnt = {}
for r in rows:
    cnt[r["delta"]] = cnt.get(r["delta"], 0) + 1
print("\n变化统计:", json.dumps(cnt, ensure_ascii=False))
lat = {m: round(sum(r[m]["ms"] for r in rows) / n) for m in MODES}
print("平均延迟(ms):", lat)

out = {"tag": tag, "ts": time.strftime("%Y-%m-%d %H:%M:%S"), "n": n,
       "stat": stat, "groups": grp, "delta": cnt, "latency_ms": lat, "cases": rows}
p = os.path.join(HERE, "logs", f"a5_fusion_{tag}.json")
with open(p, "w", encoding="utf-8") as f:
    json.dump(out, f, ensure_ascii=False, indent=1)
print("写入", p)
