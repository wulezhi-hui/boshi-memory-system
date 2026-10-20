# -*- coding: utf-8 -*-
"""P3.1 回归：抽提器的三个脏数据来源（2026-10-20 实测）。

跑法：
    cd ~/.boshi && ./venv/Scripts/python.exe -m unittest tests.test_slots_p31 -v

三个 bug 都是「事实层写进去的第一条就是垃圾」的直接原因：
  1. 列表项不切行 —— 整段 markdown 列表算「一句」，关键词在最后一条，
     截断却从块首数 180 字，于是把无关的第 2 条日志路径存成了决策。
  2. 截断从块首硬切 —— 长句里关键词在末尾时，存下来的前半段跟决策无关。
  3. 决策词太宽 —— `我建议`（列菜单）、`切到`（一个备选方案）被当成决策；
     反过来 `还没决定` 又被当成决策。

每条用例的期望值都来自真实出现过的那段文字，不是编的。
"""
import importlib.util
import unittest
from pathlib import Path

SLOTS_PY = Path(__file__).resolve().parent.parent / "slots.py"

spec = importlib.util.spec_from_file_location("boshi_slots_p31_ut", str(SLOTS_PY))
slots = importlib.util.module_from_spec(spec)
spec.loader.exec_module(slots)

# 真实输入 1：2026-10-20 13:18 那条助手答复里的 markdown 列表（原样节选）。
# 旧版把整块当一句 → decisions 里出现「**2. 你那个新窗口的日志**…」这条垃圾。
REAL_LIST_BLOCK = """## 二、当前可用的替代（config 里已有的）

- **2. 你那个新窗口的日志**（`~/.hermes-web-ui/.ekko/logs/default/ekko-agent.jsonl`） 会话 `mvg70a4gb6l60p`，provider `custom:radeon-cloud` / `DeepSeek-V4.1-Flash`
- 不想改也行——Studio 模型下拉手动切到 `bili-deepseek / deepseek-flash` 就能继续用，等 AMD 那边并发槽释放再切回来
- 13:18 复测还是 429，暂未恢复
"""

# 真实输入 2：用户一句真取舍，关键词落在长列表项**末尾**。
REAL_DECISION_TAIL = """- 前端登录页已经写完、后端三个接口也都通了、日志在 H:/logs/app.log、配置在 C:/cfg/app.yaml，这一块就定下来用 DuckDB 存快照
"""


class TestListItemsAreSeparate(unittest.TestCase):
    """bug①：列表项必须一行一句。"""

    def test_列表块不再整体算一句(self):
        sents = slots._sentences(REAL_LIST_BLOCK)
        self.assertEqual(len(sents), 3, sents)
        # 列表记号（"- " / "1. "）在切句后被剥掉（2026-10-20 22:5x，commit 974c20d）：
        # 槽位值要进 BM25，留着 "- " 只会把记号和相邻词粘成一个假 token；
        # 剥记号后仍是原文连续子串，不破坏「只搬运不生成」不变式。
        self.assertTrue(sents[0].startswith("**2. 你那个新窗口的日志**"), sents[0])
        self.assertIn("切到", sents[1])
        self.assertTrue(sents[2].startswith("13:18 复测"), sents[2])

    def test_切到不再算决策_整块不该进decisions(self):
        out = slots.extract(REAL_LIST_BLOCK)
        self.assertEqual(out["decisions"], [],
                         "「切到 X 就能继续用」是备选方案，不是决策")

    def test_引出行仍与下文合并(self):
        """不按换行切的原设计要保住：冒号引出行不能单独成为一句。"""
        text = "当前状态一句话：\n所有测试都过了，没有遗留问题。\n"
        sents = slots._sentences(text)
        self.assertEqual(len(sents), 1, sents)
        self.assertIn("所有测试都过了", sents[0])


class TestAnchoredClip(unittest.TestCase):
    """bug②：截断要以命中词为锚，不能永远从块首数 180 字。"""

    def test_关键词在长句末尾时被保住(self):
        out = slots.extract(REAL_DECISION_TAIL)
        self.assertEqual(len(out["decisions"]), 1, out["decisions"])
        got = out["decisions"][0]
        self.assertIn("DuckDB", got, got)
        self.assertLessEqual(len(got), slots._ITEM_CAP)

    def test_锚定截取仍是原文连续子串(self):
        out = slots.extract(REAL_DECISION_TAIL)
        for v in out["decisions"]:
            self.assertIn(v, REAL_DECISION_TAIL)

    def test_clip_at短句原样返回(self):
        self.assertEqual(slots._clip_at("决定用 A 方案", 2), "决定用 A 方案")


class TestDecisionVocabulary(unittest.TestCase):
    """bug③：决策词要收窄，且否定式不算决策。"""

    def test_我建议不算决策(self):
        out = slots.extract("我建议先用 bili-deepseek 顶着，等额度回来再说。")
        self.assertEqual(out["decisions"], [])

    def test_真取舍词仍然算决策(self):
        for s in ("这件事决定用 A 方案了。",
                  "结论是放弃 Chroma，改 PG。",
                  "参数已经定了，就按 0.85。",
                  "A 方案定了，剩下的下周说。",
                  "不再用 Ollama 做反推。"):
            with self.subTest(s=s):
                self.assertTrue(slots.extract(s)["decisions"], s)

    def test_定了的假阳性被挡住(self):
        """「定了」很常见，但多数不是取舍：指定/规定/固定/判定。"""
        for s in ("他指定了日志路径在 H:/logs 下。",
                  "文档里规定了字段顺序。",
                  "这个位置固定了，别再挪。",
                  "判定为可疑请求，直接丢掉。"):
            with self.subTest(s=s):
                self.assertEqual(slots.extract(s)["decisions"], [], s)

    def test_还没决定不算决策_归到待办(self):
        out = slots.extract("数据库还没决定用哪个，下周再定。")
        self.assertEqual(out["decisions"], [], "否定式不是决策")
        self.assertTrue(any("还没决定" in v for v in out["open_questions"]),
                        out["open_questions"])


class TestVerbatimInvariant(unittest.TestCase):
    """全槽位不变式：值必须是原文连续子串（只搬运、不生成）。"""

    def test_mixed_corpus_all_values_verbatim(self):
        text = REAL_LIST_BLOCK + "\n" + REAL_DECISION_TAIL + (
            "\n端口 8199 跑 PE，显存 11.2 GB，铁律是别在活跃服务上压测。")
        out = slots.extract(text)
        for key in slots.SLOT_KEYS:
            for v in out[key]:
                self.assertIn(v, text, f"{key} 的值不是原文子串: {v!r}")


if __name__ == "__main__":
    unittest.main(verbosity=2)
