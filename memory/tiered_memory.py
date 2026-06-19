#!/usr/bin/env python3
"""
伯仕分层记忆系统 (BoShi Tiered Memory) v3.0 🦄
============================================
四层架构：
  一层 🔥 热区 — 当前专注的事（自动加载到上下文）
  二层 🌡️ 温区 — 有关联的过去（语义搜索快速召回）
  三层 ❄️ 冷区 — 时间久远压缩封存（多轮挖掘唤醒）
  四层 🕉️ 全量记录 — state.db 原始会话（事无巨细全保存）

核心改进：
  - 热度自动追踪（最近提及频率 + 时间衰减）
  - 热区话题自动升级/降级
  - 记忆关联标记（引用关系链）
  - 冷区压缩归档 + 深度挖掘模式
"""

import json
import os
import sys
import uuid
import time
import logging
import sqlite3
from datetime import datetime, timezone
import numpy as np

logger = logging.getLogger("伯仕记忆")

# ========== 配置 ==========

BOSHI_HOME = os.path.expanduser("~/.boshi")
MEMORY_DIR = os.path.join(BOSHI_HOME, "memory")
STATE_DB = os.path.join(
    os.environ.get('LOCALAPPDATA', r'C:\Users\Administrator\AppData\Local'),
    'hermes', 'state.db'
)

# 导入 ChromaDB 桥接模块（2026-05-31：HNSW 索引已重建修复 ✅）
sys.path.insert(0, BOSHI_HOME)
try:
    import chroma_bridge
except Exception:
    chroma_bridge = None

# 文件路径
HOT_FILE = os.path.join(MEMORY_DIR, "hot.json")         # 热区话题
WARM_FILE = os.path.join(MEMORY_DIR, "warm.json")        # 温区记忆（含向量）
COLD_FILE = os.path.join(MEMORY_DIR, "cold.json")        # 冷区索引
VECTORS_FILE = os.path.join(MEMORY_DIR, "vectors.npy")   # 温区向量
LIVE_TURNS_FILE = os.path.join(MEMORY_DIR, "live_turns.json")  # 实时会话轮次快照

# 热度参数
HEAT_DECAY_HOURS = 12          # 热度半衰期（小时）— 从6→12，会话期间不轻易冷掉
HEAT_BOOST_PER_MENTION = 20    # 每次提及热度增量 — 从15→20，同一话题更快燃烧
HEAT_HOT_THRESHOLD = 30        # 超过此值视为热区燃烧态
HEAT_WARM_THRESHOLD = 10       # 超过此值视为热区余温态
HEAT_COLD_THRESHOLD = 3        # 低于此值且超过7天转为冷区

# 默认配置
DEFAULT_CONFIG = {
    "embed_model": "qwen3-embedding:4b-q4_K_M",
    "ollama_url": "http://localhost:11434",
    "top_k": 5,
    "similarity_threshold": 0.6,
    "heat_decay_hours": HEAT_DECAY_HOURS,
    "hot_threshold": HEAT_HOT_THRESHOLD,
    "warm_threshold": HEAT_WARM_THRESHOLD,
    "cold_threshold": HEAT_COLD_THRESHOLD,
}


# ========== 话题模型 ==========

class Topic:
    """一个话题：热区/温区的最小单位"""

    def __init__(self, name: str, category: str = ""):
        self.id = str(uuid.uuid4())[:8]
        self.name = name                # 话题名（如"工作台改造"）
        self.category = category        # 分类标签
        self.heat = 0.0                 # 当前热度值
        self.tier = "warm"              # hot / warm / cold
        self.created_at = datetime.now(timezone.utc).isoformat()
        self.last_mentioned = time.time()
        self.mention_count = 0
        self.session_ids = []           # 关联的会话ID
        self.summary = ""               # 最新摘要
        self.status = "active"          # active / paused / done / archived
        self.linked_topics = []         # 关联话题ID列表
        self.tags = []                  # 标签

    def to_dict(self):
        return {
            "id": self.id,
            "name": self.name,
            "category": self.category,
            "heat": self.heat,
            "tier": self.tier,
            "created_at": self.created_at,
            "last_mentioned": self.last_mentioned,
            "mention_count": self.mention_count,
            "session_ids": self.session_ids[-20:],  # 只保留最近20个
            "summary": self.summary,
            "status": self.status,
            "linked_topics": self.linked_topics,
            "tags": self.tags,
        }

    @classmethod
    def from_dict(cls, d):
        t = cls(d.get("name", ""), d.get("category", ""))
        t.id = d.get("id", t.id)
        t.heat = d.get("heat", 0.0)
        t.tier = d.get("tier", "warm")
        t.created_at = d.get("created_at", t.created_at)
        t.last_mentioned = d.get("last_mentioned", time.time())
        t.mention_count = d.get("mention_count", 0)
        t.session_ids = d.get("session_ids", [])
        t.summary = d.get("summary", "")
        t.status = d.get("status", "active")
        t.linked_topics = d.get("linked_topics", [])
        t.tags = d.get("tags", [])
        return t

    def mention(self, session_id: str = ""):
        """被提及一次，增加热度"""
        now = time.time()
        self.heat += HEAT_BOOST_PER_MENTION
        self.last_mentioned = now
        self.mention_count += 1
        if session_id and session_id not in self.session_ids:
            self.session_ids.append(session_id)

    def decay(self, hours: float = HEAT_DECAY_HOURS):
        """按半衰期衰减热度"""
        elapsed = (time.time() - self.last_mentioned) / 3600
        if elapsed > 0:
            factor = 0.5 ** (elapsed / hours)
            self.heat *= factor

    def get_status_label(self):
        """获取热力状态描述"""
        if self.heat >= HEAT_HOT_THRESHOLD:
            return "🔥 燃烧态"
        elif self.heat >= HEAT_WARM_THRESHOLD:
            return "🔥 余温态"
        elif self.heat >= HEAT_COLD_THRESHOLD:
            return "🔥 休眠态"
        elif self.tier == "cold":
            return "❄️ 冷区"
        else:
            return "🌡️ 温区"


