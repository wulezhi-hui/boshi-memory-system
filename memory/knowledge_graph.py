"""
伯仕知识图谱模块 v1.0 — Succmemory 借鉴
==========================================
关系类型：
  updates  — 版本替代（新事实覆盖旧事实）
  extends  — 补充细节（新记忆补充旧记忆的更多信息）
  derives  — 推理产生（从多条记忆推导出的新结论）
  related  — 一般关联（两个实体/记忆之间存在关联但关系不明确）

边存储：存在 ChromaDB 中（type=relation），节点通过 id 引用
版本链追踪：通过 _parent_id → _superseded_by 链追溯

用法：
    from knowledge_graph import add_relation, get_relations, trace_version_chain, merge_duplicates
"""

import time
import uuid
import logging
from typing import List, Optional, Set

logger = logging.getLogger(__name__)

# 关系边去重缓存（模块级，2026-08-19 新增）
# auto_link_entities 用它做 O(1) 去重，避免每次全量扫描 ChromaDB
_EDGE_CACHE = None

# 常见英文词（大写形式出现时不应作为实体，2026-08-19 新增）
_COMMON_EN_WORDS = {
    "the", "and", "for", "with", "from", "this", "that", "have", "has", "was",
    "were", "will", "would", "can", "could", "should", "about", "into", "than",
    "then", "them", "they", "their", "there", "these", "those", "which", "while",
    "after", "before", "during", "without", "within", "across", "between",
    "over", "under", "again", "further", "when", "where", "why", "how", "what",
    "who", "whom", "whose", "your", "yours", "you", "yourself", "him", "her",
    "his", "its", "our", "ours", "their", "theirs", "my", "mine", "all", "any",
    "both", "each", "few", "more", "most", "other", "some", "such", "only",
    "own", "same", "very", "just", "also", "too", "not", "no", "nor", "yes",
    "well", "now", "here", "still", "already", "always", "never", "often",
    "sometimes", "usually", "being", "been", "did", "does", "doing", "done",
    "make", "made", "use", "used", "using", "get", "got", "gotten", "like",
    "note", "see", "new", "old", "high", "low", "large", "small", "good", "bad",
    "first", "last", "next", "part", "user", "data", "file", "full", "step",
    "need", "make", "one", "two", "three", "run", "running", "show", "shown",
}

# 需要保留的纯大写缩写（技术术语，2026-08-19 新增）
_KEEP_ACRONYMS = {
    "CPU", "GPU", "RAM", "ROM", "API", "URL", "HTTP", "HTTPS", "SSH", "SQL",
    "JSON", "XML", "YAML", "TOML", "PDF", "PNG", "JPEG", "GIF", "MP4", "WAV",
    "MP3", "AI", "ML", "DL", "NLP", "CV", "VR", "AR", "OS", "PC", "UI", "UX",
    "CLI", "TUI", "GUI", "IDE", "DB", "SDK", "CLI", "MCP", "RAG", "LLM", "GGUF",
    "INT8", "FP16", "FP32", "BF16", "FP8", "INT4", "CUDA", "NVLink", "HDR",
    "FPS", "IT", "IOT", "CDN", "DNS", "TCP", "UDP", "IP", "LAN", "WAN",
}


# ── 核心关系操作 ──────────────────────────────────────

