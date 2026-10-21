# -*- coding: utf-8 -*-
"""知识图谱实体噪声门（is_noise_entity）的单测。

2026-10-21 实测：7,514 个实体里 1,428（19.0%）是
「进了记忆库 / 只有记忆库 / 了你说的工作站 / 一个没在线的模型」这类
从句碎片——上游中文后缀正则允许任意 2~6 汉字前缀，没有虚词检查。

这里只锁形态判断，不连库。
"""
import os
import sys
import unittest

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)

import knowledge_graph


class NoiseEntityTest(unittest.TestCase):
    def test_fragments_are_noise(self):
        for name in ("进了记忆库", "只有记忆库", "今天给记忆库", "了你说的工作站",
                     "一个没在线的模型", "一轮我查一次库", "把它放到系统", "我们就用这个库"):
            self.assertTrue(knowledge_graph.is_noise_entity(name), name)

    def test_whitelist_survives(self):
        # 「乐之」含虚词字「之」，白名单必须救回来；「伯仕记忆系统」同时是已知实体
        for name in knowledge_graph._ENT_WHITELIST:
            self.assertFalse(knowledge_graph.is_noise_entity(name), name)

    def test_ascii_entities_survive(self):
        # 不含 CJK 的名字一律放行：模型名/硬件名/HTTP 状态码都可能带空格与符号
        for name in ("Cursor", "Qwen-Image", "MiniMax H3", "RTX 3060 12GB", "HTTP 200"):
            self.assertFalse(knowledge_graph.is_noise_entity(name), name)

    def test_chinese_tech_terms_survive(self):
        for name in ("ComfyUI插件", "精排服务", "心智模型", "佛光大辞典"):
            self.assertFalse(knowledge_graph.is_noise_entity(name), name)

    def test_empty_is_noise(self):
        self.assertTrue(knowledge_graph.is_noise_entity(""))
        self.assertTrue(knowledge_graph.is_noise_entity("   "))


if __name__ == "__main__":
    unittest.main()
