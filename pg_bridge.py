"""伯仕记忆系统 —— PostgreSQL (pgvector) 后端
=================================================
与 chroma_bridge **同函数名 / 同签名 / 同返回约定**，由 chroma_bridge 在
`BOSHI_BACKEND=pg` 时自动接管（见 chroma_bridge.py 末尾的切换块）。

⚠️ 约定（必须与 Chroma 后端一致，别改）：
- `search_memory` / `hybrid_search` 返回的 `score` = **余弦距离**（越小越近，0..2）
  —— boshi_core 会做 `1 - score` 转成相似度、再降序排序
- 记忆条目存 `boshi.memories`（非图谱），图谱边存 `boshi.graph_edges`
  → 「type=relation 的隔离」在 PG 里是**天然的表隔离**，不再需要 where 补丁

连接：`BOSHI_PG_DSN`（默认 postgresql://postgres@127.0.0.1:5432/hermes_data）
"""
from __future__ import annotations

import json
import os
from typing import Any, Dict, List, Optional

PG_DSN = os.environ.get("BOSHI_PG_DSN") or \
    "postgresql://postgres@127.0.0.1:5432/hermes_data"

_conn_holder: Dict[str, Any] = {}
_ef_holder: Dict[str, Any] = {}


def _conn():
    """模块级单例连接（psycopg3，自动重连）。"""
    import psycopg
    c = _conn_holder.get("c")
    if c is None or c.closed:
        c = psycopg.connect(PG_DSN, autocommit=True)
        _conn_holder["c"] = c
    return c


def _ef():
    """复用 chroma_bridge 的 ONNX 嵌入函数（bge-m3, 1024 维）。"""
    if "ef" not in _ef_holder:
        from chroma_bridge import _get_embedding_function
        _ef_holder["ef"] = _get_embedding_function()
    return _ef_holder["ef"]


# ── 归属 / scope（复用 chroma_bridge 的实现，避免两套规则）────────────
def _resolve_profile() -> str:
    from chroma_bridge import resolve_profile
    return resolve_profile()


def _default_scope(me: str) -> str:
    from chroma_bridge import default_scope
    return default_scope(me)


def _vec_literal(v) -> str:
    return "[" + ",".join("%.7g" % float(x) for x in v) + "]"


# ── where DSL → SQL ───────────────────────────────────────────────
def _cond(key: str, val) -> tuple:
    """单条件 → (sql, params)"""
    k = key.replace("'", "")
    if key == "isLatest":
        return ("is_latest = %s", [bool(val)])
    if key == "profile":
        return ("profile IS NOT DISTINCT FROM %s", [val])
    if key == "type":
        # 图谱边已分表；这里只处理"排除 relation"的常见诉求
        if isinstance(val, dict) and "$ne" in val:
            return ("TRUE" if val["$ne"] == "relation" else "FALSE", [])
        return ("meta->>'type' = %s", [val])
    if isinstance(val, dict):
        for op, sqlop in (("$eq", "="), ("$ne", "IS DISTINCT FROM")):
            if op in val:
                return ("meta->>'%s' %s %%s" % (k, sqlop), [val[op]])
        for op, sqlop in (("$gte", ">="), ("$lte", "<="), ("$gt", ">"), ("$lt", "<")):
            if op in val:
                # jsonb 取出来是 text，必须显式转 numeric 才能比较
                return ("(meta->>'%s')::numeric %s %%s" % (k, sqlop), [val[op]])
    return ("meta->>'%s' = %%s" % k, [val])


