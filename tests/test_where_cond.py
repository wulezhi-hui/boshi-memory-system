# -*- coding: utf-8 -*-
"""_cond("created_at", ...) 走真列（不是 meta jsonb）的单测。

动机：评测期间插件仍在往库里写本节工作摘要，纯中文 query 的 kw 候选会被自身
写入污染，跨轮名次不可比（2026-10-20 A9 v2→v3 实测：一条 10:07 写入的自述
把 gold 从 top1 挤到 top3）。给 _cond 加 created_at 真列支持后，评测可以带
`where={"created_at": {"$lte": asof}}` 复现同一份库快照。
"""
import os
import sys
import datetime
import unittest

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)
import pg_bridge


class CreatedAtCondTest(unittest.TestCase):
    def test_lte_numeric(self):
        sql, params = pg_bridge._cond("created_at", {"$lte": 1792461898.0})
        self.assertIn("created_at", sql)
        self.assertIn("to_timestamp", sql)
        self.assertIn("<=", sql)
        self.assertEqual(params, [1792461898.0])

    def test_gte_and_gt(self):
        for op, sym in (("$gte", ">="), ("$gt", ">")):
            sql, _ = pg_bridge._cond("created_at", {op: 1.0})
            self.assertIn(sym, sql)

    def test_iso_string(self):
        iso = "2026-10-20T10:04:58+08:00"
        sql, params = pg_bridge._cond("created_at", {"$lte": iso})
        self.assertIn("created_at", sql)
        self.assertAlmostEqual(
            params[0],
            datetime.datetime.fromisoformat(iso).timestamp(), places=1)

    def test_scalar_equals(self):
        sql, params = pg_bridge._cond("created_at", 1792461898.0)
        self.assertIn("to_timestamp", sql)
        self.assertEqual(params, [1792461898.0])

    def test_unknown_op_is_true(self):
        # 认不出的操作符 → TRUE（宁可不筛，也不误筛掉全库）
        sql, _ = pg_bridge._cond("created_at", {"$between": [1, 2]})
        self.assertEqual(sql, "TRUE")

    def test_not_jsonb(self):
        # 关键：不能落到 meta->>'created_at'（真列不在 meta 里，会全表筛空）
        sql, _ = pg_bridge._cond("created_at", {"$lte": 1.0})
        self.assertNotIn("meta->>", sql)


if __name__ == "__main__":
    unittest.main()
