# -*- coding: utf-8 -*-
"""A9 上 BOSHI_KW_REL_BLEND=0 vs 0.4 的稳定性判定（决定是否改默认）

为什么：单次 A9 上 b=0.4 是 14/20/26 vs 13/20/24，但 40 条样本的 ±1 就是噪声。
判定口径：
  1. 换 3 个 ASOF 快照（语料不同 → 候选池不同），看结论是否同向；
  2. 每条 query 做配对比较（同一 gold、同一 query），数 top1/top3/top5 的涨跌，
     用精确二项符号检验看是否可区分。
用法: ./venv/Scripts/python.exe scratch/a9_b_stability.py
"""
import json
import os
import subprocess
import sys
from math import comb

HERE = r"C:/Users/wulezhi/.boshi"
PY = os.path.join(HERE, "venv", "Scripts", "python.exe")
SNAPSHOTS = ["1792461898", "1791500000", "1790500000", ""]  # "" = 最新语料
BS = ["0", "0.4"]


def run(asof, b):
    tag = f"stab_{asof or 'live'}_b{b}"
    env = dict(os.environ)
    env["A9_ASOF"] = asof
    env["A9_SCOPE"] = "all"
    env["BOSHI_KW_REL_BLEND"] = b
    path = os.path.join(HERE, "logs", f"a9_{tag}.json")
    if not os.path.exists(path):
        subprocess.run([PY, "eval/a9_eval.py", tag], cwd=HERE, env=env,
                       capture_output=True, text=True, timeout=1800)
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def rk(c):
    v = c["auto"]
    return v.get("rank") if isinstance(v, dict) else v


def sign_test(up, dn):
    n = up + dn
    if not n:
        return 1.0
    return min(1.0, sum(comb(n, i) for i in range(0, min(up, dn) + 1)) / 2 ** n * 2)


summary = {}
for asof in SNAPSHOTS:
    a, b = run(asof, "0"), run(asof, "0.4")
    key = asof or "live"
    row = {"asof": key, "base_stat": a["stat"]["overlap"]["auto"],
           "base_para": a["stat"]["paraphrase"]["auto"],
           "b4_stat": b["stat"]["overlap"]["auto"],
           "b4_para": b["stat"]["paraphrase"]["auto"], "cmp": {}}
    for k in (1, 3, 5):
        up = dn = 0
        ups, dns = [], []
        for x, y in zip(a["cases"], b["cases"]):
            if x["gold"] != y["gold"]:
                continue
            r1, r2 = rk(x), rk(y)
            h1 = r1 is not None and r1 <= k
            h2 = r2 is not None and r2 <= k
            if h2 and not h1:
                up += 1
                ups.append((x["gold"], x["kind"], r1, r2))
            if h1 and not h2:
                dn += 1
                dns.append((x["gold"], x["kind"], r1, r2))
        row["cmp"][f"top{k}"] = {"up": up, "down": dn, "p": round(sign_test(up, dn), 3),
                                 "ups": ups, "downs": dns}
    summary[key] = row

out = os.path.join(HERE, "logs", "a9_b_stability.json")
with open(out, "w", encoding="utf-8") as f:
    json.dump(summary, f, ensure_ascii=False, indent=1)

print("| 快照 | b=0 overlap t1/3/5 | b=0.4 overlap | b=0 para t1/3/5 | b=0.4 para | top1 +/− (p) | top3 +/− (p) | top5 +/− (p) |")
print("|---|---|---|---|---|---|---|---|")
for k, r in summary.items():
    f3 = lambda d: f"{d['top1']}/{d['top3']}/{d['top5']}"
    c = r["cmp"]
    print(f"| {k} | {f3(r['base_stat'])} | {f3(r['b4_stat'])} | {f3(r['base_para'])} | "
          f"{f3(r['b4_para'])} | +{c['top1']['up']}/−{c['top1']['down']} ({c['top1']['p']}) | "
          f"+{c['top3']['up']}/−{c['top3']['down']} ({c['top3']['p']}) | "
          f"+{c['top5']['up']}/−{c['top5']['down']} ({c['top5']['p']}) |")
print(f"\n-> {out}")
