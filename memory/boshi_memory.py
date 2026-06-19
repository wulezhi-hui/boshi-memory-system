#!/usr/bin/env python3
"""
伯仕记忆系统 (BoShi Memory) — 轻量级语义记忆模块
================================================
基于 Ollama embedding + numpy 的纯本地向量记忆存储。
存到 ~/.hermes/memory/，随配置迁移。
"""

import json
import os
import sys
import uuid
import numpy as np
from datetime import datetime, timezone

# ========== 配置 ==========

MEMORY_DIR = os.path.dirname(os.path.abspath(__file__))
VECTORS_PATH = os.path.join(MEMORY_DIR, "vectors.npy")
METADATA_PATH = os.path.join(MEMORY_DIR, "metadata.json")
CONFIG_PATH = os.path.join(MEMORY_DIR, "config.json")

DEFAULT_CONFIG = {
    "embed_model": "qwen3-embedding:4b-q4_K_M",
    "llm_model": "qwen3.5-4b-buddhist:latest",
    "ollama_url": "http://localhost:11434",
    "top_k": 5,
    "similarity_threshold": 0.6,  # 余弦距离阈值（越小越相似）
}

# 记忆提取提示词
EXTRACT_PROMPT = """从以下对话中提取重要的事实性记忆，以 JSON 数组格式输出。

规则：
1. 只提取明确的事实性陈述
2. 忽略客套话、寒暄、对话开场白
3. 优先提取：用户偏好、经历、重要事实、作出的决定
4. 每条记忆必须是完整的中文陈述句
5. 如果没有值得记忆的内容，返回空数组 []

对话内容：{input}

输出格式（只输出 JSON，不要其他内容）：
[{{"fact": "记忆内容1"}}, {{"fact": "记忆内容2"}}]
"""


# ========== 存储引擎 ==========

class MemoryStore:
    """基于 numpy 的轻量向量记忆存储"""

    def __init__(self, config: dict = None):
        self.config = {**DEFAULT_CONFIG, **(config or {})}
        self._vectors: np.ndarray | None = None
        self._metadata: list = []
        self._loaded = False

    def _ensure_loaded(self):
        """懒加载：首次使用时才加载数据"""
        if self._loaded:
            return
        self._metadata = []
        if os.path.exists(METADATA_PATH):
            try:
                with open(METADATA_PATH, encoding='utf-8') as f:
                    self._metadata = json.load(f)
            except (OSError, json.JSONDecodeError):
                self._metadata = []
        if os.path.exists(VECTORS_PATH):
            try:
                self._vectors = np.load(VECTORS_PATH)
                # 确保向量数量与元数据一致
                if len(self._vectors) != len(self._metadata):
                    print(f"⚠️  向量数({len(self._vectors)})与元数据数({len(self._metadata)})不匹配，重建索引", file=sys.stderr)
                    self._vectors = None
                    self._metadata = []
            except Exception:
                self._vectors = None
        self._loaded = True

    def _save(self):
        """持久化到磁盘"""
        os.makedirs(MEMORY_DIR, exist_ok=True)
        if self._vectors is not None and len(self._vectors) > 0:
            np.save(VECTORS_PATH, self._vectors)
        elif os.path.exists(VECTORS_PATH):
            os.remove(VECTORS_PATH)
        with open(METADATA_PATH, 'w', encoding='utf-8') as f:
            json.dump(self._metadata, f, ensure_ascii=False, indent=2)

    def _get_embedding(self, text: str) -> list:
        """调用 Ollama 获取向量"""
        import requests
        resp = requests.post(
            f"{self.config['ollama_url']}/api/embeddings",
            json={"model": self.config['embed_model'], "prompt": text},
            timeout=30,
        )
        resp.raise_for_status()
        return resp.json()["embedding"]

    def _cosine_distance(self, a: np.ndarray, b: np.ndarray) -> float:
        """余弦距离（0=完全相同，1=完全不相关）"""
        norm_a = np.linalg.norm(a)
        norm_b = np.linalg.norm(b)
        if norm_a == 0 or norm_b == 0:
            return 1.0
        return 1.0 - float(np.dot(a, b) / (norm_a * norm_b))

    # ========== 公开 API ==========

    def add(self, content: str, user_id: str = "main_user",
            source: str = "", run_id: str = "") -> dict:
        """添加一条记忆"""
        self._ensure_loaded()
        mem_id = str(uuid.uuid4())[:8]
        now = datetime.now(timezone.utc).isoformat()
        entry = {
            "id": mem_id,
            "content": content,
            "user_id": user_id,
            "source": source,
            "run_id": run_id,
            "created_at": now,
            "updated_at": now,
        }
        embedding = self._get_embedding(content)
        if self._vectors is None:
            self._vectors = np.array([embedding])
        else:
            self._vectors = np.vstack([self._vectors, [embedding]])
        self._metadata.append(entry)
        self._save()
        return {"id": mem_id, "content": content}

    def add_facts(self, text: str, user_id: str = "main_user",
                  source: str = "", run_id: str = "") -> list:
        """直接存原文（跳过 LLM 提取，加快速度）"""
        result = self.add(text[:500], user_id, source, run_id)
        return [result]

    def search(self, query: str, user_id: str = None,
               top_k: int = None, threshold: float = None) -> list:
        """语义搜索记忆，返回按相似度排序的结果"""
        self._ensure_loaded()
        if self._vectors is None or len(self._vectors) == 0:
            return []
        top_k = top_k or self.config['top_k']
        threshold = threshold or self.config['similarity_threshold']
        query_vec = np.array(self._get_embedding(query))
        # 计算所有向量的余弦距离
        distances = np.array([
            self._cosine_distance(query_vec, v) for v in self._vectors
        ])
        # 过滤用户
        indices = list(range(len(self._metadata)))
        if user_id:
            indices = [i for i in indices if self._metadata[i].get("user_id") == user_id]
        if not indices:
            return []
        # 按距离排序
        sorted_indices = sorted(indices, key=lambda i: distances[i])
        results = []
        for i in sorted_indices:
            if distances[i] > threshold:
                continue
            results.append({
                "id": self._metadata[i]["id"],
                "content": self._metadata[i]["content"],
                "score": float(round(1.0 - distances[i], 4)),
                "created_at": self._metadata[i].get("created_at", ""),
                "source": self._metadata[i].get("source", ""),
            })
            if len(results) >= top_k:
                break
        return results

    def get_all(self, user_id: str = None) -> list:
        """获取所有记忆"""
        self._ensure_loaded()
        if user_id:
            return [m for m in self._metadata if m.get("user_id") == user_id]
        return list(self._metadata)

    def get(self, memory_id: str) -> dict | None:
        """获取单条记忆"""
        self._ensure_loaded()
        for m in self._metadata:
            if m["id"] == memory_id:
                return dict(m)
        return None

    def update(self, memory_id: str, new_content: str) -> dict:
        """更新记忆内容（重新计算向量）"""
        self._ensure_loaded()
        for i, m in enumerate(self._metadata):
            if m["id"] == memory_id:
                now = datetime.now(timezone.utc).isoformat()
                # 记录历史
                history = m.get("history", [])
                history.append({"old": m["content"], "new": new_content, "at": now})
                m["content"] = new_content
                m["updated_at"] = now
                m["history"] = history
                # 重新计算向量
                new_vec = self._get_embedding(new_content)
                self._vectors[i] = new_vec
                self._save()
                return {"id": memory_id, "content": new_content}
        return {"error": f"记忆 {memory_id} 不存在"}

    def delete(self, memory_id: str) -> dict:
        """删除记忆"""
        self._ensure_loaded()
        for i, m in enumerate(self._metadata):
            if m["id"] == memory_id:
                self._metadata.pop(i)
                self._vectors = np.delete(self._vectors, i, axis=0)
                if len(self._vectors) == 0:
                    self._vectors = None
                self._save()
                return {"success": True, "id": memory_id}
        return {"error": f"记忆 {memory_id} 不存在"}

    def delete_all(self, user_id: str = None) -> dict:
        """清空记忆"""
        self._ensure_loaded()
        if user_id:
            # 只删除特定用户的记忆
            new_meta = []
            new_vecs = []
            for i, m in enumerate(self._metadata):
                if m.get("user_id") != user_id:
                    new_meta.append(m)
                    if self._vectors is not None:
                        new_vecs.append(self._vectors[i])
            self._metadata = new_meta
            self._vectors = np.array(new_vecs) if new_vecs else None
        else:
            self._metadata = []
            self._vectors = None
        self._save()
        return {"success": True}

    # ========== 公开 API ==========

