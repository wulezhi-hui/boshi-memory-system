#!/usr/bin/env python3
"""
伯仕记忆系统 MCP Server 🦄 — mcp 2.0.0 适配版
===========================================
通过 MCP 协议暴露记忆系统，让任何支持 MCP 的 Agent 都能使用伯仕的记忆。

启动方式:
  python boshi_mcp_server.py                # stdio 模式（Hermes/Claude Code/Cursor/DSH 连接）

暴露的 Tools:
  boshi_search    — 三路融合检索
  boshi_save      — 存入一条记忆
  boshi_delete    — 删除一条记忆
  boshi_status    — 记忆库状态
  boshi_profile   — 用户画像/会话简报
  boshi_graph     — 知识图谱查询
  boshi_graph_add — 添加图谱节点/边
  boshi_recent    — 最近N条记忆

适配说明:
  原版基于 mcp SDK 1.x 的 Server.list_tools 装饰器；mcp 2.0.0 改用
  MCPServer + @server.tool() 装饰器 + run_stdio_async()。
"""
import os
import re
import sys
import json
import asyncio
import argparse
from datetime import datetime
from typing import Literal, Optional

from pydantic import Field
from typing_extensions import Annotated

# ── 路径 ──
BOSHI_HOME = os.path.expanduser("~/.boshi")
if BOSHI_HOME not in sys.path:
    sys.path.insert(0, BOSHI_HOME)

from boshi_core import (
    search, save, delete, status, profile,
    graph_query, graph_add_node, graph_add_edge,
    recent, time_range,
)

from mcp.server import MCPServer
from mcp.server.mcpserver.context import Context


# ══════════════════════════════════════════════════════
# 调用方身份识别（通用 MCP 接口：任何 agent 直接调用，
# 伯仕自动带上它自己的标识——接入方无需任何配置）
# ══════════════════════════════════════════════════════

def _sanitize_identity(name: str) -> str:
    """把 MCP 客户端名规整成可用的归属标识（小写、去特殊字符、限长）"""
    n = re.sub(r"[^0-9A-Za-z._-]+", "-", (name or "").strip()).strip("-._")
    return n.lower()[:32]


def _client_name(ctx) -> str:
    """取 MCP 握手自报的客户端名（原始，未规整）"""
    try:
        params = getattr(ctx.session, "client_params", None) if ctx is not None else None
        if params is None:
            return ""
        info = getattr(params, "clientInfo", None) or getattr(params, "client_info", None)
        return str(getattr(info, "name", None) or "")
    except Exception:
        return ""


def _caller_identity(ctx) -> Optional[str]:
    """解析调用方归属标识。

    **优先级（MCP 握手是"谁在调用"最具体的信号，故优先于环境变量）**：
      1. ``BOSHI_PROFILE`` 环境变量（接入方显式配置）
      2. **MCP 握手 clientInfo.name**（零配置自动识别）
      3. 若设了 ``HERMES_HOME`` → 返回 None，交下游按 HERMES_HOME 解析 profile
         （仅用于"没有客户端名的 Hermes 自带 MCP"场景）
      4. 兜底 ``external``（不落 default，避免污染主身记忆空间）

    注意：``HERMES_HOME`` 常被写入用户级环境变量（Windows HKCU\\Environment），
    外部 agent 会一并继承——所以**不能**把它当作"这是 Hermes 在调用"的证据。
    """
    env = (os.environ.get("BOSHI_PROFILE") or "").strip()
    if env:
        return env
    ident = _sanitize_identity(_client_name(ctx))
    if ident:
        return ident
    if (os.environ.get("HERMES_HOME") or "").strip():
        return None
    return "external"


# ══════════════════════════════════════════════════════
# 通用接口自述（MCP initialize 的 instructions）：
# 把归属约定讲给任何接入的 agent，接入方无需任何适配
# ══════════════════════════════════════════════════════