# ========== 热区管理器 ==========

class HotManager:
    """管理热区话题：创建、热度衰减、持久化"""

    def __init__(self):
        self.topics: dict[str, Topic] = {}
        self._hot_loaded = False

    def _load_hot(self):
        if self._hot_loaded:
            return
        # 从 hot.json 加载（ChromaDB 已停用，索引损坏需修复）
        if os.path.exists(HOT_FILE):
            try:
                with open(HOT_FILE, encoding='utf-8') as f:
                    data = json.load(f)
                self.topics = {}
                for td in data.get("topics", []):
                    t = Topic.from_dict(td)
                    self.topics[t.id] = t
            except Exception as e:
                logger.warning(f"热区加载失败: {e}")
                self.topics = {}
        else:
            logger.warning("hot.json 不存在，热区为空")
            self.topics = {}
        self._hot_loaded = True

    def _save_hot(self):
        """Write topics to hot.json (ChromaDB 已停用，索引损坏需修复)."""
        try:
            data = {
                "version": 3,
                "updated_at": datetime.now().isoformat(),
                "topics": [t.to_dict() for t in self.topics.values()],
            }
            with open(HOT_FILE, 'w', encoding='utf-8') as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.warning(f"hot.json 保存失败: {e}")

    def _decay_all(self, heat_decay_hours=HEAT_DECAY_HOURS):
        for t in self.topics.values():
            t.decay(heat_decay_hours)

    def _reclassify_topics(self):
        now = time.time()
        for t in self.topics.values():
            if t.heat >= HEAT_HOT_THRESHOLD:
                t.tier = "hot"
            elif t.heat >= HEAT_WARM_THRESHOLD:
                t.tier = "hot"
            elif t.heat >= HEAT_COLD_THRESHOLD:
                days_since = (now - t.last_mentioned) / 86400
                if days_since > 7:
                    t.tier = "cold"
                else:
                    t.tier = "warm"
            else:
                days_since = (now - t.last_mentioned) / 86400
                if days_since > 7:
                    t.tier = "cold"
                else:
                    t.tier = "warm"

    def get_active_topics(self) -> list[Topic]:
        self._load_hot()
        self._decay_all()
        self._reclassify_topics()
        hot = [t for t in self.topics.values() if t.tier == "hot" and t.status != "archived"]
        hot.sort(key=lambda t: t.heat, reverse=True)
        return hot

    def get_warm_topics(self) -> list[Topic]:
        self._load_hot()
        self._decay_all()
        self._reclassify_topics()
        warm = [t for t in self.topics.values() if t.tier == "warm" and t.status != "archived"]
        warm.sort(key=lambda t: t.heat, reverse=True)
        return warm

    def mention_topic(self, topic_name: str, category: str = "",
                      session_id: str = "", summary: str = "") -> Topic:
        self._load_hot()
        topic = None
        for t in self.topics.values():
            if t.name == topic_name:
                topic = t
                break
        if not topic:
            topic = Topic(topic_name, category)
            self.topics[topic.id] = topic
        topic.mention(session_id)
        if summary:
            topic.summary = summary
        self._save_hot()
        return topic

    def detect_topics(self, text: str) -> list[str]:
        self._load_hot()
        detected = []
        text_lower = text.lower()
        for t in self.topics.values():
            if t.name.lower() in text_lower:
                detected.append(t.name)
                continue
            for tag in t.tags:
                if tag.lower() in text_lower:
                    detected.append(t.name)
                    break
        return detected

    def set_topic_status(self, topic_name: str, status: str) -> bool:
        self._load_hot()
        for t in self.topics.values():
            if t.name == topic_name:
                t.status = status
                if status == "done":
                    t.tier = "warm"
                elif status == "archived":
                    self._archive_topic(t.id)
                self._save_hot()
                return True
        return False

    def _archive_topic(self, topic_id: str):
        topic = self.topics.get(topic_id)
        if not topic:
            return
        import chroma_bridge as _cb
        try:
            _cb.add_memory(
                content=f"话题(已归档): {topic.name}\n摘要: {topic.summary}",
                metadata={
                    "type": "cold_topic",
                    "topic_name": topic.name, "category": topic.category,
                    "mention_count": topic.mention_count,
                    "archived_at": datetime.now(timezone.utc).isoformat(),
                    "tags": ",".join(topic.tags) if topic.tags else "",
                },
                memory_id=f"cold_{topic.id}_{int(time.time())}",
            )
        except Exception as e:
            logger.warning(f"ChromaDB archive failed: {e}")
        del self.topics[topic_id]

    def get_hot_summary(self) -> str:
        active = self.get_active_topics()
        if not active:
            return ""
        lines = ["🔥 当前热区话题："]
        for t in active:
            label = t.get_status_label()
            lines.append(f"  {label} {t.name}")
            if t.summary:
                lines.append(f"    摘要: {t.summary[:120]}")
            if t.tags:
                lines.append(f"    标签: {'、'.join(t.tags[:5])}")
        return "\n".join(lines)


