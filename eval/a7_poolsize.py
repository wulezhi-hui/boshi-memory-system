# -*- coding: utf-8 -*-
"""A7：同一批 gold query 在 top_k=(5,10,20,50) 下的召回——检验"请求条数改变排序"

动机：A6 里 base 与 A5 的 top3/top5 不一致，唯一差别是 top_k（进而 k_each 由 20 变 80）。
若绝对打分是池无关的，大池的 top5 应该 ⊇ 小池的更好结果，指标不该变差。
用法: python a7_poolsize.py
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
from cases import CASES     # 复用同一批 (query, gold)

KS = (5, 10, 20, 50, 100)
res = {k: {"top1": 0, "top3": 0, "top5": 0, "miss5": []} for k in KS}
n = len(CASES)
t0 = time.time()
for q, gold, _anchor in CASES:
    for k in KS:
        items = pg_bridge.search_memory(q, top_k=k, fusion="auto", scope="all")
        ids = [str(x["id"])[:8] for x in items]
        if gold in ids:
            r = ids.index(gold) + 1
            if r == 1:
                res[k]["top1"] += 1
            if r <= 3:
                res[k]["top3"] += 1
            if r <= 5:
                res[k]["top5"] += 1
        else:
            res[k]["miss5"].append(gold)
print(f"n={n}  {time.time()-t0:.0f}s")
print("top_k | top1 | top3 | top5 | top5 漏召")
for k in KS:
    s = res[k]
    print(f"{k:5d} | {s['top1']:2d}/{n} ({s['top1']/n:.0%}) | {s['top3']:2d} | "
          f"{s['top5']:2d} | {','.join(s['miss5'])}")
with open(os.path.join(HERE, "logs", "a7_poolsize.json"), "w", encoding="utf-8") as f:
    json.dump({"ts": time.strftime("%F %T"), "n": n, "res": res}, f,
              ensure_ascii=False, indent=1)
