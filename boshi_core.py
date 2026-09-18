"""
伯仕记忆系统 Core — MCP Server + CLI 共享底层
=============================================
从 chroma_bridge / knowledge_graph / memory_bridge_api 抽取的通用记忆操作层。
MCP Server 和 CLI 都通过此模块访问记忆，不直接操作 ChromaDB。
"""
import os
import sys
import json
import re
import time
from datetime import datetime, timezone

# ── 路径配置 ──
BOSHI_HOME = os.path.expanduser("~/.boshi")
MEMORY_DIR = os.path.join(BOSHI_HOME, "memory")
CHROMA_DIR = os.path.join(BOSHI_HOME, "chroma_db")
KG_PATH = os.path.join(MEMORY_DIR, "knowledge_graph.json")

# ── 确保导入路径 ──
if MEMORY_DIR not in sys.path:
    sys.path.insert(0, MEMORY_DIR)
if BOSHI_HOME not in sys.path:
    sys.path.insert(0, BOSHI_HOME)


# ═══════════════════════════════════════════
# 惰性导入
# ═══════════════════════════════════════════
_chroma_bridge = None
_knowledge_graph = None


def _get_chroma():
    """获取 chroma_bridge 模块（惰性加载，避免启动时加载 embedding 模型）"""
    global _chroma_bridge
    if _chroma_bridge is None:
        import chroma_bridge
        _chroma_bridge = chroma_bridge
    return _chroma_bridge


def _get_kg():
    """获取知识图谱实例（惰性加载）"""
    global _knowledge_graph
    if _knowledge_graph is None:
        from knowledge_graph import KnowledgeGraph
        _knowledge_graph = KnowledgeGraph(KG_PATH)
    return _knowledge_graph


# ═══════════════════════════════════════════
# 核心 API — MCP Server 和 CLI 的统一接口
# ═══════════════════════════════════════════

def search(query: str, top_k: int = 5, source: str = "all",
           include_graph: bool = False, scope: str = None,
           me: str = None) -> dict:
    """
    多策略检索记忆。

    参数:
        query:  查询文本
        top_k:  返回条数（默认5）
        source: "all"(三路融合) | "vector"(语义) | "hybrid"(混合) | "graph"(图谱)
        include_graph: 是否把知识图谱自动提边（type=relation）计入召回。
            默认 False —— 这类边占全库 85%，混入召回会挤占真记忆的名额
            （实测技术类 query 噪声率 70%）。需要图谱联想时传 True。
        scope:  记忆归属范围。"self" 只读本 profile/agent 的记忆（默认，取自
            ~/.boshi/profiles.json）；"all" 读全库（含其他 profile 与外部 agent）。
        me:     调用方归属标识（如 "opencode"/"dsh"/"dashi"）。显式传入可避免
            依赖进程环境变量，供多客户端共用同一 MCP 进程时使用。

    返回:
        {"query": str, "total": int, "results": [...], "sources": {...}}
    """
    cb = _get_chroma()

    if source == "vector":
        results = cb.search_memory(query, top_k=top_k, include_graph=include_graph,
                                   scope=scope, me=me)
        for r in results:
            r["score"] = round(1.0 - r.get("score", 0), 4)
            r["source"] = "vector"
        return {"query": query, "total": len(results), "results": results, "sources": {"vector": len(results)}}

    elif source == "hybrid":
        result = cb.hybrid_search(query, top_k=top_k, include_graph=include_graph,
                                  scope=scope, me=me)
        mems = result.get("memories", [])
        for r in mems:
            r["source"] = "hybrid"
        return {
            "query": query,
            "total": len(mems),
            "results": mems,
            "sessions": result.get("sessions", []),
            "sources": {"hybrid": len(mems), "sessions": len(result.get("sessions", []))},
        }

    elif source == "graph":
        results = _graph_search(query, top_k=top_k)
        return {"query": query, "total": len(results), "results": results, "sources": {"graph": len(results)}}

    else:
        # 三路融合：hybrid_search(语义+全文) + graph
        hybrid = cb.hybrid_search(query, top_k=top_k, include_graph=include_graph,
                                  scope=scope, me=me)
        vector_results = hybrid.get("memories", [])
        for r in vector_results:
            # ChromaDB 返回的是距离（越小越相似），转为相似度（越大越好）
            r["score"] = round(1.0 - r.get("score", 0), 4)
            r["source"] = "hybrid"

        graph_results = _graph_search(query, top_k=3)

        all_results = vector_results + graph_results
        seen = set()
        deduped = []
        for r in sorted(all_results, key=lambda x: x.get("score", 0), reverse=True):
            key = str(r.get("content", ""))[:50]
            if key not in seen:
                seen.add(key)
                deduped.append(r)

        return {
            "query": query,
            "total": len(deduped[:top_k]),
            "results": deduped[:top_k],
            "sessions": hybrid.get("sessions", []),
            "sources": {
                "hybrid": len(vector_results),
                "graph": len(graph_results),
                "sessions": len(hybrid.get("sessions", [])),
            },
        }


