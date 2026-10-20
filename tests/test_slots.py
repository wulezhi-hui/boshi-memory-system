# -*- coding: utf-8 -*-
"""P2 验收 4：抽提器单测（零模型规则，值与期望逐项对齐）。

跑法：
    cd ~/.boshi && ./venv/Scripts/python.exe -m unittest tests.test_slots -v

三条核心不变量：
  1. **逐字搬运**：所有槽位值必须是原文的连续子串（不许改写、不许拼装）
  2. **负例不误抓**：`https://` 不能进 files；`47.6 MB` 的 47.6 不能进 versions；
     `_KW_LENB=0.5` 的参数值不能进 versions
  3. **标题不是陈述**：markdown/编号标题行不能占掉 statement
"""
import importlib.util
import json
import sys
import unittest
from pathlib import Path

SLOTS_PY = Path(__file__).resolve().parent.parent / "slots.py"

spec = importlib.util.spec_from_file_location("boshi_slots_ut", str(SLOTS_PY))
slots = importlib.util.module_from_spec(spec)
spec.loader.exec_module(slots)

# 固定语料 1：技术与决策混合（含 URL 负例、单位数字负例、标题负例）
T1 = """## 一、显存与端口

这次的问题在 F:\\models\\qwen3-27b.gguf 上：显存 11.2 GB 刚好不够，端口 8199 的反推服务抢了显存。

nvidia-smi --query-gpu=memory.used --format=csv

结论：改用 llama.cpp 的 --tensor-split 手动分配，不再用 auto-fit。

铁律：跑 ComfyUI 前先 nvidia-smi 看显存，别和 llama-server 抢。

剩下的：A7 的 cap=50 还没测，待补充。

参考 https://github.com/ggerganov/llama.cpp 的文档，v7.2 之后再说。

下载速率为 47.6 MB/s。
"""

# 固定语料 2：纯寒暄，抽不出任何槽位
T2 = "嗯，好的。"

# 固定语料 3：只有标题 + 短列表，测 statement 不被标题占用
T3 = """## 当前进度

1. 先跑评测
2. 再看召回
"""


class TestInvariants(unittest.TestCase):
    def test_all_values_are_verbatim_substrings(self):
        """所有槽位值必须是原文连续子串——这是「只搬运不生成」的硬约束。"""
        for name, text in (("T1", T1), ("T2", T2), ("T3", T3)):
            r = slots.extract(text)
            self.assertIn("statement", r)
            if r["statement"]:
                self.assertIn(r["statement"], text, f"{name}: statement 不是原文子串")
            for k in slots.SLOT_KEYS:
                for v in r[k]:
                    self.assertIn(v, text, f"{name}/{k}: 值「{v}」不是原文子串")

    def test_statement_not_heading(self):
        r = slots.extract(T1)
        self.assertNotIn("一、显存与端口", r["statement"])
        self.assertTrue(r["statement"].startswith("这次的问题在"), r["statement"])
        r3 = slots.extract(T3)
        self.assertNotIn("当前进度", r3["statement"])

    def test_empty_input_safe(self):
        for t in ("", None, "   \n\n  "):
            r = slots.extract(t)
            self.assertEqual(r["statement"], "")
            self.assertEqual(slots.filled_count(r), 0)
        self.assertEqual(slots.doc_text("x", None), "x")
        self.assertEqual(slots.doc_text("x", "not-json"), "x")
        self.assertEqual(slots.doc_text("x", {}), "x")


class TestNegativeCases(unittest.TestCase):
    def test_url_not_in_files(self):
        r = slots.extract(T1)
        joined = " ".join(r["files"])
        self.assertNotIn("https", joined.lower())
        self.assertNotIn("s:/", joined.lower())
        # 但真实盘符路径必须抓到
        self.assertTrue(any("qwen3-27b.gguf" in f for f in r["files"]), r["files"])

    def test_unit_number_not_version(self):
        r = slots.extract(T1)
        self.assertNotIn("47.6", r["versions"])
        self.assertNotIn("11.2", r["versions"])
        self.assertIn("47.6 MB", r["numbers"])

    def test_param_value_not_version(self):
        r = slots.extract("阻尼 _KW_LENB=0.5，阈值 0.85 都试过了。")
        self.assertEqual(r["versions"], [])

    def test_ip_not_version(self):
        r = slots.extract("服务在 172.16.0.219 上监听 8648。")
        self.assertEqual(r["versions"], [])


