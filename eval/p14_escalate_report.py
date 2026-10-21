# -*- coding: utf-8 -*-
"""阈值网格：从 logs/p14_escalate.jsonl 算「浅池 + 低分才升级」混合策略的成绩。

规则：同一条查询先看浅池 top1 logit；logit < thr 才用深池结果，否则用浅池结果。
（logit 越大越相关；分数为负说明模型不确信，这正是要升级的信号。）
用法: ./venv/Scripts/python.exe eval/p14_escalate_report.py
"""
import json
import os

HERE = r"C:/Users/wulezhi/.boshi"
rows = [json.loads(l) for l in open(os.path.join(HERE, "logs", "p14_escalate.jsonl"),
                                    encoding="utf-8") if l.strip()]


def score(pick):
    st = {}
    for r in rows:
        k = "A5" if r["kind"] == "A5" else r["kind"]
        d = st.setdefault(k, {"n": 0, "t1": 0, "t3": 0, "t5": 0})
        rk = pick(r)
        d["n"] += 1
        d["t1"] += 1 if rk == 1 else 0
        d["t3"] += 1 if rk and rk <= 3 else 0
        d["t5"] += 1 if rk else 0
    return st


def fmt(tag, st, esc=None):
    def f(k):
        d = st[k]
        return "%d/%d/%d" % (d["t1"], d["t3"], d["t5"])
    extra = "" if esc is None else " 升级 %d/%d 次" % (esc, len(rows))
    print("%-22s over %-9s para %-9s A5 %-9s%s" %
          (tag, f("overlap"), f("paraphrase"), f("A5"), extra))


print("样本 %d 条\n" % len(rows))
fmt("全浅池", score(lambda r: r["shallow_rank"]))
fmt("全深池", score(lambda r: r["deep_rank"]))

logits = sorted([r["shallow_top1_logit"] for r in rows
                 if r["shallow_top1_logit"] is not None])
print("\n浅池 top1 logit 分布: n=%d min=%.2f p25=%.2f p50=%.2f p75=%.2f max=%.2f" % (
    len(logits), logits[0], logits[len(logits) // 4], logits[len(logits) // 2],
    logits[int(len(logits) * .75)], logits[-1]))

print()
for thr in (2.0, 1.0, 0.5, 0.0, -0.5, -1.0, -1.5, -2.0, -3.0):
    esc = sum(1 for r in rows if (r["shallow_top1_logit"] or -99) < thr)
    st = score(lambda r: r["deep_rank"] if (r["shallow_top1_logit"] or -99) < thr
               else r["shallow_rank"])
    fmt("thr=%+.1f" % thr, st, esc)
