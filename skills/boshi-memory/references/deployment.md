# 伯仕记忆系统部署参考

## 文件结构
```
~/.hermes/memory/
├── boshi_memory.py    ← 核心模块（~12KB）
├── vectors.npy        ← 向量数据（numpy 二进制）
├── metadata.json      ← 记忆文本+时间戳
└── config.json        ← 配置（可选覆盖）
```

## 依赖
- Python: numpy（Hermes venv 已有）
- Ollama: bge-m3:latest（乐之本机已下载）
- 网络：localhost:11434（Ollama 默认端口）

## 实测性能
| 操作 | 耗时 | 说明 |
|------|------|------|
| search（3条记忆） | ~3秒 | bge-m3 向量化 + numpy 距离计算 |
| add（存1条） | ~2-3秒 | 只做向量化，不调 LLM |
| get-all | <0.1秒 | 纯内存读取 |

## 向量模型选择历程
1. 原 mem0 用 `nomic-embed-text`（137M）→ 乐之没有
2. 考虑 `qwen3-embedding:4b`（2.5G）→ 太大
3. 最终选 **`bge-m3:latest`（1.2G）** → 多语言旗舰，中文强，大小适中

## 余弦距离阈值说明
- 默认 0.6（即相似度 >0.4 才返回）
- bge-m3 的余弦距离分布：相关记忆约 0.35-0.55，不相关约 0.7+
- 如果搜索不到想要的，调高阈值：`--top-k 10` 或改 config

## 迁移步骤
```bash
# 打包
tar czf hermes-memory-backup.tar.gz ~/.hermes/memory/

# 新设备解压到相同路径
mkdir -p ~/.hermes/memory/
tar xzf hermes-memory-backup.tar.gz -C ~/.hermes/memory/
# 确保 Ollama 有 bge-m3
ollama pull bge-m3:latest
```
