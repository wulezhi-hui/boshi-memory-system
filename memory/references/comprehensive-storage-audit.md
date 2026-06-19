# 伯仕记忆系统存储审计报告 (2026-06-01)

## 背景

乐之指示：记忆必须统一在一个数据库，热/温/冷靠标签+时间戳解决，不是物理隔离。v4.0 声明了但代码没做到。本次审计全面排查所有存储文件。

## 审计方法

逐个排查 `~/.boshi/memory/` + `~/.boshi/chroma_db/` 所有文件，追踪每文件的写入/读取代码路径，判断是否合理独立还是僵尸数据。

## 审计结果

### ✅ ChromaDB — 统一主存储（38MB）
`~/.boshi/chroma_db/chroma.sqlite3`
- 1444 hot_topic 条目（已从 hot.json 迁移完毕）
- 热/温/冷靠 type + heat + tier 元数据标签区分

### 🟢 合理独立文件（保留）

| 文件 | 大小 | 用途 | 为何独立 |
|------|------|------|----------|
| `knowledge_graph.json` | 232KB | 实体节点+关系边邻接图 | 图数据结构，BFS多跳遍历，与向量检索范式不同 |
| `hot_topics.json` | 3KB | 桌面项目状态树 | Desktop 模块职责，非记忆范畴 |
| `live_turns.json` | 808B | 跨通道实时会话快照 | 实时缓存，不需要持久化 |
| `converge_mode.json` | 62B | 网关标志位 | 简单 flag，gateway_patch 需要 |

### 🗑️ 已清理僵尸文件

| 文件 | 大小 | 死因 |
|------|------|------|
| `warm.json` → .bak | 100KB | 5/21前旧WarmStore遗留，已无人写入 |
| `vectors.npy` → .bak | 1.9MB | boshi_memory.py 已无人引用 |
| `cross_platform_history.json` → .bak | 96KB | 旧版跨通道对话转存，已迁移 |
| `metadata.json` → .bak | 2.4KB | 5/19旧版用户画像，已由ChromaDB替代 |

### 🔒 已冻结 hot.json 系列

| 文件 | 大小 | 说明 |
|------|------|------|
| `hot.json.migrated` | 1.0MB | v4.0迁移产物 |
| `hot.json.recreated` | 1.0MB | 旧进程重建后被冻结 |
| `hot.json.bak` | 492KB | 5/24旧版备份 |
| `hot.json.pre_tagged.bak` | 792KB | Tag清洗前的备份 |

## 代码变更清单

| 文件 | 变更 |
|------|------|
| `chroma_bridge.py` | 新增 `get_memories()` / `delete_memories()` / `add_memories_batch()` |
| `tiered_memory.py` | `_save_hot()` → 批量写入ChromaDB + 安全守卫删除hot.json |
| `tiered_memory.py` | `_load_hot()` → 优先读ChromaDB，hot.json降级fallback |
| `tiered_memory.py` | `_archive_topic()` → 写入ChromaDB cold_topic类型 |
| `integrity_guard.py` | **新增** — 记忆完整性守护系统 |
| `safeguard_memory.sh` | **新增** — 升级安全脚本（快照/恢复/巡检） |
| `boshi-memory` skill | 更新完整性守护文档 |

## 剩余风险

1. `knowledge_graph.json` 独立于 ChromaDB 存在，但知识图谱有独立图结构和BFS遍历需求，保留是合理的设计决策
2. `hot_topics.json`（desktop模块）独立于记忆系统，互不干扰
3. 任何 Hermes 升级可能覆写 memory provider plugin，升级后需手动恢复
4. integrity_guard 目前做文件级监控，不做运行时拦截（可拦截但会增加延迟）
