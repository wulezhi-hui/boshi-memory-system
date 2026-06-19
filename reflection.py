"""
伯仕自省引擎 v2 — 三省数据后端

触发时机：三省（午/晚/夜）运行前自动调用
功能：
1. 桌面健康检查（v1 保留）
2. 追踪近期用户纠正（记忆中的消极反馈）
3. 追踪待处理的升级点（work_log / memory 中的问题）
4. 扫描重复模式（同一话题被反复纠正）
5. 输出结构化报告 → 三省 LLM 消费
"""

import os, sys, json, re
from datetime import datetime, timedelta

BASE = os.path.expanduser("~/.boshi/memory")

# ─── 路径定义 ───
REFLECTIONS_DIR = os.path.join(BASE, "reflections")
HOT_FILE = os.path.join(BASE, "hot_topics.json")

os.makedirs(REFLECTIONS_DIR, exist_ok=True)


# ═══════════════════════════════════════════════
# 1.  桌面健康检查（v1 保留）
# ═══════════════════════════════════════════════

def collect_desktop() -> dict:
    """桌面项目分布 + 健康告警"""
    result = {"central": 0, "side": 0, "corner": 0, "total": 0, "alerts": []}
    if not os.path.exists(HOT_FILE):
        return result
    try:
        with open(HOT_FILE, encoding="utf-8") as f:
            desk = json.load(f)
        result["central"] = len(desk.get("central", []))
        result["side"] = len(desk.get("side", []))
        result["corner"] = len(desk.get("corner", []))
        result["total"] = result["central"] + result["side"] + result["corner"]

        if result["central"] > 5:
            result["alerts"].append(f"中央项目过多({result['central']}个)，注意分散注意力")
        if result["total"] > 20:
            result["alerts"].append(f"桌面总项目({result['total']}个)超载，建议归档旧话题")
        if result["central"] == 0 and result["side"] == 0:
            result["alerts"].append("桌面中央和旁边都空着，似乎没有活跃项目")
    except Exception as e:
        result["alerts"].append(f"读取桌面异常: {e}")
    return result


# ═══════════════════════════════════════════════
# 2.  近期自省记录摘要
# ═══════════════════════════════════════════════

def collect_reflection_history(days: int = 3) -> list:
    """最近 N 天的自省日志摘要"""
    entries = []
    for fname in sorted(os.listdir(REFLECTIONS_DIR), reverse=True):
        if not fname.endswith(".json"):
            continue
        date_str = fname.replace(".json", "")
        try:
            date = datetime.strptime(date_str, "%Y-%m-%d")
        except:
            continue
        if (datetime.now() - date).days > days:
            continue
        fpath = os.path.join(REFLECTIONS_DIR, fname)
        try:
            with open(fpath, encoding="utf-8") as f:
                logs = json.load(f)
        except:
            continue
        for log in (logs if isinstance(logs, list) else [logs]):
            entries.append({
                "date": date_str,
                "time": log.get("timestamp", ""),
                "issues": log.get("issues", []),
                "total_projects": log.get("total_projects", 0),
                "newly_archived": log.get("newly_archived", 0),
            })
    return entries


# ═══════════════════════════════════════════════
# 3.  追踪用户纠正记录
# ═══════════════════════════════════════════════

# 存放纠正记录的文件
CORRECTIONS_FILE = os.path.join(BASE, "corrections.json")


def load_corrections() -> dict:
    """加载纠正记录"""
    if os.path.exists(CORRECTIONS_FILE):
        try:
            with open(CORRECTIONS_FILE, encoding="utf-8") as f:
                return json.load(f)
        except:
            pass
    return {"entries": [], "last_updated": ""}


