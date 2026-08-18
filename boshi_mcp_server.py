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
import sys
import json
import asyncio
import argparse
from typing import Literal, Optional

# ── 路径 ──
BOSHI_HOME = os.path.expanduser("~/.boshi")
if BOSHI_HOME not in sys.path:
    sys.path.insert(0, BOSHI_HOME)

from boshi_core import (
    search, save, delete, status, profile,
    graph_query, graph_add_node, graph_add_edge,
    recent,
)

from mcp.server import MCPServer


def create_server():
    """创建 mcp 2.0 MCPServer，注册全部 8 个工具。"""
    server = MCPServer("boshi-memory", version="6.1.0")

    @server.tool(
        name="boshi_search",
        description="搜索伯仕的记忆。支持多策略检索（语义向量+全文混合+知识图谱），找到最相关的记忆。",
    )
    def boshi_search(
        query: str,
        top_k: int = 5,
        source: Literal["all", "vector", "hybrid", "graph"] = "all",
    ) -> str:
        """搜索记忆：query 搜索查询文本；top_k 返回条数默认5；source 检索策略 all=融合(hybrid+图谱), vector=语义, hybrid=语义+全文混合, graph=图谱"""
        result = search(query=query, top_k=top_k, source=source)
        return json.dumps(result, ensure_ascii=False, indent=2)

    @server.tool(
        name="boshi_save",
        description="向伯仕记忆系统存入一条记忆/事实。适合保存用户偏好、项目决策、重要信息等。",
    )
    def boshi_save(content: str, topic: str = "external", metadata: Optional[dict] = None) -> str:
        """存入记忆：content 记忆内容；topic 主题标签默认 external；metadata 附加元数据（可选）"""
        result = save(content=content, topic=topic, metadata=metadata)
        return json.dumps(result, ensure_ascii=False, indent=2)

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
    def boshi_profile() -> str:
        """用户画像摘要：当前热区话题、记忆总数、最近记忆"""
        result = profile()
        return json.dumps(result, ensure_ascii=False, indent=2)

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
    def boshi_recent(n: int = 10) -> str:
        """最近记忆：n 返回条数默认10"""
        result = recent(n=n)
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
