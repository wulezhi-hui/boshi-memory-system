# 伯仕工作台 v5 — Gateway SSE 直连架构

## 最终架构（2026-05-19 确定）

```
你在工作台右栏输入
  │
  ▼
WebSocket → 伯仕工作台 (aiohttp :7681)
  │
  ▼ POST /v1/chat/completions (stream: True)
Hermes Gateway (:8642)
  │
  ▼ SSE stream
  伯仕回复流回工作台
  │
  ├── thinking → 左栏（灰色斜体，思考过程）
  ├── tool_calls → 左栏（黄色，工具调用）
  └── content → 右栏（流式输出）
```

## 历史弯路（以此为戒）

| 版本 | 方案 | 问题 |
|------|------|------|
| v3 | inbox/outbox 文件通道 | 非实时、需要轮询 |
| v4-v1 | Gateway API 创建独立会话 | 两个伯仕，上下文不共享 |
| v4-v2 | 文件通道纯展示 | 右栏输入无人响应 |
| **v5** | **Gateway SSE 直连** | **✅ 实时、同一伯仕、过程可见** |

**教训：** 一开始就该走 Gateway SSE。乐之说"转一个大湾，白干那么多事"。

## Windows aiohttp 启动陷阱

`web.run_app()` 在 Windows 后台模式下会打印"Server listening on..."但端口永不绑定。

**不要用：**
```python
web.run_app(app, host=HOST, port=PORT)
```

**必须用：**
```python
async def run_app():
    app = web.Application()
    app.router.add_get("/", index)
    app.router.add_get("/ws", websocket_handler)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, host=HOST, port=PORT)
    await site.start()
    print(f"Listening on http://localhost:{PORT}")
    await asyncio.Event().wait()  # 保持运行

def main():
    asyncio.run(run_app())
```

## ChatSession 设计

每个 WebSocket 连接对应一个独立的 ChatSession：
- 维护 messages 数组（含 system prompt）
- 用户输入追加到 messages
- SSE 流结束后追加 assistant reply
- 页面刷新重新开始

## 与微信的关系

- 微信和工作台走同一个 Gateway（同一个 Hermes Agent 实例）
- 微信：只看到最终回复（无过程展示）
- 工作台：看到完整过程（thinking + tool_calls + content）
- **同一个伯仕，共享上下文**

## 入口偏好（乐之实际使用方式）

乐之的主力入口是**工作台（7681）**，原因是：
- 网页可以自由复制粘贴大段文本（终端/微信做不到）
- 能看到工作全过程（thinking、tool_calls、content 流式展示）
- 微信只能看到最终回答，看不到工作流

**TUI 命令行入口已弃用**——安装向导卡在 14%（Windows 不支持安装脚本），没有跳跃选项。不用再尝试修复，直接用工作台。

## 长期目标：伯仕独立智能体

乐之的深层次需求不是"修好某个入口"，而是让伯仕成为真正的独立智能体：
1. **统一记忆** — 不管在哪个入口聊天，记忆互通（当前内存条里的 MEMORY 段 vs boshi_memory.py 是两套独立系统）
2. **迁移方案** — 配置路径 `~/.hermes/memory/` 可整体迁移
3. **最终目标** — 与 Hermes 框架的分裂设计解耦

## 启动命令

```bash
# 1. 确保 Gateway 在跑
hermes gateway run

# 2. 启动工作台（后台）
cd ~/.hermes/workstation
python -u "伯仕工作台.pyw" > run.log 2>&1 &

# 3. 打开浏览器
# http://localhost:7681

# 4. 关闭
kill PID  # 从 netstat -ano | grep 7681 获取
```
