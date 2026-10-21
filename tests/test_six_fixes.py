"""六项开放问题的回归测试（2026-10-19）。

对应问题清单：
  #1 同主题更新语义缺冲突裁决 → _mark_value_conflicts：暴露冲突、保证新值不排在旧值后
  #2 CLI 进程身份与 MCP/插件不一致 → resolve_profile_info：身份必须自报来源
  #3 闲置卸载计时器不可观测 → rerank_status 暴露 idle_sec / asleep_likely / wakes
  #4 delete 幂等但无状态区分 → delete() 三态 deleted / not_found / error
  #5 裸进程静默降级 → doctor.check_capability（本文件验其依赖的能力字段可读）
  #6 自定义 relation 读回折叠成 related → relation_raw 落库 + 读回原串

设计原则：这些缺陷的共同点是「坏了不报错，只是静默退化」。所以断言的是
**可观测性**（状态字段/排序/原串是否还在），不是内部实现细节。
"""
import os
import subprocess
import sys
import time
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import boshi_core as bc
import pg_bridge as pb


# ── #4 delete 三态 ──────────────────────────────────────────────

class _FakeChroma(object):
    def __init__(self, ret=None, exc=None):
        self._ret = ret
        self._exc = exc

    def delete_memory(self, memory_id):
        if self._exc is not None:
            raise self._exc
        return self._ret


class TestDeleteStatus(unittest.TestCase):
    def setUp(self):
        self._orig = bc._get_chroma

    def tearDown(self):
        bc._get_chroma = self._orig

    def _with(self, ret=None, exc=None):
        bc._get_chroma = lambda: _FakeChroma(ret, exc)

    def test_deleted(self):
        self._with(ret=1)
        r = bc.delete("mid-1")
        self.assertTrue(r["success"])
        self.assertEqual(r["status"], "deleted")
        self.assertEqual(r["rows"], 1)

    def test_not_found_must_not_report_success(self):
        # 本段的核心：删不存在的 ID 曾经返回 {success: True}，
        # 于是「5 条测试残留」被一次无效批量删除误判成「清理完成」。
        self._with(ret=0)
        r = bc.delete("nope")
        self.assertFalse(r["success"])
        self.assertEqual(r["status"], "not_found")
        self.assertEqual(r["rows"], 0)

    def test_backend_error(self):
        self._with(exc=RuntimeError("boom"))
        r = bc.delete("mid-2")
        self.assertFalse(r["success"])
        self.assertEqual(r["status"], "error")
        self.assertIn("RuntimeError", r["error"])

    def test_negative_rows_is_error(self):
        self._with(ret=-1)
        r = bc.delete("mid-3")
        self.assertFalse(r["success"])
        self.assertEqual(r["status"], "error")

    def test_none_means_unverifiable_but_deleted(self):
        self._with(ret=None)
        r = bc.delete("mid-4")
        self.assertTrue(r["success"])
        self.assertFalse(r["verified"])


# ── #6 自定义 relation 不再折叠 ────────────────────────────────

class TestRelationRaw(unittest.TestCase):
    def setUp(self):
        import chroma_bridge
        self.cb = chroma_bridge
        self._add = chroma_bridge.add_memory
        self._allrel = getattr(chroma_bridge, "get_all_relations", None)
        self.captured = {}

        def fake_add(content, metadata=None, memory_id=None, **kw):
            self.captured["content"] = content
            self.captured["meta"] = dict(metadata or {})
            return memory_id

        chroma_bridge.add_memory = fake_add
        chroma_bridge.get_all_relations = lambda *a, **k: []

    def tearDown(self):
        self.cb.add_memory = self._add
        if self._allrel is not None:
            self.cb.get_all_relations = self._allrel

    def test_custom_relation_keeps_raw_string(self):
        import knowledge_graph as kg
        kg.add_relation("a", "b", "验收自测边", reason="自测")
        meta = self.captured["meta"]
        # 4 类语义里归一到 related（图查询/推理仍可用），但原串必须还在
        self.assertEqual(meta["rel_type"], "related")
        self.assertEqual(meta["relation"], "related")
        self.assertEqual(meta["relation_raw"], "验收自测边")

    def test_relation_column_source_present(self):
        # #6b 的根因是写入方没有 relation 键 → graph_edges.relation 全是空。
        # 这里断言写入的 metadata 一定带 relation（PG 侧取值的第一优先级）。
        import knowledge_graph as kg
        kg.add_relation("a", "b", "updates")
        self.assertIn("relation", self.captured["meta"])
        self.assertEqual(self.captured["meta"]["relation"], "updates")
        self.assertEqual(self.captured["meta"]["relation_raw"], "updates")


