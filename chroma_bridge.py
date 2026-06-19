"""
ChromaDB 记忆模块 — 伯仕记忆系统 v6.0
使用自带 ONNX 模型（all-MiniLM-L6-v2）做 embedding，零外部依赖
不依赖 Ollama / HuggingFace / torch / transformers
"""

import os
import uuid
import time as _time
from datetime import datetime as _datetime
import chromadb
from onnx_embed import BoshiEmbeddingFunction

# ── 配置 ──────────────────────────────────────────────
CHROMA_DIR = os.path.expanduser("~/.boshi/chroma_db")
# 本地 ONNX 模型路径（伯仕自带，零外部依赖）
# 优先使用仓库内的 models/all-MiniLM-L6-v2/onnx/ 目录
from onnx_embed import get_embedding_function as _get_embedding_function


# 兼容：chroma_bridge.py 内部都用 _get_embedding_function()，已由 onnx_embed 提供


COLLECTION_NAME = "boshi_memory"


def _get_client():
    """获取 ChromaDB 持久化客户端"""
    return chromadb.PersistentClient(path=CHROMA_DIR)

def _normalize_metadata(metadata: dict) -> dict:
    """归一化 metadata 中的时间戳字段：ISO 字符串 → float (Unix epoch)
    ChromaDB 的 hot load 要求 timestamp 字段是 float，ISO 字符串会导致
    'could not convert string to float' 错误并触发无限自愈循环。
    """
    if not metadata:
        return metadata or {}
    _TS_KEYS = {"timestamp", "last_decay", "date", "collected_at",
                 "created_at", "updated_at", "last_mentioned"}
    result = dict(metadata)
    for key in list(result.keys()):
        val = result[key]
        # 如果值是 ISO 格式字符串，转成 float
        if isinstance(val, str) and key in _TS_KEYS:
            try:
                # 尝试解析 ISO 格式
                dt = _datetime.fromisoformat(val.replace("Z", "+00:00"))
                result[key] = dt.timestamp()
            except (ValueError, TypeError):
                pass  # 不是日期字符串，保持原样
    return result


def add_memory(content: str, metadata: dict = None, memory_id: str = None):
    """
    添加一条记忆到 ChromaDB。
    参数：
        content: 记忆内容（纯文本）
        metadata: 附加元数据（支持 isLatest 字段控制版本可见性）
        memory_id: 自定义ID，不传则自动生成
    返回：
        memory_id

    不可变日志模式（v5.8）：
    - 所有写入都是追加，不覆盖
    - isLatest=true 表示当前有效版本
    - 历史版本 isLatest=false 但保留可追溯
    """
    client = _get_client()
    ef = _get_embedding_function()
    col = client.get_or_create_collection(COLLECTION_NAME, embedding_function=ef)

    if memory_id is None:
        import uuid
        memory_id = str(uuid.uuid4())

    # 确保新写入默认 isLatest=true（不可变日志模式）
    meta = _normalize_metadata(metadata or {})
    if "isLatest" not in meta:
        meta["isLatest"] = True

    # 记录版本时间
    import time as _time_module
    meta["_version_created"] = _time_module.time()

    col.add(
        documents=[content],
        metadatas=[meta],
        ids=[memory_id]
    )
    return memory_id


def add_memories_batch(entries: list):
    """
    批量添加记忆（自动分批，每批 100 条，间隔 3 秒）。
    参数：
        entries: [{"content": str, "metadata": dict, "id": str}, ...]
    返回：
        {"added": int, "failed": int, "errors": [str]}
    """
    client = _get_client()
    ef = _get_embedding_function()
    col = client.get_or_create_collection(COLLECTION_NAME, embedding_function=ef)

    BATCH_SIZE = 100
    BATCH_INTERVAL = 3
    added = 0
    failed = 0
    errors = []

    for i in range(0, len(entries), BATCH_SIZE):
        batch = entries[i:i + BATCH_SIZE]
        try:
            docs = []
            metas = []
            ids = []
            for e in batch:
                docs.append(e["content"])
                metas.append(_normalize_metadata(e.get("metadata", {})))
                ids.append(e.get("id") or str(uuid.uuid4()))
            col.add(documents=docs, metadatas=metas, ids=ids)
            added += len(batch)
            if i + BATCH_SIZE < len(entries):
                _time.sleep(BATCH_INTERVAL)
        except Exception as ex:
            failed += len(batch)
            errors.append(f"batch {i // BATCH_SIZE}: {ex}")

    return {"added": added, "failed": failed, "errors": errors}


