#!/usr/bin/env python3
"""将架构信息和技术文档的索引注册到 ChromaDB（type=doc_index + source=docs/ 路径）"""
import sys, os, uuid
sys.path.insert(0, os.path.expanduser('~/.boshi'))
from chroma_bridge import add_memories_batch, search_memory

BOSHI_HOME = os.path.expanduser('~/.boshi')
DESKTOP_DOCS = r'C:\Users\Administrator\Desktop\伯仕技术文档'

entries = [
    # === 正本（~/.boshi/）===
    {
        "content": "技术文档：Hermes集成技术手册.md — 伯仕记忆系统与 Hermes 框架集成的详细技术手册，含插件架构、配置、自愈机制",
        "metadata": {
            "type": "doc_index",
            "doc_category": "tech_doc",
            "title": "Hermes集成技术手册.md",
            "version": "v5.7",
            "last_updated": "2026-05-27",
            "primary_path": os.path.join(BOSHI_HOME, "docs", "Hermes集成技术手册.md"),
            "status": "outdated",
            "notes": "停在5月27日，SKILL.md 才是当前最新技术文档。需要同步更新。",
            "isLatest": True,
        }
    },
    {
        "content": "技术架构：记忆系统v5.3技术方案与实现报告.md — 伯仕记忆系统 v5.3 完整技术方案和实现细节",
        "metadata": {
            "type": "doc_index",
            "doc_category": "arch_doc",
            "title": "记忆系统v5.3技术方案与实现报告.md",
            "version": "v5.3",
            "last_updated": "2026-05-28",
            "primary_path": os.path.join(BOSHI_HOME, "记忆系统v5.3技术方案与实现报告.md"),
            "status": "historical",
            "isLatest": True,
        }
    },
    {
        "content": "技术文档：记忆读取优化方案_v2.1.md — 伯仕记忆系统读取层优化方案 v2.1",
        "metadata": {
            "type": "doc_index",
            "doc_category": "tech_doc",
            "title": "记忆读取优化方案_v2.1.md",
            "version": "v2.1",
            "last_updated": "2026-05-28",
            "primary_path": os.path.join(BOSHI_HOME, "记忆读取优化方案_v2.1.md"),
            "status": "historical",
            "isLatest": True,
        }
    },
    {
        "content": "技术文档：EVOLUTION.md — 伯仕自我进化机制框架文档",
        "metadata": {
            "type": "doc_index",
            "doc_category": "tech_doc",
            "title": "EVOLUTION.md",
            "version": "-",
            "last_updated": "2026-05-21",
            "primary_path": os.path.join(BOSHI_HOME, "EVOLUTION.md"),
            "status": "historical",
            "isLatest": True,
        }
    },
    {
        "content": "技术文档：升级后自愈指引.md — Hermes 升级后伯仕系统的自愈恢复操作指引",
        "metadata": {
            "type": "doc_index",
            "doc_category": "tech_doc",
            "title": "升级后自愈指引.md",
            "version": "-",
            "last_updated": "2026-06-06",
            "primary_path": os.path.join(BOSHI_HOME, "升级后自愈指引.md"),
            "status": "current",
            "isLatest": True,
        }
    },
    # === Hermes Skill（最新技术文档正本）===
    {
        "content": "技术文档：SKILL.md（boshi-memory skill）— 伯仕记忆系统当前最新最完整的技术文档，持续更新中",
        "metadata": {
            "type": "doc_index",
            "doc_category": "tech_doc",
            "title": "boshi-memory SKILL.md",
            "version": "v5.8",
            "last_updated": "2026-06-10",
            "primary_path": os.path.join(os.path.expanduser('~'), "AppData", "Local", "hermes", "skills", "boshi-memory", "SKILL.md"),
            "status": "current",
            "notes": "这是当前最新的完整技术文档，持续更新。桌面副本和 ~/.boshi/docs/ Hermes集成技术手册.md 均已过时。",
            "isLatest": True,
        }
    },
    # === 桌面副本 ===
    {
        "content": "架构文档：伯仕记忆系统v5.7_技术架构文档.md（桌面副本）— 最新版架构文档",
        "metadata": {
            "type": "doc_index",
            "doc_category": "arch_doc",
            "title": "伯仕记忆系统v5.7_技术架构文档.md",
            "version": "v5.7",
            "last_updated": "2026-06-10",
            "primary_path": os.path.join(DESKTOP_DOCS, "伯仕记忆系统v5.7_技术架构文档.md"),
            "copy_of": "SKILL.md（主线版本v5.8）",
            "status": "outdated",
            "notes": "桌面副本，停在了v5.7。实际最新版本在SKILL.md（v5.8）。",
            "isLatest": True,
        }
    },
    {
        "content": "架构文档：伯仕记忆系统v5.0_技术架构文档.md（桌面副本）— 历史版本架构文档",
        "metadata": {
            "type": "doc_index",
            "doc_category": "arch_doc",
            "title": "伯仕记忆系统v5.0_技术架构文档.md",
            "version": "v5.0",
            "last_updated": "2026-05-27",
            "primary_path": os.path.join(DESKTOP_DOCS, "伯仕记忆系统v5.0_技术架构文档.md"),
            "status": "historical",
            "isLatest": True,
        }
    },
    {
        "content": "架构文档：伯仕记忆系统v4.0_技术架构文档.md（桌面副本）— 历史版本架构文档",
        "metadata": {
            "type": "doc_index",
            "doc_category": "arch_doc",
            "title": "伯仕记忆系统v4.0_技术架构文档.md",
            "version": "v4.0",
            "last_updated": "2026-05-27",
            "primary_path": os.path.join(DESKTOP_DOCS, "伯仕记忆系统v4.0_技术架构文档.md"),
            "status": "historical",
            "isLatest": True,
        }
    },
    {
        "content": "架构文档：伯仕记忆系统v3.2_技术架构文档.md（桌面副本）— 历史版本架构文档",
        "metadata": {
            "type": "doc_index",
            "doc_category": "arch_doc",
            "title": "伯仕记忆系统v3.2_技术架构文档.md",
            "version": "v3.2",
            "last_updated": "2026-05-27",
            "primary_path": os.path.join(DESKTOP_DOCS, "伯仕记忆系统v3.2_技术架构文档.md"),
            "status": "historical",
            "isLatest": True,
        }
    },
    {
        "content": "技术文档：伯仕自我进化机制_项目框架.md（桌面副本）— 自我进化机制和项目框架",
        "metadata": {
            "type": "doc_index",
            "doc_category": "tech_doc",
            "title": "伯仕自我进化机制_项目框架.md",
            "version": "-",
            "last_updated": "2026-05-27",
            "primary_path": os.path.join(DESKTOP_DOCS, "伯仕自我进化机制_项目框架.md"),
            "status": "historical",
            "isLatest": True,
        }
    },
    {
        "content": "技术文档：伯仕能力自检清单.md（桌面副本）— 伯仕能力自检清单",
        "metadata": {
            "type": "doc_index",
            "doc_category": "tech_doc",
            "title": "伯仕能力自检清单.md",
            "version": "-",
            "last_updated": "2026-05-27",
            "primary_path": os.path.join(DESKTOP_DOCS, "伯仕能力自检清单.md"),
            "status": "historical",
            "isLatest": True,
        }
    },
    {
        "content": "技术文档：记忆系统合并方案v4.0.md（桌面副本）— 记忆系统合并方案历史记录",
        "metadata": {
            "type": "doc_index",
            "doc_category": "tech_doc",
            "title": "记忆系统合并方案v4.0.md",
            "version": "v4.0",
            "last_updated": "2026-05-27",
            "primary_path": os.path.join(DESKTOP_DOCS, "记忆系统合并方案v4.0.md"),
            "status": "historical",
            "isLatest": True,
        }
    },
]