# ========== 温区存储器 ==========

class WarmStore:
    """管理温区记忆：向量化存储、语义搜索"""

    def __init__(self):
        self._warm_vectors: np.ndarray | None = None
        self._warm_metadata: list = []
        self._warm_loaded = False

    def _load_warm(self):
        if self._warm_loaded:
            return
        self._warm_metadata = []
        try:
            recent = chroma_bridge.get_recent(1000)
            for r in recent:
                meta = r["metadata"] or {}
                self._warm_metadata.append({
                    "id": r["id"], "content": r["content"],
                    "user_id": meta.get("user_id", "lezhi"),
                    "source": meta.get("source", "conversation"),
                    "topic": meta.get("topic", ""),
                    "created_at": meta.get("created_at", ""),
                    "updated_at": meta.get("updated_at", ""),
                })
        except Exception:
            self._warm_metadata = []
        self._warm_loaded = True

    def _save_warm(self):
        pass  # managed by chroma_bridge

    def _get_embedding(self, text: str) -> list:
        ef = chroma_bridge._get_embedding_function()
        return ef([text])[0].tolist()

    def add_warm(self, content: str, source: str = "conversation",
                 topic_name: str = "", user_id: str = "lezhi") -> dict:
        self._load_warm()
        mem_id = str(uuid.uuid4())[:8]
        now = datetime.now(timezone.utc).isoformat()
        entry = {
            "id": mem_id, "content": content,
            "user_id": user_id, "source": source,
            "topic": topic_name, "created_at": now, "updated_at": now,
        }
        chroma_bridge.add_memory(
            content=content,
            metadata={"user_id": user_id, "source": source, "topic": topic_name,
                      "created_at": now, "updated_at": now},
            memory_id=mem_id,
        )
        self._warm_metadata.append(entry)
        return {"id": mem_id, "content": content}

    def search_warm(self, query: str, user_id: str = None,
                    top_k: int = None, threshold: float = None,
                    topic_name: str = None) -> list:
        if not query:
            return []
        top_k = top_k or 5
        results = chroma_bridge.search_memory(query, top_k=top_k)
        output = []
        for r in results:
            meta = r.get("metadata", {})
            if user_id and meta.get("user_id") not in (None, user_id):
                continue
            if topic_name and meta.get("topic") != topic_name:
                continue
            output.append({
                "id": r["id"], "content": r["content"],
                "score": float(round(1.0 - r["score"], 4)),
                "topic": meta.get("topic", ""),
                "created_at": meta.get("created_at", ""),
                "source": meta.get("source", ""),
            })
            if len(output) >= top_k:
                break
        return output


# ========== 冷区管理器 ==========

