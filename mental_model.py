"""心智模型页（P2 认知层）—— 固定问题的**零模型预计算页**。

为什么要有这个（2026-10-21 实测数据）：
  `import boshi_core` 1.09s，**首次 hybrid search 6.39s**（bge-m3 首次加载），
  第二次 0.06s。Hermes prefetch 的超时线是 8s，冷启那一次几乎贴着红线过。
  而「用户是谁 / 环境铁律 / 在做什么 / 踩过哪些坑 / 哪些事没完」这几件事，
  **每个会话开局都要知道，且几乎不变** —— 每轮花 6 秒重新向量检索一遍是浪费。
  预计算页把答案提前算好存一行，读 = 一条 SELECT（毫秒级，不加载任何模型）。

设计约束（乐之铁律：记忆/语料不用模型生成内容）：
  - 本模块**不生成、不改写、不总结**任何文字。页面每一行都是某条记忆的
    **原文片段**，或由槽位里的**原值**聚合出的计数（如端口出现次数）。
    每一行都带 evidence（记忆 id），可回溯到具体那一条。
  - 超预算时**整条删**，不截半句；只有单条本身就超过 MAX_LINE 时，
    才在标点处切断并显式标 `…`（让人一眼看出被切过）。
  - 输出确定性：同一库、同一时刻 → 逐字节相同的页面（便于 diff 与单测）。

存储：复用 `boshi.memories`，`topic='mental_model'`，
`session_id='__mental_model__'`，去重键 `meta.page_key`。**不建新表**——
与经验层同一个理由：天然享受版本化约定（is_latest + meta.supersedes）。
页面行**不写 embedding**（embedding 列可空）：页是给人/给模型直读的，
不需要参与向量检索；同时检索路径会显式排除 `topic='mental_model'`，
避免页面正文反过来污染召回。

开关：`BOSHI_MENTAL_MODEL=0` 全局关闭（读/建都 no-op）。
"""

from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

MM_TOPIC = "mental_model"
MM_SESSION = "__mental_model__"
MM_PAGE_VERSION = 1

PAGE_BUDGET = 2400          # 注入总字上限（每次会话只注入一次，故给到 2400）
MAX_LINE = 220              # 单行上限（超过才按标点切）
PAGE_MAX_LINES = 8          # 单页最多几行
STALE_HOURS = 24            # 超过这个时间没重建 → 后台重建
REBUILD_MIN_GAP = 300       # 两次重建最小间隔（秒），防抖
KEEP_VERSIONS = 8           # 每页保留的历史版本数（prune 时用，只删派生态）

# 测试残留/空话（都是见过的真实污染样本，字面匹配，不做语义判断）
_JUNK = ("只回复两个字", "不要调用任何工具", "收到，不要调用")
_DRIVE_RE = re.compile(r"^([A-Za-z]:[\\/][^\\/\s，。；;、)）]{1,24})")
_PORT_RE = re.compile(r"^\d{2,5}$")


def enabled() -> bool:
    return os.environ.get("BOSHI_MENTAL_MODEL", "1") != "0"


def _clean(text: str) -> str:
    """压掉换行/连续空白（页面是单行列表，换行会毁掉排版）。"""
    return re.sub(r"\s+", " ", (text or "").strip())


def _worth(text: str, min_len: int = 12) -> bool:
    t = _clean(text)
    if len(t) < min_len:
        return False
    if any(j in t for j in _JUNK):
        return False
    return True


def _clip(text: str) -> str:
    """超 MAX_LINE 才切：只切在标点上并显式标记，绝不切出半句无标记。"""
    t = _clean(text)
    if len(t) <= MAX_LINE:
        return t
    cut = max(t.rfind("。", 0, MAX_LINE), t.rfind("；", 0, MAX_LINE),
              t.rfind("，", 0, MAX_LINE))
    if cut < 60:
        cut = MAX_LINE
        return t[:cut] + "…"
    return t[:cut + 1] + "…"


def _dedupe(rows: List[Dict[str, Any]], key_len: int = 28) -> List[Dict[str, Any]]:
    out, seen = [], set()
    for r in rows:
        k = _clean(r["text"])[:key_len]
        if k in seen:
            continue
        seen.add(k)
        out.append(r)
    return out


def _rows(cur, sql: str, params: tuple) -> List[tuple]:
    cur.execute(sql, params)
    return cur.fetchall()


