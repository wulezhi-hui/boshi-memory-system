# -*- coding: utf-8 -*-
"""P1④ 置信度门控升级：先浅池重排，top1 交叉编码器分不够高才扩池重排。

产出 logs/p14_escalate.jsonl（每条一行，含浅池名次/浅池 top1 logit/深池名次/耗时），
阈值网格离线算（eval/p14_escalate_report.py），不必重跑。

用法:
  A9_ASOF=1792461898 A9_SCOPE=all PGOPTIONS="-c hnsw.ef_search=400" \
  ./venv/Scripts/python.exe eval/p14_escalate.py [deep_pool=150]
"""
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

DEEP = int(sys.argv[1]) if len(sys.argv) > 1 else 150
SHALLOW = pg_bridge._RERANK_POOL          # 生产值 15
SCOPE = os.environ.get("A9_SCOPE", "all")
ASOF = os.environ.get("A9_ASOF")
WHERE = {"created_at": {"$lte": ASOF}} if ASOF else None
OUT = os.path.join(HERE, "logs", "p14_escalate.jsonl")


def run(q, gid, pool):
    pg_bridge._RERANK_POOL = pool
    t0 = time.time()
    items = pg_bridge.search_memory(q, top_k=5, fusion="auto", scope=SCOPE, where=WHERE)
    ids = [str(it["id"])[:8] for it in items]
    rank = (ids.index(gid) + 1) if gid in ids else None
    md = items[0].get("metadata") if items else {}
    top1 = md.get("rerank_score") if isinstance(md, dict) else None
    return rank, top1, round(time.time() - t0, 2)


rows = []
cases = [(k, g, a, q) for k, g, a, q in A9] + [("A5", g, a, q) for q, g, a in A5]
for i, (kind, gid, _a, q) in enumerate(cases, 1):
    rs, ts, ws = run(q, gid, SHALLOW)
    rd, td, wd = run(q, gid, DEEP)
    row = {"kind": kind, "gid": gid, "q": q, "shallow_rank": rs,
           "shallow_top1_logit": ts, "shallow_s": ws,
           "deep_rank": rd, "deep_top1_logit": td, "deep_s": wd}
    rows.append(row)
    with open(OUT, "a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False) + "\n")
    print("%3d/%d %-10s sh=%s logit=%s deep=%s  %2.1fs/%2.1fs %s" %
          (i, len(cases), kind, rs, ts, rd, ws, wd, q[:26]), flush=True)
print("DONE", OUT)
