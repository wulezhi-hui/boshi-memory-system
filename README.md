# boshi-memory-system

Hermes Agent 的四层记忆架构 ，实现持久化、热度调度、知识图谱、跨通道对话桥接。

## 功能概览

| 能力 | 说明 |
|:-----|:-----|
| 🔥 热区 | 当前专注的事，自动注入上下文 |
| 🌡️ 温区 | 语义搜索快速召回（ChromaDB + ONNX embedding，零外部依赖） |
| ❄️ 冷区 | 时间久远压缩封存 |
| 🕉️ 全量 | state.db SQLite FTS5 原始会话 |
| 版本链 | 追加不覆盖 + isLatest 标记，记忆可追溯 |
| 知识图谱 | 4 种关系 + 版本追踪 + 实体自动关联 |
| 混合搜索 | 语义向量 + FTS5 全文会话合一 |
| 用户画像 | Static+Dynamic 双层自动维护 |
| 自动遗忘 | 热度衰减 + 时间过期折旧 |
| 开放接口 | Hermes Memory Provider 插件 + MCP Server（8 工具）+ CLI（9 子命令） |

## 快速开始

### 1. 安装

```bash
# 一键安装（跨平台，自动部署 + 装依赖 + 配置 Hermes 双轨接入）
python install.py

# Ubuntu / Linux 传统方式
curl -sL https://raw.githubusercontent.com/wulezhi-hui/boshi-memory-system/main/install.sh | bash

# Windows：手动 clone 后安装依赖
git clone https://github.com/wulezhi-hui/boshi-memory-system.git ~/.boshi
pip install chromadb "mcp>=2.0.0" onnxruntime transformers
```

### 2. 接入 Hermes（双轨：插件 + MCP）

伯仕同时提供**两种**接入方式，安装脚本会全部配置好：

| 方式 | 机制 | 能力 |
|:-----|:-----|:-----|
| **插件方式**（推荐） | `memory.provider = boshi`，Hermes MemoryProvider 插件 | 每轮对话**自动召回**相关记忆（prefetch）、**自动存储**（sync_turn）、画像/热区注入系统提示词；用内置 memory 工具写入自动镜像到伯仕 |
| **MCP 方式** | `mcp_servers.boshi` → `boshi_mcp_server.py` | 8 个工具：`boshi_search` / `boshi_save` / `boshi_delete` / `boshi_status` / `boshi_profile` / `boshi_graph` / `boshi_graph_add` / `boshi_recent` |

插件源码在 `plugins/boshi/__init__.py`（实现 Hermes 的 `MemoryProvider` ABC，
数据层与 MCP/CLI 共享 `boshi_core.py`，不重复实现）。

验证安装：

```bash
hermes memory status          # 应显示 Provider: boshi / Plugin: installed
hermes mcp test boshi         # 应显示连接成功
```

### 3. CLI 使用

```bash
python boshi_cli.py search "查询文本"
python boshi_cli.py save "记忆内容" --topic 项目
python boshi_cli.py status
python boshi_cli.py profile
```

## 技术文档

- [v6.1 架构文档](docs/伯仕记忆系统v6.1_技术架构文档.md)
- [v6.1 技术文档](docs/伯仕记忆系统v6.1_技术文档.md)
- [技能文件](skills/boshi-memory/SKILL.md)

## 已知注意点

- 路径已统一为 `~/.boshi`（不再硬编码 Administrator）
- 实体提取默认走 Ollama LLM，对话中请用 `extract_facts_async`（后台线程）避免阻塞
- 检索优先 FTS5，失败自动降级 LIKE
