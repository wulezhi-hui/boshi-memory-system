#!/usr/bin/env python3
"""
伯仕记忆系统 v4 —— ChromaDB 版

零外部依赖，自带 embedding，不调 Ollama。
ChromaDB 用 all-MiniLM-L6-v2 做默认 embedding（~80MB，CPU运行）。
所有数据存在 ~/.boshi/memory/chroma/ 下。

彻底告别 "Ollama 堵车就失忆" 的问题。
"""

import os
import sys
import uuid
from datetime import datetime

# ChromaDB 入口
import chromadb
from chromadb.config import Settings

# ========== 配置 ==========
MEMORY_DIR = os.path.expanduser("~/.boshi/memory/chroma")
COLLECTION_NAME = "boshi_memory"

# 确保目录存在
os.makedirs(MEMORY_DIR, exist_ok=True)


class BoShiMemoryChroma:
    """伯仕记忆系统 v4 —— ChromaDB 实现"""

    def __init__(self):
        # ChromaDB 持久化客户端（数据存磁盘）
        self.client = chromadb.PersistentClient(
            path=MEMORY_DIR,
            settings=Settings(anonymized_telemetry=False)
        )

        # 自定义 embedding 函数：用 sentence-transformers 代替 ChromaDB 内置的 ONNX
        self._init_embedding()

        # 创建/获取集合时指定自定义 embedding
        try:
            self.collection = self.client.get_collection(
                COLLECTION_NAME,
                embedding_function=self._ef
            )
        except Exception:
            self.collection = self.client.create_collection(
                COLLECTION_NAME,
                embedding_function=self._ef
            )

    def _init_embedding(self):
        """初始化本地 embedding 模型"""
        from sentence_transformers import SentenceTransformer
        import glob

        snapshots = glob.glob(os.path.expanduser(
            "~/.cache/huggingface/hub/"
            "models--sentence-transformers--all-MiniLM-L6-v2/snapshots/*"
        ))

        if snapshots:
            model_path = snapshots[0]
        else:
            # 没有缓存，直接加载（会下载）
            model_path = "all-MiniLM-L6-v2"

        self._encoder = SentenceTransformer(model_path, device="cpu")

        # ChromaDB embedding 函数接口
        from chromadb.api.types import EmbeddingFunction

        class LocalEF(EmbeddingFunction):
            def __init__(self, encoder):
                self.encoder = encoder

            def __call__(self, texts):
                emb = self.encoder.encode(texts, show_progress_bar=False)
                return emb.tolist()

        self._ef = LocalEF(self._encoder)

    def add(self, content: str, topic: str = "", metadata: dict = None) -> str:
        """
        存入一条记忆。
        
        参数:
            content: 记忆内容（原文存储）
            topic: 话题标签（可选）
            metadata: 额外元数据（可选）
        
        返回:
            record_id: 记录 ID
        """
        record_id = str(uuid.uuid4())
        timestamp = datetime.now().isoformat()

        # 构建元数据
        meta = {
            "content": content[:500],  # Chroma 元数据限制，存摘要
            "topic": topic[:200],
            "timestamp": timestamp,
            "source": "conversation",
        }
        if metadata:
            meta.update(metadata)

        # 自定义 embedding（用 sentence-transformers 本地算）
        self.collection.add(
            documents=[content],
            metadatas=[meta],
            ids=[record_id]
        )

        return record_id

    def search(self, query: str, top_k: int = 5, topic: str = "") -> list:
        """
        搜索记忆。
        
        参数:
            query: 搜索关键词
            top_k: 返回条数
            topic: 限定话题（可选）
        
        返回:
            [{"id": str, "content": str, "topic": str, "timestamp": str, "distance": float}, ...]
        """
        # 构建过滤条件
        where = None
        if topic:
            where = {"topic": topic}

        results = self.collection.query(
            query_texts=[query],
            n_results=top_k,
            where=where
        )

        formatted = []
        if results["ids"]:
            for i in range(len(results["ids"][0])):
                record = {
                    "id": results["ids"][0][i],
                    "content": results["documents"][0][i] if results["documents"] else "",
                    "distance": results["distances"][0][i] if results["distances"] else 0,
                }
                # 合并元数据
                if results["metadatas"] and results["metadatas"][0]:
                    meta = results["metadatas"][0][i]
                    record["topic"] = meta.get("topic", "")
                    record["timestamp"] = meta.get("timestamp", "")

                formatted.append(record)

        return formatted

    def get_all(self, limit: int = 50, offset: int = 0) -> list:
        """获取所有记忆（翻页）"""
        results = self.collection.get(limit=limit, offset=offset)

        formatted = []
        if results["ids"]:
            for i in range(len(results["ids"])):
                record = {
                    "id": results["ids"][i],
                    "content": results["documents"][i] if results["documents"] else "",
                }
                if results["metadatas"] and results["metadatas"][i]:
                    meta = results["metadatas"][i]
                    record["topic"] = meta.get("topic", "")
                    record["timestamp"] = meta.get("timestamp", "")
                formatted.append(record)

        return formatted

    def delete(self, record_id: str) -> bool:
        """删除一条记忆"""
        try:
            self.collection.delete(ids=[record_id])
            return True
        except Exception:
            return False

    def count(self) -> int:
        """记忆总数"""
        return self.collection.count()

    def get_topics(self) -> list:
        """获取所有话题标签"""
        all_meta = self.collection.get()["metadatas"]
        topics = set()
        for m in all_meta:
            if m and m.get("topic"):
                topics.add(m["topic"])
        return sorted(topics)


# ========== 快捷工具函数 ==========

_memory_instance = None


def _get_memory():
    """获取单例"""
    global _memory_instance
    if _memory_instance is None:
        _memory_instance = BoShiMemoryChroma()
    return _memory_instance


def search(query: str, top_k: int = 5, topic: str = "") -> list:
    """搜索记忆（快捷调用）"""
    return _get_memory().search(query, top_k, topic)


def remember(content: str, topic: str = "", metadata: dict = None) -> str:
    """存入记忆（快捷调用）"""
    return _get_memory().add(content, topic, metadata)


def forget(record_id: str) -> bool:
    """删除记忆"""
    return _get_memory().delete(record_id)


def stats() -> dict:
    """记忆统计"""
    m = _get_memory()
    return {
        "total": m.count(),
        "topics": m.get_topics(),
        "engine": "chromadb (all-MiniLM-L6-v2)",
        "storage_path": MEMORY_DIR,
    }


# ========== CLI 入口 ==========
if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "help"

    if cmd == "search":
        query = sys.argv[2] if len(sys.argv) > 2 else ""
        results = search(query)
        for r in results:
            print(f"[{r['distance']:.3f}] {r['content'][:100]}")

    elif cmd == "add":
        content = sys.argv[2] if len(sys.argv) > 2 else ""
        topic = sys.argv[3] if len(sys.argv) > 3 else ""
        rid = remember(content, topic)
        print(f"OK: {rid}")

    elif cmd == "stats":
        s = stats()
        print(f"总记忆: {s['total']}")
        print(f"话题: {', '.join(s['topics'])}")
        print(f"引擎: {s['engine']}")

    elif cmd == "topics":
        for t in _get_memory().get_topics():
            print(t)

    else:
        print("用法: python chroma_memory.py <search|add|stats|topics> [参数]")
