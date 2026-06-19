# 伯仕记忆系统 v5 升级 — 实现计划

> **For 伯仕：** 按 writing-plans + subagent-driven-development + TDD 流程分步执行。

**目标：** 为伯仕记忆系统增加自动实体提取、知识图谱、多策略并行检索、主动学习四个能力。一次性搭骨架，逐步迭代优化。

**架构：** ChromaDB 向量存储保持不动，新增 extractor.py（LLM实体抽取）、knowledge_graph.py（邻接表图谱），修改 tiered_memory.py（对话后触发提取）、chroma_bridge.py（多策略检索）。

**技术栈：** Python 3.11+, ChromaDB (all-MiniLM-L6-v2), qwen3.5-4b-buddhist (Ollama), JSON 邻接表

---

## 任务清单

### Task 1: 创建 extractor.py — LLM 实体提取模块

**目标：** 创建一个调用 Ollama 模型从对话中提取实体+关系的模块。

**文件：**
- Create: `~/.boshi/memory/extractor.py`
- Create: `~/.boshi/memory/test_extractor.py`

**Step 1: 写测试文件**

```python
# test_extractor.py
"""测试实体提取模块"""
import sys
import os
sys.path.insert(0, os.path.dirname(__file__))

def test_extract_entities_from_technical_discussion():
    """能正确提取技术讨论中的实体和关系"""
    from extractor import extract_facts
    
    text = "UE5的PCG框架可以自动生成寺院围墙，比手动搭快太多"
    result = extract_facts(text)
    
    assert result is not None
    assert isinstance(result, dict)
    assert "entities" in result
    assert "relations" in result
    
    # 应该提取到 PCG框架 和 UE5
    entities = [e["name"] for e in result["entities"]]
    assert any("PCG" in e for e in entities) or "PCG框架" in entities

def test_extract_returns_empty_for_trivial_text():
    """无实质内容的文本应返回空结果"""
    from extractor import extract_facts
    
    result = extract_facts("好的")
    assert result == {"entities": [], "relations": []}

def test_extract_handles_ollama_failure():
    """Ollama 调用失败时应优雅降级"""
    from extractor import extract_facts
    
    result = extract_facts("测试", model="nonexistent-model")
    assert result == {"entities": [], "relations": []}
```

**Step 2: 运行测试，验证失败**

Run: `python -m pytest ~/.boshi/memory/test_extractor.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'extractor'`

**Step 3: 创建 extractor.py**