SERVER_INSTRUCTIONS = """\
伯仕记忆系统 —— 多 agent 共享记忆库。归属约定由伯仕自动执行，接入方无需任何配置：

1) 身份：你每次调用的「归属标识」由伯仕按 MCP 握手自动识别（即你的客户端名，例如 opencode / dsh）。
2) 写入：boshi_save 存下的记忆会自动带上你的标识，无需传参。
3) 读取：默认只读你自己写入的记忆（scope="self"）。想看其它 agent / 全部记忆时，
   显式传 scope="all"（含其它 Hermes profile 与其它接入 agent 的记忆）。
4) 每次返回结果里的 _identity / _scope 字段会告诉你"我是谁、这次生效的范围"。
5) 知识图谱边（type=relation）是全局共享的跨领域知识网络，默认不参与召回；
   需要图谱联想时传 include_graph=true。
6) 用户说"查其它 agent 的记忆 / 全部记忆 / 全局记忆"时，就用 scope="all"；
   只想自己的经验时用默认值即可。
"""


def _with_meta(result, ctx, scope=None):
    """给 dict 结果补上 _identity / _scope，让调用方模型看得见自己的身份与生效范围。

    仅对 dict 结果生效；list 结果（time_range/recent）保持原样，
    避免改变既有消费方的解析约定（如 dsh 插件用 Array.isArray 判定）。
    """
    if not isinstance(result, dict):
        return result
    try:
        ident = _caller_identity(ctx)
        out = dict(result)
        out["_identity"] = ident if ident else "default"
        out["_scope"] = scope if scope else f"self(默认，全库请传 scope=all)"
    except Exception:
        return result
    return out


# ══════════════════════════════════════════════════════
# scope 参数（参数级说明 = 最贴近模型决策点的渠道）
# ══════════════════════════════════════════════════════

SCOPE_DESC = ('归属范围。默认 "self"（只读你自己写入的记忆）。'
              '当用户要求"查其它 agent / 全部记忆 / 全局记忆 / 跨 agent 的记忆"时，'
              '必须显式传 "all" 才会读到其它 agent 与其它 Hermes profile 的记忆。')

ScopeParam = Annotated[Optional[Literal["self", "all"]], Field(description=SCOPE_DESC)]


# ══════════════════════════════════════════════════════
# 调用审计（验证各 agent 实际传了什么，而非靠推测）
# ══════════════════════════════════════════════════════

AUDIT_LOG = os.path.join(BOSHI_HOME, "logs", "mcp_calls.jsonl")


