# -*- coding: utf-8 -*-
"""② 融合权重网格扫参（只读 sweep 缓存，不动线上代码）。

上一步 a9_gap_inspect.py 显示：A9 paraphrase 的融合 top5 几乎被 kw 通道垄断
（top5 里 kw_abs≈1.0 的文档恒得 f≈0.55，vec 余弦 0.85 的文档只有 0.38），
而 kw 通道自己的 paraphrase top5 召回(12/20) 反而高于融合后的 8/20。
所以这里扫 wv（vec 权重），看"少听向量"能不能把 paraphrase 拉回来。
"""
import json
import os

BASE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                    'logs', 'sweep_fuse_cache.json')
REL = 0.4
WVS = [0.0, 0.1, 0.2, 0.3, 0.35, 0.4, 0.45, 0.5, 0.6, 0.7]


def load(c):
    vec = c['vec'] if isinstance(c['vec'], list) else json.loads(c['vec'])
    kw = c['kw'] if isinstance(c['kw'], list) else json.loads(c['kw'])
    return vec, kw


def fused(vec, kw, wv):
    qweak = any(len(x) > 3 and x[3] for x in kw)
    pool = {}
    for i, (g, cs) in enumerate(vec, 1):
        d = pool.setdefault(g, {'v': 0.0, 'k': 0.0})
        d['v'] = max(d['v'], max(0.0, float(cs)))
    for x in kw:
        g = x[0]
        d = pool.setdefault(g, {'v': 0.0, 'k': 0.0})
        if d['v'] == 0.0 and len(x) > 4 and x[4]:
            d['v'] = max(0.0, float(x[4]))
        d['k'] = float(x[2] or 0.0) + (REL * float(x[1] or 0.0) if qweak else 0.0)
    wk = 1.0 - wv
    rows = [((wv * d['v'] + wk * d['k']), g) for g, d in pool.items()]
    rows.sort(key=lambda t: -t[0])
    return [g for _, g in rows]


def hit(order, gold, k):
    return gold in order[:k]


def main():
    C = json.load(open(BASE, encoding='utf-8'))['A9']
    print('%-5s | %-17s | %-17s' % ('wv', 'overlap 1/3/5', 'paraphrase 1/3/5'))
    for wv in WVS:
        res = {}
        for kind in ('overlap', 'paraphrase'):
            n = t = None
            for c in C:
                if c['kind'] != kind:
                    continue
                vec, kw = load(c)
                order = fused(vec, kw, wv)
                lt = [int(hit(order, c['gold'], k)) for k in (1, 3, 5)]
                if n is None:
                    n, t = 0, [0, 0, 0]
                n += 1
                t = [a + b for a, b in zip(t, lt)]
            res[kind] = '%d/%d/%d' % tuple(t) + '  (n=%d)' % n
        print('%-5s | %-17s | %-17s' % (wv, res['overlap'].split(' ')[0],
                                        res['paraphrase'].split(' ')[0]))
    print()
    print('补充：单通道基线（同一份深度 60 缓存）')
    for kind in ('overlap', 'paraphrase'):
        tv = [0, 0, 0]
        tk = [0, 0, 0]
        for c in C:
            if c['kind'] != kind:
                continue
            vec, kw = load(c)
            vo = [g for g, _ in sorted(vec, key=lambda x: -float(x[1]))]
            ko = [x[0] for x in sorted(kw, key=lambda x: -(float(x[2] or 0) + REL * float(x[1] or 0)))]
            tv = [a + int(hit(vo, c['gold'], k)) for a, k in zip(tv, (1, 3, 5))]
            tk = [a + int(hit(ko, c['gold'], k)) for a, k in zip(tk, (1, 3, 5))]
        print('%-10s vec %d/%d/%d   kw %d/%d/%d' % (kind, *tv, *tk))


if __name__ == '__main__':
    main()
