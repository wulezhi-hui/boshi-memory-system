# 伯仕演进树 🌳

> 每个模块一个分支，每次改动挂上去，迭代关系清晰可见。

---

## 🧠 记忆系统

### v1 — JSON 文本存储（~2026-05-20 之前）
- **存储结构**：`hot.json`（热区话题）+ `warm.json`（温区记忆）+ `vectors.npy`（向量）
- **向量引擎**：Ollama BGE-M3 embedding（1024维）
- **搜索方式**：Numpy 余弦距离暴力计算
- **瓶颈**：Ollama 经常超时（BGE-M3 加载到 2070 8GB 显存失败），qwen3-embedding 太慢

### v2 — ChromaDB 离线迁移（2026-05-21）
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

### v3 — 统一热区存储（2026-05-26）
- 存储从三套（hot.json + Chroma + cold.json）统一为纯 Chroma
- 新增 conversation_turn 类型兜底写入；sync_turn 每轮对话即时写库

### v4 — 五大能力引入（2026-06-11，借鉴 Supermemory）
- 版本链（追加不覆盖 + isLatest）、用户画像（Static+Dynamic）、知识图谱（4 种关系）、混合搜索（语义+全文）、自动遗忘（热度衰减+时间折旧）
- 清理 ChromaDB 重建，索引修复

### v5 — 开放接口层（2026-06-12）
- 新增 MCP Server（boshi_mcp_server.py，8 工具）+ CLI（boshi_cli.py）+ 共享 Core 层（boshi_core.py）

### v6 — 零外网依赖向量层（2026-06-18）
- Embedding 从 SentenceTransformer（torch ~1.5GB）重构为 ONNX 推理，去掉 torch/sentence-transformers/HF 缓存依赖

### v6.1 — bge-m3 ONNX 升级（2026-08-16）
- 向量模型升级为 BAAI/bge-m3（Xenova ONNX，1024 维，中文检索质量优秀）；chroma 改 cosine 空间
- MCP 适配 2.0.0（MCPServer + @server.tool）
- 新增 session_sources.py 多 Agent 会话源路由
- 安全清理：移除泄露凭据，.gitignore 排除运行时数据

### v6.4 — 跨话题联想召回版（2026-09-05）← 当前
- **问题**：跨话题时记忆召回失效——"微调"搜不到"2080Ti推理框架配置"
- **根因**：查询词=当前消息无联想；知识图谱只匹配静态KNOWN_ENTITIES列表
- **方案**：`_expand_query_search()`从主查询结果提取实体名（正则：大写缩写、数字+字母、驼峰），扩展二次查询（最多3次）
- **依赖修复**：install.py新增`install_hermes_deps()`，首次安装时同步安装到Hermes venv
- **文件**：`plugins/boshi/__init__.py` 第307-375行

### v6.3 — prefetch缓存TTL修复版（2026-09-02）
- **问题**：Hermes v0.20.6升级后`_EXTERNAL_PREFETCH_TIMEOUT_S=8.0`，伯仕搜索常超8秒被静默跳过
- **方案**：给缓存加5分钟TTL，第2轮起毫秒级返回，避免超时
- **文件**：`plugins/boshi/__init__.py` 第289-305行
- **跨平台一键安装**：install.sh（Linux curl|bash）+ install.py（Windows）+ download_model.py（bge-m3 模型下载，hf-mirror 国内镜像，断点续传）
- **工程规范**：.gitattributes 统一 LF；已发布 GitHub tag v6.2 + Release
- **开源可用**：仓库 public，任何人可 clone 安装使用

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
