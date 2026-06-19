"""测试实体提取模块"""
import sys
import os
sys.path.insert(0, os.path.dirname(__file__))

def test_extract_returns_dict_with_entities_and_relations():
    """函数永远返回正确的数据结构"""
    from extractor import extract_facts
    
    result = extract_facts("UE5的PCG框架可以自动生成寺院围墙，比手动搭快太多")
    
    assert isinstance(result, dict)
    assert "entities" in result
    assert "relations" in result
    assert isinstance(result["entities"], list)
    assert isinstance(result["relations"], list)


def test_extract_returns_empty_for_trivial_text():
    """无实质内容的文本应返回空结果"""
    from extractor import extract_facts
    
    result = extract_facts("好的")
    assert result == {"entities": [], "relations": []}


def test_extract_returns_empty_for_short_text():
    """极短文本返回空"""
    from extractor import extract_facts
    
    result = extract_facts("好")
    assert result == {"entities": [], "relations": []}


def test_extract_returns_empty_for_empty_text():
    """空字符串返回空"""
    from extractor import extract_facts
    
    result = extract_facts("")
    assert result == {"entities": [], "relations": []}


def test_extract_handles_ollama_failure():
    """Ollama 调用失败时应优雅降级"""
    from extractor import extract_facts
    
    result = extract_facts("测试", model="nonexistent-model")
    assert result == {"entities": [], "relations": []}


def test_cleanup_think_tags():
    """think 标签清理正常"""
    from extractor import _cleanup_response
    
    raw = '<think>\n\n</think>\n\n{"entities": [{"name": "PCG"}]}'
    cleaned = _cleanup_response(raw)
    assert cleaned == '{"entities": [{"name": "PCG"}]}'


def test_cleanup_markdown_blocks():
    """markdown 代码块清理正常"""
    from extractor import _cleanup_response
    
    raw = '```json\n{"entities": [{"name": "PCG"}]}\n```'
    cleaned = _cleanup_response(raw)
    assert cleaned == '{"entities": [{"name": "PCG"}]}'


def test_normalize_relation_keys():
    """关系字段名归一化正常"""
    from extractor import _normalize_relation_keys
    
    relations = [{"source": "UE5", "relation": "包含", "target": "PCG"}]
    normalized = _normalize_relation_keys(relations)
    assert normalized[0]["from"] == "UE5"
    assert normalized[0]["to"] == "PCG"
    assert normalized[0]["relation"] == "包含"


def test_filter_blacklist():
    """黑名单中的通用术语被过滤"""
    from extractor import _filter_entities, _BLACKLIST
    
    entities = [
        {"name": "PCG框架", "type": "技术"},
        {"name": "系统", "type": "概念"},
        {"name": "记忆", "type": "概念"},
        {"name": "UE5", "type": "引擎"},
    ]
    filtered = _filter_entities(entities)
    names = [e["name"] for e in filtered]
    assert "PCG框架" in names
    assert "UE5" in names
    assert "系统" not in names
    assert "记忆" not in names
