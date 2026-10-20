"""伯仕记忆系统 —— PostgreSQL (pgvector) 后端
=================================================
与 chroma_bridge **同函数名 / 同签名 / 同返回约定**，由 chroma_bridge 在
`BOSHI_BACKEND=pg` 时自动接管（见 chroma_bridge.py 末尾的切换块）。

⚠️ 约定（必须与 Chroma 后端一致，别改）：
- `search_memory` / `hybrid_search` 返回的 `score` = **距离**（越小越近，0..2）
  —— boshi_core 会做 `1 - score` 转成相似度、再降序排序
- 距离语义分两种（2026-10-19 P0 起）：
  · 默认 `fusion=rrf`（向量 + 关键词 RRF 融合）→ score 是**伪距离** `1-sim`，
    sim = 0.45+0.55*rrf_norm，单调且落在 0.7~1.0 习惯区间，排序意义不变
  · `BOSHI_SEARCH_FUSION=vector` 或 `fusion="vector"` → 纯余弦距离（旧行为，回滚用）
- 记忆条目存 `boshi.memories`（非图谱），图谱边存 `boshi.graph_edges`
  → 「type=relation 的隔离」在 PG 里是**天然的表隔离**，不再需要 where 补丁

连接：`BOSHI_PG_DSN`（默认 postgresql://postgres@127.0.0.1:5432/hermes_data）
"""
from __future__ import annotations

import json
import math
import os
import re
import subprocess
import threading
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


PG_SERVICE = os.environ.get("BOSHI_PG_SERVICE") or "boshi-pg"


def _service_state() -> str:
    """Windows 服务状态：RUNNING / STOPPED / START_PENDING / STOP_PENDING / absent / unknown。"""
    if os.name != "nt":
        return "absent"
    try:
        r = subprocess.run(["sc.exe", "query", PG_SERVICE], capture_output=True, timeout=15)
        txt = ((r.stdout or b"") + (r.stderr or b"")).decode("gbk", "replace").upper()
        if "1060" in txt:          # 指定的服务未安装
            return "absent"
        for st in ("START_PENDING", "STOP_PENDING", "RUNNING", "STOPPED"):
            if st in txt:
                return st
        return "unknown"
    except Exception:  # noqa: BLE001
        return "unknown"


def _start_server() -> None:
    """启动 PG。

    Windows 机制事实（2026-10-10 实测，血泪）：
    1. **不能直接启动 postgres.exe**：调用方若是管理员令牌，PG 会拒绝启动并报
       "不能以管理员权限的用户运行 PostgreSQL 服务器" —— pg_ctl 会用**受限令牌**
       启动 postmaster，这正是 Windows 上必须经 pg_ctl 的原因。
    2. **pg_ctl 在 Windows 上固定经 `cmd.exe /C "postgres ... < nul >> LOG 2>&1"`
       启动**（源码如此，无法绕过）。若 pg_ctl 自己没有控制台（DETACHED_PROCESS），
       cmd 会**新分配一个"可见"控制台窗口**，postmaster 就挂在该控制台上。
    3. 因此只剩两种选择：**可见窗口**（DETACHED）或**隐藏控制台**（CREATE_NO_WINDOW）。
       这里选 CREATE_NO_WINDOW —— 无窗口（用户无感），且隐藏控制台无法被误关。
       但两者 postmaster 都挂在 cmd 的控制台上，控制台事件仍可能打死整库
       （2026-10-10 两次 0xC000013A）。
    4. **2026-10-10 追加：已注册 Windows 服务 `boshi-pg`（账号 NT AUTHORITY\\NetworkService，
       非管理员令牌 → PG 不再拒绝；服务无控制台 → 彻底免疫控制台事件，且开机自启）**。
       因此本函数**优先走服务**：服务在跑 → 什么都不做（交给服务自愈）；服务停着 → `sc start`；
       服务不可用/没权限 → 回退 pg_ctl 独立启动（保持旧行为，绝不至于"起不来"）。
    """
    if os.name == "nt":
        st = _service_state()
        if st in ("RUNNING", "START_PENDING", "STOP_PENDING"):
            _log("PG 服务 %s 状态=%s → 交给服务自愈，不另起实例" % (PG_SERVICE, st))
            return
        if st != "absent":
            try:
                r = subprocess.run(["sc.exe", "start", PG_SERVICE],
                                   capture_output=True, timeout=30)
                _log("尝试以服务方式启动 %s: rc=%d" % (PG_SERVICE, r.returncode))
                if r.returncode == 0:
                    return
            except Exception as e:  # noqa: BLE001
                _log("服务方式启动异常: %s" % e)
            _log("服务方式未成功 → 回退 pg_ctl 独立启动")
    log = os.path.join(PG_DATA, "pg.log")
    # pg.log 被占用（孤儿进程持句柄）时退回带时间戳的日志名，避免"启动彻底失败"
    try:
        with open(log, "ab"):
            pass
    except PermissionError:
        log = os.path.join(PG_DATA, "pg-%s.log" % time.strftime("%Y%m%d-%H%M%S"))
        _log("⚠️ pg.log 被占用，本实例日志改落 %s" % log)
    cmd = [os.path.join(PG_BIN, "pg_ctl.exe" if os.name == "nt" else "pg_ctl"),
           "-D", PG_DATA, "-l", log, "start"]
    kw = dict(stdin=subprocess.DEVNULL, close_fds=True)
    # pg_ctl 自身的报错落文件（不能丢 DEVNULL，否则"启动失败"永远是黑盒）
    try:
        out_fh = open(_ctl_log_path(), "ab", buffering=0)
        kw["stdout"] = out_fh
        kw["stderr"] = out_fh
    except Exception:  # noqa: BLE001
        out_fh = None
        kw["stdout"] = kw["stderr"] = subprocess.DEVNULL
    try:
        if os.name == "nt":
            CREATE_NEW_PROCESS_GROUP = 0x00000200
            CREATE_NO_WINDOW = 0x08000000   # 隐藏控制台 → 无可见窗口
            subprocess.Popen(cmd, creationflags=CREATE_NEW_PROCESS_GROUP |
                             CREATE_NO_WINDOW, **kw)
        else:
            subprocess.Popen(cmd, start_new_session=True, **kw)
        _log("已发出启动命令: %s" % " ".join(cmd))
    except Exception as e:  # noqa: BLE001
        _log("启动命令执行失败: %s" % e)
    finally:
        if out_fh is not None:
            try:
                out_fh.close()
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
    """模块级单例连接；连不上时先尝试自动拉起 PG，仍失败则**响亮报错**（不静默退回 Chroma）。

    注意：PG 被重启/崩过一次后，**旧的缓存连接会失效但对象未必被标记 closed** ——
    若直接返回，调用方会一直拿到 OperationalError（进程不重启就永远好不了）。
    所以这里对缓存连接先做一次极廉价的 `SELECT 1` 探活，失败即丢弃重建。
    """
    import psycopg
    c = _conn_holder.get("c")
    if c is not None and not c.closed:
        try:
            c.execute("SELECT 1")        # 探活（本地 socket，开销可忽略）
            return c
        except Exception:                # noqa: BLE001
            try:
                c.close()
            except Exception:            # noqa: BLE001
                pass
            _conn_holder.pop("c", None)
            c = None
    try:
        c = psycopg.connect(PG_DSN, connect_timeout=3, autocommit=True)
    except Exception as e:  # noqa: BLE001
        if not ensure_pg_running(assume_down=True):
            raise RuntimeError(
                "伯仕 PG 后端不可用：连接 %s 失败，且自动拉起未成功。\n"
                "  排查：① 查看自动拉起日志 %s ② 手动运行 J:\\pgsql\\pg_start.cmd "
                "③ 临时绕过：显式设 BOSHI_BACKEND=chroma（回 Chroma）" % (PG_DSN, _LOG)) from e
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
    if key in ("created_at", "createdAt"):
        # 真列（不在 meta 里）。值是 Unix 秒或 ISO 串；用于时间窗口检索/可复现评测
        # （评测期间自身还在往库里写，不带截止时间就无法跨轮比较名次）
        def _ts(x):
            try:
                return float(x)
            except (TypeError, ValueError):
                import datetime as _dt
                return _dt.datetime.fromisoformat(str(x)).timestamp()
        if isinstance(val, dict):
            for op, sqlop in (("$gte", ">="), ("$lte", "<="),
                              ("$gt", ">"), ("$lt", "<")):
                if op in val:
                    return ("created_at %s to_timestamp(%%s)" % sqlop, [_ts(val[op])])
            for op, sqlop in (("$eq", "="), ("$ne", "IS DISTINCT FROM")):
                if op in val:
                    return ("created_at %s to_timestamp(%%s)" % sqlop, [_ts(val[op])])
            return ("TRUE", [])
        return ("created_at = to_timestamp(%s)", [_ts(val)])
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


