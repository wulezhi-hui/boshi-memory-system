"""经验层（P1③ / 2026-10-20）—— 零模型、跨会话累积的「可复用教训」。

定位（原设计 v7.2 §P1③ 的补课项）：
  - 事实层（`session_fact`）回答「这一轮定了什么」，**只在会话内滚动**，会话一结束
    就沉底。经验层回答「这类问题该怎么办、在什么前提下成立」，必须**跨会话**累积，
    并且要带适用条件（boundary_envs）和出处（source_fact_ids / source_msg_ids）。
  - 条目全部来自 `slots.extract()` 命中的 **原文片段**，本模块只做搬运、判重、累计
    **不生成、不改写、不总结**（乐之铁律：记忆/语料不用模型生成内容）。
  - 价值门槛两级：
      A. 质量门（写入即判，纯规则）：长度区间 + 非提问 + 有技术锚点或因果条件词；
      B. 复用门（跨会话）：同一 `exp_key` 在 **≥2 个不同 session** 出现才转正
         （status=promoted）；只出现过一次的留 candidate，召回降权但不丢。
    单靠 A 会把「顺带一提的感想」也收进来；单靠 B 则永远收不到第一条。两级合用，
    才能既收得住又不被污染。

存储：复用 `boshi.memories`，`topic='experience'`，去重键 `meta.exp_key`
（sha1(归一化正文)[:16]）。**不建新表** —— 经验行天然享受现有的向量/关键词检索、
时间线查询与版本化约定；独立通道只是在排序前把它们单独捞一遍。

后端：只在 pg 后端启用（默认）。chroma 回滚时本层自动 no-op 返回，不影响主链路。
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import time
from typing import Any, Dict, Iterable, List, Optional

logger = logging.getLogger(__name__)

EXP_TOPIC = "experience"
EXP_MIN_LEN = 12
EXP_MAX_LEN = 300          # 超过就丢弃（不截断：截断等于伪造半句，违反原文保真）
PROMOTE_SESSIONS = 2       # 跨 ≥N 个不同会话出现 → 转正
SESSION_KEEP = 6           # meta.sessions 最多留几个（判重用，不必留全）
ROUND_CAP = 3              # 每轮最多新收几条（slots 本身每个槽位也限 3）

# 质量门用的词表（全部是可人工复核的字面规则，不做语义推断）
_CAUSAL = ("因为", "所以", "导致", "否则", "才能", "必须", "不能", "避免",
           "只要", "除非", "实测", "不要", "禁止", "注意", "坑", "教训", "铁律")
_QUESTION_TAIL = ("？", "?")
_META_TALK = ("你觉得", "要不要", "你说", "谢谢", "你好", "请问", "怎么样呢")
# 具名物锚点：出现的英文标识符（工具/技能/参数/库名，如 local-rag-kb-querying、CUDA）。
# 一条教训只要点到了具体的东西，就比「代码要写干净点」这种观点句可复用。
_IDENT_RE = re.compile(r"(?<![A-Za-z0-9])[A-Za-z][A-Za-z0-9_.\-]{2,}")
# 召回用停用 bigram（纯虚词，命中它们不代表相关）
_STOP_BIGRAMS = {
    "这个", "那个", "我们", "你们", "他们", "什么", "怎么", "可以", "已经", "还是",
    "就是", "不是", "没有", "一个", "现在", "时候", "因为", "所以", "但是", "而且",
    "如果", "然后", "这样", "那样", "这些", "那些", "问题", "东西", "事情", "一下",
}

_CACHE: Dict[str, Any] = {"ts": 0.0, "rows": [], "profile": None, "gen": 0}
CACHE_TTL = 45.0


# ── 纯函数：归一化 / 判重键 / 质量门 ────────────────────────────────
def norm(s: str) -> str:
    """比较与落库用的归一化：压空白、去首尾、去尾部标点。不改正文其余字符。"""
    t = " ".join((s or "").split())
    return t.strip().rstrip("。；;，,、 　")


def exp_key(entry: str) -> str:
    """跨会话判重键：同一条原文（仅空白/尾标点差异）→ 同 key。"""
    return hashlib.sha1(norm(entry).encode("utf-8")).hexdigest()[:16]


def _has_anchor(slots: Optional[Dict[str, Any]]) -> bool:
    """是否有「技术锚点」：路径/命令/端口/版本/参数/带单位数字任一非空。

    锚点是「可复用」的硬证据：只有具体到某个路径、端口、参数值的教训，下次
    遇到同类场景才用得上；纯观点句（「代码要写得干净」）不算经验。
    """
    if not isinstance(slots, dict):
        return False
    for k in ("files", "commands", "ports", "versions", "flags", "numbers"):
        v = slots.get(k)
        if isinstance(v, (list, tuple)) and any(str(x).strip() for x in v):
            return True
    return False


def passes_value_gate(entry: str, slots: Optional[Dict[str, Any]] = None) -> bool:
    """质量门（A）：这条原文片段值不值得当经验收进来。"""
    t = norm(entry)
    if not (EXP_MIN_LEN <= len(t) <= EXP_MAX_LEN):
        return False
    if t.endswith(_QUESTION_TAIL) or "？" in t:      # 提问不是结论
        return False
    if any(k in t for k in _META_TALK):
        return False
    if not (_has_anchor(slots) or _IDENT_RE.search(t)
            or any(k in t for k in _CAUSAL)):
        return False
    return True


def collect(slots_meta_list: Iterable[Optional[Dict[str, Any]]]) -> List[Dict[str, Any]]:
    """从本轮各条消息的 slots meta 里收集候选经验（已过质量门、已去重）。

    返回 `[{"entry","envs","slots"}]`；`envs` 取**同一条消息**抽到的
    boundary_envs（适用条件与教训出自同一段话，才不会是张冠李戴的拼接）。
    """
    out: List[Dict[str, Any]] = []
    seen = set()
    for meta in slots_meta_list or []:
        raw = (meta or {}).get("slots")
        if isinstance(raw, str):
            try:
                raw = json.loads(raw)
            except Exception:
                continue
        if not isinstance(raw, dict):
            continue
        for entry in (raw.get("reusable_experience") or []):
            e = norm(str(entry))
            if not e or not passes_value_gate(e, raw):
                continue
            k = exp_key(e)
            if k in seen:
                continue
            seen.add(k)
            envs = [norm(str(x)) for x in (raw.get("boundary_envs") or []) if norm(str(x))]
            out.append({"entry": e, "envs": envs[:3], "slots": raw})
            if len(out) >= ROUND_CAP:
                return out
    return out


# ── 后端可用性 ─────────────────────────────────────────────────────
def backend_ok() -> bool:
    """经验层只在 pg 后端启用（写路径依赖 pg_bridge 的列结构）。"""
    try:
        import chroma_bridge
        return chroma_bridge.backend_name() == "pg"
    except Exception:
        return False


def _profile(profile: Optional[str] = None) -> str:
    if profile:
        return profile
    try:
        import chroma_bridge
        return chroma_bridge.resolve_profile()
    except Exception:
        return "default"


def _conn():
    import pg_bridge
    return pg_bridge._conn()


# ── 写入：新收 / 确认（跨会话）────────────────────────────────────
def _find(profile: str, key: str) -> Optional[Dict[str, Any]]:
    c = _conn()
    with c.cursor() as cur:
        cur.execute("""SELECT id, content, meta FROM boshi.memories
                       WHERE profile = %s AND topic = %s AND is_latest
                         AND meta->>'exp_key' = %s
                       ORDER BY created_at DESC LIMIT 1""",
                    (profile, EXP_TOPIC, key))
        row = cur.fetchone()
    if not row:
        return None
    meta = row[2] or {}
    return {"id": str(row[0]), "content": row[1], "meta": meta}


def _save(entry: str, meta: Dict[str, Any], profile: str) -> str:
    """落一条经验行。走 boshi_core.save → 与主链路同一个写入入口（自动打 profile/
    timestamp/槽位/embedding），不自己拼 INSERT。"""
    import boshi_core
    r = boshi_core.save(entry, topic=EXP_TOPIC, metadata=dict(meta), profile=profile)
    return str(r.get("memory_id") or "")


def _update_meta(memory_id: str, patch: Dict[str, Any]) -> None:
    c = _conn()
    with c.cursor() as cur:
        cur.execute("UPDATE boshi.memories SET meta = COALESCE(meta,'{}'::jsonb) || %s::jsonb "
                    "WHERE id = %s", (json.dumps(patch, ensure_ascii=False), memory_id))


def merge_meta(meta: Optional[Dict[str, Any]], session_id: str = "",
               fact_ids: Optional[List[str]] = None,
               msg_ids: Optional[List[str]] = None,
               envs: Optional[List[str]] = None) -> Dict[str, Any]:
    """纯函数：把一次重复出现并入既有 meta，返回 patch + 判定。

    这是「复用门」的全部逻辑，独立成纯函数是为了能脱离 PG 单测：
      - 同一会话重复：只 +hits，**不升级**（同会话里说两遍不值钱）；
      - 新会话出现：sessions 追加、hits+1，累计 ≥PROMOTE_SESSIONS 个会话 → promoted；
      - 已 promoted：不再改 status，只累加计数与出处。
    出处/环境条目都只**追加去重**并保留最近 8/3 条，避免 meta 无限膨胀。
    """
    m = dict(meta or {})
    sessions = [str(x) for x in (m.get("sessions") or []) if x]
    hits = int(m.get("hits") or 1) + 1
    new_session = bool(session_id) and session_id not in sessions
    if new_session:
        sessions = (sessions + [session_id])[-SESSION_KEEP:]
    status = m.get("status") or "candidate"
    if status != "promoted" and len(set(sessions)) >= PROMOTE_SESSIONS:
        status = "promoted"
    patch: Dict[str, Any] = {"hits": hits, "sessions": sessions,
                             "status": status, "last_seen": time.time()}
    if fact_ids:
        fids = [str(x) for x in (m.get("source_fact_ids") or []) if x]
        patch["source_fact_ids"] = (fids + [str(x) for x in fact_ids
                                            if str(x) not in fids])[-8:]
    if msg_ids:
        mids = [str(x) for x in (m.get("source_msg_ids") or []) if x]
        patch["source_msg_ids"] = (mids + [str(x) for x in msg_ids
                                          if str(x) not in mids])[-8:]
    if envs:
        ev = [str(x) for x in (m.get("boundary_envs") or []) if x]
        patch["boundary_envs"] = (ev + [str(x) for x in envs if str(x) not in ev])[:3]
    return {"patch": patch, "new_session": new_session, "status": status,
            "hits": hits, "sessions": sessions}


def remember(candidates: List[Dict[str, Any]], *, session_id: str = "",
             source_fact_ids: Optional[List[str]] = None,
             source_msg_ids: Optional[List[str]] = None,
             profile: Optional[str] = None) -> List[Dict[str, Any]]:
    """把本轮候选经验并入库。返回每条的处理结果（便于日志/自测）。

    处理分支：
      - 首次出现 → 新建行，status=candidate；
      - 同一会话重复出现 → 只累加 hits，**不升级**（同会话重复不值钱）；
      - 跨会话再次出现 → sessions 追加、hits+1，满 PROMOTE_SESSIONS 转 promoted；
      - 已 promoted 且本次是新会话 → 只累加（不再改 status）。
    任一条失败只影响该条（`status="error"`），绝不抛给调用方。
    """
    res: List[Dict[str, Any]] = []
    if not candidates or not backend_ok():
        return res
    prof = _profile(profile)
    sid = session_id or ""
    fact_ids = [str(x) for x in (source_fact_ids or []) if x]
    msg_ids = [str(x) for x in (source_msg_ids or []) if x]
    for cand in candidates[:ROUND_CAP]:
        entry = norm(cand.get("entry") or "")
        if not entry:
            continue
        key = exp_key(entry)
        envs = [str(x) for x in (cand.get("envs") or []) if x][:3]
        try:
            old = _find(prof, key)
            if old:
                meta = old.get("meta") or {}
                mg = merge_meta(meta, sid, fact_ids, msg_ids, envs)
                _update_meta(old["id"], mg["patch"])
                res.append({"action": "confirmed" if mg["new_session"] else "duplicate",
                            "id": old["id"], "status": mg["status"], "hits": mg["hits"],
                            "key": key})
            else:
                meta = {
                    "source": "hermes_plugin",
                    "session_id": sid,
                    "role": "system",
                    "kind": "lesson",
                    "exp_key": key,
                    "status": "candidate",
                    "hits": 1,
                    "sessions": [sid] if sid else [],
                    "boundary_envs": envs,
                    "source_fact_ids": fact_ids,
                    "source_msg_ids": msg_ids,
                    "first_seen": time.time(),
                    "last_seen": time.time(),
                    "env_source": "same_message" if envs else "",
                }
                mid = _save(entry, meta, prof)
                res.append({"action": "new", "id": mid, "status": "candidate",
                            "hits": 1, "key": key})
        except Exception as e:      # 单条坏了不影响其它条，也不影响主写入
            logger.debug("boshi 经验层写入跳过（%s）: %s", key, e)
            res.append({"action": "error", "key": key, "error": str(e)[:120]})
    if any(r.get("action") in ("new", "confirmed") for r in res):
        _CACHE["gen"] += 1          # 让召回缓存尽快失效
    return res


def link_back(result: List[Dict[str, Any]], source_fact_ids: Optional[List[str]],
              source_msg_ids: Optional[List[str]] = None) -> int:
    """双向链的另一半：在事实行/结论行的 meta 上写 `derived_experience_ids`。

    经验行 → 出处（source_fact_ids）在 `remember` 里已写；这里补反向索引，让
    「这条事实派生了哪些经验」一次查询就能拿到，不必全表 jsonb 扫描。
    返回成功链上的 (行数)。失败一律吞掉（反向链是增强项）。
    """
    ids = [str(x) for x in (source_fact_ids or []) + (source_msg_ids or []) if x]
    new_ids = [r["id"] for r in (result or [])
               if r.get("action") in ("new", "confirmed") and r.get("id")]
    if not ids or not new_ids:
        return 0
    n = 0
    try:
        c = _conn()
        with c.cursor() as cur:
            for rid in ids:
                cur.execute(
                    """UPDATE boshi.memories
                          SET meta = COALESCE(meta,'{}'::jsonb) ||
                              jsonb_build_object('derived_experience_ids',
                                (SELECT COALESCE(jsonb_agg(DISTINCT x), '[]'::jsonb)
                                   FROM jsonb_array_elements_text(
                                     COALESCE(meta->'derived_experience_ids','[]'::jsonb)
                                     || %s::jsonb) AS t(x)))
                        WHERE id = %s""",
                    (json.dumps(new_ids), rid))
                n += cur.rowcount or 0
    except Exception as e:
        logger.debug("boshi 经验反向链失败: %s", e)
    return n


# ── 召回：独立通道 ─────────────────────────────────────────────────
def _tokens(text: str) -> List[str]:
    """查询侧 token：中文取 2-gram，英文取词（小写）。用于关键词命中，不做语义。"""
    t = (text or "").lower()
    toks: List[str] = []
    for run in re.findall(r"[\u4e00-\u9fff]+", t):
        if len(run) < 2:
            continue
        for i in range(len(run) - 1):
            bg = run[i:i + 2]
            if bg not in _STOP_BIGRAMS:
                toks.append(bg)
    for w in re.findall(r"[a-z][a-z0-9_.#+-]{1,}", t):
        toks.append(w)
    seen, out = set(), []
    for x in toks:
        if x not in seen:
            seen.add(x)
            out.append(x)
    return out


def _rows(profile: Optional[str], force: bool = False) -> List[Dict[str, Any]]:
    """取本 profile 的候选经验（≤300 条，带 45s 进程内缓存）。

    profile=None → 全库（scope="all"）。
    经验层是「小而精」的集合（跨会话才转正，条目天然增长慢），全量拉进内存做
    关键词打分比在 SQL 里拼中文 bigram 简单得多，也避免给 PG 装分词扩展。
    """
    pkey = profile if profile is not None else "*"
    gen = _CACHE["gen"]
    if (not force and _CACHE["rows"] and _CACHE["profile"] == pkey
            and (time.time() - _CACHE["ts"]) < CACHE_TTL and _CACHE.get("gen_seen") == gen):
        return _CACHE["rows"]
    c = _conn()
    rows: List[Dict[str, Any]] = []
    with c.cursor() as cur:
        if profile is None:
            cur.execute("""SELECT id, content, meta FROM boshi.memories
                           WHERE topic = %s AND is_latest
                           ORDER BY created_at DESC LIMIT 300""", (EXP_TOPIC,))
        else:
            cur.execute("""SELECT id, content, meta FROM boshi.memories
                           WHERE profile = %s AND topic = %s AND is_latest
                           ORDER BY created_at DESC LIMIT 300""",
                        (profile, EXP_TOPIC))
        for rid, content, meta in (cur.fetchall() or []):
            meta = meta or {}
            rows.append({"id": str(rid), "content": content or "", "meta": meta})
    _CACHE.update({"ts": time.time(), "rows": rows, "profile": pkey, "gen_seen": gen})
    return rows


def recall(query: str, top_k: int = 3, profile: Optional[str] = None,
           scope: str = "self") -> List[Dict[str, Any]]:
    """按关键词重叠给经验打分，返回 top_k（promoted 优先）。

    打分口径（可解释、无模型）：
      score = 命中 token 数 / min(查询 token 数, 8)，再按状态加权
      promoted ×1.0，candidate ×0.5（只出现过一次的教训先降权，不让它挤掉主线召回）。
    入选门槛：score ≥ 0.34 且命中 ≥2 个 token（单 token 命中在中文里几乎等于噪声），
    或整条查询（≥4 字）作为子串出现。
    """
    if not query or not (query or "").strip():
        return []
    if not backend_ok():
        return []
    prof = None if scope == "all" else _profile(profile)
    rows = _rows(prof)
    qt = _tokens(query)
    if not qt:
        return []
    qn = max(1, min(len(qt), 8))
    q_raw = norm(query)
    out: List[Dict[str, Any]] = []
    for r in rows:
        content = r["content"]
        meta = r["meta"]
        low = content.lower()
        hit = sum(1 for t in qt if t in low)
        score = hit / qn
        if len(q_raw) >= 4 and q_raw in low:
            score = max(score, 0.8)
        if score < 0.34 or hit < 2:
            continue
        if (meta.get("status") or "candidate") != "promoted":
            score *= 0.5
        out.append({
            "id": r["id"],
            "content": content,
            "metadata": {**meta, "topic": EXP_TOPIC, "channels": ["experience"]},
            "score": round(min(score, 1.0), 4),
            "channels": ["experience"],
        })
    out.sort(key=lambda x: (x["metadata"].get("status") != "promoted", -x["score"]))
    return out[:max(0, int(top_k))]


def stats(profile: Optional[str] = None) -> Dict[str, Any]:
    """经验层现状（自测/巡检用）。"""
    if not backend_ok():
        return {"backend": "no-pg", "total": 0}
    prof = _profile(profile)
    c = _conn()
    with c.cursor() as cur:
        cur.execute("""SELECT COALESCE(meta->>'status','candidate') AS st, count(*)
                       FROM boshi.memories WHERE profile=%s AND topic=%s AND is_latest
                       GROUP BY 1""", (prof, EXP_TOPIC))
        by = {r[0]: int(r[1]) for r in (cur.fetchall() or [])}
    return {"backend": "pg", "profile": prof, "total": sum(by.values()), "by_status": by}
