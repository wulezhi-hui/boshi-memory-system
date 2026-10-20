# -*- coding: utf-8 -*-
"""P2+ 线上打点审计：读 logs/fm_trace.jsonl，统计「抬升改变了多少名次」。

背景：A8 的 gold 由「含该字面量的记忆」反推，构造上偏向本特性有效，
回答不了「线上会不会抬错」。本脚本读的是真实/抽样 query 的打点，
只回答可观测的三件事：
  1) 带字面量的 query 占多少、抬升命中率多少；
  2) top1 被改动的比例（churn）——改动越多，误抬面越大；
  3) 逐条列出改动样本，供人工判断抬上来的是不是该要的东西。

用法:
  BOSHI_FM_TRACE=1 python eval/a5_eval.py        # 先攒样本（任何走 search_memory 的脚本都行）
  python scratch/fm_trace_report.py [--n 20]
"""
import argparse
import json
import os

HERE = os.path.expanduser("~/.boshi")
TRACE = os.path.join(HERE, "logs", "fm_trace.jsonl")


def load(path=TRACE):
    if not os.path.exists(path):
        return []
    out = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except Exception:
                continue
    return out


def summarize(recs):
    n = len(recs)
    if not n:
        return {"n": 0}
    changed = [r for r in recs if r.get("top1_changed")]
    hit = [r for r in recs if r.get("hit_n")]
    return {
        "n": n,
        "with_literal": sum(1 for r in recs if r.get("fields")),
        "any_hit": len(hit),
        "top1_changed": len(changed),
        "churn_pct": round(100.0 * len(changed) / n, 1),
        "top1_hit_pct": round(100.0 * sum(1 for r in recs if r.get("top1_hit")) / n, 1),
        "avg_hit_n": round(sum(r.get("hit_n") or 0 for r in recs) / n, 2),
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=20, help="列出最近 N 条改动样本")
    ap.add_argument("--path", default=TRACE)
    a = ap.parse_args()

    recs = load(a.path)
    s = summarize(recs)
    if not s["n"]:
        print(f"没有打点数据：{a.path}\n先跑 `BOSHI_FM_TRACE=1 python eval/a5_eval.py` 攒样本。")
        return
    print(f"打点文件: {a.path}")
    print(f"样本数: {s['n']}  带字面量: {s['with_literal']}  候选有命中: {s['any_hit']}")
    print(f"top1 被改动: {s['top1_changed']}  ({s['churn_pct']}%)")
    print(f"改动后 top1 本身带命中: {s['top1_hit_pct']}%   平均命中条数: {s['avg_hit_n']}")

    changed = [r for r in recs if r.get("top1_changed")][-a.n:]
    if changed:
        print(f"\n最近 {len(changed)} 条改动样本（pre → post）：")
        for r in changed:
            print(f"  [{r['ts'][:19]}] {r['query'][:56]}")
            print(f"     fields={list((r.get('fields') or {}).keys())} "
                  f"hit_n={r.get('hit_n')} "
                  f"{r['pre_top'][0]} → {r['post_top'][0]} "
                  f"(top1_hit={r.get('top1_hit')})")

    # 改动但抬上来的那条没有命中字段 → 逻辑上不该发生，出现即实现 bug
    bad = [r for r in changed if not r.get("top1_hit")]
    print(f"\n可疑（改了 top1 但新 top1 无字段命中，应为 0）: {len(bad)}")


if __name__ == "__main__":
    main()
