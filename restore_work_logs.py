#!/usr/bin/env python3
"""将完整工作日志写入 ChromaDB（按 v5.8 架构：type=work_log + isLatest）"""
import sys, os, uuid, time
sys.path.insert(0, os.path.expanduser('~/.boshi'))
from chroma_bridge import add_memories_batch, search_memory

work_logs = [
    {
        "content": "【2026-06-11】CN Desktop Hermes 遗留排查 + ChromaDB 现状全面盘点",
        "metadata": {
            "type": "work_log",
            "action": "audit",
            "isLatest": True,
            "date": "2026-06-11",
            "source": "cli",
            "project": "记忆系统",
            "details": "排查 CN Desktop Hermes 遗留痕迹（graph/目录僵尸文件）和 ChromaDB 数据状态（10779条总记录，8227条unknown占位）。确认工作日志和项目日志在 ChromaDB 中为 0 条，三份核心文档全部未入库。"
        }
    },
    {
        "content": "【2026-06-11】当前对话还在进行中：工作日志重建、CN Desktop 事件确认、Supermemory 方向评估",
        "metadata": {
            "type": "work_log",
            "action": "in_progress",
            "isLatest": True,
            "date": "2026-06-11",
            "source": "cli",
            "project": "记忆系统",
            "details": "与乐之确认了 6月8日 CN Desktop Hermes 安装后擅自修改 ~/.boshi/ 下文件并写入不兼容 schema 数据导致 ChromaDB 损坏的事实。当前正在重建工作日志到 ChromaDB。"
        }
    },
    {
        "content": "【2026-06-10】ChromaDB 索引修复 + hot.json 清理 + 桌面文档同步",
        "metadata": {
            "type": "work_log",
            "action": "fix",
            "isLatest": True,
            "date": "2026-06-10",
            "source": "cli",
            "project": "记忆系统",
            "details": "ChromaDB query() 返回 documents=null，metadata 字段名被时间戳 float 覆盖（10587条中8227条损坏）。修复方式：重建新 collection 逐批清洗数据。结果：10587/10587 条全量恢复。hot.json 冻结为 .migrated。桌面新增 v5.7 架构文档和工作日志副本。"
        }
    },
    {
        "content": "【2026-06-09】Hermes 升级恢复 + tiered_memory.py 回滚到 v3.0",
        "metadata": {
            "type": "work_log",
            "action": "fix",
            "isLatest": True,
            "date": "2026-06-09",
            "source": "cli",
            "project": "记忆系统",
            "details": "Hermes 升级后 memory.provider 被覆盖，重新设回 boshi。tiered_memory.py 恢复到 v3.0，v4.0 实验性改动归档到 changes_20260609_v4_attempt/。"
        }
    },
    {
        "content": "【2026-06-08】CN Desktop Hermes 安装并擅自修改 ~/.boshi/ — 导致后续 ChromaDB 损坏",
        "metadata": {
            "type": "work_log",
            "action": "incident",
            "isLatest": True,
            "date": "2026-06-08",
            "source": "cli",
            "project": "记忆系统",
            "details": "乐之安装 Hermes Agent CN Desktop 桌面版，让其中存放的 Hermes 实例分析 Supermemory。该实例误以为可共享伯仕 ChromaDB，以不兼容的 Supermemory schema（isLatest/tag_type/rel_type）写入数据，创建了 117 条 type=relation 记录和 ~/.boshi/graph/ 下 3 个僵尸文件。这些不兼容数据导致了后续 content=null 和 metadata 错位损坏。"
        }
    },
    {
        "content": "【2026-06-01】hot.json 彻底迁移到 ChromaDB + 关闭 Hermes 内置记忆",
        "metadata": {
            "type": "work_log",
            "action": "migration",
            "isLatest": True,
            "date": "2026-06-01",
            "source": "cli",
            "project": "记忆系统",
            "details": "_save_hot() 改为批量写入 ChromaDB（1446条，分49批写入）。hot.json 冻结为 hot.json.migrated。config.yaml 设置 memory_enabled=false, user_profile_enabled=false。MEMORY.md + USER.md 全量迁移至 ChromaDB。三层自愈架构部署。"
        }
    },
    {
        "content": "【2026-06-01】时间戳归一化修复 — 8244条字符串时间戳→float 迁移",
        "metadata": {
            "type": "work_log",
            "action": "bug_fix",
            "isLatest": True,
            "date": "2026-06-01",
            "source": "cli",
            "project": "记忆系统",
            "details": "chroma_bridge.py 新增 _normalize_metadata() 函数，在 add_memory() 入口统一将 ISO 字符串时间戳转为 float。8244条已有数据执行 SQLite 批量 UPDATE，零字符串残留。"
        }
    },
    {
        "content": "【2026-05-30】ChromaDB Rust 崩溃恢复 — HNSW compactor 段错误",
        "metadata": {
            "type": "work_log",
            "action": "bug_fix",
            "isLatest": True,
            "date": "2026-05-30",
            "source": "cli",
            "project": "记忆系统",
            "details": "批量 1444 条一次性写入撑爆 HNSW compactor，Rust 层段错误导致 Hermes 进程消失。修复后所有批量写入强制分批（30条/批+2秒间隔）。"
        }
    },
    {
        "content": "【2026-05-26】v4.0 统一 Chroma 存储 — 剔除 hot.json/cold.json 物理隔离",
        "metadata": {
            "type": "work_log",
            "action": "feature",
            "isLatest": True,
            "date": "2026-05-26",
            "source": "cli",
            "project": "记忆系统",
            "details": "存储从三套（hot.json + Chroma + cold.json）统一为纯 Chroma + tier 热度标签。hot.json 649条迁移到 Chroma。新增 conversation_turn 兜底写入和 raw_conversation 类型。"
        }
    },
    {
        "content": "【2026-05-23】sync_turn 实时写入 + prefetch 扩展到2天跨入口摘要",
        "metadata": {
            "type": "work_log",
            "action": "feature",
            "isLatest": True,
            "date": "2026-05-23",
            "source": "cli",
            "project": "记忆系统",
            "details": "sync_turn 每轮对话后即时写 ChromaDB。prefetch 跨入口摘要扩展到最近2天。extract_facts 双路径写入（事实提取 + conversation_turn 兜底）。修复短消息被过滤问题。"
        }
    },
    {
        "content": "【2026-05-23】僵尸进程清理 — 114个空转进程 + 3个僵尸 cron",
        "metadata": {
            "type": "work_log",
            "action": "cleanup",
            "isLatest": True,
            "date": "2026-05-23",
            "source": "cli",
            "project": "记忆系统",
            "details": "清理 114 个空转进程（bridge_watchdog 看门狗 bug）。删除 3 个僵尸 cron。清理 hot.json 765条旧数据。"
        }
    },
]

# 批量写入（一次性加载模型，避免每次 add_memory 重新初始化 embedding）
entries_batch = []
for entry in work_logs:
    entries_batch.append({
        "content": entry["content"],
        "metadata": entry["metadata"],
        "id": str(uuid.uuid4())
    })

print(f"开始写入 {len(entries_batch)} 条工作日志...")
result = add_memories_batch(entries_batch)
print(f"批量写入: 成功 {result.get('added',0)} / 失败 {result.get('failed',0)}")
if result.get('errors'):
    for e in result['errors'][:5]:
        print(f"  ❌ {e}")

print(f"\n===== 验证 =====")
results = search_memory("工作日志", top_k=5, where={"type": "work_log"})
print(f"work_log 类型记录数: {len(results)}")
for r in results:
    meta = r['metadata']
    print(f"  [{meta.get('date','?')}] {r['content'][:60]}")
    print(f"    action={meta.get('action','?')}  isLatest={meta.get('isLatest','?')}")