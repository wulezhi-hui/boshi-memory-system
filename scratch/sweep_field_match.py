# -*- coding: utf-8 -*-
"""P2+ 字段级匹配参数扫描（auto 融合模式，35 条 gold）。

在进程内 monkeypatch 模块级参数，避免每档重启（embedding 只加载一次）。
用法: python scratch/sweep_field_match.py
"""
import json
import os
import sys

HERE = r"C:/Users/wulezhi/.boshi"
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, "eval"))
os.chdir(HERE)
os.environ.pop("BOSHI_FIELD_MATCH", None)
import pg_bridge
from cases import CASES

def run(tag):
    st = {"top1": 0, "top3": 0, "top5": 0}
    rows = []
    for q, gold, _anchor in CASES:
        items = pg_bridge.search_memory(q, top_k=5, fusion="auto", scope="all")
        ids = [str(it["id"])[:8] for it in items]
        rank = (ids.index(gold) + 1) if gold in ids else None
        hits = [it.get("field_hits") for it in items if it.get("field_hits")]
        rows.append((gold, rank, hits))
        if rank == 1:
            st["top1"] += 1
        if rank and rank <= 3:
            st["top3"] += 1
        if rank:
            st["top5"] += 1
    print(f"{tag:28s} top1={st['top1']:2d} top3={st['top3']:2d} top5={st['top5']:2d}")
    return st, rows

base_st, base_rows = run("baseline(fm off)")

COMBOS = [
    ("all5 s.10 m.30", ("ports", "versions", "flags", "files", "numbers"), 0.10, 0.30),
    ("all5 s.15 m.40", ("ports", "versions", "flags", "files", "numbers"), 0.15, 0.40),
    ("all5 s.25 m.60", ("ports", "versions", "flags", "files", "numbers"), 0.25, 0.60),
    ("all5 s.40 m.90", ("ports", "versions", "flags", "files", "numbers"), 0.40, 0.90),
    ("noNum s.15 m.40", ("ports", "versions", "flags", "files"), 0.15, 0.40),
    ("noNum s.25 m.60", ("ports", "versions", "flags", "files"), 0.25, 0.60),
    ("noNum s.40 m.90", ("ports", "versions", "flags", "files"), 0.40, 0.90),
    ("lit3 s.25 m.60", ("ports", "versions", "flags"), 0.25, 0.60),
]

results = {}
for tag, keys, step, mx in COMBOS:
    pg_bridge._FM_KEYS = keys
    pg_bridge._FM_STEP = step
    pg_bridge._FM_MAX = mx
    os.environ["BOSHI_FIELD_MATCH"] = "1"
    st, rows = run(tag)
    diff = [(b[0], b[1], r[1], r[2]) for b, r in zip(base_rows, rows) if b[1] != r[1]]
    same = [d for d in diff if d[2] == 1]
    hurt = [d for d in diff if d[1] == 1 and d[2] != 1]
    print(f"   rank 变化 {len(diff)} 条：转好 {len(same)}，掉出 top1 {len(hurt)}")
    for d in diff:
        print(f"     {d[0]} base={d[1]} -> {d[2]} hits={d[3]}")
    results[tag] = st

print("\n=== 汇总 ===")
print(f"{'baseline':28s} top1={base_st['top1']:2d} top3={base_st['top3']:2d} top5={base_st['top5']:2d}")
for tag, st in results.items():
    print(f"{tag:28s} top1={st['top1']:2d} top3={st['top3']:2d} top5={st['top5']:2d}")

with open(os.path.join(HERE, "logs", "sweep_field_match.json"), "w", encoding="utf-8") as f:
    json.dump({"baseline": base_st, "combos": results}, f, ensure_ascii=False, indent=1)
