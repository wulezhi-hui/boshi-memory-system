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
| 开放接口 | MCP Server（8 工具）+ CLI（9 子命令） |

## 快速开始

### 1. 安装

```bash
# Ubuntu / Linux
curl -sL https://raw.githubusercontent.com/wulezhi-hui/boshi-memory-system/main/install.sh | bash

# Windows：clone 后安装依赖
git clone https://github.com/wulezhi-hui/boshi-memory-system.git ~/.boshi
pip install chromadb
```

### 2. 接入 Hermes（MCP）

在 `config.yaml` 添加：

```yaml
mcp_servers:
  boshi:
    command: "python"
    args: ["~/.boshi/boshi_mcp_server.py"]
```

重启 Hermes 后自动注册 8 个工具：`boshi_search` / `boshi_save` / `boshi_delete` / `boshi_status` / `boshi_profile` / `boshi_graph` / `boshi_graph_add` / `boshi_recent`。

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
