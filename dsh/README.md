# 伯仕记忆系统 — DSH 集成

DSH（Cordis 插件体系）通过**双轨**接入伯仕记忆：

| 轨 | 文件 | 作用 |
|:--|:--|:--|
| MCP 工具轨 | `../boshi_mcp_server.py` | 暴露 9 个工具（search/save/delete/status/profile/graph/graph_add/recent/time_range），模型可主动调用 |
| 自动记忆插件轨 | `boshi-auto-memory.mjs` | 每轮自动存用户消息、归档助手结论、自动召回注入 systemPrompt、画像注入、记忆使用规则注入 |
| 可选监控插件 | `boshi-monitor.mjs` | 记忆库状态监控（工具形态） |

> 自动记忆插件依赖 `../boshi_bridge.py`（输出单行 JSON 的 Python 桥接层，供 Node 侧 subprocess 解析）。

---

## ⚠️ dsh 侧部署现状（与本文件默认形态有差异，以实际部署为准）

dsh 桌面版（profile `desktop`）跑的是**本文插件的 dsh 侧 v3 分支**（2026-10-10 起），
相对上游 v2 有四处差异：

| # | 差异 | 原因 |
|:--|:--|:--|
| ① | `agent/session-start` → `agent/created` | DSH 0.2.0-rc.2 没有前者；不改则该监听器永不触发 |
| ② | spawn `env` 展开 `process.env` 并注入 `BOSHI_BACKEND` | 子进程需要 `USERPROFILE` 才能定位 `~/.boshi`；后端要显式指定 |
| ③ | 写入校验 + 失败落 spool + 本地日志 + 助手结论归档 + 过滤可配 | v3 新增，见下节 |
| ④ | 入口用带 `?v=N` 的 `file://` URL | 插件文件不在 `dsh-hmr` 监视范围，且 Node 的 ESM 缓存**按 URL 命中** —— 只改文件内容不会加载新代码。**实测：即便递增 N，实时重载也不会重新导入模块；插件改动一律需要重启 DSH。** |

部署位置：`~/.dsh/profiles/desktop/plugins/boshi-auto-memory.mjs`
注册位置：`~/.dsh/profiles/desktop/cordis.patch.yml` 末尾的 `insert` 块

> **该条目不要给 `config:` 块。** 插件没有导出 `Config` schema，给了会让 Cordis
> **不实例化 fiber**（`fiberPhase: null`、Config 目录里显示 `inactive`），表现为"装了但完全不工作"，且没有任何报错。
> 要改配置请改插件内的默认值（见下表）。

---

## v3 新增能力

### A. 写入不再"发射后不管"

v2 的写入调用**没有回调**，失败静默丢弃（无重试 / 无队列 / 无日志 / 无提示）。v3 校验 bridge 返回值，
失败时把 `{content, topic, ts}` 追加进 spool 文件（JSONL，默认上限 500 条），后端恢复后自动补投：

- 每轮最多补投 `flushPerTurn`（默认 3）条
- **只在"上一次调用成功"时才尝试**，避免对着挂掉的后端反复 spawn
- 保存成功的回调里也补投一次 —— 否则"失败后的下一轮"会因为健康标记还是 false 而永远不补

### B. 本地日志

`~/.boshi/logs/dsh-plugin.jsonl`，单行一条 JSON。记录：

| `op` | 内容 |
|:--|:--|
| `plugin-start` | 版本、profile、backend、各项配置、待补投条数 |
| `save` | topic、`ok`、字符数、失败原因 |
| `recall` | mode（search/time_range）、返回条数、注入长度 |
| `skip` | 原因（trivial / assistant-too-short）与字符数 |
| `spool-flush` / `spool-drop` / `spool-persist-fail` | 补投与异常 |

**写日志的任何异常都被吞掉，绝不影响记忆主流程** —— 代价是"日志自身失败"也会静默，排查时注意这一点。

### C. 助手半边（`assistant_conclusion`）

v2 只存用户消息，但注入的使用规则文本却让模型去找 `topic=assistant_conclusion` 的条目 ——
那类条目**全部来自 Hermes，dsh 一条不产**。v3 监听 `agent/assistant-stream`：

- 按 `attemptId` 累积 `text-delta`（**不含 `reasoning-delta`**，思考过程不入库）
- 只在 `end` 帧 `outcome.kind === 'committed'` 且 `eventType === 'assistant/message'` 时采纳；
  `abandoned` 与 `assistant/attempt`（重试中的半成品）都丢弃
- 同一回合多步时后用者覆盖 → 留最后一步的正文（即结论）
- 在 `agent/turn-stopping` 时写入，topic `assistant_conclusion`
- **只收顶层会话**（`assistantScope: 'root'`，用 `ctx.agents.roots()` 判定），不收录子代理的中间输出

### D. 过滤可配

| 配置 | 默认 | 说明 |
|:--|:--|:--|
| `minChars` | `2` | 用户消息短于此值则跳过。v2 硬编码 `<= 2`，把"继续"这类也丢了 |
| `skipSymbolOnly` | `true` | 纯标点/符号/空白消息是否跳过（Unicode 感知，不误伤中文） |

---

## 插件配置项（全部可选，省略即用默认值）

