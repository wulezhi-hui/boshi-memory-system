"""E 方案实测：用本机 11437 的 bge-reranker-v2-m3（llama.cpp --rerank）重排。

背景：A9 paraphrase 的缺口在排序而非召回（union@60 上限 16/20），A 方案的锚点奖励
只对有锚点的 query 生效。本机 BuddhistRAG 早有 bge-reranker-v2-m3-FP16.gguf
（D:/llama/models/，1.1GB，11437 端口 --rerank），直接拿来当候选重排器，零下载。

测三档：
  base        : search_memory top5 原样
  rerank@20   : 取 top20 候选全部送重排，取前 5
  rerank@5    : 只重排已进榜的 5 条
同时记录每次 rerank 的耗时（GPU 上，含 HTTP 往返），观察能否上线。
"""
import os, sys, json, time, requests

sys.path.insert(0, r'C:/Users/wulezhi/.boshi')
os.environ.setdefault('BOSHI_FM_STATS', '0')
import pg_bridge as P
from eval.cases_a9 import CASES as A9
from eval.cases import CASES as A5

ASOF = 1792461898.0
API = 'http://127.0.0.1:11437/rerank'
MODEL = 'bge-reranker-v2-m3'
CHAR_CAP = 700          # 单条候选截断（记忆条目普遍短，长文截尾）
CHUNK_CHARS = 2200      # 单次请求的字符预算（ubatch 4096 tokens 以内）


def rerank(query, docs):
    """返回 [(idx, score)]，按 score 降序（bge 输出负 logit，越大越相关）。"""
    out = []
    buf, buf_idx = [], []

    def flush():
        if not buf:
            return
        r = requests.post(API, json={'model': MODEL, 'query': query, 'documents': buf},
                          timeout=120)
        r.raise_for_status()
        for it in r.json().get('results', []):
            out.append((buf_idx[it['index']], it['relevance_score']))

    for i, d in enumerate(docs):
        t = (d or '')[:CHAR_CAP]
        if sum(len(x) for x in buf) + len(t) > CHUNK_CHARS and buf:
            flush()
            buf, buf_idx = [], []
        buf.append(t)
        buf_idx.append(i)
    flush()
    return sorted(out, key=lambda x: -x[1])


def run(cases, set_name, where, asof_where, k_pool):
    rows = []
    for c in cases:
        if len(c) == 4:
            kind, gid, anchor, case_q = c
        else:
            q, gid, anchor = c
            kind = 'anchor'
            case_q = q
        q = case_q
        base = P.search_memory(q, top_k=5, scope='all', where=asof_where)
        b_ids = [str(x['id'])[:8] for x in base]
        b_rank = b_ids.index(gid) + 1 if gid in b_ids else None
        rec = {'set': set_name, 'kind': kind, 'gold': gid, 'base': b_rank}

        pool = P.search_memory(q, top_k=k_pool, scope='all', where=asof_where)
        p_ids = [str(x['id'])[:8] for x in pool]
        docs = [x.get('content') or x.get('document') or '' for x in pool]
        t0 = time.time()
        order = rerank(q, docs)
        rec['rr_ms'] = round((time.time() - t0) * 1000)
        head = [p_ids[i] for i, _ in order][:5]
        rec['rr20'] = head.index(gid) + 1 if gid in head else None
        h5 = [p_ids[i] for i, _ in order if i < 5][:5]
        rec['rr5'] = h5.index(gid) + 1 if gid in h5 else None
        rec['in_pool'] = gid in p_ids
        rows.append(rec)
    return rows


rows = run(A9, 'A9', None, {'created_at': {'$lte': ASOF}}, 20)
rows += run(A5, 'A5', 'all', None, 20)

def m(sel, key):
    sub = [r for r in rows if sel(r)]
    return tuple(sum(1 for r in sub if r[key] and r[key] <= k) for k in (1, 3, 5))

print(f"{'group':18s} {'n':>3s}  {'base':>12s} {'rerank@20':>12s} {'rerank@5':>12s}")
for name, sel in [('A9-overlap', lambda r: r['set'] == 'A9' and r['kind'] == 'overlap'),
                  ('A9-paraphrase', lambda r: r['set'] == 'A9' and r['kind'] == 'paraphrase'),
                  ('A5', lambda r: r['set'] == 'A5')]:
    n = sum(1 for r in rows if sel(r))
    print(f"{name:18s} {n:3d}  {str(m(sel,'base')):>12s} {str(m(sel,'rr20')):>12s} {str(m(sel,'rr5')):>12s}")

ms = [r['rr_ms'] for r in rows]
ms.sort()
print(f"\nrerank 耗时(ms): p50={ms[len(ms)//2]} p90={ms[int(len(ms)*0.9)]} max={ms[-1]}")

print('\n--- A9 paraphrase 逐条 base / rr20 / rr5 ---')
for r in rows:
    if r['set'] == 'A9' and r['kind'] == 'paraphrase':
        print(f"{r['gold']} base={str(r['base']):>4s} rr20={str(r['rr20']):>4s} rr5={str(r['rr5']):>4s}")

print('\n--- A5 变化条 ---')
for r in rows:
    if r['set'] == 'A5' and r['rr20'] != r['base']:
        print(f"{r['gold']} base={str(r['base']):>4s} rr20={str(r['rr20']):>4s} rr5={str(r['rr5']):>4s}")

json.dump({'rows': rows, 'lat': {'p50': ms[len(ms)//2], 'p90': ms[int(len(ms)*0.9)], 'max': ms[-1]}},
          open(r'C:/Users/wulezhi/.boshi/logs/e_rerank.json', 'w', encoding='utf-8'),
          ensure_ascii=False, indent=1)
print('\nsaved logs/e_rerank.json')