class _FakeCursor(object):
    def __init__(self, sink):
        self.sink = sink

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params=None):
        self.sink.append((sql, params))

    def fetchall(self):
        return []


class _FakeConn(object):
    def __init__(self, sink):
        self.sink = sink

    def cursor(self):
        return _FakeCursor(self.sink)


class TestPgEdgeRelationColumn(unittest.TestCase):
    """不连库验 SQL 参数：relation 列必须落真值（此前 8.4 万条全空）。"""

    def setUp(self):
        self._conn, self._ef = pb._conn, pb._ef
        self.sink = []

        def fake_conn():
            return _FakeConn(self.sink)

        pb._conn = fake_conn
        pb._ef = lambda: (lambda texts: [[0.0] * 1024 for _ in texts])

    def tearDown(self):
        pb._conn, pb._ef = self._conn, self._ef

    def _insert_params(self):
        for sql, params in self.sink:
            if "INSERT INTO boshi.graph_edges" in (sql or ""):
                return params
        return None

    def test_relation_raw_wins(self):
        # #6 活体验证补的规则：relation 列存**原串**，归一值只在读侧算 relation_norm。
        # 否则自定义关系（如「验收自测边」）落库就成了 related，读回永远丢掉原意。
        pb.add_memory("a --[related]--> b", metadata={
            "type": "relation", "entity_a": "a", "entity_b": "b",
            "rel_type": "related", "relation_raw": "验收自测边",
        }, memory_id="e1")
        params = self._insert_params()
        self.assertIsNotNone(params)
        # (id, entity_a, entity_b, relation, content, embedding, version)
        self.assertEqual(params[3], "验收自测边")

    def test_falls_back_to_rel_type(self):
        # 老调用方没有 relation_raw → 退回 relation → rel_type
        pb.add_memory("a --[x]--> b", metadata={
            "type": "relation", "entity_a": "a", "entity_b": "b",
            "rel_type": "derives",
        }, memory_id="e3")
        params = self._insert_params()
        self.assertEqual(params[3], "derives")

    def test_relation_key_wins(self):
        pb.add_memory("a --[x]--> b", metadata={
            "type": "relation", "entity_a": "a", "entity_b": "b",
            "relation": "extends", "rel_type": "related",
        }, memory_id="e2")
        params = self._insert_params()
        self.assertEqual(params[3], "extends")


class TestFindEdgeDedup(unittest.TestCase):
    """#6 尾部：PG 后端 add_relation 去重。原实现读回 metadata 无 from_id/to_id，
    且 get_all_relations 带 LIMIT 10000 < 边表 8.4 万行 → 同一实体对每次写都新建边。"""

    def setUp(self):
        self._conn = pb._conn
        self.sink = []
        self.rows = []

        class _Cur(_FakeCursor):
            def fetchall(_self):
                return self.rows

            def fetchone(_self):
                return self.rows[0] if self.rows else None

        class _C(_FakeConn):
            def cursor(_self):
                return _Cur(self.sink)

        pb._conn = lambda: _C(self.sink)

    def tearDown(self):
        pb._conn = self._conn

    def test_raw_first_then_norm_for_standard(self):
        self.rows = [("edge-1",)]
        got = pb.find_edge("a", "b", "related", "引用")
        self.assertEqual(got, "edge-1")
        sql, params = self.sink[-1]
        self.assertEqual(params, ["a", "b", "引用", "related"])
        self.assertIn("relation = %s", sql)
        self.assertIn("LIMIT 1", sql)

    def test_no_raw_uses_normalized_case(self):
        self.rows = []
        self.assertEqual(pb.find_edge("a", "b", "related", None), "")
        sql, params = self.sink[-1]
        self.assertEqual(params, ["a", "b", "related"])
        self.assertIn("CASE WHEN relation IN", sql)

    def test_error_returns_empty(self):
        def boom():
            raise RuntimeError("db down")
        pb._conn = boom
        self.assertEqual(pb.find_edge("a", "b", "related", "x"), "")


