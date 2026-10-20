# -*- coding: utf-8 -*-
"""体检：检索结果的重复拥挤（top5 里同正文/同前 120 字占几位）"""
import os
import sys
from collections import Counter

HERE = r"C:/Users/wulezhi/.boshi"
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, "eval"))
os.chdir(HERE)

import pg_bridge            # noqa: E402
from cases import CASES     # noqa: E402

crowded = 0
rows = []
for q, gold, anchor in CASES:
    items = pg_bridge.search_memory(q, top_k=5, fusion="auto", scope="all")
    keys = [str(it["content"])[:120] for it in items]
    cnt = Counter(keys)
    dup = sum(v - 1 for v in cnt.values() if v > 1)
    if dup:
        crowded += 1
        rows.append((q[:30], dup, [k.replace("\n", " ")[:34] for k, v in cnt.items() if v > 1]))

print("35 条 query 中 top5 存在重复的:", crowded)
for r in rows:
    print(r)