_store: MemoryStore | None = None


def get_store(config: dict = None) -> MemoryStore:
    """获取全局记忆存储实例"""
    global _store
    if _store is None:
        _store = MemoryStore(config)
    return _store


# ========== CLI 入口 ==========

def main():
    import argparse
    parser = argparse.ArgumentParser(description="伯仕记忆系统")
    sub = parser.add_subparsers(dest="cmd")

    # add
    p = sub.add_parser("add", help="添加记忆")
    p.add_argument("content")
    p.add_argument("--user", default="main_user")
    p.add_argument("--source", default="")

    # search
    p = sub.add_parser("search", help="语义搜索")
    p.add_argument("query")
    p.add_argument("--user", default="main_user")
    p.add_argument("--top-k", type=int, default=5)

    # get-all
    sub.add_parser("get-all", help="全部记忆")

    # get
    p = sub.add_parser("get", help="单条记忆")
    p.add_argument("mem_id")

    # delete
    p = sub.add_parser("delete", help="删除记忆")
    p.add_argument("mem_id")

    # delete-all
    p = sub.add_parser("delete-all", help="清空全部")

    # stats
    sub.add_parser("stats", help="统计信息")

    args = parser.parse_args()
    if not args.cmd:
        parser.print_help()
        sys.exit(1)

    store = get_store()

    if args.cmd == "add":
        result = store.add_facts(args.content, user_id=args.user, source=args.source)
        print(json.dumps(result, ensure_ascii=False, indent=2))

    elif args.cmd == "search":
        results = store.search(args.query, user_id=args.user, top_k=args.top_k)
        print(json.dumps(results, ensure_ascii=False, indent=2))

    elif args.cmd == "get-all":
        meta = store.get_all()
        print(f"共 {len(meta)} 条记忆")
        for m in meta:
            print(f"  [{m['id']}] {m['content'][:80]}")

    elif args.cmd == "get":
        m = store.get(args.mem_id)
        if m:
            print(json.dumps(m, ensure_ascii=False, indent=2))
        else:
            print(f"记忆 {args.mem_id} 不存在")

    elif args.cmd == "delete":
        result = store.delete(args.mem_id)
        print(json.dumps(result, ensure_ascii=False, indent=2))

    elif args.cmd == "delete-all":
        store.delete_all()
        print("✅ 全部记忆已清空")

    elif args.cmd == "stats":
        meta = store.get_all()
        vecs = store._vectors
        print(f"记忆总数: {len(meta)}")
        print(f"向量维度: {vecs.shape[1] if vecs is not None else 'N/A'}")
        print(f"存储路径: {MEMORY_DIR}")
        print(f"向量模型: {store.config['embed_model']}")


if __name__ == "__main__":
    main()