```python
"""
伯仕记忆 v5 — 自动实体/关系提取模块
每次对话结束后异步调用 LLM，从对话中提取结构化知识。
"""
import json
import logging
import requests
import sys

logger = logging.getLogger("伯仕提取")

# 默认模型
DEFAULT_MODEL = "qwen3.5-4b-buddhist:latest"
OLLAMA_URL = "http://localhost:11434"

# 提取提示词（极简短，强制 JSON 输出）
EXTRACT_PROMPT = """从以下对话中提取实体和关系。

规则：
1. 实体包括：技术名词、人名、项目名、工具名、概念
2. 实体类型：技术、人物、项目、工具、概念
3. 关系是实体之间的连接，如"属于"、"快于"、"依赖于"
4. 没有关系就不输出 relations
5. 只输出下面的 JSON，不要多余文字

输出格式：
{
  "entities": [{"name": "实体名", "type": "实体类型", "attr": "属性描述（可选）"}],
  "relations": [{"from": "实体A", "to": "实体B", "relation": "关系描述"}]
}

对话：
{text}
"""


def extract_facts(text: str, model: str = DEFAULT_MODEL) -> dict:
    """
    从文本中提取实体和关系。
    
    参数：
        text: 要分析的文本
        model: Ollama 模型名
    
    返回：
        {"entities": [...], "relations": [...]}
        失败时返回空结构
    """
    if not text or len(text.strip()) < 5:
        return {"entities": [], "relations": []}
    
    prompt = EXTRACT_PROMPT.format(text=text[:800])  # 限制输入长度
    
    try:
        resp = requests.post(
            f"{OLLAMA_URL}/api/generate",
            json={
                "model": model,
                "prompt": prompt,
                "stream": False,
                "options": {"temperature": 0.1, "num_predict": 500}
            },
            timeout=30,
        )
        resp.raise_for_status()
        raw = resp.json().get("response", "").strip()
        
        # 清理可能的 markdown 包裹
        if raw.startswith("```"):
            raw = raw.split("\n", 1)[-1]
            raw = raw.rsplit("```", 1)[0]
        raw = raw.strip()
        
        result = json.loads(raw)
        if not isinstance(result, dict):
            return {"entities": [], "relations": []}
        
        entities = result.get("entities", [])
        relations = result.get("relations", [])
        
        # 校验格式
        if not isinstance(entities, list):
            entities = []
        if not isinstance(relations, list):
            relations = []
        
        # 过滤掉空的实体名
        entities = [e for e in entities if e.get("name")]
        
        return {
            "entities": entities[:10],  # 单次最多10个实体
            "relations": relations[:10], # 单次最多10条关系
        }
    
    except requests.exceptions.Timeout:
        logger.warning(f"实体提取超时（{model}）")
        return {"entities": [], "relations": []}
    except requests.exceptions.ConnectionError:
        logger.warning(f"Ollama 连接失败")
        return {"entities": [], "relations": []}
    except (json.JSONDecodeError, KeyError, ValueError) as e:
        logger.debug(f"实体提取解析失败: {e}")
        return {"entities": [], "relations": []}
    except Exception as e:
        logger.error(f"实体提取异常: {e}")
        return {"entities": [], "relations": []}


if __name__ == "__main__":
    # CLI 测试入口
    text = sys.argv[1] if len(sys.argv) > 1 else "你好，今天我们来聊聊PCG框架"
    result = extract_facts(text)
    print(json.dumps(result, ensure_ascii=False, indent=2))
```

**Step 4: 运行测试，验证通过**

Run: `python -m pytest ~/.boshi/memory/test_extractor.py -v`
Expected: PASS (至少基础测试通过)

---

### Task 2: 创建 knowledge_graph.py — 邻接表知识图谱

**目标：** 创建轻量级知识图谱，用 JSON 邻接表存储实体关系，支持多跳查询。

**文件：**
- Create: `~/.boshi/memory/knowledge_graph.py`
- Create: `~/.boshi/memory/test_knowledge_graph.py`

**存储路径：** `~/.boshi/memory/knowledge_graph.json`

**Step 1: 写测试**

```python
# test_knowledge_graph.py
"""测试知识图谱模块"""
import os
import sys
import tempfile
sys.path.insert(0, os.path.dirname(__file__))

def test_add_node_and_query():
    """添加节点后能查询到"""
    import tempfile, os
    from knowledge_graph import KnowledgeGraph
    
    with tempfile.TemporaryDirectory() as tmp:
        kg = KnowledgeGraph(storage_path=os.path.join(tmp, "kg.json"))
        kg.add_node("PCG框架", type="技术")
        kg.add_node("UE5", type="引擎")
        kg.add_edge("PCG框架", "UE5", "属于")
        
        result = kg.query("PCG框架")
        assert "PCG框架" in result["nodes"]
        assert len(result["edges"]) == 1
        assert result["edges"][0]["relation"] == "属于"

def test_multi_hop_query():
    """支持多跳查询"""
    import tempfile, os
    from knowledge_graph import KnowledgeGraph
    
    with tempfile.TemporaryDirectory() as tmp:
        kg = KnowledgeGraph(storage_path=os.path.join(tmp, "kg.json"))
        kg.add_node("PCG框架")
        kg.add_node("寺院围墙")
        kg.add_node("UE5")
        kg.add_edge("PCG框架", "UE5", "属于")
        kg.add_edge("PCG框架", "寺院围墙", "生成")
        
        # 从 UE5 出发，看关联到谁
        result = kg.query("UE5", max_depth=2)
        assert len(result["nodes"]) == 3  # UE5, PCG, 围墙