def _prof_clause(scope: str, prof: str) -> tuple:
    """scope=self → 只看本 profile；all → 全库（沿用 profiles.json 的既有语义）。"""
    if (scope or "self") == "all":
        return "", ()
    return "profile IS NOT DISTINCT FROM %s AND ", (prof,)


# ══════════════════════════════════════════════════════════════════
# 五个固定页（问题 → 取数规则）
# ══════════════════════════════════════════════════════════════════
_ENV_KW = ("端口", "路径", "代理", "显存", "临时", "目录", "磁盘", "模型放", "镜像", "GPU")
# 坑页的质量门：自检/连通性测试这类自测残留没有复用价值（实测里真混进来了）
_PIT_KW = ("教训", "坑", "bug", "根因", "原因", "症状", "修复", "踩", "误杀", "冲突", "报错", "失败")


def _page_identity(cur, prof: str = None, scope: str = "self") -> List[Dict[str, Any]]:
    """用户是谁 / 长期规矩（来自 memory 工具的写入 + preference 主题）。"""
    pc, pp = _prof_clause(scope, prof)
    rows = _rows(cur, """
        SELECT id::text, content FROM boshi.memories
         WHERE %sis_latest
           AND topic IN ('memory_tool', 'preference', 'user')
           AND content IS NOT NULL
         ORDER BY created_at DESC NULLS LAST
         LIMIT 40""" % pc, pp)
    items = [{"text": c, "evidence": [i]} for i, c in rows
             if _worth(c) and not any(k in c for k in _ENV_KW)]
    return _dedupe(items)[:PAGE_MAX_LINES]


def _page_env(cur, prof: str = None, scope: str = "self") -> List[Dict[str, Any]]:
    """环境铁律（含路径/端口/代理的规矩）+ 槽位里的高频端口聚合。"""
    pc, pp = _prof_clause(scope, prof)
    rows = _rows(cur, """
        SELECT id::text, content FROM boshi.memories
         WHERE %sis_latest
           AND topic IN ('memory_tool', 'preference', 'user')
           AND content IS NOT NULL
         ORDER BY created_at DESC NULLS LAST
         LIMIT 60""" % pc, pp)
    items = [{"text": c, "evidence": [i]} for i, c in rows
             if _worth(c) and any(k in c for k in _ENV_KW)]
    items = _dedupe(items)[:PAGE_MAX_LINES - 1]

    # 端口聚合：只认 100–65535 的纯数字（槽位里混进过 "09"/"32" 这类时间碎片）
    prt = _rows(cur, """
        SELECT p.v, count(*) AS n, min(m.id::text) AS eid
          FROM boshi.memories m,
               jsonb_array_elements_text(COALESCE(m.slots->'ports', '[]'::jsonb)) AS p(v)
         WHERE %sm.is_latest
           AND p.v ~ '^[0-9]{2,5}$'
           AND p.v::int BETWEEN 100 AND 65535
         GROUP BY p.v ORDER BY n DESC, p.v LIMIT 6""" % pc, pp)
    if prt:
        items.append({
            "text": "常用端口（按出现次数）：" + "、".join("%s(%d)" % (v, n) for v, n, _ in prt),
            "evidence": [e for _, _, e in prt if e][:3],
        })
    return items


def _page_active_projects(cur, prof: str = None, scope: str = "self") -> List[Dict[str, Any]]:
    """活跃项目：近 45 天的 project 主题记录（按天去重，保留最新一条）。"""
    pc, pp = _prof_clause(scope, prof)
    rows = _rows(cur, """
        SELECT id::text, content FROM boshi.memories
         WHERE %sis_latest
           AND topic = 'project' AND content IS NOT NULL
           AND created_at > now() - interval '45 days'
         ORDER BY created_at DESC NULLS LAST LIMIT 30""" % pc, pp)
    items = [{"text": c, "evidence": [i]} for i, c in rows if _worth(c, 20)]
    return _dedupe(items, key_len=40)[:6]