# 返回给调用方的 metadata 必须与 Chroma 后端同形：把拆成列的 profile/isLatest 拼回去，
# **并且总是补出 timestamp**：优先取 created_at 列（权威），回落到 meta 里的旧值。
# 缺了这步，凡是 meta 里没写 timestamp 的记录，调用方按 metadata.timestamp 排序/展示时
# 就会"没有时间"（2026-10-10 实测：dsh 侧正是踩到这个缺口）
_META_SEL = ("meta || jsonb_build_object('profile', profile, 'isLatest', is_latest, "
             "'timestamp', COALESCE(EXTRACT(EPOCH FROM created_at), "
             "NULLIF(meta->>'timestamp', '')::float8)) AS meta")


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


def add_memory(content: str, metadata: dict = None, memory_id: str = None,
               upsert: bool = False):
    """写入一条记忆。metadata 为 None 时按当前进程归属打标（与 Chroma 后端一致）。

    upsert=True（P3，2026-10-20）：同 `(profile, session_id, topic)` 下已有有效行时，
    先把旧行 `is_latest=false`、写新行 `is_latest=true`，并给新行 meta 打
    `supersedes=<旧 id>`。**不物理覆盖**，沿用 v5.8「不可变日志 + isLatest」约定，
    这样"每会话恰好 1 条事实记录"对读路径成立，历史仍可追溯。
    `session_id` 或 `topic` 任一为空 → 自动退化为普通 add（避免无会话的写入互相顶掉）。
    """
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
    # P2：槽位。slots 以 JSON 字符串随 metadata 传进来（Chroma 侧只收标量），
    # 落 PG 时进独立 jsonb 列，**不回填进 meta**，避免每条检索结果都驮着 ~1KB 槽位。
    _slots = meta.pop("slots", None)
    _raw_len = meta.pop("raw_len", None)
    if isinstance(_slots, str):
        try:
            _slots = json.loads(_slots)
        except Exception:
            _slots = None
    elif not isinstance(_slots, dict):
        _slots = None
    try:
        _raw_len = int(_raw_len) if _raw_len is not None else None
    except Exception:
        _raw_len = None

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

    # ── P2 槽位中心化（2026-10-19 升级）────────────────────────────────────
    # 原先只有 hermes_plugin 写助手结论时才抽槽位；MCP / 其它 agent 走
    # boshi_core.save → 这里时 slots 恒为 NULL（实测近三天 218 条 boshi_api
    # 写入全无槽位）。槽位是纯规则的元数据抽取，不该依赖调用方自觉，所以在
    # 写入唯一入口统一补齐：调用方没传就现抽，传了就用调用方的（插件已抽过，
    # 避免重复计算）。`BOSHI_WRITE_SLOTS=0` 一键关掉。
    if _slots is None and content and os.environ.get("BOSHI_WRITE_SLOTS", "1") != "0":
        try:
            _sm = _slots_mod()
            _sl = _sm.extract(content)
            if _sm.filled_count(_sl):
                _slots = _sl
            if _raw_len is None:
                _raw_len = len(content)
        except Exception:      # 抽提是增强项，坏了绝不能阻断写入
            _slots = None

    # ── P3 事实层 upsert：加锁 → 旧行降级 → 写新行 → 解锁 ────────────────
    # 用**会话级** advisory lock（`_conn()` 是 autocommit，事务级 `pg_advisory_xact_lock`
    # 会在语句结束立刻释放，等于没锁）。锁粒度 = (profile, session_id, topic)，
    # 保证同会话同主题的并发送写不会各插一条都标 is_latest。
    _upsert_lock = None
    if upsert and sid and topic:
        _upsert_lock = "boshi.upsert:%s|%s|%s" % (prof, sid, topic)
        with c.cursor() as cur:
            cur.execute("SELECT pg_advisory_lock(hashtext(%s)::bigint)", (_upsert_lock,))
        try:
            with c.cursor() as cur:
                cur.execute("""UPDATE boshi.memories SET is_latest = false
                               WHERE profile = %s AND session_id = %s AND topic = %s
                                 AND is_latest
                               RETURNING id""", (prof, sid, topic))
                _old_ids = [str(r[0]) for r in (cur.fetchall() or [])]
            if _old_ids:
                meta["supersedes"] = _old_ids[0] if len(_old_ids) == 1 else _old_ids
        except Exception:
            with c.cursor() as cur:
                cur.execute("SELECT pg_advisory_unlock(hashtext(%s)::bigint)", (_upsert_lock,))
            raise

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
    try:
        with c.cursor() as cur:
            cur.execute("""
                INSERT INTO boshi.memories
                  (id, content, embedding, profile, source, topic, role, session_id,
                   meta, is_latest, created_at, version_created, slots, raw_len)
                VALUES (%s,%s,%s::vector,%s,%s,%s,%s,%s,%s::jsonb,%s,%s,%s,%s::jsonb,%s)
                RETURNING id
            """, (mid, content, _vec_literal(v), prof, source, topic, role, sid,
                  json.dumps(meta, ensure_ascii=False), is_latest, created, vc,
                  json.dumps(_slots, ensure_ascii=False) if _slots else None, _raw_len))
            return str(cur.fetchone()[0])      # ← 与 chroma_bridge 契约一致：返回 id 字符串
    finally:
        if _upsert_lock:
            try:
                with c.cursor() as cur:
                    cur.execute("SELECT pg_advisory_unlock(hashtext(%s)::bigint)",
                                (_upsert_lock,))
            except Exception:      # 解锁失败不能盖掉写入结果；连接复用时 _conn() 会探活重建
                pass


