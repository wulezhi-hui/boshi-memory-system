"""
伯仕用户画像模块 v1.0 — Succmemory 借鉴
=========================================
Static + Dynamic 两层画像自动维护

Static 层：用户长期稳定偏好/身份信息（type=profile_static）
Dynamic 层：近期活动上下文（type=profile_dynamic）

用法：
    from user_profile import get_profile, update_profile, format_for_prompt
    profile = get_profile("lezhi")
    prompt_text = format_for_prompt("lezhi")
"""

import os
import time
import json
from datetime import datetime
from typing import List, Optional


# ── 配置 ──────────────────────────────────────────────
PROFILE_CACHE_FILE = os.path.expanduser("~/.boshi/.profile_cache.json")
CACHE_TTL = 600  # 缓存10分钟
MAX_STATIC_ITEMS = 15
MAX_DYNAMIC_ITEMS = 10


def _load_cache() -> Optional[dict]:
    """加载缓存的画像（带 TTL）"""
    try:
        if not os.path.exists(PROFILE_CACHE_FILE):
            return None
        with open(PROFILE_CACHE_FILE, "r", encoding="utf-8") as f:
            cache = json.load(f)
        if time.time() - cache.get("_cached_at", 0) > CACHE_TTL:
            return None
        return cache
    except Exception:
        return None


def _save_cache(profile: dict) -> None:
    """写入缓存"""
    try:
        profile["_cached_at"] = time.time()
        os.makedirs(os.path.dirname(PROFILE_CACHE_FILE), exist_ok=True)
        with open(PROFILE_CACHE_FILE, "w", encoding="utf-8") as f:
            json.dump(profile, f, ensure_ascii=False, indent=2)
    except Exception:
        pass


def get_profile(user_id: str = "lezhi", force_refresh: bool = False) -> dict:
    """
    获取用户画像（优先走缓存）。
    返回：
        {
            "static": [{"content": ..., "confidence": ..., "source": ...}, ...],
            "dynamic": [{"content": ..., "updated_at": ..., "source": ...}, ...],
            "last_updated": timestamp
        }
    """
    if not force_refresh:
        cached = _load_cache()
        if cached:
            return {k: v for k, v in cached.items() if not k.startswith("_")}

    # 从 ChromaDB 查询
    try:
        from chroma_bridge import search_memory

        # Static 画像（长期稳定事实）
        static_results = search_memory(
            user_id, top_k=MAX_STATIC_ITEMS,
            where={"type": "profile_static"}
        )
        static = []
        for r in static_results:
            meta = r.get("metadata", {})
            static.append({
                "content": r.get("content", ""),
                "confidence": meta.get("confidence", 0.5),
                "source": meta.get("source", "auto"),
                "category": meta.get("category", ""),
            })

        # Dynamic 画像（近期活动）
        dynamic_results = search_memory(
            user_id, top_k=MAX_DYNAMIC_ITEMS,
            where={"type": "profile_dynamic"}
        )
        dynamic = []
        for r in dynamic_results:
            meta = r.get("metadata", {})
            dynamic.append({
                "content": r.get("content", ""),
                "updated_at": meta.get("updated_at", ""),
                "source": meta.get("source", "auto"),
                "heat": meta.get("heat", 0),
            })

        # 按热度排序 dynamic
        dynamic.sort(key=lambda x: x.get("heat", 0), reverse=True)

        profile = {
            "static": static,
            "dynamic": dynamic,
            "last_updated": time.time(),
        }
        _save_cache(profile)
        return profile

    except ImportError:
        return {"static": [], "dynamic": [], "last_updated": 0}
    except Exception:
        return {"static": [], "dynamic": [], "last_updated": 0}