# 先查一下是否已有 doc_index 注册记录
existing = search_memory("doc_index", top_k=3, where={"type": "doc_index"})
print(f"当前已有 doc_index 记录: {len(existing)} 条")
if existing:
    for r in existing:
        m = r['metadata']
        print(f"  [{m.get('status','?')}] {m.get('title','?')} @ {m.get('primary_path','?')}")
print()

# 批量写入
batch = []
for entry in entries:
    batch.append({
        "content": entry["content"],
        "metadata": entry["metadata"],
        "id": str(uuid.uuid4())
    })

result = add_memories_batch(batch)
print(f"写入: 成功 {result.get('added',0)} / 失败 {result.get('failed',0)}")
if result.get('errors'):
    for e in result['errors'][:5]:
        print(f"  ❌ {e}")

# 验证
print("\n===== 写入验证 =====")
ver = search_memory("技术文档 架构", top_k=15, where={"type": "doc_index"})
print(f"doc_index 类型共 {len(ver)} 条:")
for r in sorted(ver, key=lambda x: x['metadata'].get('title','')):
    m = r['metadata']
    print(f"  [{m.get('status','?'):>10}] {m.get('title','?'):40s} | {m.get('primary_path','?')[:70]}")
    print(f"          version={m.get('version','?')} last={m.get('last_updated','?')}")