#!/usr/bin/env python3
"""经验日志 — 记录被纠正、成功经验，统一存在 ChromaDB 中"""
import sys, os, json
from datetime import datetime

sys.path.insert(0, os.path.expanduser("~/.boshi"))
from chroma_bridge import add_memory, search_memory

TYPE_CORRECTION = "experience_correction"
TYPE_SUCCESS = "experience_success"
TYPE_RULE_UPDATE = "experience_rule_update"

def log_correction(scene, what_was_wrong, corrected_to, lesson=""):
    """记录一次被纠正"""
    content = f"【纠正】{scene}\n错误: {what_was_wrong}\n纠正: {corrected_to}\n教训: {lesson}"
    meta = {
        "type": TYPE_CORRECTION,
        "scene": scene,
        "time": datetime.now().isoformat(),
        "status": "active"
    }
    add_memory(content, meta)
    print(f"✅ 已记录纠正: {scene}")

def log_success(scene, approach, why_worked=""):
    """记录一次成功经验"""
    content = f"【成功】{scene}\n做法: {approach}\n原因: {why_worked}"
    meta = {
        "type": TYPE_SUCCESS,
        "scene": scene,
        "time": datetime.now().isoformat(),
        "status": "active"
    }
    add_memory(content, meta)
    print(f"✅ 已记录成功经验: {scene}")

def log_rule_update(old_rule, new_rule, reason):
    """记录一次规则修改"""
    content = f"【规则更新】{reason}\n旧: {old_rule}\n新: {new_rule}"
    meta = {
        "type": TYPE_RULE_UPDATE,
        "reason": reason,
        "time": datetime.now().isoformat(),
        "status": "active"
    }
    add_memory(content, meta)
    print(f"✅ 已记录规则更新: {reason}")

def analyze():
    """分析经验日志，找出重复模式"""
    corrections = search_memory("", top_k=50, where={"type": TYPE_CORRECTION})
    
    if not corrections:
        return "暂无纠正记录"
    
    # 统计场景重复次数
    scenes = {}
    for c in corrections:
        s = c.get("metadata", {}).get("scene", "未知")
        scenes[s] = scenes.get(s, 0) + 1
    
    repeated = {s: n for s, n in scenes.items() if n >= 2}
    
    report = []
    if repeated:
        report.append("🔴 重复纠正的模式：")
        for s, n in repeated.items():
            report.append(f"  · {s}（被纠正 {n} 次）")
    else:
        report.append("✅ 无重复错误模式")
    
    report.append(f"\n📋 纠正总计: {len(corrections)} 条")
    return "\n".join(report)

if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "analyze":
        print(analyze())
    else:
        # 统计
        c = search_memory("", top_k=100, where={"type": TYPE_CORRECTION})
        s = search_memory("", top_k=100, where={"type": TYPE_SUCCESS})
        r = search_memory("", top_k=100, where={"type": TYPE_RULE_UPDATE})
        print(f"经验日志（存储在 ChromaDB）")
        print(f"  纠正记录: {len(c)} 条")
        print(f"  成功记录: {len(s)} 条")
        print(f"  规则变更: {len(r)} 条")