def search_memory(query: str, top_k: int = 5, where: dict = None,
                  all_versions: bool = False):
    """
    语义搜索记忆。
    参数：
        query: 查询文本
        top_k: 返回条数（默认5，最多20）
        where: 可选的 metadata 过滤条件，如 {"type": "project_registry"}
        all_versions: 是否返回历史版本（默认 false，只返回 isLatest=true）
    返回：
        [{"id": str, "content": str, "metadata": dict, "score": float}, ...]
        按相似度降序排列
    """
    client = _get_client()
    ef = _get_embedding_function()
    col = client.get_or_create_collection(COLLECTION_NAME, embedding_function=ef)

    if col.count() == 0:
        return []

    # 版本链过滤：默认只返回当前有效版本
    if not all_versions:
        if where:
            # 合并 isLatest 条件
            if "$and" in where:
                where["$and"].append({"isLatest": True})
            else:
                where = {"$and": [where, {"isLatest": True}]}
        else:
            where = {"isLatest": True}

    kwargs = {"query_texts": [query], "n_results": min(top_k, 20)}
    if where:
        kwargs["where"] = where

    # CN Desktop 污染修复：ChromaDB where 过滤在损坏的 metadata 上会 InternalError
    # 降级为无过滤查询 + Python 端过滤
    try:
        results = col.query(**kwargs)
    except Exception:
        # 完全降级：无 where 过滤的大查询 + Python 端过滤
        fallback_kwargs = {"query_texts": [query], "n_results": max(top_k * 5, 50)}
        results = col.query(**fallback_kwargs)
        # Python 端过滤：检查 isLatest + 原始 where 条件
        if results["ids"] and results["ids"][0]:
            filtered_ids, filtered_docs, filtered_metas, filtered_dists = [], [], [], []
            metas_list = results.get("metadatas")
            docs_list = results.get("documents")
            dists_list = results.get("distances")
            for i in range(len(results["ids"][0])):
                meta = metas_list[0][i] if metas_list and metas_list[0] else {}
                meta = meta if meta is not None else {}
                # 版本过滤
                if not all_versions and meta.get("isLatest") is False:
                    continue
                # 原始 where 条件过滤（简化为 type 匹配）
                if where and isinstance(where, dict):
                    # 直接匹配: {"type": "xxx"}
                    if "type" in where and not isinstance(where["type"], dict):
                        if meta.get("type") != where["type"]:
                            continue
                    # $and 条件
                    if "$and" in where:
                        skip = False
                        for cond in where["$and"]:
                            if "type" in cond and meta.get("type") != cond["type"]:
                                skip = True
                                break
                        if skip:
                            continue
                filtered_ids.append(results["ids"][0][i])
                filtered_docs.append(docs_list[0][i] if docs_list and docs_list[0] else "")
                filtered_metas.append(meta)
                filtered_dists.append(dists_list[0][i] if dists_list and dists_list[0] else 0.0)
            results = {"ids": [filtered_ids], "documents": [filtered_docs], "metadatas": [filtered_metas], "distances": [filtered_dists]}

    output = []
    if results["ids"] and results["ids"][0]:
        for i in range(len(results["ids"][0])):
            # ChromaDB 在某些版本下 documents/metadatas 可能返回 None
            doc = (results["documents"][0][i]
                   if results.get("documents") and results["documents"][0]
                   else "")
            meta = (results["metadatas"][0][i]
                    if results.get("metadatas") and results["metadatas"][0]
                    else {})
            dist = (results["distances"][0][i]
                    if results.get("distances") and results["distances"][0]
                    else 0.0)
            output.append({
                "id": results["ids"][0][i],
                "content": doc,
                "metadata": meta,
                "score": dist,
            })
    return output