def get_latest_by_topic(session_id: str, topic: str, profile: str = None):
    """取 `(profile, session_id, topic)` 当前有效行（is_latest=true）的正文。

    P3（2026-10-20）：只为**事实层滚动累积**服务——`session_fact` 每轮要把
    「本轮新抽到的决策/待办」并进上一版正文，先得读回上一版。不参与检索路径，
    因此不走 RRF / 不加分，找不到就返回 None（首次写入的正常情形）。
    """
    if not session_id or not topic:
        return None
    prof = profile or _resolve_profile()
    c = _conn()
    with c.cursor() as cur:
        cur.execute("""SELECT content FROM boshi.memories
                       WHERE profile = %s AND session_id = %s AND topic = %s
                         AND is_latest
                       ORDER BY created_at DESC NULLS LAST
                       LIMIT 1""", (prof, session_id, topic))
        row = cur.fetchone()
    return row[0] if row else None


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


_CJK_RE = re.compile(r"[\u4e00-\u9fff]+")
_ASCII_RE = re.compile(r"[A-Za-z][A-Za-z0-9_.+\-]{1,}")
_NUM_RE = re.compile(r"\d+(?:\.\d+)?")
# 关键词通道参数（P0）。BOSHI_SEARCH_FUSION=vector 可一键回滚到纯向量。
_RRF_K = int(os.environ.get("BOSHI_RRF_K", "60"))
_KW_MIN = float(os.environ.get("BOSHI_KW_MIN", "0.05"))  # 相对分门槛（相对本轮 top1）
_KW_MAX_TOKENS = 16
_KW_BM25_K1 = float(os.environ.get("BOSHI_BM25_K1", "1.2"))
_KW_BM25_B = float(os.environ.get("BOSHI_BM25_B", "0.50"))
_KW_TTL = float(os.environ.get("BOSHI_KW_TTL", "180"))   # 索引最长复用秒数
_KW_CACHE_MAX = int(os.environ.get("BOSHI_KW_CACHE_MAX", "4"))
# 「字面锚点」：型号(qwen3.6-27b)、端口(17891)、版本(0.7.23)、英文标识、两位以上数字。
# 有锚点时字面匹配比嵌入可靠；纯中文自然语言则相反（见 _has_anchor 注释）。
_ANCHOR_RE = re.compile(r"[A-Za-z][A-Za-z0-9_.+\-]{1,}|\d{2,}")
# kw_abs 的 CJK 部分匹配地板：文档只对上多字词的一部分（"显存"之于"显存不足"）时
# 给 _KW_PARTIAL 的信用，其余按 bigram 覆盖比例补足到 1.0。
_KW_PARTIAL = float(os.environ.get("BOSHI_KW_PARTIAL", "0.85"))
# 关键词通道的候选池上限（P0 修 A7「池污染」）。
# 0 = 跟随 k_each（旧行为）；默认 20 = 与 top_k 解耦，固定 20 条。
# 为什么：BM25 原始分尾部是噪声，池子随 top_k 变大时这些长尾文档带着中等
# kw_abs 挤进融合（融合用绝对分，不做池内归一），把真金标顶出去——实测
# top1/top3/top5 = 21/30/32 (k=5) → 21/26/29 (k=20)，纯粹「要得多反而更差」。
# 固定 20 后 5/10/20/50/100 五档指标完全一致（21/30/32）。
# 曾试过「截断尺子改成 kw_abs」（与融合同一把尺子）：k=5 时 27/29，更差——
# 因为小池子里放进高 kw_abs/低 BM25 的长尾文档就是引噪，问题在池子大小不在尺子。
_KW_POOL_CAP = int(os.environ.get("BOSHI_KW_POOL_CAP", "20") or 0)
# kw_abs 的长度阻尼系数（BM25 同形）：0 = 关闭（纯覆盖率，长文占便宜）
_KW_LENB = float(os.environ.get("BOSHI_KW_LENB", "0.5"))
# 弱证据降权（2026-10-20）：关键词通道只剩 bigram 证据时（主 token 全 df=0），
# 与向量通道等权融合会把噪声顶进 top5，故把 wk 从 0.55 降到 0.30。
_KW_WEAK_WV = float(os.environ.get("BOSHI_KW_WEAK_WV", "0.70"))
_KW_WEAK_WK = float(os.environ.get("BOSHI_KW_WEAK_WK", "0.30"))


def _has_anchor(query: str) -> bool:
    return bool(_ANCHOR_RE.search(query or ""))


def _kw_tokens(query: str) -> list:
    """query → [(token, weight)]。

    权重设计：**区分度越高权重越大**
      - 型号/端口/参数名（ASCII、数字）：3.0 / 2.5 —— 这类词语义模型最不可靠
        （实测 "Qwen3.6-27B QLoRA" 纯向量召回全是 Qwen3.8/35B，型号被吃掉了）
      - 中文整段（连续汉字，2 字及以上）：3.0
      - 中文 bigram：1.0（兜住"整段不匹配但词根匹配"的情况）

    token 里出现 % / _ 会污染 LIKE，直接剔除；长度 <2 的丢弃（单字噪声过大）。
    """
    q = query or ""
    toks: list = []
    seen = set()

    def add(t: str, w: float):
        t = (t or "").strip()
        if len(t) < 2:
            return
        low = t.lower()
        if low in seen:
            return
        seen.add(low)
        toks.append((low, float(w)))

    for m in _ASCII_RE.finditer(q):
        add(_norm_ascii(m.group(0)), 3.0)
    for m in _NUM_RE.finditer(q):
        add(m.group(0), 2.5)
    for seg in _CJK_RE.findall(q):
        n = len(seg)
        if n >= 2:
            add(seg, 3.0)
            if n >= 3:
                for i in range(min(n - 1, 6)):
                    add(seg[i:i + 2], 1.0)
    return toks[:_KW_MAX_TOKENS]


