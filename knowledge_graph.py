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

        # 去重：同一条边不重复写入
        dedup_key = f"{from_id}---{rel_type}---{to_id}"
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
    如果发现了 >= 2 个已知实体，自动生成 related 关系。
    返回创建的关系数。
    """
    text = (user_content or "") + " " + (assistant_content or "")
    entities = extract_entities(text)

    if len(entities) < 2:
        return 0

    created = 0
    entity_list = sorted(entities)
    for i in range(len(entity_list)):
        for j in range(i + 1, len(entity_list)):
            # 检查是否已有关系
            existing = get_relations(
                rel_type=None,
                top_k=3,
            )
            already_exists = False
            for e in existing:
                meta = e.get("metadata", {})
                pair = {meta.get("from_id", ""), meta.get("to_id", "")}
                if entity_list[i] in pair and entity_list[j] in pair:
                    already_exists = True
                    break
            if already_exists:
                continue

            # 用实体名作为 from/to（简化版，不依赖记忆 ID）
            rel_id = add_relation(
                from_id=entity_list[i],
                to_id=entity_list[j],
                rel_type="related",
                reason="共同出现于对话中",
                confidence=0.5,
                metadata={
                    "entity_a": entity_list[i],
                    "entity_b": entity_list[j],
                    "source": "auto_extract",
                },
            )
            if rel_id:
                created += 1

    return created
