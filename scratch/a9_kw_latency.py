"""A9 纯中文 query 的关键词通道延迟探针（2026-10-21）。

问题：放开全部 bigram 后 query 侧 token 数从 ~9 涨到 ~20+（_KW_MAX_TOKENS=64），
`_kw_channel` 每个 token 都要扫一遍 `_kw_index` 的 postings（进程内倒排，仍是
纯 Python 遍历），需要确认线上单次检索成本没有明显上涨。

用法：`./venv/Scripts/python.exe scratch/a9_kw_latency.py`
两种配置交替跑（交叉排序），各 3 轮，取中位数：
  - 旧窗口：BOSHI_KW_QBIG_LIMIT=6
  - 新默认：不设（全部 bigram）
注意：环境变量必须在新进程里读，故用 subprocess 各起一个进程，避免 import 缓存。
"""
import json
import os
import statistics
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)

RUNNER = r'''
import json, os, statistics, sys, time
sys.path.insert(0, r"{root}")
sys.path.insert(0, r"{root}/eval")
import pg_bridge
from cases_a9 import CASES

qs = [c[3] for c in CASES][:20]   # CASES: (kind, gold8, anchor, q)
pg_bridge.ensure_pg_running()
times = []
for rep in range(3):
    for q in qs:
        t0 = time.perf_counter()
        pg_bridge.search_memory(q, top_k=5, scope="all", fusion="auto")
        times.append((time.perf_counter() - t0) * 1000)
times.sort()
print(json.dumps({{
    "n": len(times),
    "median": round(statistics.median(times), 1),
    "mean": round(statistics.mean(times), 1),
    "p95": round(times[int(len(times) * 0.95) - 1], 1),
    "max": round(times[-1], 1),
    "tokens": len(pg_bridge._kw_tokens(qs[0])),
    "max_tokens": max(len(pg_bridge._kw_tokens(q)) for q in qs),
}}))
'''.format(root=ROOT.replace("\\", "/"))


def run(env_extra: dict, label: str) -> dict:
    env = dict(os.environ)
    env.update(env_extra)
    out = subprocess.run([os.path.join(ROOT, "venv", "Scripts", "python.exe"), "-c", RUNNER],
                         capture_output=True, text=True, env=env, cwd=ROOT)
    if out.returncode != 0:
        print(label, "FAILED\n", out.stderr[-2000:])
        sys.exit(1)
    d = json.loads(out.stdout.strip().splitlines()[-1])
    d["label"] = label
    return d


def main():
    runs = []
    cfgs = [({"BOSHI_KW_QBIG_LIMIT": "6"}, "旧窗口 6 bigram"),
            ({}, "新默认 全部 bigram")]
    for _ in range(2):                       # 交叉排序：A B B A，抵消冷启动
        for env_extra, label in (cfgs if _ == 0 else list(reversed(cfgs))):
            runs.append(run(env_extra, label))
    by = {}
    for r in runs:
        by.setdefault(r["label"], []).append(r)
    for label, rs in by.items():
        print(f"{label}: median {statistics.median([r['median'] for r in rs])}ms "
              f"(runs {[r['median'] for r in rs]}), p95 {max(r['p95'] for r in rs)}ms, "
              f"max_tokens {rs[0]['max_tokens']}")


if __name__ == "__main__":
    main()
