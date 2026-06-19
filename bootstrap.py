"""
伯仕启动自检 — 每次对话自动加载桌面状态
"""

import sys, os, json
sys.path.insert(0, os.path.expanduser("~/.boshi/memory"))

try:
    from desktop import get_desk_summary, mention_project, decay_check
    summary = get_desk_summary()
    
    # 自动执行沉降检查
    shelf = decay_check()
    
    print("\n" + summary)
    if shelf:
        print(f"📦 {len(shelf)}个项目已自动归档到书架")
    print("━" * 40)
except Exception as e:
    print(f"[桌面加载跳过: {e}]")