def hybrid_search(query: str, top_k: int = 5, where: dict = None,
                  all_versions: bool = False, search_sessions: bool = True):
    """
    混合搜索（Supermemory 借鉴）：语义向量 + 全文会话 合一。
    参数：
        query: 查询文本
        top_k: 返回条数（默认5，最多20）
        where: 可选的 metadata 过滤条件
        all_versions: 是否返回历史版本
        search_sessions: 是否同时搜索 state.db 会话记录
    返回：
        {
            "memories": [{id, content, metadata, score}, ...],
            "sessions": [{session_id, source, snippet, timestamp}, ...] (if search_sessions),
            "source": "hybrid"
        }
    """
    # 1. 语义搜索（主路径）
    memories = search_memory(query, top_k=top_k, where=where,
                             all_versions=all_versions)

    result = {
        "memories": memories,
        "sessions": [],
        "source": "hybrid",
    }

    # 2. 全文会话搜索（副路径）
    if search_sessions:
        try:
            import sqlite3
            state_db = os.path.join(
                os.environ.get("LOCALAPPDATA",
                               os.path.expanduser("~/AppData/Local")),
                "hermes", "state.db"
            )
            if os.path.exists(state_db):
                db = sqlite3.connect(state_db)
                db.text_factory = str

                # FTS5 全文搜索最近30天的会话
                cutoff = _time.time() - 2592000
                query_safe = query.replace("%", "\\%").replace("_", "\\_")
                rows = db.execute(
                    """SELECT m.session_id, m.content, m.role, m.timestamp,
                              s.source, s.title
                       FROM messages m
                       JOIN sessions s ON m.session_id = s.id
                       WHERE m.content LIKE ?
                         AND m.role IN ('user', 'assistant')
                         AND m.timestamp > ?
                         AND s.message_count >= 2
                       ORDER BY m.timestamp DESC
                       LIMIT ?""",
                    (f"%{query_safe}%", cutoff, top_k)
                ).fetchall()

                seen = set()
                for sid, content, role, ts, source, title in rows:
                    if sid in seen:
                        continue
                    seen.add(sid)
                    result["sessions"].append({
                        "session_id": sid,
                        "source": source,
                        "title": title or "",
                        "snippet": str(content)[:200] if content else "",
                        "timestamp": ts,
                        "role": role,
                    })

                db.close()
        except Exception:
            pass

    return result


def get_recent(n: int = 10):
    """
    获取最近的 n 条记忆。
    返回：
        [{"id": str, "content": str, "metadata": dict}, ...]
    """
    client = _get_client()
    ef = _get_embedding_function()
    col = client.get_or_create_collection(COLLECTION_NAME, embedding_function=ef)

    if col.count() == 0:
        return []

    results = col.get(limit=n)
    output = []
    if results["ids"]:
        for i in range(len(results["ids"])):
            doc = (results["documents"][i]
                   if results.get("documents") and results["documents"][i]
                   else "")
            meta = (results["metadatas"][i]
                    if results.get("metadatas") and results["metadatas"][i]
                    else {})
            output.append({
                "id": results["ids"][i],
                "content": doc,
                "metadata": meta,
            })
    return output


def get_total_count() -> int:
    """获取记忆库总条数"""
    client = _get_client()
    ef = _get_embedding_function()
    col = client.get_or_create_collection(COLLECTION_NAME, embedding_function=ef)
    return col.count()


def delete_memory(memory_id: str):
    """删除一条记忆"""
    client = _get_client()
    ef = _get_embedding_function()
    col = client.get_or_create_collection(COLLECTION_NAME, embedding_function=ef)
    col.delete(ids=[memory_id])


def delete_memories(ids: list):
    """批量删除记忆（分批，每批 100 条）"""
    if not ids:
        return
    client = _get_client()
    ef = _get_embedding_function()
    col = client.get_or_create_collection(COLLECTION_NAME, embedding_function=ef)
    BATCH = 100
    for i in range(0, len(ids), BATCH):
        batch = ids[i:i + BATCH]
        try:
            col.delete(ids=batch)
        except Exception:
            pass


