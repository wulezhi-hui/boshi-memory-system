# 先冒烟：确认 11437 /rerank 返回结构与分数方向
import json, sys, time, requests
sys.path.insert(0, r'C:/Users/wulezhi/.boshi')
import os
os.environ.setdefault('BOSHI_FM_STATS', '0')
import pg_bridge as P
from eval.cases_a9 import CASES

kind, gid, anchor, q = [c for c in CASES if c[0] == 'paraphrase'][0]
print('case:', kind, gid, anchor, q)
c = P._conn()
with c.cursor() as cur:
    cur.execute("SELECT content FROM boshi.memories WHERE is_latest AND left(id::text,8)=%s", (gid,))
    gold = cur.fetchone()[0]
    cur.execute("SELECT content FROM boshi.memories WHERE is_latest ORDER BY id LIMIT 4")
    others = [r[0] for r in cur.fetchall()]
docs = [gold] + others
t0 = time.time()
r = requests.post('http://127.0.0.1:11437/rerank',
                  json={'model': 'bge-reranker-v2-m3', 'query': q, 'documents': docs},
                  timeout=60)
print('status', r.status_code, 'latency %.2fs' % (time.time() - t0))
print(json.dumps(r.json(), ensure_ascii=False)[:800])
