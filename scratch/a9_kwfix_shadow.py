# -*- coding: utf-8 -*-
"""影子修复：coord 退化分支（不改源码，monkeypatch 对比）

修复思路（最小切片）：
  `_kw_channel` 里的 coord 因子分母是「查询里所有 w>=2.5 主 token 的权重和」。
  当查询里**没有任何主 token 命中库内文档**（纯中文整句，整段 token df=0）时，
  pr 全为 0 → 所有分数被乘成 0 → 候选全灭 → 关键词通道静默失效。
  coord 的信息量在这一支上恒为 0（大家覆盖率都是 0），它只毁掉 bigram 信号。
  最小修法：只在「至少有一个主 token 命中」时施加 coord，否则保留原始 BM25 分。

本脚本量化三件事：
  1. A9（40 条纯中文）：score 模式在修复前后的名次
  2. A5（35 条带锚点）：修复是否改变任何一条（应当 0 变化，因为主 token 有命中）
  3. 修复影响的 query 面：A5+A9 里 main_hit_n==0 的占比
"""
import math
import os
import sys

HERE = r"C:/Users/wulezhi/.boshi"
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, "eval"))
os.chdir(HERE)
import pg_bridge
from cases import CASES as A5_CASES
from cases_a9 import CASES as A9_CASES

ORIG = pg_bridge._kw_channel


def patched_kw_channel(c, toks, k, cond, params):
    """原 _kw_channel 的逐行复制，唯一差异 = coord 退化分支保护。"""
    if not toks:
        return []
    ent = pg_bridge._kw_index(c, cond, params)
    docs, postings = ent["docs"], ent["postings"]
    if not docs:
        return []
    n = len(docs)
    avgdl = ent["avgdl"] or 1.0
    k1, b = pg_bridge._KW_BM25_K1, pg_bridge._KW_BM25_B

    scores, hits, prim = {}, {}, []
    widf_total = 0.0
    main_hit_n = 0
    for tok, w in toks:
        pl = postings.get(tok)
        df = len(pl) if pl else 0
        idf = math.log(1.0 + (n - df + 0.5) / (df + 0.5))
        if w >= 2.5:
            widf_total += w * idf
            prim.append((tok, w, idf))
            if df:
                main_hit_n += 1
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
        return []
    # ←← 唯一改动：main_hit_n==0 时不施加 coord
    if main_hit_n:
        pr = {i: (hits.get(i) or {}).get("cov", 0.0) for i in scores}
        denom_w = sum(w for _, w in toks if w >= 2.5) or 1.0
        scores = {i: s * (pr[i] / denom_w) for i, s in scores.items()}
    ranked = sorted(scores.items(), key=lambda x: -x[1])[:int(k)]
    if not ranked:
        return []
    top = ranked[0][1] or 1.0
    out = []
    for idx, s in ranked:
        rel = s / top
        if rel < pg_bridge._KW_MIN:
            continue
        rid, content, meta, _dl, dtxt = docs[idx]
        it = pg_bridge._row_to_item((rid, content, meta, None))
        it["kw"] = round(rel, 3)
        it["kw_abs"] = round(pg_bridge._kw_abs(dtxt, prim, widf_total, _dl, avgdl), 3)
        out.append(it)
    return out


def main_hit_count(toks, ent):
    postings = ent["postings"]
    return sum(1 for t, w in toks if w >= 2.5 and postings.get(t))


c = pg_bridge._conn()
cond, params = pg_bridge._where_sql({}, "all", pg_bridge._resolve_profile(), False)
ent = pg_bridge._kw_index(c, cond, params)

print("=== 影响面：main_hit_n==0 的 query ===")
for name, cases, qi in (("A5", A5_CASES, 0), ("A9", A9_CASES, 3)):
    zero = [cs[qi] for cs in cases if main_hit_count(pg_bridge._kw_tokens(cs[qi]), ent) == 0]
    print(f"  {name}: {len(zero)}/{len(cases)} 条会走修复分支")
    for q in zero[:3]:
        print("    -", q[:36])

print("\n=== A9：修复前后（score 模式）===")
pg_bridge.search_memory("预热 嵌入 模型", top_k=3, fusion="vector", scope="all")
TAGS = ("overlap", "paraphrase")


def run_a9():
    res = {}
    for kind, gid, anim, q in A9_CASES:
        for label, fn in (("orig", ORIG), ("fix", patched_kw_channel)):
            pg_bridge._kw_channel = fn
            items = pg_bridge.search_memory(q, top_k=5, fusion="score", scope="all")
            ids = [str(it["id"])[:8] for it in items]
            res.setdefault((kind, q), {})[label] = (ids.index(gid) + 1) if gid in ids else None
    pg_bridge._kw_channel = ORIG
    for kind in TAGS:
        for label in ("orig", "fix"):
            sub = {q: v[label] for (k, q), v in res.items() if k == kind}
            t1 = sum(1 for r in sub.values() if r == 1)
            t3 = sum(1 for r in sub.values() if r and r <= 3)
            t5 = sum(1 for r in sub.values() if r)
            print(f"  {kind:11s} {label:4s}  top1={t1:2d} top3={t3:2d} top5={t5:2d} / {len(sub)}")
    diff = [q for (k, q), v in res.items() if v["orig"] != v["fix"]]
    print(f"  名次有变化的 query: {len(diff)}/{len(res)}")
    for q in diff[:6]:
        v = res[(next(k for (k, qq) in res if qq == q), q)]
        print(f"    {v['orig']} → {v['fix']}  {q[:34]}")
    return res


a9 = run_a9()

print("\n=== A5 回归：修复前后逐条（三模式）===")
mismatch = 0
for q, gid, anchor in A5_CASES:
    for mode in ("auto", "score", "rrf"):
        got = []
        for fn in (ORIG, patched_kw_channel):
            pg_bridge._kw_channel = fn
            items = pg_bridge.search_memory(q, top_k=5, fusion=mode, scope="all")
            ids = [str(it["id"])[:8] for it in items]
            got.append((ids.index(gid) + 1) if gid in ids else None)
        pg_bridge._kw_channel = ORIG
        if got[0] != got[1]:
            mismatch += 1
            print(f"  变化! {mode} {got[0]} → {got[1]}  {q[:36]}")
print(f"  A5 名次变化数: {mismatch} / {len(A5_CASES)*3}")
