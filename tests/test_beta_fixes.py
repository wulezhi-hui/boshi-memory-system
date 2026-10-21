# -*- coding: utf-8 -*-
"""β 缺陷修复单测（2026-10-19 缺陷验收）

β2 写侧碎片门  is_content_fragment + save 拦截分支
β2 读侧置信度  _rerank_items 写 relevance/confidence；降级态标 unknown
β3 换值折叠    _fold_value_changes 取新弃旧 + _cond 真列路由
纯零模型断言，不依赖 PG / 重排服务在线。
"""
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import boshi_core
import pg_bridge as pb


# ════════════════════════════ β2 写侧碎片门 ════════════════════════════
class TestContentFragment(unittest.TestCase):
    """无实词锚点的口语碎片要拦；带实词双字的真意图要放。"""

    def test_blocks_bare_fragments(self):
        for s in ["补", "？", "改", "这", "为什么", "怎么回事", "怎么这么慢",
                  "停", "修", "a", "，", "好", "的", "没", "啊", "嗯", "？？"]:
            self.assertTrue(boshi_core.is_content_fragment(s), f"应拦: {s!r}")

    def test_passes_real_intent(self):
        for s in ["装完没", "改完了吗", "婴儿辅食", "水晶怎么消磁", "皮鞋打蜡",
                  "开闸宽度改成100", "把 J 盘模型备份到 218",
                  "显存小的老显卡能不能跑音乐生成", "颂文要粗体消文释义不要粗体",
                  "好，按这个来", "降上下文 160", "2080Ti 显存爆了吗", "辅食怎么加"]:
            self.assertFalse(boshi_core.is_content_fragment(s), f"应放行: {s!r}")

    def test_cjk_not_treated_as_ascii_anchor(self):
        # 历史坑：isalnum() 对 CJK 返回 True → 单字「补」被误放行
        self.assertTrue(boshi_core.is_content_fragment("补"))
        self.assertFalse(boshi_core.is_content_fragment("2080Ti"))

    def test_save_gates_autocapture_user_only(self):
        added = []

        class FakeChroma:
            def add_memory(self, content, metadata=None, memory_id=None, upsert=False):
                added.append(content)
                return "fake-id"
            def resolve_profile(self):
                return "default"

        old = boshi_core._chroma_bridge
        boshi_core._chroma_bridge = FakeChroma()
        try:
            # 自动采集用户碎片 → 拦
            r1 = boshi_core.save("补", topic="conversation",
                                 metadata={"source": "hermes_plugin", "role": "user",
                                           "session_id": "s1"})
            self.assertTrue(r1.get("skipped"), "碎片应被拦")
            # 自动采集用户真意图 → 放行
            r2 = boshi_core.save("婴儿辅食怎么加", topic="conversation",
                                 metadata={"source": "hermes_plugin", "role": "user",
                                           "session_id": "s1"})
            self.assertTrue(r2.get("memory_id"), "真意图应入库")
            # 手动 MCP save 碎片 → 放行（尊重显式 save）
            r3 = boshi_core.save("补", topic="external",
                                 metadata={"source": "boshi_api"})
            self.assertTrue(r3.get("memory_id"), "手动 save 不应被拦")
            # 助手结论短文本 → 放行
            r4 = boshi_core.save("已修复", topic="assistant_conclusion",
                                 metadata={"source": "hermes_plugin",
                                           "role": "assistant"})
            self.assertTrue(r4.get("memory_id"), "助手结论不应被拦")
            self.assertEqual(len(added), 3, "只该入库 3 条")
        finally:
            boshi_core._chroma_bridge = old


