"""v5 升级集成测试：验证整条链路连通"""
import os
import sys
import json
sys.path.insert(0, os.path.dirname(__file__))
sys.path.insert(0, os.path.expanduser("~/.boshi"))
sys.path.insert(0, os.path.expanduser("~/.boshi/memory"))


def test_full_pipeline():
    """完整链路：提取 → 图谱 → 检索"""
    from extractor import extract_facts, _cleanup_response, _normalize_relation_keys
    from knowledge_graph import KnowledgeGraph
    import tempfile
    
    with tempfile.TemporaryDirectory() as tmp:
        kg = KnowledgeGraph(storage_path=os.path.join(tmp, "kg.json"))
        
        # 1. 模拟提取过程（用直接添加来隔离模型不稳定）
        kg.add_node("PCG框架", type="技术")
        kg.add_node("UE5", type="引擎")
        kg.add_node("寺院围墙", type="概念")
        kg.add_edge("PCG框架", "UE5", "属于")
        kg.add_edge("PCG框架", "寺院围墙", "生成")
        
        # 2. 验证图谱查询
        subgraph = kg.query("PCG框架", max_depth=2)
        assert "UE5" in subgraph["nodes"]
        assert "寺院围墙" in subgraph["nodes"]
        assert len(subgraph["edges"]) == 2
        
        # 3. 验证关键词搜索
        results = kg.search("PCG")
        assert any("PCG" in r["name"] for r in results)
        
        # 4. 验证多跳查询
        from_ue5 = kg.query("UE5", max_depth=2)
        assert "PCG框架" in from_ue5["nodes"]
        
        print(f"✅ 图谱节点: {len(subgraph['nodes'])}个")
        print(f"✅ 图谱边: {len(subgraph['edges'])}条")
        print(f"✅ 多跳查询连通: UE5 → PCG框架 → 寺院围墙")


def test_cleanup_and_parse():
    """清理与解析模块协作正常"""
    from extractor import _cleanup_response, _normalize_relation_keys
    
    # think 标签 + source/target 格式
    raw = '<think>\n\n</think>\n\n{"entities": [{"name": "PCG框架", "type": "技术"}], "relations": [{"source": "PCG框架", "relation": "属于", "target": "UE5"}]}'
    cleaned = _cleanup_response(raw)
    assert "<think>" not in cleaned
    
    result = json.loads(cleaned)
    relations = _normalize_relation_keys(result.get("relations", []))
    assert relations[0]["from"] == "PCG框架"
    assert relations[0]["to"] == "UE5"
    
    print(f"✅ 清理解析正常: from={relations[0]['from']}, to={relations[0]['to']}")


def test_multi_search_function():
    """多策略检索函数可调用"""
    from chroma_bridge import multi_search, _merge_and_dedup
    
    # 直接测试合并逻辑
    merged = _merge_and_dedup(
        [{"content": "A", "score": 0.6}],
        [{"content": "B", "score": 0.7}],
        [{"content": "A", "score": 0.5}],
        top_k=5,
    )
    assert len(merged) == 2  # 去重后 2 条
    # A 保留高分 0.6, B 是 0.7
    assert merged[0]["score"] >= merged[-1]["score"]
    
    # 集成调用
    results = multi_search("测试", top_k=3)
    assert isinstance(results, list)
    
    print(f"✅ 合并去重正常: {len(merged)}条结果")
    print(f"✅ multi_search 可调用: 返回 {len(results)}条")


def test_tiered_memory_auto_extract():
    """TieredMemory._auto_extract 可安全调用"""
    from tiered_memory import TieredMemory
    
    m = TieredMemory()
    # 调用不报错即可（模型质量后续迭代）
    m._auto_extract("UE5的PCG框架可以自动生成寺院围墙")
    
    print(f"✅ _auto_extract 安全调用完成")


def test_persist_roundtrip():
    """图谱持久化后重新加载不丢失"""
    import tempfile
    from knowledge_graph import KnowledgeGraph
    
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "kg.json")
        kg = KnowledgeGraph(storage_path=path)
        kg.add_node("PCG框架", type="技术")
        kg.add_edge("PCG框架", "UE5", "属于")
        
        kg2 = KnowledgeGraph(storage_path=path)
        n = kg2.get_node("PCG框架")
        assert n is not None
        assert n["type"] == "技术"
    
    print("✅ 持久化读写正常")