def _page_pitfalls(cur, prof: str = None, scope: str = "self") -> List[Dict[str, Any]]:
    """反复踩的坑：troubleshooting 记录 + 复现过（hits>=2）的经验。"""
    pc, pp = _prof_clause(scope, prof)
    rows = _rows(cur, """
        SELECT id::text, content FROM boshi.memories
         WHERE %sis_latest
           AND topic = 'troubleshooting' AND content IS NOT NULL
         ORDER BY created_at DESC NULLS LAST LIMIT 20""" % pc, pp)
    items = [{"text": c, "evidence": [i]} for i, c in rows
             if _worth(c, 24) and any(k in c for k in _PIT_KW)]

    exp = _rows(cur, """
        SELECT id::text, content FROM boshi.memories
         WHERE %sis_latest
           AND topic = 'experience' AND content IS NOT NULL
           AND COALESCE((meta->>'hits')::int, 1) >= 2
         ORDER BY COALESCE((meta->>'hits')::int, 1) DESC,
                  created_at DESC NULLS LAST LIMIT 10""" % pc, pp)
    items += [{"text": c, "evidence": [i]} for i, c in exp if _worth(c, 20)]
    return _dedupe(items, key_len=36)[:6]


# ── 未了事项页的质量门（2026-10-21）──
# 全部是字面/形态判断，不调模型（与"不用模型生成内容"铁律一致）。
_OPEN_TASK_KW = ("待办", "待定", "待你", "待确认", "待补", "TODO", "下一步", "还需",
                 "还在等", "还没", "尚未", "未完成", "未开工", "未合并", "未解决",
                 "需要确认", "需要你", "等你", "需要补")
_OPEN_CLOSED_KW = ("正式关闭", "这项关闭", "已关闭", "不需要改", "无需再改", "已推送",
                   "已合并", "作废", "不用了")
_OPEN_META_KW = ("你还没选", "你没有选", "什么都没动", "什么都不动", "什么都不改",
                 "一个字都还没", "等你回来", "等你召唤", "等我召唤", "等你放行")
_OPEN_LEAD_DROP = ("没有", "不是", "无法")
# 强待办词：agent/用户把活儿明确挂在某处才会这么写。只有强信号才允许长句（带上下文说明）；
# 仅靠「还没/尚未」这类弱信号的长句基本是讨论/总结（实测反例：《从 RAG 走向 LLM Wiki…》读书笔记）。
_OPEN_STRONG_KW = ("待你", "等你", "待办", "待定", "待确认", "TODO", "下一步", "需要你",
                   "还需", "未开工", "未合并", "未解决",
                   "未完成", "尚未完成", "需要确认", "需要补")
_OPEN_WEAK_MAX = 60


def _open_thread_ok(text: str, topic: str = "") -> bool:
    """这条 open question 够不够格进「未了事项」页（规则见 _page_open_threads 文档串）。"""
    t = _clean(text)
    if any(k in t for k in _OPEN_CLOSED_KW) or any(k in t for k in _OPEN_META_KW):
        return False
    if t.startswith(_OPEN_LEAD_DROP):
        return False
    has_q = ("?" in t) or ("？" in t)
    # 用户原话：只有提问才算未了；陈述（"暂停，…等我召唤"）是状态播报
    if topic == "conversation" and not has_q:
        return False
    return has_q or any(k in t for k in _OPEN_TASK_KW)


def _open_thread_rank(text: str, topic: str = "") -> int:
    """排序/裁剪用的分档：0=强待办，1=弱信号，2=丢弃。

    弱信号（只有「还没/尚未」这类字面）本身就不可靠：实测反例是一条读书笔记
    「这篇《从 RAG 走向 LLM Wiki 的 4 阶段》…区分我们真做到了的和还没做到的」，
    命中「还没做」。所以弱信号只留短句（短句多是「某服务还没挂自启」这种真待办），
    长句一律当讨论/总结丢掉。
    """
    if not _open_thread_ok(text, topic):
        return 2
    if any(k in text for k in _OPEN_STRONG_KW):
        return 0
    return 1 if len(text) <= _OPEN_WEAK_MAX else 2


