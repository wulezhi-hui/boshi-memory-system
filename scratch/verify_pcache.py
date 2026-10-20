# -*- coding: utf-8 -*-
"""验证 per-query 召回缓存：不同 query 不再共用同一份召回；同一 query 仍命中缓存。"""
import os
import sys
import time
import types

sys.path.insert(0, r"C:/Users/wulezhi/.boshi/plugins")
sys.path.insert(0, r"C:/Users/wulezhi/.boshi")
os.chdir(r"C:/Users/wulezhi/.boshi")

# ---- stub agent.memory_provider / tools.registry（只测缓存逻辑，不连真实后端）----
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


class FakeCore:
    """假 core：按 query 返回不同内容，并记录调用次数。"""
    calls = []

    def search(self, query, top_k=5, source="all"):
        FakeCore.calls.append(query)
        return {"results": [{"content": "召回内容 for:" + query, "score": 0.9,
                             "source": "fake"}]}


p = boshi.BoshiMemoryProvider({})
p._core = FakeCore()
p._search_session_history = lambda q, limit=3: []      # 关掉历史会话检索
p._expand_query_search = lambda q, lines: []           # 关掉实体扩展检索

t0 = time.time()
p.queue_prefetch("第一个问题：queryA 端口 8199")
time.sleep(1.2)                                        # 等后台线程写完
a = p.prefetch("第一个问题：queryA 端口 8199")
b = p.prefetch("第二个问题：queryB 微调显存")
c = p.prefetch("第一个问题：queryA 端口 8199  ")        # 空白差异 → 应命中同一指纹
print("A:", a.replace("\n", " | ")[:70])
print("B:", b.replace("\n", " | ")[:70])
print("C:", c.replace("\n", " | ")[:70])
print("检索调用次数:", len(FakeCore.calls), "→", FakeCore.calls)
print("A/B 不同:", a != b, "| A/C 相同:", a == c)
print("缓存条数:", len(p._pcache))

# 同步兜底分支：没预热过的 query 第一次真检索，第二次走缓存
d1 = p.prefetch("第三个问题：queryC 养老金")
n1 = len(FakeCore.calls)
d2 = p.prefetch("第三个问题：queryC 养老金")
print("兜底检索后调用数:", n1, "→第二次:", len(FakeCore.calls),
      "| 内容一致:", d1 == d2, "| 缓存条数:", len(p._pcache))
