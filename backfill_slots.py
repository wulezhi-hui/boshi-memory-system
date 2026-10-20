# -*- coding: utf-8 -*-
"""存量库 slots 回填（零模型、纯规则重算）。

背景：抽提器升级（四段版本号 `RE_VER4_V` / `RE_VER4`）后，存量行的 `slots` 列仍是
旧规则产物（如 `v0.1.44.1` 被截成 `v0.1.44` + `0.5`）。字段级匹配优先读 DB slots，
于是同一条记忆「新查询抽得到、旧槽位比不过」，A5 实测把 gold 挤出 top5。

做法：对 boshi.memories 每行重跑 `slots.extract(content)`，与现有 slots 逐字段比较，
只 UPDATE 有差异的行；null 槽位一并补。全量备份 id+旧 slots 到
`backups/slots_backfill_<ts>.json`，可整表还原。

用法:
  python backfill_slots.py --dry-run     # 只统计，不写库
  python backfill_slots.py --apply       # 备份 + 分批 UPDATE
"""
import argparse
import datetime
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
os.chdir(HERE)

import pg_bridge  # noqa: E402

BATCH = 200


def norm(sl):
    if isinstance(sl, str):
        try:
            sl = json.loads(sl)
        except Exception:
            return None
    return sl if isinstance(sl, dict) else None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--limit", type=int, default=0)
    a = ap.parse_args()
    if not (a.dry_run or a.apply):
        ap.error("需要 --dry-run 或 --apply")

    sm = pg_bridge._slots_mod()
    conn = pg_bridge._conn()
    ts = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    bak_path = os.path.join(HERE, "backups", f"slots_backfill_{ts}.json")
    os.makedirs(os.path.dirname(bak_path), exist_ok=True)

    sql = "SELECT id, content, slots, raw_len FROM boshi.memories ORDER BY id"
    if a.limit:
        sql += f" LIMIT {int(a.limit)}"
    with conn.cursor() as cur:
        cur.execute(sql)
        rows = cur.fetchall()

    changed, nulls, same, skipped, backup = [], 0, 0, 0, []
    for rid, content, slots, raw_len in rows:
        if not content or not str(content).strip():
            skipped += 1
            continue
        old = norm(slots)
        new = sm.extract(content)
        if old is None:
            nulls += 1
            changed.append((rid, new, len(str(content)), raw_len))
        elif old != new:
            changed.append((rid, new, len(str(content)), raw_len))
        else:
            same += 1
        if old != new:
            backup.append({"id": str(rid), "slots": old})

    print(f"总行 {len(rows)} | 有差异 {len(changed)}（其中原为 NULL {nulls}）"
          f" | 一致 {same} | 跳过空内容 {skipped}")
    if a.dry_run:
        for rid, new, _c, _r in changed[:5]:
            print("  例:", str(rid)[:8], json.dumps(new, ensure_ascii=False)[:160])
        return

    with open(bak_path, "w", encoding="utf-8") as f:
        json.dump(backup, f, ensure_ascii=False)
        f.write("\n")
    print("备份:", bak_path, len(backup), "条")

    n = 0
    with conn.cursor() as cur:
        for rid, new, clen, raw_len in changed:
            cur.execute(
                "UPDATE boshi.memories SET slots = %s, raw_len = COALESCE(raw_len, %s) "
                "WHERE id = %s",
                (json.dumps(new, ensure_ascii=False), clen or raw_len, rid),
            )
            n += 1
            if n % BATCH == 0:
                conn.commit()
                print(f"  已写 {n}/{len(changed)}")
        conn.commit()
    print(f"写入完成 {n} 行")


if __name__ == "__main__":
    main()
