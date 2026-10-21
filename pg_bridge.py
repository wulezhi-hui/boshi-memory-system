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
# HNSW 检索宽度：pgvector 默认 hnsw.ef_search=40，而 HNSW 只会返回
# min(LIMIT, ef_search) 行 —— 深池（k>40）会静默退化成浅池。实测 k=150 只回 40 行。
_HNSW_EF_FLOOR = int(os.environ.get("BOSHI_HNSW_EF_FLOOR", "40"))
_HNSW_EF_NOW = [0]          # 当前会话已生效的 ef_search（只升不降，换连接时归零）
_HNSW_ITER = [False]        # 当前会话是否已开 iterative_scan


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
    _HNSW_EF_NOW[0] = 0        # 新会话：ef_search 回到 pgvector 默认，别以为还是旧的
    _HNSW_ITER[0] = False
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
    if key in ("session_id", "topic", "role", "source"):
        # 真列（写入时 add_memory 把这几项 pop 出 meta 落成真列），**不在 meta jsonb 里**。
        # 早期误按 `meta->>'session_id'` 查 → 永远 NULL → 过滤恒空（2026-10-19 β3 验收实测）。
        col = key
        if isinstance(val, dict):
            for op, sqlop in (("$eq", "="), ("$ne", "IS DISTINCT FROM"),
                              ("$gte", ">="), ("$lte", "<="), ("$gt", ">"), ("$lt", "<")):
                if op in val:
                    return ("%s %s %%s" % (col, sqlop), [val[op]])
            return ("TRUE", [])
        return ("%s = %%s" % col, [val])
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

    # 心智模型预计算页是派生数据（正文来自别的记忆），绝不能反向污染召回/最近列表
    parts.append("COALESCE(topic, '') <> 'mental_model'")

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
             "NULLIF(meta->>'timestamp', '')::float8), "
             # topic/session_id/role/source 是**真列**（add_memory 把它们 pop 出 meta），
             # 早期不在这里回填 → 检索结果 metadata 里根本没有 topic，于是所有
             # 「按 topic 分组」的读侧判据（_fold_value_changes 换值折叠、
             # _mark_value_conflicts 冲突暴露）全都分组为空、静默失效
             # （2026-10-19 实测：云闸 40层/100层 两条都不带 topic，新旧并列）。
             "'topic', COALESCE(topic, ''), "
             "'session_id', COALESCE(session_id::text, ''), "
             "'role', COALESCE(role, ''), "
             "'source', COALESCE(source, '')) AS meta")

