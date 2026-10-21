# -*- coding: utf-8 -*-
"""伯仕记忆系统性能测试（交付验收用）

覆盖四层：
  1. 规模/环境   —— PG 服务、行数、索引、库大小
  2. 检索质量    —— A9(40) / A5(35) 的 top1/top3/top5
  3. 延迟        —— 四类 query × 三种 fusion 的 p50/p95/p99；写入延迟
  4. 端到端      —— 插件 prefetch 实际调用路径、CLI、并发吞吐

用法:
  ./venv/Scripts/python.exe eval/perf_bench.py [tag]
结果: ~/.boshi/logs/perf_<tag>.json  +  stdout 打印 markdown 表
"""
import json
import os
import statistics
import subprocess
import sys
import threading
import time

HERE = r"C:/Users/wulezhi/.boshi"
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, "eval"))
os.chdir(HERE)
import pg_bridge
from cases_a9 import CASES as A9
from cases import CASES as A5

tag = sys.argv[1] if len(sys.argv) > 1 else time.strftime("%m%d_%H%M")
PY = os.path.join(HERE, "venv", "Scripts", "python.exe")
out = {"tag": tag, "ts": time.strftime("%Y-%m-%d %H:%M:%S"), "parts": {}}


def pct(xs, p):
    if not xs:
        return None
    xs = sorted(xs)
    k = max(0, min(len(xs) - 1, int(round((p / 100.0) * (len(xs) - 1)))))
    return xs[k]


def stat(xs):
    return {
        "n": len(xs),
        "p50": round(pct(xs, 50), 1),
        "p95": round(pct(xs, 95), 1),
        "p99": round(pct(xs, 99), 1),
        "mean": round(statistics.mean(xs), 1),
        "max": round(max(xs), 1),
    }


# ---------------------------------------------------------------- 1. 规模
def part_env():
    c = pg_bridge._conn()
    d = {}
    with c.cursor() as cur:
        cur.execute("select count(*) from boshi.memories")
        d["rows_all"] = cur.fetchone()[0]
        cur.execute("select count(*) from boshi.memories where is_latest")
        d["rows_latest"] = cur.fetchone()[0]
        cur.execute("select count(*) from boshi.graph_edges")
        d["edges"] = cur.fetchone()[0]
        cur.execute("select count(*) from boshi.fm_stats")
        d["fm_stats"] = cur.fetchone()[0]
        cur.execute("select pg_size_pretty(pg_database_size('hermes_data')), "
                    "pg_size_pretty(pg_total_relation_size('boshi.memories'))")
        d["db_size"], d["memories_size"] = cur.fetchone()
        cur.execute("select count(distinct profile) from boshi.memories where is_latest")
        d["profiles"] = cur.fetchone()[0]
        cur.execute("select count(*) from boshi.memories where topic='session_fact'")
        d["fact_rows"] = cur.fetchone()[0]
    return d


# ---------------------------------------------------------------- 2. 质量
def part_quality():
    d = {}
    for name, script in (("a9", "a9_eval.py"), ("a5", "a5_eval.py")):
        t0 = time.time()
        env = dict(os.environ)
        if name == "a9":
            env["A9_ASOF"] = env.get("A9_ASOF", "1792461898")
            env["A9_SCOPE"] = "all"
        r = subprocess.run([PY, os.path.join("eval", script), f"perf_{tag}"],
                           cwd=HERE, env=env, capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=1800)
        el = time.time() - t0
        payload = os.path.join(HERE, "logs", f"{name}_perf_{tag}.json")
        rec = {"secs": round(el, 1), "rc": r.returncode}
        if os.path.exists(payload):
            with open(payload, encoding="utf-8") as f:
                j = json.load(f)
            rec["stat"] = j.get("stat") or j.get("summary")
        else:
            rec["tail"] = (r.stdout or "")[-800:] + (r.stderr or "")[-800:]
        d[name] = rec
    return d


# ---------------------------------------------------------------- 3. 延迟
CLASSES = {}
ov = [c[3] for c in A9 if c[0] == "overlap"]
pa = [c[3] for c in A9 if c[0] == "paraphrase"]
an = [c[0] for c in A5]
CLASSES["A_overlap"] = ov
CLASSES["B_paraphrase"] = pa
CLASSES["C_anchor"] = an
CLASSES["D_long"] = [(ov[i % len(ov)] + "，" + pa[(i + 3) % len(pa)]) for i in range(10)]