# ════════════════════════════ β2 读侧置信度 ════════════════════════════
class TestConfidenceAnnotation(unittest.TestCase):
    """_fold_value_changes / 降级打标：纯函数断言（不打重排服务）。"""

    def test_fold_marks_superseded(self):
        items = [
            {"id": "old", "content": "开闸宽度当前设定为 40", "score": 0.05,
             "metadata": {"topic": "gate", "timestamp": 100}},
            {"id": "new", "content": "开闸宽度当前设定为 100", "score": 0.10,
             "metadata": {"topic": "gate", "timestamp": 200}},
        ]
        out = pb._fold_value_changes(items)
        order = [o["id"] for o in out]
        self.assertEqual(order, ["new", "old"], "新值应在前")
        old = out[1]
        self.assertTrue(old["metadata"]["superseded"])
        self.assertEqual(old["metadata"]["superseded_by"], "new")

    def test_fold_not_triggered_when_body_differs(self):
        # 场景 B：跨会话数值互斥（256 的 vllm vs 4% 的养老金）不是同一事实，不折叠
        items = [
            {"id": "a", "content": "降上下文 160", "score": 0.1,
             "metadata": {"topic": "conversation", "timestamp": 100}},
            {"id": "b", "content": "进度 100%", "score": 0.2,
             "metadata": {"topic": "conversation", "timestamp": 200}},
        ]
        out = pb._fold_value_changes(items)
        self.assertEqual([o["id"] for o in out], ["a", "b"], "去数字正文不同不折叠")

    def test_fold_same_topic_required(self):
        # 相同正文但不同 topic → 不折叠
        items = [
            {"id": "a", "content": "开闸宽度当前设定为 40", "score": 0.1,
             "metadata": {"topic": "gate", "timestamp": 100}},
            {"id": "b", "content": "开闸宽度当前设定为 100", "score": 0.2,
             "metadata": {"topic": "other", "timestamp": 200}},
        ]
        out = pb._fold_value_changes(items)
        self.assertEqual([o["id"] for o in out], ["a", "b"], "跨 topic 不折叠")

    def test_norm_value_body(self):
        self.assertEqual(pb._norm_value_body("开闸宽度当前设定为 40"),
                         pb._norm_value_body("开闸宽度当前设定为 100"))
        self.assertNotEqual(pb._norm_value_body("降上下文 160"),
                            pb._norm_value_body("进度 100"))


# ════════════════════════════ β3 检索契约：_cond 真列路由 ════════════════
class TestCondTrueColumns(unittest.TestCase):
    """session_id/topic/role/source 是真列，_cond 必须路由到真列而非 meta jsonb。

    历史 bug：早期误按 meta->>'session_id' 查 → 这些键写入时被 pop 出 meta，
    永远 NULL → where={"session_id":...} 恒返回空，β3 验收时实测命中。
    """

    def test_session_id_routes_to_true_column(self):
        sql, params = pb._cond("session_id", "s123")
        self.assertIn("session_id", sql)
        self.assertNotIn("meta", sql)
        self.assertEqual(params, ["s123"])

    def test_topic_role_source_route_to_true_columns(self):
        for key in ("topic", "role", "source"):
            sql, params = pb._cond(key, "v")
            self.assertIn(key, sql)
            self.assertNotIn("meta->>", sql)

    def test_dict_ops_on_true_columns(self):
        sql, params = pb._cond("topic", {"$eq": "conversation"})
        self.assertEqual(sql, "topic = %s")
        sql, params = pb._cond("role", {"$ne": "user"})
        self.assertIn("IS DISTINCT FROM", sql)

    def test_meta_keys_still_go_to_jsonb(self):
        sql, params = pb._cond("supersedes", "x")
        self.assertIn("meta->>'supersedes'", sql)

    def test_is_latest_still_true_column(self):
        sql, params = pb._cond("isLatest", True)
        self.assertIn("is_latest", sql)