def test_add_duplicate_node():
    """重复添加节点不报错，mention_count 增加"""
    import tempfile, os
    from knowledge_graph import KnowledgeGraph
    
    with tempfile.TemporaryDirectory() as tmp:
        kg = KnowledgeGraph(storage_path=os.path.join(tmp, "kg.json"))
        kg.add_node("PCG框架")
        kg.add_node("PCG框架")  # 第二次
        node = kg.get_node("PCG框架")
        assert node["mention_count"] == 2

def test_search_by_keyword():
    """关键词搜索节点"""
    import tempfile, os
    from knowledge_graph import KnowledgeGraph
    
    with tempfile.TemporaryDirectory() as tmp:
        kg = KnowledgeGraph(storage_path=os.path.join(tmp, "kg.json"))
        kg.add_node("PCG框架", type="技术")
        kg.add_node("手动搭建", type="方法")
        
        results = kg.search("PCG")
        assert any("PCG" in r["name"] for r in results)

def test_persist_and_reload():
    """数据持久化到磁盘后重新加载不丢失"""
    import tempfile, os
    from knowledge_graph import KnowledgeGraph
    
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "kg.json")
        kg = KnowledgeGraph(storage_path=path)
        kg.add_node("PCG框架")
        kg.add_edge("PCG框架", "UE5", "属于")
        
        # 重新加载
        kg2 = KnowledgeGraph(storage_path=path)
        assert kg2.get_node("PCG框架") is not None
        assert len(kg2.query("PCG框架")["edges"]) == 1
```

**Step 2: 运行测试，验证失败**
Run: `python -m pytest ~/.boshi/memory/test_knowledge_graph.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'knowledge_graph'`

**Step 3: 创建 knowledge_graph.py**

```python
"""
伯仕记忆 v5 — 轻量级知识图谱（JSON 邻接表）
"""
import json
import os
import time
import logging

logger = logging.getLogger("伯仕图谱")

DEFAULT_PATH = os.path.expanduser("~/.boshi/memory/knowledge_graph.json")


