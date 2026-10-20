# -*- coding: utf-8 -*-
"""弱证据融合权重网格（_KW_WEAK_WV / _KW_WEAK_WK）。

背景：2026-10-21 把 kw_abs 的分母从「phantom 主 token」换成「库内真实 posting
token」后，kw_abs 的取值分布从 0.5~0.88（人人接近饱和）拉开到 0.08~0.72。
旧标定 0.70/0.30 是在饱和分布下拍的，量纲变了必须重扫。

做法：单进程内直接改模块全局变量（_score_merge 内是运行时读取），逐个网格点
跑 A9 全量 query，只看 auto 通道 —— 弱证据路径只影响「无主 token 命中」的
纯中文 query，带锚点（A5）走强证据分支（0.45/0.55），本网格不触及。

用法: python scratch/a9_weak_w_scan.py [asof]
"""
import json
import os
import sys

HERE = r"C:/Users/wulezhi/.boshi"
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, "eval"))
os.chdir(HERE)
import pg_bridge
from cases_a9 import CASES

ASOF = sys.argv[1] if len(sys.argv) > 1 else "1792461898"
WHERE = {"created_at": {"$lte": ASOF}}
SCOPE = "all"

# 预热嵌入模型，避免把加载时间算进任何一格
pg_bridge.search_memory("预热 嵌入 模型", top_k=3, fusion="vector", scope=SCOPE, where=WHERE)

GRID = [(1.00, 0.00), (0.90, 0.10), (0.85, 0.15), (0.80, 0.20),
        (0.75, 0.25), (0.70, 0.30)]

rows = {}
plan = [(kind, gid, q) for kind, gid, anchor, q in CASES]
for wv, wk in GRID:
    pg_bridge._KW_WEAK_WV = wv
    pg_bridge._KW_WEAK_WK = wk
    stat = {"overlap": [0, 0, 0], "paraphrase": [0, 0, 0]}
    detail = []
    for kind, gid, q in plan:
        items = pg_bridge.search_memory(q, top_k=5, fusion="auto", scope=SCOPE, where=WHERE)
        ids = [str(x["id"])[:8] for x in items]
        r = (ids.index(gid) + 1) if gid in ids else None
        s = stat[kind]
        if r == 1:
            s[0] += 1
        if r and r <= 3:
            s[1] += 1
        if r:
            s[2] += 1
        detail.append({"gold": gid, "kind": kind, "rank": r})
    rows[(wv, wk)] = {"stat": stat, "detail": detail}
    ov, pp = stat["overlap"], stat["paraphrase"]
    tot = [ov[i] + pp[i] for i in range(3)]
    print(f"wv={wv:.2f} wk={wk:.2f} | overlap {ov[0]:2d}/{ov[1]:2d}/{ov[2]:2d} "
          f"| para {pp[0]:2d}/{pp[1]:2d}/{pp[2]:2d} | 合计 {tot[0]:2d}/{tot[1]:2d}/{tot[2]:2d}",
          flush=True)

base = rows[(0.70, 0.30)]
print("\n=== 与基线 0.70/0.30 逐条差异 ===")
for key, v in rows.items():
    if key == (0.70, 0.30):
        continue
    diff = [(b["gold"], b["rank"], a["rank"]) for b, a in zip(base["detail"], v["detail"])
            if b["rank"] != a["rank"]]
    up = sum(1 for _, b, a in diff if (a and (not b or a < b)))
    dn = sum(1 for _, b, a in diff if (b and (not a or a > b)))
    print(f"wv={key[0]:.2f} wk={key[1]:.2f} ↑{up} ↓{dn} " +
          " ".join(f"{g}:{b}->{a}" for g, b, a in diff[:8]))

out = os.path.join(HERE, "logs", "a9_weak_w_scan.json")
with open(out, "w", encoding="utf-8") as f:
    json.dump({"asof": ASOF, "grid": [list(k) for k in rows],
               "result": {f"{k[0]}/{k[1]}": v for k, v in rows.items()}},
              f, ensure_ascii=False, indent=1)
print("\n写入", out)
