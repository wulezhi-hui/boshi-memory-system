# -*- coding: utf-8 -*-
"""A9 评测：纯中文 query 要不要放开关键词通道（见 cases_a9.py 的设计说明）

用法: python a9_eval.py [tag]
结果写 ~/.boshi/logs/a9_<tag>.json

自检（不通过直接报错退出）：
  1. 每条 query 的 anchor 必须逐字出现在 gold 正文里（防止 gold 记错）
  2. 每条 query 必须 `_has_anchor(q) is False`（否则不是"纯中文"）
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
from cases_a9 import CASES

tag = sys.argv[1] if len(sys.argv) > 1 else "run"
MODES = ("auto", "score", "vector")
SCOPE = os.environ.get("A9_SCOPE", "all")
# 可复现评测：评测期间插件仍在往库里写本节工作摘要（会污染纯中文 query 的 kw 候选），
# 给定 A9_ASOF（Unix 秒或 ISO）即只看该时刻之前写入的记忆，跨轮名次才可比。
ASOF = os.environ.get("A9_ASOF")
WHERE = {"created_at": {"$lte": ASOF}} if ASOF else None

c = pg_bridge._conn()
body = {}
with c.cursor() as cur:
    for _, gid, anchor, q in CASES:
        if gid in body:
            continue
        cur.execute("""SELECT content FROM boshi.memories
                       WHERE is_latest AND left(id::text, 8) = %s LIMIT 1""", (gid,))
        r = cur.fetchall()
        body[gid] = r[0][0] if r else None

bad = []
for kind, gid, anchor, q in CASES:
    if body.get(gid) is None:
        bad.append(f"gold {gid} 不存在")
    elif anchor not in body[gid]:
        bad.append(f"gold {gid} 不含 anchor「{anchor}」")
    if pg_bridge._has_anchor(q):
        bad.append(f"query 仍含锚点: {q}")
if bad:
    print("自检失败：")
    for b in bad:
        print("  -", b)
    sys.exit(1)
print(f"自检通过：{len(CASES)} 条，anchor 全在 gold 内，且全部无锚点")

pg_bridge.search_memory("预热 嵌入 模型", top_k=3, fusion="vector", scope=SCOPE,
                        where=WHERE)

rows = []
stat = {k: {m: {"top1": 0, "top3": 0, "top5": 0} for m in MODES}
        for k in ("overlap", "paraphrase")}
for kind, gid, anchor, q in CASES:
    rec = {"kind": kind, "q": q, "gold": gid, "anchor": anchor}
    for mode in MODES:
        items = pg_bridge.search_memory(q, top_k=5, fusion=mode, scope=SCOPE,
                                        where=WHERE)
        ids = [str(it["id"])[:8] for it in items]
        rank = (ids.index(gid) + 1) if gid in ids else None
        ch = [it.get("channels") for it in items]
        rec[mode] = {"rank": rank, "ids": ids, "ch": ch,
                     "top1_src": (ch[0] if ch and ch[0] else None)}
        s = stat[kind][mode]
        if rank == 1:
            s["top1"] += 1
        if rank and rank <= 3:
            s["top3"] += 1
        if rank:
            s["top5"] += 1
    rows.append(rec)

print("\n=== 分组汇总 ===")
print(f"{'组':11s} {'n':>3s}  " + "  ".join(f"{m:>16s}" for m in MODES))
for kind in ("overlap", "paraphrase"):
    g = stat[kind]
    n = sum(1 for r in rows if r["kind"] == kind)
    cells = [f"{g[m]['top1']:2d}/{g[m]['top3']:2d}/{g[m]['top5']:2d}       " for m in MODES]
    print(f"{kind:11s} {n:3d}  " + "  ".join(cells))
print("(格式 top1/top3/top5)")

print("\n=== 逐条（auto → score）===")
for r in rows:
    a, s = r["auto"]["rank"], r["score"]["rank"]
    d = "=" if a == s else ("↑" if (s and (not a or s < a)) else "↓")
    print(f"{r['kind'][:4]} {r['gold']} auto={a} score={s} {d}  {r['q'][:30]}")

out = {"tag": tag, "ts": time.strftime("%Y-%m-%d %H:%M:%S"), "asof": ASOF,
       "n": len(CASES), "scope": SCOPE, "stat": stat, "cases": rows}
p = os.path.join(HERE, "logs", f"a9_{tag}.json")
with open(p, "w", encoding="utf-8") as f:
    json.dump(out, f, ensure_ascii=False, indent=1)
print("\n写入", p)
