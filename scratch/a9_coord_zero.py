# -*- coding: utf-8 -*-
"""证实 A9 上 auto==score 的机制：kw 通道在纯中文 query 上恒为零候选。

根因链（三处代码）：
  1. `_kw_tokens` (pg_bridge.py:646-652)：一整段连续汉字被当成**一个** 3.0 权重的
     token（例如 18 字的整句），bigram 只取前 6 个（`range(min(n-1, 6))`）。
     整句几乎不可能作为 token 存在于 postings（df=0）→ 主 token 不产生分数。
  2. `coord` 协调因子 (pg_bridge.py:889-901)：只有 w>=2.5 的主 token 才累加
     `hits[idx]["cov"]`；纯中文 query 的主 token 零命中 → pr 全为 0
     → `scores = s * (0 / 1.0) = 0`，bigram 命中的分数被整体归零。
  3. `pg_bridge.py:906-911`：`top = ranked[0][1] or 1.0`、`rel = 0/1.0 = 0 < _KW_MIN`
     → 全部候选被丢弃 → `_kw_channel` 返回 [] → `search_memory:1237` 退回纯向量。

本脚本直接复算打分（不改源码），打印 raw / coord 后 两种分，验证第 2 步归零。
"""
import os
import sys

HERE = r"C:/Users/wulezhi/.boshi"
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, "eval"))
os.chdir(HERE)
import math
import pg_bridge

c = pg_bridge._conn()
cond, params = pg_bridge._where_sql({}, "all", pg_bridge._resolve_profile(), False)
ent = pg_bridge._kw_index(c, cond, params)
docs, postings, n, avgdl = ent["docs"], ent["postings"], len(ent["docs"]), ent["avgdl"] or 1.0
k1, b = pg_bridge._KW_BM25_K1, pg_bridge._KW_BM25_B

QS = [
    "群晖硬盘显示已损毁但检测是好的怎么办",
    "显存不足",
    "显存不足怎么办",
    "回显磁盘已损毁",          # 故意带上库内可能存在的字面段
]

for q in QS:
    toks = pg_bridge._kw_tokens(q)
    scores, hits = {}, {}
    main_hit_tokens = 0
    for tok, w in toks:
        pl = postings.get(tok)
        df = len(pl) if pl else 0
        idf = math.log(1.0 + (n - df + 0.5) / (df + 0.5))
        if df == 0:
            continue
        if w >= 2.5:
            main_hit_tokens += 1
        if not pl:
            continue
        for idx, tf in pl:
            dl = docs[idx][3]
            denom = tf + k1 * (1.0 - b + b * dl / avgdl)
            scores[idx] = scores.get(idx, 0.0) + w * idf * (tf * (k1 + 1.0)) / denom
            if w >= 2.5:
                d = hits.setdefault(idx, {"hit": 0.0, "cov": 0.0})
                d["hit"] += w * idf
                d["cov"] += w
    if not scores:
        print(f"\nq={q}\n  raw 命中 0 条（所有 token df=0 或 token 为空）")
        continue
    raw = sorted(scores.items(), key=lambda x: -x[1])[:5]
    pr = {i: (hits.get(i) or {}).get("cov", 0.0) for i in scores}
    denom_w = sum(w for _, w in toks if w >= 2.5) or 1.0
    coord = {i: s * (pr[i] / denom_w) for i, s in scores.items()}
    ctop = sorted(coord.items(), key=lambda x: -x[1])[:5]
    print(f"\nq={q}  主token命中数={main_hit_tokens}  raw候选={len(scores)}")
    print("  raw  :", [(str(docs[i][0])[:8], round(s, 3)) for i, s in raw])
    print("  coord:", [(str(docs[i][0])[:8], round(s, 3)) for i, s in ctop])
    nonzero = sum(1 for s in coord.values() if s > 0)
    print(f"  coord 后非零={nonzero}/{len(coord)}  → _kw_channel 返回 {nonzero if nonzero else 0} 条（过 _KW_MIN 之后）")
