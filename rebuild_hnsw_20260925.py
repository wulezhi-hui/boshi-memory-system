#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
伯仕记忆库 HNSW 索引损坏恢复工具 (2026-09-25)

背景：chroma_db 的 HNSW 段 (a20c7193-...) 的 index_metadata.pickle 损坏
      （dimensionality=None / max_seq_id=None / id_to_seq_id 清空），
      Rust 读 HNSW 时 access violation → count/query 段错误。
      sqlite 层完好（30728 条文档 + 全量 metadata）。

思路：从 sqlite 逻辑导出文档+metadata → 用同一 bge-m3 ONNX 重算 1024 维 →
      在全新目录重建同名 collection (boshi_memory / cosine) → 校验 → 替换。

用法：
  venv/Scripts/python.exe rebuild_hnsw_20260925.py extract   # 逻辑导出 (只读)
  venv/Scripts/python.exe rebuild_hnsw_20260925.py build     # 重建到 chroma_db_rebuild
  venv/Scripts/python.exe rebuild_hnsw_20260925.py verify --path <dir>
"""

import io
import json
import os
import sqlite3
import sys
import time

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

BOSHI = os.path.expanduser("~/.boshi")
SRC_DB = os.path.join(BOSHI, "chroma_db")
SRC_SQLITE = os.path.join(SRC_DB, "chroma.sqlite3")
EXTRACT = os.path.join(BOSHI, "chroma_extract_20260925.json")
NEW_DB = os.path.join(BOSHI, "chroma_db_rebuild")
COLLECTION = "boshi_memory"
MAX_COUNT = 256      # 单批条数上限
CHAR_BUDGET = 12000  # 单批 (条数 × 批内最长文本) 字符预算
                     # —— attention 代价≈batch×seq²，固定 256 条对长文档会把内存打爆
                     #    （实测 256 条 ×2000token → 15GB 驻留、换页、30 分钟不落盘）


def extract():
    """从损坏库的 sqlite 逻辑导出全部文档 + metadata。只读。

    注意两点（都踩过）：
    1. embedding_metadata.id 是整数行号，真正的文档 ID 在
       embeddings.embedding_id（UUID），必须 JOIN 取，否则得到 int 而非 str。
    2. 值是分列存的：字符串在 string_value、布尔在 bool_value、整数在 int_value、
       浮点在 float_value。SQLite 的 BOOLEAN 读出来是 int，必须显式 bool()，
       否则重建后 isLatest 变 1，where={"isLatest": True} 会全不命中。
    """
    src = SRC_SQLITE
    if "--src" in sys.argv:
        src = sys.argv[sys.argv.index("--src") + 1]
    con = sqlite3.connect("file:%s?mode=ro" % src, uri=True)
    meta = {}
    for eid, key, sv, iv, fv, bv in con.execute(
        "SELECT e.embedding_id, em.key, em.string_value, em.int_value, "
        "em.float_value, em.bool_value "
        "FROM embedding_metadata em JOIN embeddings e ON e.id = em.id"
    ):
        if sv is not None:
            val = sv
        elif bv is not None:
            val = bool(bv)      # BOOLEAN 列必须显式转 bool
        elif iv is not None:
            val = int(iv)
        elif fv is not None:
            val = float(fv)
        else:
            continue
        meta.setdefault(eid, {})[key] = val

    ids, docs, metas = [], [], []
    nodoc = 0
    for eid, kv in meta.items():
        doc = kv.pop("chroma:document", None)
        if doc is None:
            nodoc += 1
            continue
        # 剔除 chroma 内部键，只留业务 metadata
        clean = {k: v for k, v in kv.items() if not k.startswith("chroma:")}
        ids.append(eid)
        docs.append(doc)
        metas.append(clean)

    with open(EXTRACT, "w", encoding="utf-8") as f:
        json.dump({"ids": ids, "documents": docs, "metadatas": metas},
                  f, ensure_ascii=False)
    print("EXTRACT_OK  docs=%d (无文档条目跳过=%d) -> %s (%.1f MB)" % (
        len(ids), nodoc, EXTRACT, os.path.getsize(EXTRACT) / 1e6))
    print("  全部 id 均为 str: %s" % all(isinstance(i, str) for i in ids))
    # 抽样核对
    for i in (0, len(ids) // 2, len(ids) - 1):
        print("  sample[%d] id=%s doc_len=%d meta_keys=%s" % (
            i, ids[i], len(docs[i] or ""), sorted(metas[i].keys())[:6]))
    return len(ids)


def _pack(order, docs, char_budget=None, max_count=None):
    """把已按长度排序的索引打包：条数 ≤ max_count 且 条数×批内最长 ≤ char_budget。

    长文档因此只会得到很小的批（如 2000 字符 → 约 6 条），把
    attention 的 batch×seq² 内存压成线性可控，避免换页。
    """
    char_budget = char_budget or CHAR_BUDGET
    max_count = max_count or MAX_COUNT
    out, batch, cur_max = [], [], 0
    for i in order:
        L = len(docs[i] or "")
        m = max(cur_max, L)
        if batch and (len(batch) + 1 > max_count or m * (len(batch) + 1) > char_budget):
            out.append(batch)
            batch, cur_max = [], 0
            m = L
        batch.append(i)
        cur_max = m
    if batch:
        out.append(batch)
    return out


def build():
    """读取导出文件，重算向量，重建到全新目录。--resume 时跳过已入库的 id。"""
    import chromadb
    import numpy as np
    from onnx_embed import get_embedding_function

    with open(EXTRACT, encoding="utf-8") as f:
        data = json.load(f)
    ids, docs, metas = data["ids"], data["documents"], data["metadatas"]
    print("loaded %d docs" % len(ids))

    ef = get_embedding_function()
    resume = "--resume" in sys.argv
    if os.path.isdir(NEW_DB) and not resume:
        import shutil
        shutil.rmtree(NEW_DB)
    client = chromadb.PersistentClient(path=NEW_DB)
    col = client.get_or_create_collection(
        COLLECTION, embedding_function=ef, metadata={"hnsw:space": "cosine"})

    have = set()
    if resume:
        try:
            have = set(col.get(include=[])["ids"])
            print("resume: 库内已有 %d 条，跳过" % len(have))
        except Exception as e:
            print("resume 读取失败(%s)，改为全量重建" % e)

    todo = [i for i in range(len(ids)) if ids[i] not in have]
    order = sorted(todo, key=lambda i: len(docs[i] or ""))
    batches = _pack(order, docs)
    print("todo=%d  batches=%d  (max_count=%d char_budget=%d)" % (
        len(order), len(batches), MAX_COUNT, CHAR_BUDGET))
    done = 0
    t0 = time.time()
    for bi, idx in enumerate(batches, 1):
        texts = [docs[i] or " " for i in idx]
        vecs = ef(texts)
        col.add(
            ids=[ids[i] for i in idx],
            documents=[docs[i] for i in idx],
            metadatas=[metas[i] for i in idx],
            embeddings=np.asarray(vecs, dtype=np.float32),
        )
        done += len(idx)
        if bi % 10 == 0 or done == len(order):
            el = time.time() - t0
            rate = done / el if el else 0
            print("  %d/%d  批%d/%d  %.1f docs/s  elapsed %.1fm  eta %.1fm" % (
                done, len(order), bi, len(batches), rate, el / 60,
                ((len(order) - done) / rate / 60) if rate else -1), flush=True)
    n = col.count()
    print("BUILD_DONE count=%d in %.1f min" % (n, (time.time() - t0) / 60))
    if n != len(ids):
        print("WARNING count mismatch: col=%d extract=%d (resume 跳过后仍应相等)" % (
            n, len(ids)))
    return n


def fixmeta():
    """把已建库中类型写错的 metadata 就地修回（主要 isLatest: 1 -> True）。

    只用 col.update，不重算向量，所以秒级~分钟级完成。
    """
    import chromadb
    from onnx_embed import get_embedding_function

    path = NEW_DB
    if "--path" in sys.argv:
        path = sys.argv[sys.argv.index("--path") + 1]
    with open(EXTRACT, encoding="utf-8") as f:
        data = json.load(f)
    ids, metas = data["ids"], data["metadatas"]

    ef = get_embedding_function()
    c = chromadb.PersistentClient(path=path)
    col = c.get_collection(COLLECTION, embedding_function=ef)
    have = set(col.get(include=[])["ids"])
    keep = [i for i in range(len(ids)) if ids[i] in have]
    skip = len(ids) - len(keep)
    print("count=%d  extract=%d  库内可更新=%d  不在库内(跳过)=%d" % (
        col.count(), len(ids), len(keep), skip))
    B = 500
    done = 0
    t0 = time.time()
    for s in range(0, len(keep), B):
        idx = keep[s:s + B]
        col.update(ids=[ids[i] for i in idx], metadatas=[metas[i] for i in idx])
        done += len(idx)
        if done % 5000 == 0 or done == len(keep):
            print("  update %d/%d  %.1fm" % (done, len(keep), (time.time() - t0) / 60),
                  flush=True)
    print("FIXMETA_DONE %d in %.1fm" % (done, (time.time() - t0) / 60))
    # 立即验证
    for where in ({"isLatest": True}, {"isLatest": 1}):
        try:
            r = col.query(query_texts=["微调"], n_results=3, where=where)
            n = len(r["ids"][0]) if r.get("ids") and r["ids"][0] else 0
            print("  where=%-22s -> %d 条" % (json.dumps(where), n))
        except Exception as e:
            print("  where=%s ERR %s" % (json.dumps(where), e))
    return done


def verify(path):
    import chromadb
    from onnx_embed import get_embedding_function
    c = chromadb.PersistentClient(path=path)
    col = c.get_collection(COLLECTION, embedding_function=get_embedding_function())
    n = col.count()
    print("count=%d" % n)
    r = col.query(query_texts=["微调 大模型 Turing"], n_results=3)
    for i, d, m in zip(r["ids"][0], r["documents"][0], r["metadatas"][0]):
        print("  hit:", (d or "")[:60].replace("\n", " "), "|",
              {k: m.get(k) for k in ("topic", "type", "profile") if k in m})
    r2 = col.get(limit=2)
    print("get() ok, ids:", r2["ids"])
    return n


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    if cmd == "extract":
        extract()
    elif cmd == "build":
        build()
    elif cmd == "fixmeta":
        fixmeta()
    elif cmd == "verify":
        p = sys.argv[sys.argv.index("--path") + 1] if "--path" in sys.argv else NEW_DB
        verify(p)
    else:
        print(__doc__)
