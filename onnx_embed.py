"""
伯仕自带 ONNX 向量模型 — 零外部依赖的 Embedding

优先使用仓库内自带的 models/all-MiniLM-L6-v2/onnx/ 目录。
如果该目录不存在，fallback 到 ChromaDB 的自动下载机制（需联网）。

不依赖: torch, transformers, sentence-transformers, HuggingFace
仅需: chromadb (已内含 onnxruntime + tokenizers)

Usage:
    from onnx_embed import BoshiEmbeddingFunction
    ef = BoshiEmbeddingFunction()
    vecs = ef(["伯仕记忆系统"])
"""

import os
from pathlib import Path
from typing import List, Optional

# ── 配置 ──────────────────────────────────────────────
BOSHI_MODEL_DIR = Path.home() / ".boshi" / "models" / "all-MiniLM-L6-v2"


def _get_model_path() -> Optional[Path]:
    """检查自带模型是否存在"""
    bundled = BOSHI_MODEL_DIR / "onnx" / "model.onnx"
    if bundled.exists():
        return BOSHI_MODEL_DIR  # 返回父目录（ONNXMiniLM_L6_V2 会在里面找 onnx/ 子目录）
    return None


def _create_boshi_ef():
    """
    创建伯仕专属的 EmbeddingFunction。
    优先使用自带模型；否则 fallback 到 ChromaDB 默认行为。
    """
    # 懒导入（避免启动时加载 ONNX）
    from chromadb.utils.embedding_functions.onnx_mini_lm_l6_v2 import ONNXMiniLM_L6_V2

    model_parent = _get_model_path()

    if model_parent is not None:
        # 有自带模型：monkey-patch 指向本地
        original_download_path = ONNXMiniLM_L6_V2.DOWNLOAD_PATH
        original_extracted_folder = ONNXMiniLM_L6_V2.EXTRACTED_FOLDER_NAME

        ONNXMiniLM_L6_V2.DOWNLOAD_PATH = model_parent
        ONNXMiniLM_L6_V2.EXTRACTED_FOLDER_NAME = "onnx"

        try:
            ef = ONNXMiniLM_L6_V2()
            # 标记：这是用自带模型的版本
            ef._boshi_source = "bundled"
            return ef
        finally:
            # 恢复原始值（不影响其他代码）
            ONNXMiniLM_L6_V2.DOWNLOAD_PATH = original_download_path
            ONNXMiniLM_L6_V2.EXTRACTED_FOLDER_NAME = original_extracted_folder
    else:
        # 无自带模型：fallback 到 ChromaDB 自动下载
        ef = ONNXMiniLM_L6_V2()
        ef._boshi_source = "downloaded"
        return ef


# ── 单例缓存 ──────────────────────────────────────────
_boshi_ef_instance = None


def _get_cached_ef():
    """惰性单例，避免重复加载"""
    global _boshi_ef_instance
    if _boshi_ef_instance is None:
        _boshi_ef_instance = _create_boshi_ef()
    return _boshi_ef_instance


# ── 公共接口 ───────────────────────────────────────────

class BoshiEmbeddingFunction:
    """
    伯仕记忆系统专用 Embedding Function。

    - 优先使用 ~/.boshi/models/all-MiniLM-L6-v2/ 目录下的 ONNX 模型
    - 模型不存在时自动 fallback 到 ChromaDB 的下载机制
    - 384 维向量，兼容现有 ChromaDB collection
    """

    def __call__(self, input: List[str]) -> List[List[float]]:
        ef = _get_cached_ef()
        return ef(input)

    def get_source(self) -> str:
        """返回当前使用的模型来源"""
        ef = _get_cached_ef()
        return getattr(ef, "_boshi_source", "unknown")

    @staticmethod
    def is_bundled_available() -> bool:
        """检查自带模型是否可用（安装脚本可用此判断）"""
        return _get_model_path() is not None

    @staticmethod
    def model_info() -> dict:
        """返回模型信息"""
        path = _get_model_path()
        onnx_file = path / "onnx" / "model.onnx" if path else None
        return {
            "model_name": "all-MiniLM-L6-v2",
            "dimensions": 384,
            "bundled_path": str(path) if path else None,
            "bundled_exists": onnx_file.exists() if onnx_file else False,
            "onnx_file_size_mb": round(onnx_file.stat().st_size / (1024 * 1024), 1) if onnx_file and onnx_file.exists() else None,
        }


# ── chroma_bridge.py 兼容接口 ──────────────────────────

def get_embedding_function():
    """chroma_bridge.py 用这个函数获取 ChromaDB 兼容的 EmbeddingFunction"""
    return _create_boshi_ef()


# ── 调试入口 ───────────────────────────────────────────

if __name__ == "__main__":
    print("🦄 伯仕自带 ONNX Embedding 测试")
    print(f"模型信息: {BoshiEmbeddingFunction.model_info()}")
    print()

    ef = get_embedding_function()
    print(f"返回类型: {type(ef).__name__}")
    print(f"模型来源: {getattr(ef, '_boshi_source', 'unknown')}")

    texts = ["伯仕记忆系统 v6.0", "Ubuntu 部署测试", "Hello World"]
    vecs = ef(texts)

    for t, v in zip(texts, vecs):
        print(f"  '{t}' -> dim={len(v)}, norm={sum(x*x for x in v)**0.5:.3f}")

    print("\n✅ 向量化成功！")
