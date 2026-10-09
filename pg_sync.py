"""伯仕增量追平：把 Chroma 侧新增（PG 里没有）的记忆/边补进 PG。

用法（boshi venv）：
    python pg_sync.py            # 只报告差异
    python pg_sync.py --apply    # 实际写入

设计：
- **永远以 Chroma 为源**（独立于 BOSHI_BACKEND，直接按指针路径读），避免切换混乱
- 只补 PG 缺失的 id（幂等，可反复跑）
- 边/记忆按 type 分流到 graph_edges / memories
- 走 pg_bridge 的连接（含"PG 没跑就自动拉起"）
"""
import io
import json
import os
import sys
import time

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
BOSHI = r'C:/Users/wulezhi/.boshi'
sys.path.insert(0, BOSHI)

import chromadb  # noqa: E402
from onnx_embed import get_embedding_function  # noqa: E402
from chroma_bridge import CHROMA_DIR  # noqa: E402
import pg_bridge  # noqa: E402

APPLY = '--apply' in sys.argv
ef = get_embedding_function()


def pg_ids():
    c = pg_bridge._conn()
    with c.cursor() as cur:
        cur.execute('SELECT id::text FROM boshi.memories')
        mem = {r[0] for r in cur.fetchall()}
        cur.execute('SELECT id::text FROM boshi.graph_edges')
        edg = {r[0] for r in cur.fetchall()}
    return mem, edg


def chroma_pages(page=2000):
    col = chromadb.PersistentClient(path=CHROMA_DIR).get_collection(
        'boshi_memory', embedding_function=ef)
    total = col.count()
    off = 0
    while off < total:
        r = col.get(limit=page, offset=off, include=['metadatas', 'documents', 'embeddings'])
        ids = r.get('ids') or []
        if not ids:
            break
        yield ids, r['metadatas'], r['documents'], r['embeddings']
        off += len(ids)


mem_have, edg_have = pg_ids()
print('PG 现有: memories=%d edges=%d' % (len(mem_have), len(edg_have)))

new_mem, new_edg = [], []
for ids, metas, docs, embs in chroma_pages():
    for i, cid in enumerate(ids):
        m = metas[i] or {}
        if m.get('type') == 'relation':
            if cid not in edg_have:
                new_edg.append((cid, m, docs[i], embs[i]))
        elif cid not in mem_have:
            new_mem.append((cid, m, docs[i], embs[i]))

print('待补: 记忆 %d 条 / 边 %d 条' % (len(new_mem), len(new_edg)))
if not APPLY:
    print('（未加 --apply，仅报告。加 --apply 才会写入）')
    sys.exit(0)

c = pg_bridge._conn()
t0 = time.time()


def vec(x):
    return '[' + ','.join('%.7g' % float(v) for v in x) + ']'


SKIP = {'timestamp', '_version_created', 'isLatest', 'source', 'topic', 'role',
        'session_id', 'profile', 'type'}
for cid, m, doc, emb in new_mem:
    ts = m.get('timestamp')
    created = None
    if ts:
        try:
            from datetime import datetime, timezone
            created = datetime.fromtimestamp(float(ts), timezone.utc)
        except Exception:
            created = None
    meta = {k: v for k, v in m.items() if k not in SKIP}
    with c.cursor() as cur:
        cur.execute("""INSERT INTO boshi.memories
            (id, content, embedding, profile, source, topic, role, session_id,
             meta, is_latest, created_at, version_created)
            VALUES (%s,%s,%s::vector,%s,%s,%s,%s,%s,%s::jsonb,%s,%s,%s)
            ON CONFLICT (id) DO NOTHING""",
            (cid, doc or '', vec(emb), m.get('profile'), m.get('source'), m.get('topic'),
             m.get('role'), m.get('session_id'), json.dumps(meta, ensure_ascii=False),
             bool(m.get('isLatest', True)), created, m.get('_version_created')))

for cid, m, doc, emb in new_edg:
    with c.cursor() as cur:
        cur.execute("""INSERT INTO boshi.graph_edges
            (id, entity_a, entity_b, relation, content, embedding, version_created)
            VALUES (%s,%s,%s,%s,%s,%s::vector,%s)
            ON CONFLICT (id) DO NOTHING""",
            (cid, m.get('entity_a', ''), m.get('entity_b', ''), m.get('relation', ''),
             doc or '', vec(emb), m.get('_version_created')))

print('✅ 补入完成，用时 %.1f 秒' % (time.time() - t0))
mem_have2, edg_have2 = pg_ids()
print('PG 现状: memories=%d edges=%d' % (len(mem_have2), len(edg_have2)))
