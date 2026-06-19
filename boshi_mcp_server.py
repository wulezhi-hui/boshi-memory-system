#!/usr/bin/env python3
"""
伯仕记忆系统 MCP Server 🦄
===========================
通过 MCP 协议暴露记忆系统，让任何支持 MCP 的 Agent 都能使用伯仕的记忆。

启动方式:
  python boshi_mcp_server.py                # stdio 模式（Hermes/Claude Code/Cursor 连接）

Hermes 配置 (config.yaml):
  mcp_servers:
    boshi:
      command: "python"
      args: ["~/.boshi/boshi_mcp_server.py"]

暴露的 Tools:
  boshi_search    — 三路融合检索
  boshi_save      — 存入一条记忆
  boshi_delete    — 删除一条记忆
  boshi_status    — 记忆库状态
  boshi_profile   — 用户画像/会话简报
  boshi_graph     — 知识图谱查询
  boshi_graph_add — 添加图谱节点/边
  boshi_recent    — 最近N条记忆
"""
import os
import sys
import json
import argparse

# ── 路径 ──
BOSHI_HOME = os.path.expanduser("~/.boshi")
if BOSHI_HOME not in sys.path:
    sys.path.insert(0, BOSHI_HOME)

from boshi_core import (
    search, save, delete, status, profile,
    graph_query, graph_add_node, graph_add_edge,
    recent, brief,
)


# ═══════════════════════════════════════════
# MCP Server — 使用 mcp SDK (v1.27+)
# ═══════════════════════════════════════════

def create_server():
    """创建 MCP Server 实例，注册所有 tools。"""
    from mcp.server import Server
    from mcp.types import Tool, TextContent

    server = Server("boshi-memory")

    # ── 注册 tool 元数据 ──
    @server.list_tools()
    async def list_tools():
        return [
            Tool(
                name="boshi_search",
                description="搜索伯仕的记忆。支持多策略检索（语义向量+全文混合+知识图谱），找到最相关的记忆。",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "query": {"type": "string", "description": "搜索查询文本"},
                        "top_k": {"type": "integer", "description": "返回条数，默认5", "default": 5},
                        "source": {
                            "type": "string",
                            "enum": ["all", "vector", "hybrid", "graph"],
                            "description": "检索策略：all=融合(hybrid+图谱), vector=语义, hybrid=语义+全文混合, graph=图谱",
                            "default": "all",
                        },
                    },
                    "required": ["query"],
                },
            ),
            Tool(
                name="boshi_save",
                description="向伯仕记忆系统存入一条记忆/事实。适合保存用户偏好、项目决策、重要信息等。",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "content": {"type": "string", "description": "记忆内容"},
                        "topic": {"type": "string", "description": "主题标签，默认 external", "default": "external"},
                        "metadata": {"type": "object", "description": "附加元数据（可选）"},
                    },
                    "required": ["content"],
                },
            ),
            Tool(
                name="boshi_delete",
                description="删除一条记忆（按ID）。",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "memory_id": {"type": "string", "description": "要删除的记忆ID"},
                    },
                    "required": ["memory_id"],
                },
            ),
            Tool(
                name="boshi_status",
                description="查看记忆库状态：总条数、知识图谱节点/边数、ChromaDB路径。",
                inputSchema={"type": "object", "properties": {}},
            ),
            Tool(
                name="boshi_profile",
                description="获取用户画像摘要：当前热区话题、记忆总数、最近记忆。适合作为对话开场的上下文注入。",
                inputSchema={"type": "object", "properties": {}},
            ),
            Tool(
                name="boshi_graph",
                description="查询知识图谱：从指定实体出发，BFS遍历关联实体和关系。用于了解实体间的关联。",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "entity": {"type": "string", "description": "起始实体名"},
                        "max_depth": {"type": "integer", "description": "遍历深度，默认2", "default": 2},
                    },
                    "required": ["entity"],
                },
            ),
            Tool(
                name="boshi_graph_add",
                description="向知识图谱添加节点或关系边。用于手动补充实体关系。",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "action": {
                            "type": "string",
                            "enum": ["add_node", "add_edge"],
                            "description": "添加节点(add_node)还是关系边(add_edge)",
                        },
                        "name": {"type": "string", "description": "节点名（add_node时必填）"},
                        "type": {"type": "string", "description": "节点类型（add_node时可选）"},
                        "attr": {"type": "string", "description": "节点属性（add_node时可选）"},
                        "from_name": {"type": "string", "description": "关系起点（add_edge时必填）"},
                        "to_name": {"type": "string", "description": "关系终点（add_edge时必填）"},
                        "relation": {"type": "string", "description": "关系类型（add_edge时必填）"},
                    },
                    "required": ["action"],
                },
            ),
            Tool(
                name="boshi_recent",
                description="获取最近N条记忆，用于快速了解最近的活动记录。",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "n": {"type": "integer", "description": "返回条数，默认10", "default": 10},
                    },
                },
            ),
        ]

    # ── tool 执行 ──
    @server.call_tool()
    async def call_tool(name: str, arguments: dict):
        try:
            if name == "boshi_search":
                result = search(
                    query=arguments["query"],
                    top_k=arguments.get("top_k", 5),
                    source=arguments.get("source", "all"),
                )
                return [TextContent(type="text", text=json.dumps(result, ensure_ascii=False, indent=2))]

            elif name == "boshi_save":
                result = save(
                    content=arguments["content"],
                    topic=arguments.get("topic", "external"),
                    metadata=arguments.get("metadata"),
                )
                return [TextContent(type="text", text=json.dumps(result, ensure_ascii=False, indent=2))]

            elif name == "boshi_delete":
                result = delete(arguments["memory_id"])
                return [TextContent(type="text", text=json.dumps(result, ensure_ascii=False, indent=2))]

            elif name == "boshi_status":
                result = status()
                return [TextContent(type="text", text=json.dumps(result, ensure_ascii=False, indent=2))]

            elif name == "boshi_profile":
                result = profile()
                return [TextContent(type="text", text=json.dumps(result, ensure_ascii=False, indent=2))]

            elif name == "boshi_graph":
                result = graph_query(
                    entity=arguments["entity"],
                    max_depth=arguments.get("max_depth", 2),
                )
                return [TextContent(type="text", text=json.dumps(result, ensure_ascii=False, indent=2))]

            elif name == "boshi_graph_add":
                action = arguments["action"]
                if action == "add_node":
                    result = graph_add_node(
                        name=arguments["name"],
                        type=arguments.get("type", ""),
                        attr=arguments.get("attr", ""),
                    )
                elif action == "add_edge":
                    result = graph_add_edge(
                        from_name=arguments["from_name"],
                        to_name=arguments["to_name"],
                        relation=arguments["relation"],
                    )
                else:
                    result = {"error": f"Unknown action: {action}"}
                return [TextContent(type="text", text=json.dumps(result, ensure_ascii=False, indent=2))]

            elif name == "boshi_recent":
                results = recent(n=arguments.get("n", 10))
                return [TextContent(type="text", text=json.dumps(results, ensure_ascii=False, indent=2))]

            else:
                return [TextContent(type="text", text=json.dumps({"error": f"Unknown tool: {name}"}))]

        except Exception as e:
            return [TextContent(type="text", text=json.dumps({"error": str(e)}, ensure_ascii=False))]

    return server


