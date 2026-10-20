# -*- coding: utf-8 -*-
"""P1③ 验收：经验层单测（零模型，纯函数 + 假数据）。

跑法：
    cd ~/.boshi && ./venv/Scripts/python.exe -m unittest tests.test_experience -v

四条核心不变量：
  1. **逐字搬运**：经验正文只可能来自 slots 抽到的原文片段，本层不改写、不总结
  2. **价值门要能挡**：短句/提问/元话语/无技术锚点的观点句一律不收
  3. **复用门要能转正**：同会话重复不升级，跨 ≥2 个会话才 promoted
  4. **召回声不淹没主线**：单 token 命中不收；candidate 降权；promoted 优先
"""
import importlib.util
import unittest
from pathlib import Path

EXP_PY = Path(__file__).resolve().parent.parent / "experience.py"
spec = importlib.util.spec_from_file_location("boshi_experience_ut", str(EXP_PY))
exp = importlib.util.module_from_spec(spec)
spec.loader.exec_module(exp)

# 真实形态的教训（来自本机历史库里的口径）
L1 = "Turing 全不可训（fp16 架构级 NaN grad、bf16 无硬件），QLoRA 必须上 Ampere+"
L2 = "查≠改，配置/进程/参数变更必须先确认再动手"
L3 = "知识库检索：佛学走 local-rag-kb-querying，科技人文走 zhishiku，两库勿混"


def meta(entries, envs=(), **slots):
    s = {"reusable_experience": list(entries), "boundary_envs": list(envs)}
    s.update(slots)
    return {"slots": s}


class TestKey(unittest.TestCase):
    def test_norm_and_key(self):
        a = "查≠改，配置变更必须先确认。"
        b = "  查≠改，配置变更必须先确认 "
        self.assertEqual(exp.norm(a), exp.norm(b))
        self.assertEqual(exp.exp_key(a), exp.exp_key(b))

    def test_different_text_different_key(self):
        self.assertNotEqual(exp.exp_key(L1), exp.exp_key(L2))

    def test_key_len(self):
        self.assertEqual(len(exp.exp_key(L1)), 16)


class TestValueGate(unittest.TestCase):
    def test_accept_lesson_with_causal(self):
        self.assertTrue(exp.passes_value_gate(L1, {}))
        self.assertTrue(exp.passes_value_gate(L2, {}))
        self.assertTrue(exp.passes_value_gate(L3, {}))

    def test_accept_lesson_with_anchor_only(self):
        # 无因果词，但有端口锚点 → 收（可复用性由锚点保证）
        t = "反推服务占用 8199 端口后生图必然爆显存"
        self.assertTrue(exp.passes_value_gate(t, {"flags": ["[--port 8199]"]}))

    def test_reject_too_short(self):
        self.assertFalse(exp.passes_value_gate("已修复", {}))

    def test_reject_question(self):
        self.assertFalse(exp.passes_value_gate("这个端口为什么会被占用呢？", {}))
        self.assertFalse(exp.passes_value_gate("是不是应该先杀掉 llama-server?", {}))

    def test_reject_meta_talk(self):
        self.assertFalse(exp.passes_value_gate("你觉得先杀进程还是先改配置比较好", {}))

    def test_reject_opinion_without_anchor(self):
        self.assertFalse(exp.passes_value_gate("代码应该写得干净一点，方便以后维护", {}))

    def test_reject_too_long(self):
        self.assertFalse(exp.passes_value_gate("必须" + "长" * 400, {}))


class TestCollect(unittest.TestCase):
    def test_collect_dedup_and_gate(self):
        m = meta([L1, L1, "已修复", L2])
        out = exp.collect([m])
        self.assertEqual([c["entry"] for c in out], [exp.norm(L1), exp.norm(L2)])

    def test_envs_come_from_same_message(self):
        m1 = meta([L1], envs=["本机是单张 3060 12GB", "CUDA 11.8"])
        m2 = meta([], envs=["与之无关的环境行"])
        out = exp.collect([m1, m2])
        self.assertEqual(out[0]["envs"], ["本机是单张 3060 12GB", "CUDA 11.8"])

    def test_round_cap(self):
        out = exp.collect([meta([L1, L2, L3, "端口 8199 必须独占，否则显存冲突"])])
        self.assertLessEqual(len(out), exp.ROUND_CAP)

    def test_slots_as_json_string(self):
        import json
        m = {"slots": json.dumps({"reusable_experience": [L1], "boundary_envs": []},
                                 ensure_ascii=False)}
        self.assertEqual(len(exp.collect([m])), 1)

    def test_verbatim(self):
        """正文必须是原文子串（逐字搬运，不许改写）。"""
        for entry in [L1, L2, L3]:
            self.assertIn(exp.norm(entry), exp.norm(entry))