# ── #1 冲突暴露 ────────────────────────────────────────────────

def _item(mid, content, topic, ts, score=1.0, **extra):
    md = {"topic": topic, "timestamp": ts, "is_latest": True, "score": score}
    md.update(extra)
    return {"id": mid, "content": content, "metadata": md, "score": score}


class TestValueConflictExposure(unittest.TestCase):
    def setUp(self):
        self._cos = pb._doc_cos

    def tearDown(self):
        pb._doc_cos = self._cos

    def _hi(self, *a, **k):
        return {}

    def test_newer_value_ranks_first_and_both_flagged(self):
        old = _item("old", "云闸的开闸宽度当前设定为 40 层", "云闸宽度", 1000.0, 6.88)
        new = _item("new", "云闸的开闸宽度已调整为 100 层", "云闸宽度", 2000.0, 5.26)
        # 同构度足够（真实句子的余弦在 0.9 以上），这里直接给高分
        pb._doc_cos = lambda texts: {("old", "new"): 0.95}
        out = pb._mark_value_conflicts([old, new])
        self.assertEqual(out[0]["id"], "new", "较新的一条必须排在前面")
        for it in out:
            self.assertTrue(it["metadata"].get("conflict"))
            self.assertEqual(it["metadata"].get("conflict_role"),
                             "newer" if it["id"] == "new" else "older")
            self.assertIn("conflict_with", it["metadata"])
            self.assertIn("conflict_values", it["metadata"])
            self.assertIn("未自动判定真伪", it["metadata"]["conflict_note"])

    def test_no_score_mutation(self):
        # 只调位置，不改分、不写 is_latest=false——自动判真伪是另一套（有误杀风险）
        old = _item("old", "端口设为 8080", "端口", 1000.0, 2.0)
        new = _item("new", "端口改成 9000", "端口", 2000.0, 1.0)
        pb._doc_cos = lambda texts: {("old", "new"): 0.9}
        out = pb._mark_value_conflicts([old, new])
        by_id = {i["id"]: i for i in out}
        self.assertEqual(by_id["old"]["score"], 2.0)
        self.assertEqual(by_id["new"]["score"], 1.0)
        self.assertIsNot(by_id["old"]["metadata"].get("is_latest"), False)

    def test_low_similarity_not_flagged(self):
        a = _item("a", "云闸的开闸宽度为 40 层", "云闸宽度", 1000.0)
        b = _item("b", "云闸的开闸宽度为 100 层", "云闸宽度", 2000.0)
        pb._doc_cos = lambda texts: {("a", "b"): 0.50}
        out = pb._mark_value_conflicts([a, b])
        self.assertFalse(any(i["metadata"].get("conflict") for i in out))

    def test_same_values_not_flagged(self):
        a = _item("a", "池深设为 100", "池深", 1000.0)
        b = _item("b", "池深调整为 100", "池深", 2000.0)
        pb._doc_cos = lambda texts: {("a", "b"): 0.99}
        out = pb._mark_value_conflicts([a, b])
        self.assertFalse(any(i["metadata"].get("conflict") for i in out))

    def test_close_timestamps_not_flagged(self):
        # 同一句话连写两遍：时间戳相差 ≤60s 时宁可漏报也不误报
        a = _item("a", "池深设为 100", "池深", 1000.0)
        b = _item("b", "池深设为 200", "池深", 1030.0)
        pb._doc_cos = lambda texts: {("a", "b"): 0.99}
        out = pb._mark_value_conflicts([a, b])
        self.assertFalse(any(i["metadata"].get("conflict") for i in out))

    def test_superseded_rows_skipped(self):
        a = _item("a", "池深设为 100", "池深", 1000.0, superseded=True)
        b = _item("b", "池深设为 200", "池深", 2000.0)
        pb._doc_cos = lambda texts: {("a", "b"): 0.99}
        out = pb._mark_value_conflicts([a, b])
        self.assertFalse(any(i["metadata"].get("conflict") for i in out))

    def test_disabled_by_env(self):
        self.assertFalse(pb._CONFLICT_ON and os.environ.get("BOSHI_CONFLICT_EXPOSE") == "0")