def _norm_ascii(tok: str) -> str:
    """ASCII token 归一：去掉 `_ - . / ,`，让 IQ4_XS / iq4xs / IQ4-XS 互相命中。

    实测 "IQ4XS 解码速度" 关键词通道零命中，因为库里写的是 `IQ4_XS`、`UD-IQ4_XS`。
    型号写法在语料里不统一（下划线/连字符/粘连），归一后才能对齐。
    """
    return tok.replace("_", "").replace("-", "").replace(".", "").replace("/", "").replace(",", "")


def _vec_channel(c, q: str, k: int, cond: str, params: list, v=None) -> list:
    """向量通道（纯余弦，与旧实现完全一致）。v 可传入已算好的查询向量。"""
    if v is None:
        v = _vec_literal(_ef()([q or " "])[0])
    sql = f"""
        SELECT id, content, {_META_SEL}, embedding <=> %s::vector AS dist
        FROM boshi.memories
        WHERE {cond}
        ORDER BY embedding <=> %s::vector
        LIMIT %s
    """
    with c.cursor() as cur:
        cur.execute(sql, [v] + params + [v, int(k)])
        return [_row_to_item(r) for r in cur.fetchall()]


def _cosine_for_ids(c, v: str, ids: list) -> dict:
    """给「只被关键词通道捞到」的文档补真实余弦相似度。

    不补的话它们在融合里 v=0（只是没进向量 Top-N），会被「向量通道里排名靠后但
    v 非零」的文档白占便宜，融合分也不可比。
    """
    if not ids:
        return {}
    with c.cursor() as cur:
        cur.execute("SELECT id, 1 - (embedding <=> %s::vector)::float8 "
                    "FROM boshi.memories WHERE id = ANY(%s::uuid[])", [v, list(ids)])
        return {str(r[0]): float(r[1] or 0.0) for r in cur.fetchall()}


def _doc_tokens(text: str) -> list:
    """文档侧分词（与 _kw_tokens 的 query 侧对齐）。

    中文不引分词器，用「连续汉字整段 + bigram」：整段命中最准，bigram 兜词根。
    单字不入索引（噪声大且把 avgdl 拉爆）。
    """
    low = (text or "").lower()
    toks: list = []
    toks.extend(t for t in (_norm_ascii(x) for x in _ASCII_RE.findall(low)) if len(t) >= 2)
    toks.extend(_NUM_RE.findall(low))
    for seg in _CJK_RE.findall(low):
        n = len(seg)
        if n >= 2:
            toks.append(seg)
            if n >= 3:
                toks.extend(seg[i:i + 2] for i in range(n - 1))
    return toks


_KW_LOCK = threading.Lock()
_KW_CACHE: dict = {}          # (cond, params) → 索引；只留最近几个 scope

_SLOTS_MOD = None


def _slots_mod():
    """按文件路径加载 ~/.boshi/slots.py（避免 `import slots` 撞上同名第三方模块）。"""
    global _SLOTS_MOD
    if _SLOTS_MOD is not None:
        return _SLOTS_MOD
    import importlib.util as _ilu
    import os as _os
    p = _os.path.join(_os.path.dirname(_os.path.abspath(__file__)), "slots.py")
    spec = _ilu.spec_from_file_location("boshi_slots", p)
    mod = _ilu.module_from_spec(spec)
    spec.loader.exec_module(mod)
    _SLOTS_MOD = mod
    return mod


def _doc_text(content: str, slots_json) -> str:
    """BM25 文档文本。默认**恒用原文**，slots 只当元数据（2026-10-19 A3 实测结论）。

    A3 对照（8 query × auto/rrf，详见 v7.1 §8.4）：
      · 槽位文本替代全文 → top1 仅 7/16 与 baseline 相同、top5 重叠 2.56/5，
        auto top5 里 user 角色 0→15（明显退化：BM25 长度归一奖励短 doc，
        三十来字的用户小问被顶到第一）；
      · guard 0.6（丢失超 40% 原文就回退）15/16、additive（全文+槽位值）14/16，
        两者都只是打平 baseline，无一 query 变好。
    机制：槽位把长 assistant 文档压成 statement + 槽位值，BM25 的长度归一化
    反过来奖励短文档 → 这层压缩对排序是净负收益。故 slots 留在库里供 P2+
    字段级匹配用，不进 BM25 文档。

    回退开关（仅供日后实验，默认 content）：BOSHI_KW_DOC=content|guard|additive|slots
    """
    c = content or ""
    mode = (os.environ.get("BOSHI_KW_DOC") or "content").strip().lower()
    if mode == "content" or not slots_json:
        return c
    try:
        txt = _slots_mod().doc_text(c, slots_json)
    except Exception:
        return c
    if not txt:
        return c
    if mode == "additive":
        return c if txt in c else (c + "\n" + txt)
    if mode == "guard":
        try:
            g = float(os.environ.get("BOSHI_KW_GUARD") or 0.6)
        except Exception:
            g = 0.6
        return c if len(txt) < g * len(c) else txt
    return txt


def _kw_index(c, cond: str, params: list) -> dict:
    """按 cond 建进程内 BM25 索引（倒排 + df + avgdl），带签名失效。

    为什么要换掉原来的 SQL ILIKE 覆盖率（实测 2026-10-19）：
      1) 覆盖率在「所有 token 都命中」处饱和 → 多个文档并列 1.00，排序退化成
         created_at DESC，正确但较旧的那条被新文档挤掉；
      2) 没有 IDF → 「显存」这种超高频词与 `tensor-split` 同权；
      3) 没有长度归一 → 长文里零星命中一段就进候选，把短而准的文档稀释。
    BM25 一次给齐 tf 饱和 / IDF / 长度归一；6029 条 × ~320 字符全量入内存约
    几十 MB，签名查询（count + max(created_at)）每次 ~10ms，重建才付一次分词成本。
    """
    key = (cond, tuple(params))
    with _KW_LOCK:
        with c.cursor() as cur:
            cur.execute(f"SELECT count(*), max(created_at) FROM boshi.memories WHERE {cond}", params)
            n_now, mx = cur.fetchone()
        ent = _KW_CACHE.get(key)
        if ent and ent["sig"] == (n_now, mx) and time.time() - ent["t"] < _KW_TTL:
            return ent

        with c.cursor() as cur:
            cur.execute(f"SELECT id, content, slots, {_META_SEL} FROM boshi.memories WHERE {cond}", params)
            rows = cur.fetchall()

        docs: list = []        # (id, content, meta, dl, 检索用文本)
        postings: dict = {}    # token → [(doc_idx, tf)]
        total_dl = 0
        for idx, (rid, content, sl, meta) in enumerate(rows):
            # P2：有 slots 的记录，入索引的是「statement + 槽位值」而不是全文。
            # 全文只用于对外返回（content 不动），避免长原文里的半截 token 制造假匹配。
            dtxt = _doc_text(content, sl)
            tf: dict = {}
            for t in _doc_tokens(dtxt):
                tf[t] = tf.get(t, 0) + 1
            dl = sum(tf.values())
            total_dl += dl
            docs.append((rid, content, meta, dl, dtxt))
            for t, f in tf.items():
                postings.setdefault(t, []).append((idx, f))

        n = len(docs)
        ent = {
            "sig": (n_now, mx), "t": time.time(),
            "docs": docs, "postings": postings,
            "avgdl": (total_dl / n) if n else 0.0,
            "vocab": len(postings),
        }
        if len(_KW_CACHE) >= _KW_CACHE_MAX:
            _KW_CACHE.clear()
        _KW_CACHE[key] = ent
        return ent