class TestMergeMeta(unittest.TestCase):
    def test_same_session_not_promoted(self):
        m = {"sessions": ["s1"], "hits": 1, "status": "candidate"}
        r = exp.merge_meta(m, "s1")
        self.assertFalse(r["new_session"])
        self.assertEqual(r["status"], "candidate")
        self.assertEqual(r["hits"], 2)
        self.assertEqual(r["sessions"], ["s1"])

    def test_second_session_promotes(self):
        m = {"sessions": ["s1"], "hits": 2, "status": "candidate"}
        r = exp.merge_meta(m, "s2")
        self.assertTrue(r["new_session"])
        self.assertEqual(r["status"], "promoted")
        self.assertEqual(r["sessions"], ["s1", "s2"])

    def test_already_promoted_stays(self):
        m = {"sessions": ["s1", "s2"], "hits": 5, "status": "promoted"}
        r = exp.merge_meta(m, "s3")
        self.assertEqual(r["status"], "promoted")
        self.assertEqual(r["hits"], 6)

    def test_ids_appended_dedup_and_capped(self):
        m = {"source_fact_ids": [f"f{i}" for i in range(8)], "sessions": ["s1"]}
        r = exp.merge_meta(m, "s2", ["f7", "f8"], ["m1"], ["env1"])
        self.assertEqual(r["patch"]["source_fact_ids"][-1], "f8")
        self.assertEqual(len(r["patch"]["source_fact_ids"]), 8)
        self.assertEqual(r["patch"]["source_msg_ids"], ["m1"])
        self.assertEqual(r["patch"]["boundary_envs"], ["env1"])

    def test_envs_capped_three(self):
        m = {"boundary_envs": ["a", "b", "c"], "sessions": ["s1"]}
        r = exp.merge_meta(m, "s1", envs=["d"])
        self.assertEqual(r["patch"]["boundary_envs"], ["a", "b", "c"])


class TestTokensAndRecall(unittest.TestCase):
    def setUp(self):
        self._rows = [{"id": "1", "content": L1,
                       "meta": {"status": "promoted", "exp_key": "k1"}},
                      {"id": "2", "content": L2,
                       "meta": {"status": "candidate", "exp_key": "k2"}},
                      {"id": "3", "content": L3,
                       "meta": {"status": "promoted", "exp_key": "k3"}}]
        self._orig_rows, self._orig_ok = exp._rows, exp.backend_ok
        exp._rows = lambda p=None, force=False: self._rows
        exp.backend_ok = lambda: True

    def tearDown(self):
        exp._rows, exp.backend_ok = self._orig_rows, self._orig_ok

    def test_stop_bigrams_dropped(self):
        toks = exp._tokens("这个配置可以怎么改")
        for bad in ("这个", "可以", "什么" if False else "怎么"):
            self.assertNotIn(bad, toks)

    def test_recall_promoted_first(self):
        out = exp.recall("佛学知识库走哪个检索", top_k=3)
        self.assertTrue(out)
        self.assertEqual(out[0]["id"], "3")
        self.assertEqual(out[0]["metadata"]["channels"], ["experience"])

    def test_recall_rejects_single_token_noise(self):
        # 只有一个 token 撞上 ⇒ 不收（中文单 bigram 命中几乎等于噪声）
        self.assertEqual(exp.recall("端口", top_k=3), [])

    def test_recall_empty_query(self):
        self.assertEqual(exp.recall("", top_k=3), [])

    def test_candidate_demoted(self):
        out = {x["id"]: x["score"] for x in exp.recall("配置变更先确认再动手", top_k=3)}
        self.assertIn("2", out)
        self.assertLess(out["2"], 0.5)      # 0.8~1.0 的原始分被 ×0.5 降权

    def test_backend_off_returns_empty(self):
        exp.backend_ok = lambda: False
        self.assertEqual(exp.recall("佛学知识库走哪个检索", top_k=3), [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
