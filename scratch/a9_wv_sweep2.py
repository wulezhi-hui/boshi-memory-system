# -*- coding: utf-8 -*-
"""修正版：按线上口径（qweak → wv/wk = 0.70/0.30，否则 0.45/0.55）重建融合分，
   并对 paraphrase 组扫 kw 权重占比，看"少听关键词"能否救回排序失败条目。

只读 logs/sweep_fuse_cache.json，不动线上代码。
"""
import json
import os

BASE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                    'logs', 'sweep_fuse_cache.json')
REL = 0.4
FAILS = ['7f6c4aba', 'd13aed83', 'f9b2f45f', '0d28d510', 'c0abea75', 'f6c71ce8',
         'a457b574', '231e8410', 'bc695685', '1aac404f', '89a147e0', '3f1be78e']


def load(c):
    vec = c['vec'] if isinstance(c['vec'], list) else json.loads(c['vec'])
    kw = c['kw'] if isinstance(c['kw'], list) else json.loads(c['kw'])
    return vec, kw


def build(vec, kw):
    qweak = any(len(x) > 3 and x[3] for x in kw)
    pool = {}
    for i, (g, cs) in enumerate(vec, 1):
        d = pool.setdefault(g, {'v': 0.0, 'k': 0.0, 'rv': None, 'rk': None})
        d['v'] = max(0.0, float(cs))
        d['rv'] = i
    for i, x in enumerate(kw, 1):
        g = x[0]
        d = pool.setdefault(g, {'v': 0.0, 'k': 0.0, 'rv': None, 'rk': None})
        d['rk'] = i
        if d['v'] == 0.0 and len(x) > 4 and x[4]:
            d['v'] = max(0.0, float(x[4]))
        d['k'] = float(x[2] or 0.0) + (REL * float(x[1] or 0.0) if qweak else 0.0)
    return pool, qweak


def order(pool, qweak, kw_frac=None):
    if kw_frac is None:
        wv, wk = (0.70, 0.30) if qweak else (0.45, 0.55)
    else:
        wv, wk = 1.0 - kw_frac, kw_frac
    return [g for g, _ in sorted(pool.items(),
                                 key=lambda kv: -(wv * kv[1]['v'] + wk * kv[1]['k']))]


def main():
    C = json.load(open(BASE, encoding='utf-8'))['A9']

    print('=== A. 线上口径基线（qweak 0.70/0.30）及其分差 ===')
    print('%-9s %5s %6s %8s %8s %7s' % ('gold', 'qweak', 'rank', 'gold_f', 'top5_f', 'gap'))
    for c in C:
        vec, kw = load(c)
        pool, qweak = build(vec, kw)
        wv, wk = (0.70, 0.30) if qweak else (0.45, 0.55)
        rows = sorted(((wv * d['v'] + wk * d['k']) / (wv + wk), g, d) for g, d in pool.items())
        rows.reverse()
        if c['kind'] != 'paraphrase' or c['gold'] not in FAILS:
            continue
        rank = next((i + 1 for i, r in enumerate(rows) if r[1] == c['gold']), None)
        gf = rows[rank - 1][0] if rank else 0.0
        t5 = rows[4][0]
        gap = '%.4f' % (t5 - gf) if rank and rank > 5 else '-'
        print('%-9s %5s %6s %8.4f %8.4f %7s' % (c['gold'], 'Y' if qweak else 'n',
                                                rank or '-', gf, t5, gap))

    print()
    print('=== B. paraphrase 组扫 kw 权重占比（0.30=线上；越小越信向量）===')
    print('%-6s %s' % ('kw占比', 'top1/top3/top5  (n=20)'))
    for kf in (0.0, 0.10, 0.20, 0.30, 0.40, 0.50, 0.60):
        t = [0, 0, 0]
        for c in C:
            if c['kind'] != 'paraphrase':
                continue
            vec, kw = load(c)
            pool, qweak = build(vec, kw)
            o = order(pool, qweak, kf)
            t = [a + int(c['gold'] in o[:k]) for a, k in zip(t, (1, 3, 5))]
        print('%-6.2f %d/%d/%d' % (kf, *t))

    print()
    print('=== C. overlap 组同样扫一遍（防回归）===')
    for kf in (0.30, 0.55, 0.70):
        t = [0, 0, 0]
        for c in C:
            if c['kind'] != 'overlap':
                continue
            vec, kw = load(c)
            pool, qweak = build(vec, kw)
            o = order(pool, qweak, kf)
            t = [a + int(c['gold'] in o[:k]) for a, k in zip(t, (1, 3, 5))]
        print('kw占比 %.2f -> %d/%d/%d' % (kf, *t))


if __name__ == '__main__':
    main()
