# -*- coding: utf-8 -*-
"""A9 真实库上：_KW_ABS_ALL 开/关时 kw_abs 的分布（验证「融合层证据是否存在」）。"""
import os
import statistics
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import pg_bridge as p  # noqa: E402

QS = [
    "群晖硬盘显示已损毁但检测是好的怎么办",
    "干净石面被模型自己加刻纹怎么办",
    "显存小的老显卡能不能跑音乐生成",
]
for q in QS:
    c = p._conn()
    toks = p._kw_tokens(q)
    print("\nQ:", q, "tokens=%d" % len(toks), toks[:4], "...")
    for flag in (False, True):
        p._KW_ABS_ALL = flag
        items = p._kw_channel(c, toks, 40, "1=1", []) or []
        vals = [it["kw_abs"] for it in items]
        print("  abs_all=%-5s n=%-3d zero=%d/%d max=%.3f median=%.3f top1src=%r" % (
            flag, len(vals), sum(1 for v in vals if v == 0.0), len(vals),
            max(vals) if vals else -1, statistics.median(vals) if vals else -1,
            (items[0].get("kw_abs_all") if items else None)))
