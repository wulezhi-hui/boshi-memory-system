# -*- coding: utf-8 -*-
"""① 用 sweep 缓存重建线上 base 融合分，看 A9 paraphrase 失败条目的 gold 分差有多大。

目的：判断这些失败属"差一点点"（公式微调可能救）还是"差一大截"（公式无望，需扩召回）。
不改任何线上代码，只读 logs/sweep_fuse_cache.json。
"""
import json
import os

FAILS = ['7f6c4aba', 'd13aed83', 'f9b2f45f', '0d28d510', 'c0abea75', 'f6c71ce8',
         'a457b574', '231e8410', 'bc695685', '1aac404f', '89a147e0', '3f1be78e']
BASE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                    'logs', 'sweep_fuse_cache.json')
W_V, W_K, REL = 0.45, 0.55, 0.4


def load(c):
    vec = c['vec'] if isinstance(c['vec'], list) else json.loads(c['vec'])
    kw = c['kw'] if isinstance(c['kw'], list) else json.loads(c['kw'])
    return vec, kw


def fused(vec, kw):
    """复刻 _score_merge 的 base 公式（wv=0.45, wk=0.55, 弱证据 rel blend）。"""
    qweak = any(len(x) > 3 and x[3] for x in kw)
    pool = {}
    for i, (g, cs) in enumerate(vec, 1):
        pool.setdefault(g, {'v': 0.0, 'k': 0.0, 'rv': None, 'rk': None})['rv'] = i
        pool[g]['v'] = max(0.0, float(cs))
    for i, x in enumerate(kw, 1):
        g = x[0]
        d = pool.setdefault(g, {'v': 0.0, 'k': 0.0, 'rv': None, 'rk': None})
        d['rk'] = i
        if d['v'] == 0.0 and len(x) > 4 and x[4]:
            d['v'] = max(0.0, float(x[4]))
        d['k'] = float(x[2] or 0.0) + (REL * float(x[1] or 0.0) if qweak else 0.0)
    rows = []
    for g, d in pool.items():
        f = (W_V * d['v'] + W_K * d['k'])
        rows.append((f, g, d))
    rows.sort(key=lambda t: -t[0])
    return rows


def main():
    C = json.load(open(BASE, encoding='utf-8'))['A9']
    print('%-9s %6s %5s %8s %8s %7s' % ('gold', 'pool', 'rank', 'gold_f', 'top5_min', 'gap'))
    for c in C:
        if c['kind'] != 'paraphrase' or c['gold'] not in FAILS:
            continue
        vec, kw = load(c)
        rows = fused(vec, kw)
        rank = next((i + 1 for i, r in enumerate(rows) if r[1] == c['gold']), None)
        gf = rows[rank - 1][0] if rank else 0.0
        t5 = rows[4][0] if len(rows) >= 5 else 0.0
        gap = (t5 - gf) if rank and rank > 5 else None
        print('%-9s %6d %5s %8.4f %8.4f %7s' % (
            c['gold'], len(rows), rank or '-', gf, t5,
            '-' if gap is None else '%.4f' % gap))
        print('   q: %s' % c['q'][:40])
        print('   top5: %s' % ', '.join('%s(%.4f,v%s/k%s)' % (
            r[1], r[0], r[2]['rv'] or '-', r[2]['rk'] or '-') for r in rows[:5]))


if __name__ == '__main__':
    main()