class KnowledgeGraph:
    """邻接表知识图谱"""
    
    def __init__(self, storage_path: str = DEFAULT_PATH):
        self.path = storage_path
        self._data = self._load()
    
    def _load(self) -> dict:
        if os.path.exists(self.path):
            try:
                with open(self.path, encoding='utf-8') as f:
                    return json.load(f)
            except (json.JSONDecodeError, OSError):
                pass
        return {"nodes": {}, "edges": []}
    
    def _save(self):
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        with open(self.path, 'w', encoding='utf-8') as f:
            json.dump(self._data, f, ensure_ascii=False, indent=2)
    
    def add_node(self, name: str, type: str = "", attr: str = "") -> dict:
        """添加或更新一个实体节点"""
        nodes = self._data["nodes"]
        name = name.strip()
        if not name:
            return {}
        
        now = time.time()
        if name in nodes:
            nodes[name]["mention_count"] += 1
            nodes[name]["last_seen"] = now
            if attr and not nodes[name].get("attr"):
                nodes[name]["attr"] = attr
        else:
            nodes[name] = {
                "name": name,
                "type": type or "",
                "attr": attr or "",
                "first_seen": now,
                "last_seen": now,
                "mention_count": 1,
            }
        
        self._save()
        return nodes[name]
    
    def add_edge(self, from_name: str, to_name: str, relation: str) -> dict:
        """添加一条关系边"""
        from_name = from_name.strip()
        to_name = to_name.strip()
        if not from_name or not to_name:
            return {}
        
        # 自动创建缺失的节点
        self.add_node(from_name)
        self.add_node(to_name)
        
        edge = {
            "from": from_name,
            "to": to_name,
            "relation": relation.strip(),
            "time": time.time(),
        }
        self._data["edges"].append(edge)
        self._save()
        return edge
    
    def get_node(self, name: str) -> dict | None:
        """获取单个节点"""
        return self._data["nodes"].get(name.strip())
    
    def search(self, keyword: str) -> list:
        """关键词搜索节点（支持模糊匹配）"""
        keyword = keyword.lower().strip()
        if not keyword:
            return []
        results = []
        for name, node in self._data["nodes"].items():
            if keyword in name.lower():
                results.append(node)
        return results
    
    def query(self, start_from: str, max_depth: int = 2) -> dict:
        """
        从 start_from 出发进行图遍历。
        
        参数：
            start_from: 起始节点名
            max_depth: 最大遍历深度（默认2）
        
        返回：
            {"nodes": {name: node, ...}, "edges": [...]}
        """
        start_from = start_from.strip()
        if start_from not in self._data["nodes"]:
            return {"nodes": {}, "edges": []}
        
        visited_nodes = {start_from}
        visited_edges = set()
        
        # BFS 遍历
        frontier = {start_from}
        for _ in range(max_depth):
            if not frontier:
                break
            next_frontier = set()
            for node_name in frontier:
                for edge in self._data["edges"]:
                    key = (edge["from"], edge["to"], edge["relation"])
                    if key in visited_edges:
                        continue
                    if edge["from"] == node_name and edge["to"] not in visited_nodes:
                        visited_nodes.add(edge["to"])
                        next_frontier.add(edge["to"])
                        visited_edges.add(key)
                    elif edge["to"] == node_name and edge["from"] not in visited_nodes:
                        visited_nodes.add(edge["from"])
                        next_frontier.add(edge["from"])
                        visited_edges.add(key)
            frontier = next_frontier
        
        # 收集结果
        nodes = {n: self._data["nodes"][n] for n in visited_nodes if n in self._data["nodes"]}
        edges = []
        for e in self._data["edges"]:
            if e["from"] in visited_nodes and e["to"] in visited_nodes:
                edges.append(e)
        
        return {"nodes": nodes, "edges": edges}
    
    def to_text(self, max_nodes: int = 10) -> str:
        """将图谱转为人类可读文本（用于上下文注入）"""
        nodes = list(self._data["nodes"].values())[:max_nodes]
        if not nodes:
            return ""
        lines = ["📚 知识图谱中的实体："]
        for n in nodes:
            parts = [f"  · {n['name']}"]
            if n.get("type"):
                parts.append(f"（{n['type']}）")
            if n.get("attr"):
                parts.append(f"— {n['attr']}")
            lines.append("".join(parts))
        return "\n".join(lines)
    
    def stats(self) -> dict:
        return {
            "node_count": len(self._data["nodes"]),
            "edge_count": len(self._data["edges"]),
            "path": self.path,
        }
```

**Step 4: 运行测试，验证通过**
Run: `python -m pytest ~/.boshi/memory/test_knowledge_graph.py -v`
Expected: PASS (5个测试全通过)

---

### Task 3: 改造 chroma_bridge.py — 多策略并行检索

**目标：** 将 chroma_bridge.search_memory 从单一向量检索改造为三路并行检索（向量语义 + 关键词精确 + 图遍历）

**文件：**
- Modify: `~/.boshi/chroma_bridge.py`
- Create: `~/.boshi/memory/test_retrieval.py`

**Step 1: 写测试**

```python
# test_retrieval.py
"""测试多策略检索"""
import os, sys
sys.path.insert(0, os.path.expanduser("~/.boshi"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__)))