def _where_sql(where: Optional[dict], scope: Optional[str], me: str,
               include_graph: bool = False) -> tuple:
    """把 Chroma 风格的 where + scope 翻成 SQL 条件（作用于 boshi.memories）。

    支持 $and / $or 任意嵌套；isLatest/profile 走真列，其余键走 meta jsonb。
    """

    def build(w: dict) -> tuple:
        if not w:
            return "", []
        subs: List[str] = []
        ps: List[Any] = []
        for k, v in w.items():
            if k in ("$and", "$or") and isinstance(v, list):
                inner: List[str] = []
                for x in v:
                    s, p = build(x)
                    if s:
                        inner.append(s)
                        ps.extend(p)
                if inner:
                    subs.append("(" + (" AND " if k == "$and" else " OR ").join(inner) + ")")
            else:
                s, p = _cond(k, v)
                if s:
                    subs.append(s)
                    ps.extend(p)
        if not subs:
            return "", []
        return "(" + " AND ".join(subs) + ")", ps

    base, params = build(where or {})
    parts = [base or "TRUE"]

    # scope：self → 只看自己的 profile；all → 全库
    sc = scope if scope is not None else _default_scope(me or _resolve_profile())
    if sc != "all":
        parts.append("profile IS NOT DISTINCT FROM %s")
        params.append(me or _resolve_profile())

    # include_graph=False 时无需额外条件：图谱边本就存在另一张表里
    return " AND ".join(parts), params


def _row_to_item(row) -> dict:
    """(id, content, meta, dist) → 与 Chroma 后端同构的 dict"""
    rid, content, meta, dist = row
    meta = meta or {}
    return {"id": str(rid), "content": content or "",
            "metadata": meta, "score": float(dist) if dist is not None else 0.0}


# 返回给调用方的 metadata 必须与 Chroma 后端同形：把拆成列的 profile/isLatest 拼回去
_META_SEL = ("meta || jsonb_build_object('profile', profile, 'isLatest', is_latest) "
             "AS meta")


# ── 公开 API（与 chroma_bridge 同名同签名）────────────────────────
def add_memory(content: str, metadata: dict = None, memory_id: str = None):
    """写入一条记忆。metadata 为 None 时按当前进程归属打标（与 Chroma 后端一致）。"""
    import uuid
    c = _conn()
    meta = dict(metadata or {})
    mid = memory_id or str(uuid.uuid4())
    prof = meta.pop("profile", None) or _resolve_profile()
    is_latest = bool(meta.pop("isLatest", True))
    ts = meta.pop("timestamp", None)
    vc = meta.pop("_version_created", None)
    topic = meta.pop("topic", None)
    source = meta.pop("source", None)
    role = meta.pop("role", None)
    sid = meta.pop("session_id", None)
    meta.pop("type", None)          # 关系边不入此表
    v = _ef()([content or " "])[0]
    if ts:
        try:
            from datetime import datetime, timezone
            created = datetime.fromtimestamp(float(ts), timezone.utc)
        except Exception:
            created = None
    else:
        created = None
        meta.setdefault("timestamp", None)
    with c.cursor() as cur:
        cur.execute("""
            INSERT INTO boshi.memories
              (id, content, embedding, profile, source, topic, role, session_id,
               meta, is_latest, created_at, version_created)
            VALUES (%s,%s,%s::vector,%s,%s,%s,%s,%s,%s::jsonb,%s,%s,%s)
            RETURNING id
        """, (mid, content, _vec_literal(v), prof, source, topic, role, sid,
              json.dumps(meta, ensure_ascii=False), is_latest, created, vc))
        return str(cur.fetchone()[0])      # ← 与 chroma_bridge 契约一致：返回 id 字符串


def add_memories_batch(entries: list):
    """与 chroma_bridge 契约一致：{"added": n, "failed": m, "errors": [...]}"""
    added = failed = 0
    errors = []
    for e in (entries or []):
        try:
            add_memory(e.get("content", ""), e.get("metadata"))
            added += 1
        except Exception as ex:  # noqa: BLE001
            failed += 1
            errors.append(str(ex)[:120])
    return {"added": added, "failed": failed, "errors": errors}


