#!/usr/bin/env python
"""boshi RPC server —— 让 hermes 侧插件在**任意**解释器版本下复用伯仕数据层。

背景（2026-10-23）：
  hermes 运行时升到 Python 3.14，而伯仕数据层依赖 onnxruntime（bge-m3 嵌入，
  无 3.14 wheel）。内联 `sys.path` 导入在 3.14 进程里 import 不动 → hermes 自动
  禁用 boshi 插件 → 🦄 图标消失。数据层本体健康（跑在 ~/.boshi/venv, py3.11）。

本进程 = 数据层的「常驻前台」。hermes 侧插件通过 stdin/stdout 的 JSON-lines
把它当 RPC 客户端调用，从而**不依赖 hermes 解释器版本**。零 LLM、零生成，
只是把同一条 SQL / 同一个 boshi_core 函数跨进程执行。

协议（每行一个 JSON）：
  请求  {"id": n, "target": "boshi_core|chroma_bridge|rpc",
         "fn": "name", "args": [...], "kwargs": {...}}
  应答  {"id": n, "ok": true, "result": ...}   或
        {"id": n, "ok": false, "error": "..."}

target 含义：
  - boshi_core   → 调 boshi_core.<fn>(*)
  - chroma_bridge → 调 chroma_bridge.<fn>(*)（如 backend_name）
  - rpc          → 调本文件内的 <fn>(*)（如 session_state）

运行方式（由 hermes 插件 spawn）：
  ~/.boshi/venv/Scripts/python.exe  boshi_rpc_server.py
"""
from __future__ import annotations

import io
import json
import sys
import traceback
from datetime import datetime
from pathlib import Path

# 确保能 import 数据层模块（与 boshi_core / MCP server 同口径）
_BOSHI = Path(__file__).resolve().parent
if str(_BOSHI) not in sys.path:
    sys.path.insert(0, str(_BOSHI))

# 惰性导入：让 ONNX / PG 的首次加载发生在**第一次真正调用**时（冷启 ~6s），
# 而不是 import 本文件时。这样 spawn 本身是毫秒级的。
_MODULES = {}


def _get_module(name: str):
    if name not in _MODULES:
        if name == "boshi_core":
            import boshi_core  # noqa
            _MODULES[name] = boshi_core
        elif name == "chroma_bridge":
            import chroma_bridge  # noqa
            _MODULES[name] = chroma_bridge
        elif name == "rpc":
            _MODULES[name] = sys.modules[__name__]
        else:
            raise ValueError(f"unknown target: {name}")
    return _MODULES[name]


# ---------------------------------------------------------------------------
# rpc.* —— 服务器侧函数（数据层直接访问，返回值必须 JSON 可序列化）
# ---------------------------------------------------------------------------

def session_state(session_id: str, profile=None, tail_n: int = 4,
                  cap: int = 0) -> dict:
    """取「任务起点 + 最近 N 行 + 最新事实层」原始数据（时间已格式化为 HH:MM）。

    hermes 插件的 `_session_state` 只保留**渲染/判重/截断**逻辑，SQL 挪到这里。
    任何异常 → 返回全空（不能拖垮召回主路径），与插件原口径一致。
    """
    out = {"origin": None, "rows": [], "fact": None}
    if not session_id:
        return out
    try:
        import pg_bridge
        conn = pg_bridge._conn()
        cur = conn.cursor()
        cur.execute(
            """SELECT content, created_at FROM boshi.memories
               WHERE session_id = %s AND (profile = %s OR profile IS NULL)
                 AND topic = 'conversation'
               ORDER BY created_at ASC LIMIT 1""",
            (session_id, profile),
        )
        o = cur.fetchone()
        cur.execute(
            """SELECT topic, content, created_at FROM boshi.memories
               WHERE session_id = %s AND (profile = %s OR profile IS NULL)
                 AND topic IN ('conversation', 'assistant_conclusion')
               ORDER BY created_at DESC LIMIT %s""",
            (session_id, profile, int(tail_n)),
        )
        rows = cur.fetchall()[::-1]  # 时间正序
        cur.execute(
            """SELECT content, created_at FROM boshi.memories
               WHERE session_id = %s AND (profile = %s OR profile IS NULL)
                 AND topic = 'session_fact'
               ORDER BY created_at DESC LIMIT 1""",
            (session_id, profile),
        )
        f = cur.fetchone()

        def _ts(v):
            return v.strftime("%H:%M") if hasattr(v, "strftime") else ""

        if o is not None:
            out["origin"] = {"content": o[0], "ts": _ts(o[1])}
        for topic, content, ts in rows:
            out["rows"].append({"topic": topic, "content": content, "ts": _ts(ts)})
        if f is not None:
            out["fact"] = {"content": f[0], "ts": _ts(f[1])}
    except Exception:  # noqa: BLE001
        traceback.print_exc()
        out = {"origin": None, "rows": [], "fact": None}
    return out


def ping() -> dict:
    """存活探针：顺带报告数据层后端，供 hermes 侧 is_available 判断。"""
    try:
        import chroma_bridge
        backend = chroma_bridge.backend_name()
    except Exception as e:  # noqa: BLE001
        import sys as _sys, os as _os, json as _json
        venv_cfg = _os.path.join(_os.path.dirname(_sys.executable), "pyvenv.cfg")
        return {
            "ok": False, "error": str(e), "backend": None,
            "debug": {
                "exe": _sys.executable,
                "pyvenv_cfg": _os.path.exists(venv_cfg),
                "site_paths": [p for p in _sys.path if 'site-packages' in p.lower() or '.boshi' in p.lower()],
                "boshi_pkgs": [p for p in _sys.path if 'pydantic_core' in p][:1],
                "env_python": {k: v for k, v in _os.environ.items() if k.startswith(('PYTHON', 'BOSHI', 'VIRTUAL'))},
            },
        }
    return {"ok": True, "backend": backend, "ts": datetime.now().isoformat()}


# ---------------------------------------------------------------------------
# 主循环
# ---------------------------------------------------------------------------

def _dispatch(req: dict) -> dict:
    rid = req.get("id")
    try:
        mod = _get_module(req.get("target", "boshi_core"))
        fn = getattr(mod, req.get("fn", ""))
        result = fn(*req.get("args", []), **req.get("kwargs", {}))
        return {"id": rid, "ok": True, "result": result}
    except Exception as e:  # noqa: BLE001
        traceback.print_exc()
        return {"id": rid, "ok": False, "error": f"{type(e).__name__}: {e}"}


def main() -> None:
    # 直接用 text-mode 的 sys.stdin / sys.stdout（避免 Windows 二次 wrapper）。
    # ensure_ascii=True：中文转 \uXXXX 纯 ASCII，规避 Windows 控制台/locale(GBK) 编码坑，
    # 客户端 json.loads 还原，语义不变。
    import os
    sys.stdout.write(json.dumps({"ready": True, "pid": os.getpid()}) + "\n")
    sys.stdout.flush()
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except Exception as e:  # noqa: BLE001
            sys.stdout.write(json.dumps({"ok": False, "error": f"bad request: {e}"}) + "\n")
            sys.stdout.flush()
            continue
        sys.stdout.write(json.dumps(_dispatch(req)) + "\n")
        sys.stdout.flush()


if __name__ == "__main__":
    main()
