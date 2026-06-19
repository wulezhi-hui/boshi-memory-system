#!/usr/bin/env python3
"""
增量分析缓存系统 — 文件哈希 + git diff 判断是否需要重新分析
每次分析前调用 can_skip()，返回需要重新分析的文件列表。
"""
import json
import os
import hashlib
import subprocess
from pathlib import Path

CACHE_DIR = Path.home() / ".boshi" / "tools" / "code-analysis" / ".cache"
CACHE_FILE = CACHE_DIR / "analysis_cache.json"


def _load_cache():
    if CACHE_FILE.exists():
        try:
            with open(CACHE_FILE, encoding='utf-8') as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            return {"version": 2, "files": {}}
    return {"version": 2, "files": {}}


def _save_cache(cache):
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    with open(CACHE_FILE, 'w', encoding='utf-8') as f:
        json.dump(cache, f, ensure_ascii=False, indent=2)


def _file_hash(filepath):
    """计算文件 SHA256 哈希"""
    h = hashlib.sha256()
    with open(filepath, 'rb') as f:
        for chunk in iter(lambda: f.read(65536), b''):
            h.update(chunk)
    return h.hexdigest()[:16]  # 16位足够


def _git_changed_files(project_dir):
    """通过 git diff 获取变更文件列表"""
    try:
        result = subprocess.run(
            ["git", "diff", "--name-only", "HEAD"],
            capture_output=True, text=True, cwd=project_dir, timeout=10
        )
        if result.returncode == 0 and result.stdout.strip():
            return set(result.stdout.strip().split('\n'))
    except (subprocess.TimeoutExpired, FileNotFoundError):
        pass
    return set()


def _is_binary(filepath):
    """粗略判断是否二进制文件"""
    try:
        with open(filepath, 'rb') as f:
            return b'\0' in f.read(1024)
    except OSError:
        return True


def get_changed_files(project_dir, file_globs=("*.py",)):
    """返回需要分析的文件列表（增量判断）"""
    import glob
    project_dir = str(project_dir)
    cache = _load_cache()
    cache_files = cache.setdefault("files", {})

    # 收集所有目标文件
    all_files = []
    for pattern in file_globs:
        for f in glob.glob(os.path.join(project_dir, "**", pattern), recursive=True):
            if _is_binary(f):
                continue
            # 自动排除常见非源码目录
            skip_dirs = {"venv", ".venv", "__pycache__", ".git", "node_modules",
                         ".cache", ".ruff_cache", "dist", "build"}
            parts = Path(f).parts
            if any(s in parts for s in skip_dirs):
                continue
            all_files.append(f)

    git_changed = _git_changed_files(project_dir)
    changed = []
    skipped = 0

    for f in sorted(all_files):
        rel = os.path.relpath(f, project_dir)
        try:
            cur_hash = _file_hash(f)
        except OSError:
            changed.append(f)
            continue

        cached = cache_files.get(rel)
        if cached and cached.get("hash") == cur_hash and rel not in git_changed:
            skipped += 1
            continue

        changed.append(f)
        cache_files[rel] = {
            "hash": cur_hash,
            "last_analyzed": __import__('datetime').datetime.now().isoformat(),
        }

    _save_cache(cache)
    return changed, skipped, len(all_files)


def clear_cache():
    """清除缓存，下次全量分析"""
    if CACHE_FILE.exists():
        CACHE_FILE.unlink()
    return "缓存已清除，下次将全量分析"


if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1 and sys.argv[1] == "--clear":
        print(clear_cache())
    else:
        target = sys.argv[1] if len(sys.argv) > 1 else "."
        changed, skipped, total = get_changed_files(target)
        print(f"共 {total} 个文件：{len(changed)} 个需分析，{skipped} 个跳过（缓存命中）")
        for f in changed:
            print(f"  ⚡ {os.path.relpath(f, target)}")