def search_memory(query: str, top_k: int = 5, where: dict = None,
                  all_versions: bool = False, include_graph: bool = False,
                  scope: str = None, me: str = None):
    c = _conn()
    w = dict(where or {})
    if not all_versions:
        w.setdefault("isLatest", True)
    cond, params = _where_sql(w, scope, me or _resolve_profile(), include_graph)
    v = _vec_literal(_ef()([query or " "])[0])
    sql = f"""
        SELECT id, content, {_META_SEL}, embedding <=> %s::vector AS dist
        FROM boshi.memories
        WHERE {cond}
        ORDER BY embedding <=> %s::vector
        LIMIT %s
    """
    with c.cursor() as cur:
        cur.execute(sql, [v] + params + [v, int(min(top_k, 100))])
        rows = cur.fetchall()
    out = [_row_to_item(r) for r in rows]

    if include_graph:      # 需要图谱联想时，并入边表结果（与 Chroma 后端行为对齐）
        with c.cursor() as cur:
            cur.execute("""
                SELECT id, content, jsonb_build_object('type','relation',
                       'entity_a',entity_a,'entity_b',entity_b,'relation',relation) AS meta,
                       embedding <=> %s::vector AS dist
                FROM boshi.graph_edges ORDER BY embedding <=> %s::vector LIMIT %s
            """, (v, v, int(min(top_k, 100))))
            out += [_row_to_item(r) for r in cur.fetchall()]
        out.sort(key=lambda x: x["score"])
        out = out[:top_k]
    return out


def hybrid_search(query: str, top_k: int = 5, where: dict = None,
                  all_versions: bool = False, search_sessions: bool = True,
                  include_graph: bool = False, scope: str = None, me: str = None):
    """语义检索（PG）+ 会话历史（Hermes state.db FTS，与存储后端无关）。"""
    mems = search_memory(query, top_k=top_k, where=where, all_versions=all_versions,
                         include_graph=include_graph, scope=scope, me=me)
    sessions: List[dict] = []
    if search_sessions:
        try:
            sessions = _search_sessions(query, top_k)
        except Exception:
            sessions = []
    return {"memories": mems, "sessions": sessions, "source": "hybrid"}


def _search_sessions(query: str, top_k: int = 5) -> List[dict]:
    """复用 Hermes state.db 的 FTS（与 Chroma 后端同一数据源，中文用 trigram）。"""
    import sqlite3
    import time as _t
    home = os.environ.get("LOCALAPPDATA", os.path.expanduser("~/AppData/Local"))
    db = os.path.join(home, "hermes", "state.db")
    if not os.path.exists(db):
        return []
    q = (query or "").strip()
    if not q:
        return []
    cutoff = _t.time() - 2592000
    con = sqlite3.connect("file:%s?mode=ro" % db.replace("\\", "/"), uri=True)
    con.text_factory = str
    rows = []
    try:
        rows = con.execute("""
            SELECT m.session_id, substr(m.content,1,500), m.role, m.timestamp, s.source, s.title
            FROM messages_fts f JOIN messages m ON m.id = f.rowid
            JOIN sessions s ON m.session_id = s.id
            WHERE messages_fts MATCH ? AND m.role IN ('user','assistant')
              AND m.timestamp > ? AND s.message_count >= 2
            ORDER BY m.timestamp DESC LIMIT ?
        """, ('"%s"' % q.replace('"', '""'), cutoff, top_k)).fetchall()
    except Exception:
        rows = []
    con.close()
    out = []
    for sid, content, role, ts, source, title in rows:
        out.append({"session_id": sid, "source": source, "title": title,
                    "snippet": content, "role": role, "timestamp": ts})
    return out


def get_time_range(since: float, until: float = None, top_k: int = 50,
                   scope: str = None, me: str = None, include_graph: bool = False):
    c = _conn()
    cond, params = _where_sql({"isLatest": True}, scope, me or _resolve_profile())
    sql = f"SELECT id, content, {_META_SEL}, NULL FROM boshi.memories WHERE {cond} AND created_at >= to_timestamp(%s)"
    ps = params + [float(since)]
    if until:
        sql += " AND created_at <= to_timestamp(%s)"
        ps.append(float(until))
    sql += " ORDER BY created_at DESC LIMIT %s"
    ps.append(int(top_k))
    with c.cursor() as cur:
        cur.execute(sql, ps)
        return [_row_to_item(r) for r in cur.fetchall()]