def _kw_abs(content: str, prim: list, widf_total: float,
            dl: float = None, avgdl: float = None) -> float:
    """绝对关键词命中度（0~1）：主 token 的 idf 加权覆盖率，CJK 多字词给部分信用。

    为什么不用「精确 token 命中」：库里没有分词器，中文按「整段 + bigram」入索引。
    查 "ComfyUI 显存不足" 时，正确的那条写的是「…查显存, 无关推理服务…」——没有
    "显存不足" 这个精确串，精确匹配下它只能拿 1/2 的主词分，被「显存不足」字面
    命中的无关文档压过。所以对 len>=3 的中文主词，只要命中其中任一 bigram 就给
    _KW_PARTIAL 的信用，命中越多越接近 1.0。
    """
    low = (content or "").lower()
    # token 在索引里是归一化形式（IQ4_XS→iq4xs），所以原文也要按同样规则归一后再比，
    # 否则「库里写下划线写法」的文档在这里会被判成零命中（实测 IQ4XS 排序崩掉的原因）。
    low_n = _norm_ascii(low)
    hit = 0.0
    for tok, w, idf in prim:
        if tok in low or tok in low_n:
            hit += w * idf
            continue
        if len(tok) >= 3 and _CJK_RE.fullmatch(tok):
            grams = [tok[i:i + 2] for i in range(len(tok) - 1)]
            m = sum(1 for g in grams if g in low)
            if m:
                hit += w * idf * (_KW_PARTIAL + (1.0 - _KW_PARTIAL) * m / len(grams))
    cov = min(1.0, hit / widf_total) if widf_total else 0.0
    # 长度阻尼：substring 命中不看文档长度，长文里「顺带提到两个词一次」就能拿满
    # 覆盖率，把真正讲这件事的短记录压下去（实测「如实回答，分三层说…」这种长文
    # 在 ComfyUI 显存不足 上霸榜）。用与 BM25 同形的饱和因子折回，b 可调。
    if cov and dl and avgdl and _KW_LENB > 0:
        cov /= 1.0 + _KW_LENB * max(0.0, dl / avgdl - 1.0)
    return max(0.0, min(1.0, cov))


def _kw_channel(c, toks: list, k: int, cond: str, params: list) -> list:
    """关键词通道：进程内 BM25 打分，返回与向量通道同构的候选（按分降序）。

    `kw` 字段是**相对本轮 top1 的归一化分**（0~1），便于观测；排序只依赖原始分。
    """
    if not toks:
        return []
    ent = _kw_index(c, cond, params)
    docs, postings = ent["docs"], ent["postings"]
    if not docs:
        return []
    n = len(docs)
    avgdl = ent["avgdl"] or 1.0
    k1, b = _KW_BM25_K1, _KW_BM25_B

    scores: dict = {}
    widf_total = 0.0
    hits: dict = {}
    prim: list = []
    main_hit_n = 0                 # 主 token 里「库内真有」的个数（coord 的可行性前提）
    for tok, w in toks:
        pl = postings.get(tok)
        df = len(pl) if pl else 0
        idf = math.log(1.0 + (n - df + 0.5) / (df + 0.5))
        if w >= 2.5:
            widf_total += w * idf
            prim.append((tok, w, idf))
            if df:
                main_hit_n += 1
        if not pl:
            continue
        for idx, tf in pl:
            dl = docs[idx][3]
            denom = tf + k1 * (1.0 - b + b * dl / avgdl)
            scores[idx] = scores.get(idx, 0.0) + w * idf * (tf * (k1 + 1.0)) / denom
            if w >= 2.5:
                d = hits.setdefault(idx, {"hit": 0.0, "cov": 0.0})
                d["hit"] += w * idf
                d["cov"] += w
    if not scores:
        return []
    # 协调因子（coord）：只有主 token（整数段/型号/数字，w>=2.5）参与。
    # 纯 BM25 里「命中一个极稀有词」就能压过「命中两个常见词」——实测
    # "佛教微调 数据集" 会被只含"数据集"的文档霸榜。乘上主词覆盖率把
    # 「两三个词都对上」的文档提上来，bigram 不参与分母（否则短查询被稀释）。
    #
    # 退化保护（2026-10-20）：纯中文整句提问时，整段汉字被当成一个 3.0 权重的
    # 主 token，它几乎不可能作为 token 存在于 postings（df=0），于是 pr 全为 0
    # → 所有候选分被乘成 0 → `_KW_MIN` 把候选全砍掉 → 关键词通道静默失效
    # （实测 A9：「群晖硬盘显示已损毁但检测是好的怎么办」raw 295 候选，coord 后
    # 非零 0/295）。此时 coord 的信息量恒为 0（大家覆盖率都是 0），它只毁掉
    # bigram 信号，所以只在「至少一个主 token 命中」时施加。
    weak = main_hit_n == 0         # 关键词证据只剩 bigram（见 _KW_WEAK_WV/WK）
    if main_hit_n:
        pr = {i: (hits.get(i) or {}).get("cov", 0.0) for i in scores}
        scores = {i: s * (pr[i] / (sum(w for _, w in toks if w >= 2.5) or 1.0))
                  for i, s in scores.items()}

    ranked = sorted(scores.items(), key=lambda x: -x[1])[:int(k)]
    if not ranked:
        return []
    top = ranked[0][1] or 1.0
    out = []
    for idx, s in ranked:
        rel = s / top
        if rel < _KW_MIN:
            continue
        rid, content, meta, _dl, dtxt = docs[idx]
        it = _row_to_item((rid, content, meta, None))
        it["kw"] = round(rel, 3)
        # kw_abs：**绝对** idf 加权覆盖率（0~1，分母=全部主 token 的 w*idf）。
        # 与 kw（相对 top1 归一）不同，它表达「这条文档到底对上了多少查询要义」，
        # 可跨文档比较、可与向量 sim 直接加权求和。top1 归一化会把「弱命中里的
        # 第一名」抬成 1.0，做加权融合时会把噪声顶上去。
        it["kw_abs"] = round(_kw_abs(dtxt, prim, widf_total, _dl, avgdl), 3)
        it["_weak"] = weak          # 供 _score_merge 判权（融合后即被 pop）
        out.append(it)
    return out


