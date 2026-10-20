# -*- coding: utf-8 -*-
"""参数扫描：同会话扩展在不同 penalty/min_cos 下的 A5 指标（35 条 gold，auto 模式）"""
import os
import sys
import time

HERE = r"C:/Users/wulezhi/.boshi"
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, "eval"))
os.chdir(HERE)

import pg_bridge                      # noqa: E402
from cases import CASES               # noqa: E402

CONFIGS = [
    ("baseline", {"BOSHI_SESSION_EXPAND": "0"}),
    ("p95_c62_n3", {"BOSHI_SESSION_EXPAND": "1", "P": 0.95, "C": 0.62, "N": 3}),
    ("p85_c70_n3", {"BOSHI_SESSION_EXPAND": "1", "P": 0.85, "C": 0.70, "N": 3}),
    ("p100_c70_n2", {"BOSHI_SESSION_EXPAND": "1", "P": 1.00, "C": 0.70, "N": 2}),
    ("p100_c75_n2", {"BOSHI_SESSION_EXPAND": "1", "P": 1.00, "C": 0.75, "N": 2}),
]

orig = pg_bridge._session_expand
print(f"{'config':14} top1 top3 top5   用时")
for name, env in CONFIGS:
    os.environ["BOSHI_SESSION_EXPAND"] = env.get("BOSHI_SESSION_EXPAND", "0")
    if env.get("BOSHI_SESSION_EXPAND") == "1":
        p, c, n = env["P"], env["C"], env["N"]
        pg_bridge._session_expand = (
            lambda _c, qv, items, cond, params, top_k, **kw:
            orig(_c, qv, items, cond, params, top_k,
                 penalty=p, min_cos=c, max_cand=n, topn=1, only_concl=True))
    else:
        pg_bridge._session_expand = orig
    t0 = time.time()
    s = {"top1": 0, "top3": 0, "top5": 0}
    for q, gold, anchor in CASES:
        items = pg_bridge.search_memory(q, top_k=5, fusion="auto", scope="all")
        ids = [str(it["id"])[:8] for it in items]
        if gold in ids:
            r = ids.index(gold) + 1
            if r == 1:
                s["top1"] += 1
            if r <= 3:
                s["top3"] += 1
            if r <= 5:
                s["top5"] += 1
    print(f"{name:14} {s['top1']:4} {s['top3']:4} {s['top5']:4}  {time.time()-t0:5.1f}s", flush=True)
pg_bridge._session_expand = orig
