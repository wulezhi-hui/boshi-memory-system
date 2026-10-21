"""rerank 守卫回归：空/空白 query、候选不足 2 条 → 必须原样返回，且一次模型都不调。

为什么要有这条：_rerank_items 的守卫一旦丢了，空 query 会被送去 /rerank，端点**不会报错**
（HTTP 200），而是返回一个近乎随机的顺序——它会把按热度/时间排好的结果打乱，
属于「静默错序」，只有靠断言才看得见。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pg_bridge as b


def _items():
    return [{"id": "a", "content": "AAA 内容", "score": 0.1},
            {"id": "b", "content": "BBB 内容", "score": 0.2},
            {"id": "c", "content": "CCC 内容", "score": 0.3}]


def _ids(rows):
    return [r["id"] for r in rows]


def test_empty_query_keeps_order():
    for q in ("", "   ", "\n\t "):
        out = b._rerank_items(q, _items(), 5)
        assert _ids(out) == ["a", "b", "c"], q
        assert all("rerank_score" not in (r.get("metadata") or {}) for r in out)


def test_none_query_keeps_order():
    out = b._rerank_items(None, _items(), 5)
    assert _ids(out) == ["a", "b", "c"]


def test_single_candidate_skips():
    out = b._rerank_items("伯仕记忆系统", _items()[:1], 5)
    assert _ids(out) == ["a"]


def test_top_k_truncates_without_model():
    out = b._rerank_items("", _items(), 2)
    assert _ids(out) == ["a", "b"]


def test_dead_endpoint_degrades_to_original_order():
    """端点不可达 → 原序返回 + 进熔断，不抛异常（这条不依赖 11437 是否在跑）。"""
    _url, _ct = b._RERANK_URL, b._RERANK_CONNECT_TIMEOUT
    b._RERANK_DOWN_UNTIL[0] = 0.0
    b._RERANK_URL, b._RERANK_CONNECT_TIMEOUT = "http://127.0.0.1:11999/rerank", 0.2
    try:
        out = b._rerank_items("伯仕记忆系统", _items(), 5)
        assert _ids(out) == ["a", "b", "c"]
        assert b._RERANK_DOWN_UNTIL[0] > 0, "失败后应进熔断"
    finally:
        b._RERANK_URL, b._RERANK_CONNECT_TIMEOUT = _url, _ct
        b._RERANK_DOWN_UNTIL[0] = 0.0


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print("ok", name)
    print("all guard tests passed")
