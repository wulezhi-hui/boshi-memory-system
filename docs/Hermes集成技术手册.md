# 伯仕记忆系统 · Hermes 集成技术手册

> 当 Hermes 升级后，阅读此文档 + 新版本 Hermes 文档，即可手动适配。

---

## 一、架构总览

```
Hermes (run_agent.py)
   │
   ├── memory_manager.py          ← Hermes 自带记忆管理器
   │     └── provider: boshi      ← 配置指定使用我们的插件
   │           └── plugins/boshi/__init__.py  ← 伯仕记忆系统入口
   │               （$HERMES_HOME/plugins/boshi/，实现 MemoryProvider ABC）
   │
   ├── mcp_servers.boshi          ← MCP 双轨（8 个 boshi_* 工具）
   │     └── boshi_mcp_server.py
   │
   ├── config.yaml                 ← 配置（时间区、模型、provider等）
   │
   └── cron jobs                   ← 通过 hermes cron 管理的计划任务
         ├── daily_crawl.py        ← 知识爬取
         ├── daily_session.py      ← 学习三省
         ├── knowledge_maintenance.py  ← 知识库维护
         └── weekly_inspection.py  ← 巡检
```

> **双轨说明（v6.2+）：** 插件方式（memory.provider=boshi）负责**自动**记忆
> 召回/存储；MCP 方式（mcp_servers.boshi）提供 8 个**手动**工具。两者并存，
> 共享 boshi_core.py 同一数据层。

---

## 二、关键集成点

### 2.1 Memory Provider 接口（最重要的集成点）

**文件位置：** `$HERMES_HOME/plugins/boshi/__init__.py`（仓库 `plugins/boshi/`）

**实现接口：** Hermes 新版 `MemoryProvider` 抽象类（`agent/memory_provider.py`），
**不需要 run_agent.py 补丁**（v6.2+ 的 Hermes 原生提供完整生命周期）。

**已实现的方法：**

| 方法 | 作用 | 调用时机 |
|:-----|:-----|:---------|
| `is_available()` | 检查 ~/.boshi 是否已安装 | Hermes 启动时 |
| `initialize(session_id, **kwargs)` | 导入 boshi_core（数据层） | Hermes 启动后 |
| `system_prompt_block()` | 注入记忆状态 + 热区话题 | 系统提示词组装时 |
| `prefetch(query)` | 返回召回的记忆 | 每轮 API 调用前 |
| `queue_prefetch(query)` | 后台线程检索（不阻塞对话） | 每轮对话后 |
| `sync_turn(user, asst)` | 自动存储用户消息 | 每轮对话后 |
| `on_memory_write(action,target,content)` | 镜像内置 memory 写入到伯仕 | agent 调用 memory 工具时 |
| `on_session_switch()` / `on_pre_compress()` | 会话边界处理 | 会话切换/压缩前 |
| `backup_paths()` | 声明 ~/.boshi 数据目录 | hermes backup 时 |
| `shutdown()` | 清理 | 进程退出 |

**破坏风险：** Hermes 升级可能改 `MemoryProvider` 的接口签名、新增必需方法。
**修复方法：** 阅读新版本 `agent/memory_provider.py` 定义，对照本表修改签名。

### 2.3 config.yaml 关键配置

**文件位置：** `AppData/Local/hermes/config.yaml`

**必须保留的配置项：**

```yaml
memory:
  provider: boshi            # 指定记忆提供者为伯仕
  memory_enabled: true        # 开启记忆
  user_profile_enabled: true  # 开启用户画像

agent:
  system_prompt: |            # 包含伯仕身份定义
    你是伯仕...

timezone: Asia/Shanghai       # 上海时区

cron:                         # cron配置
  wrap_response: true
```

**破坏风险：** 升级后 config.yaml 可能会被重置或新增必填项。
**修复方法：** 从备份 `~/.boshi/hermes-backup/config.yaml` 恢复，或手动补全。

---

## 三、外部依赖（Hermes 不需要管的）

