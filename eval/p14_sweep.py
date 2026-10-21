# -*- coding: utf-8 -*-
"""P1④ 参数扫描：只跑生产模式 auto，A9(40)+A5(35)，输出单行 JSON

环境变量即参数（pg_bridge 在 import 时读）：
  BOSHI_KW_POOL_CAP / BOSHI_RERANK_POOL / BOSHI_KW_WEAK_WK / BOSHI_KW_WEAK_WV
  BOSHI_KW_REL_BLEND / BOSHI_KW_MIN
用法: [ENV=..] ./venv/Scripts/python.exe eval/p14_sweep.py <tag>
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

tag = sys.argv[1] if len(sys.argv) > 1 else "sweep"
SCOPE = os.environ.get("A9_SCOPE", "all")
ASOF = os.environ.get("A9_ASOF")
WHERE = {"created_at": {"$lte": ASOF}} if ASOF else None


def run(q, gid):
    items = pg_bridge.search_memory(q, top_k=5, fusion="auto", scope=SCOPE, where=WHERE)
    ids = [str(it["id"])[:8] for it in items]
    return (ids.index(gid) + 1) if gid in ids else None


st = {k: {"top1": 0, "top3": 0, "top5": 0, "n": 0} for k in ("overlap", "paraphrase", "A5")}
miss = []
for kind, gid, _a, q in A9:
    r = run(q, gid)
    s = st[kind]
    s["n"] += 1
    if r == 1:
        s["top1"] += 1
    if r and r <= 3:
        s["top3"] += 1
    if r:
        s["top5"] += 1
    if r != 1:
        miss.append(f"{kind[:4]}:{gid}={r}")
for q, gid, _a in A5:
    r = run(q, gid)
    s = st["A5"]
    s["n"] += 1
    if r == 1:
        s["top1"] += 1
    if r and r <= 3:
        s["top3"] += 1
    if r:
        s["top5"] += 1
    if r != 1:
        miss.append(f"A5:{gid}={r}")

cfg = {k: os.environ.get(k) for k in
       ("BOSHI_KW_POOL_CAP", "BOSHI_RERANK_POOL", "BOSHI_KW_WEAK_WK",
        "BOSHI_KW_REL_BLEND", "BOSHI_KW_MIN", "BOSHI_RERANK")}
line = {"tag": tag, "cfg": cfg, "stat": st, "miss": miss,
        "ts": time.strftime("%H:%M:%S")}
print("SWEEP " + json.dumps(line, ensure_ascii=False))
with open(os.path.join(HERE, "logs", "p14_sweep_%s.json" % tag), "w",
          encoding="utf-8") as f:
    json.dump(line, f, ensure_ascii=False, indent=1)