def save_corrections(data: dict):
    """保存纠正记录"""
    data["last_updated"] = datetime.now().isoformat()
    os.makedirs(os.path.dirname(CORRECTIONS_FILE), exist_ok=True)
    with open(CORRECTIONS_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def add_correction(topic: str, detail: str, source: str = "对话"):
    """记录一次用户纠正（三省或主动调用）"""
    data = load_corrections()
    data["entries"].append({
        "topic": topic,
        "detail": detail,
        "source": source,
        "timestamp": datetime.now().isoformat(),
        "fixed": False,
        "fix_note": "",
    })
    save_corrections(data)


def collect_corrections(days: int = 7) -> dict:
    """最近 N 天的纠正统计"""
    data = load_corrections()
    now = datetime.now()
    recent = []
    for e in data.get("entries", []):
        ts = e.get("timestamp", "")
        try:
            t = datetime.fromisoformat(ts)
        except:
            continue
        if (now - t).days <= days:
            recent.append(e)

    # 按话题聚类
    topic_counts = {}
    for e in recent:
        t = e.get("topic", "其他")
        topic_counts[t] = topic_counts.get(t, 0) + 1

    # 找出高频话题（重复纠正 ≥2次）
    repeated = {t: c for t, c in topic_counts.items() if c >= 2}

    # 未修复的
    unfixed = [e for e in recent if not e.get("fixed")]

    return {
        "total_recent": len(recent),
        "by_topic": topic_counts,
        "repeated_topics": repeated,
        "unfixed_count": len(unfixed),
        "unfixed": unfixed[:5],  # 最多返回5条
        "has_repeated_issues": len(repeated) > 0,
    }


# ═══════════════════════════════════════════════
# 4.  扫描工作日志中的待处理问题
# ═══════════════════════════════════════════════

def collect_pending_issues() -> list:
    """从改进日志目录扫描待处理项"""
    issues = []
    log_dir = os.path.join(BASE, "改进日志")
    if not os.path.isdir(log_dir):
        return issues

    for fname in os.listdir(log_dir):
        if not fname.endswith(".md"):
            continue
        fpath = os.path.join(log_dir, fname)
        try:
            with open(fpath, encoding="utf-8") as f:
                content = f.read()
        except:
            continue
        # 扫描 "待办"、"未解决"、"TODO" 等关键词
        for keyword in ["待办", "未解决", "TODO", "待处理", "遗留"]:
            for line in content.split("\n"):
                if keyword in line and len(line.strip()) > 3:
                    issues.append({
                        "file": fname,
                        "line": line.strip(),
                        "keyword": keyword,
                    })
    return issues


# ═══════════════════════════════════════════════
# 5.  整体报告生成
# ═══════════════════════════════════════════════

def generate_report() -> dict:
    """生成三省数据报告"""
    report = {
        "timestamp": datetime.now().isoformat(),
        "summary": "",
        "sections": {
            "desktop": collect_desktop(),
            "reflections": collect_reflection_history(days=3),
            "corrections": collect_corrections(days=7),
            "pending": collect_pending_issues(),
        },
    }

    # ─── 自动判断三省焦点 ───
    alerts = []
    corrections = report["sections"]["corrections"]
    desktop = report["sections"]["desktop"]

    # 优先级 1：重复纠正
    if corrections["has_repeated_issues"]:
        topics_str = "、".join(corrections["repeated_topics"].keys())
        alerts.append(f"⚠️ 注意：存在重复纠正话题——{topics_str}，三省请重点检讨")

    # 优先级 2：未修复问题
    if corrections["unfixed_count"] > 0:
        alerts.append(f"📌 有 {corrections['unfixed_count']} 条用户纠正尚未标记修复，三省请确认是否已解决")

    # 优先级 3：桌面健康
    alerts.extend(desktop["alerts"])

    # 优先级 4：待处理项
    pending_count = len(report["sections"]["pending"])
    if pending_count > 0:
        alerts.append(f"🗂️ 改进日志中有 {pending_count} 个待处理项，三省可复查是否需要推进")

    if not alerts:
        alerts.append("✅ 状态正常，本次三省无重点关注项")

    report["alerts"] = alerts
    report["summary"] = "\n".join(alerts)

    # 保存本次报告
    today = datetime.now().strftime("%Y-%m-%d")
    report_path = os.path.join(REFLECTIONS_DIR, f"report-{today}.json")
    existing = []
    if os.path.exists(report_path):
        try:
            with open(report_path, encoding="utf-8") as f:
                existing = json.load(f)
        except:
            pass
    if not isinstance(existing, list):
        existing = [existing] if existing else []
    existing.append(report)
    with open(report_path, "w", encoding="utf-8") as f:
        json.dump(existing, f, ensure_ascii=False, indent=2)

    return report


# ═══════════════════════════════════════════════
# CLI 入口
# ═══════════════════════════════════════════════

def cmd_report():
    """三省触发：生成完整数据报告"""
    r = generate_report()
    print("=" * 56)
    print("  伯仕自省引擎 v2 — 三省数据报告")
    print("=" * 56)
    print(f"\n📊 桌面状态：中央{r['sections']['desktop']['central']} 旁边{r['sections']['desktop']['side']} 角落{r['sections']['desktop']['corner']} 共{r['sections']['desktop']['total']}")
    print(f"\n🔍 近期纠正：{r['sections']['corrections']['total_recent']}次（最近7天）")
    if r['sections']['corrections']['repeated_topics']:
        print(f"⚠️  重复纠正：{r['sections']['corrections']['repeated_topics']}")
    if r['sections']['corrections']['unfixed_count']:
        print(f"📌 未修复：{r['sections']['corrections']['unfixed_count']}条")
    print(f"\n🗂️  待处理项：{len(r['sections']['pending'])}条")
    print(f"\n💡 三省关注点：")
    for a in r['alerts']:
        print(f"   {a}")
    print()
    print("报告已保存到 reflections/")


def cmd_add_correction():
    """手动添加纠正记录"""
    if len(sys.argv) < 4:
        print("用法: python reflection.py add <话题> <详情>")
        sys.exit(1)
    topic = sys.argv[2]
    detail = " ".join(sys.argv[3:])
    add_correction(topic, detail)
    print(f"✅ 已记录纠正：{topic} — {detail}")


def cmd_list_corrections():
    """列出近期纠正"""
    data = load_corrections()
    if not data["entries"]:
        print("暂无纠正记录")
        return
    print(f"纠正记录共 {len(data['entries'])} 条:\n")
    for i, e in enumerate(data["entries"][-10:], 1):
        fixed = "✅" if e.get("fixed") else "⬜"
        ts = e.get("timestamp", "")[:16]
        print(f"  {i}. {fixed} [{ts}] {e['topic']} — {e['detail'][:60]}")
        if e.get("fix_note"):
            print(f"     修复: {e['fix_note'][:60]}")


def cmd_fix_correction():
    """标记纠正已修复"""
    data = load_corrections()
    if len(sys.argv) < 3:
        print("用法: python reflection.py fix <关键词> [修复说明]")
        sys.exit(1)
    keyword = sys.argv[2]
    note = " ".join(sys.argv[3:]) if len(sys.argv) > 3 else "已处理"
    count = 0
    for e in data["entries"]:
        if not e.get("fixed") and keyword.lower() in e["topic"].lower():
            e["fixed"] = True
            e["fix_note"] = note
            count += 1
    if count > 0:
        save_corrections(data)
        print(f"✅ 标记了 {count} 条纠正为已修复")
    else:
        print(f"未找到匹配 '{keyword}' 的未修复纠正")


if __name__ == "__main__":
    if len(sys.argv) > 1:
        cmd = sys.argv[1]
        if cmd == "report":
            cmd_report()
        elif cmd == "add":
            cmd_add_correction()
        elif cmd == "list":
            cmd_list_corrections()
        elif cmd == "fix":
            cmd_fix_correction()
        else:
            print(f"未知命令: {cmd}")
            print("可用命令: report, add, list, fix")
    else:
        # 默认：显示近期摘要
        r = generate_report()
        print(f"伯仕自省引擎 v2 | {r['timestamp'][:16]}")
        print(f"桌面: {r['sections']['desktop']['total']}项目 | 纠正: {r['sections']['corrections']['total_recent']}次/7天 | 待处理: {len(r['sections']['pending'])}项")
        for a in r['alerts']:
            print(f"  {a}")