def deprecate_memory(memory_id: str, superseded_by: str = None):
    """
    将一条记忆标记为非最新版本（不可变日志模式）。
    参数：
        memory_id: 要折旧的记忆 ID
        superseded_by: 替代它的新记忆 ID（可选，用于追溯链）
    返回：
        True 成功 / False 未找到
    """
    client = _get_client()
    ef = _get_embedding_function()
    col = client.get_or_create_collection(COLLECTION_NAME, embedding_function=ef)

    try:
        existing = col.get(ids=[memory_id])
        if not existing["metadatas"] or not existing["metadatas"][0]:
            return False

        new_meta = dict(existing["metadatas"][0])
        new_meta["isLatest"] = False
        new_meta["_deprecated_at"] = _time.time()
        if superseded_by:
            new_meta["_superseded_by"] = superseded_by

        col.update(ids=[memory_id], metadatas=[new_meta])
        return True
    except Exception:
        return False


def update_memory(memory_id: str, new_content: str, new_metadata: dict = None) -> str:
    """
    版本化更新记忆（不可变日志 — 不覆盖旧数据）。
    1. 将旧版本标记 isLatest=false
    2. 写入新版本（新 ID，isLatest=true，带 parent_id 追溯）
    参数：
        memory_id: 旧记忆 ID
        new_content: 新内容
        new_metadata: 新元数据（可选）
    返回：
        新记忆的 ID
    """
    # 1. 标记旧版本
    new_id = str(uuid.uuid4())
    ok = deprecate_memory(memory_id, superseded_by=new_id)

    # 2. 写入新版本，带追溯链
    meta = _normalize_metadata(new_metadata or {})
    meta["isLatest"] = True
    meta["_version_created"] = _time.time()
    if ok:
        meta["_parent_id"] = memory_id

    add_memory(
        content=new_content,
        metadata=meta,
        memory_id=new_id
    )
    return new_id


# ═══════ P2 — Succmemory 借鉴：矛盾消解 + 自动遗忘 ═══════

def auto_forget(dry_run: bool = False) -> dict:
    """
    自动遗忘 — 扫描所有设置了 forget_after（时间过期标记）的记忆。
    将已过期的条目标记 isLatest=false，记录遗忘原因。
    参数：
        dry_run: 如果 True，只返回统计不实际修改
    返回：
        {"forgotten": N, "skipped": N, "total_scanned": N, "dry_run": bool}
    """
    client = _get_client()
    ef = _get_embedding_function()
    col = client.get_or_create_collection(COLLECTION_NAME, embedding_function=ef)

    if col.count() == 0:
        return {"forgotten": 0, "skipped": 0, "total_scanned": 0, "dry_run": dry_run}

    now = _time.time()
    forgotten = 0
    skipped = 0
    total_scanned = 0

    # 分批获取（避免一次加载 11k 条）
    # CN Desktop 污染：metadata 损坏时 col.get 也会 InternalError
    batch_size = 500
    offset = 0

    while offset < col.count():
        try:
            results = col.get(limit=batch_size, offset=offset,
                              include=["metadatas", "documents"])
        except Exception:
            # 跳过损坏批次，继续下一批
            offset += batch_size
            continue

        if not results["ids"]:
            break

        for i in range(len(results["ids"])):
            total_scanned += 1
            meta = results["metadatas"][i] if results.get("metadatas") and results["metadatas"][i] else {}
            mem_id = results["ids"][i]

            # 检查 forget_after
            forget_after = meta.get("forget_after")
            if forget_after is None:
                skipped += 1
                continue

            try:
                if isinstance(forget_after, (int, float)):
                    expiry = float(forget_after)
                else:
                    expiry = float(forget_after)
            except (ValueError, TypeError):
                skipped += 1
                continue

            if now < expiry:
                skipped += 1
                continue

            # 已过期 → 折旧
            if not dry_run:
                reason = meta.get("forget_reason", f"Auto-forgot: expired at {expiry}")
                new_meta = dict(meta)
                new_meta["isLatest"] = False
                new_meta["isForgotten"] = True
                new_meta["forget_reason"] = reason
                new_meta["_forgotten_at"] = now
                col.update(ids=[mem_id], metadatas=[new_meta])

            forgotten += 1

        offset += batch_size

    return {"forgotten": forgotten, "skipped": skipped, "total_scanned": total_scanned, "dry_run": dry_run}