def _page_open_threads(cur, prof: str = None, scope: str = "self") -> List[Dict[str, Any]]:
    """未了事项：槽位 open_questions 里近 21 天的条目（每条取第一个问题）。

    质量门（2026-10-21 加）：本页以前的取数是「最近 25 条、每条取第一项」，
    没有任何判据，于是最新的一条垃圾稳居第一行。实测 top1 是用户原话
    「暂停，目前伯仕记忆系统升级尚未完成，并且工作日志和技术文档也还没写入，
    等我召唤你再继续」——上游 `slots.py` 的 OPEN_Q 是纯字面命中（待/还没/下一步…），
    陈述句也会被抽成 open question。这里按形态过滤，不调模型：

      a) `topic='conversation'`（用户原话）必须带问号：用户提问才是真未了，用户陈述不是；
      b) 必须含任务标记（待你/还没/下一步/…）：完成播报（"v3片头剪52帧已落地…"）不进本页；
      c) 已声明收尾的丢掉（正式关闭/不需要改/已推送）；
      d) 对话元语言丢掉（"你还没选"/"等你回来"/"什么都不动"）——那是 agent 的状态播报；
      e) 断言式开头（"没有…"/"不是…"）丢掉，这类极少是待办。
    """
    pc, pp = _prof_clause(scope, prof)
    rows = _rows(cur, """
        SELECT id::text, topic, slots->'open_questions' AS q FROM boshi.memories
         WHERE %sis_latest
           AND slots->'open_questions' <> '[]'::jsonb
           AND created_at > now() - interval '21 days'
         ORDER BY created_at DESC NULLS LAST LIMIT 40""" % pc, pp)
    items = []
    for i, topic, q in rows:
        if isinstance(q, str):
            try:
                q = json.loads(q)
            except Exception:  # noqa: BLE001
                continue
        if not isinstance(q, list):
            continue
        for one in q[:1]:
            one = str(one)
            one = re.sub(r"\*\*(.+?)\*\*", r"\1", one)         # 成对 `**` 是原文强调，先剥掉再判形态
            one = re.sub(r"^[-*•\s]+", "", one).strip()       # 槽位里带过 "- " / "**" 开头
            one = one.rstrip("；;，,、 ")                        # 切句残留的尾部分隔符
            if len(one) < 2:
                continue
            if not _worth(one, 15):
                continue
            rank = _open_thread_rank(one, topic)
            if rank == 2:
                continue
            items.append({"text": one, "evidence": [i], "_rank": rank})
    items.sort(key=lambda x: x["_rank"])       # 稳定排序：强待办在前，各自保持时间倒序
    for it in items:
        it.pop("_rank", None)
    return _dedupe(items, key_len=36)[:6]


PAGES = (
    ("identity", "用户画像与长期规矩", _page_identity),
    ("env", "环境参数与铁律", _page_env),
    ("active_projects", "活跃项目", _page_active_projects),
    ("pitfalls", "反复踩的坑", _page_pitfalls),
    ("open_threads", "未了事项", _page_open_threads),
)


