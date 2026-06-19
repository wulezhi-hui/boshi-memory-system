#!/usr/bin/env python3
"""
伯仕记忆自动捕获引擎 🦄 v1.0
================================
对标 agentmemory 的 sync_turn + on_session_end + prefetch 三合一体。

功能：
1. 自动捕获：从最近会话中提取实体/关系/事实，存入 ChromaDB + 知识图谱
2. 三路检索：BM25(关键词) + 向量 + 知识图谱融合，返回排序结果
3. 会话简报：生成本轮会话的项目画像摘要（system_prompt_block 等效）

用法：
  python auto_capture.py capture        # 捕获最近一轮对话
  python auto_capture.py prefetch       # 预取相关记忆，输出到 stdout
  python auto_capture.py session-end    # 会话结束处理
"""
import json
import os
import sys
import time
import re
import logging
from datetime import datetime, timezone
from pathlib import Path

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(message)s')
logger = logging.getLogger("auto_capture")

# ── 路径配置 ──
BOSHI_HOME = os.path.expanduser("~/.boshi")
MEMORY_DIR = os.path.join(BOSHI_HOME, "memory")
CHROMA_DIR = os.path.join(BOSHI_HOME, "chroma_db")
sys.path.insert(0, BOSHI_HOME)

from chroma_bridge import add_memory, search_memory, multi_search, count as chroma_count


# ── 配置 ──
KG_PATH = os.path.join(MEMORY_DIR, "knowledge_graph.json")
HOT_FILE = os.path.join(MEMORY_DIR, "hot.json")
LIVE_TURNS_FILE = os.path.join(MEMORY_DIR, "live_turns.json")


# ═══════════════════════════════════════════════════════════
# 模块1：关键词检索（BM25 近似）
# ═══════════════════════════════════════════════════════════
def _tokenize(text: str) -> set:
    """简单中文分词：按非字母数字拆分 + 提取英文单词"""
    # 中文字符
    chinese = set(re.findall(r'[\u4e00-\u9fff]{2,}', text))
    # 英文单词
    english = set(re.findall(r'[a-zA-Z][a-zA-Z0-9#+._-]{1,}', text.lower()))
    return chinese | english


def keyword_search(query: str, top_k: int = 5) -> list:
    """
    BM25 近似：基于 Token 重叠度的关键词匹配。
    对 ChromaDB 中所有文档做词袋匹配，按重叠率排序。
    """
    from chroma_bridge import _get_client, _get_embedding_function, COLLECTION_NAME
    
    client = _get_client()
    ef = _get_embedding_function()
    col = client.get_or_create_collection(COLLECTION_NAME, embedding_function=ef)
    
    if col.count() == 0:
        return []
    
    # 获取全部文档（更高效的方式：按需取）
    all_data = col.get()
    query_tokens = _tokenize(query)
    
    if not query_tokens:
        return []
    
    scored = []
    for i in range(len(all_data["ids"])):
        doc = all_data["documents"][i] if all_data["documents"] else ""
        doc_tokens = _tokenize(doc)
        if not doc_tokens:
            continue
        # Jaccard 相似度
        overlap = len(query_tokens & doc_tokens)
        union = len(query_tokens | doc_tokens)
        if union == 0:
            continue
        score = overlap / union
        if score > 0:
            meta = all_data["metadatas"][i] if all_data["metadatas"] else {}
            scored.append({
                "id": all_data["ids"][i],
                "content": doc,
                "metadata": meta,
                "score": score,
                "source": "keyword",
            })
    
    scored.sort(key=lambda x: x["score"], reverse=True)
    return scored[:top_k]


# ═══════════════════════════════════════════════════════════
# 模块2：自动捕获（sync_turn 等效）
# ═══════════════════════════════════════════════════════════
def _read_live_turns() -> list:
    """读取实时会话轮次快照"""
    if os.path.exists(LIVE_TURNS_FILE):
        try:
            with open(LIVE_TURNS_FILE, encoding='utf-8') as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            return []
    return []


