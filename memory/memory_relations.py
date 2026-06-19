"""
伯仕记忆 v5.8 — 图关系定义层
借鉴 Supermemory 的 Updates/Extends/Related 三种关系类型，
为伯仕记忆系统提供语义丰富的关系管理。

关系类型：
- UPDATES:    信息变更（新事实取代旧事实）
- EXTENDS:    信息丰富（在已有事实上增加细节）
- RELATED:    信息关联（两个独立事实之间的语义关联）
"""

import json
import os
import time
import logging
from dataclasses import dataclass, field, asdict
from typing import Optional

logger = logging.getLogger("伯仕关系")

# ── 预定义关系类型 ──────────────────────────────────

REL_UPDATES = "updates"      # 新信息替代旧信息
REL_EXTENDS = "extends"      # 新信息补充旧信息
REL_RELATED = "related"      # 两个事实相互关联
REL_CAUSES = "causes"        # 事实A导致事实B
REL_DEPENDS = "depends_on"   # 事实B依赖于事实A
REL_EXCLUDES = "excludes"    # 事实A与事实B互斥

# 所有预定义关系类型
PREDEFINED_RELATIONS = {
    REL_UPDATES,
    REL_EXTENDS,
    REL_RELATED,
    REL_CAUSES,
    REL_DEPENDS,
    REL_EXCLUDES,
}

# 关系类型的自然语言描述
RELATION_LABELS = {
    REL_UPDATES: "更新了",
    REL_EXTENDS: "补充了",    # 原 EXTENDS 的中文意译
    REL_RELATED: "关联了",
    REL_CAUSES: "导致了",
    REL_DEPENDS: "依赖于",
    REL_EXCLUDES: "排除了",
}


@dataclass
class MemoryRelation:
    """一条记忆关系"""
    from_memory_id: str       # 源记忆 ID（ChromaDB ID 或 内容前缀）
    to_memory_id: str         # 目标记忆 ID
    rel_type: str             # 关系类型（updates/extends/related/causes/depends_on/excludes）
    label: str = ""           # 关系描述（人类可读，如"用户搬家"）
    from_content: str = ""    # 源记忆摘要（方便阅读，不存储到文件）
    to_content: str = ""      # 目标记忆摘要
    created_at: float = 0.0   # 创建时间戳
    active: bool = True       # 是否有效（updates 类型中旧关系可标记为 inactive）


