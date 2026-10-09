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
import subprocess
import time
from datetime import datetime as _dt, timezone as _timezone
from typing import Any, Dict, List, Optional

PG_DSN = os.environ.get("BOSHI_PG_DSN") or \
    "postgresql://postgres@127.0.0.1:5432/hermes_data"

# ── PG 自动拉起（钩子：任何 agent 首次访问伯仕时自愈）──────────────────
# 设计（2026-10-10，用户选型「钩子」）：
#   1. 所有接入方（Hermes 插件 / MCP server / 桥接 / CLI）都经过本模块 → 钩子装这里 = 100% 覆盖
#   2. 并发安全：文件锁 + 二次确认，只让一个进程去 start，其余等就绪
#   3. **失败响亮报错，绝不静默退回 Chroma**（否则会一半写 PG 一半写 Chroma → 数据分叉）
#   4. 启动必须**脱离调用方进程树**，否则 agent 进程被杀会把 PG 连带杀死
PG_BIN = os.environ.get("BOSHI_PG_BIN", r"J:/pgsql/bin")
PG_DATA = os.environ.get("BOSHI_PG_DATA", r"J:/pgdata")
PG_AUTOSTART = (os.environ.get("BOSHI_PG_AUTOSTART", "1").strip() != "0")
PG_START_TIMEOUT = float(os.environ.get("BOSHI_PG_START_TIMEOUT", "90"))
_LOCK = os.path.expanduser("~/.boshi/pg_autostart.lock")
_LOG = os.path.expanduser("~/.boshi/logs/pg_autostart.log")

_conn_holder: Dict[str, Any] = {}
_ef_holder: Dict[str, Any] = {}


def _log(msg: str) -> None:
    try:
        os.makedirs(os.path.dirname(_LOG), exist_ok=True)
        with open(_LOG, "a", encoding="utf-8") as f:
            f.write("%s %s\n" % (time.strftime("%Y-%m-%d %H:%M:%S"), msg))
    except Exception:
        pass


def _try_connect(timeout: float = 2.0):
    """尝试连接；成功返回连接对象，失败返回 None。"""
    try:
        import psycopg
        return psycopg.connect(PG_DSN, connect_timeout=timeout, autocommit=True)
    except Exception:
        return None


def _server_alive() -> bool:
    c = _try_connect(2.0)
    if c is None:
        return False
    try:
        with c.cursor() as cur:
            cur.execute("SELECT 1")
            cur.fetchone()
        return True
    except Exception:
        return False
    finally:
        try:
            c.close()
        except Exception:
            pass