def current_profile() -> str:
    """当前进程的记忆归属标识（BOSHI_PROFILE > HERMES_HOME 推导 > default）"""
    return _get_chroma().resolve_profile()


def save(content: str, topic: str = "external", metadata: dict = None,
         profile: str = None) -> dict:
    """
    存入一条记忆。

    参数:
        content:  记忆内容
        topic:    主题标签
        metadata: 附加元数据
        profile:  记忆归属标识（默认由当前 HERMES_HOME/BOSHI_PROFILE 推导，
                  即写入方所属 profile/agent）

    返回:
        {"success": True, "memory_id": str, "content_preview": str}
    """
    cb = _get_chroma()
    meta = metadata or {}
    meta.setdefault("source", "boshi_api")
    meta.setdefault("topic", topic)
    meta.setdefault("timestamp", datetime.now(timezone.utc).isoformat())
    if profile:
        meta["profile"] = profile
    # profile 未显式给出时，由 chroma_bridge.add_memory 按当前进程归属打标

    memory_id = cb.add_memory(content=content, metadata=meta)

    # 知识图谱联动（2026-08-19 接线）：自动提取实体并建立关系边。
    # 失败不阻断主流程（图谱只是增强层）。
    try:
        from knowledge_graph import auto_link_entities
        try:
            auto_link_entities(content)
        except Exception:
            pass  # 图谱写入失败不影响记忆保存
    except ImportError:
        pass

    return {
        "success": True,
        "memory_id": memory_id,
        "content_preview": content[:80],
    }


def delete(memory_id: str) -> dict:
    """删除一条记忆。返回 {"success": True, "deleted": memory_id}"""
    cb = _get_chroma()
    cb.delete_memory(memory_id)
    return {"success": True, "deleted": memory_id}


def status() -> dict:
    """
    记忆库状态总览。

    返回:
        {"service": str, "total_memories": int, "knowledge_graph": {...}, "chroma_dir": str}
    """
    cb = _get_chroma()
    total = cb.get_total_count()
    kg_info = {"nodes": 0, "edges": 0}
    try:
        kg = _get_kg()
        kg_info = kg.stats()
    except Exception:
        pass
    return {
        "service": "boshi-memory",
        "version": "6.0",
        "total_memories": total,
        "knowledge_graph": kg_info,
        "chroma_dir": CHROMA_DIR,
    }


def profile(scope: str = None, me: str = None) -> dict:
    """
    获取用户画像摘要（热区话题 + 最近记忆）。

    参数:
        scope: 记忆归属范围 self/all（默认取 profiles.json 配置）
        me:    调用方归属标识（显式传入，避免依赖进程环境变量）

    返回:
        {"hot_topic": str, "total_memories": int, "recent_memories": [...]}
    """
    cb = _get_chroma()
    total = cb.get_total_count()

    # 热区话题
    hot_topic = ""
    try:
        hot = cb.search_memory("", top_k=1, where={"heat": {"$gte": 10.0}},
                               scope=scope, me=me)
        if hot:
            hot_topic = hot[0].get("content", "")[:60]
    except Exception:
        pass

    recent_memories = []
    try:
        for r in cb.search_memory(hot_topic or "记忆", top_k=3, scope=scope, me=me):
            recent_memories.append({
                "content": r.get("content", "")[:100],
                "topic": r.get("metadata", {}).get("topic", ""),
            })
    except Exception:
        pass

    return {
        "hot_topic": hot_topic,
        "total_memories": total,
        "recent_memories": recent_memories,
    }


