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


class OpenThreadGateTest(unittest.TestCase):
    """未了事项页质量门（2026-10-21）：纯字面/形态判断，不调模型。"""

    def test_user_statement_rejected(self):
        # 实测 top1：用户原话「暂停，…等我召唤你再继续」是状态播报，不是未了事项
        self.assertEqual(mental_model._open_thread_rank(
            "暂停，目前伯仕记忆系统升级尚未完成，并且工作日志和技术文档也还没写入，等我召唤你再继续",
            "conversation"), 2)

    def test_user_question_kept(self):
        self.assertNotEqual(mental_model._open_thread_rank(
            "这个技能可以导出，还需要吗？", "conversation"), 2)

    def test_completion_broadcast_rejected(self):
        self.assertEqual(mental_model._open_thread_rank(
            "v3片头剪52帧已落地，两仓都已提交，正式关闭。"), 2)

    def test_meta_language_rejected(self):
        self.assertEqual(mental_model._open_thread_rank(
            "问询超时了，你还没选，等你回来再说。"), 2)

    def test_weak_signal_long_discussion_rejected(self):
        # 读书笔记命中「还没做到的」；弱信号 + 长句 = 讨论，不是待办
        self.assertEqual(mental_model._open_thread_rank(
            "这篇《从 RAG 走向 LLM Wiki 的 4 阶段》（嘉为蓝鲸，09-18）基本就是在描述"
            "我们知识库在做的事——先给结论，再较真区分\"我们真做到了的\"和\"还没做到的\"。"), 2)

    def test_strong_task_long_kept(self):
        self.assertEqual(mental_model._open_thread_rank(
            "另有两个小待办挂着：4 段版本号（`v` 前缀规则，收益 18 次）、"
            "4 条 `created_at IS NULL`（时间窗会漏掉它们）——都等你点头才动。"), 0)

    def test_weak_short_kept(self):
        self.assertEqual(mental_model._open_thread_rank(
            "精排服务重启电脑后要手动双击 start_llama_rerank.bat（开机自启还没挂）"), 1)


class BackendApiTest(unittest.TestCase):
    def test_implemented(self):
        self.assertTrue(callable(getattr(pg_bridge, "get_mental_model", None)))
        self.assertTrue(callable(getattr(pg_bridge, "rebuild_mental_model", None)))
        self.assertTrue(callable(getattr(pg_bridge, "prune_mental_model", None)))

    def test_public_api(self):
        self.assertIn("get_mental_model", chroma_bridge._PUBLIC_API)
        self.assertIn("rebuild_mental_model", chroma_bridge._PUBLIC_API)
        self.assertIn("prune_mental_model", chroma_bridge._PUBLIC_API)

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
        for flag in ("--show", "--rebuild", "--key", "--scope",
                     "--prune", "--prune-apply", "--keep"):
            self.assertIn(flag, r.stdout)


class PruneTest(unittest.TestCase):
    """页面旧版清理：只动派生态，默认只统计不删。"""

    def test_keep_versions(self):
        self.assertEqual(mental_model.KEEP_VERSIONS, 8)

    def test_dry_run_default(self):
        sig = inspect.signature(mental_model.prune_pages)
        self.assertIs(sig.parameters["dry_run"].default, True)
        sig2 = inspect.signature(pg_bridge.prune_mental_model)
        self.assertIs(sig2.parameters["dry_run"].default, True)

    def test_only_non_latest_deleted(self):
        src = inspect.getsource(mental_model.prune_pages)
        self.assertIn("is_latest = false", src)
        self.assertIn("is_latest DESC", src)

    def test_dry_run_no_delete(self):
        src = inspect.getsource(mental_model.prune_pages)
        # DELETE 只能在 not dry_run 分支之后
        self.assertLess(src.index("not dry_run"), src.index("DELETE"))
        self.assertIn("if rows and not dry_run:", src)

    def test_core_wrapper(self):
        self.assertTrue(callable(getattr(boshi_core, "prune_mental_model", None)))


class SavePageTest(unittest.TestCase):
    def test_content_dedupe(self):
        """24h 定时重建：内容没变就不落库，否则每天白多 4~5 行派生态。"""
        src = inspect.getsource(mental_model.save_page)
        self.assertIn("内容未变", src)
        self.assertIn("rows[0][1]", src)

    def test_keeps_old_versions(self):
        src = inspect.getsource(mental_model.save_page)
        self.assertIn("is_latest = false", src)


class EvalHygieneTest(unittest.TestCase):
    def test_fm_stats_disabled_in_evals(self):
        """评测用例不能污染 boshi.fm_stats（否则 _FM_W 权重永远估不准）。"""
        for name in ("a9_eval.py", "a5_eval.py"):
            with open(os.path.join(HERE, "eval", name), encoding="utf-8") as f:
                src = f.read()
            self.assertIn('BOSHI_FM_STATS', src, name)
            self.assertIn('"0"', src, name)
            idx_env = src.index("BOSHI_FM_STATS")
            self.assertLess(idx_env, src.index("import pg_bridge"), name)


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
        # 2026-10-22：拼接顺序改为 心智模型页 → 本会话态 → 本轮召回。
        # 断言实现体而不是旧的字面量 mm + (...) —— 会话态插在中间，
        # 旧断言留在原处会让这条测试永远红。
        self.assertIn("parts = [x for x in (mm, ss, text) if x]", src)
        self.assertIn('return "\\n\\n".join(parts)', src)


if __name__ == "__main__":
    unittest.main()
