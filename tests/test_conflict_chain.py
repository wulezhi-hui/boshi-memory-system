# -*- coding: utf-8 -*-
"""冲突建模（resolve_conflict / fact_chain / detect_conflicts）的接线测试。

不连库，只验：
- 三个 API 在 pg_bridge 有实现，且 `fact_chain` 进了 chroma_bridge 的后端接管表；
- boshi_core 有包装且返回 success 形状（后端缺能力时回 error 不抛）；
- CLI 有 `chain` / `conflicts` 子命令与对应参数。
"""
import inspect
import os
import sys
import unittest

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)

import pg_bridge
import chroma_bridge
import boshi_core
import boshi_cli


class BackendApiTest(unittest.TestCase):
    def test_implemented(self):
        for name in ("resolve_conflict", "fact_chain", "detect_conflicts"):
            self.assertTrue(callable(getattr(pg_bridge, name, None)), name)

    def test_public_api(self):
        self.assertIn("fact_chain", chroma_bridge._PUBLIC_API)
        self.assertIn("resolve_conflict", chroma_bridge._PUBLIC_API)
        self.assertIn("detect_conflicts", chroma_bridge._PUBLIC_API)

    def test_fact_chain_signature(self):
        sig = inspect.signature(pg_bridge.fact_chain)
        self.assertEqual(list(sig.parameters)[:2], ["memory_id", "max_depth"])

    def test_detect_conflicts_signature(self):
        sig = inspect.signature(pg_bridge.detect_conflicts)
        for p in ("topic", "top_k", "sim_threshold"):
            self.assertIn(p, sig.parameters)


class CoreWrapperTest(unittest.TestCase):
    def test_wrappers(self):
        for name in ("fact_chain", "detect_conflicts"):
            self.assertTrue(callable(getattr(boshi_core, name, None)), name)

    def test_fact_chain_missing_id(self):
        # 不存在的 id：后端返回 None → core 转成 error 而不是抛
        r = boshi_core.fact_chain("00000000-0000-0000-0000-000000000000")
        self.assertFalse(r.get("success"))
        self.assertIn("未找到", r.get("error", ""))


class CliTest(unittest.TestCase):
    def _parser(self):
        return boshi_cli.build_parser() if hasattr(boshi_cli, "build_parser") else None

    def test_cli_functions_exist(self):
        self.assertTrue(callable(boshi_cli.cmd_chain))
        self.assertTrue(callable(boshi_cli.cmd_conflicts))

    def test_parser_has_subcommands(self):
        p = self._parser()
        if p is None:
            self.skipTest("boshi_cli 未暴露 build_parser")
        # argparse 子命令名从 _SubParsersAction.choices 拿
        import argparse
        subs = {}
        for a in p._actions:
            if isinstance(a, argparse._SubParsersAction):
                subs = a.choices
        self.assertIn("chain", subs)
        self.assertIn("conflicts", subs)


if __name__ == "__main__":
    unittest.main()
