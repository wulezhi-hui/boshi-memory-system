#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""伯仕 P2 存量回填工具（方案 A1/A2）

A1 原文回填：把 `asst[:800] + "…"` 的截断行，用 Hermes state.db 里的完整
              assistant 消息覆盖 content，并重算 embedding（否则向量仍按旧摘要算）。
A2 存量槽位：只为 slots IS NULL 的行补 slots 列，**不动 content、不重嵌入**。

用法（都在 ~/.boshi 下执行）：
    python boshi_backfill.py --snapshot            # 快照将被 A1 修改的行（CSV）
    python boshi_backfill.py --dry-run --n 200     # 只看 diff，不写库
    python boshi_backfill.py --apply               # 按 dry-run 生成的 plan 执行 A1
    python boshi_backfill.py --slots-only [--apply]  # A2；不加 --apply 只统计

安全约定：
  - 匹配必须 content[:-1] == 原文[:800] 且原文更长，否则算 miss，记日志不猜。
  - 同前缀多条候选时取**最长**且前 800 字完全一致的那条；长度相同但不一致 → miss。
  - miss 一律不补、不改，写 logs/backfill-miss.jsonl。
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import sqlite3
import sys
import time
from datetime import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
os.chdir(HERE)

import pg_bridge  # noqa: E402

LOGS = os.path.join(HERE, "logs")
os.makedirs(LOGS, exist_ok=True)
STATE_DB = os.environ.get("BOSHI_STATE_DB") or \
    r"C:/Users/wulezhi/AppData/Local/hermes/state.db"
TRUNC_WHERE = "length(content) = 801 and right(content, 1) = '…'"


def _slots_mod():
    return pg_bridge._slots_mod()


# ── A1 快照 ────────────────────────────────────────────────────────────────
def cmd_snapshot():
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    path = os.path.join(LOGS, f"backfill-snapshot-{ts}.csv")
    with pg_bridge._conn() as c, c.cursor() as cur:
        cur.execute(f"SELECT id::text, content, slots, raw_len, created_at "
                    f"FROM boshi.memories WHERE {TRUNC_WHERE} ORDER BY created_at")
        rows = cur.fetchall()
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["id", "content", "slots", "raw_len", "created_at"])
        for r in rows:
            w.writerow([r[0], r[1], json.dumps(r[2], ensure_ascii=False) if r[2] else "",
                        r[3] if r[3] is not None else "", r[4].isoformat()])
    print(f"快照写入 {path}  共 {len(rows)} 行  {os.path.getsize(path)/1e6:.1f} MB")
    return path


# ── 原文匹配 ───────────────────────────────────────────────────────────────
def _load_candidates():
    """state.db 里所有长度 > 800 的 assistant 消息，按前 120 字建索引。"""
    con = sqlite3.connect(f"file:{STATE_DB}?mode=ro", uri=True)
    con.text_factory = str
    cur = con.cursor()
    cur.execute("SELECT content FROM messages WHERE role='assistant' "
                "AND length(content) > 800")
    idx = {}
    n = 0
    for (txt,) in cur:
        if not txt:
            continue
        idx.setdefault(txt[:120], []).append(txt)
        n += 1
    con.close()
    print(f"state.db 候选消息 {n} 条（>800 字 assistant）")
    return idx


def build_plan():
    """返回 (plan, miss)。plan: [(id, old, new)]"""
    with pg_bridge._conn() as c, c.cursor() as cur:
        cur.execute(f"SELECT id::text, content FROM boshi.memories "
                    f"WHERE {TRUNC_WHERE} ORDER BY created_at")
        rows = cur.fetchall()
    print(f"待回填截断行 {len(rows)} 条")
    idx = _load_candidates()
    plan, miss = [], []
    for rid, old in rows:
        head = old[:-1]                     # 去掉末尾 "…"
        cands = idx.get(head[:120], [])
        best = None
        for t in cands:
            if t[:800] != head:
                continue
            if best is None or len(t) > len(best):
                best = t
        if best is None or len(best) <= 800:
            miss.append({"id": rid, "reason": "no_prefix_match", "len": len(old),
                         "head": old[:60]})
            continue
        plan.append((rid, old, best))
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    plan_path = os.path.join(LOGS, f"backfill-plan-{ts}.jsonl")
    with open(plan_path, "w", encoding="utf-8") as f:
        for rid, old, new in plan:
            f.write(json.dumps({"id": rid, "old": old, "new": new},
                               ensure_ascii=False) + "\n")
    if miss:
        with open(os.path.join(LOGS, "backfill-miss.jsonl"), "a", encoding="utf-8") as f:
            for m in miss:
                f.write(json.dumps(m, ensure_ascii=False) + "\n")
    with open(os.path.join(LOGS, "backfill-plan-latest.txt"), "w") as f:
        f.write(plan_path)
    print(f"命中 {len(plan)} / 未命中 {len(miss)}  → plan: {plan_path}")
    return plan, miss, plan_path


