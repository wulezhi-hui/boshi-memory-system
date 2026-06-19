#!/usr/bin/env python3
"""
伯仕记忆恢复工具 🦄
从 state.db 找回 v6.0 升级前的重要对话，重新写入 ChromaDB。
只恢复有实质内容的讨论，琐碎对话跳过。

用法：
  python3 ~/.boshi/boshi_restore_from_state.py          # dry-run 预览
  python3 ~/.boshi/boshi_restore_from_state.py --execute # 执行恢复
"""

import json
import logging
import os
import sqlite3
import sys
import time
from datetime import datetime

# ── 路径 ──

STATE_DB = os.path.join(
    os.environ.get('LOCALAPPDATA', r'C:\Users\Administrator\AppData\Local'),
    'hermes', 'state.db'
)
BOSHI_HOME = os.path.expanduser("~/.boshi")

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s"
)
logger = logging.getLogger("boshi_restore")

# ── 琐碎消息过滤 ──

TRIVIAL_MESSAGES = {
    "好的", "好", "是的", "对", "没错", "明白了", "收到",
    "继续", "接着搞", "开始吧", "动手", "可以", "行", "嗯",
    "谢谢", "ok", "okay", "好。", "好的。", "是的。", "明白",
    "知道了", "清楚", "没问题", "不错", "很好", "辛苦了",
    "谢谢", "感谢", "ok", "继续。", "开始", "动手吧", "执行",
    "做", "做吧", "好，开始", "好，继续", "可以，继续",
    "早上好", "晚上好", "下午好", "你好", "你好啊",
    "好好好", "好嘞", "没问题了", "搞定了", "完成了",
    "在吗", "在不在", "你好", "你好",
}

# 有意义的关键词（包含这些关键词的对话一定恢复）
IMPORTANT_KEYWORDS = [
    "决定", "方案", "架构", "结论", "选择",
    "规则", "策略", "设计", "规划", "路线",
    "推荐", "建议", "评估", "对比", "分析",
    "备份", "权限", "配置", "部署", "升级",
    "cost", "token", "模型", "provider", "配置",
    "bug", "错误", "修复", "问题", "异常",
    "工作台", "三友", "虚拟寺院", "书库", "知识库",
    "Supermemory", "记忆系统", "ChromaDB", "遗忘",
    "乐之", "小乐", "伯仕", "乐乐",
    "UE5", "Unreal", "引擎", "场景", "AI僧人",
    "PCG", "程序化", "古建", "中式建筑",
    "Windows", "ollama", "本地模型", "显存",
    "gateway", "profile", "分身", "策略",
]


def get_state_db() -> sqlite3.Connection:
    if not os.path.exists(STATE_DB):
        raise FileNotFoundError(f"state.db 不存在: {STATE_DB}")
    c = sqlite3.connect(STATE_DB)
    c.row_factory = sqlite3.Row
    return c


def is_trivial_message(text: str) -> bool:
    """判断是否为琐碎消息"""
    text = text.strip().lower()
    if len(text) < 4:
        return True
    for t in TRIVIAL_MESSAGES:
        if t.lower() in text:
            # 但如果同时包含重要关键词，不算琐碎
            for kw in IMPORTANT_KEYWORDS:
                if kw.lower() in text:
                    return False
            if text == t.lower() or text == t.lower() + "。":
                return True
    return False


def has_substance(content: str, role: str) -> bool:
    """判断对话内容是否有实质价值"""
    if not content or not content.strip():
        return False
    if role == "user" and is_trivial_message(content):
        return False
    if role == "assistant":
        # 助手回复太短（<15字）且不含关键信息
        if len(content.strip()) < 15 and not any(kw.lower() in content.lower() for kw in IMPORTANT_KEYWORDS):
            return False
    return True


def session_has_value(messages: list) -> bool:
    """判断一个会话是否有价值恢复"""
    if not messages:
        return False
    meaningful = 0
    total = 0
    for role, content in messages:
        if role != "user" and role != "assistant":
            continue
        total += 1
        if has_substance(content or "", role):
            meaningful += 1
        # 含重要关键词的直接标记
        if content and any(kw.lower() in content.lower() for kw in IMPORTANT_KEYWORDS):
            meaningful += 2  # 给重要关键词加权
    if total == 0:
        return False
    # 至少 30% 的内容有实质，或包含重要关键词
    score = meaningful / total if total > 0 else 0
    return score >= 0.3


# ── 主恢复逻辑 ──

