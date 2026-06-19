#!/usr/bin/env python3
"""注册资源位置索引到 ChromaDB（type=resource_locator）——让系统知道自己的资源在哪"""
import sys, os, uuid
sys.path.insert(0, os.path.expanduser('~/.boshi'))
from chroma_bridge import add_memories_batch, search_memory

BOSHI_HOME = os.path.expanduser('~/.boshi')
HERMES_HOME = os.path.expanduser('~/AppData/Local/hermes')
SKILLS_DIR = os.path.join(HERMES_HOME, 'skills', 'boshi-memory')
CHROMA_DB = os.path.join(BOSHI_HOME, 'chroma_db')
DESKTOP_DOCS = r'C:\Users\Administrator\Desktop\伯仕技术文档'
OBSIDIAN_VAULT = r'D:\ObsidianVault\虚拟寺院知识库'
WORKSTATION = os.path.join(BOSHI_HOME, 'workstation', '伯仕工作台.pyw')
SHARED = r'C:\Users\.openclaw\shared'

entries = [
    # 记忆系统核心
    {
        "content": "记忆系统核心存储 — ChromaDB（全部记忆数据的统一主存储）",
        "metadata": {
            "type": "resource_locator",
            "resource": "memory_store",
            "path": CHROMA_DB,
            "description": "伯仕记忆系统主存储，向量数据库，10,779条记录",
            "access_tool": "boshi_search / boshi_conclude",
            "isLatest": True,
        }
    },
    {
        "content": "原始会话全量存档 — Hermes state.db",
        "metadata": {
            "type": "resource_locator",
            "resource": "session_archive",
            "path": os.path.join(HERMES_HOME, 'state.db'),
            "description": "Hermes 自带 SQLite，所有原始对话永久保留，FTS5全文搜索",
            "access_tool": "session_search",
            "isLatest": True,
        }
    },
    {
        "content": "工作日志 — 记忆系统的操作记录，存储在 ChromaDB type=work_log",
        "metadata": {
            "type": "resource_locator",
            "resource": "work_log",
            "path": "ChromaDB (type=work_log)",
            "description": "伯仕记忆系统的所有操作日志，含日期/操作类型/详情",
            "access_tool": "boshi_search('工作日志') 或 boshi_search('', where={'type':'work_log'})",
            "doc_index": "ChromaDB type=work_log",
            "doc_file": os.path.join(DESKTOP_DOCS, '伯仕工作日志.md'),
            "isLatest": True,
        }
    },
    {
        "content": "资源索引 — 所有资源位置的统一索引，存储在 ChromaDB type=resource_locator",
        "metadata": {
            "type": "resource_locator",
            "resource": "resource_index",
            "path": "ChromaDB (type=resource_locator)",
            "description": "本条目。记录伯仕所有核心资源的位置。system_prompt_block 自动注入。",
            "access_tool": "自动注入系统提示词 / boshi_search('resource_locator')",
            "isLatest": True,
        }
    },
    # 技术文档和架构信息
    {
        "content": "技术文档索引 — 所有技术文档/架构文档的注册表，存储在 ChromaDB type=doc_index",
        "metadata": {
            "type": "resource_locator",
            "resource": "doc_index",
            "path": "ChromaDB (type=doc_index)",
            "description": "已注册 13 个文档（含正本和桌面副本），每个带完整路径、版本、状态",
            "access_tool": "boshi_search('doc_index') 或 boshi_search('', where={'type':'doc_index'})",
            "isLatest": True,
        }
    },
    {
        "content": "最新完整技术文档 — Hermes Skill（SKILL.md，持续更新）",
        "metadata": {
            "type": "resource_locator",
            "resource": "tech_doc_latest",
            "path": os.path.join(SKILLS_DIR, 'SKILL.md'),
            "description": "boshi-memory skill，当前最新最完整的技术文档，v5.8",
            "access_tool": "skill_view('boshi-memory')",
            "isLatest": True,
        }
    },
    {
        "content": "技术手册正本（过时）— ~/.boshi/docs/Hermes集成技术手册.md",
        "metadata": {
            "type": "resource_locator",
            "resource": "tech_doc_manual",
            "path": os.path.join(BOSHI_HOME, 'docs', 'Hermes集成技术手册.md'),
            "description": "旧版技术手册，停在5月27日，需从 SKILL.md 同步",
            "status": "outdated",
            "isLatest": True,
        }
    },
    {
        "content": "桌面副本 — 伯仕技术文档文件夹",
        "metadata": {
            "type": "resource_locator",
            "resource": "desktop_docs",
            "path": DESKTOP_DOCS,
            "description": "桌面伯仕技术文档目录，含 v3.2~v5.7 架构文档、工作日志副本、自检清单等",
            "access_tool": "桌面直接打开",
            "isLatest": True,
        }
    },
    # 知识库
    {
        "content": "虚拟寺院知识库 — Obsidian 主目录",
        "metadata": {
            "type": "resource_locator",
            "resource": "knowledge_base",
            "path": OBSIDIAN_VAULT,
            "description": "虚拟寺院项目技术知识库，UE5/PCG/AI僧人/古建筑等调研资料",
            "access_tool": "Obsidian 直接打开",
            "isLatest": True,
        }
    },
    # 工作台和共享
    {
        "content": "伯仕工作台 — 7681 网页界面",
        "metadata": {
            "type": "resource_locator",
            "resource": "workstation",
            "path": WORKSTATION,
            "description": "伯仕工作台主页，WebSocket 实时对话",
            "url": "http://127.0.0.1:7681",
            "access_tool": "浏览器打开 http://127.0.0.1:7681",
            "isLatest": True,
        }
    },
    {
        "content": "三友共享目录 — 团队协作文件共享",
        "metadata": {
            "type": "resource_locator",
            "resource": "shared_dir",
            "path": SHARED,
            "description": "三友工作间共享目录",
            "url": "http://127.0.0.1:8888",
            "access_tool": "文件系统访问",
            "isLatest": True,
        }
    },
    {
        "content": "boshi 插件 — Hermes 集成入口",
        "metadata": {
            "type": "resource_locator",
            "resource": "boshi_plugin",
            "path": os.path.join(HERMES_HOME, 'plugins', 'boshi'),
            "description": "伯仕记忆系统的 Hermes 插件，含 __init__.py（927行）等",
            "isLatest": True,
        }
    },
    {
        "content": "boshi 核心库 — chroma_bridge.py 数据操作层",
        "metadata": {
            "type": "resource_locator",
            "resource": "chroma_bridge",
            "path": os.path.join(BOSHI_HOME, 'chroma_bridge.py'),
            "description": "ChromaDB 数据操作封装，230行",
            "isLatest": True,
        }
    },
    {
        "content": "boshi 配置文件 — config.yaml（Hermes 配置）",
        "metadata": {
            "type": "resource_locator",
            "resource": "hermes_config",
            "path": os.path.join(HERMES_HOME, 'config.yaml'),
            "description": "Hermes 主配置，memory.provider=boshi",
            "isLatest": True,
        }
    },
]

# 先看看是否已有 resource_locator
existing = search_memory("resource_locator", top_k=3, where={"type": "resource_locator"})
print(f"当前已有 resource_locator 记录: {len(existing)} 条")

# 写入
batch = []
for entry in entries:
    batch.append({
        "content": entry["content"],
        "metadata": entry["metadata"],
        "id": str(uuid.uuid4())
    })

result = add_memories_batch(batch)
print(f"写入: 成功 {result.get('added',0)} / 失败 {result.get('failed',0)}")

# 验证
print("\n===== 资源索引 =====")
ver = search_memory("", top_k=20, where={"type": "resource_locator"})
for r in sorted(ver, key=lambda x: x['metadata'].get('resource','')):
    m = r['metadata']
    print(f"  [{m.get('resource','?'):20s}] {m.get('description','')[:60]}")
    print(f"    path: {m.get('path','?')[:70]}")