# ══════════════════════════════════════════════════════════════════
# 建页 / 存页 / 读页
# ══════════════════════════════════════════════════════════════════
def build_page(key: str, profile: Optional[str] = None,
               scope: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """算一页。纯 SQL + 规则，不加载模型，不需要网络。

    scope 默认取 `profiles.json[profile].scope`（与检索路径同一套隔离设置）。
    """
    import chroma_bridge
    import pg_bridge as pgb
    prof = profile or pgb._resolve_profile()
    sc = scope or chroma_bridge.default_scope(prof)
    spec = next((p for p in PAGES if p[0] == key), None)
    if spec is None:
        return None
    _, title, fn = spec
    c = pgb._conn()
    with c.cursor() as cur:
        lines = fn(cur, prof, sc)
    evidence = []
    for ln in lines:
        evidence.extend(ln.get("evidence") or [])
    return {
        "key": key, "title": title, "lines": lines,
        "evidence_ids": list(dict.fromkeys(evidence)),
        "generated_at": time.time(),
        "profile": prof, "scope": sc,
    }


def render(page: Dict[str, Any]) -> str:
    """一页 → markdown 正文（每行前面加 `- `，末尾标注证据数）。"""
    body = []
    for ln in page.get("lines") or []:
        t = _clip(ln.get("text", ""))
        if not t:
            continue
        body.append("- " + t)
    if not body:
        return ""
    head = "### %s" % page.get("title", page.get("key", ""))
    return head + "\n" + "\n".join(body)


def save_page(page: Dict[str, Any]) -> Optional[str]:
    """存一页：旧版 is_latest=false，插入新版（不覆盖、不写 embedding）。"""
    import uuid
    import pg_bridge as pgb
    text = render(page)
    if not text:
        return None
    prof = page.get("profile") or pgb._resolve_profile()
    mid = str(uuid.uuid4())
    meta = {
        "page_key": page["key"],
        "page_version": MM_PAGE_VERSION,
        "scope": page.get("scope") or "self",
        "generated_at": page["generated_at"],
        "evidence_ids": page.get("evidence_ids") or [],
        "line_count": len(page.get("lines") or []),
        "kind": "mental_model",
        "source": "mental_model.py",
        "session_id": MM_SESSION,
        "topic": MM_TOPIC,
    }
    c = pgb._conn()
    with c.cursor() as cur:
        cur.execute("""SELECT id::text, content FROM boshi.memories
                        WHERE profile IS NOT DISTINCT FROM %s AND topic = %s
                          AND meta->>'page_key' = %s
                          AND COALESCE(meta->>'scope', 'self') = %s
                          AND is_latest""",
                    (prof, MM_TOPIC, page["key"], meta["scope"]))
        rows = cur.fetchall()
        old = [r[0] for r in rows]
        # 内容逐字节相同的页不落新版本：24h 定时重建时大部分页是不变的，
        # 不拦就会每天凭空多 4~5 行派生态，旧版只能靠 prune 收。
        if rows and (rows[0][1] or "") == text:
            logger.debug("mental_model %s 内容未变，跳过落库", page["key"])
            return rows[0][0]
        if old:
            cur.execute("""UPDATE boshi.memories SET is_latest = false
                            WHERE topic = %s AND id = ANY(%s::uuid[])""",
                        (MM_TOPIC, old))
            meta["supersedes"] = old[0]
        cur.execute("""INSERT INTO boshi.memories
                       (id, content, profile, source, topic, session_id, meta,
                        is_latest, created_at, version_created, raw_len)
                       VALUES (%s, %s, %s, 'mental_model', %s, %s, %s::jsonb,
                               true, now(), %s, %s)""",
                    (mid, text, prof, MM_TOPIC, MM_SESSION,
                     json.dumps(meta, ensure_ascii=False), time.time(), len(text)))
    return mid


def prune_pages(keep: int = KEEP_VERSIONS, profile: Optional[str] = None,
                dry_run: bool = True) -> Dict[str, Any]:
    """只清理派生态：每个 (profile, page_key, scope) 只留最新 keep 版。

    页面是「可重算的派生态」，不是记忆：rebuild 一次就多 4~5 行旧版，
    长期会往 boshi.memories 里堆无用的历史。默认 dry_run=True，
    真正删除必须由调用方显式传 dry_run=False（CLI 是 `--prune-apply`）。
    """
    import pg_bridge as pgb
    prof = profile or pgb._resolve_profile()
    out: Dict[str, Any] = {"dry_run": dry_run, "keep": keep, "profile": prof,
                           "pages": {}, "candidates": [], "removed": 0}
    c = pgb._conn()
    with c.cursor() as cur:
        # 只按 id/时间排序取 rn>keep 的行；不碰 is_latest 行（rn=1 恒为有效页）
        cur.execute("""WITH ranked AS (
                         SELECT id::text AS id,
                                COALESCE(meta->>'page_key', '?') AS page_key,
                                row_number() OVER (
                                  PARTITION BY profile,
                                               COALESCE(meta->>'page_key', '?'),
                                               COALESCE(meta->>'scope', 'self')
                                  ORDER BY is_latest DESC, created_at DESC) AS rn
                         FROM boshi.memories
                         WHERE topic = %s
                           AND profile IS NOT DISTINCT FROM %s)
                       SELECT id, page_key, rn FROM ranked
                       WHERE rn > %s ORDER BY page_key, rn""",
                    (MM_TOPIC, prof, max(1, int(keep))))
        rows = cur.fetchall()
        for mid, key, rn in rows:
            out["candidates"].append({"id": mid, "page_key": key, "rn": rn})
            out["pages"][key] = out["pages"].get(key, 0) + 1
        if rows and not dry_run:
            ids = [r[0] for r in rows]
            cur.execute("""DELETE FROM boshi.memories
                            WHERE topic = %s AND is_latest = false
                              AND id = ANY(%s::uuid[])""", (MM_TOPIC, ids))
            out["removed"] = cur.rowcount
    if not dry_run:
        logger.info("mental_model prune：删除 %d 行旧版（keep=%d）", out["removed"], keep)
    return out


def rebuild(profile: Optional[str] = None, keys: Optional[List[str]] = None,
            scope: Optional[str] = None) -> Dict[str, Any]:
    """重建（全部或指定页）。返回 {"built": [...], "empty": [...], "ms": n}。"""
    if not enabled():
        return {"built": [], "empty": [], "ms": 0, "disabled": True}
    import pg_bridge as pgb
    prof = profile or pgb._resolve_profile()
    t0 = time.time()
    built, empty = [], []
    for key, _title, _fn in PAGES:
        if keys and key not in keys:
            continue
        page = build_page(key, prof, scope)
        if not page or not page["lines"]:
            empty.append(key)
            continue
        if save_page(page):
            built.append(key)
    ms = int((time.time() - t0) * 1000)
    logger.info("mental_model 重建完成：built=%s empty=%s %dms", built, empty, ms)
    return {"built": built, "empty": empty, "ms": ms, "profile": prof,
            "scope": scope or "self"}


def load_pages(profile: Optional[str] = None,
               scope: str = "self") -> Dict[str, Dict[str, Any]]:
    """读全部页：**一条 SELECT**，不碰 embedding、不加载模型。"""
    import pg_bridge as pgb
    prof = profile or pgb._resolve_profile()
    c = pgb._conn()
    with c.cursor() as cur:
        cur.execute("""SELECT meta->>'page_key', content, meta
                         FROM boshi.memories
                        WHERE profile IS NOT DISTINCT FROM %s AND topic = %s
                          AND is_latest
                        ORDER BY created_at DESC""", (prof, MM_TOPIC))
        rows = cur.fetchall()
    out: Dict[str, Dict[str, Any]] = {}
    for key, content, meta in rows:
        if not key or key in out:
            continue
        m = meta or {}
        # 页面按 scope 分开存（self / all），读的时候只认同一 scope，避免全库页覆盖本 profile 页
        if (m.get("scope") or "self") != scope:
            continue
        out[key] = {"key": key, "content": content or "",
                    "generated_at": m.get("generated_at", 0),
                    "evidence_ids": m.get("evidence_ids") or []}
    return out


def render_all(profile: Optional[str] = None, max_chars: int = PAGE_BUDGET,
               scope: str = "self") -> str:
    """读页并拼成注入文本；空则返回空串（调用方自行决定要不要实时检索兜底）。

    超预算时**按行从末尾裁剪**：每页至少留标题+首行，绝不让预算把整页
    （尤其 env 这类最贵的页）整块丢掉——旧实现整页丢，结果只剩 identity 一页。
    """
    if not enabled():
        return ""
    pages = load_pages(profile, scope)
    order = [k for k, _t, _f in PAGES]
    blocks = []
    for k in order:
        c = (pages.get(k) or {}).get("content") or ""
        if c:
            blocks.append(c.split("\n"))
    if not blocks:
        return ""
    head = "## 伯仕心智模型（预计算页·可能滞后，与本轮召回冲突时以召回原文为准）"

    def _join(bs) -> str:
        body = "\n".join("\n".join(b) for b in bs if b)
        return head + "\n" + body if body else ""

    text = _join(blocks)
    while len(text) > max_chars:
        # 从最后一页尾部丢行（len(b) > 2 才允许丢，保住「### 标题 + 至少一行」）
        idx = next((i for i in range(len(blocks) - 1, -1, -1) if len(blocks[i]) > 2), None)
        if idx is None:
            break
        blocks[idx].pop()
        text = _join(blocks)
    return text


def is_stale(profile: Optional[str] = None, hours: float = STALE_HOURS,
             scope: str = "self") -> bool:
    pages = load_pages(profile, scope)
    if not pages:
        return True
    newest = max((p.get("generated_at") or 0) for p in pages.values())
    return (time.time() - newest) > hours * 3600


_REBUILD_LOCK = threading.Lock()
_LAST_REBUILD = [0.0]


def refresh_async(profile: Optional[str] = None, force: bool = False) -> bool:
    """后台重建（不阻塞调用方）。已是最新且非 force → 直接返回 False。"""
    if not enabled():
        return False
    if not force and not is_stale(profile):
        return False
    if time.time() - _LAST_REBUILD[0] < REBUILD_MIN_GAP:
        return False
    if not _REBUILD_LOCK.acquire(blocking=False):
        return False

    def _run():
        try:
            rebuild(profile)
        except Exception as e:  # noqa: BLE001
            logger.warning("mental_model 后台重建失败：%s", e)
        finally:
            _LAST_REBUILD[0] = time.time()
            _REBUILD_LOCK.release()

    threading.Thread(target=_run, name="boshi-mental-model", daemon=True).start()
    return True