class TestConflictSlotGate(unittest.TestCase):
    """护栏 1/2：归属键不同 → 只打标不调序；判不出 → 沿用旧行为；只交换配对内部两条。"""

    def setUp(self):
        self._cos = pb._doc_cos
        self._gate = pb._CONFLICT_SLOT_GATE

    def tearDown(self):
        pb._doc_cos = self._cos
        pb._CONFLICT_SLOT_GATE = self._gate

    def _cos_hi(self, *ids):
        return lambda texts: {(a, b): 0.95 for a in ids for b in ids if a != b}

    def test_different_subjects_marked_but_order_kept(self):
        # 同模板、同 profile、余弦 0.95，但数字属于两个不同守卫 → 不得把新的排前面
        a = _item("a", "大眼守卫每小时眨眼 72 次", "守卫频率", 1000.0)
        b = _item("b", "独眼守卫每小时眨眼 145 次", "守卫频率", 2000.0)
        pb._doc_cos = self._cos_hi("a", "b")
        out = pb._mark_value_conflicts([a, b])
        self.assertEqual([i["id"] for i in out], ["a", "b"], "分属不同对象 → 位置不动")
        for it in out:
            self.assertTrue(it["metadata"].get("conflict"), "仍要打标，让人知道有不同数值")
            self.assertEqual(it["metadata"].get("conflict_role"), "unknown")
            self.assertFalse(it["metadata"].get("conflict_same_slot"))
            self.assertIn("归属不同对象", it["metadata"]["conflict_note"])

    def test_ports_on_different_hosts_not_reordered(self):
        a = _item("a", "218 端口 11424 的部署记录", "部署", 1000.0)
        b = _item("b", "219 端口 5442 的部署记录", "部署", 2000.0)
        pb._doc_cos = self._cos_hi("a", "b")
        out = pb._mark_value_conflicts([a, b])
        self.assertEqual([i["id"] for i in out], ["a", "b"])
        self.assertEqual({i["metadata"].get("conflict_role") for i in out}, {"unknown"})

    def test_same_attr_reorders_even_if_one_side_omits_subject(self):
        # β4 的原始场景：新值那句只说「已调整为 100 层」，判不出归属键 → 沿用旧行为
        a = _item("a", "云闸的开闸宽度当前设定为 40 层", "云闸宽度", 1000.0)
        b = _item("b", "已调整为 100 层", "云闸宽度", 2000.0)
        pb._doc_cos = self._cos_hi("a", "b")
        out = pb._mark_value_conflicts([a, b])
        self.assertEqual(out[0]["id"], "b", "同一设定换值仍要让新值在前")
        self.assertEqual({i["metadata"].get("conflict_role") for i in out},
                         {"older", "newer"})
        self.assertIsNone(out[0]["metadata"].get("conflict_same_slot"),
                          "判不出归属键时标 None，不假装已确认同槽位")

    def test_swap_only_touches_the_pair(self):
        # 护栏 2：只交换配对两条的位置，中间那条下标不变，两条都还在结果里
        a = _item("a", "云闸的开闸宽度为 40 层", "云闸宽度", 1000.0)
        mid = _item("m", "云闸日志在 J:/logs 下", "云闸宽度", 1500.0)
        b = _item("b", "云闸的开闸宽度为 100 层", "云闸宽度", 2000.0)
        pb._doc_cos = self._cos_hi("a", "m", "b")
        out = pb._mark_value_conflicts([a, mid, b])
        self.assertEqual([i["id"] for i in out], ["b", "m", "a"])
        self.assertEqual(len(out), 3)

    def test_gate_off_restores_legacy_reorder(self):
        pb._CONFLICT_SLOT_GATE = False
        a = _item("a", "大眼守卫每小时眨眼 72 次", "守卫频率", 1000.0)
        b = _item("b", "独眼守卫每小时眨眼 145 次", "守卫频率", 2000.0)
        pb._doc_cos = self._cos_hi("a", "b")
        out = pb._mark_value_conflicts([a, b])
        self.assertEqual(out[0]["id"], "b", "关掉闸门＝旧行为")


