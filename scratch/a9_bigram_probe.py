# -*- coding: utf-8 -*-
"""探针：query 侧 bigram 截断到底丢了多少证据（A9 40 条纯中文 query）。

背景：_kw_tokens 给连续汉字只取前 6 个 bigram（pg_bridge.py:735 `range(min(n-1, 6))`），
而 _doc_tokens 索引全部 bigram（pg_bridge.py:794 `range(n-1)`）。
→ 中文长句的后半段在 query 侧根本没有 token，永远不可能命中。

本探针逐条统计：
  · seg 长度 / 应有 bigram 数 / 实际用到的 / 被截断的
  · 截断掉的 bigram 里有多少在 postings 里真实存在（df>0）：即「本来能命中」的证据
  · gold 正文包含被截断 bigram、却不包含任何已用 bigram 的条数（＝纯损失）
"""
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
os.chdir(os.path.dirname(HERE))

import pg_bridge as P          # noqa: E402
from eval.cases_a9 import CASES  # noqa: E402

CJK = re.compile(r"[\u4e00-\u9fff]+")


def run():
    c = P._conn()
    idx = P._kw_index(c, "TRUE", [])
    postings = idx["postings"]
    docs = {str(d[0]): d[1] for d in idx["docs"]}   # id -> content

    n_q = 0
    tot_bg = tot_used = tot_cut = tot_cut_hit = 0
    pure_loss = []       # gold 只有截断 bigram 命中
    q_with_cut_hit = 0

    for grp, gid, anchor, q in CASES:
        n_q += 1
        segs = CJK.findall(q)
        used, cut = [], []
        for seg in segs:
            n = len(seg)
            if n < 3:
                continue
            allg = [seg[i:i + 2] for i in range(n - 1)]
            u = allg[:6]
            used.extend(u)
            cut.extend(allg[6:])
        tot_bg += len(used) + len(cut)
        tot_used += len(used)
        tot_cut += len(cut)
        cut_hit = [g for g in cut if g in postings]
        used_hit = [g for g in used if g in postings]
        tot_cut_hit += len(cut_hit)
        if cut_hit:
            q_with_cut_hit += 1
        # gold 侧：找 content 以 gid 开头的行
        gold = next((v for k, v in docs.items() if k.startswith(gid)), None)
        if gold:
            g_cut = [g for g in cut_hit if g in gold]
            g_used = [g for g in used_hit if g in gold]
            if g_cut and not g_used:
                pure_loss.append((grp, gid, q, g_cut[:4]))

    print(f"query 数            : {n_q}")
    print(f"bigram 总需求       : {tot_bg}")
    print(f"query 实际使用      : {tot_used}  ({tot_used/max(1,tot_bg):.0%})")
    print(f"被截断              : {tot_cut}  ({tot_cut/max(1,tot_bg):.0%})")
    print(f"截断中库内真实存在  : {tot_cut_hit}  ({tot_cut_hit/max(1,tot_cut):.0%} of 截断)")
    print(f"含「截断也能命中」的 query: {q_with_cut_hit}/{n_q}")
    print()
    print(f"纯损失条数（gold 只靠截断 bigram 命中）: {len(pure_loss)}")
    for row in pure_loss:
        print("   ", row[0], row[1], "|", row[2], "→", row[3])


if __name__ == "__main__":
    run()
