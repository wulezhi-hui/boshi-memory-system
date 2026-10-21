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


def _page_open_threads(cur, prof: str = None, scope: str = "self") -> List[Dict[str, Any]]:
    """未了事项：槽位 open_questions 里近 21 天的条目（每条取第一个问题）。"""
    pc, pp = _prof_clause(scope, prof)
    rows = _rows(cur, """
        SELECT id::text, slots->'open_questions' AS q FROM boshi.memories
         WHERE %sis_latest
           AND slots->'open_questions' <> '[]'::jsonb
           AND created_at > now() - interval '21 days'
         ORDER BY created_at DESC NULLS LAST LIMIT 25""" % pc, pp)
    items = []
    for i, q in rows:
        if isinstance(q, str):
            try:
                q = json.loads(q)
            except Exception:  # noqa: BLE001
                continue
        if not isinstance(q, list):
            continue
        for one in q[:1]:
            one = re.sub(r"^[-*\s]+", "", str(one))       # 槽位里带过 "- " / "**" 开头
            if _worth(one, 10):
                items.append({"text": one, "evidence": [i]})
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
        cur.execute("""SELECT id::text FROM boshi.memories
                        WHERE profile IS NOT DISTINCT FROM %s AND topic = %s
                          AND meta->>'page_key' = %s
                          AND COALESCE(meta->>'scope', 'self') = %s
                          AND is_latest""",
                    (prof, MM_TOPIC, page["key"], meta["scope"]))
        old = [r[0] for r in cur.fetchall()]
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