def add_relation(
    from_id: str,
    to_id: str,
    rel_type: str,
    reason: str = "",
    confidence: float = 1.0,
    metadata: dict = None,
) -> str:
    """
    在图谱中添加一条关系边。
    参数：
        from_id: 源记忆 ID
        to_id: 目标记忆 ID
        rel_type: 关系类型（updates / extends / derives / related）
        reason: 关系产生原因
        confidence: 置信度（0~1）
        metadata: 额外的元数据
    返回：
        新关系边的 ID
    """
    try:
        from chroma_bridge import add_memory, search_memory

        if rel_type not in ("updates", "extends", "derives", "related"):
            logger.warning(f"未知关系类型 {rel_type}，降级为 related")
            rel_type = "related"

        # 去重：同一条边不重复写入（基于精确 metadata 匹配，2026-08-19 修复）
        # 原实现用语义搜索 dedup_key 判断，会误匹配原始记忆文本导致新边被跳过。
        dedup_key = f"{from_id}---{rel_type}---{to_id}"
        try:
            from chroma_bridge import get_all_relations
            existing_edges = get_all_relations()
            for e in existing_edges:
                em = e.get("metadata", {})
                if (em.get("from_id") == from_id and em.get("to_id") == to_id
                        and em.get("rel_type") == rel_type):
                    return e.get("id", "")
        except (ImportError, AttributeError):
            # fallback：语义搜索（旧逻辑，可能有误判但保底）
            existing = search_memory(dedup_key, top_k=1, where={"type": "relation"})
            if existing and existing[0].get("score", 0) < 0.3:
                return existing[0]["id"]

        edge_id = str(uuid.uuid4())
        now = time.time()

        edge_meta = {
            "type": "relation",
            "rel_type": rel_type,
            "from_id": from_id,
            "to_id": to_id,
            "reason": reason[:500] if reason else "",
            "confidence": confidence,
            "tier": "warm",
            "heat": 20.0,
            "created_at": now,
        }
        if metadata:
            edge_meta.update(metadata)

        add_memory(
            content=f"{from_id} --[{rel_type}]--> {to_id} [{reason[:100] if reason else ''}]",
            metadata=edge_meta,
            memory_id=edge_id,
        )
        return edge_id
    except ImportError:
        return ""
    except Exception as e:
        logger.warning(f"add_relation 失败: {e}")
        return ""


def get_relations(
    memory_id: str = None,
    rel_type: str = None,
    top_k: int = 20,
) -> List[dict]:
    """
    查询与某条记忆相关的所有关系边。
    参数：
        memory_id: 记忆 ID（查该记忆作为 from 或 to 的所有边）
        rel_type: 过滤关系类型
        top_k: 返回条数
    返回：
        [{id, content, metadata, score}, ...]
    """
    try:
        from chroma_bridge import search_memory

        where = {"type": "relation"}
        if rel_type:
            where = {"$and": [where, {"rel_type": rel_type}]}

        if memory_id:
            # 搜索包含此 ID 的所有关系边
            results = search_memory(memory_id, top_k=top_k, where=where)
        else:
            results = search_memory("relation edge", top_k=top_k, where=where)

        return results
    except ImportError:
        return []
    except Exception as e:
        logger.warning(f"get_relations 失败: {e}")
        return []


def trace_version_chain(memory_id: str, max_depth: int = 20) -> List[dict]:
    """
    追溯版本链 — 从给定记忆沿 _parent_id / _superseded_by 双向追溯。
    返回完整的版本演变历史。
    参数：
        memory_id: 起始记忆 ID
        max_depth: 最大追溯深度
    返回：
        [{id, content, metadata, direction: "parent"/"child"}, ...]
    """
    try:
        from chroma_bridge import _get_client, _get_embedding_function, COLLECTION_NAME

        client = _get_client()
        ef = _get_embedding_function()
        col = client.get_or_create_collection(COLLECTION_NAME, embedding_function=ef)

        chain = []
        visited = {memory_id}
        current_id = memory_id

        # 向父级追溯
        for _ in range(max_depth):
            data = col.get(ids=[current_id])
            if not data["metadatas"] or not data["metadatas"][0]:
                break
            meta = data["metadatas"][0]
            content = data["documents"][0] if data.get("documents") and data["documents"][0] else ""

            chain.append({
                "id": current_id,
                "content": content[:200],
                "metadata": meta,
                "direction": "child",  # 当前是子节点，往上找父
            })

            # 找父节点
            parent_id = meta.get("_parent_id")
            if parent_id and parent_id not in visited:
                visited.add(parent_id)
                current_id = parent_id
            else:
                break

        chain.reverse()  # 反转，从最旧到最新

        return chain
    except ImportError:
        return []
    except Exception as e:
        logger.warning(f"trace_version_chain 失败: {e}")
        return []