def _acquire_lock(max_age: float = 60.0) -> bool:
    """抢占启动锁；陈锁（>max_age 秒，说明上个持锁进程崩了）可接管。"""
    try:
        if os.path.exists(_LOCK):
            if time.time() - os.path.getmtime(_LOCK) > max_age:
                os.remove(_LOCK)
            else:
                return False
        fd = os.open(_LOCK, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        os.write(fd, str(os.getpid()).encode())
        os.close(fd)
        return True
    except FileExistsError:
        return False
    except Exception:
        return False


def _release_lock() -> None:
    try:
        os.remove(_LOCK)
    except Exception:
        pass


def _ctl_log_path() -> str:
    return os.path.join(os.path.dirname(_LOG), "pg_ctl_start.log")


def _log_ctl_output() -> None:
    """把 pg_ctl 自己的输出附到日志（原实现丢 DEVNULL，故障时无从下手）。"""
    try:
        p = _ctl_log_path()
        if not os.path.exists(p):
            return
        with open(p, "rb") as f:
            data = f.read()[-1500:]
        txt = data.decode("utf-8", "replace")
        if "\ufffd" in txt:                      # 是 GBK 报错就换 GBK 解
            txt = data.decode("gbk", "replace")
        lines = [l.strip() for l in txt.strip().splitlines() if l.strip()]
        if lines:
            _log("pg_ctl 输出: " + " | ".join(lines[-4:]))
    except Exception:  # noqa: BLE001
        pass


def _diagnose_start_failure() -> None:
    """启动失败时的定点体检：pg.log 是否被占（孤儿进程持有句柄是最常见原因）。"""
    log = os.path.join(PG_DATA, "pg.log")
    try:
        with open(log, "ab"):
            pass
        _log("体检: pg.log 可写（不是文件占用问题）")
    except PermissionError:
        _log("体检: ⚠️ pg.log 被占用（Permission denied）→ 极可能有**孤儿 postgres 子进程**"
             "持有句柄（postmaster 已死、子进程还在）。处理：taskkill /F 杀掉该 PID 后再启动")
    except Exception as e:  # noqa: BLE001
        _log("体检: pg.log 检查异常 %s" % e)


def _start_server() -> None:
    """脱离调用方进程树启动 PG（Windows 用 DETACHED_PROCESS，避免被 agent 生命周期连带杀死）。"""
    exe = os.path.join(PG_BIN, "pg_ctl.exe" if os.name == "nt" else "pg_ctl")
    log = os.path.join(PG_DATA, "pg.log")
    cmd = [exe, "-D", PG_DATA, "-l", log, "start"]
    kw = dict(stdin=subprocess.DEVNULL, close_fds=True)
    # pg_ctl 自身的报错落文件（不能用 DEVNULL，否则"启动失败"永远是黑盒）。
    # 安全：postmaster 的输出由 pg_ctl 经 -l 重定向到 pg.log，不会继承这个句柄。
    try:
        ctl_fh = open(_ctl_log_path(), "ab", buffering=0)
        kw["stdout"] = ctl_fh
        kw["stderr"] = ctl_fh
    except Exception:  # noqa: BLE001
        ctl_fh = None
        kw["stdout"] = kw["stderr"] = subprocess.DEVNULL
    try:
        if os.name == "nt":
            DETACHED_PROCESS = 0x00000008
            CREATE_NEW_PROCESS_GROUP = 0x00000200
            # ⚠️ 绝不可加 CREATE_NO_WINDOW(0x08000000)：它会创建"隐藏控制台"，
            #    postmaster 一旦与控制台绑定，控制台被关闭/收到 Ctrl+C 时会收到
            #    CTRL_CLOSE_EVENT，整库被 0xC000013A 打死（2026-10-10 实测事故，
            #    pg.log 里留下 "terminated by exception 0xC000013A" + "^C"）。
            #    只有 DETACHED_PROCESS 才是真正的"无控制台"，免疫控制台事件。
            subprocess.Popen(cmd, creationflags=DETACHED_PROCESS |
                             CREATE_NEW_PROCESS_GROUP, **kw)
        else:
            subprocess.Popen(cmd, start_new_session=True, **kw)
        _log("已发出启动命令: %s" % " ".join(cmd))
    except Exception as e:  # noqa: BLE001
        _log("启动命令执行失败: %s" % e)
    finally:
        if ctl_fh is not None:
            try:
                ctl_fh.close()
            except Exception:  # noqa: BLE001
                pass


def ensure_pg_running(assume_down: bool = False) -> bool:
    """确保 PG 可用：已在跑→True；否则自动拉起并等待就绪（并发安全）。

    assume_down=True 时跳过首次探测（调用方刚连失败，可省一次握手）。
    """
    if not PG_AUTOSTART:
        _log("BOSHI_PG_AUTOSTART=0，跳过自动拉起")
        return False
    if not assume_down and _server_alive():
        return True
    got = _acquire_lock()
    try:
        if _server_alive():          # 拿锁后再确认一次：可能别的进程刚好拉起来了
            return True
        if got:
            _log("PG 未运行 → 自动拉起（数据目录 %s）" % PG_DATA)
            _start_server()
        else:
            _log("PG 未运行，未拿到启动锁 → 等待其它进程拉起")
        t0 = time.time()
        while time.time() - t0 < PG_START_TIMEOUT:
            time.sleep(0.25)
            if _server_alive():
                _log("PG 就绪（等待 %.1f 秒）" % (time.time() - t0))
                return True
        _log("PG 拉起失败或超时（%.0f 秒）" % PG_START_TIMEOUT)
        _log_ctl_output()
        _diagnose_start_failure()
        return False
    finally:
        if got:
            _release_lock()


def _conn():
    """模块级单例连接；连不上时先尝试自动拉起 PG，仍失败则**响亮报错**（不静默退回 Chroma）。"""
    import psycopg
    c = _conn_holder.get("c")
    if c is not None and not c.closed:
        return c
    try:
        c = psycopg.connect(PG_DSN, connect_timeout=3, autocommit=True)
    except Exception as e:  # noqa: BLE001
        if not ensure_pg_running(assume_down=True):
            raise RuntimeError(
                "伯仕 PG 后端不可用：连接 %s 失败，且自动拉起未成功。\n"
                "  排查：① 查看自动拉起日志 %s ② 手动运行 J:\\pgsql\\pg_start.cmd "
                "③ 临时绕过：去掉环境变量 BOSHI_BACKEND（回 Chroma）" % (PG_DSN, _LOG)) from e
        c = psycopg.connect(PG_DSN, connect_timeout=5, autocommit=True)
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
def _norm_ts(ts) -> Optional[float]:
    """把各种形态的时间戳统一成 Unix 秒：float/int 直接用；ISO 字符串解析；
    无法解析 → None（调用方会补当前时间）。

    必须做这一步：boshi_core.save() 传的是 `datetime.now(timezone.utc).isoformat()`
    （**ISO 字符串**），直接 float() 会抛异常 → created_at 落 NULL → 该条记忆对
    time_range/recent 等时间线查询永久隐形（2026-10-10 实测，插件/MCP 路径全中招）。
    Chroma 侧等价逻辑见 `chroma_bridge._normalize_metadata`。
    """
    if ts is None or ts == "":
        return None
    if isinstance(ts, (int, float)):
        return float(ts)
    s = str(ts).strip()
    try:
        dt = _dt.fromisoformat(s.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=_timezone.utc)
        return dt.timestamp()
    except Exception:  # noqa: BLE001
        try:
            return float(s)
        except Exception:  # noqa: BLE001
            return None


def add_memory(content: str, metadata: dict = None, memory_id: str = None):
    """写入一条记忆。metadata 为 None 时按当前进程归属打标（与 Chroma 后端一致）。"""
    import uuid
    c = _conn()
    meta = dict(metadata or {})
    mid = memory_id or str(uuid.uuid4())
    prof = meta.pop("profile", None) or _resolve_profile()
    is_latest = bool(meta.pop("isLatest", True))
    ts = _norm_ts(meta.pop("timestamp", None))
    vc = meta.pop("_version_created", None)
    topic = meta.pop("topic", None)
    source = meta.pop("source", None)
    role = meta.pop("role", None)
    sid = meta.pop("session_id", None)

    # ── 图谱边路由：type=relation（或带 entity_a/entity_b）→ 写进 graph_edges 表 ──
    # Chroma 时代边和记忆同 collection；PG 里分了表，写入必须分流，否则边会被当成记忆
    if meta.get("type") == "relation" or ("entity_a" in meta and "entity_b" in meta):
        ea = meta.get("entity_a", "") or ""
        eb = meta.get("entity_b", "") or ""
        rel = meta.get("relation", "") or ""
        mv = _ef()([content or " "])[0]
        with c.cursor() as cur:
            cur.execute("""INSERT INTO boshi.graph_edges
                           (id, entity_a, entity_b, relation, content, embedding, version_created)
                           VALUES (%s,%s,%s,%s,%s,%s::vector,%s)
                           ON CONFLICT (id) DO NOTHING""",
                        (mid, ea, eb, rel, content, _vec_literal(mv), vc))
        return mid

    v = _ef()([content or " "])[0]
    from datetime import datetime, timezone
    try:
        if not ts:
            # 调用方未传 timestamp 时**必须补当前时间**：否则 created_at 落 NULL，
            # 该条记忆对 time_range / recent 等时间线查询**永久隐形**
            # （2026-10-10 实测：PG 库里已积累 11 条 created_at=NULL 的记录）
            ts = time.time()
        created = datetime.fromtimestamp(float(ts), timezone.utc)
        meta.setdefault("timestamp", float(ts))
    except Exception:
        created = None
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


def _scan_type(w: dict):
    """扫描 where，判断是否要检索「图谱边」（type=='relation'），并取出 rel_type。"""
    want, rel_type = False, None

    def walk(x):
        nonlocal want, rel_type
        if not isinstance(x, dict):
            return
        for k, v in x.items():
            if k in ("$and", "$or") and isinstance(v, list):
                for y in v:
                    walk(y)
            elif k == "type" and v == "relation":
                want = True
            elif k == "rel_type":
                rel_type = v

    walk(w)
    return want, rel_type


def _search_edges(query: str, top_k: int, rel_type: str = None):
    """在 graph_edges 表里做语义检索（Chroma 时代边与记忆同表，PG 里必须分表查）。"""
    c = _conn()
    v = _vec_literal(_ef()([query or " "])[0])
    sql = ("""SELECT id, content,
              jsonb_build_object('type','relation','entity_a',entity_a,
                                 'entity_b',entity_b,'relation',relation) AS meta,
              embedding <=> %s::vector AS dist
              FROM boshi.graph_edges""")
    ps = [v]
    if rel_type:
        sql += " WHERE relation = %s"
        ps.append(rel_type)
    sql += " ORDER BY embedding <=> %s::vector LIMIT %s"
    ps += [v, int(min(top_k, 200))]
    with c.cursor() as cur:
        cur.execute(sql, ps)
        return [_row_to_item(r) for r in cur.fetchall()]


def search_memory(query: str, top_k: int = 5, where: dict = None,
                  all_versions: bool = False, include_graph: bool = False,
                  scope: str = None, me: str = None):
    c = _conn()
    w = dict(where or {})
    if not all_versions:
        w.setdefault("isLatest", True)

    # 图谱边检索请求 → 走 graph_edges 表（与 Chroma 后端行为对齐）
    want_rel, rel_type = _scan_type(w)
    if want_rel:
        return _search_edges(query, top_k, rel_type)

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


def get_by_id(memory_id: str):
    """按 id 取单条记忆（先查记忆表，再查图谱边表）→ {"id","content","metadata"} 或 None。

    与 chroma_bridge 同契约；图谱/版本链等只读场景走它，避免绕过后端调度。
    """
    c = _conn()
    with c.cursor() as cur:
        cur.execute("SELECT id, content, " + _META_SEL +
                    " FROM boshi.memories WHERE id = %s", (memory_id,))
        row = cur.fetchone()
        if row:
            return {"id": str(row[0]), "content": row[1] or "", "metadata": row[2] or {}}
        cur.execute("""SELECT id, content,
                       jsonb_build_object('type','relation','entity_a',entity_a,
                                          'entity_b',entity_b,'relation',relation) AS meta
                       FROM boshi.graph_edges WHERE id = %s""", (memory_id,))
        row = cur.fetchone()
        if row:
            return {"id": str(row[0]), "content": row[1] or "", "metadata": row[2] or {}}
    return None


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