def _rrf_merge(vec_items: list, kw_items: list, top_k: int, k: int = None) -> list:
    """RRF（Reciprocal Rank Fusion）融合两路候选。

    score = Σ 1/(k + rank)。契约要求返回**距离**（越小越好，见模块文档），
    故把融合分归一化成 pseudo-sim 后再取 1-sim：
        sim = 0.45 + 0.55 * rrf/max_rrf      （top1 双路命中 → 1.0；单路 top1 → 0.725）
    该变换单调，不改变排序，只是让对外展示的"相关度"落在旧的 0.7~1.0 习惯区间。
    """
    k = k or _RRF_K
    pool: dict = {}
    for rank, it in enumerate(vec_items, 1):
        d = pool.setdefault(it["id"], {"item": it, "rrf": 0.0, "ch": []})
        d["rrf"] += 1.0 / (k + rank)
        d["ch"].append("vec")
    for rank, it in enumerate(kw_items, 1):
        d = pool.setdefault(it["id"], {"item": it, "rrf": 0.0, "ch": []})
        d["rrf"] += 1.0 / (k + rank)
        d["ch"].append("kw")
        if "kw" in it:
            d["item"].setdefault("metadata", {})
    max_rrf = 2.0 / (k + 1)
    out = []
    for d in pool.values():
        sim = 0.45 + 0.55 * min(d["rrf"] / max_rrf, 1.0)
        it = d["item"]
        it["score"] = round(1.0 - sim, 6)
        it["channels"] = d["ch"]
        it.pop("_weak", None)                    # 内部标记不外泄
        out.append(it)
    out.sort(key=lambda x: x["score"])
    return out[:int(top_k)]


def _score_merge(vec_items: list, kw_items: list, top_k: int,
                 wv: float = 0.45, wk: float = 0.55) -> list:
    """加权分融合：sim = wv*(1-dist) + wk*kw_abs，再映射到 0.6~1.0 展示区间。

    与 RRF 的分工：RRF 只看名次，两个通道各排第 30 的文档能压过单通道第 1
    （RRF 数学上如此，实测「退休 养老金」的金标被压出 top5）；加权分看的是
    「离得多近 + 字面对上多少」，单通道第 1 只要绝对分高就守得住。

    弱证据降权（2026-10-20）：纯中文 query 的关键词通道只剩 bigram 证据
    （候选带 `_weak=True`），此时 kw_abs 表达的是「零星 2 字切片对上」，与
    整词命中不可同日而语。等权融合实测把 gold 挤出 top5（A9 paraphrase
    56d59b74），故按 _KW_WEAK_WV/_KW_WEAK_WK 降权——不是关掉通道。
    """
    if kw_items and any(it.get("_weak") for it in kw_items) and (wv, wk) == (0.45, 0.55):
        wv, wk = _KW_WEAK_WV, _KW_WEAK_WK
    wsum = (wv + wk) or 1.0
    pool: dict = {}
    for it in vec_items:
        pool.setdefault(it["id"], {"item": it, "v": max(0.0, 1.0 - it["score"]),
                                   "k": 0.0, "ch": []})["ch"].append("vec")
    for it in kw_items:
        d = pool.setdefault(it["id"], {"item": it, "v": 0.0, "k": 0.0, "ch": []})
        if d["v"] == 0.0 and it.get("_cos"):
            d["v"] = max(0.0, float(it["_cos"]))       # 关键词独有文档补的真实余弦
            d["item"] = it
        d["k"] = float(it.get("kw_abs") or 0.0)
        d["ch"].append("kw")
    out = []
    for d in pool.values():
        f = (wv * d["v"] + wk * d["k"]) / wsum          # 0~1
        it = d["item"]
        it["score"] = round(1.0 - (0.60 + 0.40 * min(f, 1.0)), 6)   # 单调，展示仍落 0.6~1.0
        it["channels"] = d["ch"]
        it["fused"] = round(f, 4)
        it.pop("_weak", None)                    # 内部标记不外泄
        out.append(it)
    out.sort(key=lambda x: x["score"])
    return out[:int(top_k)]


def _session_expand(c, qv: str, items: list, cond: str, params: list, top_k: int,
                    penalty: float = None, min_cos: float = None,
                    max_cand: int = None, topn: int = None,
                    only_concl: bool = None) -> list:
    """同会话扩展（2026-10-19 新增，默认关闭，`BOSHI_SESSION_EXPAND=1` 开启）。

    动机（A5 实测）：auto 未中 top1 的 14 条里，有 10 条的 gold 与某个 top5
    命中**同属一次会话**——记忆库里"用户怎么问"和"当时的结论"通常是两条，
    问句措辞与 query 更像，结论被顶到后面。做法：取 top-3 命中的 session_id，
    把同会话内与 query 余弦 ≥ min_cos 的记忆拉进来参与排序，分数乘 penalty
    （轻微惩罚，保证原有命中不会被无成本顶掉）。

    代价：多 1~2 次 SQL（6k 行的表 + session_id 索引，实测 <10ms）。
    """
    if not items:
        return items
    # 参数可环境覆盖，便于不重启进程做参数扫描（全默认 = 当前生效值）
    penalty = 0.95 if penalty is None else penalty
    min_cos = 0.62 if min_cos is None else min_cos
    max_cand = 3 if max_cand is None else max_cand
    topn = 1 if topn is None else topn
    if only_concl is None:
        only_concl = os.environ.get("BOSHI_SESSION_EXPAND_CONCL", "1") == "1"
    ids = [str(x["id"]) for x in items[:int(topn)]]
    with c.cursor() as cur:
        cur.execute(
            "SELECT DISTINCT session_id::text FROM boshi.memories "
            "WHERE id::text = ANY(%s) AND session_id IS NOT NULL", (ids,))
        sids = [r[0] for r in cur.fetchall()]
    if not sids:
        return items
    have = {str(x["id"]) for x in items}
    extra = " AND role = 'assistant' AND topic = 'assistant_conclusion'" if only_concl else ""
    sql = (f"SELECT id, content, {_META_SEL}, embedding <=> %s::vector AS dist "
           f"FROM boshi.memories WHERE {cond} AND session_id::text = ANY(%s){extra} "
           f"AND embedding <=> %s::vector < %s ORDER BY dist LIMIT %s")
    with c.cursor() as cur:
        cur.execute(sql, [qv] + list(params) + [sids, qv, 1.0 - min_cos, int(max_cand)])
        rows = cur.fetchall()
    added = 0
    for r in rows:
        it = _row_to_item(r)
        if str(it["id"]) in have:
            continue
        cos = max(0.0, 1.0 - float(r[3] or 0.0))
        f = penalty * cos
        it["score"] = round(1.0 - (0.60 + 0.40 * min(f, 1.0)), 6)
        it["fused"] = round(f, 4)
        it["channels"] = ["session"]
        it["expanded"] = True
        it["metadata"] = dict(it.get("metadata") or {}, expanded_from="session")
        items.append(it)
        added += 1
    if added:
        items.sort(key=lambda x: x["score"])
    return items[:int(top_k)]