def get_recent(n: int = 10, scope: str = None, me: str = None,
               include_graph: bool = False):
    c = _conn()
    cond, params = _where_sql({"isLatest": True}, scope, me or _resolve_profile())
    with c.cursor() as cur:
        cur.execute(f"""SELECT id, content, {_META_SEL}, NULL FROM boshi.memories
                        WHERE {cond} ORDER BY created_at DESC NULLS LAST LIMIT %s""",
                    params + [int(n)])
        return [_row_to_item(r) for r in cur.fetchall()]


def get_total_count() -> int:
    c = _conn()
    with c.cursor() as cur:
        cur.execute("SELECT count(*) FROM boshi.memories")
        m = cur.fetchone()[0]
        cur.execute("SELECT count(*) FROM boshi.graph_edges")
        e = cur.fetchone()[0]
    return int(m + e)


def delete_memory(memory_id: str):
    """删除一条记忆（含同名图谱边）。契约与 chroma_bridge 一致：返回 True/False。"""
    c = _conn()
    try:
        with c.cursor() as cur:
            cur.execute("DELETE FROM boshi.memories WHERE id = %s", (memory_id,))
            n = cur.rowcount
            cur.execute("DELETE FROM boshi.graph_edges WHERE id = %s", (memory_id,))
            n += cur.rowcount
        return n > 0
    except Exception:  # noqa: BLE001
        return False


def delete_memories(ids: list):
    """批量删除。契约与 chroma_bridge 一致：返回 True/False。"""
    ok = True
    for i in (ids or []):
        ok = delete_memory(i) and ok
    return ok


def update_memory(memory_id: str, new_content: str, new_metadata: dict = None):
    """追加式更新：旧版本置 is_latest=false，写入新版本并继承 profile。
    契约与 chroma_bridge 一致：返回**新版本 id 字符串**；找不到则返回 None。"""
    import uuid
    c = _conn()
    with c.cursor() as cur:
        cur.execute("SELECT profile FROM boshi.memories WHERE id = %s", (memory_id,))
        row = cur.fetchone()
    if not row:
        return None
    prof = row[0]
    meta = dict(new_metadata or {})
    meta.setdefault("profile", prof)
    meta.setdefault("supersedes", memory_id)
    with c.cursor() as cur:
        cur.execute("UPDATE boshi.memories SET is_latest = false WHERE id = %s", (memory_id,))
    return add_memory(new_content, meta, str(uuid.uuid4()))


def deprecate_memory(memory_id: str, superseded_by: str = None):
    """标记为旧版本。契约与 chroma_bridge 一致：返回 True/False。"""
    c = _conn()
    try:
        with c.cursor() as cur:
            cur.execute("""UPDATE boshi.memories SET is_latest = false,
                           meta = meta || %s::jsonb WHERE id = %s""",
                        (json.dumps({"superseded_by": superseded_by}), memory_id))
        return True
    except Exception:  # noqa: BLE001
        return False


def get_all_relations(top_k: int = 10000) -> list:
    c = _conn()
    with c.cursor() as cur:
        cur.execute("""SELECT id, content, jsonb_build_object('type','relation',
                       'entity_a',entity_a,'entity_b',entity_b,'relation',relation) AS meta
                       FROM boshi.graph_edges LIMIT %s""", (int(top_k),))
        return [{"id": str(r[0]), "content": r[1], "metadata": r[2]} for r in cur.fetchall()]


# ── 以下功能 PG 后端暂未实现（现役路径不依赖）────────────────────
def _not_impl(name: str):
    raise NotImplementedError(
        "%s 尚未在 PG 后端实现（现役链路不使用）；如需使用请暂用 chroma 后端" % name)


def auto_forget(dry_run: bool = False):
    _not_impl("auto_forget")


def detect_conflicts(query: str = "", top_k: int = 10, **kw):
    _not_impl("detect_conflicts")


def resolve_conflict(winner_id: str, loser_id: str, reason: str = "") -> bool:
    _not_impl("resolve_conflict")