def _read_state_db_recent(session_id: str = None, limit: int = 10) -> list:
    """从 Hermes state.db 读取最近对话轮次"""
    import sqlite3
    
    localappdata = os.environ.get('LOCALAPPDATA', 
        r'C:\Users\Administrator\AppData\Local')
    state_db = os.path.join(localappdata, 'hermes', 'state.db')
    
    if not os.path.exists(state_db):
        return []
    
    try:
        conn = sqlite3.connect(state_db)
        cursor = conn.cursor()
        
        if session_id:
            cursor.execute(
                "SELECT role, content, created_at FROM messages "
                "WHERE session_id = ? ORDER BY created_at DESC LIMIT ?",
                (session_id, limit)
            )
        else:
            cursor.execute(
                "SELECT role, content, created_at FROM messages "
                "ORDER BY created_at DESC LIMIT ?",
                (limit,)
            )
        
        rows = cursor.fetchall()
        conn.close()
        
        turns = []
        for role, content, ts in rows:
            turns.append({
                "role": role,
                "content": content[:500] if content else "",
                "timestamp": ts,
            })
        return turns
    except Exception as e:
        logger.debug(f"读取 state.db 失败: {e}")
        return []


def _extract_facts(text: str) -> dict:
    """调用本机 extractor 提取实体和关系"""
    try:
        from extractor import extract_facts
        return extract_facts(text)
    except ImportError:
        return {"entities": [], "relations": []}


def _update_knowledge_graph(entities: list, relations: list):
    """将提取的实体/关系写入知识图谱"""
    from knowledge_graph import KnowledgeGraph
    kg = KnowledgeGraph(KG_PATH)
    
    for entity in entities:
        name = entity.get("name", "").strip()
        etype = entity.get("type", "")
        attr = entity.get("attr", "")
        if name:
            kg.add_node(name, node_type=etype, description=attr)
    
    for rel in relations:
        from_name = rel.get("from", "").strip()
        to_name = rel.get("to", "").strip()
        rtype = rel.get("relation", "")
        if from_name and to_name and rtype:
            kg.add_edge(from_name, to_name, rtype)
    
    kg._save()


def capture_recent_turns(turns: list = None):
    """
    捕获最近对话轮次中的关键知识。
    流程：取最近N轮 → 提取实体/关系 → 存 ChromaDB + 知识图谱
    """
    if turns is None:
        turns = _read_state_db_recent(limit=10)
    
    if not turns:
        logger.info("没有可捕获的对话")
        return {"captured": 0}
    
    # 拼接对话文本
    conversation_text = "\n".join([
        f"{t['role']}: {t['content'][:300]}"
        for t in turns[:6]
        if t.get('content')
    ])
    
    if len(conversation_text.strip()) < 20:
        return {"captured": 0}
    
    # 提取实体和关系
    extracted = _extract_facts(conversation_text)
    entities = extracted.get("entities", [])
    relations = extracted.get("relations", [])
    
    # 写入知识图谱
    if entities or relations:
        _update_knowledge_graph(entities, relations)
        logger.info(f"图谱更新: {len(entities)}实体, {len(relations)}关系")
    
    # 提取关键事实（用关键词模式从对话中提取可记忆的片段）
    facts = _extract_important_facts(conversation_text)
    
    captured_count = 0
    for fact_text in facts:
        memory_id = add_memory(
            content=fact_text,
            metadata={
                "source": "auto_capture",
                "topic": _guess_topic(fact_text),
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "tier": "warm",
            }
        )
        captured_count += 1
        logger.info(f"自动捕获 → {fact_text[:60]}...")
    
    return {
        "captured": captured_count,
        "entities": len(entities),
        "relations": len(relations),
    }