# ── P2+ 字段级匹配（默认开，`BOSHI_FIELD_MATCH=0` 关闭）─────────────────
# 动机：v7.1 §8.4 / A3 证明「槽位文本替代全文」进 BM25 是净负收益（长度归一化
# 奖励短文档）。但槽位本身仍是对齐度更强的信号——query 里有端口/版本/开关/
# 路径这类**字面字段**时，只看「候选的同一字段是否也含这个值」，不看全文词袋，
# 因此不会被文档长度或半截 token 干扰。故改为**排序抬升**，不动 BM25 文档。
# `numbers` 不进默认集合：A5 参数扫描（8 组）显示它只在 numbers 一类上触发，
# 且净收益 0（1 条 1→2、1 条 3→1），属歧义字面量（`218`/`24`/`5` 跨语境复用）。
# 需要时可 `BOSHI_FM_KEYS=ports,versions,flags,files,numbers` 打开。
_FM_KEYS = tuple(k.strip() for k in os.environ.get(
    "BOSHI_FM_KEYS", "ports,versions,flags,files").split(",") if k.strip())
_FM_W = {"ports": 1.0, "versions": 1.0, "flags": 1.0, "files": 0.8, "numbers": 0.4}
_FM_STEP = float(os.environ.get("BOSHI_FM_STEP", "0.10"))    # 每命中一个字段抬多少 sim
_FM_MAX = float(os.environ.get("BOSHI_FM_MAX", "0.30"))       # 抬升上限
_FM_POOL = int(os.environ.get("BOSHI_FM_POOL", "3"))          # 候选池 = top_k × N


def _fm_norm(v: str) -> str:
    """字段值归一：小写、去空白、去首尾标点。只做形态归一，不改字。"""
    s = (v or "").strip().lower()
    s = re.sub(r"\s+", "", s)
    return s.strip("，。、；;:：,.'\"()[]")


def _fm_hits(qfields: dict, cand: dict) -> list:
    """返回命中的字段名列表。同字段双向包含即算命中（`0.1.44.1` ⊂ `0.1.44.1 `）。"""
    hits = []
    for key, qvals in qfields.items():
        cvals = [_fm_norm(x) if isinstance(x, str) else _fm_norm(str(x))
                 for x in (cand.get(key) or [])]
        for qv in qvals:
            if any(qv and cv and (qv in cv or cv in qv) for cv in cvals):
                hits.append(key)
                break
    return hits


# 端口关键词两侧：文档侧常见写法是「端口 8199」（关键词在前，slots.py 已覆盖），
# 而人提问常说「8199 端口怎么配」（数字在前）。对称补一条前置扫描，
# 只用于 query 侧扩展，不改文档抽提语义（改抽提器要重跑全量回填，blast radius 更大）。
_RE_PORT_LOOKAHEAD = re.compile(
    r"(?<![\d.])(\d{2,5})(?=[^\d。；;，,\n]{0,6}(?:端口|端口号|监听|port|PORT))")


def _fm_query_extra(query: str) -> dict:
    """query 侧字面量补充扫描（只补「端口数字在前」这一种真实口语写法）。"""
    out: dict = {}
    try:
        ports = [p for p in _RE_PORT_LOOKAHEAD.findall(query or "")
                 if 1 <= int(p) <= 65535]
    except Exception:
        ports = []
    if ports:
        out["ports"] = ports
    return out


def _field_match(c, query: str, items: list, top_k: int) -> list:
    """按「query 字段 ∩ 候选同名字段」抬升排序（P2+）。

    打分：sim' = sim + min(_FM_MAX, _FM_STEP × Σ权重)，再落回距离（1 - sim'）。
    候选没槽位（历史行）→ 现场用同一套零模型规则从原文抽，行为与「有槽位」一致；
    抽不到就是空，绝不猜。任何异常都退回原排序（增强项不能影响主检索）。
    """
    if not items:
        return items[:int(top_k)]
    try:
        sm = _slots_mod()
        q = sm.extract(query)
    except Exception:
        return items[:int(top_k)]
    qfields = {k: [] for k in _FM_KEYS}
    for k in _FM_KEYS:
        for v in (q.get(k) or []):
            nv = _fm_norm(v)
            if nv:
                qfields[k].append(nv)
    # 抽提器是为长文档写的（「端口 8199」），人提问常写成「8199 端口」→ 补 query 侧
    for k, vs in _fm_query_extra(query).items():
        if k not in qfields:
            continue
        for v in vs:
            nv = _fm_norm(v)
            if nv and nv not in qfields[k]:
                qfields[k].append(nv)
    qfields = {k: v for k, v in qfields.items() if v}
    if not qfields:
        return items[:int(top_k)]

    ids = [str(it["id"]) for it in items]
    db_slots: dict = {}
    try:
        with c.cursor() as cur:
            cur.execute("SELECT id::text, slots FROM boshi.memories "
                        "WHERE id::text = ANY(%s)", (ids,))
            for rid, sl in cur.fetchall():
                if isinstance(sl, str):
                    try:
                        sl = json.loads(sl)
                    except Exception:
                        sl = None
                db_slots[rid] = sl if isinstance(sl, dict) else None
    except Exception:
        db_slots = {}

    for it in items:
        sl = db_slots.get(str(it["id"]))
        if sl is None:
            try:
                sl = sm.extract(it.get("content") or "")
            except Exception:
                sl = {}
        hits = _fm_hits(qfields, sl)
        if not hits:
            continue
        gain = min(_FM_MAX, _FM_STEP * sum(_FM_W.get(h, 0.5) for h in hits))
        sim = max(0.0, 1.0 - float(it.get("score") or 0.0))
        it["score"] = round(1.0 - min(1.0, sim + gain), 6)
        it["field_hits"] = hits
        it["field_gain"] = round(gain, 4)
    _fm_trace(query, qfields, items, top_k)
    items.sort(key=lambda x: x["score"])
    return items[:int(top_k)]


