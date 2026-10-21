# -*- coding: utf-8 -*-
"""A5：35 条 gold 标注 query 的绝对检索质量评测（当前索引：原文进 BM25 + 向量）

与 A3 的区别：A3 只比两个变体的相对次序，A5 有 gold（答案必须落在这条记忆里），
回答"现在的索引到底能不能把正确那条捞到第一"。

用法: python a5_eval.py [tag]
结果写 ~/.boshi/logs/a5_<tag>.json
"""
import json
import os
import sys
import time

HERE = r"C:/Users/wulezhi/.boshi"
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, "eval"))
os.chdir(HERE)
# 与 a9_eval 同：评测流量不进线上 fm_stats 打点。
os.environ.setdefault("BOSHI_FM_STATS", "0")
import pg_bridge

# (query, gold_id8, 该 gold 必须包含的锚串——防止 gold 记错)
from cases import CASES  # (query, gold_id8, anchor)

tag = sys.argv[1] if len(sys.argv) > 1 else "run"
out = {"tag": tag, "ts": time.strftime("%Y-%m-%d %H:%M:%S"), "n": len(CASES), "cases": []}
stat = {m: {"top1": 0, "top3": 0, "top5": 0} for m in ("auto", "rrf", "vector")}

for q, gold, anchor in CASES:
    rec = {"q": q, "gold": gold, "anchor": anchor}
    for mode in ("auto", "rrf", "vector"):
        try:
            items = pg_bridge.search_memory(q, top_k=5, fusion=mode,
                                           scope=os.environ.get("A5_SCOPE", "all"))
        except Exception as e:
            rec[mode] = {"error": f"{type(e).__name__}: {e}"}
            continue
        ids = [str(it["id"])[:8] for it in items]
        rank = (ids.index(gold) + 1) if gold in ids else None
        if rank == 1:
            stat[mode]["top1"] += 1
        if rank and rank <= 3:
            stat[mode]["top3"] += 1
        if rank:
            stat[mode]["top5"] += 1
        rec[mode] = {"rank": rank, "ids": ids,
                     "ch": [it.get("channels") for it in items],
                     "heads": [(it.get("content") or "")[:40].replace("\n", " ") for it in items]}
    # gold 里是否真含锚串（自检）
    out["cases"].append(rec)
    r = rec.get("auto", {})
    flag = "" if r.get("rank") == 1 else "  <<<"
    rv = rec["vector"].get("rank")
    print(f"{gold} auto={r.get('rank')} rrf={rec['rrf'].get('rank')} "
          f"vector={rv}  {q[:34]}{flag}")

out["stat"] = stat
print("\n=== 汇总 (n=%d) ===" % len(CASES))
for m in ("auto", "rrf", "vector"):
    s = stat[m]
    print(f"{m:5s} top1={s['top1']:2d}/{len(CASES)} ({s['top1']/len(CASES):.1%})  "
          f"top3={s['top3']:2d}  top5={s['top5']:2d}")

p = os.path.join(HERE, "logs", f"a5_{tag}.json")
with open(p, "w", encoding="utf-8") as f:
    json.dump(out, f, ensure_ascii=False, indent=1)
print("写入", p)
