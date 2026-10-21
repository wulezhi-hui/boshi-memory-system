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


class KwAbsAllTest(unittest.TestCase):
    """弱证据下 kw_abs 改用「库内真实存在的 token」做分母（2026-10-21，§11.22）。

    修复前的病灶：纯中文 query 的主 token 是整段汉字，df=0。它的 idf 因此被
    算成 log(1+(n+0.5)/0.5)（极大），分母 widf_total 被它单方面占满，而
    `_kw_abs` 对 CJK 主词又给「共享 bigram 的部分信用」——于是 kw_abs 退化成
    「这条文档里出现过多少查询的 bigram」，实测 A9 上中位数 0.44/0.53/0.78、
    最高 0.88，几乎人人接近满分（噪声）；而 `_score_merge` 只读 kw_abs，
    所以关键词通道在融合层等于在往排序里灌噪声。
    abs_all 打开后，分母改成「库里真有 posting 的 token」，idf 重新有意义，
    分布被拉开（实测 Q2 中位 0.08/最大 0.72，Q3 0.18/0.32）。
    """

    TOKS = [("群晖硬盘显示已损毁但检测是好的怎么办", 3.0), ("损毁", 1.0), ("检测", 1.0)]

    def setUp(self):
        self._orig_idx = pg_bridge._kw_index
        self._orig_abs = pg_bridge._KW_ABS_ALL
        pg_bridge._kw_index = lambda c, cond, params: fake_index(
            {"损毁": [(0, 1)], "检测": [(0, 1), (1, 1)]})

    def tearDown(self):
        pg_bridge._kw_index = self._orig_idx
        pg_bridge._KW_ABS_ALL = self._orig_abs

    def test_weak_gets_real_kw_abs(self):
        pg_bridge._KW_ABS_ALL = True
        out = pg_bridge._kw_channel(None, self.TOKS, 5, "", [])
        by = {it["id"]: it for it in out}
        self.assertTrue(out and all(it["_weak"] for it in out))
        self.assertGreater(by[DOC0[0]]["kw_abs"], 0.0, "bigram 命中却没换来任何 kw_abs")
        self.assertAlmostEqual(by[DOC0[0]]["kw_abs"], by[DOC0[0]]["kw_abs_all"], places=6)
        self.assertLess(by[DOC1[0]]["kw_abs"], by[DOC0[0]]["kw_abs"])

    def test_flag_off_falls_back_to_phantom_denominator(self):
        pg_bridge._KW_ABS_ALL = True
        on = {it["id"]: it["kw_abs"] for it in pg_bridge._kw_channel(None, self.TOKS, 5, "", [])}
        pg_bridge._KW_ABS_ALL = False
        off_out = pg_bridge._kw_channel(None, self.TOKS, 5, "", [])
        off = {it["id"]: it["kw_abs"] for it in off_out}
        self.assertTrue(off_out)
        # 关掉后没有 kw_abs_all 字段；分数由整句 phantom token 的 partial credit 决定
        # （合成用例里两者数值可能巧合接近，故只断言口径不同、不再走 abs_all）
        self.assertTrue(all("kw_abs_all" not in it for it in off_out))
        self.assertNotEqual(off[DOC0[0]], on[DOC0[0]])

    def test_strong_path_untouched(self):
        # 主 token 命中（8199 在 doc 里）→ 不走 abs_all，kw_abs 仍按 prim 算
        pg_bridge._kw_index = lambda c, cond, params: fake_index(
            {"8199": [(0, 1)], "端口": [(0, 1)]})
        pg_bridge._KW_ABS_ALL = True
        out = pg_bridge._kw_channel(None, [("8199", 3.0), ("端口", 1.0)], 5, "", [])
        self.assertTrue(out)
        self.assertFalse(out[0]["_weak"])
        self.assertNotIn("kw_abs_all", out[0])


class KwRelBlendTest(unittest.TestCase):
    """弱证据下掺入关键词通道相对分（2026-10-21，§11.23）。

    病灶：`_score_merge` 只读 kw_abs。改写式 query 的 gold 常只对上两三个
    bigram（kw_abs 低），但它在关键词通道里是第 1 名——绝对分把它埋掉。
    `BOSHI_KW_REL_BLEND=b` 时：k = kw_abs + b*kw（kw 为通道内 top1 归一）。
    """

    @staticmethod
    def items():
        # rank1：kw_abs 低但相对分满格；rank2：绝对覆盖率高
        return [
            {"id": "reltop", "content": "a", "metadata": {}, "score": 0.0,
             "kw_abs": 0.2, "kw": 1.0, "_weak": True},
            {"id": "abstop", "content": "b", "metadata": {}, "score": 0.0,
             "kw_abs": 0.5, "kw": 0.4, "_weak": True},
        ]

    def setUp(self):
        self._old = pg_bridge._KW_REL_BLEND
        pg_bridge._KW_REL_BLEND = 0.0

    def tearDown(self):
        pg_bridge._KW_REL_BLEND = self._old

    def test_off_by_default_keeps_abs_order(self):
        self.assertEqual(pg_bridge._score_merge([], self.items(), 5)[0]["id"], "abstop")

    @unittest.skipIf("BOSHI_KW_REL_BLEND" in os.environ,
                     "环境里显式设了 BOSHI_KW_REL_BLEND，跳过默认值断言")
    def test_module_default_is_04(self):
        # 交付口径（2026-10-21）：默认开 0.4 —— 4 个 ASOF 快照上 paraphrase
        # top5 全 +2、零退化，见 §11.23 与 scratch/a9_b_stability.py
        self.assertEqual(self._old, 0.4)

    def test_on_promotes_channel_leader(self):
        pg_bridge._KW_REL_BLEND = 1.0
        out = pg_bridge._score_merge([], self.items(), 5)
        self.assertEqual(out[0]["id"], "reltop")
        # k = 0.2 + 1.0*1.0 = 1.20；f = (0.70*0 + 0.30*1.20)/1.0
        self.assertAlmostEqual(out[0]["fused"], 0.36, places=4)

    def test_strong_query_untouched(self):
        # 主 token 命中（无 _weak）时即使开了 blend 也不掺相对分
        pg_bridge._KW_REL_BLEND = 0.4
        strong = [dict(it, _weak=False) for it in self.items()]
        out = pg_bridge._score_merge([], strong, 5)
        self.assertEqual(out[0]["id"], "abstop")          # 绝对分仍占优
        self.assertAlmostEqual(out[0]["fused"], 0.275, places=4)   # 0.55*0.5


if __name__ == "__main__":
    unittest.main()