def _fm_trace(query: str, qfields: dict, items: list, top_k: int) -> None:
    """P2+ 打点：记录「抬升有没有改变名次」，用于事后审计线上误抬（默认关）。

    `BOSHI_FM_TRACE=1` 才写；只在 query 带字面量（qfields 非空）时被调用，
    频率低（每千条 query 个位数），一次 append 开销可忽略。
    记录的是**排序前**的顺序（items 尚未 sort），所以能算出 pre/post top1 差异。
    """
    if os.environ.get("BOSHI_FM_TRACE", "0") != "1":
        return
    try:
        pre = [(str(it["id"])[:8], round(float(it.get("score") or 0), 6)) for it in items]
        order = sorted(range(len(items)), key=lambda i: float(items[i].get("score") or 0))
        post = [pre[i][0] for i in order[:5]]
        rec = {
            "ts": _dt.now(_timezone.utc).astimezone().isoformat(timespec="seconds"),
            "query": (query or "")[:200],
            "fields": qfields,
            "top_k": int(top_k),
            "cand": len(items),
            "hit_n": sum(1 for it in items if it.get("field_hits")),
            "pre_top": [p[0] for p in pre[:5]],
            "post_top": post,
            "top1_changed": bool(post and pre and post[0] != pre[0][0]),
            "top1_hit": bool(items[order[0]].get("field_hits")) if order else False,
        }
        with open(os.path.join(os.path.dirname(_LOG), "fm_trace.jsonl"),
                  "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except Exception:
        pass


def search_memory(query: str, top_k: int = 5, where: dict = None,
                  all_versions: bool = False, include_graph: bool = False,
                  scope: str = None, me: str = None, fusion: str = None):
    c = _conn()
    w = dict(where or {})
    if not all_versions:
        w.setdefault("isLatest", True)

    # 图谱边检索请求 → 走 graph_edges 表（与 Chroma 后端行为对齐）
    want_rel, rel_type = _scan_type(w)
    if want_rel:
        return _search_edges(query, top_k, rel_type)

    cond, params = _where_sql(w, scope, me or _resolve_profile(), include_graph)
    mode = (fusion or os.environ.get("BOSHI_SEARCH_FUSION", "auto")).lower()
    # P2+：字段级匹配要在 top_k 之外还能看见候选（否则只能重排已进榜的几条）
    fm_on = os.environ.get("BOSHI_FIELD_MATCH", "1") == "1"
    pool_k = int(top_k) * max(1, _FM_POOL) if fm_on else int(top_k)
    k_each = int(min(max(int(top_k) * 4, 20), 100))
    # kw 通道候选数与 top_k 解耦（见 _KW_POOL_CAP 注释：池污染导致「要得多反而更差」）
    k_kw = min(k_each, _KW_POOL_CAP) if _KW_POOL_CAP > 0 else k_each

    qv = _vec_literal(_ef()([query or " "])[0])
    vec_items = _vec_channel(c, query, max(k_each, pool_k), cond, params, v=qv)
    if mode == "vector":
        items = vec_items[:int(pool_k)]
    else:
        toks = _kw_tokens(query)
        # auto：默认纯中文自然语言查询也走关键词通道（2026-10-20 乐之拍板放开）。
        # 早先关门的原因是 BM25 不辨否定/语义（「不是佛教微调，是通用的」会把佛教
        # 微调顶到第一），但 coord 退化保护 + 弱证据降权之后，纯中文 query 的 kw
        # 证据一定是 bigram 级弱证据（降权 0.70/0.30），A9 实测 overlap top5
        # 6→12、top1 0→4，而 A5 带锚点场景逐条不变。
        # 需要回退旧行为：BOSHI_AUTO_KW_NOANCHOR=0（纯中文只走向量）。
        _auto_kw_noanchor = os.environ.get("BOSHI_AUTO_KW_NOANCHOR", "1") == "1"
        if not toks or (mode == "auto" and not _auto_kw_noanchor
                        and not _has_anchor(query)):
            items = vec_items[:int(pool_k)]
        else:
            kw_items = _kw_channel(c, toks, k_kw, cond, params)
            if not kw_items:                       # 关键词零命中 → 退回纯向量（旧行为）
                items = vec_items[:int(pool_k)]
            elif mode == "rrf":
                items = _rrf_merge(vec_items, kw_items, pool_k)
            else:
                seen = {x["id"] for x in vec_items}
                miss = [str(x["id"]) for x in kw_items if x["id"] not in seen]
                cmap = _cosine_for_ids(c, qv, miss)
                for it in kw_items:
                    if it["id"] not in seen:
                        it["_cos"] = cmap.get(str(it["id"]), 0.0)
                items = _score_merge(vec_items, kw_items, pool_k)

    # P2+ 字段级匹配（默认关；命中查询字面字段时抬升同字段候选）
    if fm_on:
        try:
            items = _field_match(c, query, list(items), top_k)
        except Exception:            # 增强项，坏了退回融合排序
            items = items[:int(top_k)]
    else:
        items = items[:int(top_k)]

    # 同会话扩展（2026-10-19 新增，默认关；A5 实测后再决定是否默认开）
    if os.environ.get("BOSHI_SESSION_EXPAND", "0") == "1":
        try:
            items = _session_expand(c, qv, list(items), cond, params, top_k)
        except Exception:            # 扩展是增强项，坏了不能影响主检索
            pass

    if include_graph:      # 需要图谱联想时，并入边表结果（与 Chroma 后端行为对齐）
        v = _vec_literal(_ef()([query or " "])[0])
        with c.cursor() as cur:
            cur.execute("""
                SELECT id, content, jsonb_build_object('type','relation',
                       'entity_a',entity_a,'entity_b',entity_b,'relation',relation) AS meta,
                       embedding <=> %s::vector AS dist
                FROM boshi.graph_edges ORDER BY embedding <=> %s::vector LIMIT %s
            """, (v, v, int(min(top_k, 100))))
            items = items + [_row_to_item(r) for r in cur.fetchall()]
        items.sort(key=lambda x: x["score"])
        items = items[:top_k]
    return items


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
    """记忆条数（只数 memories）。

    历史 bug：这里曾返回 memories + graph_edges，`boshi_cli.py status` 于是把 6 千条
    记忆 + 6.5 万条关系边报成「记忆总数 71241」，与 chroma 时代同一个错（collection
    里混进了 relation 文档）。关系边另有 get_graph_count() 单独报。
    """
    c = _conn()
    with c.cursor() as cur:
        cur.execute("SELECT count(*) FROM boshi.memories")
        m = cur.fetchone()[0]
    return int(m)


def get_graph_count() -> dict:
    """知识图谱规模：边数 = graph_edges 行数，节点数 = 边两端实体去重后的个数。"""
    c = _conn()
    with c.cursor() as cur:
        cur.execute("SELECT count(*) FROM boshi.graph_edges")
        e = cur.fetchone()[0]
        cur.execute(
            "SELECT count(*) FROM ("
            "  SELECT entity_a AS ent FROM boshi.graph_edges"
            "  UNION SELECT entity_b FROM boshi.graph_edges) t"
        )
        n = cur.fetchone()[0]
    return {"nodes": int(n), "edges": int(e)}


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