class ColdArchive:
    """管理冷区归档：索引搜索、深度挖掘"""

    def __init__(self):
        self._cold_index: list = []
        self._cold_loaded = False

    def _load_cold(self):
        if self._cold_loaded:
            return
        if os.path.exists(COLD_FILE):
            try:
                with open(COLD_FILE, encoding='utf-8') as f:
                    self._cold_index = json.load(f)
            except Exception:
                self._cold_index = []
        self._cold_loaded = True

    def _save_cold(self):
        os.makedirs(MEMORY_DIR, exist_ok=True)
        with open(COLD_FILE, 'w', encoding='utf-8') as f:
            json.dump(self._cold_index, f, ensure_ascii=False, indent=2)

    def search_cold(self, query: str) -> list:
        self._load_cold()
        if not self._cold_index:
            return []
        query_lower = query.lower()
        results = []
        for entry in self._cold_index:
            score = 0
            if query_lower in entry["name"].lower():
                score += 0.8
            for tag in entry.get("tags", []):
                if query_lower in tag.lower():
                    score += 0.5
            if query_lower in entry.get("summary", "").lower():
                score += 0.3
            if score > 0:
                results.append({
                    "name": entry["name"], "summary": entry.get("summary", ""),
                    "session_ids": entry.get("session_ids", []),
                    "archived_at": entry.get("archived_at", ""),
                    "score": round(score, 2),
                })
        results.sort(key=lambda r: r["score"], reverse=True)
        return results[:5]

    def excavate_cold(self, query: str, session_ids: list = None) -> dict:
        if not os.path.exists(STATE_DB):
            return {"status": "error", "message": "无会话数据库"}
        try:
            db = sqlite3.connect(STATE_DB)
            db.text_factory = str
            if session_ids:
                placeholders = ",".join(["?"] * len(session_ids))
                rows = db.execute(
                    f"SELECT s.id, s.title, m.role, m.content "
                    f"FROM sessions s JOIN messages m ON s.id = m.session_id "
                    f"WHERE s.id IN ({placeholders}) "
                    f"ORDER BY m.rowid LIMIT 500", session_ids
                ).fetchall()
            else:
                rows = db.execute(
                    "SELECT DISTINCT s.id, s.title, m.role, m.content "
                    "FROM sessions s JOIN messages m ON s.id = m.session_id "
                    "WHERE m.content LIKE ? "
                    "ORDER BY s.started_at DESC LIMIT 200",
                    (f"%{query}%",)
                ).fetchall()
            db.close()
            sessions_map = {}
            for sid, title, role, content in rows:
                if sid not in sessions_map:
                    sessions_map[sid] = {"title": title, "messages": []}
                sessions_map[sid]["messages"].append({"role": role, "content": content})
            return {"status": "ok", "query": query,
                    "session_count": len(sessions_map), "sessions": sessions_map}
        except Exception as e:
            return {"status": "error", "message": str(e)}


# ========== 分层记忆引擎 ==========