# graph_edges 的 metadata 视图：表里只有列（id/entity_a/entity_b/relation/content），
# 没有 meta jsonb，读侧 metadata 全靠这里重建。约定：**relation 列存原串**
# （自定义关系不再被折叠成 related 丢掉原串），relation_norm 给归一后的 4 类语义。
# 2026-10-19 β5：此前列里写的是归一值，读侧又连 rel_type 都不给 → 读回永远 related。
_EDGE_META_SEL = ("jsonb_build_object('type','relation','entity_a',entity_a,"
                  "'entity_b',entity_b,'relation',relation,'relation_raw',relation,"
                  "'relation_norm', CASE WHEN relation IN "
                  "('updates','extends','derives','related') THEN relation ELSE 'related' END,"
                  "'rel_type', CASE WHEN relation IN "
                  "('updates','extends','derives','related') THEN relation ELSE 'related' END)")


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
        # relation 列取原串：写入方（knowledge_graph.add_relation）把原串放 relation_raw、
        # 归一值放 rel_type/relation。历史故障：该列自 PG 化以来一直写入空串/None（8 万条真值全丢），
        # 且早期只写归一值 → 自定义关系读回全变 related（2026-10-19 β5 修复）。
        rel = (meta.get("relation_raw") or meta.get("relation")
               or meta.get("rel_type") or "")
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
    sql = ("""SELECT id, content,""" + _EDGE_META_SEL + """ AS meta,
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
_KW_MAX_TOKENS = int(os.environ.get("BOSHI_KW_MAX_TOKENS", "64") or 0) or 64
# query 侧 CJK bigram 上限（2026-10-21）：0/未设 = 全部生成（与文档侧对齐）。
# 旧行为是 6（`range(min(n-1, 6))`），对中文长句等于只让**前半句**参与匹配：
# A9 40 条纯中文 query 实测 588 个 bigram 需要 588，只用上 240（41%），
# 被截断的 348 个里 295 个（85%）在 postings 里真实存在——证据本来能命中却被丢掉；
# 40/40 条都含「截断也能命中」的 bigram，4 条 gold 只靠截断 bigram 命中（纯损失）。
# 回退：BOSHI_KW_QBIG_LIMIT=6 复原旧行为。
_KW_QBIG_LIMIT = int(os.environ.get("BOSHI_KW_QBIG_LIMIT", "0") or 0)
# 后半段 bigram 的权重衰减（0 = 不衰减，全部 1.0）。用于抑制长句尾部噪声。
_KW_QBIG_DECAY = float(os.environ.get("BOSHI_KW_QBIG_DECAY", "0") or 0)
# query 侧「功能字 bigram」过滤（2026-10-21）。放开全部 bigram 后实测变差，
# 原因是「怎么 / 什么 / 还要 / 能不 / 是什」这类纯功能字组合贡献了大量低信息
# 命中：它们 idf 低但数量多，把长尾文档的分数叠起来，挤掉金标（overlap
# top1 4→2）。规则：两字都是功能字 → 直接丢弃；一字功能一字实义 → 权重 ×0.5。
_KW_QBIG_FUNC = (os.environ.get("BOSHI_KW_QBIG_FUNC", "1") != "0")
# 功能字过滤是否也作用于前 6 个 bigram（旧窗口）。默认只过滤尾部，保持前窗口
# 与旧行为逐字一致——overlap 型 query 的金标主要靠前窗口命中，动它就有掉分风险。
_KW_QBIG_FUNC_HEAD = (os.environ.get("BOSHI_KW_QBIG_FUNC_HEAD", "0") == "1")
_FUNC_CHARS = set("的了着过吗呢吧啊呀哦嘛么怎什怎样如何能不能可会要就都也还又很太更最这那哪"
                  "个只些是把被在对我你他她它们和与跟同及或并且但是因为所以如果不然没不是有无"
                  "上下里中前后时当一二三四五六七八九十多少几")
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
# 弱证据下 kw_abs 改用「库内真实存在的 token」做分母（1=开，默认开）。
_KW_ABS_ALL = (os.environ.get("BOSHI_KW_ABS_ALL", "1") != "0")
# 相对分掺入（2026-10-21）：`kw_abs` 是绝对 idf 覆盖率，改写式 query 的 gold
# 只对上几个 bigram、覆盖率低，但**在关键词通道里名次靠前**（A9 实测
# 8b027120 名次 1、eec4f527 5、f6c71ce8 10），却被只读 kw_abs 的融合顶出去。
# 故弱证据 query 的 k 值 = kw_abs + b*kw（kw 是通道内 top1 归一的相对分）。
# 判定（scratch/a9_b_stability.py，4 个 ASOF 快照 × b∈{0,0.4}）：
#   paraphrase top5 每个快照都 +2（6→8 / 4→6 / 1→3 / 5→7），无一退化；
#   top1 四个快照 +1/+1/+1/+1（live 为 overlap −1、para +1，净 0）；
#   overlap 只在 live 语料掉 1 个 top3（仍在 top5 内）。
# 结论：不是噪声，是把「通道头名」这个确定性信号接回来 → **默认开到 0.4**。
_KW_REL_BLEND = float(os.environ.get("BOSHI_KW_REL_BLEND", "0.4") or 0)
# 锚点奖励（2026-10-21，池结构改造 A 的落地）：query 带型号/端口/版本/两位数字时，
# 「字面通道内名次」比 kw_abs 的绝对量级更可靠——那种 query 里通道 top1 几乎必是
# 同一主题。给向量通道前 5、关键词通道前 3 各加一点分（等价于把 RRF 的名次信号
# 作为奖励项叠加在绝对分融合上，不动 kw_abs 的量纲）。
# 离线实测（eval/sweep_fuse.py，深度 60 的回放）：
#   A5（35 条全带锚点）top1/top3/top5 20/26/29 → 26/33/33
#   A9（40 条纯中文，无锚点 → 不加分）逐条不变 10/18 · 4/7
# 之所以按锚点分叉而不是全局加：全局加会把 A9 overlap top5 18→14（名次奖励在
# 纯中文里放大 bigram 噪声）。默认关，BOSHI_ANCHOR_BONUS=1 才生效。
_ANCHOR_BONUS_ON = os.environ.get("BOSHI_ANCHOR_BONUS", "0") == "1"
_AB_V = float(os.environ.get("BOSHI_ANCHOR_BONUS_V", "0.10"))   # 向量通道 rank<=5
_AB_K = float(os.environ.get("BOSHI_ANCHOR_BONUS_K", "0.15"))   # 关键词通道 rank<=3


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
                grams = [seg[i:i + 2] for i in range(n - 1)]
                lim = _KW_QBIG_LIMIT if _KW_QBIG_LIMIT > 0 else len(grams)
                for i, g in enumerate(grams[:lim]):
                    # 衰减只作用于「超出旧 6 个窗口」的尾部，前 6 个恒 1.0，
                    # 保证 BOSHI_KW_QBIG_DECAY 调到 >0 时旧行为仍是特例。
                    w = 1.0
                    if _KW_QBIG_DECAY > 0 and i >= 6 and n > 2:
                        w = max(0.35, 1.0 - _KW_QBIG_DECAY * (i - 5) / max(1, n - 2))
                    if _KW_QBIG_FUNC and (i >= 6 or _KW_QBIG_FUNC_HEAD):
                        nf = sum(1 for ch in g if ch in _FUNC_CHARS)
                        if nf == 2:
                            continue          # 纯功能字组合：无信息量，纯噪声
                        if nf == 1:
                            w *= 0.5
                    add(g, w)
    return toks[:_KW_MAX_TOKENS] if _KW_MAX_TOKENS > 0 else toks


def _norm_ascii(tok: str) -> str:
    """ASCII token 归一：去掉 `_ - . / ,`，让 IQ4_XS / iq4xs / IQ4-XS 互相命中。

    实测 "IQ4XS 解码速度" 关键词通道零命中，因为库里写的是 `IQ4_XS`、`UD-IQ4_XS`。
    型号写法在语料里不统一（下划线/连字符/粘连），归一后才能对齐。
    """
    return tok.replace("_", "").replace("-", "").replace(".", "").replace("/", "").replace(",", "")


def _ensure_ef(c, k: int) -> None:
    """把 hnsw.ef_search 抬到 ≥ k（只升不降，会话级）。

    pgvector 的 HNSW 只返回 min(LIMIT, ef_search) 行，默认 ef_search=40 ——
    传 LIMIT 150 只会拿回 40 条，深池检索静默退化成浅池。_conn 是 autocommit
    单例，所以用会话级 SET（不是 SET LOCAL，事务外无效）。
    """
    need = max(_HNSW_EF_FLOOR, int(k))
    deep = int(k) > _HNSW_EF_FLOOR          # 只有深池才需要放宽顺序换召回
    if need <= _HNSW_EF_NOW[0] and (not deep or _HNSW_ITER[0]):
        return
    try:
        with c.cursor() as cur:
            if need > _HNSW_EF_NOW[0]:
                cur.execute("SELECT set_config('hnsw.ef_search', %s, false)", (str(need),))
                _HNSW_EF_NOW[0] = need
            if deep and not _HNSW_ITER[0]:
                # 加了 WHERE（isLatest/scope/时间窗）后 HNSW 先在 ef_search 个候选里过滤，
                # 命中不足就永远凑不满 k —— 实测 k=150 只回 103 行。pgvector 0.8 起
                # 可开 iterative_scan 继续扫，凑满 k 行（relaxed_order 放宽顺序换召回）。
                # 小查询（k<=40）不开：那是线上主链路，保持严格近邻序。
                cur.execute("SELECT set_config('hnsw.iterative_scan', 'relaxed_order', false)")
                cur.execute("SELECT set_config('hnsw.max_scan_tuples', %s, false)",
                            (str(max(40000, need * 200)),))
                _HNSW_ITER[0] = True
    except Exception:                # 索引/参数不存在（老库）→ 不影响主链路
        pass


def _vec_channel(c, q: str, k: int, cond: str, params: list, v=None) -> list:
    """向量通道（纯余弦，与旧实现完全一致）。v 可传入已算好的查询向量。"""
    if v is None:
        v = _vec_literal(_ef()([q or " "])[0])
    _ensure_ef(c, k)
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
    # 纯中文长句的 kw_abs 退化（2026-10-21）：主 token = 整段汉字，df≈0 →
    # widf_total 全是"库里不存在的词"，kw_abs 对所有人恒 0.0，而 _score_merge
    # 只看 kw_abs 不看 kw/名次 → 关键词通道在融合层等于不存在（bigram 证据全废）。
    # 开启 abs_all 后，改用「库内真实存在的 token（含 bigram）」做分母，
    # 让 bigram 覆盖率成为可与向量 sim 直接对比的绝对分。
    allp: list = []
    widf_all = 0.0
    if weak and _KW_ABS_ALL:
        for tok, w in toks:
            pl = postings.get(tok)
            df = len(pl) if pl else 0
            if not df:
                continue
            allp.append((tok, w, math.log(1.0 + (n - df + 0.5) / (df + 0.5))))
        widf_all = sum(w * i for _, w, i in allp)
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
        if allp and widf_all > 0:
            it["kw_abs_all"] = round(_kw_abs(dtxt, allp, widf_all, _dl, avgdl), 3)
            it["kw_abs"] = it["kw_abs_all"]
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
                 wv: float = 0.45, wk: float = 0.55, query: str = None) -> list:
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
    # 弱证据 query：只在通道内名次好、绝对覆盖率低的 gold 才需要相对分兜底
    qweak = _KW_REL_BLEND > 0 and any(it.get("_weak") for it in kw_items)
    wsum = (wv + wk) or 1.0
    bonus_on = _ANCHOR_BONUS_ON and _has_anchor(query or "")
    pool: dict = {}
    for rank, it in enumerate(vec_items, 1):
        pool.setdefault(it["id"], {"item": it, "v": max(0.0, 1.0 - it["score"]),
                                   "k": 0.0, "ch": [], "rv": rank, "rk": None})["ch"].append("vec")
    for rank, it in enumerate(kw_items, 1):
        d = pool.setdefault(it["id"], {"item": it, "v": 0.0, "k": 0.0, "ch": [],
                                      "rv": None, "rk": None})
        d["rk"] = rank
        if d["v"] == 0.0 and it.get("_cos"):
            d["v"] = max(0.0, float(it["_cos"]))       # 关键词独有文档补的真实余弦
            d["item"] = it
        d["k"] = float(it.get("kw_abs") or 0.0)
        if qweak:
            d["k"] += _KW_REL_BLEND * float(it.get("kw") or 0.0)
        d["ch"].append("kw")
    out = []
    for d in pool.values():
        f = (wv * d["v"] + wk * d["k"]) / wsum          # 0~1
        it = d["item"]
        it["score"] = round(1.0 - (0.60 + 0.40 * min(f, 1.0)), 6)   # 单调，展示仍落 0.6~1.0
        it["channels"] = d["ch"]
        it["fused"] = round(f, 4)
        # 锚点奖励只进排序键，不进展示分（否则 0.6~1.0 区间会被顶爆）
        it["_sortf"] = f + ((_AB_V if d["rv"] and d["rv"] <= 5 else 0.0)
                            + (_AB_K if d["rk"] and d["rk"] <= 3 else 0.0) if bonus_on else 0.0)
        it.pop("_weak", None)                    # 内部标记不外泄
        out.append(it)
    out.sort(key=lambda x: -x["_sortf"])
    for it in out:
        it.pop("_sortf", None)
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

# 线上打点：按天×字段聚合，供 eval/fm_stats_report.py 读（表在首次写入时懒建）
_FM_STATS_READY = False
_FM_STATS_DDL = """
CREATE TABLE IF NOT EXISTS boshi.fm_stats (
    day date NOT NULL,
    field text NOT NULL,
    queries bigint NOT NULL DEFAULT 0,
    hits bigint NOT NULL DEFAULT 0,
    gain_sum double precision NOT NULL DEFAULT 0,
    top1_changed bigint NOT NULL DEFAULT 0,
    PRIMARY KEY (day, field)
)
"""
_FM_STATS_UPSERT = """
INSERT INTO boshi.fm_stats AS s (day, field, queries, hits, gain_sum, top1_changed)
VALUES (CURRENT_DATE, %s, 1, %s, %s, %s)
ON CONFLICT (day, field) DO UPDATE SET
    queries = s.queries + 1,
    hits = s.hits + EXCLUDED.hits,
    gain_sum = s.gain_sum + EXCLUDED.gain_sum,
    top1_changed = s.top1_changed + EXCLUDED.top1_changed
"""


def _fm_norm(v: str) -> str:
    """字段值归一：小写、去空白、去首尾标点。只做形态归一，不改字。"""
    s = (v or "").strip().lower()
    s = re.sub(r"\s+", "", s)
    return s.strip("，。、；;:：,.'\"()[]")


_RE_VER_STRIP = re.compile(r"^[vV]\s*")


def _fm_ver(v: str) -> str:
    """版本号归一：去前导 `v`、压空白、去尾标点。只做形态归一，不改数字。"""
    return _RE_VER_STRIP.sub("", _fm_norm(v))


def _fm_hits(qfields: dict, cand: dict) -> list:
    """返回命中的字段名列表。同字段双向包含即算命中（`127.0.0.1:8199` ⊃ `8199`）。

    `versions` 例外：必须**精确相等**，不做双向包含。原因（2026-10-21 A5 实测）：
    旧抽提器把 `v0.1.44.1` 截成 `v0.1.44`，新抽提器抽全段，query 抽到 `0.1.44.1` 时
    按包含判定只剩「截断值 ⊂ 真值」单向命中——真答案因自己也含该值被抬同时，
    截断的邻居也被抬，反把 gold 挤到 top5 外；而精确比较两边都不命中，排序回到
    未启用字段匹配时的样子（净收益不丢，A8 的 4 条转好全部来自 ports/flags）。
    """
    hits = []
    for key, qvals in qfields.items():
        cvals = [_fm_norm(x) if isinstance(x, str) else _fm_norm(str(x))
                 for x in (cand.get(key) or [])]
        if key == "versions":
            cver = {_fm_ver(x) for x in cvals if x}
            if any(qv and _fm_ver(qv) in cver for qv in qvals):
                hits.append(key)
            continue
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
    pre_top1 = str(items[0]["id"]) if items else None
    items.sort(key=lambda x: x["score"])
    top = items[:int(top_k)]
    _fm_stats(c, qfields, top, pre_top1)
    return top


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


def _fm_stats(c, qfields: dict, top: list, pre_top1: str = None) -> None:
    """P2+ 线上打点（默认开，`BOSHI_FM_STATS=0` 关）：按天×字段累计命中与抬升效果。

    目的：`_FM_W` 的字段权重目前是拍的（ports/versions/flags 1.0、files 0.8），
    有了这张表就能用真实流量算「命中率 / 平均抬升 / 改变 top1 的比率」，
    以后调权看数据而不是看直觉。

    只记聚合计数（每 query 每个字段一行 upsert，最多 4 行），走的是 `_conn()` 的
    autocommit 连接，不占事务；任何异常都吞掉——打点永远不许拖累检索。
    """
    if os.environ.get("BOSHI_FM_STATS", "1") != "1" or not qfields or not top:
        return
    try:
        global _FM_STATS_READY
        if not _FM_STATS_READY:
            c.execute(_FM_STATS_DDL)
            _FM_STATS_READY = True
        t1 = top[0]
        changed = int(bool(pre_top1) and str(t1.get("id")) != str(pre_top1))
        t1_hits = t1.get("field_hits") or []
        for key in qfields:
            hit_items = [it for it in top if key in (it.get("field_hits") or [])]
            gain = sum(float(it.get("field_gain") or 0.0) for it in hit_items)
            c.execute(_FM_STATS_UPSERT,
                      (key, len(hit_items), round(gain, 4), int(changed and key in t1_hits)))
    except Exception:  # noqa: BLE001
        pass


def _time_bounds(since=None, until=None) -> tuple:
    """since/until（Unix 秒、数字串或 ISO 串）→ (SQL 片段, 参数)。

    P2 遗留项：`get_time_range()` 早就存在，但检索主路径没有时间入口，
    导致「上周/上个月的记忆」只能整库召回再人工过滤。这里把时间窗做成
    检索的一等参数，落在 created_at 真列上（可用索引，不进 meta 扫描）。
    """
    import datetime as _dt

    def _ts(x):
        if x is None or x == "":
            return None
        if isinstance(x, bool):
            return None
        if isinstance(x, (int, float)):
            return float(x)
        s = str(x).strip()
        try:
            return float(s)
        except ValueError:
            pass
        try:
            return _dt.datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()
        except ValueError:
            return None

    parts, ps = [], []
    a, b = _ts(since), _ts(until)
    if a is not None:
        parts.append("created_at >= to_timestamp(%s)")
        ps.append(a)
    if b is not None:
        parts.append("created_at <= to_timestamp(%s)")
        ps.append(b)
    return " AND ".join(parts), ps


# ── E 方案：交叉编码器重排（2026-10-21）────────────────────────────────
# 动机：A9 paraphrase 的缺口在**排序**不在召回——union 池里已经有 gold，融合分被
# bigram/余弦噪声压住。本机 BuddhistRAG 早配好了 bge-reranker-v2-m3-FP16.gguf
# （D:/llama/models/，llama.cpp --rerank 独立实例 11437），直接复用做第二段排序，
# 零下载、零训练、零标注。不引入新模型文件，只是把已有的重排器接进记忆召回。
#
# 实测（2026-10-21，A9 40 条 + A5 35 条，top1/top3/top5）：
#   A9-overlap    10/15/18 → 13/17/18
#   A9-paraphrase  4/ 5/ 8 →  6/ 9/11
#   A5            21/30/32 → 30/34/34
# 延迟（20 候选，GPU，含 HTTP 往返）p50 769ms / p90 1105ms。
#
# ── P1④ 定档（2026-10-21 19:00 定案，纯重排 + 池 100）────────────────
# 目标：把 A9 完全改写型（括弧里换个说法、字面几乎不重叠）top5 从 40% 推到 >60%。
# 池深扫描（重排存活、ef_search 已修，top_k=5）：
#   pool  15 → ov 13/17/18  para  6/10/10  A5 31/34/34
#   pool 100 → ov 13/18/20  para  7/ 9/12  A5 30/34/34   ← 定档
#   pool 150 → ov 13/18/19  para  7/ 9/10  A5 30/34/34   （更深处开始进噪声）
# 漏的 8 条 para 里 3 条（d13aed83/a457b574/89a147e0）连 800 深度的向量池都不在，
# 即失败发生在**召回层**而非重排层；池内近失的 5 条名次 8/12/19/25/27，没有一个是
# 「池够深就能救」的。所以 100 是收益拐点，再深只增加 token 成本。
# 代价（75 条真实 query，3060 12G，重排常驻）：
#   rerank OFF  p50 64ms / p95 92ms      （纯融合序基线）
#   pool 40     p50 976ms / p95 1386ms   para 6/8/11（55%）不达标
#   pool 100    p50 1701ms / p95 2527ms  ← 生产默认
# 杠杆实验（都试过，都不换）：截断 BOSHI_RERANK_CHARS 300/200/120 → p50 1456/1280/1113ms
# 但 para 掉到 11/11/9；加大 BOSHI_RERANK_CHUNK 2200→3500 反而更慢（1817ms）；
# 请求并发（2/4 线程、-np 4 新实例）只省 9%——rerank 前向在 GPU 上已 90~99% util 打满。
# 结论：池深 × 每条约 288 字正文就是固有成本；嫌慢就降池（BOSHI_RERANK_POOL=40 换回 55%）。
# 置信度门控升级（先浅池、top1 logit 低才扩池）也量过并否决：thr=1.0 时 20% 升级，
# 质量仍低于单跑深池（ov 17/19 vs 18/20），均值 1.59s、p95 3.0s 反而更差。
# 默认开；端点不可达/超时一律静默降级回融合序（增强项绝不能伤主链路）。
_RERANK_ON = os.environ.get("BOSHI_RERANK", "1") == "1"
_RERANK_URL = os.environ.get("BOSHI_RERANK_URL", "http://127.0.0.1:11437/rerank")
_RERANK_MODEL = os.environ.get("BOSHI_RERANK_MODEL", "bge-reranker-v2-m3")
_RERANK_POOL = int(os.environ.get("BOSHI_RERANK_POOL", "100"))
# 锚点查询（型号/端口/版本/路径/英文词）用小池：这类查询的答案靠字面定位，深池只会
# 把相邻的远亲文档捞进来搅局。A5(35 条带锚点) 实测 pool 40 → 31/34/34、pool 100 →
# 30/34/34（top1 反而退 1），而 p50 从 1701ms 降到 976ms。所以按查询形态分档：
# 有锚点 40、纯中文 100（A9 改写型 40→para 6/8/11、100→7/9/12，深池确实必要）。
_RERANK_POOL_ANCHOR = int(os.environ.get("BOSHI_RERANK_POOL_ANCHOR", "40"))
_RERANK_CHARS = int(os.environ.get("BOSHI_RERANK_CHARS", "700"))
# 融合序混入（RRF，默认关）：纯重排会「救一批、打掉一批」——把融合序本来排第 1 的
# 目标重排到前 5 之外（A9 实测 8b027120：融合 1 → 重排 19），而融合序又有它自己的
# 盲区（改写句向量远）。两组名次做 RRF 加权融合，两边各保留发言权：
#   key = W_RK/(K+重排名次) + W_FU/(K+融合名次)，只让融合前 HEAD 名参与投票
#   （融合尾部是噪声，不该有权重）。
# 生产口径 trace 复算（池 100，A9 40 + A5 35，名次取自线上同一条路径）：
#   纯重排    ov 13/18/20  para 7/9/12  A5 30/34/34
#   等权 blend ov 12/17/18 para 6/9/13  A5 30/34/35
# 病例分型：gold 两侧都进前 5 的 56 条、只有重排进前 5 的 10 条、只有融合序进前 5 的
# 仅 2 条（para 8b027120、A5 90161ac0）→ 重排以 5:1 压倒融合序。blend 每救回 1 条
# 就要打掉 89a147e0（融合 93 → 重排 2）、3f1be78e（融合 31 → 重排 4）这类重排独有的
# 大跨度命中；四档「给融合 top1 留固定名额」全试过，插第 1 位 A5 top1 塌到 26，
# 插第 2/3 位 para 零收益，插第 4/5 位与纯重排完全等价。故 W_FU 默认 0，代码保留
# 供环境变量开启对照。
_BLEND_W_RK = float(os.environ.get("BOSHI_BLEND_W_RK", "1.2"))
_BLEND_W_FU = float(os.environ.get("BOSHI_BLEND_W_FU", "0"))
_BLEND_K = int(os.environ.get("BOSHI_BLEND_K", "10"))
_BLEND_HEAD = int(os.environ.get("BOSHI_BLEND_HEAD", "20"))
_RERANK_CHUNK = int(os.environ.get("BOSHI_RERANK_CHUNK", "2200"))
_RERANK_TIMEOUT = float(os.environ.get("BOSHI_RERANK_TIMEOUT", "8"))
# 连接超时必须单独压到亚秒级：本机实测「连一个没人听的 127.0.0.1 端口」要 2.0s 才拒绝
# （Windows 的 SYN 重试），不设 connect 超时的话降级路径每次要多花 2 秒。
_RERANK_CONNECT_TIMEOUT = float(os.environ.get("BOSHI_RERANK_CONNECT_TIMEOUT", "0.35"))
_RERANK_COOLDOWN = float(os.environ.get("BOSHI_RERANK_COOLDOWN", "60"))
_RERANK_DOWN_UNTIL = [0.0]        # 熔断：失败后静默 N 秒，避免每次都付连接代价
# 显存预检：重排模型 FP16 常驻约 650 MiB（实测 +650）。3060 12G 是共卡，
# 别的 llama-server 一起载入时会把显存吃到只剩 4xx MiB，这时再唤醒重排
# 会让 llama-server OOM 直接**掉进程**（2026-10-21 实测：14:35 另起两个服务
# 后 11437 进程消失，此后所有查询静默退回融合序，成绩 para 4/5/8）。
# 所以开火前先看空闲显存，不够就当场降级并在状态里写明原因，不赌。
_RERANK_VRAM_MIN = int(os.environ.get("BOSHI_RERANK_VRAM_MIN", "800"))
_RERANK_VRAM_CHECK = os.environ.get("BOSHI_RERANK_VRAM", "1") == "1"
_RERANK_WARM_SEC = float(os.environ.get("BOSHI_RERANK_WARM", "300"))
_RERANK_LAST_OK = [0.0]           # 上次成功重排的时间（判定「权重是否还在卡上」）
_VRAM_CACHE = {"t": 0.0, "free": None}
# 运行状态（给 doctor/运维看，避免"到底有没有在重排"只能靠猜）
_RERANK_STATUS: Dict[str, Any] = {"ok": None, "reason": "", "ts": 0.0, "detail": ""}
_STATUS_LOG_AT = [0.0]

# ── β2 置信度分级（重排后验概率 relevance 的阈值）─────────────────────────
# 重排后验 p 是天然的相关性概率：bge-reranker 直接输出「这条跟 query 相关」的
# 0~1 概率。实测 2026-10-19（重排在线）：在域题 top1 p 全 ≥0.95，离域题最高 0.42；
# 分界干净压在 0.5。把它暴露成 relevance + confidence，agent 才知道「这条可不可信」
# ——之前离域题静默降级后拿 0.30 融合伪距离满分离谱，分数高低完全不能判可信度。
# 阈值可调：BOSHI_REL_HIGH / BOSHI_REL_MID。
_RELEVANCE_HIGH = float(os.environ.get("BOSHI_REL_HIGH", "0.5"))
_RELEVANCE_MID = float(os.environ.get("BOSHI_REL_MID", "0.3"))


def _vram_free_mib():
    """当前空闲显存（MiB）；取不到返回 None（取不到就不阻断重排）。"""
    if not _RERANK_VRAM_CHECK:
        return None
    if time.time() - _VRAM_CACHE["t"] < 20:
        return _VRAM_CACHE["free"]
    free = None
    try:
        import subprocess
        out = subprocess.run(
            ["nvidia-smi", "--query-gpu=memory.free", "--format=csv,noheader,nounits"],
            capture_output=True, timeout=4)
        txt = (out.stdout or b"").decode("ascii", "replace").strip().splitlines()
        if out.returncode == 0 and txt:
            free = int(txt[0].strip())
    except Exception:                # noqa: BLE001  没装 nvidia-smi / 超时 → 不阻断
        free = None
    _VRAM_CACHE["t"], _VRAM_CACHE["free"] = time.time(), free
    return free


# ── 重排服务按需拉起（2026-10-22）─────────────────────────────────────────
# 缘起：11436 嵌入早有「按需拉起 + 空闲卸载」（BuddhistRAG/embed_client.py 的
# ensure_service，DETACHED 隐藏窗口，用户 2026-10-10 批准并实测过），但重排 11437
# 走的是另一条路径（本模块 _rerank_items 的 HTTP 直连），从没接这个钩子 —— 只靠
# 手动跑 start_llama_rerank.bat，而那个 bat 里只有 llama.cpp 原生
# --sleep-idle-seconds 600：**睡 = 让出 ~650MiB 显存、进程与端口都留着、请求即醒；
# 它不会在服务没起时把服务拉起来**。后果：机器重启 / 进程被外部杀掉之后，检索会
# 静默降级成融合序（relevance=null），表面一切正常、分数却不可信。
# 这里把 embed_client 那套已验证的做法搬过来：
#   端口不通 → 显存预检 → DETACHED 启动（脱离调用方进程树，父进程退出不连坐）
#   → 轮询 /health 就绪 → 继续本次重排；任一步失败 → 照旧降级，绝不抛异常。
# 开关：BOSHI_RERANK_AUTOSTART=0 关闭 ｜ BOSHI_RERANK_AUTOSTART_WAIT=秒（默认 25）
#       BOSHI_RERANK_SPAWN_COOLDOWN=秒（默认 120，防拉起风暴）
_RERANK_AUTOSTART = os.environ.get("BOSHI_RERANK_AUTOSTART", "1") == "1"
_RERANK_AUTOSTART_WAIT = float(os.environ.get("BOSHI_RERANK_AUTOSTART_WAIT", "25"))
_RERANK_SPAWN_COOLDOWN = float(os.environ.get("BOSHI_RERANK_SPAWN_COOLDOWN", "120"))
_RERANK_LLAMA_DIR = os.environ.get("BOSHI_RERANK_LLAMA_DIR", r"D:\llama")
_RERANK_GGUF = os.environ.get("BOSHI_RERANK_GGUF",
                              os.path.join(_RERANK_LLAMA_DIR, "models",
                                           "bge-reranker-v2-m3-FP16.gguf"))
_RERANK_LOG = os.environ.get(
    "BOSHI_RERANK_LOG",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "logs", "rerank_server.log"))
_RERANK_LAST_SPAWN = [0.0]          # 上次「尝试」拉起（含失败）的时间 → 冷却判据
_RERANK_LAST_START = [0.0]          # 上次真正发出 Popen 的时间（观测用）
# 空闲卸载可观测（2026-10-19）：真正的 600s 计时在 llama-server 进程里
# （--sleep-idle-seconds），本进程看不到它的状态；但「最后一次成功重排」是等价锚点
# ——超过 sleep_after 没成功重排，权重就很可能已让出显存。把它暴露成
# idle_sec / asleep_likely / wakes，运维不必再靠猜（原状态里只有一个 ts，看不出空闲多久）。
_RERANK_SLEEP_AFTER = int(os.environ.get("BOSHI_RERANK_SLEEP_SEC", "600"))
_RERANK_WAKE_COUNT = [0]            # 本进程按需拉起「服务不在」的次数
_RERANK_LAST_WAKE = [0.0]


def _rerank_endpoint():
    """从 _RERANK_URL 抠出 (host, port)；默认 127.0.0.1:11437。"""
    m = re.match(r"^https?://([^/:]+)(?::(\d+))?", _RERANK_URL or "")
    if not m:
        return "127.0.0.1", 11437
    return m.group(1), int(m.group(2) or 11437)


def _rerank_port_alive(timeout: float = 0.4) -> bool:
    import socket
    host, port = _rerank_endpoint()
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def _rerank_ready(timeout: float = 1.0) -> bool:
    """就绪 = /health 返回 2xx（睡眠中的实例也返回 200，请求会把它唤醒）。"""
    import urllib.request
    host, port = _rerank_endpoint()
    try:
        with urllib.request.urlopen("http://%s:%d/health" % (host, port), timeout=timeout) as r:
            return 200 <= getattr(r, "status", 200) < 300
    except Exception:                # noqa: BLE001
        return False


def _spawn_detached(cmd: list, logpath: str, cwd: str) -> int:
    """脱离调用方进程树 + 隐藏控制台启动（父进程退出不连坐）。"""
    d = os.path.dirname(logpath)
    if d:
        os.makedirs(d, exist_ok=True)
    fh = open(logpath, "ab")
    kw = dict(stdin=subprocess.DEVNULL, stdout=fh, stderr=fh, close_fds=True, cwd=cwd)
    if os.name == "nt":
        DETACHED_PROCESS = 0x00000008
        CREATE_NEW_PROCESS_GROUP = 0x00000200
        CREATE_BREAKAWAY_FROM_JOB = 0x01000000
        try:      # 优先彻底脱离 Job：agent/终端跑在 job 里，不脱离会被一并清理
            p = subprocess.Popen(cmd, creationflags=(DETACHED_PROCESS |
                                                     CREATE_NEW_PROCESS_GROUP |
                                                     CREATE_BREAKAWAY_FROM_JOB), **kw)
        except OSError:                   # 不在 job 中时 breakaway 会失败 → 退回
            p = subprocess.Popen(cmd, creationflags=(DETACHED_PROCESS |
                                                     CREATE_NEW_PROCESS_GROUP), **kw)
    else:
        p = subprocess.Popen(cmd, start_new_session=True, **kw)
    return p.pid


def _start_rerank_service() -> int:
    """沿用 start_llama_rerank.bat 的同一套参数（参数变了分数就变，别乱动）。"""
    exe = os.path.join(_RERANK_LLAMA_DIR, "llama-server.exe")
    if not os.path.exists(exe):
        raise RuntimeError("找不到 llama-server：%s" % exe)
    if not os.path.exists(_RERANK_GGUF):
        raise RuntimeError("找不到重排模型：%s" % _RERANK_GGUF)
    host, port = _rerank_endpoint()
    cmd = [exe, "-m", _RERANK_GGUF, "--rerank",
           "--host", host, "--port", str(port),
           "-c", "8192", "-b", "4096", "-ub", "4096",
           "--sleep-idle-seconds", "600", "-ngl", "99"]
    pid = _spawn_detached(cmd, _RERANK_LOG, _RERANK_LLAMA_DIR)
    _RERANK_LAST_START[0] = time.time()
    _log("rerank 按需拉起：pid=%d port=%s（日志 %s）" % (pid, port, _RERANK_LOG))
    return pid


def _ensure_rerank() -> bool:
    """确保 11437 可用：已起 → True；没起 → 拉起并等就绪。失败返回 False（绝不抛）。"""
    if _rerank_port_alive():
        return True
    if not _RERANK_AUTOSTART:
        _set_rerank_status(False, "service_down",
                           "11437 未监听，且已禁用按需拉起（BOSHI_RERANK_AUTOSTART=0）")
        return False
    now = time.time()
    if now - _RERANK_LAST_SPAWN[0] < _RERANK_SPAWN_COOLDOWN:
        return False                     # 冷却中：别每次检索都连发 Popen
    _RERANK_LAST_SPAWN[0] = now
    free = _vram_free_mib()
    if free is not None and free < _RERANK_VRAM_MIN:
        _set_rerank_status(False, "vram_low",
                           "按需拉起跳过：free=%dMiB < min=%dMiB" % (free, _RERANK_VRAM_MIN))
        return False
    try:
        _start_rerank_service()
    except Exception as e:               # noqa: BLE001
        _set_rerank_status(False, "autostart_failed", "%s: %s" % (type(e).__name__, e))
        return False
    t0 = time.time()
    while time.time() - t0 < _RERANK_AUTOSTART_WAIT:
        if _rerank_port_alive() and _rerank_ready():
            _VRAM_CACHE["t"] = 0.0        # 权重已上卡，显存变了 → 下次重查
            _log("rerank 按需拉起就绪：%.1fs" % (time.time() - t0))
            return True
        time.sleep(0.25)
    _set_rerank_status(False, "autostart_timeout",
                       "%.0f 秒内未就绪，见 %s" % (_RERANK_AUTOSTART_WAIT, _RERANK_LOG))
    return False


def _set_rerank_status(ok: bool, reason: str = "", detail: str = "") -> None:
    _RERANK_STATUS.update(ok=ok, reason=reason, ts=time.time(), detail=detail[:200])
    if ok:
        return
    if time.time() - _STATUS_LOG_AT[0] > 120:      # 降级原因写日志，但不刷屏
        _STATUS_LOG_AT[0] = time.time()
        _log("rerank 降级：%s %s" % (reason, detail[:160]))


def rerank_status() -> Dict[str, Any]:
    """重排运行状态快照（ok/reason/detail/ts + 配置 + 按需拉起 + 空闲卸载推断），供 doctor/人工核对。"""
    free = _VRAM_CACHE["free"]
    now = time.time()
    last_ok = _RERANK_LAST_OK[0]
    idle = (now - last_ok) if last_ok else None
    return {"enabled": _RERANK_ON, "url": _RERANK_URL, "ok": _RERANK_STATUS["ok"],
            "reason": _RERANK_STATUS["reason"], "detail": _RERANK_STATUS["detail"],
            "ts": _RERANK_STATUS["ts"], "pool": _RERANK_POOL,
            "pool_anchor": _RERANK_POOL_ANCHOR,
            "blend": {"w_rk": _BLEND_W_RK, "w_fu": _BLEND_W_FU,
                      "k": _BLEND_K, "head": _BLEND_HEAD},
            "vram_free_mib": free, "vram_min": _RERANK_VRAM_MIN,
            "down_until": _RERANK_DOWN_UNTIL[0],
            "autostart": _RERANK_AUTOSTART, "port_alive": _rerank_port_alive(),
            "spawn_cooldown": _RERANK_SPAWN_COOLDOWN,
            "last_spawn": _RERANK_LAST_SPAWN[0], "last_start": _RERANK_LAST_START[0],
            # ── 空闲卸载可观测 ──
            "sleep_after_sec": _RERANK_SLEEP_AFTER,
            "last_ok": last_ok,
            "idle_sec": round(idle, 1) if idle is not None else None,
            "asleep_likely": bool(idle is not None and idle >= _RERANK_SLEEP_AFTER),
            "wakes": _RERANK_WAKE_COUNT[0], "last_wake": _RERANK_LAST_WAKE[0]}



def _rerank_items(query: str, items: list, top_k: int) -> list:
    """用本地 bge-reranker 重排候选池，返回新的前 top_k 条（score 同步改写）。

    - 分数方向：llama.cpp /rerank 返回负 logit（越大越相关）；本库 score 是
      距离语义（越小越前），所以统一映射成 score = 1 - sigmoid(logit)，
      既保住「升序=好」的既有约定，又让展示相似度与重排概率对齐。
    - 分批：一次请求把多对 query-doc 塞进同一 batch，ubatch 4096 会报
      "input too large to process"，所以按字符预算切块（中文≈1 char/token）。
    - 任何失败 → 原序返回，不抛异常。
    """
    items = list(items)
    k = int(top_k)
    if len(items) < 2 or not (query or "").strip():
        return items[:k]
    if time.time() < _RERANK_DOWN_UNTIL[0]:      # 熔断中：直接走融合序
        return items[:k]
    # 按需拉起：服务没起（机器重启 / 被外部杀 / 从没启动）→ 拉起再重排；
    # 拉不起来（显存不够 / 冷却中 / 启动失败）→ 本次降级走融合序。
    if not _rerank_port_alive():
        if not _ensure_rerank():
            return items[:k]
        woke_now = True
        _RERANK_WAKE_COUNT[0] += 1
        _RERANK_LAST_WAKE[0] = time.time()
    else:
        woke_now = False
    # 显存预检：只有「模型很可能已经睡了」时才查显存（wake 需 +650MiB）；
    # 刚成功调用过（≤_RERANK_WARM_SEC）说明权重还在卡上，直接调，不做无谓拦截。
    # 刚被我们拉起 = 权重已加载，跳过预检（否则刚起来就被自己的显存检查挡掉）。
    if not woke_now and time.time() - _RERANK_LAST_OK[0] > _RERANK_WARM_SEC:
        free = _vram_free_mib()
        if free is not None and free < _RERANK_VRAM_MIN:
            _set_rerank_status(False, "vram_low",
                               "free=%dMiB < min=%dMiB" % (free, _RERANK_VRAM_MIN))
            return items[:k]
    try:
        import math
        import requests
    except Exception:
        return items[:k]

    texts = [(it.get("content") or it.get("document") or "")[:_RERANK_CHARS]
             for it in items]
    scores = {}
    buf, bidx = [], []

    def _flush():
        if not buf:
            return True
        resp = requests.post(_RERANK_URL,
                             timeout=(_RERANK_CONNECT_TIMEOUT, _RERANK_TIMEOUT),
                             json={"model": _RERANK_MODEL, "query": query,
                                   "documents": list(buf)})
        resp.raise_for_status()
        for rr in resp.json().get("results", []):
            scores[bidx[int(rr["index"])]] = float(rr.get("relevance_score") or 0.0)
        del buf[:], bidx[:]
        return True

    try:
        for i, t in enumerate(texts):
            if buf and sum(len(x) for x in buf) + len(t) > _RERANK_CHUNK:
                _flush()
            buf.append(t)
            bidx.append(i)
        _flush()
    except Exception as e:            # 端点没起 / 超时 / 500 / 显存不够被 OOM：降级 + 熔断
        _RERANK_DOWN_UNTIL[0] = time.time() + _RERANK_COOLDOWN
        _set_rerank_status(False, "endpoint_error", "%s: %s" % (type(e).__name__, e))
        return items[:k]

    # 有候选没拿到分（某一片 500/超时）→ 不能整套弃用：那会让「一片坏掉」等于
    # 「全部退化成融合序」。改成有分的按重排分排前面，没分的保持融合原序接在后面。
    if not scores:
        _RERANK_DOWN_UNTIL[0] = time.time() + _RERANK_COOLDOWN
        _set_rerank_status(False, "no_scores", "0/%d 有分" % len(items))
        return items[:k]
    missing = [i for i in range(len(items)) if i not in scores]
    if missing:
        _set_rerank_status(False, "partial_scores",
                           "%d/%d 无分，已按融合序补在后" % (len(missing), len(items)))
        for i in missing:
            items[i]["score"] = 1.0        # 距离语义的最差值；sort 稳定 → 组内保留原序
    else:
        _RERANK_LAST_OK[0] = time.time()
        _set_rerank_status(True)

    for i, it in enumerate(items):
        if i in missing:
            continue
        lg = scores[i]
        p = 1.0 / (1.0 + math.exp(-max(-30.0, min(30.0, lg))))
        # 落回库里惯用的 0.7~1.0 伪距离区间（与 RRF 融合同一约定）：score=1-(0.6+0.4p)。
        # 直接写 1-p 会把量纲压到 0~0.05，和 include_graph 并进来的余弦距离不可比。
        it["score"] = round(1.0 - (0.60 + 0.40 * p), 6)
        md = it.get("metadata")
        if isinstance(md, dict):
            md["rerank_score"] = round(lg, 4)
            # β2：暴露重排后验概率 + 三档置信度。离域/无答案的 query 拿到的这条
            # relevance 会很低（实测离域最高 ~0.42），调用方据此知道「别全信」。
            md["relevance"] = round(p, 4)
            md["confidence"] = ("high" if p >= _RELEVANCE_HIGH
                                else "mid" if p >= _RELEVANCE_MID else "low")
    scored = [i for i in range(len(items)) if i not in missing]
    rk_rank = {i: r for r, i in enumerate(
        sorted(scored, key=lambda i: (items[i]["score"], i)))}

    # 诊断采样（默认关）：把「融合序 + 原始重排分」整串落盘，用于离线复算任意
    # 混参而不必反复跑 150 条查询的在线评测。BOSHI_RERANK_TRACE=<目录>
    _tr = os.environ.get("BOSHI_RERANK_TRACE")
    if _tr:
        try:
            with open(os.path.join(_tr, "trace.jsonl"), "a", encoding="utf-8") as _f:
                _f.write(json.dumps({
                    "q": query,
                    "fu": [str(items[i].get("id"))[:8] for i in range(len(items))],
                    "rs": [None if scores.get(i) is None else round(scores[i], 4)
                           for i in range(len(items))],
                }, ensure_ascii=False) + "\n")
        except Exception:            # 采样失败绝不能影响检索
            pass


    if _BLEND_W_FU > 0 and len(scored) > 1:
        # 融合名次天然等于「进函数时的下标」：此时 items 仍是融合序，尚未被重排覆盖。
        key = {i: _BLEND_W_RK / (_BLEND_K + rk_rank[i] + 1.0) for i in scored}
        head = scored[:_BLEND_HEAD] if _BLEND_HEAD > 0 else scored
        for r, i in enumerate(head):
            key[i] += _BLEND_W_FU / (_BLEND_K + r + 1.0)
        order = sorted(key, key=lambda i: (-key[i], rk_rank[i]))
    else:
        order = sorted(scored, key=lambda i: (items[i]["score"], i))
    order += sorted(missing)              # 无分的按融合原序接在尾部

    # 按融合后的名次落盘顺序，同时把 score 压成单调不减（升序＝好）：被融合序
    # 顶上来的条目本来重排分偏差，若原样留着，下游任何按 score 再排一次的地方
    # 都会把它翻回去；单调化之后「顺序」与「分数」永远同向。
    out, prev = [], 0.0
    for i in order:
        it = items[i]
        d = float(it.get("score") or 0.0)
        if d < prev:
            d = prev
        prev = d
        it["score"] = round(d, 6)
        md = it.get("metadata")
        if isinstance(md, dict) and _BLEND_W_FU > 0:
            md["fusion_rank"] = i + 1
        out.append(it)
    return out[:k]


# ── β3 换值折叠：结果内「同一设定的新旧值」取新弃旧 ─────────────────────
# 库已有 upsert（旧行 is_latest=false + 新行 supersedes）和 detect_conflicts/
# resolve_conflict，但**检索没接上**：agent 问「当前设定」时，旧值行因字面更匹配
# query 仍顶到 top1（实测 40 vs 100：旧 0.9976 > 新 0.9777），读到已失效值。
#
# 判据（纯字符串、零模型、零 SQL）：同 topic + 去数字正文相等 + 数字部分不同
#   = 「同一事实改了个数」。跨 session 的数值互斥是噪声（实测 80 对全废：
#   「256 的 vllm」vs「4% 的养老金」碰巧数值不重叠但不是同一设定），所以必须
#   限定同 topic；去数字归一相等又比「数值互斥」更强（排掉同主题不同事）。
# 只改结果顺序与标记，**不写库**（真降级留给 resolve_conflict），让 agent 读新值。
_NUM_BODY_RE = re.compile(r"[\d.,]+")


def _norm_value_body(text: str) -> str:
    """去数字归一：数字/小数/千分位逗号 → '#'，空白去掉。「设定为 40」/「设定为 100」→「设定为#」相等。"""
    return _NUM_BODY_RE.sub("#", (text or "").replace(" ", ""))


def _fold_value_changes(items: list) -> list:
    """结果内换值对折叠：同 topic、去数字正文相等、数字不同 → 旧值标 superseded
    并甩到新值后面。零模型、纯字符串、不写库。"""
    if len(items) < 2:
        return items
    from collections import defaultdict

    def _ts(it):
        md = it.get("metadata") or {}
        return float(md.get("timestamp") or md.get("created_at") or 0.0)

    by_topic = defaultdict(list)
    for it in items:
        md = it.get("metadata") or {}
        by_topic[str(md.get("topic") or "")].append(it)

    folded = False
    for grp in by_topic.values():
        if len(grp) < 2:
            continue
        norm = [_norm_value_body(it.get("content", "")) for it in grp]
        for i in range(len(grp)):
            for j in range(i + 1, len(grp)):
                if norm[i] != norm[j]:
                    continue                     # 去数字后不等 → 不是「同一事实换值」
                a, b = grp[i], grp[j]
                raw_a = (a.get("content") or "").strip()
                raw_b = (b.get("content") or "").strip()
                if raw_a == raw_b:
                    continue                     # 数字也相同 = 完全重复，交给常规去重
                old, new = (a, b) if _ts(a) <= _ts(b) else (b, a)
                om = old.setdefault("metadata", {})
                om["superseded"] = True
                om["superseded_by"] = str(new["id"])
                om["value_superseded"] = True
                om["note"] = "数值已被同主题更新值取代，读「当前设定」请用新值"
                try:
                    old["score"] = round(max(float(new.get("score") or 0.0), 0.0) + 0.5, 6)
                except Exception:
                    old["score"] = 1.0
                folded = True
    if folded:
        # 稳定排序：只有旧值 score 被拉远，其余保序；结果此时全是记忆（未混图谱边）
        items.sort(key=lambda x: (float(x.get("score") or 0.0), str(x.get("id") or "")))
    return items


# β4（2026-10-19）：同主题数值冲突「暴露」而非「裁决」。
#
# 缘起：实测「云闸的开闸宽度当前设定为 40 层」→ 后来写入「已调整为 100 层」→
# 查「云闸的开闸宽度现在设定多少？」，旧值仍排 top1（rr 6.88 conf high）、新值第二
# （rr 5.26）。_fold_value_changes 的判据是「去数字后整句同构」，这两句措辞不同
# （「当前设定为」vs「已调整为」）→ 永不配对；detect_conflicts 又要槽位交集，
# 且只在离线可调。于是「同一设定的两个值」在检索里谁也不让谁，agent 无从知道有冲突。
#
# 这里只做两件事，绝不自动判定真伪、绝不写库（更不写 is_latest=false）：
#   1) 给冲突双方打 conflict/conflict_role/conflict_with/conflict_values 标记；
#   2) 把较新的一条排到较旧的之前（只调位置，score 不动）。
# 判据（全部可调/可关）：
#   BOSHI_CONFLICT_EXPOSE=0 关闭 ｜ BOSHI_CONFLICT_COS=0.88（两条正文余弦）
#   BOSHI_CONFLICT_MIN_GAP=60（秒，时间戳差太近的不算「新旧」）
_CONFLICT_ON = os.environ.get("BOSHI_CONFLICT_EXPOSE", "1") == "1"
_CONFLICT_COS = float(os.environ.get("BOSHI_CONFLICT_COS", "0.88"))
_CONFLICT_MIN_GAP = float(os.environ.get("BOSHI_CONFLICT_MIN_GAP", "60"))
_UNIT_AFTER_NUM = re.compile(
    r"(\d+(?:\.\d+)?)\s*(层|个|条|台|张|次|天|秒|分钟|小时|周|月|年|人|GB|GiB|MB|MiB|KB|%|ms|t/s|tok/s)?",
    re.IGNORECASE)


def _value_tokens(text: str) -> set:
    """正文里的量值集合：{(数值串, 单位小写)}。「40 层」→ {("40","层")}，无单位记 ""。"""
    out = set()
    for num, unit in _UNIT_AFTER_NUM.findall(text or ""):
        out.add((num.rstrip("."), (unit or "").strip().lower()))
    return out


def _doc_cos(texts: dict) -> dict:
    """{(id_a, id_b): cosine} —— 只对候选对算，失败返回空（降级为不判冲突）。"""
    try:
        import math
        ids = list(texts)
        vecs = {}
        for i in ids:
            v = _ef()([texts[i] or " "])[0]
            vecs[i] = v
        sims = {}
        for x in range(len(ids)):
            for y in range(x + 1, len(ids)):
                a, b = ids[x], ids[y]
                va, vb = vecs[a], vecs[b]
                dot = sum(p * q for p, q in zip(va, vb))
                na = math.sqrt(sum(p * p for p in va)) or 1.0
                nb = math.sqrt(sum(q * q for q in vb)) or 1.0
                sims[(a, b)] = sims[(b, a)] = dot / (na * nb)
        return sims
    except Exception:            # noqa: BLE001  冲突标记是增强项，坏了一律不标
        return {}


def _mark_value_conflicts(items: list) -> list:
    """同 topic + 数值互斥 + 正文高度相似 → 双向打 conflict 标记，并把新的排前面。

    不写库、不改 score、不判真伪：只让「这里有两个互斥值、哪个更新」对 agent 可见。
    """
    if not _CONFLICT_ON or len(items) < 2:
        return items
    from collections import defaultdict

    by_topic = defaultdict(list)
    for it in items:
        md = it.get("metadata") or {}
        if md.get("is_latest") is False or md.get("superseded"):
            continue                      # 已折叠的旧版本不再重复标冲突
        topic = str(md.get("topic") or "")
        if not topic:
            continue
        vals = _value_tokens(it.get("content", ""))
        if not vals:
            continue
        by_topic[topic].append((it, vals))

    pairs = []
    for grp in by_topic.values():
        if len(grp) < 2:
            continue
        texts = {str(it["id"]): (it.get("content") or "") for it, _ in grp}
        sims = _doc_cos(texts)
        for i in range(len(grp)):
            for j in range(i + 1, len(grp)):
                a, va = grp[i]
                b, vb = grp[j]
                if va == vb:
                    continue                  # 数值完全相同 → 纯重复，交给常规去重
                if sims.get((str(a["id"]), str(b["id"])), 0.0) < _CONFLICT_COS:
                    continue
                ua = {u for _, u in va if u}
                ub = {u for _, u in vb if u}
                if ua and ub and not (ua & ub):
                    continue                  # 单位完全不同 → 很可能不是同一量
                ma, mb = (a.get("metadata") or {}), (b.get("metadata") or {})
                ta = float(ma.get("timestamp") or ma.get("created_at") or 0.0)
                tb = float(mb.get("timestamp") or mb.get("created_at") or 0.0)
                if not ta or not tb or abs(ta - tb) <= _CONFLICT_MIN_GAP:
                    continue                  # 判不出新旧 → 不标（宁可漏报不误报）
                old, new = (a, b) if ta <= tb else (b, a)
                pairs.append((old, new, va, vb))

    for old, new, va, vb in pairs:
        for me, other, role, mv, ov in ((old, new, "older", va, vb),
                                        (new, old, "newer", vb, va)):
            mm = me.setdefault("metadata", {})
            mm["conflict"] = True
            mm["conflict_role"] = role
            mm["conflict_with"] = str(other["id"])
            mm["conflict_values"] = {"mine": sorted(map(list, mv)),
                                     "other": sorted(map(list, ov))}
            mm["conflict_note"] = ("同主题存在互斥数值，未自动判定真伪；"
                                   "较新的一条已排在前面，请结合上下文确认")
        # 新的排前面：只调位置（就地搬移，其余条目相对顺序不变）
        try:
            p_old = items.index(old)
            p_new = items.index(new)
            if p_new > p_old:
                items.pop(p_new)
                items.insert(p_old, new)
        except ValueError:
            pass
    return items


# β2：重排在线时用 relevance/confidence 暴露可置信度（在 _rerank_items 内写入）。
# 重排降级（服务死/OOM 静默回退）时结果没有 relevance —— 在 search_memory 末尾给
# 每条补 confidence="unknown"，让 agent 知道「这条是未重排的融合序，别全信」。


def search_memory(query: str, top_k: int = 5, where: dict = None,
                  all_versions: bool = False, include_graph: bool = False,
                  scope: str = None, me: str = None, fusion: str = None,
                  since=None, until=None):
    c = _conn()
    w = dict(where or {})
    if not all_versions:
        w.setdefault("isLatest", True)

    # 图谱边检索请求 → 走 graph_edges 表（与 Chroma 后端行为对齐）
    want_rel, rel_type = _scan_type(w)
    if want_rel:
        return _search_edges(query, top_k, rel_type)

    cond, params = _where_sql(w, scope, me or _resolve_profile(), include_graph)
    # P2 时间维度接检索：since/until 直接并进 where 条件（两条轨道都用同一 cond）
    _tc, _tp = _time_bounds(since, until)
    if _tc:
        cond = "(%s) AND (%s)" % (cond, _tc)
        params = list(params) + _tp
    mode = (fusion or os.environ.get("BOSHI_SEARCH_FUSION", "auto")).lower()
    # P2+：字段级匹配要在 top_k 之外还能看见候选（否则只能重排已进榜的几条）
    fm_on = os.environ.get("BOSHI_FIELD_MATCH", "1") == "1"
    pool_k = int(top_k) * max(1, _FM_POOL) if fm_on else int(top_k)
    if _RERANK_ON:                     # 重排要看见更深一层的候选，否则只能重排已进榜的
        # 池深按查询形态分档（见 _RERANK_POOL_ANCHOR 注释）：带锚点 40 更准也更快，
        # 纯中文 100 才有改写型需要的那层深度。
        _rp = _RERANK_POOL_ANCHOR if (_RERANK_POOL_ANCHOR > 0 and _has_anchor(query)) else _RERANK_POOL
        pool_k = max(int(pool_k), _rp)
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
                items = _score_merge(vec_items, kw_items, pool_k, query=query)

    # P2+ 字段级匹配（默认关；命中查询字面字段时抬升同字段候选）
    _cand_k = int(pool_k) if _RERANK_ON else int(top_k)
    if fm_on:
        try:
            items = _field_match(c, query, list(items), _cand_k)
        except Exception:            # 增强项，坏了退回融合排序
            items = list(items)[:_cand_k]
    else:
        items = list(items)[:_cand_k]

    # E 方案：交叉编码器重排（默认开；端点不可达自动降级回融合序）
    if _RERANK_ON:
        items = _rerank_items(query, items, top_k)
    else:
        items = items[:int(top_k)]

    # 同会话扩展（2026-10-19 新增，默认关；A5 实测后再决定是否默认开）
    if os.environ.get("BOSHI_SESSION_EXPAND", "0") == "1":
        try:
            items = _session_expand(c, qv, list(items), cond, params, top_k)
        except Exception:            # 扩展是增强项，坏了不能影响主检索
            pass

    # P1③ 经验层独立通道（2026-10-20，默认开，BOSHI_EXPERIENCE_CHANNEL=0 关）
    #    动机：经验行天生短、措辞与 query 不同构，向量通道容易被长对话淹没；
    #    而「踩过的坑」恰恰是最该在回答前看到的东西。做法是给它一条自己的路：
    #    关键词重叠打分（零模型、可解释），只占 top_k 里 1 个名额（BOSHI_EXP_SLOTS
    #    可调），且只让 promoted 或强字面命中的条目挤进来——否则一次性的感想会
    #    把真记忆顶掉。命中项 metadata.channels=["experience"]，调用方可区分来源。
    if os.environ.get("BOSHI_EXPERIENCE_CHANNEL", "1") != "0":
        try:
            try:
                import experience as _experience
            except Exception:                    # 直接以文件路径加载（CLI 场景）
                import importlib.util as _ilu
                _p = os.path.join(os.path.dirname(os.path.abspath(__file__)), "experience.py")
                _sp = _ilu.spec_from_file_location("boshi_experience", _p)
                _experience = _ilu.module_from_spec(_sp)
                _sp.loader.exec_module(_experience)
            _slots_n = int(os.environ.get("BOSHI_EXP_SLOTS", "1"))
            if _slots_n > 0:
                _exps = _experience.recall(query, top_k=_slots_n + 2, scope=scope,
                                           profile=me or _resolve_profile())
                _seen = {str(x["id"]): x for x in items}
                _pick = []
                for _e in _exps:
                    _hit = _seen.get(str(_e["id"]))
                    if _hit is not None:
                        # 向量/关键词已经把这条带上来了：不重复占位，但把来源标上，
                        # 让「经验层也认可了它」这件事对调用方可见。
                        _ch = _hit["metadata"].get("channels") or []
                        if "experience" not in _ch:
                            _hit["metadata"]["channels"] = _ch + ["experience"]
                        continue
                    if ((_e["metadata"].get("status") != "promoted")
                            and float(_e.get("score") or 0.0) < 0.8):
                        continue          # 未转正的弱命中不占名额
                    _e["score"] = round(1.0 - float(_e.get("score") or 0.0), 6)
                    _e["metadata"].setdefault("channels", ["experience"])
                    _pick.append(_e)
                    if len(_pick) >= _slots_n:
                        break
                if _pick:
                    items = (_pick + list(items))[:int(top_k)]
        except Exception:              # 增强项，坏了退回主链路结果
            pass

    # β2 降级打标：重排在线时 _rerank_items 已给每条写 relevance/confidence；
    # 重排静默降级（服务死/OOM/显存不足）时结果里没有 relevance，这里补
    # confidence="unknown"，让 agent 知道「这批是未重排的融合序，分数不能当可信度」。
    if _RERANK_ON:
        _rr_online = any(
            "relevance" in (it.get("metadata") or {}) for it in items)
        if not _rr_online:
            for it in items:
                _md = it.setdefault("metadata", {})
                if "relevance" not in _md:
                    _md["relevance"] = None
                    _md["confidence"] = "unknown"
                    _md["note"] = "重排未生效（静默降级），分数不可作相关性判据"

    # β3：同一设定的新旧值折叠（取新弃旧，不写库）——放在最终 return 前，
    # 让「当前设定」类 query 读到的是新值而非字面更匹配的旧值。
    items = _fold_value_changes(items)
    # β4：措辞不同、句式不同构的「同设定两值」折叠抓不到 → 这里补一层冲突暴露
    # （只标记 + 把新的排前面，不判真伪、不写库）。
    items = _mark_value_conflicts(items)

    if include_graph:      # 需要图谱联想时，并入边表结果（与 Chroma 后端行为对齐）
        v = _vec_literal(_ef()([query or " "])[0])
        with c.cursor() as cur:
            cur.execute("""
                SELECT id, content, """ + _EDGE_META_SEL + """ AS meta,
                       embedding <=> %s::vector AS dist
                FROM boshi.graph_edges ORDER BY embedding <=> %s::vector LIMIT %s
            """, (v, v, int(min(top_k, 100))))
            items = items + [_row_to_item(r) for r in cur.fetchall()]
        items.sort(key=lambda x: x["score"])
        items = items[:top_k]
    return items


def hybrid_search(query: str, top_k: int = 5, where: dict = None,
                  all_versions: bool = False, search_sessions: bool = True,
                  include_graph: bool = False, scope: str = None, me: str = None,
                  since=None, until=None):
    """语义检索（PG）+ 会话历史（Hermes state.db FTS，与存储后端无关）。"""
    mems = search_memory(query, top_k=top_k, where=where, all_versions=all_versions,
                         include_graph=include_graph, scope=scope, me=me,
                         since=since, until=until)
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
        cur.execute("SELECT count(*) FROM boshi.memories "
                    "WHERE COALESCE(topic, '') <> 'mental_model'")
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
    """删除一条记忆（含同名图谱边）。

    返回**行数**而不是 bool（#4：delete 幂等但无状态区分——删不存在的 ID 也回
    {success:true}，导致测试残留被误判成「清理完成」）：
      >0 = 真的删掉了（memories 或同名 graph_edges 行）
       0 = 不存在（not_found）
      -1 = 后端异常（error，与 not_found 必须能分开）
    0/正数在布尔语境下仍为假/真，老调用方 `if delete_memory(x)` 行为不变。
    """
    c = _conn()
    try:
        with c.cursor() as cur:
            cur.execute("DELETE FROM boshi.memories WHERE id = %s", (memory_id,))
            n = cur.rowcount
            cur.execute("DELETE FROM boshi.graph_edges WHERE id = %s", (memory_id,))
            n += cur.rowcount
        return int(n)
    except Exception:  # noqa: BLE001
        return -1


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
        cur.execute("""SELECT id, content,""" + _EDGE_META_SEL + """ AS meta
                       FROM boshi.graph_edges WHERE id = %s""", (memory_id,))
        row = cur.fetchone()
        if row:
            return {"id": str(row[0]), "content": row[1] or "", "metadata": row[2] or {}}
    return None


def find_edge(entity_a: str, entity_b: str, relation_norm: str = None,
              relation_raw: str = None):
    """按实体对 + 关系找一条已存在的边 ID，找不到返回 ""。

    用途：add_relation 的去重。原实现拉 get_all_relations() 再在内存里比，
    PG 化后边表有 8 万+ 行、且函数带 LIMIT 10000 → 既慢又漏（超窗口的边永不去重）。

    关系比较口径：**原串优先**。relation 列存的是原串，只有当归一值相同时才算同一条边；
    否则「引用」和「不同关系」都会被归一成 related 而互相顶掉（实测会少写边）。
    4 类标准关系的原串 == 归一值，两种比较等价。
    """
    try:
        c = _conn()
        sql = "SELECT id FROM boshi.graph_edges WHERE entity_a = %s AND entity_b = %s"
        ps = [entity_a, entity_b]
        if relation_raw:
            sql += (" AND (relation = %s OR (relation IN ('updates','extends','derives','related')"
                    " AND relation = %s))")
            ps += [relation_raw, relation_norm or relation_raw]
        elif relation_norm:
            sql += (" AND (CASE WHEN relation IN ('updates','extends','derives','related')"
                    " THEN relation ELSE 'related' END) = %s")
            ps.append(relation_norm)
        sql += " LIMIT 1"
        with c.cursor() as cur:
            cur.execute(sql, ps)
            row = cur.fetchone()
            return str(row[0]) if row else ""
    except Exception:
        return ""


def get_all_relations(top_k: int = 10000) -> list:
    c = _conn()
    with c.cursor() as cur:
        cur.execute("""SELECT id, content,""" + _EDGE_META_SEL + """ AS meta
                       FROM boshi.graph_edges LIMIT %s""", (int(top_k),))
        return [{"id": str(r[0]), "content": r[1], "metadata": r[2]} for r in cur.fetchall()]


# ── 白盒可编辑 / 快照回滚（共识5：可读、可改、可回滚）──────────────
# 原先 CLI 只能 save/delete，改一条错记（错别字、错版本号）只能删了重写，
# 丢掉原 id 与时间线；也没有任何"改之前先存一份"的退路。这两件事都是
# agent 长期记忆最容易出事的地方，所以补成原生能力。


def edit_memory(memory_id: str, content: str = None, meta_patch: dict = None,
                topic: str = None, is_latest: bool = None):
    """就地修改一条记忆（正文 / 元数据 / 主题 / 有效标记）。

    正文变更时**必须重算向量与槽位**：只改 content 列不改 embedding，检索
    仍按旧语义命中，等于"改了看不见"；槽位是正文的纯规则派生，不同步就会
    让字段级匹配读到旧版本号/端口。
    meta_patch 里值为 None 表示删除该键。
    """
    c = _conn()
    with c.cursor() as cur:
        cur.execute("SELECT content, meta, slots, topic, is_latest "
                    "FROM boshi.memories WHERE id = %s", (memory_id,))
        row = cur.fetchone()
    if not row:
        return None
    old_content, meta, slots, old_topic, old_latest = row
    meta = dict(meta or {})
    changed = []
    new_content = old_content if content is None else content
    reembed = content is not None and content != old_content
    if reembed:
        changed.append("content")
        if slots is not None:          # 原本就有槽位才重抽，避免给旧行凭空造槽位
            try:
                slots = _slots_mod().extract(new_content) or slots
            except Exception:          # 抽提增强项，坏了保留旧槽位
                pass
    if meta_patch:
        for k, v in meta_patch.items():
            if v is None:
                if k in meta:
                    meta.pop(k, None)
                    changed.append("meta." + k)
            elif meta.get(k) != v:
                meta[k] = v
                changed.append("meta." + k)
    if topic is not None and topic != old_topic:
        changed.append("topic")
    if is_latest is not None and bool(is_latest) != bool(old_latest):
        changed.append("isLatest")
    if not changed:
        return {"id": memory_id, "changed": []}

    sets, ps = [], []
    if reembed:
        v = _ef()([new_content or " "])[0]
        sets += ["content = %s", "embedding = %s::vector", "slots = %s::jsonb", "raw_len = %s"]
        ps += [new_content, _vec_literal(v),
               json.dumps(slots, ensure_ascii=False) if slots else None, len(new_content or "")]
    if meta_patch:
        sets.append("meta = %s::jsonb")
        ps.append(json.dumps(meta, ensure_ascii=False))
    if topic is not None:
        sets.append("topic = %s")
        ps.append(topic)
    if is_latest is not None:
        sets.append("is_latest = %s")
        ps.append(bool(is_latest))
    ps.append(memory_id)
    with c.cursor() as cur:
        cur.execute("UPDATE boshi.memories SET " + ", ".join(sets) + " WHERE id = %s",
                    tuple(ps))
    return {"id": memory_id, "changed": changed}


def _snapshot_dir() -> str:
    d = os.path.join(os.path.dirname(os.path.abspath(__file__)), "snapshots")
    os.makedirs(d, exist_ok=True)
    return d


def export_snapshot(path: str = None, scope: str = "all", me: str = None,
                    include_graph: bool = True):
    """全库导出为 JSONL 快照（保留原 id / created_at / slots），用于备份回滚。

    与 md 导出的区别：JSONL 是**可恢复**的（restore 用原 id 回灌），
    md 只是给人读的。默认落 ~/.boshi/snapshots/。
    """
    from datetime import datetime as _dt
    c = _conn()
    cond, params = _where_sql({}, scope, me or _resolve_profile())
    with c.cursor() as cur:
        cur.execute("SELECT id, content, profile, source, topic, role, session_id, meta, "
                    "is_latest, created_at, version_created, slots, raw_len "
                    "FROM boshi.memories WHERE " + cond + " ORDER BY created_at NULLS FIRST",
                    tuple(params))
        mems = cur.fetchall()
    if not path:
        path = os.path.join(_snapshot_dir(),
                            "boshi-%s.jsonl" % _dt.now().strftime("%Y%m%d-%H%M%S"))
    n = 0
    with open(path, "w", encoding="utf-8") as f:
        for r in mems:
            rec = {"kind": "memory", "id": str(r[0]), "content": r[1] or "",
                   "profile": r[2], "source": r[3], "topic": r[4], "role": r[5],
                   "session_id": r[6], "meta": r[7] or {}, "is_latest": r[8],
                   "created_at": r[9].isoformat() if r[9] else None,
                   "version_created": r[10], "slots": r[11], "raw_len": r[12]}
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
            n += 1
        if include_graph:
            with c.cursor() as cur:
                cur.execute("SELECT id, entity_a, entity_b, relation, content, version_created "
                            "FROM boshi.graph_edges")
                for r in cur.fetchall():
                    f.write(json.dumps({"kind": "edge", "id": str(r[0]), "entity_a": r[1],
                                        "entity_b": r[2], "relation": r[3],
                                        "content": r[4] or "",
                                        "version_created": r[5]}, ensure_ascii=False) + "\n")
                    n += 1
    return {"path": path, "count": n}


def restore_snapshot(path: str, dry_run: bool = False):
    """把快照回灌进库：只补**缺失的 id**（ON CONFLICT DO NOTHING），不覆盖现状。

    这个语义是刻意的：回滚的常见诉求是"删错了/库空了，把那条找回来"，
    而不是"用三天前的版本把今天的库冲掉"。要整库回退请先手动作业。
    """
    added, skipped, errors = 0, 0, []
    with open(path, "r", encoding="utf-8") as f:
        for ln, line in enumerate(f, 1):
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except Exception as e:
                errors.append("line %d: %s" % (ln, e))
                continue
            if dry_run:
                # 演练：只回答"会补多少、会跳过多少"，不写库、不算 embedding（省时间）
                try:
                    if rec.get("kind") == "edge":
                        with _conn().cursor() as cur:
                            cur.execute("SELECT 1 FROM boshi.graph_edges WHERE id = %s",
                                        (rec.get("id"),))
                            hit = cur.fetchone() is not None
                    else:
                        with _conn().cursor() as cur:
                            cur.execute("SELECT 1 FROM boshi.memories WHERE id = %s",
                                        (rec.get("id"),))
                            hit = cur.fetchone() is not None
                    skipped += 1 if hit else 0
                    added += 0 if hit else 1
                except Exception as e:
                    errors.append("line %d: %s" % (ln, e))
                continue
            try:
                if rec.get("kind") == "edge":
                    with _conn().cursor() as cur:
                        cur.execute("""INSERT INTO boshi.graph_edges
                                       (id, entity_a, entity_b, relation, content, version_created)
                                       VALUES (%s,%s,%s,%s,%s,%s) ON CONFLICT (id) DO NOTHING""",
                                    (rec.get("id"), rec.get("entity_a", ""), rec.get("entity_b", ""),
                                     rec.get("relation", ""), rec.get("content", ""),
                                     rec.get("version_created")))
                        added += 1 if cur.rowcount else 0
                        skipped += 0 if cur.rowcount else 1
                else:
                    c = _conn()
                    from datetime import datetime as _dt
                    created = None
                    if rec.get("created_at"):
                        try:
                            created = _dt.fromisoformat(rec["created_at"])
                        except Exception:
                            created = None
                    v = _ef()([rec.get("content") or " "])[0]
                    with c.cursor() as cur:
                        cur.execute("""INSERT INTO boshi.memories
                            (id, content, embedding, profile, source, topic, role, session_id,
                             meta, is_latest, created_at, version_created, slots, raw_len)
                            VALUES (%s,%s,%s::vector,%s,%s,%s,%s,%s,%s::jsonb,%s,%s,%s,%s::jsonb,%s)
                            ON CONFLICT (id) DO NOTHING""",
                                    (rec.get("id"), rec.get("content", ""), _vec_literal(v),
                                     rec.get("profile"), rec.get("source"), rec.get("topic"),
                                     rec.get("role"), rec.get("session_id"),
                                     json.dumps(rec.get("meta") or {}, ensure_ascii=False),
                                     bool(rec.get("is_latest", True)), created,
                                     rec.get("version_created"),
                                     json.dumps(rec["slots"], ensure_ascii=False) if rec.get("slots") else None,
                                     rec.get("raw_len")))
                        added += 1 if cur.rowcount else 0
                        skipped += 0 if cur.rowcount else 1
            except Exception as e:
                errors.append("line %d: %s" % (ln, e))
    return {"path": path, "added": added, "skipped": skipped,
            "errors": errors[:20], "error_count": len(errors)}


def export_markdown(path: str = None, scope: str = "all", me: str = None,
                    limit: int = 5000):
    """导出为人读的 Markdown（按 topic 分组，带 id 与时间），便于 review/归档。"""
    from datetime import datetime as _dt
    c = _conn()
    cond, params = _where_sql({}, scope, me or _resolve_profile())
    with c.cursor() as cur:
        cur.execute("SELECT id, content, topic, profile, created_at, is_latest "
                    "FROM boshi.memories WHERE " + cond +
                    " ORDER BY topic, created_at DESC LIMIT %s",
                    tuple(params) + (int(limit),))
        rows = cur.fetchall()
    if not path:
        path = os.path.join(_snapshot_dir(),
                            "boshi-%s.md" % _dt.now().strftime("%Y%m%d-%H%M%S"))
    prov = me or _resolve_profile()
    lines = ["# 伯仕记忆导出", "",
             "- 生成时间：%s" % _dt.now().strftime("%Y-%m-%d %H:%M:%S"),
             "- profile：%s（scope=%s）" % (prov, scope),
             "- 条数：%d" % len(rows), ""]
    cur_topic = object()
    for r in rows:
        if r[2] != cur_topic:
            cur_topic = r[2]
            lines += ["", "## %s" % (cur_topic or "(未分类)"), ""]
        ts = r[4].strftime("%Y-%m-%d %H:%M") if r[4] else "无时间"
        flag = "" if r[5] else " · 已被新版取代"
        lines.append("- **%s**%s `%s`" % (ts, flag, r[0]))
        body = (r[1] or "").strip().replace("\n", "\n  ")
        lines.append("  " + body)
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    return {"path": path, "count": len(rows)}


# ── 以下功能 PG 后端暂未实现（现役路径不依赖）────────────────────
def _not_impl(name: str):
    raise NotImplementedError(
        "%s 尚未在 PG 后端实现（现役链路不使用）；如需使用请暂用 chroma 后端" % name)


def auto_forget(dry_run: bool = False):
    _not_impl("auto_forget")


def fact_chain(memory_id: str, max_depth: int = 20) -> dict:
    """同一事实的版本链（新→旧），顺着 `meta.superseded_by` 走。

    `resolve_conflict` 只做「落败行 is_latest=false + superseded_by=赢家」，
    链本身是推导出来的，不额外建表。给定任一版本 id：
    - 若它是旧版 → 向上走到最新款；
    - 若它已是最新 → 向下找出所有指向它的旧版（递归）。
    """
    c = _conn()
    with c.cursor() as cur:
        cur.execute("""SELECT id, content, topic, is_latest, created_at,
                              meta->>'superseded_by', meta->>'supersede_reason'
                       FROM boshi.memories WHERE id = %s""", (str(memory_id),))
        row = cur.fetchone()
        if row is None:
            return None

        def _node(r):
            return {"id": str(r[0]), "content": r[1], "topic": r[2],
                    "is_latest": bool(r[3]),
                    "created_at": r[4].strftime("%Y-%m-%d %H:%M") if r[4] else None,
                    "superseded_by": str(r[5]) if r[5] else None,
                    "reason": r[6] or ""}

        chain = [_node(row)]
        seen = {str(row[0])}
        nxt = row[5]
        while nxt and len(chain) < max_depth:
            sid = str(nxt)
            if sid in seen:
                break
            cur.execute("""SELECT id, content, topic, is_latest, created_at,
                                  meta->>'superseded_by', meta->>'supersede_reason'
                           FROM boshi.memories WHERE id = %s""", (sid,))
            r = cur.fetchone()
            if r is None:
                break
            chain.append(_node(r))
            seen.add(sid)
            nxt = r[5]

        # 向下：所有（多级）指向本 id 的旧版
        cur.execute("""WITH RECURSIVE down(id, depth) AS (
                         SELECT id, 1 FROM boshi.memories
                          WHERE meta->>'superseded_by' = %s
                         UNION ALL
                         SELECT m.id, d.depth + 1 FROM boshi.memories m
                           JOIN down d ON m.meta->>'superseded_by' = d.id::text
                          WHERE d.depth < %s
                       )
                       SELECT m.id, m.content, m.topic, m.is_latest, m.created_at,
                              m.meta->>'superseded_by', m.meta->>'supersede_reason'
                         FROM down d JOIN boshi.memories m ON m.id = d.id
                        ORDER BY m.created_at DESC""", (str(memory_id), max_depth))
        descendants = [_node(r) for r in cur.fetchall()]

    latest = chain[-1]  # chain 是新→旧，末项即链尾（有链时=最新版）
    summary = "共 %d 版" % (len(chain) + len(descendants))
    if latest.get("created_at"):
        summary += "，最新 %s" % latest["created_at"]
    summary += "；最新内容：「%s」" % (latest["content"] or "")[:60].replace("\n", " ")
    return {"id": str(row[0]), "chain": chain, "descendants": descendants,
            "summary": summary}


def detect_conflicts(topic: str = None, top_k: int = 20, sim_threshold: float = 0.85,
                     max_pairs: int = 4000) -> list:
    """零模型冲突候选：同文件 + 高相似 + 槽位互斥 → 交人工 `resolve_conflict` 裁决。

    不做全表 O(n²) 比对（实测 6283 行要 12s+）：先按 `slots.files` 有交集挑候选对
    （1343 对量级），再算余弦，最后只看"同一槽位键取值互斥"的对——
    取值是包含关系（['8188'] vs ['8188','11355']）算补充信息，不算冲突。
    """
    import numpy as np
    c = _conn()
    with c.cursor() as cur:
        if topic:
            cur.execute("""SELECT id, content, slots, created_at FROM boshi.memories
                            WHERE is_latest AND slots IS NOT NULL AND topic = %s""", (topic,))
        else:
            cur.execute("""SELECT id, content, slots, created_at FROM boshi.memories
                            WHERE is_latest AND slots IS NOT NULL""")
        rows = cur.fetchall()

    idx, filemap = {}, {}
    for i, r in enumerate(rows):
        idx[str(r[0])] = i
        for f in (r[2].get("files") or []):
            filemap.setdefault(f, []).append(i)

    pairs = set()
    for ids in filemap.values():
        if len(ids) < 2 or len(ids) > 60:
            continue
        for a in range(len(ids)):
            for b in range(a + 1, len(ids)):
                pairs.add((ids[a], ids[b]))
                if len(pairs) >= max_pairs:
                    break
    if not pairs:
        return []

    need = sorted({i for p in pairs for i in p})
    idlist = [rows[i][0] for i in need]
    with c.cursor() as cur:
        cur.execute("SELECT id, embedding FROM boshi.memories WHERE id = ANY(%s)", (idlist,))
        emb = {}
        for mid, vec in cur.fetchall():
            if vec is None:
                continue
            v = np.fromstring(str(vec).strip("[]"), sep=",", dtype=np.float32)
            n = float(np.linalg.norm(v))
            if n > 0:
                emb[str(mid)] = v / n

    # boundary_envs 存的是整句话（抽取器口径），互相比较只会产生噪声，不参与冲突判定
    KEYS = ("ports", "versions", "commands", "numbers", "files")
    out = []
    for a, b in pairs:
        ia, ib = str(rows[a][0]), str(rows[b][0])
        if ia not in emb or ib not in emb:
            continue
        sim = float(np.dot(emb[ia], emb[ib]))
        if sim < sim_threshold:
            continue
        sa, sb = rows[a][2], rows[b][2]
        conflicts = []
        for k in KEYS:
            va = {str(x) for x in (sa.get(k) or [])}
            vb = {str(x) for x in (sb.get(k) or [])}
            if va and vb and not (va & vb):
                conflicts.append({"key": k, "a": sorted(va), "b": sorted(vb)})
        if not conflicts:
            continue
        out.append({"a": ia, "b": ib, "similarity": round(sim, 4),
                    "conflicts": conflicts,
                    "a_time": rows[a][3].strftime("%Y-%m-%d %H:%M") if rows[a][3] else None,
                    "b_time": rows[b][3].strftime("%Y-%m-%d %H:%M") if rows[b][3] else None,
                    "a_content": (rows[a][1] or "")[:120],
                    "b_content": (rows[b][1] or "")[:120]})
    out.sort(key=lambda x: -x["similarity"])
    return out[:top_k]


def resolve_conflict(winner_id: str, loser_id: str, reason: str = "") -> bool:
    """极简冲突处理：落败行 `is_latest=false` + `meta.superseded_by=winner`。

    不改表结构（沿用 v5.8 不可变日志约定），旧答案仍可 all_versions 追溯；
    fact_chain 就是顺着 superseded_by 往上走。
    """
    c = _conn()
    with c.cursor() as cur:
        cur.execute("""UPDATE boshi.memories
                       SET is_latest = false,
                           meta = meta || jsonb_build_object(
                                  'superseded_by', %s::text,
                                  'supersede_reason', %s::text)
                       WHERE id = %s AND is_latest
                       RETURNING id""", (str(winner_id), reason or "", str(loser_id)))
        return cur.fetchone() is not None


# ══════════════════════════════════════════════════════════════════
# 认知层：心智模型预计算页（P2 ④）
# 读 = 一条 SELECT，不加载 embedding 模型 → 免掉首轮 6.4s 冷启
# （实测 measure_cold.py：import 1.09s + 首次 hybrid 6.39s，正好压在
#   Hermes prefetch 8s 超时线上）
# ══════════════════════════════════════════════════════════════════
def get_mental_model(profile: str = None, scope: str = "self",
                     max_chars: int = 1400) -> dict:
    """读预计算页（纯 SQL）。返回 {"text": 注入文本, "pages": {key: {...}}}。"""
    import mental_model as mm
    import time as _t
    _t0 = _t.perf_counter()
    prof = profile or _resolve_profile()
    pages = mm.load_pages(prof, scope)
    return {"text": mm.render_all(prof, max_chars, scope) if pages else "",
            "pages": pages, "profile": prof, "scope": scope,
            "count": len(pages),
            "ms": round((_t.perf_counter() - _t0) * 1000)}


def rebuild_mental_model(profile: str = None, keys: list = None,
                         scope: str = None) -> dict:
    """重算并落库（零模型：每条都是记忆原文片段/槽位原值）。"""
    import mental_model as mm
    return mm.rebuild(profile or _resolve_profile(), keys, scope)


def prune_mental_model(profile: str = None, keep: int = None,
                       dry_run: bool = True) -> dict:
    """清理页面旧版（派生态）。默认 dry_run，删除需显式 dry_run=False。"""
    import mental_model as mm
    return mm.prune_pages(keep or mm.KEEP_VERSIONS,
                          profile or _resolve_profile(), dry_run)
