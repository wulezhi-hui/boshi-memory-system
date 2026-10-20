# -*- coding: utf-8 -*-
"""诊断：纯中文 query 上 kw 通道为什么与纯向量逐条一致（A9 的 auto==score）

对 A9 的若干 query 直接调用 _kw_tokens / _kw_channel，打印：
  toks（含权重）、主 token（w>=2.5）数、kw 候选数、kw_abs 分布
"""
import os
import sys

HERE = r"C:/Users/wulezhi/.boshi"
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, "eval"))
os.chdir(HERE)
import pg_bridge
from cases_a9 import CASES

c = pg_bridge._conn()
cond, params = pg_bridge._where_sql({}, "all", pg_bridge._resolve_profile(), False)
print("cond:", cond[:120])

picked = [CASES[i] for i in (0, 1, 2, 3, 6, 8, 12, 16)]   # overlap/paraphrase 混采
for kind, gid, anchor, q in picked:
    toks = pg_bridge._kw_tokens(q)
    prim = [(t, w) for t, w in toks if w >= 2.5]
    kw = pg_bridge._kw_channel(c, toks, 20, cond, params)
    print(f"\n[{kind}] gold={gid} q={q}")
    print("  toks:", [(t, w) for t, w in toks][:12])
    print(f"  主token(w>=2.5)={len(prim)}  kw候选={len(kw)}")
    if kw:
        print("  kw top5:", [(str(x["id"])[:8], x.get("kw"), x.get("kw_abs")) for x in kw[:5]])
        print("  含 gold:", any(str(x["id"])[:8] == gid for x in kw))
