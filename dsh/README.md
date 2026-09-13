# 伯仕记忆系统 — DSH 集成

DSH（Cordis 插件体系）通过**双轨**接入伯仕记忆：

| 轨 | 文件 | 作用 |
|:--|:--|:--|
| MCP 工具轨 | `../boshi_mcp_server.py` | 暴露 9 个工具（search/save/delete/status/profile/graph/graph_add/recent/time_range），模型可主动调用 |
| 自动记忆插件轨 | `boshi-auto-memory.mjs` | 每轮自动存用户消息 + 自动召回注入 systemPrompt + 画像注入 + 记忆使用规则注入 |
| 可选监控插件 | `boshi-monitor.mjs` | 记忆库状态监控（工具形态） |

> 自动记忆插件依赖 `../boshi_bridge.py`（输出单行 JSON 的 Python 桥接层，供 Node 侧 subprocess 解析）。

## 桥接层命令

```bash
python boshi_bridge.py save "记忆内容" [topic]
python boshi_bridge.py search "查询" [top_k]
python boshi_bridge.py time_range <since> [until] [top_k]
python boshi_bridge.py profile
python boshi_bridge.py status
```

## 安装（DSH 侧）

把两个插件文件放到 DSH profile 的 `plugins/` 目录，然后在 `cordis.patch.yml` 里注册：

```yaml
- insert:
    - id: boshi-mcp
      name: <mcp 插件 id>
      config:
        serverName: boshi
        transport: stdio
        command: '<venv python 路径>'
        args: ['<~/.boshi/boshi_mcp_server.py>']
        cwd: '<~/.boshi>'
        env:
          PYTHONIOENCODING: 'utf-8'
    - id: boshi-auto-memory
      name: './plugins/boshi-auto-memory.mjs'
      config:
        python: '<venv python 路径>'
        bridge: '<~/.boshi/boshi_bridge.py>'
        cwd: '<~/.boshi>'
```

## 已知设计要点

1. **时间感知召回**：`detectTimeWindow()` 识别"刚才/上午/下午/晚上/今天/昨天/本周/本月"，命中则走 `time_range` 时间线查询，否则走 `search` 语义查询
2. **噪声隔离**：图谱自动提边（`type=relation` / `source=auto_extract`）在**数据层**（`chroma_bridge._apply_graph_exclusion`）默认排除，插件侧只做兜底过滤
3. **桥接参数约定**：`time_range` 的第 3 个位置参数按数值判断（<1000 视为 `top_k`，否则视为 `until`），调用方须遵守

## 变更记录

- 2026-09-13：新增 `time_range` 命令；Windows GBK stdout 修复（`sys.stdout.reconfigure(encoding="utf-8")`）
- 2026-09-13：插件 `isGraphNoise` 收敛为精确双条件（`--[` 且 `-->`）；白名单改黑名单 `isGraphFragment`；新增时间感知召回
