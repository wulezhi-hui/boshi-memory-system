# -*- coding: utf-8 -*-
"""本会话态注入（_session_state）的接线与边界测试。

背景：bili/ACP 在代理层折叠上下文，只从**最旧**那头切；Hermes 原生 compaction
不触发，所以 `on_pre_compress` 跑不到、本会话没有 task_conclusion。摘要里最容易
被抹平的是"这个任务最初要干什么"。`_session_state` 每轮查一次纯 SQL，注入
「任务起点（首条用户原话）+ 最近 4 行 + 最新事实层」，600 字上限，
BOSHI_SESSION_STATE=0 可回滚。

覆盖：
- 拼接顺序 mm → ss → recall；
- 真实库：本会话能取到、有表头、不超 600 字、时间正序；
- 开关回滚、空 session_id、查库异常三个"不能拖垮召回"的分支；
- 超长从最旧丢、单行截断；
- 缓存指纹并入 session_id（跨会话不串味）。
"""
import os
import sys
import types
import unittest

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(HERE, "plugins"))
sys.path.insert(0, HERE)

# ---- stub agent.memory_provider / tools.registry ----
if "agent" not in sys.modules:
    agent = types.ModuleType("agent")
    mp = types.ModuleType("agent.memory_provider")

    class MemoryProvider:
        def __init__(self, config=None):
            pass

    class RecallStatus:
        def __init__(self, *a, **k):
            self.args = a

    mp.MemoryProvider = MemoryProvider
    mp.RecallStatus = RecallStatus
    mp.is_trivial_prompt = lambda s: False
    agent.memory_provider = mp
    tools = types.ModuleType("tools")
    reg = types.ModuleType("tools.registry")
    reg.tool_error = lambda msg, **k: {"error": msg}
    tools.registry = reg
    sys.modules.update({"agent": agent, "agent.memory_provider": mp,
                        "tools": tools, "tools.registry": reg})

import boshi  # noqa: E402

CAP = boshi.BoshiMemoryProvider._SESSION_STATE_CAP


# ---- 按 RPC 缝打桩：_session_state 走 `self.p._core.rpc_session_state(...)`，
#      返回 {"origin","rows","fact"}（ts 已是 "HH:MM" 字符串，服务器侧格式化）。----
def _tsstr(hh, mm=0):
    return f"{hh:02d}:{mm:02d}"


def _rowd(topic, text, hh, mm=0):
    return {"topic": topic, "content": text, "ts": _tsstr(hh, mm)}


class _FakeCore:
    """替身 _VenvCore：只在 rpc_session_state 上按注入的 data 应答，可模拟异常。"""

    def __init__(self, data=None, raise_exc=None):
        self._data = data or {"origin": None, "rows": [], "fact": None}
        self.raise_exc = raise_exc

    def rpc_session_state(self, session_id, profile, tail_n=4):
        if self.raise_exc is not None:
            raise self.raise_exc
        return self._data

    def search(self, *a, **k):
        return {"total": 0, "results": []}

    def save(self, *a, **k):
        return {}


MISSING = object()   # 库里没有这条（dict 里对应 None）