def part_latency(reps=3):
    d = {}
    for cls, qs in CLASSES.items():
        for fusion in ("auto", "vector", "score"):
            lat = []
            for _ in range(reps):
                for q in qs:
                    t0 = time.perf_counter()
                    pg_bridge.search_memory(q, top_k=5, scope="all", fusion=fusion)
                    lat.append((time.perf_counter() - t0) * 1000)
            d[f"{cls}|{fusion}"] = stat(lat)
    # 写入延迟（走真实路径 boshi_core.save：含归属打标 + 图谱实体抽取；写 perfbench 专属 profile，跑完删掉）
    import boshi_core
    w = []
    for i in range(12):
        t0 = time.perf_counter()
        r = boshi_core.save(
            f"perfbench 写入延迟样本 {tag} #{i}：这条记录仅用于性能测试，测完即删。",
            topic="perfbench", profile="perfbench")
        w.append((time.perf_counter() - t0) * 1000)
    d["write"] = stat(w)
    d["write_ok"] = sum(1 for x in w if x > 0)
    # 清理
    c = pg_bridge._conn()
    with c.cursor() as cur:
        cur.execute("delete from boshi.memories where profile='perfbench' and topic='perfbench'")
        d["write_cleaned"] = cur.rowcount
    c.commit()
    return d


# ---------------------------------------------------------------- 4. 端到端
def part_e2e():
    d = {}
    # 插件 prefetch 的实际检索调用（boshi_core.search，source=all）
    import boshi_core
    lat = []
    for _ in range(3):
        for q in CLASSES["B_paraphrase"][:8]:
            t0 = time.perf_counter()
            boshi_core.search(q, top_k=5, source="all")
            lat.append((time.perf_counter() - t0) * 1000)
    d["plugin_search"] = stat(lat)
    # 插件 prefetch 全路径（含会话历史 FTS + 扩展查询）—— 直接实例化会连 Hermes 配置，
    # 这里只测它的第二个检索段：会话历史 FTS
    try:
        with pg_bridge._conn() as c, c.cursor() as cur:
            t0 = time.perf_counter()
            cur.execute("select 1 from boshi.memories where is_latest and left(content, 1) like %s limit 5", ("%",))
            cur.fetchall()
            d["sanity_sql"] = round((time.perf_counter() - t0) * 1000, 1)
    except Exception as e:  # noqa: BLE001
        d["sanity_sql"] = f"ERR {e}"
    # 冷启动 CLI（进程启动 + 导入 + 一次检索）
    t0 = time.perf_counter()
    r = subprocess.run([PY, "boshi_cli.py", "search", pa[0], "--top-k", "5", "--scope", "all"],
                       cwd=HERE, capture_output=True, text=True, encoding="utf-8",
                       errors="replace", timeout=300)
    d["cli_cold_ms"] = round((time.perf_counter() - t0) * 1000, 1)
    d["cli_rc"] = r.returncode
    d["cli_has_hit"] = bool((r.stdout or "").strip())
    # 并发吞吐：4 线程 × 8 query
    qs = (ov[:8] + pa[:8])[:8]
    errs = []

    def worker(n):
        for i in range(8):
            try:
                pg_bridge.search_memory(qs[i % len(qs)], top_k=5, scope="all")
            except Exception as e:  # noqa: BLE001
                errs.append(repr(e))

    t0 = time.perf_counter()
    ths = [threading.Thread(target=worker, args=(i,)) for i in range(4)]
    [t.start() for t in ths]
    [t.join() for t in ths]
    el = time.perf_counter() - t0
    d["concurrent_qps"] = round(32 / el, 2) if el else None
    d["concurrent_avg_ms"] = round(el * 1000 / 32, 1)
    d["concurrent_errors"] = errs[:3]
    return d


# ---------------------------------------------------------------- main
def main():
    print(f"# 伯仕记忆系统性能测试 {tag} / {out['ts']}\n")
    env = part_env()
    out["parts"]["env"] = env
    print("## 1 规模\n")
    print("| 项 | 值 |\n|---|---|")
    for k, v in env.items():
        print(f"| {k} | {v} |")

    print("\n## 3 延迟（ms）\n")
    lat = part_latency()
    out["parts"]["latency"] = lat
    print("| 类目 | n | p50 | p95 | p99 | max |\n|---|---|---|---|---|---|")
    for k, v in lat.items():
        if isinstance(v, dict) and "p50" in v:
            print(f"| {k} | {v['n']} | {v['p50']} | {v['p95']} | {v['p99']} | {v['max']} |")
        else:
            print(f"| {k} | - | - | - | - | {v} |")

    print("\n## 4 端到端\n")
    e2e = part_e2e()
    out["parts"]["e2e"] = e2e
    for k, v in e2e.items():
        print(f"- **{k}**: {v}")

    print("\n## 2 检索质量\n")
    q = part_quality()
    out["parts"]["quality"] = q
    print("```json")
    print(json.dumps(q, ensure_ascii=False, indent=1))
    print("```")

    path = os.path.join(HERE, "logs", f"perf_{tag}.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=1)
    print(f"\n-> {path}")


if __name__ == "__main__":
    main()
