"""融合序混入（RRF blend）回归：重排把融合序第 1 名打到 20 名外时，融合序要能把它拉回前列。

为什么要有这条：blend 是「两组名次相加」，一旦 W_FU 被写成 0、或融合名次取错
（比如在重排覆盖 score 之后才取），结果不会报错——只会静默退化成纯重排序，
而那正是被实测证明「救一批、打掉一批」的行为。顺序断言是唯一能看见它的地方。
"""
import os
import sys
import time
import types

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pg_bridge as b


class _Resp(object):
    def __init__(self, payload):
        self._p = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self._p


def _fake_requests(score_by_doc):
    """假 /rerank：按文档内容查预置分（越大越相关）。"""
    mod = types.ModuleType("requests")

    def post(url, timeout=None, json=None):
        docs = (json or {}).get("documents") or []
        return _Resp({"results": [{"index": i, "relevance_score": float(score_by_doc.get(d, 0.0))}
                                  for i, d in enumerate(docs)]})
    mod.post = post
    return mod


def _items(n=8):
    # 融合序：f0 最前……f7 最后（score 升序＝距离语义）
    return [{"id": "f%d" % i, "content": "DOC%d 内容" % i,
             "score": round(0.10 + 0.01 * i, 4), "metadata": {}} for i in range(n)]


def _run(scores, items, w_fu=None):
    real_req = sys.modules.get("requests")
    real_w = b._BLEND_W_FU
    sys.modules["requests"] = _fake_requests(scores)
    b._RERANK_LAST_OK[0] = time.time()          # 跳过显存预检
    b._RERANK_DOWN_UNTIL[0] = 0.0
    if w_fu is not None:
        b._BLEND_W_FU = w_fu
    try:
        return b._rerank_items("测试 query", items, 5)
    finally:
        b._BLEND_W_FU = real_w
        if real_req is None:
            sys.modules.pop("requests", None)
        else:
            sys.modules["requests"] = real_req


def test_blend_rescues_fusion_leader():
    """融合第 1 名被重排判到最后 → blend 后必须仍在前 5。"""
    # 重排把 DOC0（融合 1）打到最低分，其余依次递减
    scores = {"DOC%d 内容" % i: 5.0 - i for i in range(1, 8)}
    scores["DOC0 内容"] = -8.0
    out = _run(scores, _items(), w_fu=1.0)
    ids = [x["id"] for x in out]
    assert "f0" in ids, "融合序第 1 名被 blend 丢了：%s" % ids
    # 重排把它判成最差（第 8），blend 里 W_RK 仍然更大 → 只拉回前列而不是第 1，
    # 这是刻意的：只给融合序「发言权」，不给「否决权」。
    assert ids.index("f0") <= 4, "融合序第 1 名只被拉回到第 %d 名" % (ids.index("f0") + 1)


def test_default_is_pure_rerank():
    """线上默认必须是纯重排（W_FU=0）：blend 实测损 overlap/A5，只作对照留存。"""
    assert b._BLEND_W_FU == 0.0, "默认 W_FU 被改成 %s，线上口径会漂" % b._BLEND_W_FU
    scores = {"DOC%d 内容" % i: 5.0 - i for i in range(1, 8)}
    scores["DOC0 内容"] = -8.0
    ids = [x["id"] for x in _run(scores, _items())]      # 不传 w_fu，走默认
    assert "f0" not in ids, "默认路径仍混了融合序：%s" % ids


def test_pure_rerank_without_blend():
    """W_FU=0 = 纯重排序：融合序第 1 名该掉出去就掉出去（对照组）。"""
    scores = {"DOC%d 内容" % i: 5.0 - i for i in range(1, 8)}
    scores["DOC0 内容"] = -8.0
    out = _run(scores, _items(), w_fu=0.0)
    ids = [x["id"] for x in out]
    assert "f0" not in ids, "W_FU=0 时不该有融合序投票，f0 却还在：%s" % ids
    assert ids[0] == "f1"


def test_score_monotone_with_order():
    """score 必须与顺序同向：被顶上来的条目不能留着更低（更差）的分。"""
    scores = {"DOC0 内容": -8.0, "DOC7 内容": 9.0}
    for i in range(1, 7):
        scores["DOC%d 内容" % i] = 2.0 - i
    out = _run(scores, _items(), w_fu=1.0)
    sc = [x["score"] for x in out]
    assert sc == sorted(sc), "score 非单调递增：%s" % sc


def test_fusion_rank_recorded():
    """metadata.fusion_rank 要记下原始融合名次（1-based），便于事后诊断。"""
    scores = {"DOC%d 内容" % i: 3.0 - 0.1 * i for i in range(8)}
    out = _run(scores, _items(), w_fu=1.0)
    fr = [x["metadata"].get("fusion_rank") for x in out]
    assert all(isinstance(x, int) and 1 <= x <= 8 for x in fr), fr
    assert len(set(fr)) == len(fr), "同一条被记了两次融合名次：%s" % fr


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print("ok", name)
    print("all blend tests passed")