def _audit(tool: str, identity: Optional[str], scope=None, query: str = "",
           extra: dict = None, ctx=None) -> None:
    """追加一行调用记录到 ~/.boshi/logs/mcp_calls.jsonl（失败静默，绝不影响主流程）"""
    try:
        rec = {
            "ts": datetime.now().isoformat(timespec="seconds"),
            "identity": identity if identity else "(按 HERMES_HOME 解析)",
            "client": _client_name(ctx) or "(客户端未自报名)",
            "tool": tool,
            "scope": scope if scope else "(未传→默认 self)",
        }
        if os.environ.get("BOSHI_PROFILE"):
            rec["env_BOSHI_PROFILE"] = True
        if os.environ.get("HERMES_HOME"):
            rec["env_HERMES_HOME"] = True
        if query:
            rec["query"] = query[:80]
        if extra:
            rec.update(extra)
        os.makedirs(os.path.dirname(AUDIT_LOG), exist_ok=True)
        with open(AUDIT_LOG, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except Exception:
        pass


def create_server():
    """创建 mcp 2.0 MCPServer，注册全部 9 个工具。"""
    server = MCPServer("boshi-memory", version="6.1.0",
                       instructions=SERVER_INSTRUCTIONS)

    @server.tool(
        name="boshi_search",
        description="搜索伯仕的记忆。支持多策略检索（语义向量+全文混合+知识图谱），找到最相关的记忆。可用 since/until 限定时间范围（Unix 时间戳）。默认排除知识图谱自动提边（占库85%的噪声），需要图谱联想时 include_graph=true。",
    )
    def boshi_search(
        query: str,
        top_k: int = 5,
        source: Literal["all", "vector", "hybrid", "graph"] = "all",
        since: Optional[float] = None,
        until: Optional[float] = None,
        include_graph: bool = False,
        scope: ScopeParam = None,
        ctx: Optional[Context] = None,
    ) -> str:
        """搜索记忆：query 搜索查询文本；top_k 返回条数默认5；source 检索策略 all=融合(hybrid+图谱), vector=语义, hybrid=语义+全文混合, graph=图谱；since/until 可选，Unix 时间戳秒，限定结果时间范围；include_graph 是否包含图谱自动提边（默认 false）；scope 见参数说明（默认只读本 agent 自己的，全库须传 all）。调用方标识由伯仕按 MCP 握手自动识别，无需传参。"""
        _me = _caller_identity(ctx)
        _audit("boshi_search", _me, scope, query, ctx=ctx)
        result = search(query=query, top_k=top_k, source=source,
                        include_graph=include_graph, scope=scope,
                        me=_me)
        # 时间过滤（Python 端，不依赖 ChromaDB where）
        if since or until:
            results = result.get("results", [])
            filtered = []
            for r in results:
                ts = r.get("metadata", {}).get("_version_created", 0)
                if since and ts < since:
                    continue
                if until and ts > until:
                    continue
                filtered.append(r)
            result["results"] = filtered
            result["total"] = len(filtered)
        return json.dumps(_with_meta(result, ctx, scope), ensure_ascii=False, indent=2)

    @server.tool(
        name="boshi_save",
        description="向伯仕记忆系统存入一条记忆/事实。适合保存用户偏好、项目决策、重要信息等。",
    )
    def boshi_save(content: str, topic: str = "external", metadata: Optional[dict] = None,
                   profile: Optional[str] = None,
                   ctx: Optional[Context] = None) -> str:
        """存入记忆：content 记忆内容；topic 主题标签默认 external；metadata 附加元数据（可选）；profile 归属标识（可选，一般无需传——伯仕会按 MCP 握手自动识别调用方标识；显式传入可覆盖）"""
        _ident = profile or _caller_identity(ctx)
        _audit("boshi_save", _ident, "写入(归属自动打标)", content, ctx=ctx)
        result = save(content=content, topic=topic, metadata=metadata,
                      profile=_ident)
        return json.dumps(_with_meta(result, ctx, "self(写入归属自动打标)"), ensure_ascii=False, indent=2)

    @server.tool(
        name="boshi_delete",
        description="删除一条记忆（按ID）。",
    )
    def boshi_delete(memory_id: str) -> str:
        """删除记忆：memory_id 要删除的记忆ID"""
        result = delete(memory_id)
        return json.dumps(result, ensure_ascii=False, indent=2)

    @server.tool(
        name="boshi_status",
        description="查看记忆库状态：总条数、知识图谱节点/边数、ChromaDB路径。",
    )
    def boshi_status() -> str:
        """记忆库状态：总条数、知识图谱节点/边数、ChromaDB路径"""
        result = status()
        return json.dumps(result, ensure_ascii=False, indent=2)

    @server.tool(
        name="boshi_profile",
        description="获取用户画像摘要：当前热区话题、记忆总数、最近记忆。适合作为对话开场的上下文注入。",
    )
    def boshi_profile(scope: ScopeParam = None,
                      ctx: Optional[Context] = None) -> str:
        """用户画像摘要：当前热区话题、记忆总数、最近记忆；scope 见参数说明（默认只读本 agent 的，全库须传 all）"""
        _me = _caller_identity(ctx)
        _audit("boshi_profile", _me, scope, ctx=ctx)
        result = profile(scope=scope, me=_me)
        return json.dumps(_with_meta(result, ctx, scope), ensure_ascii=False, indent=2)

    @server.tool(
        name="boshi_graph",
        description="查询知识图谱：从指定实体出发，BFS遍历关联实体和关系。用于了解实体间的关联。",
    )
    def boshi_graph(entity: str, max_depth: int = 2) -> str:
        """图谱查询：entity 起始实体名；max_depth 遍历深度默认2"""
        result = graph_query(entity=entity, max_depth=max_depth)
        return json.dumps(result, ensure_ascii=False, indent=2)

    @server.tool(
        name="boshi_graph_add",
        description="向知识图谱添加节点或关系边。action=add_node 时提供 name；action=add_edge 时提供 from_name/to_name/relation。",
    )
    def boshi_graph_add(
        action: Literal["add_node", "add_edge"],
        name: Optional[str] = None,
        type: Optional[str] = None,
        attr: Optional[str] = None,
        from_name: Optional[str] = None,
        to_name: Optional[str] = None,
        relation: Optional[str] = None,
    ) -> str:
        """图谱添加：action 必填；add_node 需要 name/type/attr；add_edge 需要 from_name/to_name/relation"""
        if action == "add_node":
            result = graph_add_node(name=name, type=type or "", attr=attr or "")
        elif action == "add_edge":
            result = graph_add_edge(from_name=from_name, to_name=to_name, relation=relation)
        else:
            result = {"error": f"Unknown action: {action}"}
        return json.dumps(result, ensure_ascii=False, indent=2)

    @server.tool(
        name="boshi_recent",
        description="获取最近N条记忆，用于快速了解最近的活动记录。",
    )
    def boshi_recent(n: int = 10, scope: ScopeParam = None,
                     ctx: Optional[Context] = None) -> str:
        """最近记忆：n 返回条数默认10；scope 见参数说明（默认只读本 agent 的，全库须传 all）"""
        _me = _caller_identity(ctx)
        _audit("boshi_recent", _me, scope, extra={"n": n}, ctx=ctx)
        result = recent(n=n, scope=scope, me=_me)
        return json.dumps(result, ensure_ascii=False, indent=2)

    @server.tool(
        name="boshi_time_range",
        description="按时间范围查询伯仕的记忆（Unix 时间戳）。适合查'今天/昨天/本周做了什么'等时间线问题。默认排除知识图谱自动提边，需要时 include_graph=true。",
    )
    def boshi_time_range(
        since: float,
        until: Optional[float] = None,
        top_k: int = 50,
        include_graph: bool = False,
        scope: ScopeParam = None,
        ctx: Optional[Context] = None,
    ) -> str:
        """按时间范围查记忆：since 起始 Unix 时间戳（秒）必填；until 结束 Unix 时间戳（秒）可选，默认至今；top_k 返回条数默认50；include_graph 是否包含图谱自动提边（默认 false）；scope 见参数说明（默认只读本 agent 的，全库须传 all）。结果按写入时间降序。"""
        _me = _caller_identity(ctx)
        _audit("boshi_time_range", _me, scope, extra={"since": since}, ctx=ctx)
        result = time_range(since=since, until=until, top_k=top_k,
                            include_graph=include_graph, scope=scope,
                            me=_me)
        return json.dumps(result, ensure_ascii=False, indent=2)

    return server


def main():
    parser = argparse.ArgumentParser(description="伯仕记忆系统 MCP Server")
    parser.add_argument("--sse", type=int, metavar="PORT", help="SSE 模式，指定监听端口（mcp 2.0 新 API）")
    parser.add_argument("--stdio", action="store_true", help="stdio 模式（默认）")
    args = parser.parse_args()

    server = create_server()
    if args.sse:
        asyncio.run(server.run_sse_async(args.sse))
    else:
        asyncio.run(server.run_stdio_async())


if __name__ == "__main__":
    main()
