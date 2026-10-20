# -*- coding: utf-8 -*-
"""field_match 线上打点报表：读 `boshi.fm_stats`，按字段看命中率与抬升效果。

用法:
  ./venv/Scripts/python.exe eval/fm_stats_report.py            # 近 14 天
  ./venv/Scripts/python.exe eval/fm_stats_report.py --days 30
  ./venv/Scripts/python.exe eval/fm_stats_report.py --daily    # 按天×字段明细

字段含义:
  queries      带该字段字面量的检索次数（分母）
  hits         top_k 内命中该字段的条目数累计（每次检索 0~top_k）
  hit_rate     hits / queries，平均每次检索在 top_k 内命中几条
  gain_sum     累计抬升量（sim 分数），avg_gain = gain_sum / queries
  top1_changed 该字段命中且 top1 因此换人（相对抬升前）的次数
"""
import argparse
import os
import sys

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)
os.chdir(HERE)

import pg_bridge  # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=14)
    ap.add_argument("--daily", action="store_true")
    a = ap.parse_args()

    c = pg_bridge._conn()
    with c.cursor() as cur:
        cur.execute("SELECT to_regclass('boshi.fm_stats')")
        if cur.fetchone()[0] is None:
            print("表 boshi.fm_stats 还不存在：说明还没有带字面量的检索走过（或 BOSHI_FM_STATS=0）")
            return
        if a.daily:
            cur.execute(
                "SELECT day, field, queries, hits, round(gain_sum::numeric,3), top1_changed "
                "FROM boshi.fm_stats WHERE day >= CURRENT_DATE - %s "
                "ORDER BY day DESC, field", (a.days,))
            print(f"{'day':<12}{'field':<11}{'queries':>8}{'hits':>7}{'gain':>9}{'t1chg':>7}")
            for day, f, q, h, g, t in cur.fetchall():
                print(f"{str(day):<12}{f:<11}{q:>8}{h:>7}{float(g):>9.2f}{t:>7}")
            return
        cur.execute(
            "SELECT field, sum(queries), sum(hits), sum(gain_sum), sum(top1_changed) "
            "FROM boshi.fm_stats WHERE day >= CURRENT_DATE - %s "
            "GROUP BY field ORDER BY sum(queries) DESC", (a.days,))
        rows = cur.fetchall()
    if not rows:
        print(f"近 {a.days} 天没有打点记录")
        return
    print(f"近 {a.days} 天 field_match 打点（来源 boshi.fm_stats）")
    print(f"{'field':<11}{'queries':>8}{'hits':>7}{'hit/query':>10}"
          f"{'avg_gain':>10}{'t1_changed':>11}{'t1_rate':>9}")
    for f, q, h, g, t in rows:
        q = int(q or 0)
        print(f"{f:<11}{q:>8}{int(h or 0):>7}{(h / q if q else 0):>10.2f}"
              f"{(float(g or 0) / q if q else 0):>10.3f}{int(t or 0):>11}"
              f"{(int(t or 0) / q if q else 0):>9.1%}")


if __name__ == "__main__":
    main()