def detect_conflicts(query: str = "", top_k: int = 10,
                     threshold: float = 0.15) -> list:
    """
    矛盾检测 — 找出语义相似度极高（distance < threshold）但可能冲突的记忆对。
    在不可变日志中，冲突通过版本链（updates 关系）解决。
    但在自动遗忘层面，如果两条记忆 isLatest=true 且相似度极高，
    需要提醒上层做矛盾消解。

    参数：
        query: 查询文本（空串则遍历热区记忆）
        top_k: 返回条数
        threshold: 距离阈值（越小越相似，默认 0.15）
    返回：
        [{"memory_a": {...}, "memory_b": {...}, "distance": float}, ...]
    """
    memories = search_memory(
        query or "关键事实 偏好 决定 任务",
        top_k=top_k * 3,
        all_versions=False  # 只看最新版本
    )

    if len(memories) < 2:
        return []

    conflicts = []
    seen_pairs = set()

    for i in range(len(memories)):
        for j in range(i + 1, len(memories)):
            id_pair = tuple(sorted([memories[i]["id"], memories[j]["id"]]))
            if id_pair in seen_pairs:
                continue
            seen_pairs.add(id_pair)

            # 计算内容相似度（用 ChromaDB 的交叉查询）
            # 简化：通过搜索 j 的内容查 i 的 score
            try:
                cross = search_memory(
                    memories[j]["content"][:200], top_k=1,
                    where={"isLatest": True}, all_versions=False
                )
                if cross and cross[0]["id"] in id_pair:
                    dist = cross[0].get("score", 1.0)
                    # distance < threshold 意味着非常相似
                    if dist < threshold:
                        a_type = memories[i].get("metadata", {}).get("type", "unknown")
                        b_type = memories[j].get("metadata", {}).get("type", "unknown")
                        # 同类记忆 + 极高相似度 = 潜在冲突
                        if a_type == b_type:
                            conflicts.append({
                                "memory_a": {"id": memories[i]["id"], "content": memories[i]["content"][:100], "type": a_type},
                                "memory_b": {"id": memories[j]["id"], "content": memories[j]["content"][:100], "type": b_type},
                                "distance": dist,
                            })
            except Exception:
                pass

    conflicts.sort(key=lambda x: x["distance"])
    return conflicts[:top_k]


def resolve_conflict(winner_id: str, loser_id: str, reason: str = "") -> bool:
    """
    矛盾消解 — 将冲突记忆中的一条标记为 winner（保持 isLatest=true），
    另一条通过 update_memory 更新，建立 updates 关系链。
    参数：
        winner_id: 胜出的记忆 ID（保持有效）
        loser_id: 被取代的记忆 ID（通过 update_memory 折旧）
        reason: 消解原因
    返回：
        True 如果消解成功
    """
    try:
        client = _get_client()
        ef = _get_embedding_function()
        col = client.get_or_create_collection(COLLECTION_NAME, embedding_function=ef)

        # 获取 loser 信息
        loser_data = col.get(ids=[loser_id])
        if not loser_data["metadatas"] or not loser_data["metadatas"][0]:
            return False

        loser_meta = dict(loser_data["metadatas"][0])

        # 版本化更新 loser → 标记 isLatest=false, _superseded_by=winner_id
        new_meta = loser_meta.copy()
        new_meta["isLatest"] = False
        new_meta["_superseded_by"] = winner_id
        new_meta["_conflict_resolved_at"] = _time.time()
        if reason:
            new_meta["_conflict_reason"] = reason
        col.update(ids=[loser_id], metadatas=[new_meta])

        return True
    except Exception:
        return False