"""
伯仕自带 ONNX 向量模型 — bge-m3 版（零外部依赖，纯本地推理）
==============================================================
真·bge-m3（Xenova ONNX 导出，1024 维，多语言旗舰，中文检索质量优秀）。

实现: onnxruntime（CPU 推理）+ transformers tokenizer（本地加载）
不依赖: Ollama, torch, sentence-transformers, 在线推理

模型文件: ~/.boshi/models/bge-m3/
  ├─ onnx/model_quantized.onnx   （优先，int8 量化 ~600MB）
  ├─ onnx/model.onnx             （fp32 回退 ~2.3GB）
  ├─ tokenizer.json / tokenizer_config.json / config.json / special_tokens_map.json

Usage:
    from onnx_embed import BoshiEmbeddingFunction
    ef = BoshiEmbeddingFunction()
    vecs = ef(["伯仕记忆系统"])
"""

import os
import numpy as np
from pathlib import Path
from typing import List

# ── 配置 ──────────────────────────────────────────────
BOSHI_MODEL_DIR = Path.home() / ".boshi" / "models" / "bge-m3"
ONNX_DIR = BOSHI_MODEL_DIR / "onnx"
ONNX_FILE = (
    ONNX_DIR / "model_quantized.onnx"
    if (ONNX_DIR / "model_quantized.onnx").exists()
    else ONNX_DIR / "model.onnx"
)
MODEL_DIMENSIONS = 1024
MAX_SEQ_LEN = 512

_boshi_ef_instance = None


class _BoshiONNX:
    """onnxruntime + transformers tokenizer 的 bge-m3 推理封装"""

    def __init__(self, tokenizer, session, input_names):
        self._tokenizer = tokenizer
        self._session = session
        self._input_names = input_names

    def _encode(self, texts: List[str]):
        enc = self._tokenizer(
            texts,
            padding=True,
            truncation=True,
            max_length=MAX_SEQ_LEN,
            return_tensors="np",
        )
        feed = {k: v for k, v in enc.items() if k in self._input_names}
        outputs = self._session.run(None, feed)
        # 输出 0 即 last_hidden_state（[batch, seq, hidden]）
        last_hidden = outputs[0]
        return last_hidden, enc["attention_mask"]


def _get_cached_ef():
    """惰性单例：首次调用时加载模型（约 1-3 秒）"""
    global _boshi_ef_instance
    if _boshi_ef_instance is None:
        import onnxruntime as ort
        from transformers import AutoTokenizer

        tokenizer = AutoTokenizer.from_pretrained(str(BOSHI_MODEL_DIR))
        session = ort.InferenceSession(str(ONNX_FILE), providers=["CPUExecutionProvider"])
        input_names = [i.name for i in session.get_inputs()]
        _boshi_ef_instance = _BoshiONNX(tokenizer, session, input_names)
    return _boshi_ef_instance


# ── 公共接口（chroma_bridge.py 兼容）────────────────────

class BoshiEmbeddingFunction:
    """伯仕记忆系统专用 Embedding Function — bge-m3 ONNX 本地推理（1024 维）"""

    def __call__(self, input: List[str]) -> List[List[float]]:
        ef = _get_cached_ef()
        last_hidden, mask = ef._encode(input)
        # mean pooling + L2 归一化
        maskf = mask[..., None].astype(np.float32)
        summed = (last_hidden * maskf).sum(axis=1)
        counts = maskf.sum(axis=1).clip(min=1e-9)
        mean = summed / counts
        norm = np.linalg.norm(mean, axis=1, keepdims=True)
        return (mean / norm).tolist()

    def name(self) -> str:
        """chromadb 1.5+ 要求 EmbeddingFunction 提供 name() 用于冲突校验"""
        return "bge-m3-onnx"

    def embed_query(self, input):
        """chromadb 1.5+ 查询路径：兼容 str 或 List[str] 输入，返回 List[List[float]]（query_embeddings 格式）"""
        if isinstance(input, str):
            texts = [input]
        elif isinstance(input, (list, tuple)):
            texts = list(input)
        else:
            texts = [str(input)]
        return self.__call__(texts)

    def embed_documents(self, input: List[str]) -> List[List[float]]:
        """chromadb 1.5+ 文档路径：批量文本 → 向量"""
        return self.__call__(input)

    def get_source(self) -> str:
        return "bge-m3-onnx"

    @staticmethod
    def is_bundled_available() -> bool:
        return ONNX_FILE.exists()

    @staticmethod
    def model_info() -> dict:
        size_mb = None
        if ONNX_FILE.exists():
            size_mb = round(ONNX_FILE.stat().st_size / (1024 * 1024), 1)
        return {
            "model_name": "BAAI/bge-m3 (Xenova ONNX)",
            "dimensions": MODEL_DIMENSIONS,
            "onnx_file": str(ONNX_FILE),
            "onnx_exists": ONNX_FILE.exists(),
            "onnx_size_mb": size_mb,
        }


def get_embedding_function():
    """chroma_bridge.py 用这个函数获取 ChromaDB 兼容的 EmbeddingFunction"""
    return BoshiEmbeddingFunction()


# ── 调试入口 ───────────────────────────────────────────

if __name__ == "__main__":
    print("🦄 伯仕 ONNX Embedding 测试（bge-m3）")
    print(f"模型信息: {BoshiEmbeddingFunction.model_info()}")
    print()

    ef = get_embedding_function()
    texts = ["伯仕记忆系统 v6.1", "乐之喜欢直接做不解释", "Hello World"]
    vecs = ef(texts)

    for t, v in zip(texts, vecs):
        print(f"  '{t}' -> dim={len(v)}, norm={sum(x*x for x in v)**0.5:.3f}")

    print("\n✅ 向量化成功！")
