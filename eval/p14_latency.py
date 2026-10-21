# -*- coding: utf-8 -*-
"""生产口径延迟复测：重排池 100（默认）+ 纯重排（blend 关）。

只测「检索→返回」这一段（脚本内），首条丢弃（含 ONNX 嵌入模型冷载）。
用法: BOSHI_RERANK=0|1 ./venv/Scripts/python.exe eval/p14_latency.py <tag>
"""
import json
import os
import statistics
import sys
import time

HERE = r"C:/Users/wulezhi/.boshi"
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, "eval"))
os.chdir(HERE)
os.environ.setdefault("BOSHI_FM_STATS", "0")
import pg_bridge                       # noqa: E402
from cases_a9 import CASES as A9       # noqa: E402
from cases import CASES as A5          # noqa: E402

tag = sys.argv[1] if len(sys.argv) > 1 else "lat"
ASOF = os.environ.get("A9_ASOF", "1792461898")
WHERE = {"created_at": {"$lte": ASOF}}

qs = [("overlap", q) for k, _g, _a, q in A9 if k == "overlap"]
qs += [("paraphrase", q) for k, _g, _a, q in A9 if k == "paraphrase"]
qs += [("A5", q) for q, _g, _a in A5]

pg_bridge.search_memory(qs[0][1], top_k=5, fusion="auto", scope="all", where=WHERE)  # 预热

per = {}
for kind, q in qs:
    t0 = time.perf_counter()
    pg_bridge.search_memory(q, top_k=5, fusion="auto", scope="all", where=WHERE)
    dt = (time.perf_counter() - t0) * 1000
    per.setdefault(kind, []).append(dt)


def pct(a, p):
    a = sorted(a)
    if len(a) == 1:
        return a[0]
    i = min(len(a) - 1, int(round(p / 100.0 * (len(a) - 1))))
    return a[i]


out = {"tag": tag,
       "cfg": {"rerank": os.environ.get("BOSHI_RERANK"),
               "pool": pg_bridge._RERANK_POOL,
               "w_fu": pg_bridge._BLEND_W_FU,
               "ef_floor": pg_bridge._HNSW_EF_FLOOR},
       "rerank_status": pg_bridge.rerank_status()}
for kind, a in per.items():
    out[kind] = {"n": len(a), "p50": round(statistics.median(a), 1),
                 "p95": round(pct(a, 95), 1), "max": round(max(a), 1)}
allq = [x for a in per.values() for x in a]
out["ALL"] = {"n": len(allq), "p50": round(statistics.median(allq), 1),
              "p95": round(pct(allq, 95), 1), "max": round(max(allq), 1)}
print("LAT " + json.dumps(out, ensure_ascii=False))
with open(os.path.join(HERE, "logs", "p14_latency_%s.json" % tag), "w",
          encoding="utf-8") as f:
    json.dump(out, f, ensure_ascii=False, indent=1)
