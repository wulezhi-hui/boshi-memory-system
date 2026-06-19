"""测试多策略并行检索"""
import os
import sys
sys.path.insert(0, os.path.expanduser("~/.boshi"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__)))


def test_multi_search_integration():
    """multi_search 函数存在、可调用、返回 list"""
    from chroma_bridge import multi_search
    
    results = multi_search("PCG框架")
    assert isinstance(results, list)


def test_merge_and_dedup():
    """合并排序 + 去重逻辑正常"""
    from chroma_bridge import _merge_and_dedup
    
    vector_results = [
        {"content": "PCG框架生成围墙", "score": 0.6, "source": "vector"},
        {"content": "UE5引擎", "score": 0.4, "source": "vector"},
    ]
    keyword_results = [
        {"content": "PCG框架（技术）", "score": 0.7, "source": "keyword"},
    ]
    graph_results = [
        {"content": "关联: UE5引擎", "score": 0.5, "source": "graph"},
    ]
    
    merged = _merge_and_dedup(vector_results, keyword_results, graph_results, top_k=3)
    assert len(merged) <= 3
    # 先按 score 降序
    if merged:
        assert merged[0]["score"] >= merged[-1]["score"]


def test_merge_dedup_same_content():
    """相同内容只保留最高分"""
    from chroma_bridge import _merge_and_dedup
    
    vector_results = [
        {"content": "PCG框架生成围墙", "score": 0.6, "source": "vector"},
    ]
    keyword_results = [
        {"content": "PCG框架生成围墙", "score": 0.8, "source": "keyword"},
    ]
    
    merged = _merge_and_dedup(vector_results, keyword_results, [], top_k=5)
    assert len(merged) == 1
    assert merged[0]["score"] == 0.8  # 保留高分
