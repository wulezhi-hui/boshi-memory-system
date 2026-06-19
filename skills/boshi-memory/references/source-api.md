核心文件：`~/.hermes/memory/boshi_memory.py`
类：`MemoryStore`
CLI 入口：`main()` → 子命令 add/search/get-all/get/update/delete/delete-all/stats

## 关键方法
- `add(content, user_id, source, run_id)` → 向量化 + 存储
- `search(query, user_id, top_k, threshold)` → 语义搜索
- `get_all(user_id)` → 全量获取
- `get(memory_id)` → 单条
- `update(memory_id, new_content)` → 更新（重新向量化）
- `delete(memory_id)` → 删除

## 内部
- `_get_embedding(text)` → POST 到 Ollama /api/embeddings
- `_cosine_distance(a, b)` → numpy 余弦距离
- `_ensure_loaded()` → 懒加载 vectors.npy + metadata.json
- `_save()` → 持久化到磁盘
- 全局单例 `get_store()` → 避免重复加载

## 不存在的功能
- ❌ 没有 LLM 提取（已移除）
- ❌ 没有 ChromaDB 依赖
- ❌ 没有 HTTP 服务模式（如需可加）
