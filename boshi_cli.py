#!/usr/bin/env python3
"""
伯仕记忆系统 CLI 🦄
====================
命令行接口，用于在终端直接操作伯仕记忆。

用法:
  boshi search "查询文本" [--top-k 5] [--source all|vector|keyword|graph]
  boshi save "记忆内容" [--topic 主题]
  boshi delete <memory_id>
  boshi status
  boshi profile
  boshi graph <实体名> [--depth 2]
  boshi graph-add-node <名称> [--type 类型] [--attr 属性]
  boshi graph-add-edge <起点> <终点> <关系>
  boshi recent [10]
  boshi brief
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
# 输出格式化
# ═══════════════════════════════════════════

def _fmt_search(result: dict):
    """格式化搜索结果"""
    print(f"🔍 查询: {result['query']}")
    sources = result.get("sources", {})
    if sources:
        parts = []
        if sources.get("vector"):
            parts.append(f"语义{sources['vector']}")
        if sources.get("keyword"):
            parts.append(f"关键词{sources['keyword']}")
        if sources.get("graph"):
            parts.append(f"图谱{sources['graph']}")
        print(f"📊 来源: {' + '.join(parts)}")
    print(f"📋 共 {result['total']} 条结果:\n")
    for i, r in enumerate(result["results"], 1):
        score = r.get("score", 0)
        src = r.get("source", "")
        content = r.get("content", "")
        meta = r.get("metadata", {})
        topic = meta.get("topic", "")
        src_tag = f"[{src}]" if src else ""
        topic_tag = f"({topic})" if topic else ""
        print(f"  {i}. {src_tag} {score:.2f} {content[:100]} {topic_tag}")


def _fmt_profile(result: dict):
    """格式化画像"""
    print(f"👤 用户画像")
    print(f"  热区话题: {result.get('hot_topic', '无')}")
    print(f"  记忆总数: {result.get('total_memories', 0)}")
    if result.get("recent_memories"):
        print(f"  最近记忆:")
        for m in result["recent_memories"]:
            topic = m.get("topic", "")
            topic_tag = f"({topic})" if topic else ""
            print(f"    · {m['content'][:80]} {topic_tag}")


def _fmt_status(result: dict):
    """格式化状态"""
    print(f"🦄 伯仕记忆系统")
    print(f"  记忆总数: {result.get('total_memories', 0)}")
    kg = result.get("knowledge_graph", {})
    print(f"  知识图谱: {kg.get('nodes', 0)} 节点 / {kg.get('edges', 0)} 边")
    print(f"  数据目录: {result.get('chroma_dir', '未知')}")


def _fmt_graph(result: dict):
    """格式化图谱查询"""
    entity = result.get("entity", "")
    nodes = result.get("nodes", {})
    edges = result.get("edges", [])
    print(f"🔗 知识图谱 — 从「{entity}」出发")
    if not nodes:
        print(f"  未找到相关节点")
        return
    print(f"  节点 ({len(nodes)}):")
    for name, node in nodes.items():
        ntype = node.get("type", "")
        attr = node.get("attr", "")
        type_tag = f"({ntype})" if ntype else ""
        attr_tag = f"— {attr}" if attr else ""
        print(f"    · {name} {type_tag} {attr_tag}")
    if edges:
        print(f"  关系 ({len(edges)}):")
        for e in edges[:10]:
            print(f"    · {e['from']} → {e['to']} ({e['relation']})")


def _fmt_recent(results: list):
    """格式化最近记忆"""
    print(f"📋 最近 {len(results)} 条记忆:")
    for i, r in enumerate(results, 1):
        content = r.get("content", "")
        meta = r.get("metadata", {})
        topic = meta.get("topic", "")
        topic_tag = f"({topic})" if topic else ""
        print(f"  {i}. {content[:100]} {topic_tag}")


# ═══════════════════════════════════════════
# 子命令处理
# ═══════════════════════════════════════════

def cmd_search(args):
    result = search(query=args.query, top_k=args.top_k, source=args.source)
    _fmt_search(result)


def cmd_save(args):
    result = save(content=args.content, topic=args.topic)
    if result.get("success"):
        print(f"✅ 已保存 (ID: {result['memory_id']})")
        print(f"   {result['content_preview']}")
    else:
        print(f"❌ 保存失败: {result}")


def cmd_delete(args):
    result = delete(memory_id=args.memory_id)
    if result.get("success"):
        print(f"✅ 已删除: {result['deleted']}")
    else:
        print(f"❌ 删除失败: {result}")


def cmd_status(args):
    result = status()
    _fmt_status(result)


def cmd_profile(args):
    result = profile()
    _fmt_profile(result)


def cmd_graph(args):
    result = graph_query(entity=args.entity, max_depth=args.depth)
    _fmt_graph(result)


def cmd_graph_add_node(args):
    result = graph_add_node(name=args.name, type=args.type, attr=args.attr)
    print(f"✅ 节点已添加: {json.dumps(result, ensure_ascii=False)}")


def cmd_graph_add_edge(args):
    result = graph_add_edge(from_name=args.from_name, to_name=args.to_name, relation=args.relation)
    print(f"✅ 关系已添加: {json.dumps(result, ensure_ascii=False)}")


def cmd_recent(args):
    results = recent(n=args.n)
    _fmt_recent(results)


def cmd_brief(args):
    result = brief()
    _fmt_profile(result)


# ═══════════════════════════════════════════
# CLI 入口
# ═══════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(
        description="🦄 伯仕记忆系统 CLI",
        prog="boshi",
    )
    subparsers = parser.add_subparsers(dest="command", help="子命令")

    # search
    p_search = subparsers.add_parser("search", help="搜索记忆")
    p_search.add_argument("query", help="搜索查询文本")
    p_search.add_argument("--top-k", type=int, default=5, help="返回条数")
    p_search.add_argument("--source", choices=["all", "vector", "hybrid", "graph"], default="all", help="检索策略")
    p_search.set_defaults(func=cmd_search)

    # save
    p_save = subparsers.add_parser("save", help="存入记忆")
    p_save.add_argument("content", help="记忆内容")
    p_save.add_argument("--topic", default="external", help="主题标签")
    p_save.set_defaults(func=cmd_save)

    # delete
    p_delete = subparsers.add_parser("delete", help="删除记忆")
    p_delete.add_argument("memory_id", help="记忆ID")
    p_delete.set_defaults(func=cmd_delete)

    # status
    p_status = subparsers.add_parser("status", help="记忆库状态")
    p_status.set_defaults(func=cmd_status)

    # profile
    p_profile = subparsers.add_parser("profile", help="用户画像")
    p_profile.set_defaults(func=cmd_profile)

    # graph
    p_graph = subparsers.add_parser("graph", help="知识图谱查询")
    p_graph.add_argument("entity", help="起始实体名")
    p_graph.add_argument("--depth", type=int, default=2, help="遍历深度")
    p_graph.set_defaults(func=cmd_graph)

    # graph-add-node
    p_gan = subparsers.add_parser("graph-add-node", help="添加图谱节点")
    p_gan.add_argument("name", help="节点名称")
    p_gan.add_argument("--type", default="", help="节点类型")
    p_gan.add_argument("--attr", default="", help="节点属性")
    p_gan.set_defaults(func=cmd_graph_add_node)

    # graph-add-edge
    p_gae = subparsers.add_parser("graph-add-edge", help="添加图谱关系")
    p_gae.add_argument("from_name", help="起点实体")
    p_gae.add_argument("to_name", help="终点实体")
    p_gae.add_argument("relation", help="关系类型")
    p_gae.set_defaults(func=cmd_graph_add_edge)

    # recent
    p_recent = subparsers.add_parser("recent", help="最近记忆")
    p_recent.add_argument("n", type=int, nargs="?", default=10, help="条数")
    p_recent.set_defaults(func=cmd_recent)

    # brief
    p_brief = subparsers.add_parser("brief", help="会话简报")
    p_brief.set_defaults(func=cmd_brief)

    args = parser.parse_args()
    if not args.command:
        parser.print_help()
        sys.exit(0)

    args.func(args)


if __name__ == "__main__":
    main()