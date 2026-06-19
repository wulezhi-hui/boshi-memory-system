"""
伯仕 Hermes 升级恢复脚本
Hermes 升级后会覆盖：
  ① plugins/memory/boshi/  → 伯仕记忆系统代码
  ② run_agent.py           → 记忆prefetch/sync补丁
  ③ skills/                → 自定义skill文件
运行此脚本可一键恢复所有伯仕自定义内容。
用法: python restore_after_upgrade.py
"""

import os, shutil, sys, subprocess, hashlib, json
from datetime import datetime

# ── 路径配置 ──
HERMES_DIR = r"C:\Users\Administrator\AppData\Local\hermes"
AGENT_DIR = os.path.join(HERMES_DIR, "hermes-agent")
PLUGIN_DIR = os.path.join(AGENT_DIR, "plugins", "memory", "boshi")
RUN_AGENT = os.path.join(AGENT_DIR, "run_agent.py")
CONFIG = os.path.join(HERMES_DIR, "config.yaml")

BACKUP_DIR = os.path.expanduser("~/.boshi/hermes-backup")
SCRIPT_BACKUP = os.path.join(BACKUP_DIR, "scripts")
SKILL_BACKUP = os.path.join(BACKUP_DIR, "skills")

NOW = datetime.now().strftime('%Y%m%d_%H%M%S')
LOG = []

def log(msg):
    print(f"  {msg}")
    LOG.append(msg)

def ensure_dir(d):
    os.makedirs(d, exist_ok=True)

# ── 备份 ──
def backup_all():
    """备份所有伯仕自定义内容"""
    ensure_dir(BACKUP_DIR)
    ensure_dir(SCRIPT_BACKUP)
    ensure_dir(SKILL_BACKUP)
    
    # 1. 备份boshi插件
    if os.path.exists(PLUGIN_DIR):
        dst = os.path.join(BACKUP_DIR, "boshi_plugin")
        if os.path.exists(dst):
            shutil.rmtree(dst)
        shutil.copytree(PLUGIN_DIR, dst)
        log(f"✅ 备份boshi插件: {dst}")
    
    # 2. 备份run_agent.py
    if os.path.exists(RUN_AGENT):
        dst = os.path.join(BACKUP_DIR, f"run_agent.py")
        shutil.copy2(RUN_AGENT, dst)
        log(f"✅ 备份run_agent.py: {dst}")
    
    # 3. 备份config.yaml
    if os.path.exists(CONFIG):
        dst = os.path.join(BACKUP_DIR, f"config.yaml")
        shutil.copy2(CONFIG, dst)
        log(f"✅ 备份config.yaml: {dst}")
    
    # 4. 备份cron列表
    try:
        r = subprocess.run(["hermes", "cron", "list", "--json"],
                         capture_output=True, text=True, timeout=10)
        if r.stdout:
            with open(os.path.join(BACKUP_DIR, "cron_backup.json"), 'w') as f:
                f.write(r.stdout)
            log(f"✅ 备份cron列表")
    except:
        pass
    
    # 5. 备份用户自定义skill
    hermes_skills = os.path.join(HERMES_DIR, "skills")
    if os.path.exists(hermes_skills):
        for f in os.listdir(hermes_skills):
            fpath = os.path.join(hermes_skills, f)
            if os.path.isfile(fpath) or os.path.isdir(fpath):
                dst = os.path.join(SKILL_BACKUP, f)
                if os.path.isdir(fpath):
                    shutil.copytree(fpath, dst, dirs_exist_ok=True)
                else:
                    shutil.copy2(fpath, dst)
        log(f"✅ 备份skills: {SKILL_BACKUP}")
    
    print()

