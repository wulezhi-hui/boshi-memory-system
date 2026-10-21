"""B 方案试点：现算共现扩展（零模型、无持久表）。

思路：query 的某个 token t（库内 df 适中）若与伙伴 b 在库内文档中反复同现，
则 b 是 t 的「库内同义/近义/搭配」，把 b 以低权重（<2.5，不进 kw_abs 分母）
追加到 kw 通道，只帮召回、不污染绝对分。

只读：不改库、不改 pg_bridge。
"""
import sys, os, json, collections, math
sys.path.insert(0, r'C:/Users/wulezhi/.boshi')
os.environ.setdefault('BOSHI_FM_STATS', '0')
import pg_bridge as P

ASOF = 1792461898.0
c = P._conn()
cond, params = P._where_sql({**{'created_at': {'$lte': ASOF}}, 'isLatest': True},
                            'all', 'default', False)
ent = P._kw_index(c, cond, params)
docs, postings, avgdl = ent['docs'], ent['postings'], ent['avgdl']
n = len(docs)
df = {t: len(v) for t, v in postings.items()}
print(f'docs={n} vocab={ent["vocab"]} avgdl={avgdl:.1f}')

by_prefix = {}
for idx, row in enumerate(docs):
    by_prefix.setdefault(str(row[0])[:8], []).append(idx)

# ---- 共现扩展（现算，按 token 缓存） ----
tok_cache = {}
def dtoks(idx):
    t = tok_cache.get(idx)
    if t is None:
        t = set(P._doc_tokens(docs[idx][4]))
        tok_cache[idx] = t
    return t

def expand(tok, df_lo, df_hi, min_co, min_conf, topn):
    """tok 的库内共现伙伴 -> [(partner, co, df_p, conf)]"""
    dft = df.get(tok, 0)
    if not (df_lo <= dft <= df_hi):
        return []
    cnt = collections.Counter()
    for idx, _tf in postings[tok]:
        for b in dtoks(idx):
            if b != tok and len(b) >= 2:
                cnt[b] += 1
    out = []
    for b, co in cnt.most_common(200):
        if co < min_co:
            break
        dfb = df.get(b, 0)
        if dfb < 2 or dfb > 400:
            continue
        conf = co / min(dft, dfb)
        if conf < min_conf:
            continue
        out.append((b, co, dfb, round(conf, 2)))
        if len(out) >= topn:
            break
    return out


def run(query, mode, k=60, **kw):
    toks = P._kw_tokens(query)
    extra = []
    if mode != 'off':
        for tok, w in toks:
            if mode == 'prim' and w < 2.5:
                continue
            if len(tok) < 2:
                continue
            for b, co, dfb, conf in expand(tok, kw['df_lo'], kw['df_hi'],
                                           kw['min_co'], kw['min_conf'], kw['topn']):
                extra.append((b, kw['w_exp']))
    seen = set()
    add = []
    for b, w in extra:
        if b not in seen:
            seen.add(b)
            add.append((b, w))
    items = P._kw_channel(c, toks + add, k, cond, params)
    return items, add


from eval.cases_a9 import CASES as A9  # (kind, gold8, anchor, query)

# vec@60 从 sweep 缓存取（同一 ASOF 口径）
cache = json.load(open(r'C:/Users/wulezhi/.boshi/logs/sweep_fuse_cache.json', encoding='utf-8'))
vec_rank = {}
for rec in cache['A9']:
    q = rec.get('q')
    ids = [x[0] for x in rec.get('vec') or []]
    vec_rank[q] = {g: i + 1 for i, g in enumerate(ids)}

MODES = [
    ('off', {}),
    ('prim', dict(df_lo=2, df_hi=200, min_co=3, min_conf=0.35, topn=3, w_exp=1.5)),
    ('any', dict(df_lo=2, df_hi=80, min_co=3, min_conf=0.35, topn=3, w_exp=1.2)),
    ('any_tight', dict(df_lo=2, df_hi=40, min_co=4, min_conf=0.5, topn=2, w_exp=1.5)),
]

report = {}
for name, kw in MODES:
    rows = []
    for kind, gid, anchor, q in A9:
        items, add = run(q, name, **kw)
        rank = None
        for i, it in enumerate(items):
            if str(it['id'])[:8] == gid:
                rank = i + 1
                break
        vr = vec_rank.get(q, {}).get(gid)
        union = min([r for r in (rank, vr) if r], default=None)
        rows.append(dict(kind=kind, gold=gid, q=q, kw_rank=rank, vec_rank=vr,
                         union=union, n_add=len(add),
                         add=[a for a, _ in add][:8]))
    report[name] = rows
    for kind in ('overlap', 'paraphrase'):
        sub = [r for r in rows if r['kind'] == kind]
        print(f"{name:9s} {kind:10s} kw@60 {sum(1 for r in sub if r['kw_rank'] and r['kw_rank']<=60):2d}/{len(sub)}"
              f"  kw@20 {sum(1 for r in sub if r['kw_rank'] and r['kw_rank']<=20):2d}"
              f"  union@60 {sum(1 for r in sub if r['union'] and r['union']<=60):2d}"
              f"  union@20 {sum(1 for r in sub if r['union'] and r['union']<=20):2d}")

# 逐条：谁变了
print('\n--- paraphrase 逐条 kw_rank / union (off -> prim / any / any_tight) ---')
for i, r0 in enumerate([x for x in report['off'] if x['kind'] == 'paraphrase']):
    line = f"{r0['gold']} kw{str(r0['kw_rank']):>4s} u{str(r0['union']):>4s}"
    for name in ('prim', 'any', 'any_tight'):
        r = [x for x in report[name] if x['kind'] == 'paraphrase'][i]
        line += f" | {name}: kw{str(r['kw_rank']):>4s} u{str(r['union']):>4s} +{r['n_add']:2d}"
    print(line)
print('\n--- overlap 逐条 ---')
for i, r0 in enumerate([x for x in report['off'] if x['kind'] == 'overlap']):
    line = f"{r0['gold']} kw{str(r0['kw_rank']):>4s} u{str(r0['union']):>4s}"
    for name in ('prim', 'any', 'any_tight'):
        r = [x for x in report[name] if x['kind'] == 'overlap'][i]
        line += f" | {name}: kw{str(r['kw_rank']):>4s} u{str(r['union']):>4s} +{r['n_add']:2d}"
    print(line)

json.dump(report, open(r'C:/Users/wulezhi/.boshi/logs/b_expand_pilot.json', 'w', encoding='utf-8'),
          ensure_ascii=False, indent=1)
print('\nsaved logs/b_expand_pilot.json')
