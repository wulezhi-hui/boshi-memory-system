#!/usr/bin/env python3
"""
伯仕记忆完整性守护 (Memory Integrity Guardian) v1.0
================================================
职责：
1. 维护记忆系统文件的 checksum 清单
2. 检测未授权变更并告警
3. 在升级前后自动做快照/恢复
4. 整合到三省巡检流程

核心概念：
  - 授权变更：伯仕自我进化流程（遵循 self-evolution skill 的协议）
  - 未授权变更：其他任何来源的修改（Hermes升级、外部脚本、误操作等）
  - 严重级别：
    CRITICAL - 核心代码被修改（tiered_memory.py, chroma_bridge.py）
    WARNING  - 辅助文件被修改（auto_capture.py, knowledge_graph.py）
    INFO     - 数据文件变化（chroma.sqlite3 大小变化等）
"""

import hashlib
import json
import os
import time
import sys
from datetime import datetime, timezone

BOSHI_HOME = os.path.expanduser("~/.boshi")
MEMORY_DIR = os.path.join(BOSHI_HOME, "memory")
HERMES_PLUGIN = os.path.expanduser("~/.hermes/plugins/memory/boshi")
CHROMA_DIR = os.path.expanduser("~/.boshi/chroma_db")
MANIFEST_FILE = os.path.join(MEMORY_DIR, "integrity.json")
SNAPSHOT_DIR = os.path.join(BOSHI_HOME, "memory-snapshots")
LOG_FILE = os.path.join(MEMORY_DIR, "integrity.log")

# 监控范围定义
# CRITICAL: 核心记忆引擎，修改会直接影响记忆行为
CRITICAL_FILES = [
    ("tiered_memory.py", MEMORY_DIR),
    ("chroma_bridge.py", BOSHI_HOME),
    ("memory_bridge_api.py", BOSHI_HOME),
    ("__init__.py", os.path.join(HERMES_PLUGIN) if os.path.exists(HERMES_PLUGIN) else None),
]

# WARNING: 辅助模块，修改会影响记忆功能但不会致命
WARNING_FILES = [
    ("knowledge_graph.py", MEMORY_DIR),
    ("auto_capture.py", MEMORY_DIR),
    ("integrity_guard.py", MEMORY_DIR),
    ("memory/__init__.py", BOSHI_HOME),
]

# INFO: 关键配置和数据
INFO_MONITORS = [
    ("chroma.sqlite3", CHROMA_DIR),  # 只记录大小变化，不做全文件 checksum
    ("knowledge_graph.json", MEMORY_DIR),
    ("hot_topics.json", os.path.join(BOSHI_HOME, "desktop")),
]


def _sha256_file(filepath: str) -> str:
    """计算文件 SHA256"""
    if not os.path.exists(filepath):
        return None
    h = hashlib.sha256()
    with open(filepath, "rb") as f:
        # 大文件分块读取
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def _mtime(filepath: str) -> float:
    if os.path.exists(filepath):
        return os.path.getmtime(filepath)
    return 0


def _file_info(filepath: str) -> dict:
    if not os.path.exists(filepath):
        return {"exists": False}
    return {
        "exists": True,
        "checksum": _sha256_file(filepath),
        "size": os.path.getsize(filepath),
        "mtime": _mtime(filepath),
    }


def _now():
    return datetime.now(timezone.utc).isoformat()


# ===========================================================
#  Manifest Management
# ===========================================================

def _manifest_file_entries() -> list:
    """收集所有受监控文件的路径"""
    entries = []
    for name, base in CRITICAL_FILES + WARNING_FILES:
        if base is None:
            continue
        entries.append((name, base, "CRITICAL")) if (name, base) in CRITICAL_FILES else entries.append((name, base, "WARNING"))
    # Re-categorize properly
    entries = []
    for name, base in CRITICAL_FILES:
        if base is not None:
            entries.append((name, base, "CRITICAL"))
    for name, base in WARNING_FILES:
        if base is not None:
            entries.append((name, base, "WARNING"))
    return entries


def generate_manifest() -> dict:
    """生成当前完整 manifest"""
    files = {}
    for name, base, severity in _manifest_file_entries():
        path = os.path.join(base, name)
        info = _file_info(path)
        info["severity"] = severity
        files[f"{os.path.relpath(path, BOSHI_HOME)}"] = info

    # 数据文件只记录大小和 mtime
    for name, base in INFO_MONITORS:
        path = os.path.join(base, name)
        if os.path.exists(path):
            files[f"{os.path.relpath(path, BOSHI_HOME)}"] = {
                "exists": True,
                "size": os.path.getsize(path),
                "mtime": _mtime(path),
                "severity": "INFO",
                "checksum": None,  # 不计算 checksum（文件太大或频繁变动）
            }

    return {
        "version": 2,
        "generated_at": _now(),
        "boshi_version": "5.3.1",
        "files": files,
    }


