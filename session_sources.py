"""
会话源适配器 — 伯仕记忆系统多 Agent 兼容层
==========================================
自动探测本机已接入的 Agent 会话记忆体，为每个可用源提供统一的检索接口。
哪个 Agent 接入，就自动启用对应 Agent 的会话检索方式。

当前支持的源:
  hermes : %LOCALAPPDATA%/hermes/state.db        (SQLite LIKE 全文检索)
  dsh    : ~/.dsh/sessions/**/session.jsonl.zstd (zstd JSONL → 内存检索)

设计原则:
  - 任何源不可用时自动跳过，不影响其他源
  - 所有源返回统一格式:
    {session_id, source, role, snippet, timestamp, title}
  - 新增 Agent 时: 继承 BaseSessionSource 实现 search/is_available 即可

用法:
  from session_sources import get_active_sources
  for src in get_active_sources():
      for hit in src.search("关键词", limit=5):
          print(hit)
"""

import os
import json
import sqlite3
import time
from pathlib import Path

HOME = Path.home()
LOCALAPPDATA = Path(os.environ.get("LOCALAPPDATA", str(HOME / "AppData" / "Local")))

# ── 各 Agent 记忆体路径 ──────────────────────────────
HERMES_STATE_DB = LOCALAPPDATA / "hermes" / "state.db"
DSH_SESSIONS_DIR = HOME / ".dsh" / "sessions"

# ── 内部缓存 ──────────────────────────────────────────
_session_text_cache = None  # {session_id: [(role, text, ts), ...]}


def _text_from_blocks(blocks):
    """从消息块列表提取纯文本：[{type:'text', text:...}, ...]"""
    if blocks is None:
        return ""
    if not isinstance(blocks, list):
        return str(blocks)
    parts = []
    for b in blocks:
        if isinstance(b, dict):
            if b.get("type") == "text":
                parts.append(str(b.get("text", "")))
            else:
                t = b.get("text")
                if t:
                    parts.append(str(t))
        else:
            parts.append(str(b))
    return "".join(parts)


class BaseSessionSource:
    """会话源基类：新增 Agent 时继承并实现 search/is_available"""

    name = "base"
    label = "Base"

    def is_available(self) -> bool:
        return False

    def search(self, query: str, limit: int = 5) -> list:
        return []

    def status(self) -> dict:
        return {"name": self.name, "label": self.label, "available": self.is_available()}


class HermesSessionSource(BaseSessionSource):
    """Hermes Agent 会话源：%LOCALAPPDATA%/hermes/state.db（SQLite LIKE）"""

    name = "hermes"
    label = "Hermes Agent"

    def is_available(self) -> bool:
        return HERMES_STATE_DB.exists()

    def search(self, query: str, limit: int = 5) -> list:
        if not self.is_available():
            return []
        try:
            db = sqlite3.connect(str(HERMES_STATE_DB))
            db.text_factory = str
            cutoff = time.time() - 2592000  # 最近 30 天
            q = query.replace("%", "\\%").replace("_", "\\_")
            rows = db.execute(
                """SELECT m.session_id, m.content, m.role, m.timestamp,
                          s.source, s.title
                   FROM messages m
                   JOIN sessions s ON m.session_id = s.id
                   WHERE m.content LIKE ?
                     AND m.role IN ('user', 'assistant')
                     AND m.timestamp > ?
                     AND s.message_count >= 2
                   ORDER BY m.timestamp DESC
                   LIMIT ?""",
                (f"%{q}%", cutoff, limit),
            ).fetchall()
            db.close()
            return [
                {
                    "session_id": r[0],
                    "source": "hermes",
                    "role": r[2],
                    "snippet": str(r[1])[:200] if r[1] else "",
                    "timestamp": r[3],
                    "title": r[5] or "",
                }
                for r in rows
            ]
        except Exception:
            return []


class DshSessionSource(BaseSessionSource):
    """DeepSeek Harness 会话源：~/.dsh/sessions/**/session.jsonl.zstd"""

    name = "dsh"
    label = "DeepSeek Harness"

    def is_available(self) -> bool:
        try:
            return DSH_SESSIONS_DIR.exists() and any(
                DSH_SESSIONS_DIR.rglob("session.jsonl.zstd")
            )
        except Exception:
            return False

    def _load_session_texts(self) -> dict:
        """解压并解析所有 DSH 会话 → {session_id: [(role, text, ts), ...]}（进程内缓存）"""
        global _session_text_cache
        if _session_text_cache is not None:
            return _session_text_cache
        import zstandard as zstd

        result = {}
        try:
            files = list(DSH_SESSIONS_DIR.rglob("session.jsonl.zstd"))
        except Exception:
            files = []
        dctx = zstd.ZstdDecompressor()
        for f in files:
            session_id = f.parent.name
            try:
                with open(f, "rb") as fh:
                    with dctx.stream_reader(fh) as reader:
                        data = reader.read()
                texts = []
                for line in data.decode("utf-8", errors="replace").splitlines():
                    try:
                        obj = json.loads(line)
                    except Exception:
                        continue
                    t = obj.get("type")
                    ts = obj.get("time")
                    d = obj.get("data") or {}
                    if t == "user/message":
                        texts.append(("user", _text_from_blocks(d.get("content")), ts))
                    elif t == "assistant/message":
                        msg = d.get("message") or {}
                        texts.append(("assistant", _text_from_blocks(msg.get("content")), ts))
                result[session_id] = texts
            except Exception:
                continue
        _session_text_cache = result
        return result

    def search(self, query: str, limit: int = 5) -> list:
        try:
            sessions = self._load_session_texts()
        except Exception:
            return []
        q = query.lower()
        hits = []
        for sid, msgs in sessions.items():
            for role, text, ts in msgs:
                if q in text.lower():
                    hits.append(
                        {
                            "session_id": sid,
                            "source": "dsh",
                            "role": role,
                            "snippet": text[:200],
                            "timestamp": ts,
                            "title": sid,
                        }
                    )
        hits.sort(key=lambda x: x.get("timestamp") or 0, reverse=True)
        return hits[:limit]


# ── 源注册表：新增 Agent 时在此追加 ────────────────────
_SOURCES = [HermesSessionSource(), DshSessionSource()]


def get_active_sources() -> list:
    """自动探测并返回所有可用的会话源（不可用的自动跳过）"""
    return [s for s in _SOURCES if s.is_available()]


def all_sources_status() -> list:
    """返回所有注册源的可用状态（用于 status/brief 展示）"""
    return [s.status() for s in _SOURCES]


if __name__ == "__main__":
    print("🦄 伯仕会话源探测")
    for s in _SOURCES:
        print(f"  [{s.name}] {s.label}: {'✅ 可用' if s.is_available() else '❌ 不可用'}")
    print()
    for s in get_active_sources():
        hits = s.search("伯仕", limit=3)
        print(f"--- {s.label} 检索'伯仕' ({len(hits)} 条) ---")
        for h in hits[:3]:
            print(f"  [{h['role']}] {h['snippet'][:60]}")