class TieredMemory:
    """四层记忆引擎（组合模式：HotManager + WarmStore + ColdArchive）"""

    def __init__(self, config: dict = None):
        self.config = {**DEFAULT_CONFIG, **(config or {})}
        self.hot = HotManager()
        self.warm = WarmStore()
        self.cold = ColdArchive()
        os.makedirs(MEMORY_DIR, exist_ok=True)

    # -- 热区代理 --
    def _load_hot(self):
        self.hot._load_hot()

    @property
    def topics(self):
        return self.hot.topics

    def get_active_topics(self) -> list[Topic]:
        return self.hot.get_active_topics()

    def get_warm_topics(self) -> list[Topic]:
        return self.hot.get_warm_topics()

    def mention_topic(self, topic_name: str, category: str = "",
                      session_id: str = "", summary: str = "") -> Topic:
        return self.hot.mention_topic(topic_name, category, session_id, summary)

    def detect_topics(self, text: str, session_id: str = "") -> list[str]:
        return self.hot.detect_topics(text)

    def set_topic_status(self, topic_name: str, status: str) -> bool:
        return self.hot.set_topic_status(topic_name, status)

    def get_hot_summary(self) -> str:
        return self.hot.get_hot_summary()

    # -- 温区代理 --
    def _load_warm(self):
        self.warm._load_warm()

    @property
    def _warm_vectors(self):
        return self.warm._warm_vectors

    @_warm_vectors.setter
    def _warm_vectors(self, val):
        self.warm._warm_vectors = val

    @property
    def _warm_metadata(self):
        return self.warm._warm_metadata

    def add_warm(self, content: str, source: str = "conversation",
                 topic_name: str = "", user_id: str = "lezhi") -> dict:
        return self.warm.add_warm(content, source, topic_name, user_id)

    def search_warm(self, query: str, user_id: str = None,
                    top_k: int = None, threshold: float = None,
                    topic_name: str = None) -> list:
        return self.warm.search_warm(query, user_id, top_k, threshold, topic_name)

    def _get_embedding(self, text: str) -> list:
        return self.warm._get_embedding(text)

    # -- 冷区代理 --
    def _load_cold(self):
        self.cold._load_cold()

    @property
    def _cold_index(self):
        return self.cold._cold_index

    def _save_cold(self):
        self.cold._save_cold()

    def search_cold(self, query: str) -> list:
        return self.cold.search_cold(query)

    def excavate_cold(self, query: str, session_ids: list = None) -> dict:
        return self.cold.excavate_cold(query, session_ids)

    # -- 公共接口 --

    def _is_trivial_message(self, text: str) -> bool:
        text = text.strip()
        if len(text) <= 3:
            return True
        trivial_patterns = ["测试", "test", "收到", "不回", "hello", "hi", "在吗",
                            "嗯", "哦", "好", "ok", "好的", "是的", "不", "没",
                            "试试", "再试", "啊", "呀", "哈"]
        for p in trivial_patterns:
            if text.lower().startswith(p.lower()):
                return True
        return False

    def _find_similar_topic(self, search_text: str) -> str:
        self.hot._load_hot()
        if not self.hot.topics:
            return ""
        sorted_topics = sorted(self.hot.topics.values(), key=lambda t: t.heat, reverse=True)
        for t in sorted_topics[:5]:
            if t.name.lower() in search_text.lower():
                return t.name
            words = [w for w in search_text.split() if len(w) >= 2]
            for w in words:
                if w.lower() in t.name.lower():
                    return t.name
        return ""

    def store_conversation(self, user_msg: str, assistant_msg: str,
                           session_id: str = "", source: str = "conversation",
                           user_id: str = "lezhi"):
        user_text = user_msg.strip()
        detected = self.hot.detect_topics(user_msg)
        topic_name = detected[0] if detected else ""
        if not topic_name:
            if self._is_trivial_message(user_text):
                parent = self._find_similar_topic(assistant_msg[:100] if assistant_msg else "")
                if parent:
                    topic_name = parent
                else:
                    self.hot._load_hot()
                    hot_active = [t for t in self.hot.topics.values()
                                  if t.heat >= 10 and t.status == "active"]
                    if hot_active:
                        hot_active.sort(key=lambda t: t.heat, reverse=True)
                        topic_name = hot_active[0].name
                    else:
                        topic_name = ""
            else:
                guess_name = user_text[:30]
                if len(guess_name) >= 4:
                    t = self.hot.mention_topic(guess_name, session_id=session_id)
                    topic_name = t.name
        if topic_name:
            short = f"{user_text[:50]} → {assistant_msg[:100]}"
            self.hot.mention_topic(topic_name, session_id=session_id, summary=short)
        if len(user_text) > 2 and not user_text.startswith("/"):
            self.warm.add_warm(user_text[:500], source=source,
                               topic_name=topic_name, user_id=user_id)
        # 同时存储伯仕的回复，保证双向记忆
        if assistant_msg and len(assistant_msg.strip()) > 5:
            self.warm.add_warm(
                f"伯仕: {assistant_msg.strip()[:200]}",
                source=source,
                topic_name=topic_name,
                user_id=user_id,
            )
        # v5: 非平凡对话后自动提取实体和关系
        if len(user_text) > 10:
            self._auto_extract(f"用户: {user_text[:300]} 伯仕: {assistant_msg[:300]}",
                               source=source)

    def _auto_extract(self, text: str, source: str = "conversation"):
        """
        自动从对话文本中提取实体和关系，存入知识图谱和 Chroma。
        异常不影响主流程（捕获所有异常）。
        """
        try:
            from extractor import extract_facts
            from knowledge_graph import KnowledgeGraph
            
            kg = KnowledgeGraph()
            result = extract_facts(text)
            
            if result.get("entities"):
                for entity in result["entities"]:
                    kg.add_node(
                        entity["name"],
                        type=entity.get("type", ""),
                        attr=entity.get("attr", ""),
                    )
                    # 同时写入 Chroma 作为语义检索补充
                    self.warm.add_warm(
                        f"实体: {entity['name']} ({entity.get('type', '')})",
                        source=source,
                        topic_name="entity_extracted",
                    )
            
            if result.get("relations"):
                for rel in result["relations"]:
                    kg.add_edge(rel["from"], rel["to"], rel["relation"])
            
            if result.get("entities") or result.get("relations"):
                logger.info(
                    f"✅ 自动提取: {len(result['entities'])}实体, "
                    f"{len(result['relations'])}关系"
                )
        
        except Exception as e:
            logger.debug(f"自动提取跳过（非关键）: {e}")

    def push_live_turn(self, user_msg: str, assistant_msg: str,
                       session_id: str = ""):
        if not session_id:
            return
        try:
            turns = {}
            if os.path.exists(LIVE_TURNS_FILE):
                with open(LIVE_TURNS_FILE, encoding='utf-8') as f:
                    turns = json.load(f)
            if session_id not in turns:
                turns[session_id] = []
            turns[session_id].append({
                "user": user_msg[:500], "assistant": assistant_msg[:500],
                "timestamp": time.time(),
            })
            if len(turns[session_id]) > 10:
                turns[session_id] = turns[session_id][-10:]
            cutoff = time.time() - 1800
            turns = {k: v for k, v in turns.items()
                     if v and v[-1]["timestamp"] > cutoff}
            with open(LIVE_TURNS_FILE, 'w', encoding='utf-8') as f:
                json.dump(turns, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.debug(f"push_live_turn失败: {e}")

    def get_live_turns(self, exclude_session_id: str = "",
                       max_recent: int = 3) -> str:
        if not os.path.exists(LIVE_TURNS_FILE):
            return ""
        try:
            with open(LIVE_TURNS_FILE, encoding='utf-8') as f:
                turns = json.load(f)
            parts = []
            for sid, msgs in turns.items():
                if sid == exclude_session_id:
                    continue
                if not msgs:
                    continue
                last = msgs[-1]
                src = "未知"
                if sid.startswith("api-"):
                    src = "工作台"
                elif sid.startswith("cli-") or sid.startswith("local-"):
                    src = "终端"
                elif "weixin" in sid.lower() or "o9cq" in sid.lower():
                    src = "微信"
                parts.append(f"  [{src}] {last['user'][:80]} → {last['assistant'][:100]}")
                if len(parts) >= max_recent:
                    break
            if not parts:
                return ""
            return "📋 其他入口的实时对话：\n" + "\n".join(parts)
        except Exception:
            return ""

    def recover_conversation(self, session_id: str = "",
                             exclude_session_id: str = "",
                             max_turns: int = 15,
                             max_age_minutes: int = 30) -> str:
        if not os.path.exists(STATE_DB):
            return ""
        try:
            db = sqlite3.connect(STATE_DB)
            db.text_factory = str
            if session_id:
                rows = db.execute(
                    "SELECT role, content FROM messages WHERE session_id=? "
                    "ORDER BY rowid ASC LIMIT ?", (session_id, max_turns)
                ).fetchall()
            else:
                cutoff = time.time() - max_age_minutes * 60
                if exclude_session_id:
                    recent = db.execute(
                        "SELECT id, source, started_at, ended_at FROM sessions "
                        "WHERE started_at > ? AND id != ? AND message_count > 1 "
                        "ORDER BY started_at DESC LIMIT 5",
                        (cutoff, exclude_session_id)
                    ).fetchall()
                else:
                    recent = db.execute(
                        "SELECT id, source, started_at, ended_at FROM sessions "
                        "WHERE started_at > ? AND message_count > 1 "
                        "ORDER BY started_at DESC LIMIT 5", (cutoff,)
                    ).fetchall()
                rows = []
                for sid, source, ts, ended in recent:
                    msgs = db.execute(
                        "SELECT role, content FROM messages WHERE session_id=? "
                        "ORDER BY rowid ASC LIMIT ?", (sid, max_turns)
                    ).fetchall()
                    if msgs:
                        rows = msgs
                        break
            db.close()
            if not rows:
                return ""
            dialog = []
            for role, content in rows:
                if not content or not content.strip():
                    continue
                if role == "user":
                    dialog.append(f"👤 用户: {content[:500]}")
                elif role == "assistant":
                    dialog.append(f"🦄 伯仕: {content[:500]}")
                elif role == "tool":
                    trimmed = content[:200] + ("..." if len(content) > 200 else "")
                    dialog.append(f"🔧 工具: {trimmed}")
            if not dialog:
                return ""
            full = "\n".join(dialog)
            return f"📋 之前对话记录（最近{len(rows)}条消息）：\n{full}"
        except Exception as e:
            logger.debug(f"恢复对话失败: {e}")
            return ""

    def recover_conversation_compressed(self, session_id: str = "",
                                        exclude_session_id: str = "",
                                        max_age_minutes: int = 30) -> str:
        """
        结构化对话恢复（压缩模式）— 用热区摘要 + 关键记忆替代原始对话原文。
        大幅减少 token 注入量，同时保留核心信息。
        如果结构化数据不足，fallback 到原文恢复。
        """
        # 第1步：热区摘要（话题名 + summary + 热度）
        hot_summary = self.hot.get_hot_summary()
        structured_parts = []
        if hot_summary:
            structured_parts.append(hot_summary)

        # 第2步：从 state.db 取最近会话的关键事实（Top 3 记忆片段）
        key_facts = []
        try:
            db = sqlite3.connect(STATE_DB)
            db.text_factory = str
            cutoff = time.time() - max_age_minutes * 60
            if exclude_session_id:
                recent = db.execute(
                    "SELECT id FROM sessions WHERE started_at > ? AND id != ? "
                    "AND message_count > 1 ORDER BY started_at DESC LIMIT 3",
                    (cutoff, exclude_session_id)
                ).fetchall()
            else:
                recent = db.execute(
                    "SELECT id FROM sessions WHERE started_at > ? "
                    "AND message_count > 1 ORDER BY started_at DESC LIMIT 3",
                    (cutoff,)
                ).fetchall()
            for (sid,) in recent:
                # 只取 assistant 回复的最后1条（最关键的信息）
                rows = db.execute(
                    "SELECT content FROM messages WHERE session_id=? "
                    "AND role='assistant' ORDER BY rowid DESC LIMIT 1",
                    (sid,)
                ).fetchall()
                for (content,) in rows:
                    if content and len(content.strip()) > 10:
                        key_facts.append(content.strip()[:150])
            db.close()
        except Exception as e:
            logger.debug(f"关键事实提取失败: {e}")

        if key_facts:
            structured_parts.append("📌 上次关键结论：")
            for i, fact in enumerate(key_facts[:3], 1):
                structured_parts.append(f"  {i}. {fact}")

        # 第3步：如果结构化数据充足，返回压缩版本
        if structured_parts:
            return "📋 之前会话（压缩摘要）：\n" + "\n".join(structured_parts)

        # Fallback：结构化数据不足时回退到原文恢复
        return self.recover_conversation(
            exclude_session_id=exclude_session_id,
            max_turns=10,
            max_age_minutes=max_age_minutes,
        )

    def get_context_injection(self, user_msg: str = "",
                              session_id: str = "",
                              recover: bool = False) -> str:
        """结构化上下文注入 — 热区摘要 + 温区搜索 + 图谱关系"""
        parts = []
        # 热区摘要（话题名 + 摘要120字 + 热度）
        hot_summary = self.hot.get_hot_summary()
        if hot_summary:
            parts.append(hot_summary)
        # 温区语义搜索
        if user_msg:
            warm_results = self.warm.search_warm(user_msg, top_k=3, threshold=0.65)
            if warm_results:
                parts.append("🌡️ 相关记忆：")
                for r in warm_results:
                    parts.append(f"  [{r['topic'] or '通用'}] {r['content'][:100]}")
            # 知识图谱关系
            try:
                from knowledge_graph import get_graph_context
                seed_results = self.warm.search_warm(user_msg, top_k=1, threshold=0.5)
                if seed_results:
                    seed_id = seed_results[0].get("id", "")
                    if seed_id:
                        graph_ctx = get_graph_context(seed_id, depth=2)
                        if graph_ctx:
                            parts.append(f"🔗 图谱：\n{graph_ctx}")
            except Exception:
                pass
        return "\n\n".join(parts)

    def stats(self) -> dict:
        self.hot._load_hot()
        self.warm._load_warm()
        self.cold._load_cold()
        hot_count = len([t for t in self.hot.topics.values() if t.tier == "hot"])
        warm_count = len([t for t in self.hot.topics.values() if t.tier == "warm"])
        cold_count = len(self.cold._cold_index)
        return {
            "hot_topics": hot_count, "warm_topics": warm_count,
            "warm_memories": len(self.warm._warm_metadata),
            "cold_archives": cold_count, "total_topics": len(self.hot.topics),
            "memory_dir": MEMORY_DIR,
        }


# ========== 全局实例 ==========

_tiered_store: TieredMemory | None = None

def get_store(config: dict = None) -> TieredMemory:
    global _tiered_store
    if _tiered_store is None:
        _tiered_store = TieredMemory(config)
    return _tiered_store


# ========== CLI 入口 ==========

def main():
    import argparse
    parser = argparse.ArgumentParser(description="伯仕分层记忆系统 v3.0")
    sub = parser.add_subparsers(dest="cmd")

    # mention
    p = sub.add_parser("mention", help="提及话题")
    p.add_argument("name")
    p.add_argument("--category", default="")
    p.add_argument("--summary", default="")
    p.add_argument("--session", default="")

    # status
    p = sub.add_parser("status", help="设置话题状态")
    p.add_argument("name")
    p.add_argument("state", choices=["active", "paused", "done", "archived"])

    # search-warm
    p = sub.add_parser("search", help="搜索温区记忆")
    p.add_argument("query")
    p.add_argument("--user", default="lezhi")
    p.add_argument("--top-k", type=int, default=5)
    p.add_argument("--topic", default="")

    # add-warm
    p = sub.add_parser("add", help="添加记忆")
    p.add_argument("content")
    p.add_argument("--topic", default="")
    p.add_argument("--source", default="manual")

    # hot
    sub.add_parser("hot", help="查看热区话题")

    # warm
    sub.add_parser("warm", help="查看温区话题")

    # cold
    sub.add_parser("cold", help="查看冷区索引")

    # excavate
    p = sub.add_parser("excavate", help="冷区深度挖掘")
    p.add_argument("query")

    # inject
    p = sub.add_parser("inject", help="生成上下文注入")
    p.add_argument("--msg", default="")

    # stats
    sub.add_parser("stats", help="统计信息")

    # migrate — 从旧版 boshi_memory 迁移数据
    sub.add_parser("migrate", help="从旧版记忆迁移到分层系统")

    args = parser.parse_args()
    if not args.cmd:
        parser.print_help()
        sys.exit(1)

    store = get_store()

    if args.cmd == "mention":
        t = store.mention_topic(args.name, args.category, args.session, args.summary)
        print(f"✅ 话题「{t.name}」热度 → {t.heat:.1f} ({t.get_status_label()})")

    elif args.cmd == "status":
        if store.set_topic_status(args.name, args.state):
            print(f"✅ 话题「{args.name}」状态 → {args.state}")
        else:
            print(f"❌ 未找到话题「{args.name}」")

    elif args.cmd == "search":
        results = store.search_warm(args.query, user_id=args.user,
                                    top_k=args.top_k, topic_name=args.topic)
        if results:
            print(f"找到 {len(results)} 条相关记忆：")
            for r in results:
                print(f"  [{r['score']:.0%}] {r['content'][:100]}")
                if r['topic']:
                    print(f"    话题: {r['topic']}")
        else:
            print("未找到相关记忆")

    elif args.cmd == "add":
        result = store.add_warm(args.content, source=args.source, topic_name=args.topic)
        print(f"✅ 已存储 ({result['id']})")

    elif args.cmd == "hot":
        topics = store.get_active_topics()
        if topics:
            print(f"🔥 热区话题（{len(topics)}个）：")
            for t in topics:
                print(f"  {t.get_status_label()} {t.name}")
                print(f"    热度: {t.heat:.1f} | 提及: {t.mention_count}次")
                if t.summary:
                    print(f"    摘要: {t.summary[:120]}")
                if t.tags:
                    print(f"    标签: {'、'.join(t.tags[:5])}")
        else:
            print("热区暂无活跃话题")

    elif args.cmd == "warm":
        topics = store.get_warm_topics()
        if topics:
            print(f"🌡️ 温区话题（{len(topics)}个）：")
            for t in topics:
                print(f"  {t.name} (热度: {t.heat:.1f}, 提及: {t.mention_count}次)")
        else:
            print("温区暂无话题")
        wm = store._warm_metadata if store._warm_loaded else []
        print(f"\n温区记忆条目: {len(wm)}")

    elif args.cmd == "cold":
        store._load_cold()
        if store._cold_index:
            print(f"❄️ 冷区归档（{len(store._cold_index)}个）：")
            for entry in store._cold_index:
                print(f"  {entry['name']} ({entry.get('archived_at', '')[:10]})")
        else:
            print("冷区暂无归档")

    elif args.cmd == "excavate":
        results = store.excavate_cold(args.query)
        if results["status"] == "ok":
            print(f"🔍 深度挖掘「{args.query}」:")
            print(f"  找到 {results['session_count']} 个相关会话")
            for sid, sdata in list(results["sessions"].items())[:3]:
                title = sdata.get("title") or "无标题"
                print(f"\n  会话 {sid[:16]}... ({title})")
                for msg in sdata["messages"][:6]:
                    print(f"    [{msg['role']}] {msg['content'][:80]}")
        else:
            print(f"❌ {results.get('message', '未知错误')}")

    elif args.cmd == "inject":
        ctx = store.get_context_injection(args.msg)
        if ctx:
            print(ctx)
        else:
            print("（无注入内容）")

    elif args.cmd == "stats":
        s = store.stats()
        print("记忆统计:")
        print(f"  热区话题: {s['hot_topics']}")
        print(f"  温区话题: {s['warm_topics']}")
        print(f"  温区记忆: {s['warm_memories']} 条")
        print(f"  冷区归档: {s['cold_archives']} 个")
        print(f"  记忆目录: {s['memory_dir']}")

    elif args.cmd == "migrate":
        old_meta = os.path.join(MEMORY_DIR, "metadata.json")
        if os.path.exists(old_meta):
            with open(old_meta, encoding='utf-8') as f:
                old_data = json.load(f)
            store._load_warm()
            for item in old_data:
                if isinstance(item, dict) and "content" in item:
                    store._warm_metadata.append({
                        "id": item.get("id", str(uuid.uuid4())[:8]),
                        "content": item["content"],
                        "user_id": item.get("user_id", "lezhi"),
                        "source": item.get("source", "migrated"),
                        "topic": "",
                        "created_at": item.get("created_at", ""),
                        "updated_at": item.get("updated_at", ""),
                    })
            # 重建向量
            store._warm_vectors = None
            for entry in store._warm_metadata:
                emb = store._get_embedding(entry["content"])
                if store._warm_vectors is None:
                    store._warm_vectors = np.array([emb])
                else:
                    store._warm_vectors = np.vstack([store._warm_vectors, [emb]])
            store._save_warm()
            print(f"✅ 从旧版迁移了 {len(old_data)} 条记忆到温区")
        else:
            print("未找到旧版记忆文件")


if __name__ == "__main__":
    main()