def check_health():
    """检查当前状态，标记需要恢复的项目"""
    log("🔍 检查伯仕组件完整性...")
    issues = []
    
    # 1. 检查boshi插件
    plugin_file = os.path.join(PLUGIN_DIR, "__init__.py")
    if not os.path.exists(plugin_file):
        issues.append(("plugin", "boshi插件丢失", f"还原到 {PLUGIN_DIR}"))
    else:
        with open(plugin_file) as f:
            content = f.read(500)
        if "伯仕" not in content and "boshi" not in content[:200]:
            issues.append(("plugin", "boshi插件被替换（非伯仕版本）", f"还原到 {PLUGIN_DIR}"))
        else:
            log(f"  ✅ boshi插件正常（v{content[30:50].strip()[:20] if 'v' in content[:100] else '?'}）")
    
    # 2. 检查run_agent.py
    if not os.path.exists(RUN_AGENT):
        issues.append(("runner", "run_agent.py丢失", f"还原到 {RUN_AGENT}"))
    else:
        with open(RUN_AGENT) as f:
            content = f.read(300000)  # 读前300KB足够涵盖queue_prefetch
        if "_memory_manager" not in content or "queue_prefetch" not in content:
            issues.append(("runner", "run_agent.py缺少记忆预加载功能（升级覆盖了）", f"还原到 {RUN_AGENT}"))
        else:
            log(f"  ✅ run_agent.py正常（含memory_manager）")
    
    # 3. 检查config.yaml
    if os.path.exists(CONFIG):
        with open(CONFIG) as f:
            content = f.read()
        checks = [
            ("provider: boshi", "memory.provider = boshi"),
            ("provider: deepseek", "provider = deepseek"),
            ("persona: 伯仕", "persona = 伯仕"),
            ("timezone: Asia/Shanghai", "timezone = Asia/Shanghai"),
            ("cron:", "cron 配置"),
        ]
        all_ok = True
        for keyword, desc in checks:
            if keyword not in content:
                issues.append(("config", f"config.yaml缺少{desc}", f"还原到 {CONFIG}"))
                all_ok = False
        if all_ok:
            log(f"  ✅ config.yaml正常")
    else:
        issues.append(("config", "config.yaml丢失", f"还原到 {CONFIG}"))
    
    if not issues:
        log(f"  🎉 全部正常！无需恢复")
    
    print()
    return issues

# ── 恢复 ──
def restore_plugin():
    """还原boshi插件"""
    src = os.path.join(BACKUP_DIR, "boshi_plugin")
    if not os.path.exists(src):
        log(f"  ⚠️ 备份不存在: {src}")
        return False
    if os.path.exists(PLUGIN_DIR):
        shutil.rmtree(PLUGIN_DIR)
    shutil.copytree(src, PLUGIN_DIR)
    log(f"  ✅ boshi插件已还原")
    return True

def restore_runner():
    """还原run_agent.py"""
    src = os.path.join(BACKUP_DIR, "run_agent.py")
    if not os.path.exists(src):
        log(f"  ⚠️ 备份不存在: {src}")
        return False
    shutil.copy2(src, RUN_AGENT)
    log(f"  ✅ run_agent.py已还原")
    return True

def restore_config():
    """还原config.yaml"""
    src = os.path.join(BACKUP_DIR, "config.yaml")
    if not os.path.exists(src):
        log(f"  ⚠️ 备份不存在: {src}")
        return False
    shutil.copy2(src, CONFIG)
    log(f"  ✅ config.yaml已还原")
    return True

def restore_all():
    """一键恢复所有"""
    log("🔄 开始恢复伯仕组件...")
    restore_plugin()
    restore_runner()
    restore_config()
    
    # 恢复后验证
    print()
    check_health()

# ── 主流程 ──
def main():
    print(f"\n{'='*50}")
    print(f"  伯仕 Hermes 升级恢复工具")
    print(f"  {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    print(f"{'='*50}\n")
    
    action = sys.argv[1] if len(sys.argv) > 1 else "check"
    
    if action == "backup":
        print("📦 开始备份伯仕组件...")
        backup_all()
        print(f"📦 备份完成 @ {BACKUP_DIR}")
    
    elif action == "check":
        issues = check_health()
        if issues:
            print(f"⚠️ 发现 {len(issues)} 个问题:")
            for typ, desc, fix in issues:
                print(f"  [{typ}] {desc}")
                print(f"        修复: {fix}")
            print(f"\n💡 运行 `~/.boshi/scripts/restore_after_upgrade.py restore` 一键恢复")
        else:
            log(f"  🎉 伯仕系统完整健康")
    
    elif action == "restore":
        restore_all()
    
    elif action == "full":
        # 先备份→再恢复（用于升级前/后各跑一次）
        backup_all()
        print(f"\n📋 备份后检查:")
        check_health()
    
    else:
        print(f"用法: python {__file__} [backup|check|restore|full]")
        print(f"  backup  - 备份所有伯仕组件到 {BACKUP_DIR}")
        print(f"  check   - 检查当前组件完整性（默认）")
        print(f"  restore - 从备份恢复所有组件")
        print(f"  full    - 先备份再检查（升级前跑）")

if __name__ == "__main__":
    main()