def scan_valuable_sessions(cutoff_before: float, dry_run: bool = True) -> list:
    """
    扫描 state.db，找出有价值的会话。
    返回 [(session_id, title, started_at, user_msgs_str, assistant_msgs_str), ...]
    """
    db = get_state_db()
    cutoff_date = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(cutoff_before))
    logger.info(f"扫描 {cutoff_date} 之前的会话...")

    # 找出所有在截止日期前的会话
    sessions = db.execute(
        "SELECT id, title, started_at, message_count FROM sessions WHERE started_at < ? ORDER BY started_at ASC",
        (cutoff_before,)
    ).fetchall()
    logger.info(f"共 {len(sessions)} 个会话待检查")

    valuable = []
    skipped = {"trivial": 0, "too_short": 0, "empty": 0}

    for s in sessions:
        sid = s["id"]
        # Skip cron sessions - they are repetitive
        if sid.startswith("cron_"):
            skipped.setdefault("cron", 0)
            skipped["cron"] += 1
            continue

        title = s["title"] or ""
        msgs = db.execute(
            "SELECT role, content FROM messages WHERE session_id=? ORDER BY rowid ASC",
            (sid,)
        ).fetchall()

        if not msgs:
            skipped["empty"] += 1
            continue

        # 提取用户和助手的实质内容
        user_parts = []
        assistant_parts = []
        for role, content in msgs:
            if not content:
                continue
            if role == "user":
                if not is_trivial_message(content):
                    user_parts.append(content.strip()[:300])
            elif role == "assistant":
                if len(content.strip()) >= 15:
                    assistant_parts.append(content.strip()[:500])

        user_text = "\n".join(user_parts)
        assistant_text = "\n".join(assistant_parts)

        if not user_text and not assistant_text:
            skipped["trivial"] += 1
            continue

        if not user_text or len(user_text) < 10:
            skipped["too_short"] += 1
            continue

        valuable.append({
            "session_id": sid,
            "title": title,
            "started_at": s["started_at"],
            "user_text": user_text[:2000],
            "assistant_text": assistant_text[:2000],
            "total_messages": len(msgs),
            "user_count": sum(1 for r, c in msgs if r == "user" and c and not is_trivial_message(c)),
            "assistant_count": sum(1 for r, c in msgs if r == "assistant" and c and len(c.strip()) >= 15),
        })

    db.close()
    logger.info(f"检查完成：有价值 {len(valuable)} / 跳过 {sum(skipped.values())}")
    for k, v in skipped.items():
        if v > 0:
            logger.info(f"  跳过原因[{k}]: {v}")

    return valuable


def write_to_chroma(sessions: list):
    """将有价值的会话写入 ChromaDB"""
    sys.path.insert(0, BOSHI_HOME)
    try:
        from chroma_bridge import add_memory, add_memories_batch
    except ImportError:
        logger.error("无法导入 chroma_bridge")
        return 0, 0

    entries = []
    for s in sessions:
        ts = s["started_at"]
        topic = s["title"][:60] if s["title"] else f"对话恢复_{datetime.fromtimestamp(ts).strftime('%m-%d')}"

        # 恢复用户消息
        if s["user_text"]:
            entries.append({
                "content": s["user_text"],
                "metadata": {
                    "source": "conversation",
                    "session_id": s["session_id"],
                    "topic": topic,
                    "_version_created": ts,
                    "last_mentioned": ts,
                    "tier": "warm",
                    "type": "raw_conversation",
                    "isLatest": True,
                    "restored": True,
                },
            })

        # 恢复助手回复
        if s["assistant_text"]:
            entries.append({
                "content": f"伯仕: {s['assistant_text']}",
                "metadata": {
                    "source": "conversation",
                    "session_id": s["session_id"],
                    "topic": topic,
                    "_version_created": ts,
                    "last_mentioned": ts,
                    "tier": "warm",
                    "type": "conversation_turn",
                    "isLatest": True,
                    "restored": True,
                },
            })

    added = 0
    if entries:
        result = add_memories_batch(entries)
        added = result.get("added", 0)
        failed = result.get("failed", 0)
        logger.info(f"写入完成: +{added} / 失败 {failed}")
        return added, failed

    return 0, 0


def run_restore(dry_run: bool = True):
    """主流程"""
    # 截止日期：v6.0 升级前（6月10日）
    cutoff = time.mktime(time.strptime("2026-06-10", "%Y-%m-%d"))

    sessions = scan_valuable_sessions(cutoff, dry_run)

    if not sessions:
        print("✅ 未找到有价值的旧会话可恢复。")
        return

    if dry_run:
        print(f"\n📋 预览: 将恢复 {len(sessions)} 个会话")

        # 按时间分组
        date_groups = {}
        for s in sessions:
            dt = datetime.fromtimestamp(s["started_at"]).strftime("%m-%d")
            date_groups.setdefault(dt, 0)
            date_groups[dt] += 1

        print(f"\n  → 按日期分布：")
        for dt, cnt in sorted(date_groups.items()):
            print(f"    {dt}: {cnt} 个会话")

        print(f"\n  → 预览 TOP 10 会话：")
        for s in sessions[:10]:
            dt = datetime.fromtimestamp(s["started_at"]).strftime("%m-%d %H:%M")
            title = s["title"][:40] if s["title"] else "(无标题)"
            # 取用户消息第一句作为摘要
            first_line = s["user_text"].split("\n")[0][:60] if s["user_text"] else ""
            print(f"  [{dt}] {title}")
            print(f"     用户: {first_line}...")
            print(f"     消息: 用户{s['user_count']}+ 助手{s['assistant_count']} = {s['total_messages']}条")

        # 统计 token 估算
        total_chars = sum(len(s["user_text"]) + len(s["assistant_text"]) for s in sessions)
        est_tokens = total_chars * 1.5  # 中文约 1.5 tokens/字
        print(f"\n  → 数据量预估:")
        print(f"    内容长度: {total_chars:,} 字")
        print(f"    估算 tokens: ~{est_tokens:.0f}")
        print(f"    写入条目: {len(sessions) * 2} 条（用户+助手各一条）")
        print(f"\n🟡 DRY-RUN — 未写入。加 --execute 执行。")
    else:
        print(f"\n🧹 开始恢复 {len(sessions)} 个会话...")
        added, failed = write_to_chroma(sessions)
        print(f"✅ 恢复完成: +{added} 条 / 失败 {failed}")

        # 记录日志
        log_path = os.path.expanduser("~/.boshi/memory/restore_log.jsonl")
        os.makedirs(os.path.dirname(log_path), exist_ok=True)
        with open(log_path, "a", encoding="utf-8") as f:
            f.write(json.dumps({
                "timestamp": time.time(),
                "date": time.strftime("%Y-%m-%d %H:%M:%S"),
                "restored": added,
                "sessions": len(sessions),
            }, ensure_ascii=False) + "\n")


if __name__ == "__main__":
    dry = "--execute" not in sys.argv
    run_restore(dry_run=dry)