class HelperTest(unittest.TestCase):
    def setUp(self):
        self.p = boshi.BoshiMemoryProvider({})
        self.p._profile = "default"

    def tearDown(self):
        self.p._core = None
        os.environ.pop("BOSHI_SESSION_STATE", None)

    def _wire(self, origin, rows, fact):
        data = {
            "origin": None if origin is MISSING else origin,
            "rows": rows,
            "fact": None if fact is MISSING else fact,
        }
        self.p._core = _FakeCore(data)

    def _wire_error(self, exc):
        self.p._core = _FakeCore(raise_exc=exc)

    # 1) 拼接顺序
    def test_with_mm_order(self):
        out = boshi.BoshiMemoryProvider._with_mm("RECALL", "MM", "SS")
        self.assertEqual(out, "MM\n\nSS\n\nRECALL")
        self.assertEqual(boshi.BoshiMemoryProvider._with_mm("RECALL", "", ""), "RECALL")
        self.assertEqual(boshi.BoshiMemoryProvider._with_mm("", "MM", "SS"), "MM\n\nSS")

    # 2) 回滚开关 / 空 id（都不触碰数据层）
    def test_switch_and_empty(self):
        os.environ["BOSHI_SESSION_STATE"] = "0"
        self.p._core = _FakeCore()
        self.assertEqual(self.p._session_state("s1"), "")
        os.environ.pop("BOSHI_SESSION_STATE")
        self.assertEqual(self.p._session_state(""), "")

    # 3) RPC 异常不能抛
    def test_db_error_is_silent(self):
        self._wire_error(RuntimeError("pg down"))
        self.assertEqual(self.p._session_state("s1"), "")

    # 4) 渲染：表头、起点在首行、时间正序、角色标签、单行截断
    def test_render_origin_first_then_chronological(self):
        origin = {"content": "最早的那句话" + "起" * 200, "ts": _tsstr(9)}
        rows = [_rowd("conversation", "第一问" + "甲" * 200, 9, 1),
                _rowd("assistant_conclusion", "第一答" + "乙" * 300, 9, 2)]
        self._wire(origin, rows, {"content": "决策：走 PG", "ts": _tsstr(9, 3)})
        out = self.p._session_state("s1")
        self.assertTrue(out.startswith("## 本会话当前任务"))
        body = out.split("\n", 1)[1].split("\n")
        self.assertIn("[任务起点] 用户：最早的那句话", body[0])
        self.assertIn("用户：第一问", body[1])         # 时间正序：用户话在结论之前
        self.assertIn("我的结论：第一答", body[2])
        self.assertIn("最近一次事实层（09:03）：决策：走 PG", out)
        self.assertLessEqual(len(out), CAP)

    # 5) 超长先丢最近行里最旧的，起点永不丢
    def test_trim_tail_first_keeps_origin(self):
        origin = {"content": "任务起点原话", "ts": _tsstr(9)}
        rows = [_rowd("conversation", f"第{i}问" + "字" * 90, 9, i) for i in (1, 2, 3, 4, 5)]
        self._wire(origin, rows, {"content": "事实" * 100, "ts": _tsstr(9, 6)})
        out = self.p._session_state("s1")
        self.assertLessEqual(len(out), CAP)
        self.assertIn("任务起点原话", out)             # 起点必须留下
        self.assertIn("第5问", out)                    # 最新的必须留下
        self.assertNotIn("第1问", out)                 # 最近行里最旧的被丢

    # 6) 起点与最近行重合时不重复
    def test_origin_dedupe(self):
        same = {"content": "只有一轮的会话", "ts": _tsstr(9)}
        rows = [{"topic": "conversation", "content": "只有一轮的会话", "ts": _tsstr(9)}]
        self._wire(same, rows, MISSING)
        out = self.p._session_state("s1")
        self.assertEqual(out.count("只有一轮的会话"), 1)

    # 7) 只有事实层也必须能出（不能因为没原文就整块丢掉）
    def test_fact_only(self):
        self._wire(MISSING, [], {"content": "决策：只留事实", "ts": _tsstr(9, 10)})
        out = self.p._session_state("s1")
        self.assertIn("最近一次事实层（09:10）", out)

    # 8) 啥都没有 → 空串，不占上下文
    def test_all_empty(self):
        self._wire(MISSING, [], MISSING)
        self.assertEqual(self.p._session_state("s1"), "")

    # 9) 缓存指纹并入 session_id
    def test_qkey_includes_session(self):
        k = boshi.BoshiMemoryProvider._qkey
        self.assertNotEqual(k("同一句话", "sessA"), k("同一句话", "sessB"))
        self.assertEqual(k("同一句话 ", "sessA"), k("同一句话", "sessA"))
        self.assertEqual(k("同一句话"), k("同一句话", ""))

    # 10) 不同会话的同名 query 不共用缓存
    def test_pcache_isolated_by_session(self):
        p = self.p
        p._pcache_put("问题", "A 会话的召回", 1, "sessA")
        self.assertIsNone(p._pcache_get("问题", "sessB"))
        self.assertEqual(p._pcache_get("问题", "sessA")[0], "A 会话的召回")


class RealDbTest(unittest.TestCase):
    """真实库：本会话（mtntnmgwkbvkac）应能取到会话态（走真 RPC 子进程）。"""

    SID = "mtntnmgwkbvkac"

    def setUp(self):
        self.p = boshi.BoshiMemoryProvider({})
        self.p._profile = "default"
        self.p.initialize(self.SID)   # 拉起 boshi venv RPC 子进程

    def tearDown(self):
        self.p.shutdown()

    def test_real_session_state(self):
        out = self.p._session_state(self.SID)
        if not out:
            self.skipTest("库里没有该会话的行（环境相关，跳过）")
        self.assertLessEqual(len(out), CAP)
        self.assertIn("本会话当前任务", out)
        self.assertIn("[任务起点] 用户：", out)


if __name__ == "__main__":
    unittest.main(verbosity=2)
