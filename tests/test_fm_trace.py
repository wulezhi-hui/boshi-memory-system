# -*- coding: utf-8 -*-
"""P2+ 打点 `_fm_trace` 的单测：默认不写、开了才写、字段算得对、坏输入不炸。

打点是「事后审计线上误抬」的唯一数据来源，必须保证它自己不会影响检索、
也不会因为一个畸形 item 抛出异常把 search_memory 带崩。
"""
import json
import os
import shutil
import tempfile
import unittest

import pg_bridge


class FmTraceTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="fmb")
        self._old_log = pg_bridge._LOG
        pg_bridge._LOG = os.path.join(self.tmp, "pg_autostart.log")
        self._old_env = os.environ.get("BOSHI_FM_TRACE")
        self.path = os.path.join(self.tmp, "fm_trace.jsonl")

    def tearDown(self):
        pg_bridge._LOG = self._old_log
        if self._old_env is None:
            os.environ.pop("BOSHI_FM_TRACE", None)
        else:
            os.environ["BOSHI_FM_TRACE"] = self._old_env
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _items(self):
        return [
            {"id": "aaaa1111", "score": 0.10},
            {"id": "bbbb2222", "score": 0.05, "field_hits": ["ports"], "field_gain": 0.1},
            {"id": "cccc3333", "score": 0.30},
        ]

    def test_off_by_default_writes_nothing(self):
        os.environ.pop("BOSHI_FM_TRACE", None)
        pg_bridge._fm_trace("端口 8199", {"ports": ["8199"]}, self._items(), 5)
        self.assertFalse(os.path.exists(self.path))

    def test_on_writes_one_record_and_detects_top1_change(self):
        os.environ["BOSHI_FM_TRACE"] = "1"
        pg_bridge._fm_trace("端口 8199 怎么配", {"ports": ["8199"]}, self._items(), 5)
        with open(self.path, encoding="utf-8") as f:
            lines = [l for l in f if l.strip()]
        self.assertEqual(len(lines), 1)
        r = json.loads(lines[0])
        self.assertEqual(r["query"], "端口 8199 怎么配")
        self.assertEqual(r["fields"], {"ports": ["8199"]})
        self.assertEqual(r["cand"], 3)
        self.assertEqual(r["hit_n"], 1)
        self.assertEqual(r["pre_top"][0], "aaaa1111")
        self.assertEqual(r["post_top"][0], "bbbb2222")
        self.assertTrue(r["top1_changed"])
        self.assertTrue(r["top1_hit"])

    def test_no_change_when_gain_keeps_order(self):
        os.environ["BOSHI_FM_TRACE"] = "1"
        items = [{"id": "aaaa1111", "score": 0.01},
                 {"id": "bbbb2222", "score": 0.50, "field_hits": ["ports"]}]
        pg_bridge._fm_trace("8199 端口", {"ports": ["8199"]}, items, 5)
        with open(self.path, encoding="utf-8") as f:
            r = json.loads(f.read().strip())
        self.assertFalse(r["top1_changed"])
        self.assertFalse(r["top1_hit"])

    def test_garbage_items_never_raise(self):
        os.environ["BOSHI_FM_TRACE"] = "1"
        # 缺 id / score 为 None / 空列表 都不能让检索路径炸掉
        pg_bridge._fm_trace("x", {}, [{"score": None}], 5)
        pg_bridge._fm_trace("x", {}, [], 5)
        pg_bridge._fm_trace(None, None, [{"id": "z", "score": 0.2}], 5)

    def test_query_truncated_to_200(self):
        os.environ["BOSHI_FM_TRACE"] = "1"
        pg_bridge._fm_trace("端" * 500, {"ports": ["1"]}, self._items(), 5)
        with open(self.path, encoding="utf-8") as f:
            r = json.loads(f.read().strip())
        self.assertEqual(len(r["query"]), 200)


if __name__ == "__main__":
    unittest.main()