def save_manifest(manifest: dict = None):
    """保存 manifest 到文件"""
    if manifest is None:
        manifest = generate_manifest()
    os.makedirs(MEMORY_DIR, exist_ok=True)
    with open(MANIFEST_FILE, "w", encoding="utf-8") as f:
        json.dump(manifest, f, ensure_ascii=False, indent=2)
    return manifest


def load_manifest() -> dict:
    """加载现有的 manifest，不存在则生成新的"""
    if os.path.exists(MANIFEST_FILE):
        with open(MANIFEST_FILE, encoding="utf-8") as f:
            return json.load(f)
    return None


# ===========================================================
#  Integrity Check
# ===========================================================

def _log(msg: str, level: str = "INFO"):
    """写入完整性日志"""
    os.makedirs(MEMORY_DIR, exist_ok=True)
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{timestamp}] [{level}] {msg}"
    with open(LOG_FILE, "a", encoding="utf-8") as f:
        f.write(line + "\n")
    # 同时输出到 stderr
    print(line, file=sys.stderr)


def check_integrity() -> list:
    """
    执行完整性检查，返回变更列表。
    返回: [{"file": str, "severity": str, "change": str, "old": dict, "new": dict}, ...]
    """
    previous = load_manifest()
    if not previous:
        _log("首次运行：生成基准 manifest", "INFO")
        save_manifest()
        return []

    current = generate_manifest()
    changes = []

    all_keys = set(list(previous.get("files", {}).keys()) + list(current.get("files", {}).keys()))

    for key in sorted(all_keys):
        old = previous.get("files", {}).get(key, {})
        new = current.get("files", {}).get(key, {})

        if not old.get("exists") and new.get("exists"):
            changes.append({"file": key, "severity": new.get("severity", "WARNING"),
                            "change": "新文件", "old": old, "new": new})
        elif old.get("exists") and not new.get("exists"):
            changes.append({"file": key, "severity": old.get("severity", "WARNING"),
                            "change": "文件缺失", "old": old, "new": new})
        elif old.get("exists") and new.get("exists"):
            # 对于 CRITICAL/WARNING 文件，比较 checksum
            if old.get("checksum") and new.get("checksum"):
                if old["checksum"] != new["checksum"]:
                    changes.append({"file": key, "severity": old.get("severity", "CRITICAL"),
                                    "change": "内容变更", "old": old, "new": new})
            # 对于 INFO 文件，比较大小
            elif old.get("size") != new.get("size"):
                changes.append({"file": key, "severity": "INFO",
                                "change": "大小变化", "old": old, "new": new})

    # 记录变更
    for c in changes:
        _log(f"{c['severity']}: {c['file']} - {c['change']}", c['severity'])

    if not changes:
        _log("完整性检查通过", "INFO")

    # 更新 manifest
    current["checked_at"] = _now()
    save_manifest(current)

    return changes


# ===========================================================
#  Snapshot & Recovery
# ===========================================================

def create_snapshot(label: str = "pre-upgrade"):
    """
    创建记忆系统快照（升级前的备份）
    快照位置: ~/.boshi/memory-snapshots/<label>-<timestamp>/
    """
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    snapshot_path = os.path.join(SNAPSHOT_DIR, f"{label}-{timestamp}")
    os.makedirs(snapshot_path, exist_ok=True)

    snapshot_data = {
        "label": label,
        "created_at": _now(),
        "files": [],
    }

    # 备份所有受监控的代码文件
    for name, base in CRITICAL_FILES + WARNING_FILES:
        if base is None:
            continue
        src = os.path.join(base, name)
        if os.path.exists(src):
            dst = os.path.join(snapshot_path, name)
            with open(src, "rb") as f_in:
                with open(dst, "wb") as f_out:
                    f_out.write(f_in.read())
            snapshot_data["files"].append({"name": name, "source": src, "checksum": _sha256_file(src)})

    # 备份 manifest
    if os.path.exists(MANIFEST_FILE):
        import shutil
        shutil.copy2(MANIFEST_FILE, os.path.join(snapshot_path, "integrity.json"))

    # 保存快照信息
    with open(os.path.join(snapshot_path, "snapshot_info.json"), "w", encoding="utf-8") as f:
        json.dump(snapshot_data, f, ensure_ascii=False, indent=2)

    _log(f"快照创建成功: {snapshot_path} ({len(snapshot_data['files'])} 个文件)", "INFO")
    return snapshot_path


