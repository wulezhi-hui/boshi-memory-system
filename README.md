# boshi-memory-system（伯仕记忆系统）

> **当前版本：v6.2「双轨接入版」**（2026-08-17 · [GitHub Release](https://github.com/wulezhi-hui/boshi-memory-system/releases/tag/v6.2)）
> 版本演进：v1 JSON → v2 ChromaDB → v3 统一存储 → v4 五大能力 → v5 开放接口 → v6 ONNX 零依赖 → v6.1 bge-m3 → **v6.2 插件+MCP 双轨**

Hermes Agent 的四层记忆架构：持久化、热度调度、知识图谱、跨通道对话桥接。
**双轨接入 Hermes：插件方式（memory provider）+ MCP 方式（8 个工具）**，可同时启用。

## 功能概览

| 能力 | 说明 |
|:-----|:-----|
| 🔥 热区 | 当前专注的事，自动注入上下文 |
| 🌡️ 温区 | 语义搜索快速召回（ChromaDB + bge-m3 ONNX，零外部 API） |
| ❄️ 冷区 | 时间久远压缩封存 |
| 🕉️ 全量 | state.db SQLite FTS5 原始会话 |
| 版本链 | 追加不覆盖 + isLatest 标记，记忆可追溯 |
| 知识图谱 | 4 种关系 + 版本追踪 + 实体自动关联 |
| 混合搜索 | 语义向量 + FTS5 全文会话合一 |
| 用户画像 | Static+Dynamic 双层自动维护 |
| 自动遗忘 | 热度衰减 + 时间过期折旧 |
| 🔌 插件接入 | Hermes MemoryProvider ABC：每轮自动召回/存储/画像注入 |
| 🔗 MCP 接入 | 8 个工具：search/save/delete/status/profile/graph/graph_add/recent |
| 🧩 DSH 接入 | 自动记忆插件：每轮存储 + 画像/召回注入（见 `dsh/`） |

## 快速开始

### 1. 安装

```bash
# Ubuntu / Linux / macOS（自动下载 bge-m3 ONNX 模型，默认 hf-mirror 国内镜像）
curl -sL https://raw.githubusercontent.com/wulezhi-hui/boshi-memory-system/main/install.sh | bash

# Windows（克隆后执行安装脚本；--no-model 可跳过 569MB 模型下载）
git clone https://github.com/wulezhi-hui/boshi-memory-system.git %USERPROFILE%\.boshi
python %USERPROFILE%\.boshi\install.py
```

安装脚本会完成：部署代码 → 安装依赖（chromadb/mcp/onnxruntime/transformers）→
下载 bge-m3 ONNX 模型 → 复制插件 → 写双轨配置 → 安装 skill。

> 模型下载源切换：`BOSHI_MODEL_SOURCE=hf python download_model.py`（官方源，需代理）。
> 国内默认 hf-mirror.com 直连，无需代理。

### 2. 双轨接入方式（安装脚本自动配置，也可手动）

**插件方式（每轮自动召回，无需 agent 记得调用工具）** — config.yaml:

```yaml
memory:
  provider: boshi
  memory_enabled: true
  user_profile_enabled: true
```

插件文件位于 `$HERMES_HOME/plugins/boshi/`（实现 Hermes `MemoryProvider` ABC：
prefetch 每轮召回、sync_turn 每轮存储、system_prompt_block 画像/热区注入）。

**MCP 方式（agent 主动调用 8 个 boshi_* 工具）** — config.yaml:

```yaml
mcp_servers:
  boshi:
    enabled: true
    command: "<python 解释器>"
    args: ["~/.boshi/boshi_mcp_server.py"]
```

重启 Hermes 后生效：
- 插件方式：`hermes memory status` 应显示 `Provider: boshi`
- MCP 方式：`hermes mcp test boshi` 应连接成功，出现 `boshi_search` / `boshi_save` / `boshi_delete` / `boshi_status` / `boshi_profile` / `boshi_graph` / `boshi_graph_add` / `boshi_recent`

### 3. CLI 使用

```bash
python boshi_cli.py search "查询文本"
python boshi_cli.py save "记忆内容" --topic 项目
python boshi_cli.py status
python boshi_cli.py profile
```

### 4. DSH 接入（自动记忆）

DSH（DeepSeek Harness）用户可通过 `dsh/` 目录下的自动记忆插件实现每轮自动存储 + 画像/召回注入，无需 agent 记得调用工具。详见 [dsh/README.md](dsh/README.md)。

```bash
# 1. 复制插件到 DSH profile
cp ~/.boshi/dsh/boshi-auto-memory.mjs $DSH_HOME/profiles/web/plugins/

# 2. 在 $DSH_HOME/profiles/web/cordis.patch.yml 追加：
#    - insert:
#        - id: boshi-auto-memory
#          name: './plugins/boshi-auto-memory.mjs'
#          config: { python, bridge, cwd }  # 见 dsh/README.md

# 3. 重启 dsh web
```

## 技术文档

- [v6.2 技术架构文档（双轨接入版，当前）](docs/伯仕记忆系统v6.2_技术架构文档.md)
- [v6.1 架构文档](docs/伯仕记忆系统v6.1_技术架构文档.md)
- [v6.1 技术文档](docs/伯仕记忆系统v6.1_技术文档.md)
- [Hermes 集成技术手册](docs/Hermes集成技术手册.md)
- [演进树 EVOLUTION](EVOLUTION.md)
- [技能文件](skills/boshi-memory/SKILL.md)

## 已知注意点

- 路径已统一为 `~/.boshi`（不再硬编码 Administrator）
- 向量模型：bge-m3 ONNX（1024 维，int8 量化 ~569MB），缺失时用
  `python download_model.py` 下载（断点续传）
- 实体提取：规则/启发式（`auto_link_entities`，纯本地零外部依赖），`save()` 时自动提取实体并建边
- 检索优先 FTS5，失败自动降级 LIKE