def update_profile(user_id: str, facts: List[dict],
                   assistant_content: str = "") -> None:
    """
    自动更新用户画像（从 extract_facts + 对话内容中提取）。
    参数：
        user_id: 用户标识
        facts: extract_facts 的输出 [{content, type, source}, ...]
        assistant_content: 助理回复内容（用于交叉参考）
    """
    if not facts:
        return

    try:
        from chroma_bridge import add_memory, search_memory, update_memory

        now = datetime.now().isoformat()

        for fact in facts:
            ftype = fact.get("type", "")
            content = fact.get("content", "")
            if not content or len(content) < 5:
                continue

            # ── 分类到 Static 或 Dynamic ──
            if ftype in ("preference", "fact"):
                # 偏好和事实 → 静态画像
                profile_type = "profile_static"
                profile_meta = {
                    "type": profile_type,
                    "category": ftype,
                    "confidence": 0.7,
                    "source": "auto_extract",
                    "updated_at": now,
                    "tier": "warm",
                    "heat": 30.0,
                }
            elif ftype in ("task_status", "decision", "tech_find"):
                # 任务状态和决策 → 动态画像
                profile_type = "profile_dynamic"
                profile_meta = {
                    "type": profile_type,
                    "category": ftype,
                    "source": "auto_extract",
                    "updated_at": now,
                    "tier": "warm",
                    "heat": 25.0,
                }
            else:
                # unknown type → 都放 dynamic
                profile_type = "profile_dynamic"
                profile_meta = {
                    "type": profile_type,
                    "category": "unknown",
                    "source": "auto_extract",
                    "updated_at": now,
                    "tier": "warm",
                    "heat": 15.0,
                }

            # ── 去重 + 版本更新 ──
            existing = search_memory(content[:80], top_k=2,
                                     where={"type": profile_type})
            conflict = None
            for e in existing:
                if e.get("score", 0) < 0.4:
                    # 语义相似度够高，可能是更新了同一事实
                    conflict = e
                    break

            if conflict:
                # 已存在 → 版本化更新
                update_memory(
                    memory_id=conflict["id"],
                    new_content=content[:300],
                    new_metadata=profile_meta,
                )
            else:
                # 新事实 → 直接写入
                add_memory(
                    content=content[:300],
                    metadata=profile_meta,
                )

    except ImportError:
        pass
    except Exception:
        pass


def refresh_dynamic_profile(user_id: str) -> None:
    """
    刷新 dynamic 画像（降低旧条目的 heat，清理过期）。
    由热度衰减或 cron 调用。
    """
    try:
        from chroma_bridge import search_memory, deprecate_memory, add_memory

        dyn = search_memory(user_id, top_k=MAX_DYNAMIC_ITEMS + 10,
                            where={"type": "profile_dynamic"})
        cutoff = time.time() - 86400 * 7  # 7天前

        for item in dyn:
            meta = item.get("metadata", {})
            heat = meta.get("heat", 0)

            # 检查时间过期（_normalize_metadata 会将 ISO 字符串转为 float）
            updated_at = meta.get("updated_at", 0)
            if updated_at:
                try:
                    # 已经是 float（ChromaDB 返回）直接转；否则跳过
                    ts = float(updated_at) if isinstance(updated_at, (int, float)) else 0
                except (ValueError, AttributeError):
                    ts = 0
                if ts and ts < cutoff and heat < 10:
                    deprecate_memory(item["id"])
                    continue

            # 衰减热度
            new_heat = max(heat * 0.85, 1.0)
            if new_heat < 1.0:
                deprecate_memory(item["id"])
                continue

            # 更新热度
            new_meta = dict(meta)
            new_meta["heat"] = new_heat
            new_meta["last_decay"] = time.time()
            deprecate_memory(item["id"])
            add_memory(content=item["content"], metadata=new_meta)

    except ImportError:
        pass
    except Exception:
        pass


def format_for_prompt(user_id: str = "lezhi") -> str:
    """
    格式化画像为系统提示词注入文本。
    返回空串或 Markdown 文本。
    """
    profile = get_profile(user_id)

    parts = []

    if profile["static"]:
        parts.append("👤 **用户画像（Static — 长期偏好）**：")
        for item in profile["static"][:8]:
            conf = item.get("confidence", 0)
            conf_str = "✅" if conf >= 0.7 else "⚠️"
            parts.append(f"  {conf_str} {item['content'][:100]}")

    if profile["dynamic"]:
        parts.append("🔄 **用户动态（Dynamic — 近期活动）**：")
        for item in profile["dynamic"][:6]:
            heat = item.get("heat", 0)
            hot_str = "🔥" if heat >= 20 else "📋"
            parts.append(f"  {hot_str} {item['content'][:100]}")

    return "\n".join(parts) if parts else ""


def inject_to_context(user_id: str = "lezhi") -> str:
    """
    返回一个系统提示词注入块。
    与 format_for_prompt 相同，但包装成完整的 memory context block。
    """
    text = format_for_prompt(user_id)
    if not text:
        return ""
    return f"## 用户画像 (Supermemory-inspired v1.0) 🦄\\n{text}"


# ── 命令行测试 ─────────────────────────────────────────
if __name__ == "__main__":
    print("=== 伯仕用户画像 v1.0 ===")
    print(format_for_prompt("lezhi"))
    print("\n--- 强制刷新 ---")
    print(format_for_prompt("lezhi"))
