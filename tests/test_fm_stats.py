# -*- coding: utf-8 -*-
"""P2+ 打点 `_fm_stats`：不连库，用假连接核对 upsert 参数与开关。

跑法：cd ~/.boshi && ./venv/Scripts/python.exe -m unittest tests.test_fm_stats -v
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("BOSHI_FIELD_MATCH", "1")

import pg_bridge  # noqa: E402


class FakeConn:
    def __init__(self):
        self.calls = []

    def execute(self, sql, params=None):
        self.calls.append((sql, params))


def items(*spec):
    """spec: (id, score, hits)"""
    return [{"id": i, "score": s, "field_hits": h} for i, s, h in spec]


class FmStatsTest(unittest.TestCase):
    def setUp(self):
        os.environ["BOSHI_FM_STATS"] = "1"
        pg_bridge._FM_STATS_READY = True   # 跳过懒建表，只验 upsert

    def test_counts_hits_and_top1_change(self):
        c = FakeConn()
        top = items(("new", 0.10, ["ports"]), ("b", 0.12, ["ports"]), ("c", 0.20, None))
        pg_bridge._fm_stats(c, {"ports": ["8199"]}, top, pre_top1="old")
        self.assertEqual(len(c.calls), 1)
        _sql, params = c.calls[0]
        # (field, hits, gain_sum, top1_changed)
        self.assertEqual(params[0], "ports")
        self.assertEqual(params[1], 2)
        self.assertEqual(params[3], 1)

    def test_no_top1_change_when_same_id(self):
        c = FakeConn()
        top = items(("same", 0.10, ["flags"]))
        pg_bridge._fm_stats(c, {"flags": ["--model"]}, top, pre_top1="same")
        self.assertEqual(c.calls[0][1][3], 0)

    def test_one_row_per_query_field(self):
        c = FakeConn()
        top = items(("a", 0.10, ["ports", "flags"]))
        pg_bridge._fm_stats(c, {"ports": ["8199"], "flags": ["--model"], "versions": ["1.2"]}, top)
        self.assertEqual([call[1][0] for call in c.calls], ["ports", "flags", "versions"])

    def test_disabled_and_empty_are_noop(self):
        c = FakeConn()
        os.environ["BOSHI_FM_STATS"] = "0"
        pg_bridge._fm_stats(c, {"ports": ["8199"]}, items(("a", 0.1, ["ports"])))
        os.environ["BOSHI_FM_STATS"] = "1"
        pg_bridge._fm_stats(c, {}, items(("a", 0.1, ["ports"])))
        pg_bridge._fm_stats(c, {"ports": ["8199"]}, [])
        self.assertEqual(c.calls, [])

    def test_exception_is_swallowed(self):
        class Boom:
            def execute(self, *a, **k):
                raise RuntimeError("db down")
        pg_bridge._fm_stats(Boom(), {"ports": ["8199"]}, items(("a", 0.1, ["ports"])))

    def test_upsert_sql_shape(self):
        self.assertIn("ON CONFLICT (day, field)", pg_bridge._FM_STATS_UPSERT)
        self.assertIn("boshi.fm_stats", pg_bridge._FM_STATS_DDL)


if __name__ == "__main__":
    unittest.main()