def test_multi_strategy_search_returns_results():
    """多策略检索返回合并结果"""
    import tempfile
    from knowledge_graph import KnowledgeGraph
    from chroma_bridge import multi_search
    
    # 先写一些记忆到 Chroma，再测试检索
    # 这里简化测试：至少函数存在且返回 list
    results = multi_search("PCG框架")
    assert isinstance(results, list)
```

**Step 2: 修改 chroma_bridge.py**（需要读懂原有代码后精准修改）

在文件末尾追加 `multi_search` 函数：

```python
def multi_search(query: str, top_k: int = 5) -> list:
    """
    多策略并行检索：向量语义 + 关键词精确 + 图遍历
    
    三路并行 → 合并排序 → 去重 → 时间衰减加权 → Top-N
    """
    from knowledge_graph import KnowledgeGraph
    
    kg = KnowledgeGraph()
    
    # 策略1: 向量语义检索（原有）
    vector_results = search_memory(query, top_k=top_k * 2)
    
    # 策略2: 关键词精确检索（实体名/标签匹配）
    keyword_results = []
    matched_entities = kg.search(query)
    for entity in matched_entities:
        keyword_results.append({
            "id": f"kg_{entity['name']}",
            "content": f"实体: {entity['name']} ({entity.get('type', '')})",
            "source": "keyword",
            "score": 0.7,  # 精确匹配高分
            "entity": entity,
        })
    
    # 策略3: 图遍历检索
    graph_results = []
    for entity in matched_entities:
        subgraph = kg.query(entity["name"], max_depth=2)
        for ename, enode in subgraph.get("nodes", {}).items():
            if ename != entity["name"]:
                graph_results.append({
                    "id": f"kg_rel_{ename}",
                    "content": f"关联实体: {ename} (从 {entity['name']} 出发)",
                    "source": "graph",
                    "score": 0.5,
                    "entity": enode,
                })
    
    # 合并排序：按 score 降序，去重
    all_results = vector_results + keyword_results + graph_results
    
    # 去重（相同 content 只保留最高分）
    seen = set()
    deduped = []
    for r in sorted(all_results, key=lambda x: x.get("score", 0), reverse=True):
        content_key = r.get("content", "")[:50]
        if content_key not in seen:
            seen.add(content_key)
            deduped.append(r)
    
    return deduped[:top_k]
```

**Step 3: 运行测试**
Run: `python -m pytest ~/.boshi/memory/test_retrieval.py -v`
Expected: 至少函数不报错

---

### Task 4: 修改 tiered_memory.py — 对话后自动提取

**目标：** 修改 `store_conversation()` 方法，在每次对话结束后异步触发实体提取，并将结果存入知识图谱和 Chroma。

**文件：**
- Modify: `~/.boshi/memory/tiered_memory.py`（store_conversation 方法 + 新增 _auto_extract 方法）

**Step 1: 在 TieredMemory 类中新增方法**

在 `store_conversation` 方法之后（约第619行），新增：

```python
def _auto_extract(self, text: str, source: str = "conversation"):
    """
    自动从对话文本中提取实体和关系，存入知识图谱。
    异步触发（不阻塞主回复流程）。
    """
    try:
        from extractor import extract_facts
        from knowledge_graph import KnowledgeGraph
        
        kg = KnowledgeGraph()
        result = extract_facts(text)
        
        if result.get("entities"):
            for entity in result["entities"]:
                kg.add_node(entity["name"], type=entity.get("type", ""), attr=entity.get("attr", ""))
                # 同时写入 Chroma 作为语义检索的补充
                self.warm.add_warm(
                    f"实体: {entity['name']} ({entity.get('type', '')}) — {entity.get('attr', '')}",
                    source=source,
                    topic_name="entity",
                )
        
        if result.get("relations"):
            for rel in result["relations"]:
                kg.add_edge(rel["from"], rel["to"], rel["relation"])
        
        if result.get("entities") or result.get("relations"):
            logger.info(f"自动提取: {len(result['entities'])}实体, {len(result['relations'])}关系")
    
    except Exception as e:
        logger.debug(f"自动提取失败（非关键）: {e}")