def merge_duplicates(
    query: str = "",
    threshold: float = 0.08,
    top_k: int = 20,
    dry_run: bool = True,
) -> List[dict]:
    """
    合并重复记忆 — 找到内容几乎相同的记忆对（distance < threshold），
    标记旧版本 isLatest=false，保留最新版本。

    参数：
        query: 搜索文本（空则用通用查询）
        threshold: 距离阈值（越低越严格）
        top_k: 扫描条数
        dry_run: True=只统计不修改, False=执行合并
    返回：
        [{winner_id, loser_id, distance, merged}, ...]
    """
    try:
        from chroma_bridge import search_memory, deprecate_memory

        results = []
        memories = search_memory(
            query or "事实 偏好 信息",
            top_k=top_k,
            all_versions=False,
        )

        if len(memories) < 2:
            return []

        seen_pairs = set()
        for i in range(len(memories)):
            for j in range(i + 1, len(memories)):
                id_pair = tuple(sorted([memories[i]["id"], memories[j]["id"]]))
                if id_pair in seen_pairs:
                    continue
                seen_pairs.add(id_pair)

                # 交叉查询判断相似度
                try:
                    cross = search_memory(
                        memories[j]["content"][:200],
                        top_k=1,
                        where={"isLatest": True}, all_versions=False,
                    )
                    if cross and cross[0]["id"] in (memories[i]["id"], memories[j]["id"]):
                        dist = cross[0].get("score", 1.0)
                        if dist < threshold:
                            # 按创建时间决定 winner/loser
                            ti = memories[i].get("metadata", {}).get("created_at", 0)
                            tj = memories[j].get("metadata", {}).get("created_at", 0)
                            try:
                                ti = float(ti) if ti else 0
                                tj = float(tj) if tj else 0
                            except (ValueError, TypeError):
                                ti, tj = 0, 0

                            if ti >= tj:
                                winner_id, loser_id = memories[i]["id"], memories[j]["id"]
                            else:
                                winner_id, loser_id = memories[j]["id"], memories[i]["id"]

                            merged = False
                            if not dry_run:
                                merged = deprecate_memory(loser_id, superseded_by=winner_id)

                            results.append({
                                "winner_id": winner_id,
                                "loser_id": loser_id,
                                "distance": dist,
                                "merged": merged,
                            })
                except Exception:
                    pass

        return results
    except ImportError:
        return []
    except Exception as e:
        logger.warning(f"merge_duplicates 失败: {e}")
        return []


def add_derived_fact(
    source_ids: List[str],
    derived_content: str,
    confidence: float = 0.7,
    reasoning_path: str = "",
) -> Optional[str]:
    """
    添加推理产生的新事实（derives 关系）。
    参数：
        source_ids: 源记忆 ID 列表
        derived_content: 推导出的新事实内容
        confidence: 置信度（0~1，低于 0.6 的建议不写入）
        reasoning_path: 推理过程说明
    返回：
        新事实的 ID，如果 confidence 过低返回 None
    """
    if confidence < 0.6:
        logger.info(f"derive 置信度 {confidence} < 0.6，拒绝写入")
        return None

    try:
        from chroma_bridge import add_memory

        now = time.time()
        new_id = str(uuid.uuid4())

        add_memory(
            content=derived_content,
            metadata={
                "type": "derived",
                "source": "auto_derive",
                "confidence": confidence,
                "source_ids": ",".join(source_ids),
                "reasoning_path": reasoning_path[:500],
                "tier": "warm",
                "heat": 10.0,
                "created_at": now,
            },
            memory_id=new_id,
        )

        # 为每个源记忆添加 derives 关系边
        for src_id in source_ids:
            add_relation(
                from_id=new_id,
                to_id=src_id,
                rel_type="derives",
                reason=f"推理自 {len(source_ids)} 条源记忆, confidence={confidence}",
                confidence=confidence,
            )

        return new_id
    except ImportError:
        return None
    except Exception as e:
        logger.warning(f"add_derived_fact 失败: {e}")
        return None


def get_graph_context(memory_id: str, depth: int = 2) -> str:
    """
    获取一条记忆的图上下文 — 返回它周围的关联记忆。
    用于在查询时丰富上下文。
    参数：
        memory_id: 记忆 ID
        depth: 扩散深度（1=直接关联，2=间接关联）
    返回：
        Markdown 格式的图上下文文本
    """
    try:
        relations = get_relations(memory_id, top_k=20)
        version_chain = trace_version_chain(memory_id, max_depth=5)

        parts = []

        if version_chain and len(version_chain) > 1:
            parts.append("📜 **版本链**：")
            for node in version_chain:
                parts.append(f"  - [{node['direction']}] {node['content'][:80]}")

        if relations:
            parts.append("🔗 **关联记忆**：")
            seen = set()
            for r in relations[:10]:
                meta = r.get("metadata", {})
                rel = meta.get("rel_type", "related")
                from_id = meta.get("from_id", "")
                to_id = meta.get("to_id", "")
                key = f"{from_id}-{to_id}"
                if key in seen:
                    continue
                seen.add(key)
                reason = meta.get("reason", "")
                parts.append(f"  - [{rel}] {reason[:100]}")

        return "\n".join(parts) if parts else ""
    except Exception:
        return ""