def graph_query(entity: str, max_depth: int = 2) -> dict:
    """
    知识图谱查询：从指定实体出发 BFS 遍历。

    参数:
        entity:    起始实体名
        max_depth: 最大遍历深度（默认2）

    返回:
        {"entity": str, "nodes": {...}, "edges": [...]}
    """
    kg = _get_kg()
    result = kg.query(entity, max_depth=max_depth)
    result["entity"] = entity
    return result


def graph_add_node(name: str, type: str = "", attr: str = "") -> dict:
    """添加知识图谱节点。返回节点信息。"""
    kg = _get_kg()
    node = kg.add_node(name, type=type, attr=attr)
    return node if isinstance(node, dict) else {"name": name, "added": True}


def graph_add_edge(from_name: str, to_name: str, relation: str) -> dict:
    """添加知识图谱关系边。返回边信息。"""
    kg = _get_kg()
    edge = kg.add_edge(from_name, to_name, relation)
    return edge if isinstance(edge, dict) else {"from": from_name, "to": to_name, "relation": relation, "added": True}


def brief() -> dict:
    """
    获取会话简报（等价于 memory_bridge_api 的 /memory/brief）。

    返回:
        {"hot_topic": str, "total_memories": int, "recent_memories": [...]}
    """
    return profile()


def recent(n: int = 10, scope: str = None, me: str = None) -> list:
    """获取最近 n 条记忆（scope: self 只读本 profile/agent / all 读全库）。"""
    cb = _get_chroma()
    return cb.get_recent(n, scope=scope, me=me)


def time_range(since: float, until: float = None, top_k: int = 50,
               include_graph: bool = False, scope: str = None,
               me: str = None) -> list:
    """
    按时间范围查询记忆。

    参数:
        since:  起始时间（Unix 时间戳秒）
        until:  结束时间（Unix 时间戳秒，默认 None = 至今）
        top_k:  最多返回条数
        include_graph: 是否包含知识图谱自动提边（type=relation，默认 False）
        scope:  记忆归属范围 self/all（默认取 profiles.json 配置）
        me:     调用方归属标识（显式传入，避免依赖进程环境变量）

    返回:
        [{"id": str, "content": str, "metadata": dict}, ...]
    """
    cb = _get_chroma()
    return cb.get_time_range(since=since, until=until, top_k=top_k,
                             include_graph=include_graph, scope=scope, me=me)


# ═══════════════════════════════════════════
# 内部辅助
# ═══════════════════════════════════════════

def _tokenize(text: str) -> set:
    """中英文分词（BM25 近似）"""
    chinese = set(re.findall(r'[\u4e00-\u9fff]{2,}', text))
    english = set(re.findall(r'[a-zA-Z][a-zA-Z0-9#+._-]{1,}', text.lower()))
    return chinese | english


def _graph_search(query: str, top_k: int = 3) -> list:
    """知识图谱检索"""
    try:
        kg = _get_kg()
        results = []
        entities = kg.search(query)
        for e in entities:
            results.append({
                "id": f"kg_{e['name']}",
                "content": f"实体: {e['name']} ({e.get('type', '')})",
                "source": "graph",
                "score": 0.7,
            })
            subgraph = kg.query(e["name"], max_depth=1)
            for ename, enode in subgraph.get("nodes", {}).items():
                if ename != e["name"]:
                    results.append({
                        "id": f"kg_{ename}",
                        "content": f"关联: {ename} ({enode.get('type', '')})",
                        "source": "graph",
                        "score": 0.5,
                    })
        return results[:top_k]
    except Exception:
        return []