async def run_stdio():
    """stdio 模式 — Hermes/Claude Code/Cursor 通过 stdin/stdout 连接"""
    from mcp.server.stdio import stdio_server

    server = create_server()
    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, server.create_initialization_options())


def main():
    parser = argparse.ArgumentParser(description="伯仕记忆系统 MCP Server")
    parser.add_argument("--sse", type=int, metavar="PORT", help="SSE 模式，指定监听端口")
    parser.add_argument("--stdio", action="store_true", help="stdio 模式（默认）")
    args = parser.parse_args()

    import asyncio
    if args.sse:
        asyncio.run(run_sse(args.sse))
    else:
        asyncio.run(run_stdio())


async def run_sse(port: int):
    """SSE 模式 — 通过 HTTP SSE 协议连接（OpenClaw/Claude Code 等）"""
    from mcp.server.sse import SseServerTransport
    from starlette.applications import Starlette
    from starlette.routing import Route
    import uvicorn

    server = create_server()
    sse = SseServerTransport("/messages/")

    async def handle_sse(request):
        async with sse.connect_sse(
            request.scope, request.receive, request._send
        ) as (read_stream, write_stream):
            await server.run(
                read_stream, write_stream,
                server.create_initialization_options()
            )

    async def handle_messages(request):
        await sse.handle_post_message(request.scope, request.receive, request._send)

    app = Starlette(
        routes=[
            Route("/sse", endpoint=handle_sse),
            Route("/messages/", endpoint=handle_messages, methods=["POST"]),
        ]
    )
    print(f"Boshi Memory MCP SSE server running on http://127.0.0.1:{port}/sse")
    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="info")
    server_uv = uvicorn.Server(config)
    await server_uv.serve()


if __name__ == "__main__":
    main()