# ── 实体提取辅助 ───────────────────────────────────────

KNOWN_ENTITIES = [
    "记忆系统", "Chroma", "ChromaDB", "热区", "温区", "冷区", "全量", "prefetch",
    "sync_turn", "boshi_search", "工作台", "Camofox", "搬运模式",
    "OpenCode", "书库整理", "虚拟寺院", "UE5", "PCG", "AI僧人",
    "工作日志", "项目日志", "知识图谱", "进化", "三省", "画像",
    "Supermemory", "CN Desktop", "deprecate", "user_profile", "hybrid_search",
    "extract_facts", "auto_forget", "detect_conflicts", "version_chain",
    "DeepSeek", "GLM", "Ollama", "hermes", "Hermes Agent",
    "三友", "小乐", "乐之", "伯仕", "Obsidian", "state.db",
]


def extract_entities(text: str) -> Set[str]:
    """从文本中提取已知实体"""
    found = set()
    for entity in KNOWN_ENTITIES:
        if entity in text:
            found.add(entity)
    return found


def auto_link_entities(user_content: str, assistant_content: str = "") -> int:
    """
    自动从对话中提取实体并建立关系边。
    如果发现了 >= 2 个已知实体，自动生成相关关系边。
    返回创建的关系数。

    2026-08-19 增强：
    - 先动态学习新实体（learn_entities）再建边，突破静态 KNOWN_ENTITIES
    - 按上下文判断边类型：related(共同出现) / derives(技术依赖) / extends(扩展)
    - reason 更具体（含原句片段）
    """
    text = (user_content or "") + " " + (assistant_content or "")
    if not text.strip():
        return 0

    # 1) 动态实体学习：先扩展已知实体列表
    try:
        kg = KnowledgeGraph()
        kg.learn_entities(text)
    except Exception:
        pass

    # 2) 提取实体
    entities = extract_entities(text)
    if len(entities) < 2:
        return 0

    # 3) 判断边类型与原因（基于原句中的线索词）
    def _infer_edge(ea: str, eb: str) -> tuple:
        """返回 (rel_type, reason)。"""
        if any(k in text for k in ("依赖", "基于", "使用", "用", "跑在", "装", "通过", "连接")):
            return "derives", f"{ea} 依赖/使用 {eb}"
        if any(k in text for k in ("升级", "更新", "替代", "新版", "v6", "版本")):
            return "updates", f"{ea} 更新了 {eb}"
        if any(k in text for k in ("扩展", "集成", "接入", "添加", "增加", "插件")):
            return "extends", f"{ea} 扩展/集成 {eb}"
        return "related", f"{ea} 与 {eb} 共同出现于对话"

    created = 0
    entity_list = sorted(entities)
    # 去重缓存：模块级集合，一次加载全部关系边，避免每次全量扫描（O(n²)→O(1)）
    global _EDGE_CACHE
    if _EDGE_CACHE is None:
        _EDGE_CACHE = set()
        try:
            from chroma_bridge import get_all_relations
            for _e in get_all_relations():
                _m = _e.get("metadata", {})
                _a, _b = _m.get("entity_a"), _m.get("entity_b")
                if _a and _b:
                    _EDGE_CACHE.add((_a, _b, _m.get("rel_type", "related")))
        except Exception:
            pass
    for i in range(len(entity_list)):
        for j in range(i + 1, len(entity_list)):
            # 检查是否已有关系（基于缓存集合去重，2026-08-19 修复 top_k=50 失效问题）
            rel_type, reason = _infer_edge(entity_list[i], entity_list[j])
            pair_key = (entity_list[i], entity_list[j], rel_type)
            if pair_key in _EDGE_CACHE:
                continue

            rel_id = add_relation(
                from_id=entity_list[i],
                to_id=entity_list[j],
                rel_type=rel_type,
                reason=reason,
                confidence=0.5,
                metadata={
                    "entity_a": entity_list[i],
                    "entity_b": entity_list[j],
                    "entity_type_a": "learned",
                    "entity_type_b": "learned",
                    "source": "auto_extract",
                },
            )
            if rel_id:
                created += 1
                _EDGE_CACHE.add(pair_key)

    return created


