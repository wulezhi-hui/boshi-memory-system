# -*- coding: utf-8 -*-
"""诊断：coord 修复后，A9 上两处掉名次的 query（56d59b74 随风飘荡）到底被谁挤掉。"""
import math
import os
import sys

HERE = r"C:/Users/wulezhi/.boshi"
sys.path.insert(0, HERE)
os.chdir(HERE)
import pg_bridge
import importlib
sh = importlib.import_module("scratch.a9_kwfix_shadow") if False else None

# 直接复用影子脚本里的修复版（复制导入避免它跑主流程）
src = open(os.path.join(HERE, "scratch", "a9_kwfix_shadow.py"), encoding="utf-8").read()
ns = {}
exec(src.split("def main_hit_count")[0], ns)
patched = ns["patched_kw_channel"]

c = pg_bridge._conn()
cond, params = pg_bridge._where_sql({}, "all", pg_bridge._resolve_profile(), False)

for q, gid in (("随风飘荡那首歌的精简歌词", "56d59b74"),
               ("那首唱一生飘荡的歌歌词整理版", "56d59b74"),
               ("跨盘搬大文件怎么核对拷贝完整", "c0abea75")):
    toks = pg_bridge._kw_tokens(q)
    kw = patched(c, toks, 20, cond, params)
    vec = pg_bridge.search_memory(q, top_k=5, fusion="vector", scope="all")
    print(f"\n=== {q}  gold={gid}")
    print("  toks:", [(t, w) for t, w in toks][:8])
    print("  kw  top6:", [(str(x["id"])[:8], x.get("kw"), x.get("kw_abs")) for x in kw[:6]])
    print("  gold 在 kw:", any(str(x["id"])[:8] == gid for x in kw),
          [ (i, str(x["id"])[:8], x.get("kw_abs")) for i, x in enumerate(kw,1) if str(x["id"])[:8] == gid])
    print("  vec top5:", [(str(x["id"])[:8], round(1 - float(x["score"]), 3)) for x in vec])
    with c.cursor() as cur:
        cur.execute("SELECT left(id::text,8), left(content,40) FROM boshi.memories WHERE is_latest AND left(id::text,8)=ANY(%s)",
                    ([gid] + [str(x["id"])[:8] for x in kw[:4]],))
        for r in cur.fetchall():
            print("   ", r[0], r[1].replace("\n", " "))