# ═══════════════════ 重排服务按需拉起（2026-10-22）═══════════════════
class TestRerankAutostart(unittest.TestCase):
    """11437 没起时自动拉起；拉不起来必须静默降级、绝不抛异常。

    全部 mock 掉端口/Popen/nvidia-smi，不依赖真实服务与显卡。
    """

    def setUp(self):
        self._saved = dict(
            autostart=pb._RERANK_AUTOSTART,
            wait=pb._RERANK_AUTOSTART_WAIT,
            last_spawn=list(pb._RERANK_LAST_SPAWN),
            last_start=list(pb._RERANK_LAST_START),
            port_alive=pb._rerank_port_alive,
            ready=pb._rerank_ready,
            start=pb._start_rerank_service,
            vram=pb._vram_free_mib,
            log=pb._RERANK_LOG,
            status=pb._RERANK_STATUS.copy(),
        )
        # 单测不许往生产日志里写噪声，日志路径改指向临时文件
        pb._RERANK_LOG = os.path.join(tempfile.gettempdir(), "boshi_test_rerank.log")
        pb._RERANK_LAST_SPAWN[0] = 0.0
        pb._RERANK_LAST_START[0] = 0.0
        pb._RERANK_STATUS.update(ok=None, reason="", ts=0.0, detail="")
        self.port_up = [False]
        pb._rerank_port_alive = lambda timeout=0.4: self.port_up[0]
        pb._rerank_ready = lambda timeout=1.0: self.port_up[0]

    def tearDown(self):
        pb._RERANK_AUTOSTART = self._saved["autostart"]
        pb._RERANK_AUTOSTART_WAIT = self._saved["wait"]
        pb._RERANK_LAST_SPAWN[:] = self._saved["last_spawn"]
        pb._RERANK_LAST_START[:] = self._saved["last_start"]
        pb._rerank_port_alive = self._saved["port_alive"]
        pb._rerank_ready = self._saved["ready"]
        pb._start_rerank_service = self._saved["start"]
        pb._vram_free_mib = self._saved["vram"]
        pb._RERANK_LOG = self._saved["log"]
        pb._RERANK_STATUS.clear()
        pb._RERANK_STATUS.update(self._saved["status"])

    def test_already_up_does_not_spawn(self):
        self.port_up[0] = True
        spawned = []
        pb._start_rerank_service = lambda: spawned.append(1) or 1
        self.assertTrue(pb._ensure_rerank())
        self.assertEqual(spawned, [])

    def test_disabled_reports_service_down(self):
        pb._RERANK_AUTOSTART = False
        pb._start_rerank_service = lambda: self.fail("禁用后不应拉起")
        self.assertFalse(pb._ensure_rerank())
        self.assertEqual(pb._RERANK_STATUS["reason"], "service_down")

    def test_vram_low_skips_spawn(self):
        pb._vram_free_mib = lambda: pb._RERANK_VRAM_MIN - 1
        pb._start_rerank_service = lambda: self.fail("显存不足不应拉起")
        self.assertFalse(pb._ensure_rerank())
        self.assertEqual(pb._RERANK_STATUS["reason"], "vram_low")

    def test_spawn_failure_degrades_quietly(self):
        pb._vram_free_mib = lambda: 8000
        pb._start_rerank_service = lambda: (_ for _ in ()).throw(RuntimeError("找不到模型"))
        self.assertFalse(pb._ensure_rerank())          # 不抛异常
        self.assertEqual(pb._RERANK_STATUS["reason"], "autostart_failed")
        self.assertIn("找不到模型", pb._RERANK_STATUS["detail"])

    def test_spawn_then_ready(self):
        pb._vram_free_mib = lambda: 8000
        pb._start_rerank_service = lambda: (self.port_up.__setitem__(0, True), 4242)[1]
        self.assertTrue(pb._ensure_rerank())
        self.assertEqual(pb._RERANK_STATUS.get("reason"), "")

    def test_cooldown_blocks_second_spawn(self):
        pb._RERANK_AUTOSTART = True
        pb._vram_free_mib = lambda: 8000
        calls = []
        pb._start_rerank_service = lambda: calls.append(1) or 1   # 拉起成功但永不 ready
        pb._RERANK_AUTOSTART_WAIT = 0.3
        self.assertFalse(pb._ensure_rerank())     # 第一次：尝试拉起 → 超时
        self.assertFalse(pb._ensure_rerank())     # 第二次：冷却中，不再 Popen
        self.assertEqual(len(calls), 1)

    def test_rerank_items_degrades_when_unavailable(self):
        """拉不起来 → _rerank_items 原序返回，不抛，结果里 relevance 由上层标 unknown。"""
        pb._RERANK_AUTOSTART = False
        pb._vram_free_mib = lambda: 8000
        pb._RERANK_DOWN_UNTIL[0] = 0.0
        items = [{"content": "甲" * 50, "score": 0.3}, {"content": "乙" * 50, "score": 0.4}]
        out = pb._rerank_items("问句", items, 2)
        self.assertEqual([i["score"] for i in out], [0.3, 0.4])
        self.assertEqual(pb._RERANK_STATUS["reason"], "service_down")


if __name__ == "__main__":
    unittest.main(verbosity=2)