| 组件 | 位置 | 说明 |
|:-----|:-----|:------|
| ChromaDB | `~/.boshi/chroma_db/` | 所有记忆数据，Hermes升级不影响 |
| 记忆配置文件 | `~/.boshi/memory/` | tiered_memory.py 等引擎文件 |
| 学习笔记 | `~/.boshi/memory/daily-sessions/` | 三省笔记 |
| 爬取脚本 | `~/.hermes/scripts/` | cron 脚本 |
| Camofox浏览器 | `~/.boshi/camofox/` | 搬运模式 |
| Obsidian知识库 | `D:\ObsidianVault\虚拟寺院知识库\` | 文章、笔记 |
| 代理工具 | 桌面 0dcloud | 翻墙 |

这些组件**Hermes 升级完全不影响**。

---

## 四、升级后兼容性检查清单

升级后按以下顺序检查：

### 步骤 1：检查 memory provider 接口
```python
# 在新版本中找到 BaseMemoryProvider，对比
grep -n "class BaseMemoryProvider\|def __init__\|def add_memory\|def search_memory" run_agent.py
# 如果接口变了，修改 boshi/__init__.py 对应方法签名
```

### 步骤 2：补丁位置验证
```python
# 找 _MemoryManager 初始化位置
grep -n "class _MemoryManager\|queue_prefetch_all\|sync_turn" run_agent.py
# 如果位置变了，重新打补丁
```

### 步骤 3：配置生效检查
```bash
grep "provider: boshi" config.yaml   # 确认 provider 配置还在
hermes cron list                      # 确认 cron 任务还在
```

### 步骤 4：功能验证
```bash
# 手动测试记忆功能
# 发一条消息 → 确认 sync_turn 写入 Chroma
# 发相关第二条 → 确认 prefetch 能搜到第一条
```

---

## 五、常见故障与修复

| 症状 | 原因 | 修复 |
|:-----|:-----|:------|
| 启动报错 "No provider: boshi" | memory.provider 配置丢失 | 恢复 config.yaml |
| 启动报错 "Method XYZ not implemented" | provider 接口变了 | 对照新接口补方法 |
| 记忆不写入/不读取 | sync_turn/prefetch 补丁失效 | 重新打补丁 |
| 三省脚本不执行 | cron 任务在升级中被删 | 重新创建 cron |
| agent 报错 "memory_manager is None" | run_agent 初始化逻辑变了 | 重新定位补丁位置 |

---

## 六、升级流程

```
1. 备份当前状态:
   python ~/.boshi/scripts/restore_after_upgrade.py backup

2. 升级 Hermes:
   正常执行 hermes 升级命令

3. 自动检测:
   三省脚本下次运行时自动 check → 发现异常 → 告诉你

4. 手动修复（如果需要）:
   对照本文档的"升级后兼容性检查清单"
   逐步检查并修复

5. 验证:
   发几条消息测试记忆功能是否正常
```

---

*文档版本: v2.0 · 2026-08-17*
*由伯仕编写，供升级后的伯仕参考*

---

## 七、本地补丁记录（hermes-agent 源码，升级会被覆盖！）

### 7.1 mcp_tool.py — mcp SDK 2.0 CallToolResult 字段兼容

- **位置：** `hermes-agent/tools/mcp_tool.py`（约 L5442）
- **问题：** mcp SDK ≥2.0 把 CallToolResult 的 `isError` 字段改名为 `is_error`，
  Hermes 读 `result.isError` 报 `AttributeError: 'CallToolResult' object has no attribute 'isError'`，
  导致 boshi 等所有 mcp 2.0 服务器的工具调用失败。
- **补丁：**
  ```python
  if getattr(result, "isError", getattr(result, "is_error", False)):
  ```
- **升级后检查：** `grep -n "isError" tools/mcp_tool.py`，若仍是 `if result.isError:` 需重打。
- **判断是否生效：** 调用任一 boshi 工具（如 boshi_status），不再报 isError 错误即生效。