| 配置 | 默认 |
|:--|:--|
| `python` / `bridge` / `cwd` | 见文件内默认值 |
| `profile` / `backend` | `'dsh'` / `'pg'` |
| `minChars` / `skipSymbolOnly` | `2` / `true` |
| `captureAssistant` | `true` |
| `assistantMinChars` / `assistantMaxChars` | `10` / `2000` |
| `assistantScope` | `'root'`（可设 `'all'`） |
| `logFile` / `spoolFile` | `<cwd>/logs/dsh-plugin.jsonl` / `dsh-plugin-spool.jsonl` |
| `spoolMax` / `flushPerTurn` | `500` / `3` |

> 因该插件无 `Config` schema，这些值只能改插件内的默认值，**不能从 `cordis.patch.yml` 传**（传了会导致 fiber 不激活）。

---

## 桥接层命令

```bash
python boshi_bridge.py save "记忆内容" [topic]
python boshi_bridge.py search "查询" [top_k]
python boshi_bridge.py time_range <since> [--until=<ts>] [--top-k=<n>]
python boshi_bridge.py profile
python boshi_bridge.py status
```

## 安装（DSH 侧）

```yaml
- insert:
    - id: mcp-boshi
      name: '@deepseek-ai/dsh-mcp-client'
      config:
        serverName: boshi
        transport: stdio
        command: '<venv python 路径>'
        args: ['<~/.boshi/boshi_mcp_server.py>']
        cwd: '<~/.boshi>'
        env:
          PYTHONIOENCODING: 'utf-8'
          BOSHI_PROFILE: 'dsh'
          BOSHI_BACKEND: 'pg'
    - id: boshi-auto-memory
      name: 'file:///<profile 目录>/plugins/boshi-auto-memory.mjs?v=3'
      # 注意：不要加 config 块（插件无 Config schema，加了 fiber 不激活）
```

## 已知设计要点

1. **时间感知召回**：`detectTimeWindow()` 识别"刚才/上午/下午/晚上/今天/昨天/本周/本月"，
   命中则走 `time_range` 时间线查询，否则走 `search` 语义查询
2. **噪声隔离**：图谱自动提边（`type=relation` / `source=auto_extract`）在**数据层**
   （`chroma_bridge._apply_graph_exclusion`）默认排除，插件侧只做兜底过滤
3. **桥接参数约定（已更新）**：`time_range` 现在是**显式标志位** ——
   `time_range <since> [--until=<ts>] [--top-k=<n>] [--scope=self|all]`。
   旧文档写的"第 3 个位置参数按数值判断"是更早的形态，当前代码不再支持，传了会明确报 `unknown argument`

## 测试

`test-boshi-auto-memory.v3.mjs` —— 离线 mock 测试（假 `ctx` 驱动，**不碰 DSH、不碰真记忆库**），
28 项断言覆盖：用户写入、短消息过滤、`runtime-context` 排除、助手结论（reasoning 不泄漏 /
`abandoned` 与 `assistant/attempt` 不归档）、失败落 spool、恢复后补投、env 注入、卸载。

```bash
cd ~/.boshi/dsh && node test-boshi-auto-memory.v3.mjs
```

> 全绿即通过；有 ❌ 时退出码非 0。**改动插件后先跑这个再部署。**

`boshi-auto-memory.v3.mjs` 是部署副本（`~/.dsh/profiles/desktop/plugins/boshi-auto-memory.mjs`）
的可追溯来源，两者内容应保持一致。

## 运维注意（2026-10-10 实测）

1. **别在对话中途改 `cordis.patch.yml`。** 改它会触发 `dsh-hmr` 重新组合插件树，而
   `profileText` / `recallText` / **本回合累积的助手正文**都只在内存里 —— 重载即丢失。
   表现：那一轮**可能不产出 `assistant_conclusion`**，画像/召回段短暂为空。
   （spool 在磁盘上，不受影响。）要改配置就改完再开新的一轮。
2. **助手结论取"本回合最后一次非空正文"。** 纯工具调用的 assistant 消息没有 `text-delta`，
   不会覆盖已捕获的正文；但如果一轮以一句很短的收尾话结束，存下来的就是那句短话。
   若发现结论偏短，把规则从"最后一条"改成"最长一条"即可（改 `turnText.set` 那一处）。
3. **日志里出现 `save ok=false` 就去 spool 文件里找。** 补投是自动的，但要后端恢复后才会动；
   若 spool 持续增长，说明后端一直没起来，此时不要再反复触发写入。

## 变更记录

- **2026-10-10（dsh 侧 v3）**：写入校验 + 失败落 spool + 本地日志 + 助手结论归档 + 过滤可配；
  `agent/session-start` → `agent/created`；spawn env 展开 `process.env`；入口加 `?v=N`。
  详见 `../docs/伯仕工作日志.md` 同日条目
- 2026-09-13：新增 `time_range` 命令；Windows GBK stdout 修复（`sys.stdout.reconfigure(encoding="utf-8")`）
- 2026-09-13：插件 `isGraphNoise` 收敛为精确双条件（`--[` 且 `-->`）；白名单改黑名单 `isGraphFragment`；新增时间感知召回
