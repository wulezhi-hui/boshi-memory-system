"""P2+ 字段级匹配：query 侧补充扫描与同字段包含判定（不连库，纯函数）。

跑法：cd ~/.boshi && ./venv/Scripts/python.exe -m unittest tests.test_field_match -v
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("BOSHI_FIELD_MATCH", "1")

import pg_bridge  # noqa: E402


class QueryExtraTest(unittest.TestCase):
    """抽提器是给长文档写的（「端口 8199」）；人提问说「8199 端口」，必须能补出来。"""

    def test_port_number_first(self):
        self.assertEqual(pg_bridge._fm_query_extra("8199 端口 llama-server 怎么配"),
                         {"ports": ["8199"]})

    def test_port_keyword_first_still_works(self):
        # 关键词在前时 slots.extract 已覆盖，这里补扫也不该漏
        self.assertEqual(pg_bridge._fm_query_extra("端口 8199 监听了吗"),
                         {"ports": ["8199"]})

    def test_ip_is_not_port(self):
        # `172.16.0.219 端口` 里的 219 是 IP 尾段，不是端口
        self.assertEqual(pg_bridge._fm_query_extra("172.16.0.219 端口 开着的还有啥"), {})

    def test_plain_numbers_are_not_ports(self):
        for q in ("2026-10-20 的日志在哪", "上下文 131072 够不够",
                  "v7.2 版本改了啥", "跑 3 轮评测", "top1 从 25 涨到 29"):
            self.assertEqual(pg_bridge._fm_query_extra(q), {}, q)

    def test_out_of_range_port_number(self):
        self.assertEqual(pg_bridge._fm_query_extra("99999 端口"), {})

    def test_empty_query_safe(self):
        self.assertEqual(pg_bridge._fm_query_extra(""), {})
        self.assertEqual(pg_bridge._fm_query_extra(None), {})


class HitsTest(unittest.TestCase):
    """同字段、双向包含即命中；跨字段不算（字段级匹配的全部意义就在于不跨）。"""

    def test_exact_and_contained(self):
        qf = {"ports": ["8199"]}
        self.assertEqual(pg_bridge._fm_hits(qf, {"ports": ["8199"]}), ["ports"])
        # 反向包含：候选里更长/更短的写法也算同一字段
        self.assertEqual(pg_bridge._fm_hits({"ports": ["127.0.0.1:8199"]},
                                            {"ports": ["8199"]}), ["ports"])

    def test_cross_field_not_hit(self):
        self.assertEqual(pg_bridge._fm_hits({"ports": ["8199"]},
                                            {"versions": ["8199.1"]}), [])
        self.assertEqual(pg_bridge._fm_hits({"versions": ["7.2"]}, {}), [])

    def test_multi_field_hits_ordered_by_fm_keys(self):
        qf = {"ports": ["8199"], "flags": ["--tensor-split"]}
        hits = pg_bridge._fm_hits(qf, {"ports": ["8199"], "flags": ["--tensor-split"]})
        self.assertEqual(hits, [k for k in pg_bridge._FM_KEYS if k in ("ports", "flags")])

    def test_keys_configurable_and_excludes_numbers_by_default(self):
        self.assertNotIn("numbers", pg_bridge._FM_KEYS)
        self.assertIn("ports", pg_bridge._FM_KEYS)

    def test_norm_is_形态归一_only(self):
        self.assertEqual(pg_bridge._fm_norm("  8199 "), "8199")
        self.assertEqual(pg_bridge._fm_norm("--Tensor-Split"), "--tensor-split")
        self.assertEqual(pg_bridge._fm_norm("8199。"), "8199")
        self.assertEqual(pg_bridge._fm_norm(""), "")


if __name__ == "__main__":
    unittest.main()