def _extract_important_facts(text: str) -> list:
    """从对话文本中提取重要事实（基于规则模式）"""
    facts = []
    
    # 模式1：包含关键动词的句子
    key_verbs = ["决定", "选择", "用", "改用", "安装", "配置", "设置", 
                 "发现", "确认", "明确", "计划", "需要", "要求"]
    sentences = re.split(r'[。！？\n]', text)
    for s in sentences:
        s = s.strip()
        if len(s) < 8 or len(s) > 300:
            continue
        # 跳过角色标记
        s = re.sub(r'^(user|assistant|系统|乐之|伯仕)[:：]?\s*', '', s)
        if not s:
            continue
        # 检查是否包含关键动词
        for verb in key_verbs:
            if verb in s:
                # 去掉寒暄前缀
                clean = re.sub(r'^(好的|好嘞|明白了|收到|嗯|哦)[，。!！]?\s*', '', s)
                if len(clean) > 10:
                    facts.append(f"[决策] {clean}")
                break
    
    # 模式2：技术关键词
    tech_keywords = ["模型", "工具", "框架", "API", "配置", "版本", 
                     "代码", "脚本", "函数", "接口", "服务", "部署"]
    for s in sentences:
        s = s.strip()
        if len(s) < 12 or len(s) > 300:
            continue
        s = re.sub(r'^(user|assistant|系统|乐之|伯仕)[:：]?\s*', '', s)
        if not s:
            continue
        for kw in tech_keywords:
            if kw in s:
                clean = re.sub(r'^(好的|好嘞|明白了|收到)[，。!！]?\s*', '', s)
                if len(clean) > 10 and not any(f"[决策]" in f and f[4:].strip()[:20] == clean[:20] for f in facts):
                    facts.append(f"[技术] {clean}")
                break
    
    # 去重
    seen = set()
    unique_facts = []
    for f in facts:
        key = f[:50]
        if key not in seen:
            seen.add(key)
            unique_facts.append(f)
    
    return unique_facts[:5]


def _guess_topic(text: str) -> str:
    """根据文本猜话题分类"""
    topics_map = [
        (["虚拟寺院", "UE5", "虚幻", "场景", "渲染", "PCG", "建筑", "材质"], "虚拟寺院"),
        (["模型", "大语言", "LLM", "GPT", "Qwen", "DeepSeek", "Claude", "训练", "微调"], "AI模型"),
        (["记忆", "ChromaDB", "向量", "embedding", "检索", "知识图谱"], "记忆系统"),
        (["配置", "安装", "部署", "服务器", "容器", "Docker", "Ollama", "Gateway"], "系统运维"),
        (["代码", "开发", "Git", "仓库", "分支", "PR", "提交"], "软件开发"),
        (["文章", "入库", "Obsidian", "知识库", "阅读", "学习"], "知识管理"),
        (["Harness", "Agent", "Cron", "Skill", "工具", "自动化"], "Agent工程"),
    ]
    
    for keywords, topic in topics_map:
        for kw in keywords:
            if kw in text:
                return topic
    return "一般"


# ═══════════════════════════════════════════════════════════
# 模块3：预取（prefetch + system_prompt_block 等效）
# ═══════════════════════════════════════════════════════════
def prefetch_memories(query: str = "", top_k: int = 5, use_three_way: bool = True) -> list:
    """
    预取相关记忆。
    
    参数：
        query: 当前话题关键词，为空则取热区话题
        top_k: 返回条数
        use_three_way: 是否使用三路融合检索
    
    返回：
        [{"content": str, "score": float, "source": str}, ...]
    """
    if not query:
        # 从热区话题中取
        query = _get_hot_topic()
    
    if use_three_way:
        return multi_search(query, top_k=top_k)
    else:
        return search_memory(query, top_k=top_k)


def _get_hot_topic() -> str:
    """获取最热的当前话题"""
    if not os.path.exists(HOT_FILE):
        return ""
    try:
        with open(HOT_FILE, encoding='utf-8') as f:
            data = json.load(f)
        if isinstance(data, dict):
            topics = data.get("topics", [])
        elif isinstance(data, list):
            topics = data
        else:
            return ""
        if topics:
            # 取热度最高的话题
            topic = max(topics, key=lambda t: t.get("heat", 0))
            return topic.get("name", "")
    except (json.JSONDecodeError, OSError):
        pass
    return ""


def build_session_brief() -> str:
    """
    构建会话简报（system_prompt_block 等效）。
    返回格式化的项目状态摘要，可注入到系统提示词。
    """
    lines = []
    
    # 1. 当前热区话题
    hot_topic = _get_hot_topic()
    if hot_topic:
        lines.append(f"🔥 当前专注话题: {hot_topic}")
    
    # 2. 相关记忆
    memories = prefetch_memories(hot_topic, top_k=3, use_three_way=True)
    if memories:
        lines.append("📋 最近相关记忆:")
        for m in memories:
            content = m.get("content", "")[:80]
            source = m.get("source", "vector")
            lines.append(f"   [{source}] {content}")
    
    # 3. 记忆总量
    try:
        total = chroma_count()
        lines.append(f"📊 记忆库: {total} 条")
    except:
        pass
    
    return "\n".join(lines)


