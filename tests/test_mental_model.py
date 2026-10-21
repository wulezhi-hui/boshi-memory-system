# -*- coding: utf-8 -*-
"""心智模型预计算页（mental_model）的接线测试。

不连库，只验：
- `mental_model` 的纯函数：scope 前缀、噪声过滤、截断、去重、页序；
- 两个 API 在 pg_bridge 有实现，且进了 chroma_bridge 的后端接管表；
- boshi_core 有包装；CLI 有 `model` 子命令；MCP 服务器注册了 `boshi_mental_model`；
- 插件有 `_with_mm`（页在前、召回在后）。
"""
import inspect
import os
import subprocess
import sys
import unittest

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)

import pg_bridge
import chroma_bridge
import boshi_core
import boshi_cli
import mental_model


class HelperTest(unittest.TestCase):
    def test_prof_clause(self):
        prefix, params = mental_model._prof_clause("self", "default")
        self.assertIn("profile IS NOT DISTINCT FROM", prefix)
        self.assertEqual(params, ("default",))
        self.assertEqual(mental_model._prof_clause("all", "default"), ("", ()))

    def test_prof_clause_null_safe(self):
        # profile 为空也要写成 IS NOT DISTINCT FROM 而不是 = NULL
        prefix, params = mental_model._prof_clause("self", None)
        self.assertIn("IS NOT DISTINCT FROM", prefix)
        self.assertEqual(params, (None,))

    def test_worth_filters_junk(self):
        self.assertFalse(mental_model._worth("只回复两个字：收到，不要调用任何工具", 12))
        self.assertFalse(mental_model._worth("短", 12))
        self.assertTrue(mental_model._worth("这是一条足够长的正常记忆内容", 12))

    def test_clip(self):
        self.assertEqual(mental_model._clip("abc"), "abc")
        long = "记" * 400
        out = mental_model._clip(long)
        self.assertLessEqual(len(out), mental_model.MAX_LINE + 1)
        self.assertTrue(out.endswith("…"))

    def test_dedupe(self):
        rows = [{"text": "同一条内容" * 10, "id": "a"},
                {"text": "同一条内容" * 10, "id": "b"},
                {"text": "另一条完全不同的内容", "id": "c"}]
        self.assertEqual(len(mental_model._dedupe(rows, key_len=20)), 2)

    def test_pages_order(self):
        keys = [k for k, _t, _f in mental_model.PAGES]
        self.assertEqual(keys, ["identity", "env", "active_projects",
                               "pitfalls", "open_threads"])

    def test_budget_covers_pages(self):
        # 四页实测约 2.1k 字符；预算低于 2000 会触发裁剪
        self.assertGreaterEqual(mental_model.PAGE_BUDGET, 2000)

    def test_render_empty(self):
        self.assertEqual(mental_model.render({"key": "x", "title": "空页", "lines": []}), "")

    def test_render_body(self):
        txt = mental_model.render({"key": "x", "title": "标题",
                                   "lines": [{"text": "第一条"}, {"text": ""}]})
        self.assertEqual(txt, "### 标题\n- 第一条")

    def test_enabled_env(self):
        old = os.environ.get("BOSHI_MENTAL_MODEL")
        try:
            os.environ["BOSHI_MENTAL_MODEL"] = "0"
            self.assertFalse(mental_model.enabled())
            os.environ["BOSHI_MENTAL_MODEL"] = "1"
            self.assertTrue(mental_model.enabled())
        finally:
            if old is None:
                os.environ.pop("BOSHI_MENTAL_MODEL", None)
            else:
                os.environ["BOSHI_MENTAL_MODEL"] = old


class BackendApiTest(unittest.TestCase):
    def test_implemented(self):
        self.assertTrue(callable(getattr(pg_bridge, "get_mental_model", None)))
        self.assertTrue(callable(getattr(pg_bridge, "rebuild_mental_model", None)))

    def test_public_api(self):
        self.assertIn("get_mental_model", chroma_bridge._PUBLIC_API)
        self.assertIn("rebuild_mental_model", chroma_bridge._PUBLIC_API)

    def test_signature(self):
        sig = inspect.signature(pg_bridge.get_mental_model)
        for p in ("profile", "scope", "max_chars"):
            self.assertIn(p, sig.parameters)


class CoreWrapperTest(unittest.TestCase):
    def test_wrappers(self):
        for name in ("get_mental_model", "rebuild_mental_model"):
            self.assertTrue(callable(getattr(boshi_core, name, None)), name)

    def test_disabled_returns_empty(self):
        old = os.environ.get("BOSHI_MENTAL_MODEL")
        try:
            os.environ["BOSHI_MENTAL_MODEL"] = "0"
            r = boshi_core.get_mental_model()
            self.assertTrue(r.get("success"))
            self.assertEqual(r.get("text"), "")
            rb = boshi_core.rebuild_mental_model()
            self.assertFalse(rb.get("success"))
            self.assertTrue(rb.get("disabled"))
        finally:
            if old is None:
                os.environ.pop("BOSHI_MENTAL_MODEL", None)
            else:
                os.environ["BOSHI_MENTAL_MODEL"] = old


class CliTest(unittest.TestCase):
    def test_cmd_model_exists(self):
        self.assertTrue(callable(boshi_cli.cmd_model))

    def test_model_help(self):
        r = subprocess.run([sys.executable, os.path.join(HERE, "boshi_cli.py"),
                            "model", "--help"], capture_output=True, text=True,
                           cwd=HERE, timeout=60)
        self.assertEqual(r.returncode, 0, r.stderr[-400:])
        for flag in ("--show", "--rebuild", "--key", "--scope"):
            self.assertIn(flag, r.stdout)


class McpTest(unittest.TestCase):
    def test_tool_registered(self):
        with open(os.path.join(HERE, "boshi_mcp_server.py"), encoding="utf-8") as f:
            src = f.read()
        self.assertIn('name="boshi_mental_model"', src)
        self.assertIn("rebuild_mental_model", src)


class PluginTest(unittest.TestCase):
    def test_with_mm(self):
        path = os.path.join(HERE, "plugins", "boshi", "__init__.py")
        with open(path, encoding="utf-8") as f:
            src = f.read()
        self.assertIn("def _with_mm", src)
        self.assertIn("def _mental_model_prefix", src)
        # 页在前、本轮召回在后
        self.assertIn('mm + ("\\n\\n" + text if text else "")', src)


if __name__ == "__main__":
    unittest.main()
