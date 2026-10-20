# -*- coding: utf-8 -*-
"""A9 软命中指标：记忆库把同一段对话切成多条近重复记录，用 record id 判分
会低估检索质量。这里加两个"可接受命中"口径：

  soft5     top5 里存在与 gold 同 session_id 的记录（同一轮对话的兄弟切片）
  dup5      top5 里存在与 gold 余弦 >= 0.92 的记录（近乎同文）
  any5      soft5 或 dup5

对比四种：auto(现行) / score(现行) / score+coord修复(影子) / vector
"""
import math
import os
import sys

HERE = r"C:/Users/wulezhi/.boshi"
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, "eval"))
os.chdir(HERE)
import pg_bridge
from cases_a9 import CASES

src = open(os.path.join(HERE, "scratch", "a9_kwfix_shadow.py"), encoding="utf-8").read()
ns = {}
exec(src.split("def main_hit_count")[0], ns)
patched = ns["patched_kw_channel"]
ORIG = pg_bridge._kw_channel

c = pg_bridge._conn()
gold_ids = sorted({cs[1] for cs in CASES})


def full_ids(prefixes):
    with c.cursor() as cur:
        cur.execute("""SELECT left(id::text,8), id::text, session_id FROM boshi.memories
                       WHERE is_latest AND left(id::text,8) = ANY(%s)""", (prefixes,))
        return {r[0]: (r[1], r[2]) for r in cur.fetchall()}


gm = full_ids(gold_ids)
sess = {p: v[1] for p, v in gm.items()}
print("gold 映射:", len(gm), "/", len(gold_ids), " 有 session_id 的:", sum(1 for v in sess.values() if v))


def soft(top_ids, gid):
    """top_ids: 8位前缀列表"""
    s = sess.get(gid)
    if s:
        with c.cursor() as cur:
            cur.execute("""SELECT count(*) FROM boshi.memories
                           WHERE is_latest AND session_id=%s
                             AND left(id::text,8) = ANY(%s)""", (s, top_ids))
            if cur.fetchone()[0]:
                return "soft"
    # 余弦近重复：把 gold 向量与这 5 条比
    with c.cursor() as cur:
        cur.execute("""WITH g AS (SELECT embedding FROM boshi.memories WHERE id=%s::uuid)
                       SELECT max(1 - (m.embedding <=> g.embedding)) FROM boshi.memories m, g
                       WHERE m.is_latest AND left(m.id::text,8) = ANY(%s)""", (gm[gid][0], top_ids))
        mx = cur.fetchone()[0]
    return "dup" if (mx is not None and mx >= 0.92) else ""


pg_bridge.search_memory("预热 嵌入 模型", top_k=3, fusion="vector", scope="all")

MODES = ("auto", "score", "vector")
res = {}
for kind, gid, anchor, q in CASES:
    row = {}
    for mode in MODES:
        pg_bridge._kw_channel = ORIG
        items = pg_bridge.search_memory(q, top_k=5, fusion=mode, scope="all")
        ids = [str(it["id"])[:8] for it in items]
        row[mode] = (ids, (ids.index(gid) + 1) if gid in ids else None, soft(ids, gid))
    pg_bridge._kw_channel = patched
    items = pg_bridge.search_memory(q, top_k=5, fusion="score", scope="all")
    pg_bridge._kw_channel = ORIG
    ids = [str(it["id"])[:8] for it in items]
    row["score_fix"] = (ids, (ids.index(gid) + 1) if gid in ids else None, soft(ids, gid))
    res[(kind, q)] = row

KEYS = ("auto", "score", "score_fix", "vector")
print(f"\n{'组':11s} {'口径':11s} " + "  ".join(f"{k:>12s}" for k in KEYS))
for kind in ("overlap", "paraphrase"):
    sub = {q: v for (k, q), v in res.items() if k == kind}
    n = len(sub)
    for label, idx in (("gold top1", 1), ("gold top5", 1), ("soft/dup top5", 2)):
        if label == "gold top1":
            cells = [sum(1 for v in sub.values() if v[k][1] == 1) for k in KEYS]
        elif label == "gold top5":
            cells = [sum(1 for v in sub.values() if v[k][1]) for k in KEYS]
        else:
            cells = [sum(1 for v in sub.values() if v[k][2]) for k in KEYS]
        print(f"{kind:11s} {label:11s} " + "  ".join(f"{c:>10d}  " for c in cells) + f"/{n}")

print("\n=== paraphrase 逐条：auto 的 soft 情况（看是不是 gold 本身歧义）===")
for (kind, q), v in res.items():
    if kind != "paraphrase":
        continue
    print(f"  auto={v['auto'][1]} soft={v['auto'][2] or '-'} fix={v['score_fix'][1]} {q[:34]}")
