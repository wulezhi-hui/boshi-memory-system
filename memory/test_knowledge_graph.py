"""测试知识图谱模块（含实体归一化）"""
import os
import sys
sys.path.insert(0, os.path.dirname(__file__))


def test_add_node_and_query():
    """添加节点后能查询到"""
    import tempfile
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
    import tempfile
    from knowledge_graph import KnowledgeGraph
    
    with tempfile.TemporaryDirectory() as tmp:
        kg = KnowledgeGraph(storage_path=os.path.join(tmp, "kg.json"))
        kg.add_node("PCG框架")
        kg.add_node("寺院围墙")
        kg.add_node("UE5")
        kg.add_edge("PCG框架", "UE5", "属于")
        kg.add_edge("PCG框架", "寺院围墙", "生成")
        
        result = kg.query("UE5", max_depth=2)
        assert len(result["nodes"]) == 3


def test_add_duplicate_node():
    """重复添加节点，mention_count 增加"""
    import tempfile
    from knowledge_graph import KnowledgeGraph
    
    with tempfile.TemporaryDirectory() as tmp:
        kg = KnowledgeGraph(storage_path=os.path.join(tmp, "kg.json"))
        kg.add_node("PCG框架")
        kg.add_node("PCG框架")
        node = kg.get_node("PCG框架")
        assert node["mention_count"] == 2


def test_add_duplicate_edge():
    """重复添加同一条边不重复"""
    import tempfile
    from knowledge_graph import KnowledgeGraph
    
    with tempfile.TemporaryDirectory() as tmp:
        kg = KnowledgeGraph(storage_path=os.path.join(tmp, "kg.json"))
        e1 = kg.add_edge("PCG框架", "UE5", "属于")
        e2 = kg.add_edge("PCG框架", "UE5", "属于")
        assert len(kg._data["edges"]) == 1
        assert e1["from"] == e2["from"]


def test_alias_merge_keep_longer():
    """短名→已有长名：合并到长名节点"""
    import tempfile
    from knowledge_graph import KnowledgeGraph
    
    with tempfile.TemporaryDirectory() as tmp:
        kg = KnowledgeGraph(storage_path=os.path.join(tmp, "kg.json"))
        kg.add_node("PCG框架", type="技术")
        
        # 添加短名 "PCG"
        result = kg.add_node("PCG")
        assert "PCG框架" in kg._data["nodes"]  # 长名保留
        assert "PCG" not in kg._data["nodes"]   # 短名合并了
        
        node = kg.get_node("PCG框架")
        assert node["mention_count"] == 2  # 两次 add_node 合并


def test_alias_upgrade_to_longer():
    """长名→已有短名：升级为长名"""
    import tempfile
    from knowledge_graph import KnowledgeGraph
    
    with tempfile.TemporaryDirectory() as tmp:
        kg = KnowledgeGraph(storage_path=os.path.join(tmp, "kg.json"))
        kg.add_node("PCG", type="技术")
        kg.add_edge("PCG", "UE5", "属于")
        
        # 添加长名 "PCG框架"
        result = kg.add_node("PCG框架", attr="程序化生成")
        assert "PCG框架" in kg._data["nodes"]
        assert "PCG" not in kg._data["nodes"]  # 旧名被升级
        
        # 边引用也更新了
        assert kg._data["edges"][0]["from"] == "PCG框架"


def test_no_alias_for_unrelated():
    """不相关的名称不合并"""
    import tempfile
    from knowledge_graph import KnowledgeGraph
    
    with tempfile.TemporaryDirectory() as tmp:
        kg = KnowledgeGraph(storage_path=os.path.join(tmp, "kg.json"))
        kg.add_node("PCG框架")
        kg.add_node("UE5")
        assert len(kg._data["nodes"]) == 2


def test_search_by_keyword():
    """关键词搜索节点"""
    import tempfile
    from knowledge_graph import KnowledgeGraph
    
    with tempfile.TemporaryDirectory() as tmp:
        kg = KnowledgeGraph(storage_path=os.path.join(tmp, "kg.json"))
        kg.add_node("PCG框架", type="技术")
        kg.add_node("手动搭建", type="方法")
        
        results = kg.search("PCG")
        assert any("PCG" in r["name"] for r in results)


def test_persist_and_reload():
    """数据持久化到磁盘后重新加载不丢失"""
    import tempfile
    import time
    from knowledge_graph import KnowledgeGraph
    
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "kg.json")
        kg = KnowledgeGraph(storage_path=path)
        kg.add_node("PCG框架")
        kg.add_edge("PCG框架", "UE5", "属于")
        
        kg2 = KnowledgeGraph(storage_path=path)
        assert kg2.get_node("PCG框架") is not None
        assert len(kg2.query("PCG框架")["edges"]) == 1


def test_to_text_output():
    """to_text 生成人类可读文本"""
    import tempfile
    from knowledge_graph import KnowledgeGraph
    
    with tempfile.TemporaryDirectory() as tmp:
        kg = KnowledgeGraph(storage_path=os.path.join(tmp, "kg.json"))
        kg.add_node("PCG框架", type="技术")
        text = kg.to_text()
        assert "PCG框架" in text
        assert "技术" in text
