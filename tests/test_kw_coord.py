# -*- coding: utf-8 -*-
"""关键词通道 coord 退化修复 + 弱证据降权（不连库：伪造 _kw_index / 直接喂候选）。

背景见 §11.12/§11.13：纯中文整句提问时整段汉字被当成 3.0 权重主 token，df=0
→ coord 把全部候选乘成 0 → 通道静默失效。修复=只在「至少一个主 token 命中」
时施加 coord，并给降级候选打 `_weak`，由 _score_merge 按 _KW_WEAK_WV/WK 降权。

跑法：cd ~/.boshi && ./venv/Scripts/python.exe -m unittest tests.test_kw_coord -v
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pg_bridge  # noqa: E402

DOC0 = ("00000000-0000-0000-0000-000000000001", "群晖硬盘显示已损毁，但检测是好的", {}, 20,
        "群晖硬盘显示已损毁，但检测是好的")
DOC1 = ("00000000-0000-0000-0000-000000000002", "显存不足时把 batch 降下来", {}, 10,
        "显存不足时把 batch 降下来")


def fake_index(postings, avgdl=15.0):
    return {"docs": [DOC0, DOC1], "postings": postings, "avgdl": avgdl}


class CoordDegenerateTest(unittest.TestCase):
    """main_hit_n == 0：不准让 coord 把候选清零。"""

    def setUp(self):
        self._orig = pg_bridge._kw_index

    def tearDown(self):
        pg_bridge._kw_index = self._orig

    def test_pure_cjk_sentence_keeps_candidates(self):
        # 整句主 token df=0，只有 bigram「损毁」命中
        pg_bridge._kw_index = lambda c, cond, params: fake_index({"损毁": [(0, 1)]})
        toks = [("群晖硬盘显示已损毁但检测是好的怎么办", 3.0), ("损毁", 1.0)]
        out = pg_bridge._kw_channel(None, toks, 5, "", [])
        self.assertTrue(out, "coord 退化把候选全清零了")
        self.assertTrue(all(it["_weak"] for it in out))

    def test_main_token_hit_is_not_weak(self):
        pg_bridge._kw_index = lambda c, cond, params: fake_index({"8199": [(0, 1)]})
        out = pg_bridge._kw_channel(None, [("8199", 3.0), ("端口", 1.0)], 5, "", [])
        self.assertTrue(out)
        self.assertFalse(any(it["_weak"] for it in out))

    def test_coord_still_applies_when_main_token_hits(self):
        # doc0 命中主 token+「端口」，doc1 只命中 bigram「端口」→ coord 应把 doc0 顶到第一
        pg_bridge._kw_index = lambda c, cond, params: fake_index(
            {"8199": [(0, 1)], "端口": [(0, 1), (1, 1)]})
        out = pg_bridge._kw_channel(None, [("8199", 3.0), ("端口", 1.0)], 5, "", [])
        self.assertEqual(out[0]["id"], DOC0[0])
        self.assertEqual(out[0]["kw"], 1.0)
        # doc1 的主词覆盖率为 0 → coord 后落榜（这是 coord 的本意，不是退化）
        self.assertNotIn(DOC1[0], [it["id"] for it in out])

    def test_empty_postings_still_empty(self):
        pg_bridge._kw_index = lambda c, cond, params: fake_index({})
        self.assertEqual(pg_bridge._kw_channel(None, [("某某整句", 3.0)], 5, "", []), [])


class MergeWeightTest(unittest.TestCase):
    """_score_merge：弱证据按 0.70/0.30，强证据按 0.45/0.55。"""

    @staticmethod
    def kw_item(weak):
        return {"id": "kw-only", "content": "x", "metadata": {}, "score": 0.0,
                "kw_abs": 1.0, "_weak": weak}

    def test_weak_downweighted(self):
        out = pg_bridge._score_merge([], [self.kw_item(True)], 5)
        # f = (0.70*0 + 0.30*1.0)/1.0 = 0.30 → score = 1-(0.60+0.40*0.30)
        self.assertAlmostEqual(out[0]["score"], 0.28, places=6)
        self.assertAlmostEqual(out[0]["fused"], 0.30, places=4)

    def test_strong_unchanged(self):
        out = pg_bridge._score_merge([], [self.kw_item(False)], 5)
        self.assertAlmostEqual(out[0]["score"], 0.18, places=6)
        self.assertAlmostEqual(out[0]["fused"], 0.55, places=4)

    def test_flag_not_leaked_score_merge(self):
        out = pg_bridge._score_merge([], [self.kw_item(True)], 5)
        self.assertNotIn("_weak", out[0])

    def test_flag_not_leaked_rrf_merge(self):
        out = pg_bridge._rrf_merge([], [self.kw_item(True)], 5)
        self.assertNotIn("_weak", out[0])

    def test_explicit_weights_respected(self):
        # 调用方显式传权时不被弱证据覆盖
        out = pg_bridge._score_merge([], [self.kw_item(True)], 5, 0.9, 0.1)
        self.assertAlmostEqual(out[0]["fused"], 0.10, places=4)


if __name__ == "__main__":
    unittest.main()
