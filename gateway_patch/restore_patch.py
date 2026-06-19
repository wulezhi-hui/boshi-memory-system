#!/usr/bin/env python3
"""
伯仕汇聚模式补丁恢复脚本 🦄
================================
Hermes 升级后，如果 gateway/run.py 被覆盖，运行此脚本可重新打上汇聚广播补丁。

用法：
  python ~/.boshi/gateway_patch/restore_patch.py
"""

import os, sys, shutil, json

BOSHI_DIR = os.path.expanduser("~/.boshi")
PATCH_DIR = os.path.join(BOSHI_DIR, "gateway_patch")
HERMES_DIR = os.path.join(
    os.environ.get('LOCALAPPDATA', r'C:\Users\Administrator\AppData\Local'),
    'hermes', 'hermes-agent'
)

RUN_PY = os.path.join(HERMES_DIR, "gateway", "run.py")
ORIGINAL = os.path.join(PATCH_DIR, "run.py.original")
PATCHED = os.path.join(PATCH_DIR, "run.py.patched")
WORKSTATION = os.path.join(BOSHI_DIR, "workstation", "伯仕工作台.pyw")
WORKSTATION_PATCHED = os.path.join(PATCH_DIR, "伯仕工作台.pyw.patched")

def check_files():
    """检查所有必需的文件是否存在"""
    missing = []
    for name, path in [("run.py（当前）", RUN_PY), 
                       ("run.py（原件备份）", ORIGINAL),
                       ("run.py（改版备份）", PATCHED)]:
        if not os.path.exists(path):
            missing.append(name)
    return missing

def check_converge_mode():
    """检查汇聚模式标记文件"""
    converge_file = os.path.join(BOSHI_DIR, "memory", "converge_mode.json")
    if not os.path.exists(converge_file):
        with open(converge_file, 'w') as f:
            json.dump({"enabled": False, "updated_at": ""}, f)
        print("  ✅ 已创建汇聚模式标记文件（默认关闭）")
        return False
    with open(converge_file) as f:
        cfg = json.load(f)
    return cfg.get("enabled", False)

def apply_patch():
    """应用补丁"""
    missing = check_files()
    if missing:
        print(f"❌ 缺失文件: {', '.join(missing)}")
        return False

    # 备份当前 run.py（以防补丁前用户自己改过）
    backup_current = RUN_PY + ".bak"
    shutil.copy2(RUN_PY, backup_current)
    print(f"  ✅ 已备份当前 run.py → {backup_current}")

    # 检查当前 run.py 是否已经有汇聚补丁
    with open(RUN_PY) as f:
        content = f.read()
    if "_broadcast_to_all_platforms" in content:
        print("  ℹ️  当前 run.py 已有汇聚补丁，无需重新打")
        converge_on = check_converge_mode()
        status = "开启" if converge_on else "关闭"
        print(f"  ℹ️  汇聚模式当前: {status}")
        return True

    # 打补丁：替换已修改的部分
    with open(PATCHED) as f:
        patched_content = f.read()

    with open(RUN_PY, 'w', encoding='utf-8') as f:
        f.write(patched_content)
    print(f"  ✅ 汇聚广播补丁已应用到: {RUN_PY}")
    
    # 检查工作台是否需要更新
    if os.path.exists(WORKSTATION) and os.path.exists(WORKSTATION_PATCHED):
        with open(WORKSTATION) as f:
            ws_content = f.read()
        if "converge_mode.json" not in ws_content:
            shutil.copy2(WORKSTATION, WORKSTATION + ".bak")
            shutil.copy2(WORKSTATION_PATCHED, WORKSTATION)
            print(f"  ✅ 工作台补丁已应用")

    converge_on = check_converge_mode()
    status = "开启" if converge_on else "关闭"
    print(f"  ℹ️  汇聚模式当前: {status}")
    return True

def rollback():
    """回滚到原始版本"""
    if not os.path.exists(ORIGINAL):
        print("❌ 找不到原始备份")
        return False
    
    # 如果有 .bak 就删掉
    bak_file = RUN_PY + ".bak"
    if os.path.exists(bak_file):
        os.remove(bak_file)
    
    shutil.copy2(ORIGINAL, RUN_PY)
    print(f"  ✅ 已回滚到原始版本: {RUN_PY}")
    return True

def show_status():
    """显示当前状态"""
    print(f"\n🦄 伯仕汇聚模式补丁状态\n")
    print(f"  Hermes 目录: {HERMES_DIR}")
    print(f"  备份目录:    {PATCH_DIR}")
    print()
    
    missing = check_files()
    if missing:
        print(f"  ⚠️  缺失: {', '.join(missing)}")
    else:
        print(f"  ✅ 所有备份文件齐全")
    
    if os.path.exists(RUN_PY):
        with open(RUN_PY) as f:
            content = f.read()
        has_patch = "_broadcast_to_all_platforms" in content
        print(f"  {'✅' if has_patch else '❌'} 汇聚补丁: {'已安装' if has_patch else '未安装'}")
    
    converge_on = check_converge_mode()
    print(f"  {'🌐' if converge_on else '🔒'} 汇聚模式: {'开启' if converge_on else '关闭'}")
    print()
    print("  用法:")
    print("    python restore_patch.py         应用补丁")
    print("    python restore_patch.py rollback 回滚")
    print("    python restore_patch.py status   查看状态")


if __name__ == "__main__":
    if len(sys.argv) > 1:
        cmd = sys.argv[1]
        if cmd == "rollback":
            rollback()
        elif cmd == "status":
            show_status()
        else:
            print(f"未知命令: {cmd}")
            print("用法: restore_patch.py [rollback|status]")
    else:
        if apply_patch():
            print("\n✅ 补丁恢复完成，请重启 Gateway 生效:")
            print("   1. 关闭当前 Gateway")
            print("   2. 双击桌面「启动伯仕.vbs」")
            print("   3. 打开工作台 http://localhost:7681 切换 🌐 汇聚模式")
