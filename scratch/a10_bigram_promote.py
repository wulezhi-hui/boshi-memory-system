"""A10 / B 备选：query 侧 bigram 权重提升（零模型，无新依赖）。

诊断前提：中文无分词器 → 整段汉字以 w=3.0 当主 token（df≈0，库里不存在），
真正能命中的只有 w=1.0 的 bigram，于是它们换不来 kw_abs（_kw_abs 只看 w>=2.5 的
prim），纯中文长问句的关键词通道被稀释。

本脚本用「真实 search_memory 全流程 + monkeypatch _kw_tokens」测：
把 query 里 df 适中的 bigram 提升到 ≥2.5（进 prim / kw_abs / coord 分母）后，
A9(40) / A5(35) 的 top1/top3/top5 命中数如何变化。

只读：不改库、不改 pg_bridge（只在内存里替换函数）。
"""
import sys, os, json, collections
sys.path.insert(0, r'C:/Users/wulezhi/.boshi')
os.environ.setdefault('BOSHI_FM_STATS', '0')
import pg_bridge as P

ASOF = 1792461898.0
from eval.cases_a9 import CASES as A9
from eval.cases import CASES as A5

c = P._conn()
cond, params = P._where_sql({'isLatest': True}, 'all', 'default', False)
ent = P._kw_index(c, cond, params)
DF = {t: len(v) for t, v in ent['postings'].items()}
print(f"index docs={len(ent['docs'])} vocab={ent['vocab']}")

ORIG = P._kw_tokens


def patched(df_lo, df_hi, w_prom, cap, kind='promote'):
    def f(q):
        toks = ORIG(q)
        if kind == 'drop0':
            return [(t, w) for t, w in toks if DF.get(t, 0) > 0]
        out = []
        prom = []
        for t, w in toks:
            d = DF.get(t, 0)
            if w < 2.5 and len(t) == 2 and P._CJK_RE.fullmatch(t) and df_lo <= d <= df_hi:
                prom.append((t, w_prom))
            else:
                out.append((t, w))
        return out + prom[:cap]
    return f


VARIANTS = [
    ('base', None),
    ('prom1.8', patched(3, 300, 1.8, 8)),
    ('prom2.6', patched(3, 300, 2.6, 8)),
    ('prom2.6_broad', patched(2, 1000, 2.6, 8)),
]

res = {}
for name, fn in VARIANTS:
    P._kw_tokens = ORIG if fn is None else fn
    rows = []
    for kind, gid, anchor, q in A9:
        hits = P.search_memory(q, top_k=5, scope='all', where={'created_at': {'$lte': ASOF}})
        ids = [str(h.get('id'))[:8] for h in hits]
        rank = ids.index(gid) + 1 if gid in ids else None
        rows.append(dict(set='A9', kind=kind, gold=gid, rank=rank))
    for q, gid, anchor in A5:
        hits = P.search_memory(q, top_k=5, scope='all')
        ids = [str(h.get('id'))[:8] for h in hits]
        rank = ids.index(gid) + 1 if gid in ids else None
        rows.append(dict(set='A5', kind='anchor', gold=gid, rank=rank))
    res[name] = rows
    def m(sel):
        sub = [r for r in rows if sel(r)]
        return tuple(sum(1 for r in sub if r['rank'] and r['rank'] <= k) for k in (1, 3, 5))
    print(f"{name:14s} A9-overlap {m(lambda r: r['set']=='A9' and r['kind']=='overlap')}"
          f"  A9-paraphrase {m(lambda r: r['set']=='A9' and r['kind']=='paraphrase')}"
          f"  A5 {m(lambda r: r['set']=='A5')}")

print('\n--- A9 paraphrase 逐条 rank (base -> prom1.8 / prom2.6 / broad) ---')
base = [r for r in res['base'] if r['set'] == 'A9' and r['kind'] == 'paraphrase']
for i, r0 in enumerate(base):
    line = f"{r0['gold']} {str(r0['rank']):>4s}"
    for name in ('prom1.8', 'prom2.6', 'prom2.6_broad'):
        r = [x for x in res[name] if x['set'] == 'A9' and x['kind'] == 'paraphrase'][i]
        line += f" | {name}: {str(r['rank']):>4s}"
    print(line)

print('\n--- A5 逐条 rank 变化（只打有变化的）---')
b5 = [r for r in res['base'] if r['set'] == 'A5']
for i, r0 in enumerate(b5):
    ch = []
    for name in ('prom1.8', 'prom2.6', 'prom2.6_broad'):
        r = [x for x in res[name] if x['set'] == 'A5'][i]
        if r['rank'] != r0['rank']:
            ch.append(f"{name}:{r0['rank']}->{r['rank']}")
    if ch:
        print(r0['gold'], ' '.join(ch))

json.dump(res, open(r'C:/Users/wulezhi/.boshi/logs/a10_promote.json', 'w', encoding='utf-8'),
          ensure_ascii=False, indent=1)
print('\nsaved logs/a10_promote.json')