class TestFourSegmentVersion(unittest.TestCase):
    """四段版本号（2026-10-20 补）：`0.1.44.1` 必须进 versions，同时不能把 IP 放进来。"""

    def test_bare_four_seg_with_version_context(self):
        r = slots.extract("版本 0.1.44.1 已发布，修了闪退。")
        self.assertIn("0.1.44.1", r["versions"])

    def test_v_prefixed_four_seg(self):
        r = slots.extract("升级到 v0.1.40.4 之后就好了。")
        self.assertIn("v0.1.40.4", r["versions"])

    def test_four_seg_with_segment_over_255(self):
        # 任一节 >255 不可能是 IP，按版本收
        r = slots.extract("10.0.19041.3636 是 Win10 build。")
        self.assertIn("10.0.19041.3636", r["versions"])

    def test_private_ip_not_version(self):
        for text in ("绑到 0.0.0.0 供局域网访问。",
                     "ProxyServer = 127.0.0.1:17891，指向 0dcloud。",
                     "网段 192.168.1.20 和 172.16.0.219 都是内网。"):
            self.assertEqual(slots.extract(text)["versions"], [], text)

    def test_bare_four_seg_without_context_rejected(self):
        # 无版本语境时，与 IP 不可区分的裸 4 段号不收（1.2.3.4 就是反例本身）
        r = slots.extract("这两者 1.2.3.4 在语法上不可区分。")
        self.assertEqual(r["versions"], [])

    def test_server_word_not_version_context(self):
        # `api_server` / `ProxyServer` 里的 ver 不能当版本线索
        r = slots.extract("vllm.entrypoints.openai.api_server --host 0.0.0.0")
        self.assertEqual(r["versions"], [])


class TestExtraction(unittest.TestCase):
    def setUp(self):
        self.r = slots.extract(T1)

    def test_files(self):
        self.assertTrue(any("qwen3-27b.gguf" in f for f in self.r["files"]), self.r["files"])

    def test_flags(self):
        self.assertIn("--tensor-split", self.r["flags"])

    def test_ports(self):
        self.assertIn("8199", self.r["ports"])

    def test_commands(self):
        self.assertTrue(any(c.startswith("nvidia-smi") for c in self.r["commands"]),
                        self.r["commands"])

    def test_versions(self):
        self.assertIn("v7.2", self.r["versions"])

    def test_decisions(self):
        joined = " ".join(self.r["decisions"])
        self.assertIn("改用 llama.cpp", joined, self.r["decisions"])
        self.assertNotIn("参考", joined)          # URL 那句不是决策

    def test_open_questions(self):
        joined = " ".join(self.r["open_questions"])
        self.assertIn("cap=50", joined, self.r["open_questions"])

    def test_reusable_experience(self):
        joined = " ".join(self.r["reusable_experience"])
        self.assertIn("铁律", joined, self.r["reusable_experience"])

    def test_boundary_envs(self):
        self.assertTrue(self.r["boundary_envs"], self.r["boundary_envs"])

    def test_filled_count(self):
        self.assertGreaterEqual(slots.filled_count(self.r), 6)
        self.assertEqual(slots.filled_count(slots.extract(T2)), 0)

    def test_lists_capped(self):
        many = "".join(f"端口 80{i:02d} 有问题。" for i in range(30))
        r = slots.extract(many)
        self.assertLessEqual(len(r["ports"]), slots._LIST_CAP)


class TestMetaAndDocText(unittest.TestCase):
    def test_to_meta_is_json_string(self):
        r = slots.extract(T1)
        meta = slots.to_meta(r, len(T1))
        self.assertIsInstance(meta["slots"], str)          # Chroma 只吃标量
        self.assertEqual(meta["raw_len"], len(T1))
        self.assertEqual(json.loads(meta["slots"])["statement"], r["statement"])

    def test_doc_text_contains_statement_and_slots(self):
        r = slots.extract(T1)
        txt = slots.doc_text(T1, r)
        self.assertIn(r["statement"], txt)
        self.assertIn("--tensor-split", txt)
        self.assertIsInstance(slots.doc_text(T1, slots.to_meta(r, len(T1))["slots"]), str)


if __name__ == "__main__":
    unittest.main(verbosity=2)