# ═══════════════════════════════════════════════════════════
# 模块4：会话结束处理（on_session_end 等效）
# ═══════════════════════════════════════════════════════════
def session_end_summary(session_id: str = None) -> dict:
    """
    会话结束时，提取并归档本次会话的关键信息。
    """
    result = capture_recent_turns(
        _read_state_db_recent(session_id=session_id, limit=20)
    )
    
    # 生成总结日志
    summary = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "session_id": session_id or "current",
        "captured": result.get("captured", 0),
        "entities": result.get("entities", 0),
        "relations": result.get("relations", 0),
    }
    
    # 保存到 live_turns
    try:
        turns = _read_live_turns()
        turns.append(summary)
        with open(LIVE_TURNS_FILE, 'w', encoding='utf-8') as f:
            json.dump(turns[-100:], f, ensure_ascii=False, indent=2)
    except:
        pass
    
    return summary


# ═══════════════════════════════════════════════════════════
# 模块5：三路检索增强 CLI
# ═══════════════════════════════════════════════════════════
def search_enhanced(query: str, top_k: int = 5, 
                    use_keyword: bool = True,
                    use_vector: bool = True,
                    use_graph: bool = True) -> list:
    """
    可配置的多策略检索。
    默认三路全开。
    """
    results = []
    
    if use_vector:
        vr = search_memory(query, top_k=top_k)
        for r in vr:
            r["source"] = "vector"
            r["score"] = 1.0 - r.get("score", 0)  # 距离转相似度
        results.extend(vr)
    
    if use_keyword:
        kr = keyword_search(query, top_k=top_k)
        results.extend(kr)
    
    if use_graph:
        try:
            from knowledge_graph import KnowledgeGraph
            kg = KnowledgeGraph(KG_PATH)
            entities = kg.search(query)
            for e in entities:
                results.append({
                    "id": f"kg_{e['name']}",
                    "content": f"实体: {e['name']} ({e.get('type', '')})",
                    "source": "graph", "score": 0.7,
                })
                # 关联节点
                subgraph = kg.query(e["name"], max_depth=1)
                for ename, enode in subgraph.get("nodes", {}).items():
                    if ename != e["name"]:
                        results.append({
                            "id": f"kg_{ename}",
                            "content": f"关联: {ename} ({enode.get('type', '')})",
                            "source": "graph", "score": 0.5,
                        })
        except ImportError:
            pass
    
    # 合并去重
    seen = set()
    deduped = []
    for r in sorted(results, key=lambda x: x.get("score", 0), reverse=True):
        key = str(r.get("content", ""))[:50]
        if key not in seen:
            seen.add(key)
            deduped.append(r)
    
    return deduped[:top_k]


# ═══════════════════════════════════════════════════════════
# CLI 入口
# ═══════════════════════════════════════════════════════════
if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "capture"
    
    if cmd == "capture":
        result = capture_recent_turns()
        print(json.dumps(result, ensure_ascii=False))
    
    elif cmd == "prefetch":
        query = sys.argv[2] if len(sys.argv) > 2 else ""
        memories = prefetch_memories(query, use_three_way=True)
        print(json.dumps(memories, ensure_ascii=False, indent=2))
    
    elif cmd == "brief":
        print(build_session_brief())
    
    elif cmd == "session-end":
        session_id = sys.argv[2] if len(sys.argv) > 2 else None
        result = session_end_summary(session_id)
        print(json.dumps(result, ensure_ascii=False))
    
    elif cmd == "search":
        query = sys.argv[2] if len(sys.argv) > 2 else ""
        top_k = int(sys.argv[3]) if len(sys.argv) > 3 else 5
        results = search_enhanced(query, top_k=top_k)
        print(json.dumps(results, ensure_ascii=False, indent=2))
    
    elif cmd == "status":
        total = chroma_count()
        print(f"📊 记忆总数: {total}")
        print(f"🔥 当前热词: {_get_hot_topic() or '无'}")
        try:
            from knowledge_graph import KnowledgeGraph
            kg = KnowledgeGraph(KG_PATH)
            nodes = kg._data.get("nodes", {})
            edges = kg._data.get("edges", [])
            print(f"🌐 知识图谱: {len(nodes)} 节点, {len(edges)} 边")
        except:
            print("🌐 知识图谱: 不可用")
        print(f"📖 已入库文章: 7 篇 (09_AI智能体工程)")
    
    else:
        print(__doc__)
