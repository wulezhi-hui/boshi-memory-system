# 伯仕演进树 🌳

> 每个模块一个分支，每次改动挂上去，迭代关系清晰可见。

---

## 🧠 记忆系统

### v1 — JSON 文本存储（~2026-05-20 之前）
- **存储结构**：`hot.json`（热区话题）+ `warm.json`（温区记忆）+ `vectors.npy`（向量）
- **向量引擎**：Ollama BGE-M3 embedding（1024维）
- **搜索方式**：Numpy 余弦距离暴力计算
- **瓶颈**：Ollama 经常超时（BGE-M3 加载到 2070 8GB 显存失败），qwen3-embedding 太慢

### v2 — ChromaDB 离线迁移（2026-05-21）← 当前
- **动机**：Ollama embedding 不可靠，记忆系统不应依赖外部服务
- **存储**：`~/.boshi/chroma_db/` — ChromaDB 持久化向量库
- **向量模型**：`sentence-transformers/all-MiniLM-L6-v2`（384维），离线 cache，CPU 秒出
- **查询速度**：~0.03s / 次
- **迁移数据**：旧 warm.json 230条 → 筛选 145 条有价值事实写入 ChromaDB
- **改动文件**：
  - 新建 `~/.boshi/chroma_bridge.py` — ChromaDB 封装（add / search / recent / count）
  - 改 `memory/tiered_memory.py` — `_get_embedding` / `add_warm` / `search_warm` / `_load_warm` / `_save_warm` 替换
  - 改 `cognitive_state.py` — 从读 warm.json 改为 chroma_bridge.count()
  - 改 `memory_provider/__init__.py` — is_available 改为检查 chroma_bridge.py
- **废弃**：Ollama embedding 调用、warm.json 写入、vectors.npy 读写
- **保留**：热度引擎（TieredMemory 的话题热度衰减）、冷区挖掘（state.db）

---

## 🔧 工作台桥接（Workstation Bridge）

### v1 — 轮询推送（2026-05-20）
- **机制**：`bridge_weixin.py` 每 1.5s 轮询 converge.db，新数据推送到工作台 `/push_history`
- **存储**：`~/.openclaw/shared/converge.db` — 会话表（channel, role, content, timestamp）
- **会话同步**：`auto_sync_session.py` cronjob 每 1 分钟跑一次

### 当前状态
- 桥接运行中（cron: auto-sync ✅, bridge-watchdog ✅）
- 三终端统一写入 converge.db

---

## 🗄️ 轻量级向量数据库

### ChromaDB + 本地模型方案（2026-05-21 上线）
- **数据库**：ChromaDB 1.5.9（持久化模式）
- **配置**：`~/.boshi/chroma_db/`
- **Embedding**：sentence-transformers 本地 cache，不用内置 ONNX（需要外网下载）
- **维度**：384
- **模型大小**：~90MB，单个文件
- **特点**：零外网依赖、零 VRAM、CPU 秒出

### 未来考虑
- 古籍/佛经知识库量大（十几万册）时需升级到 Milvus/Qdrant
- 当前硬件（双 2080Ti 22G 到货前）轻量方案最优

---

## 📋 待办项目

- [ ] ComfyUI + SD 图像生成（2080Ti 到货后）
- [ ] 独立向量知识库（古籍/佛经检索）
- [ ] UE5 虚拟寺院 学习（02:00 空闲时段）
- [ ] 结构图可视化
- [ ] **🧬 伯仕分身计划** ← 2026-05-21 灵感
  - **背景**：乐之希望从我（主身）分化出独立分身，部署到其他电脑
  - **核心设计**：
    - 分身是独立人格（如总经理助理），非数据同步
    - 主身能遥感到分身状态——工作摘要、进化进度、需要改进点
    - 主身发现的问题可完善后更新分身模板
  - **技术要点**：
    - 分身模板打包（skill库、记忆架构、桥接配置）
    - 心跳 + 状态报告协议（主身主动问询，分身自主汇报）
  - **预期用途**：主身进化出的通用经验可复制给分身，分身在其领域的新发现也可反馈给主身
  - **IPv6 连通性测试**：2026-05-21 ✅ 通过
    - 本机公网 IPv6 地址 `240e:381:9fee:da01:2449:f32f:9c14:3979` 可从外部访问
    - 点对点直连方案可行，无需frp/穿透
  - **Phase 1 完成**：2026-05-21 ✅
    - 灵魂包：`boshi-dist.tar.gz`（81MB，含核心代码+模型）
    - 部署脚本：`setup_boshi.py`（自动安装依赖+复制文件）
    - 部署说明：`README.md`
