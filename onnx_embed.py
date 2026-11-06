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
# 首次加载的并发保护（2026-10-10 修复）：
# MCP server 会**并发执行**工具调用（如 boshi_save 与 boshi_search 同时来），
# 两个线程同时 `from transformers import AutoTokenizer` 时，后到者会看到
# "半初始化"的 transformers 模块 → 抛 `cannot import name 'AutoTokenizer'`
# （实测：单线程调用永远正常，并发才偶发；opencode 的 save+search 成对调用正好命中）
_ef_load_lock = None


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
    """惰性单例：首次调用时加载模型（约 1-3 秒）。

    **加锁**：多线程并发首次加载会因 transformers 半初始化而报
    `cannot import name 'AutoTokenizer'`（2026-10-10 实测，MCP 并发调用触发）。
    """
    global _boshi_ef_instance, _ef_load_lock
    if _boshi_ef_instance is None:
        if _ef_load_lock is None:
            import threading
            _ef_load_lock = threading.Lock()
        with _ef_load_lock:
            if _boshi_ef_instance is None:      # 双检：等锁期间别人可能已加载好
                import onnxruntime as ort

                tokenizer = _load_tokenizer()
                session = ort.InferenceSession(str(ONNX_FILE), providers=["CPUExecutionProvider"])
                input_names = [i.name for i in session.get_inputs()]
                _boshi_ef_instance = _BoshiONNX(tokenizer, session, input_names)
    return _boshi_ef_instance


def _load_tokenizer():
    """加载 bge-m3 分词器：优先 tokenizers.Tokenizer.from_file（快路径）。

    2026-11-06：`from transformers import AutoTokenizer` 冷加载实测 ~5s（transformers
    全家桶 import 占大头），是「新会话首轮检索 17s > Hermes 8s prefetch 门 → 首轮召回
    被丢」的主要构成之一。`tokenizers.Tokenizer.from_file` 只读 tokenizer.json，实测
    ~0.8s，且 token ids 与 transformers 路线**逐 token 一致**（batch 内已验证）。

    返回一个小适配器，接口覆盖本文件 `_encode` 用到的三种调用形态：
      - `tokenizer(texts, padding=True, truncation=True, max_length=..., return_tensors="np")`
        → dict {input_ids, attention_mask}（np.ndarray）
      - 语义与 transformers `AutoTokenizer` 完全对齐（pad 到 batch 内最长、截断 512）
    transformers 不可用时自动走此路径；`BOSHI_TOKENIZERS_ONLY=0` 可强制回旧路线。
    """
    import os

    if os.environ.get("BOSHI_TOKENIZERS_ONLY", "1") != "0":
        try:
            from tokenizers import Tokenizer

            tok = Tokenizer.from_file(str(BOSHI_MODEL_DIR / "tokenizer.json"))
            # bge-m3 的 pad 是小写 "<pad>"=1（token_to_id("[PAD]") 为 None，
            # 大写形式会静默落到 0=<s>，污染 padding 位置）
            pad_id = tok.token_to_id("<pad>")
            if pad_id is None:
                pad_id = tok.token_to_id("[PAD]")
            if pad_id is None:
                pad_id = 0
            return _FastTokenizerAdapter(tok, pad_id=pad_id, max_length=MAX_SEQ_LEN)
        except Exception:  # noqa: BLE001  tokenizer.json 缺失/损坏时回退 transformers
            pass
    from transformers import AutoTokenizer

    return AutoTokenizer.from_pretrained(str(BOSHI_MODEL_DIR))


class _FastTokenizerAdapter:
    """tokenizers.Tokenizer 的 transformers 兼容薄壳（只覆盖 _encode 所需）。"""

    def __init__(self, tok, pad_id: int, max_length: int):
        self._tok = tok
        self._pad_id = pad_id
        self._max_length = max_length

    def enable_truncation(self, max_length: int) -> None:
        self._max_length = max_length

    def __call__(self, texts: List[str], padding=True, truncation=True,
                 max_length=None, return_tensors="np"):
        if max_length:
            self._max_length = max_length
        if truncation:
            self._tok.enable_truncation(max_length=self._max_length)
        else:
            self._tok.no_truncation()
        encs = self._tok.encode_batch(list(texts))
        ids = [e.ids for e in encs]
        masks = [e.attention_mask for e in encs]
        if padding:
            width = max(len(x) for x in ids)
            ids = [x + [self._pad_id] * (width - len(x)) for x in ids]
            masks = [x + [0] * (width - len(x)) for x in masks]
        return {
            "input_ids": np.asarray(ids, dtype=np.int64),
            "attention_mask": np.asarray(masks, dtype=np.int64),
        }


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