def list_snapshots() -> list:
    """列出所有可用的快照"""
    if not os.path.exists(SNAPSHOT_DIR):
        return []
    snapshots = []
    for d in sorted(os.listdir(SNAPSHOT_DIR), reverse=True):
        info_file = os.path.join(SNAPSHOT_DIR, d, "snapshot_info.json")
        if os.path.exists(info_file):
            with open(info_file, encoding="utf-8") as f:
                info = json.load(f)
            snapshots.append({"dir": d, "label": info.get("label", "?"), "created_at": info.get("created_at", "?")})
    return snapshots


def restore_from_snapshot(snapshot_dir: str = None):
    """
    从最近的快照恢复记忆系统。
    如果不指定 snapshot_dir，则使用最新的快照。
    恢复内容：所有受监控的代码文件 + manifest
    """
    if snapshot_dir is None:
        snapshots = list_snapshots()
        if not snapshots:
            _log("没有可用的快照，无法恢复", "CRITICAL")
            return False
        snapshot_dir = os.path.join(SNAPSHOT_DIR, snapshots[0]["dir"])

    if not os.path.exists(snapshot_dir):
        _log(f"快照目录不存在: {snapshot_dir}", "CRITICAL")
        return False

    info_file = os.path.join(snapshot_dir, "snapshot_info.json")
    if not os.path.exists(info_file):
        _log(f"快照信息丢失: {info_file}", "CRITICAL")
        return False

    with open(info_file, encoding="utf-8") as f:
        info = json.load(f)

    restored = 0
    for fe in info.get("files", []):
        name = fe["name"]
        src = os.path.join(snapshot_dir, name)
        dst = fe["source"]
        if os.path.exists(src):
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            with open(src, "rb") as f_in:
                with open(dst, "wb") as f_out:
                    f_out.write(f_in.read())
            restored += 1
            _log(f"  已恢复: {dst}", "INFO")

    # 恢复 manifest
    manifest_src = os.path.join(snapshot_dir, "integrity.json")
    if os.path.exists(manifest_src):
        import shutil
        shutil.copy2(manifest_src, MANIFEST_FILE)
        _log(f"  manifest 已恢复", "INFO")

    _log(f"恢复完成: 从 {snapshot_dir} 恢复了 {restored} 个文件", "INFO")
    return True


# ===========================================================
#  CLI Entry Point (三省集成用)
# ===========================================================

def run_check():
    """三省巡检入口：执行完整性检查并返回结果摘要"""
    changes = check_integrity()

    if not changes:
        return {"status": "PASS", "changes": []}

    criticals = [c for c in changes if c["severity"] == "CRITICAL"]
    warnings = [c for c in changes if c["severity"] == "WARNING"]
    infos = [c for c in changes if c["severity"] == "INFO"]

    return {
        "status": "FAIL" if criticals else ("WARN" if warnings else "INFO"),
        "changes": changes,
        "summary": {
            "CRITICAL": len(criticals),
            "WARNING": len(warnings),
            "INFO": len(infos),
        }
    }


if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1:
        cmd = sys.argv[1]
        if cmd == "check":
            result = run_check()
            print(json.dumps(result, ensure_ascii=False, indent=2))
        elif cmd == "snapshot":
            label = sys.argv[2] if len(sys.argv) > 2 else "manual"
            path = create_snapshot(label)
            print(f"Snapshot: {path}")
        elif cmd == "restore":
            snapshot_dir = sys.argv[2] if len(sys.argv) > 2 else None
            ok = restore_from_snapshot(snapshot_dir)
            print(f"Restore: {'OK' if ok else 'FAILED'}")
        elif cmd == "manifest":
            manifest = save_manifest()
            print(json.dumps(manifest, ensure_ascii=False, indent=2))
        else:
            print(f"Unknown command: {cmd}")
    else:
        result = run_check()
        if result["status"] == "PASS":
            print("✅ 记忆系统完整性通过")
        else:
            print(f"⚠️ 记忆系统有 {result['summary']['CRITICAL']} 个严重 + {result['summary']['WARNING']} 个警告 + {result['summary']['INFO']} 个信息变更")
            for c in result["changes"]:
                print(f"  [{c['severity']}] {c['file']}: {c['change']}")