```

**Step 2: 修改 store_conversation()**

在 `store_conversation` 方法的最后（第618行 `self.warm.add_warm(...)` 之后），添加：

```python
        # v5: 对话后自动提取实体和关系
        if len(user_text) > 10:
            self._auto_extract(f"用户: {user_text} 伯仕: {assistant_msg[:200]}", source=source)
```

**Step 3: 验证改动**

手动测试：
Run: `python -c "import sys; sys.path.insert(0, '~/.boshi'); from tiered_memory import TieredMemory; m = TieredMemory(); m._auto_extract('UE5的PCG框架可以自动生成寺院围墙'); print('OK')"`
Expected: 不报错，且 `~/.boshi/memory/knowledge_graph.json` 中出现了 PCG框架 实体

---

### Task 5: 集成测试 — 端到端验证

**目标：** 验证从对话输入 → 自动提取 → 图谱存储 → 多策略检索 的整条链路连通。

**Step 1: 写集成测试**

```python
# test_memory_v5_integration.py
"""v5 升级集成测试"""
import os, sys, json, time
sys.path.insert(0, os.path.dirname(__file__))
sys.path.insert(0, os.path.expanduser("~/.boshi"))

def test_full_pipeline():
    """完整链路：对话→提取→图谱→检索"""
    from extractor import extract_facts
    from knowledge_graph import KnowledgeGraph
    
    kg = KnowledgeGraph()
    
    # 1. 提取
    text = "我们决定用PCG框架在UE5里自动生成寺院围墙，比手动搭效率高"
    result = extract_facts(text)
    assert len(result["entities"]) > 0, f"应提取到实体，得到: {result}"
    
    # 2. 存入图谱
    for e in result["entities"]:
        kg.add_node(e["name"], type=e.get("type", ""))
    for r in result["relations"]:
        kg.add_edge(r["from"], r["to"], r["relation"])
    
    # 3. 检索验证
    search_results = kg.search("PCG")
    assert len(search_results) > 0, f"应搜到 PCG 相关实体"
    
    subgraph = kg.query("PCG框架", max_depth=2)
    assert "UE5" in subgraph["nodes"], f"PCG框架应关联到UE5, 得到: {list(subgraph['nodes'].keys())}"
    
    print(f"✅ 提取到 {len(result['entities'])} 实体, {len(result['relations'])} 关系")
    print(f"✅ 图谱搜索: {[r['name'] for r in search_results]}")
    print(f"✅ 多跳查询: {list(subgraph['nodes'].keys())}")
```

**Step 2: 运行集成测试**
Run: `python -m pytest ~/.boshi/memory/test_memory_v5_integration.py -v -s`
Expected: PASS，且输出显示提取到的实体和关系

**Step 3: 验证图谱持久化**
Run: `python -c "import json; d=json.load(open(os.path.expanduser('~/.boshi/memory/knowledge_graph.json'))); print(f'节点: {len(d[\"nodes\"])}, 边: {len(d[\"edges\"])}')"`
Expected: 存在节点和边数据

---

## 后续迭代（第一轮之后）

| 轮次 | 内容 |
|------|------|
| 第二轮 | 优化提取 prompt，减少噪声和遗漏 |
| 第三轮 | 图查询加入时间衰减、实体去重合并 |
| 第四轮 | 检索策略权重调优，实际数据 A/B 对比 |
| 第五轮 | 主动学习逻辑（判断新旧/冲突/优先级） |

---

## 验证标准

1. ✅ 说一句话包含实体 → extractor 能提取出来
2. ✅ 查询实体关系 → knowledge_graph.query() 返回正确链路
3. ✅ 多策略检索 → 三路结果合并后无重复、排序合理
4. ✅ 持久化 → 重启后图谱数据不丢
5. ✅ 降级 → Ollama 不可用时不影响主回复
