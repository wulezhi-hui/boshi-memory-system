# -*- coding: utf-8 -*-
"""P2 时间维度 + 白盒运维面（edit/export/restore/resolve_conflict）的单测。

只验"入口是否接通、参数是否落地"，不连库：
- `_time_bounds` 把 since/until 翻成 created_at 真列条件（Unix 秒 / ISO）
- `boshi_cli._parse_when` 认 7d/24h/30m/1.5w/epoch/日期/ISO
- `search_memory`/`hybrid_search` 签名带 since/until，`boshi_core.search` 透传
- 白盒四个 API 在 pg_bridge 里存在，且被 chroma_bridge 的后端切换表接管
  （踩过：新增函数忘了进 `_PUBLIC_API`，`boshi_core.edit()` 会回“当前后端不支持就地编辑”）
"""
import inspect
import os
import sys
import unittest

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)

import pg_bridge
import boshi_core
import boshi_cli


class TimeBoundsTest(unittest.TestCase):
    def test_none_returns_empty(self):
        sql, params = pg_bridge._time_bounds()
        self.assertEqual(sql, "")
        self.assertEqual(params, [])

    def test_unix_seconds(self):
        sql, params = pg_bridge._time_bounds(1792461898, 1792548298)
        self.assertIn("created_at >=", sql)
        self.assertIn("created_at <=", sql)
        self.assertEqual(params, [1792461898.0, 1792548298.0])

    def test_iso_string(self):
        import datetime
        sql, params = pg_bridge._time_bounds("2026-10-01T00:00:00")
        self.assertIn("created_at >=", sql)
        self.assertEqual(len(params), 1)
        self.assertAlmostEqual(
            params[0], datetime.datetime.fromisoformat("2026-10-01T00:00:00").timestamp(), 1)

    def test_numeric_string(self):
        _, params = pg_bridge._time_bounds("1792461898")
        self.assertEqual(params, [1792461898.0])

    def test_garbage_ignored(self):
        sql, params = pg_bridge._time_bounds("不是时间")
        self.assertEqual(sql, "")
        self.assertEqual(params, [])

    def test_only_until(self):
        sql, params = pg_bridge._time_bounds(until=100)
        self.assertNotIn("created_at >=", sql)
        self.assertIn("created_at <=", sql)
        self.assertEqual(params, [100.0])


class ParseWhenTest(unittest.TestCase):
    def test_relative(self):
        now = __import__("time").time()
        for s, span in (("7d", 7 * 86400), ("24h", 86400), ("30m", 1800), ("1.5w", 1.5 * 604800)):
            v = boshi_cli._parse_when(s)
            self.assertAlmostEqual(v, now - span, delta=120, msg=s)

    def test_absolute(self):
        v = boshi_cli._parse_when("2026-10-01")
        self.assertIsInstance(v, float)
        v2 = boshi_cli._parse_when("2026-10-01 12:30")
        self.assertGreater(v2, v)
        w = boshi_cli._parse_when("2026/10/01")
        self.assertEqual(v, w)

    def test_epoch_and_iso(self):
        self.assertEqual(boshi_cli._parse_when("1792461898"), 1792461898.0)
        self.assertIsInstance(boshi_cli._parse_when("2026-10-01T08:00:00Z"), float)

    def test_none_and_garbage(self):
        self.assertIsNone(boshi_cli._parse_when(None))
        self.assertIsNone(boshi_cli._parse_when(""))
        self.assertIsNone(boshi_cli._parse_when("下周"))


class SignatureTest(unittest.TestCase):
    def test_backend_search_has_window(self):
        for fn in (pg_bridge.search_memory, pg_bridge.hybrid_search):
            sig = inspect.signature(fn)
            self.assertIn("since", sig.parameters, fn.__name__)
            self.assertIn("until", sig.parameters, fn.__name__)

    def test_core_search_passes_window(self):
        src = inspect.getsource(boshi_core.search)
        self.assertGreaterEqual(src.count("since=since"), 2,
                                "core.search 的三个分支都要透传 since/until")

    def test_whitebox_api_exists(self):
        for name in ("edit_memory", "export_snapshot", "export_markdown", "restore_snapshot"):
            self.assertTrue(callable(getattr(pg_bridge, name, None)), name)
            self.assertIn(name, __import__("chroma_bridge")._PUBLIC_API, name)

    def test_core_wrappers_reachable(self):
        import chroma_bridge
        # 后端切换后，core 走的 cb.<name> 必须真实存在（否则静默回"不支持"）
        for name in ("edit_memory", "export_snapshot", "export_markdown", "restore_snapshot"):
            self.assertTrue(callable(getattr(chroma_bridge, name, None)), name)


if __name__ == "__main__":
    unittest.main()
