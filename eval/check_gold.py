# -*- coding: utf-8 -*-
"""gold 自检：每条 gold 必须存在、is_latest、且 content 逐字包含 anchor。

评测前先跑这个——gold 记错会让所有指标失真，而且看起来"合理"。
用法: python eval/check_gold.py
"""
import os
import sys

HERE = r"C:/Users/wulezhi/.boshi"
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, "eval"))
os.chdir(HERE)
import pg_bridge
from cases import CASES

c = pg_bridge._conn()
bad = []
with c.cursor() as cur:
    for q, gold, anchor in CASES:
        cur.execute("select id::text, content, is_latest from boshi.memories "
                    "where id::text like %s", (gold + "%",))
        r = cur.fetchone()
        if not r:
            bad.append((gold, "NOT_FOUND", anchor))
        elif anchor not in r[1]:
            bad.append((gold, "ANCHOR_MISSING", anchor, r[1][:60]))
        elif not r[2]:
            bad.append((gold, "NOT_LATEST", anchor))
print(f"cases={len(CASES)}  bad={len(bad)}")
for b in bad:
    print("  ", b)
sys.exit(1 if bad else 0)
