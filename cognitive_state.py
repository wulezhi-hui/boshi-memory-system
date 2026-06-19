"""
伯仕认知状态 — 每次对话自动注入

加载桌面摘要 + 近期温区话题 + 记忆统计
注入到对话系统提示中，让伯仕知道自己处于什么状态
"""

import os, sys, json

def get_cognitive_state() -> str:
    """生成伯仕认知状态摘要"""
    lines = []
    lines.append("🦄 伯仕认知状态（自动注入）：")
    lines.append("")
    
    # 桌面（热区）
    sys.path.insert(0, os.path.expanduser("~/.boshi/memory"))
    try:
        from desktop import get_desk_summary, load_desk
        desk = load_desk()
        lines.append(get_desk_summary())
    except Exception as e:
        lines.append(f"[桌面: {e}]")
    
    # 记忆统计（通过 ChromaDB）
    try:
        sys.path.insert(0, os.path.expanduser("~/.boshi"))
        from chroma_bridge import count
        mem_count = count()
        lines.append(f"\n📚 书架（温区）：{mem_count} 条记忆")
    except Exception as e:
        lines.append(f"\n[书架: {e}]")
    
    # 记忆工具说明
    lines.append("")
    lines.append("🔧 记忆工具：")
    lines.append("  boshi_conclude  — 记一条新事实")
    lines.append("  boshi_search   — 查书架（语义）")
    lines.append("  boshi_excavate — 挖阁楼（深度）")
    lines.append("  session_search — 翻档案室全量")
    lines.append("")
    lines.append("📌 桌面操作：python ~/.boshi/memory/desktop.py [show|decay]")
    
    return "\n".join(lines)


if __name__ == "__main__":
    print(get_cognitive_state())
