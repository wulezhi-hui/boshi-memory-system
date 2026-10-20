"""验证 P2 写入侧：截断只由 BOSHI_WRITE_SLOTS 控制，抽提失败不截断原文。

三组：
  A. 正常环境（抽提可用）→ >500 用户消息 / >800 结论文本必须原文全存
  B. 抽提抛异常（模拟 slots.py 损坏）→ 仍必须原文全存（旧代码此处会截断=BUG）
  C. BOSHI_WRITE_SLOTS=0 → 必须回到 500/800 截断（回滚路径）

用 stub _core 截获 save 调用，不碰真实记忆库。
"""
import importlib.util
import os
import sys
from pathlib import Path

PLUGIN = Path.home() / ".boshi" / "plugins" / "boshi" / "__init__.py"


def _ensure_agent_importable():
    """把 Hermes 的 workspace 目录加进 sys.path（venv 里没有 agent 包）。"""
    try:
        import agent  # noqa: F401
        return
    except ModuleNotFoundError:
        pass
    import glob
    cands = glob.glob(str(Path.home() / "AppData/Local/hermes/installs/*/environments/*/workspace"))
    for c in cands:
        if (Path(c) / "agent" / "memory_provider.py").exists():
            sys.path.insert(0, c)
            return
    raise RuntimeError("找不到 Hermes agent 包（workspace 路径）")


def load_plugin():
    _ensure_agent_importable()
    spec = importlib.util.spec_from_file_location("boshi_plugin_t", str(PLUGIN))
    mod = importlib.util.module_from_spec(spec)
    sys.modules["boshi_plugin_t"] = mod
    spec.loader.exec_module(mod)
    return mod


class StubCore:
    def __init__(self):
        self.saved = []

    def save(self, content, topic="", metadata=None, **kw):
        self.saved.append({"content": content, "topic": topic, "metadata": metadata or {}})


LONG_USER = "这是一个很长的用户消息。" * 120      # 1440 字
LONG_ASST = "这是一段很长的助手结论，包含细节。" * 120   # 2160 字


def run(mod, user, asst):
    core = StubCore()
    prov = mod.BoshiMemoryProvider.__new__(mod.BoshiMemoryProvider)
    prov._core = core
    prov._agent_context = "primary"
    prov._session_id = "t"
    prov.sync_turn(user, asst, session_id="t")
    return core.saved


def rows(recs):
    return {r["topic"]: r for r in recs}


def main():
    fails = []

    # A 正常
    mod = load_plugin()
    r = rows(run(mod, LONG_USER, LONG_ASST))
    for t, src, lim in (("conversation", LONG_USER, 500), ("assistant_conclusion", LONG_ASST, 800)):
        got = r.get(t)
        ok = got is not None and got["content"] == src and not got["content"].endswith("…")
        fails.append((f"A/{t}", ok, None if not got else len(got["content"]), len(src)))
        print(f"A/{t}: 长度={len(got['content']) if got else 'missing'} 期望={len(src)} " + ("OK" if ok else "FAIL"))
        if got:
            print(f"   slots={'有' if got['metadata'].get('slots') else '无'} raw_len={got['metadata'].get('raw_len')}")

    # B 抽提抛异常
    mod = load_plugin()
    def boom(_t):
        raise RuntimeError("模拟 slots.py 损坏")
    mod._boshi_slots = boom
    r = rows(run(mod, LONG_USER, LONG_ASST))
    for t, src in (("conversation", LONG_USER), ("assistant_conclusion", LONG_ASST)):
        got = r.get(t)
        ok = got is not None and got["content"] == src
        fails.append((f"B/{t}", ok, None if not got else len(got["content"]), len(src)))
        print(f"B/{t}: 长度={len(got['content']) if got else 'missing'} 期望={len(src)} " + ("OK" if ok else "FAIL"))

    # C 回滚
    os.environ["BOSHI_WRITE_SLOTS"] = "0"
    try:
        mod = load_plugin()
        r = rows(run(mod, LONG_USER, LONG_ASST))
        for t, lim in (("conversation", 500), ("assistant_conclusion", 800)):
            got = r.get(t)
            ok = got is not None and len(got["content"]) == lim + 1 and got["content"].endswith("…")
            fails.append((f"C/{t}", ok, None if not got else len(got["content"]), lim + 1))
            print(f"C/{t}: 长度={len(got['content']) if got else 'missing'} 期望={lim+1}(含…) " + ("OK" if ok else "FAIL"))
            if got:
                print(f"   slots={'有' if got['metadata'].get('slots') else '无'}")
    finally:
        os.environ.pop("BOSHI_WRITE_SLOTS", None)

    bad = [f for f in fails if not f[1]]
    print("\n结果：", f"{len(fails)-len(bad)}/{len(fails)} 通过", "" if not bad else f"失败={bad}")
    return 0 if not bad else 1


if __name__ == "__main__":
    sys.exit(main())