# ═══════════════════════════════════════════════════════════════════
# KnowledgeGraph 类（2026-08-19 补齐）：桥接纯函数版，供 boshi_core
# 的 _get_kg() 使用（stats / query / add_node / add_edge）。
# 修复历史遗留：此前该类从未实现，导致 status() 图谱恒为 0。
# ═══════════════════════════════════════════════════════════════════

class KnowledgeGraph:
    """图谱门面：包装纯函数实现，提供 boshi_core 期望的类接口。"""

    def __init__(self, path: str = "", **kwargs):
        self._path = path or ""

    # -- 统计 ------------------------------------------------------
    def stats(self) -> dict:
        """统计图谱节点/边数量（基于 ChromaDB type=relation，2026-08-19 修复低估）。"""
        try:
            from chroma_bridge import get_all_relations
            rels = get_all_relations()
            edges = len(rels or [])
            # 节点 = 从边两端收集实体
            names = set()
            for r in (rels or []):
                meta = r.get("metadata", {}) or {}
                if meta.get("entity_a"):
                    names.add(meta["entity_a"])
                if meta.get("entity_b"):
                    names.add(meta["entity_b"])
            nodes = len(names) if names else len(KNOWN_ENTITIES)
            return {"nodes": nodes, "edges": edges, "path": self._path or "chroma:relation"}
        except Exception:
            return {"nodes": 0, "edges": 0, "path": self._path or "chroma:relation"}

    # -- 查询 ------------------------------------------------------
    def query(self, entity: str, max_depth: int = 2) -> dict:
        """从实体出发做 BFS 遍历（简化：直接返回该实体关联的边）。"""
        try:
            from chroma_bridge import search_memory
            rels = search_memory(entity, top_k=50, where={"type": "relation"})
            nodes = {}
            edges = []
            for r in (rels or []):
                meta = r.get("metadata", {}) or {}
                a, b = meta.get("entity_a"), meta.get("entity_b")
                if not a or not b:
                    continue
                nodes.setdefault(a, {"id": a})
                nodes.setdefault(b, {"id": b})
                edges.append({
                    "from": a, "to": b,
                    "relation": meta.get("rel_type", "related"),
                    "reason": meta.get("reason", "")[:60],
                })
            return {"nodes": nodes, "edges": edges}
        except Exception:
            return {"nodes": {}, "edges": []}

    # -- 写入 ------------------------------------------------------
    def add_node(self, name: str, type: str = "", attr: str = "") -> dict:
        """添加节点（纯函数版以实体名建边为主，节点隐式存在于边中）。"""
        return {"name": name, "type": type, "attr": attr, "added": True}

    def add_edge(self, from_name: str, to_name: str, relation: str) -> dict:
        """添加关系边（委托 add_relation）。"""
        try:
            rid = add_relation(
                from_id=from_name,
                to_id=to_name,
                rel_type=relation if relation in ("updates", "extends", "derives", "related") else "related",
                reason="手动添加",
                confidence=1.0,
                metadata={"entity_a": from_name, "entity_b": to_name, "source": "manual"},
            )
            return {"from": from_name, "to": to_name, "relation": relation, "edge_id": rid, "added": bool(rid)}
        except Exception as e:
            return {"from": from_name, "to": to_name, "relation": relation, "error": str(e), "added": False}

    # -- 检索（图谱召回核心）-------------------------------------
    def search(self, query: str, top_k: int = 5) -> list:
        """按查询词匹配实体，返回实体列表（含关联类型）。

        _graph_search 依赖此方法做图谱召回。匹配策略：
        1. 查询词直接命中实体名 / 实体名包含查询词 / 查询词包含实体名
        2. 命中的实体返回其类型（从边 metadata 或 KNOWN_ENTITIES 推断）
        """
        try:
            from chroma_bridge import search_memory
            # 收集所有 relation 边上的实体
            rels = search_memory("", top_k=10000, where={"type": "relation"})
            entity_map = {}  # name -> type
            for r in (rels or []):
                meta = r.get("metadata", {}) or {}
                a, b = meta.get("entity_a"), meta.get("entity_b")
                if a:
                    entity_map.setdefault(a, meta.get("entity_type_a", "entity"))
                if b:
                    entity_map.setdefault(b, meta.get("entity_type_b", "entity"))

            # 查询词归一化
            q = (query or "").strip().lower()
            if not q:
                # 无查询词时返回最热的实体（前 top_k 个）
                names = list(entity_map.keys())[:top_k]
            else:
                names = []
                for name in entity_map:
                    nl = name.lower()
                    if q in nl or nl in q:
                        names.append(name)
                # 已知实体兜底：KNOWN_ENTITIES 中命中的也算
                for ent in KNOWN_ENTITIES:
                    if ent.lower() in q or q in ent.lower():
                        if ent not in names:
                            names.append(ent)
                names = names[:top_k]

            results = []
            for name in names:
                etype = entity_map.get(name, "entity")
                results.append({"name": name, "type": etype})
            return results
        except Exception:
            return []

    # -- 动态实体学习（第3步）-----------------------------------
    def learn_entities(self, text: str) -> list:
        """从文本中发现新实体并加入 KNOWN_ENTITIES（就地扩展）。

        发现规则：
        1. 中英文专有名词模式（连续大写词/英文驼峰/中文书名号《》）
        2. 已有边上的实体（学过的）
        3. 与 KNOWN_ENTITIES 匹配的部分
        返回新发现的实体名列表。
        """
        import re
        if not text:
            return []
        discovered = set()
        # 英文连续大写词（如 ComfyUI, RTX 2080 Ti, Wan2.2）
        # 注意：中文语境下 \b 不生效（中英之间无 word boundary），
        # 用 (?<![A-Za-z0-9]) 替代，确保紧贴中文的词也能匹配。
        for m in re.finditer(r"(?<![A-Za-z0-9])[A-Z][A-Za-z0-9_.\-]{2,}(?:\s+[A-Za-z0-9_.\-]{2,}){0,2}(?![A-Za-z0-9])", text):
            tok = m.group(0).strip()
            # 过滤纯英文句首/普通词
            if re.fullmatch(r"[A-Z][a-z]{1,3}", tok):
                continue
            # 2026-08-19 噪音过滤：
            # 1) 排除常见英文单词（大写的普通词，非专有名词）
            if tok.lower() in _COMMON_EN_WORDS:
                continue
            # 2) 排除纯大写缩写（CPU/GPU 等已收录的除外；ACP/A3B 这类无意义缩写排除）
            if re.fullmatch(r"[A-Z]{2,6}", tok) and tok not in _KEEP_ACRONYMS:
                continue
            # 3) 排除明显是句子片段的多词组合（含 was/is/are/the/of 等虚词）
            if re.search(r"\s+(was|is|are|the|of|in|on|to|for|and|with|a|an)\s+", tok, re.I):
                continue
            discovered.add(tok)
        # 中文书名号/专名（《xxx》）
        for m in re.finditer(r"《([^》]{2,20})》", text):
            discovered.add(m.group(1))
        # 中文专有名词（含"系统/模型/库/站"等后缀）
        # 要求：后缀词前面至少 2 个中文字，且不能是已有实体的子串
        for m in re.finditer(r"([\u4e00-\u9fa5]{2,6}(?:系统|模型|引擎|工具|库|站|平台|框架|项目|技能|插件))", text):
            cand = m.group(1)
            # 过滤：如果候选是某个已有实体（或已有实体是候选的子串）的子串，跳过
            is_substring = any(e in cand and e != cand for e in KNOWN_ENTITIES)
            if not is_substring:
                discovered.add(cand)
        # 过滤已在列表中的
        new_entities = []
        for e in discovered:
            if e not in KNOWN_ENTITIES and len(e) >= 2:
                # 再次过滤：新实体若是已有实体的子串则丢弃（避免"伯仕记忆系统"被"记忆系统"污染）
                if any(e in known and e != known for known in KNOWN_ENTITIES):
                    continue
                KNOWN_ENTITIES.append(e)
                new_entities.append(e)
        return new_entities