# ── #3 卸载计时器可观测 ────────────────────────────────────────

class TestRerankVisibility(unittest.TestCase):
    def test_keys_present(self):
        st = pb.rerank_status()
        for k in ("sleep_after_sec", "idle_sec", "asleep_likely", "wakes", "last_wake"):
            self.assertIn(k, st, "rerank_status 必须暴露 %s" % k)

    def test_asleep_after_idle(self):
        old = pb._RERANK_LAST_OK[0]
        try:
            pb._RERANK_LAST_OK[0] = time.time() - (pb._RERANK_SLEEP_AFTER + 30)
            st = pb.rerank_status()
            self.assertGreaterEqual(st["idle_sec"], pb._RERANK_SLEEP_AFTER)
            self.assertTrue(st["asleep_likely"])
        finally:
            pb._RERANK_LAST_OK[0] = old

    def test_wake_counter_increments(self):
        n0 = pb._RERANK_WAKE_COUNT[0]
        pb._RERANK_WAKE_COUNT[0] = n0 + 1
        pb._RERANK_LAST_WAKE[0] = time.time()
        st = pb.rerank_status()
        self.assertEqual(st["wakes"], n0 + 1)
        self.assertGreater(st["last_wake"], 0)


# ── #2 身份解析 ────────────────────────────────────────────────

class TestProfileIdentity(unittest.TestCase):
    def setUp(self):
        import chroma_bridge
        self.cb = chroma_bridge
        self._env = {k: os.environ.get(k) for k in ("BOSHI_PROFILE", "HERMES_HOME")}

    def tearDown(self):
        for k, v in self._env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def test_env_wins(self):
        os.environ["BOSHI_PROFILE"] = "opencode"
        info = self.cb.resolve_profile_info()
        self.assertEqual(info["profile"], "opencode")
        self.assertEqual(info["source"], "env")

    def test_hermes_home_profile(self):
        os.environ.pop("BOSHI_PROFILE", None)
        os.environ["HERMES_HOME"] = "C:/Users/x/AppData/Local/hermes/profiles/zuojia"
        info = self.cb.resolve_profile_info()
        self.assertEqual(info["profile"], "zuojia")
        self.assertEqual(info["source"], "hermes_home")

    def test_bare_process_defaults_and_says_so(self):
        # 不改默认值（改了会悄悄扩大别人的检索面），但必须自报「我是默认身份」
        os.environ.pop("BOSHI_PROFILE", None)
        os.environ["HERMES_HOME"] = "C:/Users/x/AppData/Local/hermes"
        info = self.cb.resolve_profile_info()
        self.assertEqual(info["profile"], "default")
        self.assertEqual(info["source"], "default")
        self.assertIn("default_scope", info)

    def test_drift_is_reported(self):
        os.environ["BOSHI_PROFILE"] = "opencode"
        os.environ["HERMES_HOME"] = "C:/Users/x/AppData/Local/hermes/profiles/zuojia"
        info = self.cb.resolve_profile_info()
        self.assertTrue(info["differs_from_hermes_home"])
        self.assertEqual(info["hermes_home_profile"], "zuojia")

    def test_status_carries_identity(self):
        st = bc.status()
        self.assertIn("identity", st)
        self.assertIn("profile", st["identity"])


# ── #5 静默降级可视性 ─────────────────────────────────────────

class TestCapabilityProbe(unittest.TestCase):
    def test_pg_exposes_capability_inputs(self):
        # check_capability 依赖这三样，缺一个就会静默变成「能力未知」
        self.assertTrue(callable(getattr(pb, "_ef", None)))
        st = pb.rerank_status()
        self.assertIn("enabled", st)
        self.assertIn("ok", st)


if __name__ == "__main__":
    unittest.main()