def cmd_dry_run(n=200):
    t0 = time.time()
    plan, miss, plan_path = build_plan()
    print(f"命中率 {len(plan)/(len(plan)+len(miss) or 1)*100:.1f}%  用时 {time.time()-t0:.1f}s\n")
    print("── 前 %d 条 diff（截断 → 原文）──" % min(n, len(plan)))
    for i, (rid, old, new) in enumerate(plan[:n], 1):
        print(f"[{i}] {rid[:8]}  {len(old)} → {len(new)} 字  (补回 {len(new)-len(old)} 字)")
        print(f"    旧尾: …{old[-60:-1]}")
        print(f"    新尾: …{new[-60:]}")
    if miss:
        print(f"\n未命中 {len(miss)} 条（不补，已记 logs/backfill-miss.jsonl）")
    print(f"\nplan 文件: {plan_path}")
    print("确认后执行: python boshi_backfill.py --apply")


# ── A1 执行 ────────────────────────────────────────────────────────────────
def cmd_apply(batch=4):
    p = os.path.join(LOGS, "backfill-plan-latest.txt")
    if not os.path.exists(p):
        sys.exit("没有 plan，先跑 --dry-run")
    plan_path = open(p).read().strip()
    plan = [json.loads(l) for l in open(plan_path, encoding="utf-8") if l.strip()]
    print(f"按 plan 执行: {plan_path}  共 {len(plan)} 条")
    sm = _slots_mod()
    ef = pg_bridge._ef()
    t0 = time.time()
    done = 0
    # 先一次性取全部向量化文本太占内存，按批处理
    with pg_bridge._conn() as c:
        for i in range(0, len(plan), batch):
            chunk = plan[i:i + batch]
            texts = [it["new"] for it in chunk]
            vecs = ef(texts) if callable(ef) else None
            if vecs is None:
                vecs = [ef(t) for t in texts]
            for it, vec in zip(chunk, vecs):
                slots = sm.extract(it["new"])
                meta = sm.to_meta(slots, len(it["new"]))
                with c.cursor() as cur:
                    cur.execute(
                        "UPDATE boshi.memories SET content=%s, embedding=%s::vector, "
                        "slots=%s::jsonb, raw_len=%s WHERE id=%s::uuid",
                        (it["new"], pg_bridge._vec_literal(vec),
                         json.dumps(slots, ensure_ascii=False),
                         len(it["new"]), it["id"]))
                done += 1
            c.commit()
            el = time.time() - t0
            rate = done / el if el else 0
            print(f"  {done}/{len(plan)}  {rate:.2f} 条/s  剩 ~{(len(plan)-done)/rate/60:.1f} 分钟"
                  if rate else f"  {done}/{len(plan)}", flush=True)
    print(f"\n完成 {done} 条，用时 {(time.time()-t0)/60:.1f} 分钟")


# ── A2 存量槽位 ────────────────────────────────────────────────────────────
def cmd_slots_only(apply=False):
    with pg_bridge._conn() as c, c.cursor() as cur:
        cur.execute("SELECT id::text, content, raw_len FROM boshi.memories "
                    "WHERE slots IS NULL ORDER BY created_at")
        rows = cur.fetchall()
    print(f"slots 为空的存量记录 {len(rows)} 条")
    sm = _slots_mod()
    t0 = time.time()
    out = []
    empty = 0
    for rid, content, raw_len in rows:
        s = sm.extract(content or "")
        if not sm.filled_count(s):
            empty += 1
            # 抽不出槽位 **不写库**：让 slots 保持 NULL（语义=未抽提），
            # 否则 3,288 条短句会各占一个空 jsonb，且"已抽提"和"无可抽提"
            # 两种状态混成一团，日后无法判断覆盖情况。
            continue
        out.append((json.dumps(s, ensure_ascii=False), len(content or ""),
                    raw_len if raw_len is not None else len(content or ""), rid))
    print(f"抽提完成 用时 {time.time()-t0:.1f}s；抽不出任何槽位 {empty} 条"
          f"（{(empty/len(rows)*100 if rows else 0):.1f}%，保持 NULL 不写）"
          f"；待写入 {len(out)} 条")
    sample = out[:3]
    for s, ln, rl, rid in sample:
        print(f"  样例 {rid[:8]} len={ln} → {s[:160]}")
    if not apply:
        print("\n（未写库；加 --apply 执行）")
        return
    ts = time.strftime("%Y%m%d-%H%M%S")
    ids_path = os.path.join(LOGS, f"a2-ids-{ts}.txt")
    with open(ids_path, "w", encoding="utf-8") as f:
        f.write("\n".join(rid for _, _, _, rid in out))
    print(f"回滚清单: {ids_path}（回滚 = UPDATE boshi.memories SET slots=NULL WHERE id IN 清单）")
    with pg_bridge._conn() as c:
        with c.cursor() as cur:
            cur.executemany(
                "UPDATE boshi.memories SET slots=%s, raw_len=COALESCE(raw_len,%s) "
                "WHERE id=%s::uuid", [(s, rl, rid) for s, ln, rl, rid in out])
        c.commit()
    print(f"已写入 slots {len(out)} 条（content / embedding 未动）  用时 {time.time()-t0:.1f}s")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--snapshot", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--slots-only", action="store_true")
    ap.add_argument("--n", type=int, default=200)
    a = ap.parse_args()
    if a.snapshot:
        cmd_snapshot()
    elif a.dry_run:
        cmd_dry_run(a.n)
    elif a.apply and a.slots_only:
        cmd_slots_only(True)
    elif a.slots_only:
        cmd_slots_only(False)
    elif a.apply:
        cmd_apply()
    else:
        ap.print_help()


if __name__ == "__main__":
    main()