class MemoryRelationStore:
    """
    记忆关系存储。
    
    与 KnowledgeGraph 不同，这个模块专门管理 ChromaDB 中
    记忆条目之间的关系，而不是实体关系。
    
    存储格式：JSON 邻接表（与 knowledge_graph.json 互补）
    """
    
    def __init__(self, storage_path: str = None):
        if storage_path is None:
            storage_path = os.path.expanduser(
                "~/.boshi/memory/memory_relations.json"
            )
        self.path = storage_path
        self._relations = []  # list of dict
        self._load()
    
    def _load(self):
        if os.path.exists(self.path):
            try:
                with open(self.path, encoding='utf-8') as f:
                    data = json.load(f)
                    self._relations = data.get("relations", [])
            except (json.JSONDecodeError, OSError):
                self._relations = []
        else:
            self._relations = []
    
    def _save(self):
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        with open(self.path, 'w', encoding='utf-8') as f:
            json.dump({"relations": self._relations, "version": "5.8"},
                      f, ensure_ascii=False, indent=2)
    
    def add_relation(self, from_id: str, to_id: str, rel_type: str,
                     label: str = "", from_content: str = "",
                     to_content: str = "") -> dict:
        """添加一条关系"""
        if rel_type not in PREDEFINED_RELATIONS:
            logger.warning(f"未知关系类型: {rel_type}，使用 related")
            rel_type = REL_RELATED
        
        relation = {
            "from": from_id,
            "to": to_id,
            "type": rel_type,
            "label": label,
            "from_content": (from_content or "")[:100],
            "to_content": (to_content or "")[:100],
            "created_at": time.time(),
            "active": True,
        }
        self._relations.append(relation)
        self._save()
        return relation
    
    def add_update_chain(self, old_id: str, new_id: str,
                         reason: str = "",
                         old_content: str = "",
                         new_content: str = "") -> list:
        """
        添加一条更新链（new UPDATES old）。
        
        自动将旧关系的 active 设为 False。
        返回 [old_relation, new_relation]
        """
        # 旧关系标记为 inactive
        for rel in self._relations:
            if (rel["to"] == old_id and rel["type"] == REL_UPDATES
                    and rel.get("active", True)):
                rel["active"] = False
        
        # 标记旧关系关联的节点
        old_rel = self.add_relation(
            from_id=new_id, to_id=old_id,
            rel_type=REL_UPDATES,
            label=reason or "信息更新",
            from_content=new_content,
            to_content=old_content,
        )
        
        return [old_rel]
    
    def add_extend(self, base_id: str, extend_id: str,
                   detail: str = "",
                   base_content: str = "",
                   extend_content: str = "") -> dict:
        """添加一条补充关系（extend EXTENDS base）"""
        return self.add_relation(
            from_id=extend_id, to_id=base_id,
            rel_type=REL_EXTENDS,
            label=detail or "补充信息",
            from_content=extend_content,
            to_content=base_content,
        )
    
    def add_related(self, id_a: str, id_b: str,
                    reason: str = "",
                    content_a: str = "",
                    content_b: str = "") -> dict:
        """添加一条关联关系（双向）"""
        return self.add_relation(
            from_id=id_a, to_id=id_b,
            rel_type=REL_RELATED,
            label=reason or "关联",
            from_content=content_a,
            to_content=content_b,
        )
    
    def query_relations(self, memory_id: str,
                        include_inactive: bool = False) -> list:
        """
        查询某个记忆的所有关系。
        
        参数：
            memory_id: 记忆 ID 或内容前缀
            include_inactive: 是否包含已标记为 inactive 的关系（updates 的旧版本）
        
        返回：
            [{"from": ..., "to": ..., "type": ..., ...}, ...]
        """
        results = []
        for rel in self._relations:
            if not include_inactive and not rel.get("active", True):
                continue
            if rel["from"] == memory_id or rel["to"] == memory_id:
                results.append(dict(rel))
        return results
    
    def get_update_chain(self, memory_id: str) -> list:
        """
        获取某个记忆的完整更新链（从最新到最旧）。
        
        用于追踪信息变化历史：用户偏好、项目状态等的变化轨迹。
        """
        chain = []
        visited = set()
        current = memory_id
        
        while current and current not in visited:
            visited.add(current)
            # 找被 current 更新的更旧版本
            found = None
            for rel in self._relations:
                if (rel["from"] == current and rel["type"] == REL_UPDATES):
                    found = rel
                    break
            if found:
                chain.append(found)
                current = found["to"]
            else:
                break
        
        return chain
    
    def get_extensions(self, base_id: str) -> list:
        """获取某个记忆的所有补充信息"""
        extensions = []
        for rel in self._relations:
            if rel["to"] == base_id and rel["type"] == REL_EXTENDS:
                if rel.get("active", True):
                    extensions.append(rel)
        return extensions
    
    def get_related(self, memory_id: str, max_depth: int = 2) -> dict:
        """
        从某个记忆出发，进行关系图遍历。
        
        类似 knowledge_graph.query()，但基于关系类型。
        返回 {"nodes": {id: info}, "edges": [...]}
        """
        visited_nodes = {memory_id}
        visited_edges = set()
        frontier = {memory_id}
        nodes_info = {}
        edges_result = []
        
        for _ in range(max_depth):
            if not frontier:
                break
            next_frontier = set()
            for node_id in frontier:
                for rel in self._relations:
                    if not rel.get("active", True):
                        continue
                    key = (rel["from"], rel["to"], rel["type"])
                    if key in visited_edges:
                        continue
                    
                    other = None
                    if rel["from"] == node_id and rel["to"] not in visited_nodes:
                        other = rel["to"]
                    elif rel["to"] == node_id and rel["from"] not in visited_nodes:
                        other = rel["from"]
                    
                    if other:
                        visited_nodes.add(other)
                        next_frontier.add(other)
                        visited_edges.add(key)
                        edges_result.append(rel)
                        
                        # 记录节点信息
                        if other not in nodes_info:
                            if rel["to"] == other:
                                nodes_info[other] = {"content": rel.get("to_content", "")}
                            else:
                                nodes_info[other] = {"content": rel.get("from_content", "")}
            
            frontier = next_frontier
        
        return {
            "center": memory_id,
            "nodes": nodes_info,
            "edges": edges_result,
        }
    
    def stats(self) -> dict:
        """统计信息"""
        active = sum(1 for r in self._relations if r.get("active", True))
        by_type = {}
        for r in self._relations:
            t = r["type"]
            by_type[t] = by_type.get(t, 0) + 1
        
        return {
            "total_relations": len(self._relations),
            "active_relations": active,
            "by_type": by_type,
        }
    
    def to_text(self, memory_id: str, max_items: int = 10) -> str:
        """将某个记忆的关系转为人类可读文本"""
        relations = self.query_relations(memory_id)[:max_items]
        if not relations:
            return ""
        
        lines = [f"🔗 记忆关系 ({len(relations)} 条):"]
        for rel in relations:
            label = RELATION_LABELS.get(rel["type"], rel["type"])
            label_text = f" ({rel['label']})" if rel.get("label") else ""
            lines.append(f"  · [{rel['type']}] {label}{label_text}")
            if rel.get("from_content"):
                lines.append(f"    ↳ {rel['from_content'][:40]}")
            if rel.get("to_content"):
                lines.append(f"    ↲ {rel['to_content'][:40]}")
        
        return "\n".join(lines)


# ── 测试入口 ──────────────────────────────────────────
if __name__ == "__main__":
    print("🧪 测试记忆关系模块...")
    
    store = MemoryRelationStore(
        os.path.expanduser("~/.boshi/memory/test_relations.json")
    )
    
    # 1. 测试补充关系
    store.add_extend(
        base_id="mem_001",
        extend_id="mem_002",
        detail="用户偏好: TypeScript > Python",
        base_content="用户喜欢编程",
        extend_content="最喜欢 TypeScript",
    )
    print("✅ 补充关系: mem_002 EXTENDS mem_001")
    
    # 2. 测试更新链
    store.add_update_chain(
        old_id="mem_003",
        new_id="mem_004",
        reason="用户搬到了上海",
        old_content="用户住北京",
        new_content="用户住上海",
    )
    print("✅ 更新链: mem_004 UPDATES mem_003")
    
    # 3. 测试关联关系
    store.add_related(
        id_a="mem_001", id_b="mem_004",
        reason="用户相关的两个事实",
    )
    print("✅ 关联关系: mem_001 RELATED mem_004")
    
    # 4. 查询更新链
    chain = store.get_update_chain("mem_004")
    print(f"📋 更新链长度: {len(chain)}")
    
    # 5. 统计
    print(f"📊 统计: {store.stats()}")
    
    # 6. 图遍历
    graph = store.get_related("mem_001", max_depth=2)
    print(f"🔍 关系图: {len(graph['nodes'])} 节点, {len(graph['edges'])} 边")
    
    # 清理测试文件
    test_path = os.path.expanduser("~/.boshi/memory/test_relations.json")
    if os.path.exists(test_path):
        os.remove(test_path)
    
    print("✅ 测试完成")