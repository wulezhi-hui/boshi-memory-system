"""跨进程「拉起锁」回归（2026-10-24 方案 A）。

为什么要有这条：pg_bridge 原先只有进程内冷却 _RERANK_LAST_SPAWN，多个 boshi_mcp_server
进程 + BuddhistRAG/rerank_client 互相看不见对方 → 同一秒各拉一个 llama-server 抢 11437。
本文件只测锁本身的语义，**不启动任何 llama**（拉起动作交给真机竞态脚本验）。
"""
import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pg_bridge as b  # noqa: E402


def _tmp(name):
    return os.path.join(tempfile.mkdtemp(prefix="boshi_rlock_"), name)


def test_first_acquire_writes_pid():
    p = _tmp(".rerank_spawn.lock")
    assert b._rerank_lock_acquire(p) is True
    assert os.path.exists(p)
    assert str(os.getpid()) in open(p, "rb").read().decode()
    b._rerank_lock_release(p)
    assert not os.path.exists(p)


def test_second_acquire_fails_while_held():
    """核心：持锁期间第二次抢必须失败（这就是挡住重复拉起的那一步）。"""
    p = _tmp(".rerank_spawn.lock")
    assert b._rerank_lock_acquire(p) is True
    try:
        assert b._rerank_lock_acquire(p) is False
        assert b._rerank_lock_acquire(p) is False   # 重复探测也不能把它变成 True
    finally:
        b._rerank_lock_release(p)
    assert b._rerank_lock_acquire(p) is True        # 释放后可再接管
    b._rerank_lock_release(p)


def test_stale_lock_is_taken_over():
    """持锁进程崩在半路 → 超过 max_age 后允许接管，不会永久卡死。"""
    p = _tmp(".rerank_spawn.lock")
    assert b._rerank_lock_acquire(p) is True
    old = time.time() - 99
    os.utime(p, (old, old))
    assert b._rerank_lock_acquire(p, max_age=60) is True
    b._rerank_lock_release(p)


def test_touch_extends_hold():
    """持锁期间续期，防止等就绪太久被别人当陈锁抢走。"""
    p = _tmp(".rerank_spawn.lock")
    assert b._rerank_lock_acquire(p) is True
    old = time.time() - 99
    os.utime(p, (old, old))
    b._rerank_lock_touch(p)
    assert time.time() - os.path.getmtime(p) < 5
    assert b._rerank_lock_acquire(p, max_age=60) is False
    b._rerank_lock_release(p)


def test_release_is_idempotent_and_lock_path_is_absolute():
    p = _tmp(".rerank_spawn.lock")
    b._rerank_lock_release(p)          # 不存在也不抛
    b._rerank_lock_release(p)
    assert os.path.isabs(b._RERANK_LOCK_FILE), "锁文件必须是绝对路径，两边才能对上"


def test_rerank_client_side_matches_semantics():
    """BuddhistRAG 侧镜像实现语义必须一致，且默认锁路径与这边同一个文件。"""
    rag = r"C:\Users\wulezhi\projects\BuddhistRAG"
    if not os.path.isdir(rag):
        return
    sys.path.insert(0, rag)
    try:
        import rerank_client as c
    except Exception:                  # noqa: BLE001  那边环境缺依赖时不阻断本仓测试
        return
    assert str(c.LOCK_FILE) == b._RERANK_LOCK_FILE, "两边默认锁文件必须同一个"
    p = str(_tmp(".rerank_spawn.lock"))
    assert c._lock_acquire(p) is True
    assert c._lock_acquire(p) is False          # 与 pg_bridge 一样：占用中抢不到
    old = time.time() - 99
    os.utime(p, (old, old))
    assert c._lock_acquire(p, max_age=60) is True   # 陈锁可接管
    c._lock_release(p)
    assert c._lock_acquire(p) is True
    c._lock_release(p)


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print("ok", name)
    print("all spawn-lock tests passed")
