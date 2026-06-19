"""
伯仕对话初始化 — 每次对话开始自动执行
功能：
1. 加载桌面（热区）状态摘要
2. 执行沉降检查
3. 检查近期自省记录
"""

import os, sys

def get_desk_and_state() -> str:
    """生成对话初始化信息"""
    lines = ["🦄 伯仕启动状态："]
    
    # 桌面摘要
    sys.path.insert(0, os.path.expanduser("~/.boshi/memory"))
    try:
        from desktop import get_desk_summary, decay_check
        
        # 沉降检查
        shelf = decay_check()
        if shelf:
            lines.append(f"📦 {len(shelf)}个项目已自动归档到书架")
        
        # 桌面摘要
        lines.append(get_desk_summary())
    except Exception as e:
        lines.append(f"[桌面加载: {e}]")
    
    # 自省记录
    sys.path.insert(0, os.path.expanduser("~/.boshi"))
    try:
        from reflection import get_recent_reflections
        refl = get_recent_reflections(days=1)
        if "尚无" not in refl:
            lines.append(f"\n📋 今日自省：{refl}")
    except:
        pass
    
    return "\n".join(lines)


if __name__ == "__main__":
    print(get_desk_and_state())
