#!/usr/bin/env python3
"""
伯仕记忆系统 CLI 🦄
====================
命令行接口，用于在终端直接操作伯仕记忆。

用法:
  boshi search "查询文本" [--top-k 5] [--source all|vector|keyword|graph]
                [--since 7d|2026-10-01] [--until ...]
  boshi save "记忆内容" [--topic 主题]
  boshi edit <memory_id> [--content 新正文] [-f 文件] [--meta k=v ...] [--topic 主题]
  boshi delete <memory_id>
  boshi status
  boshi profile
  boshi graph <实体名> [--depth 2]
  boshi graph-add-node <名称> [--type 类型] [--attr 属性]
  boshi graph-add-edge <起点> <终点> <关系>
  boshi recent [10]
  boshi brief
  boshi export [--out 路径] [--md] [--scope self|all]
  boshi restore <快照.jsonl> [--dry-run]
  boshi chain <memory_id>
  boshi conflicts [--topic 主题] [--limit 20] [--threshold 0.85]
  boshi model [--show] [--rebuild] [--key identity] [--scope self|all]
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
    recent, brief, edit, export_snapshot, export_markdown, restore_snapshot,
    fact_chain, detect_conflicts,
    get_mental_model, rebuild_mental_model,
)


def _parse_when(s):
    """'7d' / '24h' / '30m' / '2026-10-01' / ISO / epoch → Unix 秒（解析不了返回 None）。

    相对时间按"当前时刻往前推"，方便随手问"最近一周拍板的决定"。
    """
    if not s:
        return None
    s = str(s).strip()
    units = {"m": 60, "h": 3600, "d": 86400, "w": 604800}
    if s and s[-1].lower() in units and s[:-1].replace(".", "", 1).isdigit():
        from time import time
        return time() - float(s[:-1]) * units[s[-1].lower()]
    try:
        return float(s)
    except ValueError:
        pass
    from datetime import datetime
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d", "%Y/%m/%d"):
        try:
            return datetime.strptime(s, fmt).timestamp()
        except ValueError:
            continue
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


# ═══════════════════════════════════════════
# 输出格式化
# ═══════════════════════════════════════════

def _fmt_search(result: dict):
    """格式化搜索结果"""
    print(f"🔍 查询: {result['query']}")
    sources = result.get("sources", {})
    if sources:
        parts = []
        # 后端返回的 sources 键不定：vector / kw / fused / hybrid / keyword / graph
        _label = {"vector": "语义", "vec": "语义", "keyword": "关键词", "kw": "关键词",
                  "fused": "融合", "hybrid": "混合", "graph": "图谱"}
        for k, n in sources.items():
            if n:
                parts.append(f"{_label.get(k, k)}{n}")
        print(f"📊 来源: {' + '.join(parts) if parts else '—'}")
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
    if result.get("backend"):
        print(f"  检索后端: {result['backend']}")
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
    result = search(query=args.query, top_k=args.top_k, source=args.source,
                    scope=getattr(args, "scope", None),
                    since=_parse_when(getattr(args, "since", None)),
                    until=_parse_when(getattr(args, "until", None)))
    _fmt_search(result)


def cmd_save(args):
    result = save(content=args.content, topic=args.topic,
                  profile=getattr(args, "profile", None))
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


def cmd_edit(args):
    content = args.content
    if getattr(args, "file", None):
        with open(args.file, "r", encoding="utf-8") as f:
            content = f.read()
    meta_patch = None
    if getattr(args, "meta", None):
        meta_patch = {}
        for item in args.meta:
            if "=" not in item:
                print(f"❌ --meta 需要 k=v 形式: {item}")
                return
            k, v = item.split("=", 1)
            # k= （空值）表示删除该键，与 edit_memory 的 None 语义对齐
            meta_patch[k] = v if v != "" else None
    result = edit(memory_id=args.memory_id, content=content, meta_patch=meta_patch,
                  topic=getattr(args, "topic", None),
                  is_latest=getattr(args, "is_latest", None))
    if result.get("success"):
        changed = result.get("changed") or ["（无变化）"]
        print(f"✅ 已更新 {result['memory_id']}: {', '.join(changed)}")
    else:
        print(f"❌ 更新失败: {result.get('error')}")


def cmd_export(args):
    scope = getattr(args, "scope", None) or "all"
    if getattr(args, "md", False):
        result = export_markdown(path=args.out, scope=scope, limit=args.limit)
    else:
        result = export_snapshot(path=args.out, scope=scope,
                                 include_graph=not getattr(args, "no_graph", False))
    if result.get("success"):
        print(f"✅ 已导出 {result['count']} 条 → {result['path']}")
    else:
        print(f"❌ 导出失败: {result.get('error')}")


def cmd_restore(args):
    result = restore_snapshot(args.path, dry_run=getattr(args, "dry_run", False))
    if result.get("success"):
        tag = "（演练，未写入）" if getattr(args, "dry_run", False) else ""
        print(f"✅ 回灌完成{tag}: 新增 {result['added']} / 跳过 {result['skipped']} "
              f"/ 失败 {result.get('error_count', 0)}")
        for e in (result.get("errors") or [])[:5]:
            print(f"   ⚠ {e}")
    else:
        print(f"❌ 回灌失败: {result.get('error')}")


def cmd_chain(args):
    result = fact_chain(args.memory_id)
    if not result.get("success"):
        print(f"❌ {result.get('error')}")
        return
    print(f"🔗 {result['summary']}")
    print()
    for i, n in enumerate(result.get("chain") or []):
        flag = "最新" if n["is_latest"] else "已被取代"
        print(f"  {i}. [{flag}] {n['created_at']} `{n['id']}`")
        print(f"     {(n['content'] or '')[:100]}")
    for n in result.get("descendants") or []:
        print(f"  ↳ 旧版 {n['created_at']} `{n['id']}` — {(n['content'] or '')[:70]}")


def cmd_conflicts(args):
    result = detect_conflicts(topic=args.topic, top_k=args.limit,
                              sim_threshold=args.threshold)
    if not result.get("success"):
        print(f"❌ {result.get('error')}")
        return
    items = result.get("items") or []
    if not items:
        print("✅ 未发现槽位互斥的冲突候选")
        return
    print(f"⚠ 冲突候选 {len(items)} 组（相似度 ≥ {args.threshold}，槽位取值互斥）")
    for it in items:
        print(f"\n  sim={it['similarity']}  A={it['a_time']} `{it['a']}`  B={it['b_time']} `{it['b']}`")
        for c in it["conflicts"]:
            print(f"    {c['key']}: {c['a']}  vs  {c['b']}")
        print(f"    A: {it['a_content'][:90]}")
        print(f"    B: {it['b_content'][:90]}")
    print("\n裁决：boshi edit ... 或调用 resolve_conflict(winner, loser, reason)")


def cmd_model(args):
    """打印 / 重建心智模型预计算页。"""
    scope = args.scope or "self"
    if args.rebuild:
        keys = [args.key] if args.key else None
        r = rebuild_mental_model(keys=keys, scope=scope)
        if not r.get("success", True):
            print(f"❌ {r.get('error')}")
            return
        if r.get("disabled"):
            print("⚠ BOSHI_MENTAL_MODEL=0，心智模型页已关闭")
            return
        print(f"🔧 重建完成 {r.get('ms')}ms  profile={r.get('profile')} scope={r.get('scope')}")
        print(f"   已建：{', '.join(r.get('built') or []) or '（无）'}")
        if r.get("empty"):
            print(f"   空页：{', '.join(r['empty'])}")
    r = get_mental_model(scope=scope)
    if not r.get("success"):
        print(f"❌ {r.get('error')}")
        return
    text = (r.get("text") or "").strip()
    if not text:
        print("（暂无心智模型页，运行 `boshi model --rebuild` 生成）")
        return
    print(text)
    pages = r.get("pages") or {}
    print(f"\n— {len(pages)} 页 / {len(text)} 字符 / profile={r.get('profile')} scope={r.get('scope')} / 读耗时 {r.get('ms')}ms")


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
    p_search.add_argument("--scope", choices=["self", "all"], default=None,
                          help="记忆归属范围：self=只读本 profile（默认，见 ~/.boshi/profiles.json）；all=全库")
    p_search.add_argument("--since", default=None,
                          help="起始时间：7d / 24h / 2026-10-01 / ISO / epoch")
    p_search.add_argument("--until", default=None,
                          help="截止时间：同上（默认不限）")
    p_search.set_defaults(func=cmd_search)

    # save
    p_save = subparsers.add_parser("save", help="存入记忆")
    p_save.add_argument("content", help="记忆内容")
    p_save.add_argument("--topic", default="external", help="主题标签")
    p_save.add_argument("--profile", default=None, help="记忆归属标识（默认按当前 HERMES_HOME 推导）")
    p_save.set_defaults(func=cmd_save)

    # delete
    p_delete = subparsers.add_parser("delete", help="删除记忆")
    p_delete.add_argument("memory_id", help="记忆ID")
    p_delete.set_defaults(func=cmd_delete)

    # edit
    p_edit = subparsers.add_parser("edit", help="就地修改记忆（改正文会重算 embedding 与槽位）")
    p_edit.add_argument("memory_id", help="记忆ID")
    p_edit.add_argument("--content", default=None, help="新的正文")
    p_edit.add_argument("-f", "--file", default=None, help="从文件读新正文")
    p_edit.add_argument("--meta", action="append", default=None,
                        help="meta 补丁 k=v，可多次；k= 空值表示删除该键")
    p_edit.add_argument("--topic", default=None, help="改主题标签")
    p_edit.add_argument("--is-latest", dest="is_latest", action="store_true",
                        default=None, help="标记为最新版")
    p_edit.set_defaults(func=cmd_edit)

    # export
    p_exp = subparsers.add_parser("export", help="导出快照（JSONL 可回灌 / --md 人读版）")
    p_exp.add_argument("--out", default=None, help="输出路径（默认 ~/.boshi/snapshots/）")
    p_exp.add_argument("--md", action="store_true", help="导出 Markdown 人读版")
    p_exp.add_argument("--scope", choices=["self", "all"], default=None, help="导出范围")
    p_exp.add_argument("--limit", type=int, default=5000, help="md 导出的最大条数")
    p_exp.add_argument("--no-graph", dest="no_graph", action="store_true",
                       help="不导出知识图谱边")
    p_exp.set_defaults(func=cmd_export)

    # restore
    p_res = subparsers.add_parser("restore", help="按快照回灌缺失记录（不覆盖现状）")
    p_res.add_argument("path", help="快照 JSONL 路径")
    p_res.add_argument("--dry-run", dest="dry_run", action="store_true",
                       help="只统计不写入")
    p_res.set_defaults(func=cmd_restore)

    # chain
    p_chain = subparsers.add_parser("chain", help="同一事实的版本链（新→旧）")
    p_chain.add_argument("memory_id", help="记忆ID（任一版本）")
    p_chain.set_defaults(func=cmd_chain)

    # conflicts
    p_conf = subparsers.add_parser("conflicts", help="冲突候选检测（零模型，槽位互斥）")
    p_conf.add_argument("--topic", default=None, help="只看某个主题")
    p_conf.add_argument("--limit", type=int, default=20, help="最多返回几组")
    p_conf.add_argument("--threshold", type=float, default=0.85, help="余弦相似度门槛")
    p_conf.set_defaults(func=cmd_conflicts)

    # mental_model（P2 ④ 心智模型预计算页）
    p_model = subparsers.add_parser("model", help="心智模型页（固定问题的预计算答案）")
    p_model.add_argument("--show", action="store_true", help="直接打印已存页面（默认行为）")
    p_model.add_argument("--rebuild", action="store_true", help="按当前记忆重建页面")
    p_model.add_argument("--key", default=None,
                         help="只重建某一页：identity/env/active_projects/pitfalls/open_threads")
    p_model.add_argument("--scope", default="self", choices=["self", "all"],
                         help="self=只看本 profile（默认）；all=全库")
    p_model.set_defaults(func=cmd_model)

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