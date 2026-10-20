# -*- coding: utf-8 -*-
"""P3.1 接线回归：事实层只采**用户侧**槽位。

跑法：
    cd ~/.boshi && ./venv/Scripts/python.exe -m unittest tests.test_fact_source -v

为什么做源码级断言而不是跑 sync_turn：
  插件 import 的是 Hermes 运行时（`agent.memory_provider` 等），在 ~/.boshi/venv
  里 import 不了；为了这一行接线把整个 Hermes 塞进单测不划算。这里只锁死
  「事实层的输入是哪个变量」这一件事——它一旦被改回两侧都采，本轮实测过的
  脏数据（助手列菜单被记成决策）就会立刻复发。
"""
import unittest
from pathlib import Path

PLUGIN = Path(__file__).resolve().parent.parent / "plugins" / "boshi" / "__init__.py"


class TestFactLayerSource(unittest.TestCase):
    def setUp(self):
        self.src = PLUGIN.read_text(encoding="utf-8")

    def test_只从用户侧槽位取条目(self):
        self.assertIn("items_from_slots(u_slots_meta)", self.src)

    def test_不再两侧合并(self):
        self.assertNotIn("for _m in (u_slots_meta, a_slots_meta)", self.src)

    def test_事实层仍然upsert(self):
        idx = self.src.index("_fact.FACT_TOPIC")
        self.assertIn("upsert=True", self.src[idx:idx + 600])


if __name__ == "__main__":
    unittest.main(verbosity=2)
