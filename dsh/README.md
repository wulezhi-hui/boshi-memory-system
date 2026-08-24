# 伯仕记忆系统 — DSH 自动记忆插件

让伯仕记忆系统在 [DeepSeek Harness](https://github.com/deepseek-ai/deepseek-harness)（DSH）上实现**自动记忆**，对标 Hermes 插件模式（MemoryProvider）的三件套，无需 agent 记得主动调用工具。

## 功能

| 功能 | 说明 |
|------|------|
| 存储对话 | 每轮把用户消息异步存入伯仕（过滤纯标点/≤2字符的零散输入） |
| 画像注入 | 把用户画像/热区/近期记忆注入系统提示词 |
| 召回注入 | 每轮异步检索相关历史记忆，注入下一轮系统提示词 |

## 依赖

- 伯仕已安装（`~/.boshi/` 目录含 `boshi_core.py`、`boshi_bridge.py`、`chroma_db/`）
- DSH 的 profile 已启用 `subprocess` 与 `systemPrompt` 服务（默认 web 组合已含）

## 部署（DSH 用户）

1. 复制本目录的 `boshi-auto-memory.mjs` 到你的 profile 目录下，例如：

   ```
   $DSH_HOME/profiles/web/plugins/boshi-auto-memory.mjs
   ```

2. 编辑 `$DSH_HOME/profiles/web/cordis.patch.yml`，追加：

   ```yaml
   - insert:
       - id: boshi-auto-memory
         name: './plugins/boshi-auto-memory.mjs'
         config:
           python: '<你的 venv python 路径>'
           bridge: '<你的 ~/.boshi/boshi_bridge.py 路径>'
           cwd: '<你的 ~/.boshi 路径>'
   ```

3. 重启 DSH（`dsh web`）即生效。

## 配置项

| 字段 | 默认值 | 说明 |
|------|--------|------|
| `python` | `C:/Users/wulezhi/.boshi/venv/Scripts/python.exe` | 伯仕 venv 的 python |
| `bridge` | `C:/Users/wulezhi/.boshi/boshi_bridge.py` | 桥接层脚本 |
| `cwd` | `C:/Users/wulezhi/.boshi` | 工作目录 |

## 工作原理

插件监听 DSH 的 `agent/inbox/claimed` 事件（用户消息进入处理时触发），通过 `subprocess` 异步调用 `boshi_bridge.py`（输出 JSON），把结果写入内存缓存；再用 `systemPrompt.section()` 注册动态 section，每次组装系统提示词时同步读缓存注入。所有伯仕调用均为异步 fire-and-forget，不阻塞对话。

## 与 MCP 方式的关系

- **MCP 方式**（`boshi_mcp_server.py`）：agent 主动调用 8 个工具（search/save/...），适合按需检索
- **本插件**（自动记忆）：被动自动，每轮存储 + 注入，无需 agent 记得调用

两者可同时启用，互补。
