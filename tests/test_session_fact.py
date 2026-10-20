# -*- coding: utf-8 -*-
"""P3 验收：会话事实层（`session_fact.py`）单测。

跑法：
    cd ~/.boshi && python -m unittest tests.test_session_fact -v

四条核心不变量：
  1. **门控正确**：没抽到 decisions/open_questions → `merge` 返回 None（不写库）
  2. **幂等**：同一批条目重复合并 → 返回 None（内容未变，不该 churn 一行新版本）
  3. **滚动**：旧条目保留、新条目追加、超 `FACT_CAP` 丢**最旧**的
  4. **原文保真**：条目只做空白归一，字符内容一字不改（不生成、不总结）
"""
import importlib.util
import json
import unittest
from pathlib import Path

FACT_PY = Path(__file__).resolve().parent.parent / "session_fact.py"

spec = importlib.util.spec_from_file_location("boshi_session_fact_ut", str(FACT_PY))
fact = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fact)


def _meta(decisions=None, open_questions=None, **extra):
    """按 slots.to_meta() 的真实形状造 metadata（slots 是 JSON **字符串**）。"""
    slots = {}
    if decisions is not None:
        slots["decisions"] = decisions
    if open_questions is not None:
        slots["open_questions"] = open_questions
    slots.update(extra)
    return {"slots": json.dumps(slots, ensure_ascii=False), "raw_len": 100}


class TestItemsFromSlots(unittest.TestCase):
    def test_空输入全部安全(self):
        for bad in (None, {}, {"slots": None}, {"slots": "not json"},
                    {"slots": "{}"}, {"slots": json.dumps({"decisions": []})}):
            self.assertEqual(fact.items_from_slots(bad), {})

    def test_只取决策与待办(self):
        m = _meta(decisions=["定用 PG 后端"], open_questions=["要不要补时间"],
                  files=["/h/x.py"], ports=[8199], versions=["v7.2"])
        got = fact.items_from_slots(m)
        self.assertEqual(sorted(got), ["decisions", "open_questions"])
        self.assertEqual(got["decisions"], ["定用 PG 后端"])

    def test_空白条目被丢弃(self):
        m = _meta(decisions=["  ", "", "决定启用 multiplex"])
        self.assertEqual(fact.items_from_slots(m)["decisions"], ["决定启用 multiplex"])


class TestRenderParse(unittest.TestCase):
    def test_渲染解析互逆(self):
        items = {"decisions": ["定用 PG", "关掉 kw 门"],
                 "open_questions": ["补不补 created_at"]}
        text = fact.render(items)
        self.assertEqual(text, "决策：\n- 定用 PG\n- 关掉 kw 门\n待办：\n- 补不补 created_at")
        self.assertEqual(fact.parse(text), items)

    def test_空小节不渲染(self):
        self.assertEqual(fact.render({"decisions": ["A"]}), "决策：\n- A")
        self.assertEqual(fact.render({}), "")

    def test_解析容忍脏行与未知小节(self):
        text = "决策：\n- A\n\n杂项：\n- B\n待办：\n- C\n  续行无前缀\n"
        self.assertEqual(fact.parse(text), {"decisions": ["A"], "open_questions": ["C"]})

    def test_条目内换行被压成单空格(self):
        items = {"decisions": ["一行\n二行"]}
        self.assertEqual(fact.parse(fact.render(items))["decisions"], ["一行 二行"])


class TestMerge(unittest.TestCase):
    def test_门控_无条目不写(self):
        self.assertIsNone(fact.merge(None, {}))
        self.assertIsNone(fact.merge("决策：\n- A", {}))

    def test_首次写入(self):
        got = fact.merge(None, {"decisions": ["定用 PG"]})
        self.assertEqual(got, "决策：\n- 定用 PG")

    def test_幂等_重复条目返回None(self):
        prev = "决策：\n- 定用 PG\n待办：\n- 补不补时间"
        self.assertIsNone(fact.merge(prev, {"decisions": ["定用 PG"]}))
        self.assertIsNone(fact.merge(prev, {"decisions": ["定用  PG"]}))   # 空白归一后同一条

    def test_追加新条目_旧条目保留(self):
        prev = "决策：\n- 定用 PG"
        got = fact.merge(prev, {"decisions": ["改 sync_turn"], "open_questions": ["补 4 行？"]})
        self.assertEqual(got, "决策：\n- 定用 PG\n- 改 sync_turn\n待办：\n- 补 4 行？")

    def test_超上限丢最旧(self):
        prev = "决策：\n" + "\n".join("- d%d" % i for i in range(3))
        got = fact.merge(prev, {"decisions": ["new"]}, cap=3)
        self.assertEqual(fact.parse(got)["decisions"], ["d1", "d2", "new"])

    def test_跨小节去重_决策优先(self):
        # slots 会把同一句同时抽进 decisions 与 open_questions（实测过）
        got = fact.merge(None, {"decisions": ["决定关掉关键词通道"],
                                "open_questions": ["决定关掉关键词通道", "要不要补 created_at"]})
        self.assertEqual(fact.parse(got),
                         {"decisions": ["决定关掉关键词通道"],
                          "open_questions": ["要不要补 created_at"]})

    def test_原文不被改写(self):
        raw = "把 `_KW_POOL_CAP` 从 20 调到 50（BOSHI_KW_POOL_CAP）"
        got = fact.merge(None, {"decisions": [raw]})
        self.assertIn(raw, got)          # 一字不改、原样落地


if __name__ == "__main__":
    unittest.main()
