"""池深/截断长度扫描：为 _RERANK_POOL / _RERANK_CHARS 定默认值（质量 vs 延迟）。"""
import os, sys, json, time, requests
sys.path.insert(0, r'C:/Users/wulezhi/.boshi')
os.environ.setdefault('BOSHI_FM_STATS', '0')
import pg_bridge as P
from eval.cases_a9 import CASES as A9
from eval.cases import CASES as A5

ASOF = 1792461898.0
API = 'http://127.0.0.1:11437/rerank'


def rerank(query, docs, cap, chunk):
    out, buf, bidx = [], [], []
    def flush():
        if not buf:
            return
        r = requests.post(API, json={'model': 'bge-reranker-v2-m3', 'query': query,
                                     'documents': buf}, timeout=120)
        r.raise_for_status()
        for it in r.json().get('results', []):
            out.append((bidx[it['index']], it['relevance_score']))
    for i, d in enumerate(docs):
        t = (d or '')[:cap]
        if buf and sum(len(x) for x in buf) + len(t) > chunk:
            flush(); buf, bidx = [], []
        buf.append(t); bidx.append(i)
    flush()
    return sorted(out, key=lambda x: -x[1])


def cases_all():
    cs = []
    for c in A9:
        kind, gid, anchor, q = c
        cs.append(('A9-' + kind, gid, q, {'created_at': {'$lte': ASOF}}))
    for c in A5:
        q, gid, anchor = c
        cs.append(('A5', gid, q, None))
    return cs


CASES = cases_all()
FUSED = {}
for name, gid, q, wh in CASES:                       # 融合序只算一次，供各档复用
    pool = P.search_memory(q, top_k=30, scope='all', where=wh)
    FUSED[(name, gid)] = [(str(x['id'])[:8], x.get('content') or '') for x in pool]

print(f"{'cfg':22s} {'overlap':>12s} {'paraphrase':>12s} {'A5':>12s}  p50/p90 ms")
for pool_n, cap, chunk in [(10, 700, 2200), (15, 700, 2200), (20, 700, 2200),
                           (20, 300, 2200), (15, 300, 2200), (10, 300, 2200)]:
    res = {'A9-overlap': [], 'A9-paraphrase': [], 'A5': []}
    lat = []
    for name, gid, q, wh in CASES:
        cand = FUSED[(name, gid)][:pool_n]
        ids = [c[0] for c in cand]
        t0 = time.time()
        order = rerank(q, [c[1] for c in cand], cap, chunk)
        lat.append((time.time() - t0) * 1000)
        head = [ids[i] for i, _ in order][:5]
        res[name].append(head.index(gid) + 1 if gid in head else None)
    lat.sort()
    def m(v):
        return tuple(sum(1 for r in v if r and r <= k) for k in (1, 3, 5))
    print(f"pool{pool_n:<3d}cap{cap:<4d}chunk{chunk:<5d} {str(m(res['A9-overlap'])):>12s} "
          f"{str(m(res['A9-paraphrase'])):>12s} {str(m(res['A5'])):>12s}  "
          f"{lat[len(lat)//2]:.0f}/{lat[int(len(lat)*0.9)]:.0f}")
