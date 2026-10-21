#!/usr/bin/env python3
"""伯仕体检 v2（PG 时代）— 一键看清「哪一环在静默降级」。
================================================================
背景：2026-10 起检索主线从 ChromaDB 迁到 PG（hermes_data.boshi）。迁移后最危险
的不是报错，而是**静默降级**：精排服务没起 → pg_bridge 自动退回融合序，检索照常
返回结果，只是改写型查询的命中率掉一档；心智模型页超过 24h 不重建 → 注入的仍是
旧结论。这类问题不看日志发现不了，所以做成一条命令。

用法：
    boshi doctor            # 全量体检（约 3-6 秒，含两次真实检索）
    boshi doctor --fast     # 跳过真实检索（只查状态，约 0.5 秒）
    boshi doctor --fix-ts   # 回填 created_at 为空的历史行

退出码：0=无失败项 / 1=有 ❌（可直接被 cron/脚本判断）

三组检查：
    A 依赖与进程   PG 连接、插件配置、表结构、精排端点、嵌入后端
    B 数据卫生     行数、空值、心智模型新鲜度、经验层、写入新鲜度
    C 检索自检     真实 query 走一遍（耗时/条数/池深）+ 槽位抽提 + 字段命中统计
"""
import os
import sys
import time
import socket

BOSHI_HOME = os.path.expanduser("~/.boshi")
if BOSHI_HOME not in sys.path:
    sys.path.insert(0, BOSHI_HOME)

_R = []          # (level, section, msg, hint)


def ok(sec, msg):
    _R.append(("ok", sec, msg, None))


def warn(sec, msg, hint=None):
    _R.append(("warn", sec, msg, hint))


def fail(sec, msg, hint=None):
    _R.append(("fail", sec, msg, hint))


_NOW = time.time()


# ── 小工具 ─────────────────────────────────────────────────────
def _cfg_yaml(*keys):
    """从 Hermes config.yaml 按缩进取一个标量（不引入 yaml 依赖）。"""
    p = os.path.expanduser("~/AppData/Local/hermes/config.yaml")
    try:
        lines = open(p, encoding="utf-8").read().splitlines()
    except Exception:
        return None
    stack = []
    for line in lines:
        s = line.strip()
        if not s or s.startswith("#") or ":" not in s:
            continue
        ind = len(line) - len(line.lstrip())
        k, v = s.split(":", 1)
        k, v = k.strip(), v.strip()
        while stack and stack[-1][0] >= ind:
            stack.pop()
        stack.append((ind, k))
        if [x[1] for x in stack] == list(keys):
            return v.strip("'\"")
    return None


def _age(ts):
    if not ts:
        return "未知"
    d = _NOW - float(ts)
    if d < 0:
        return "未来(!)"
    for unit, n in (("天", 86400), ("小时", 3600), ("分钟", 60)):
        if d >= n:
            return f"{d / n:.1f}{unit}前"
    return f"{d:.0f}秒前"


def _q(c, sql, params=None):
    cur = c.cursor()
    cur.execute(sql, params or [])
    return cur.fetchall()


# ══════════════════════════════════════════════════════════════
# A 依赖与进程
# ══════════════════════════════════════════════════════════════
SA = "A 依赖与进程"


def check_pg(sec):
    import pg_bridge as pb
    t0 = time.time()
    try:
        pb.ensure_pg_running()          # 与检索同一条拉起路径
    except Exception as ex:
        fail(sec, f"PG 启动失败：{ex}", "看 ~/.boshi/logs/pgctl.log")
        return None
    try:
        c = pb._conn()
        ver = _q(c, "SHOW server_version")[0][0]
        ok(sec, f"PG 在线 v{ver}（{(time.time() - t0) * 1000:.0f}ms 连上）")
        return c
    except Exception as ex:
        fail(sec, f"PG 连接失败：{ex}", "检查 J:\\pgsql 服务 / 5432 端口")
        return None


def check_plugin(sec):
    p = _cfg_yaml("memory", "provider")
    if p == "boshi":
        ok(sec, "插件 provider = boshi")
    else:
        fail(sec, f"插件 provider = {p}（应为 boshi）", "config.yaml → memory.provider: boshi")
    me = _cfg_yaml("memory", "memory_enabled")
    if me == "true":
        # memory_enabled 管的是「内置 memory 工具存不存在」，provider=boshi 只是把
        # 这个工具的落库后端换成伯仕。设成 false 会让工具直接消失、记忆写不进去，
        # 所以这里 true 才是对的（曾经的「让位给插件」理解是错的，2026-10-21 核对源码）。
        ok(sec, "memory_enabled = true（工具在位，落库后端=boshi）")
    else:
        fail(sec, f"memory_enabled = {me}（工具会被摘掉，伯仕收不到写入）",
             "config.yaml → memory.memory_enabled: true")


def check_tables(sec, c):
    if c is None:
        return
    want = ["memories", "graph_edges", "fm_stats"]
    try:
        have = {r[0] for r in _q(c, "SELECT table_name FROM information_schema.tables "
                                   "WHERE table_schema='boshi'")}
    except Exception as ex:
        fail(sec, f"读表结构失败：{ex}")
        return
    miss = [t for t in want if t not in have]
    if miss:
        fail(sec, f"缺表：{', '.join(miss)}", "python ~/.boshi/bootstrap.py")
    else:
        ok(sec, f"表结构齐全（{', '.join(sorted(have))}）")


def check_rerank(sec):
    """精排是最容易「没声就降级」的一环：端点不在 → 退回融合序，检索照常出结果。"""
    import pg_bridge as pb
    st = pb.rerank_status()
    if not st["enabled"]:
        warn(sec, "精排已关闭（BOSHI_RERANK=0）：改写型查询命中率会掉一档",
             "去掉环境变量 BOSHI_RERANK=0 后重启会话")
        return st
    up = False
    try:
        socket.create_connection(("127.0.0.1", 11437), timeout=1.0).close()
        up = True
    except Exception:
        up = False
    if not up:
        fail(sec, "精排端点 127.0.0.1:11437 不在（检索静默退回融合序）",
             "启动 C:\\Users\\wulezhi\\projects\\BuddhistRAG\\start_llama_rerank.bat"
             "（该服务无开机自启，重启电脑后必须手动起）")
    else:
        # 进程内 _RERANK_STATUS 只反映「本进程上一次真实调用」，新会话刚起时是空的，
        # 直接读它会把「还没调过」误报成「上次失败」。所以这里主动打一发探针，
        # 用真实请求验证端点能算（也顺便热一次模型，后续检索省掉冷启动）。
        probe = [{"content": "PG 连接超时的排查步骤"},
                 {"content": "写歌的歌词押韵与副歌结构"},
                 {"content": "PG 连接超时一般是端口或服务没起"}]
        t1 = time.time()
        pb._rerank_items("PG 连接超时怎么排查", probe, 3)
        ms = (time.time() - t1) * 1000
        st2 = pb.rerank_status()
        top = min(probe, key=lambda x: x.get("score", 9)).get("content", "")
        if st2["ok"]:
            ok(sec, f"精排在线可用（探针 {ms:.0f}ms，首位「{top[:16]}」/ "
                    f"池深 {st2['pool']} / 锚点 {st2['pool_anchor']} / "
                    f"blend w_rk={st2['blend']['w_rk']} w_fu={st2['blend']['w_fu']}）")
            st = st2
        else:
            fail(sec, f"精排探针失败（{ms:.0f}ms）：{st2['reason']} {st2.get('detail') or ''}".strip(),
                 "看 11437 进程日志；该服务无开机自启，重启后需手动起")
    m = "D:/llama/models/bge-reranker-v2-m3-FP16.gguf"
    if os.path.exists(m):
        ok(sec, f"精排模型 {os.path.getsize(m) // 1024 // 1024}MB")
    else:
        warn(sec, f"精排模型文件不在：{m}")
    if st.get("vram_free_mib") is not None and st["vram_min"]:
        free = st["vram_free_mib"]
        if free < st["vram_min"]:
            warn(sec, f"显存余量 {free}MiB < 门槛 {st['vram_min']}MiB（精排会被主动跳过）")
    return st


def check_embed(sec):
    """嵌入后端 bge-m3 本地 ONNX。坏了就是全盘失效，必须真算一次。"""
    t0 = time.time()
    try:
        import pg_bridge as pb
        dim = len(pb._ef()(["体检探针"])[0])
        ms = (time.time() - t0) * 1000
    except Exception as ex:
        fail(sec, f"嵌入后端不可用：{ex}", "检查 ~/.boshi/models 下 bge-m3 ONNX")
        return None
    if dim == 1024:
        ok(sec, f"嵌入后端正常（1024 维，首算含加载 {ms:.0f}ms）")
    else:
        fail(sec, f"嵌入维度 {dim}（应为 1024，与库内向量不一致会让召回全废）")
    return dim


# ══════════════════════════════════════════════════════════════
# B 数据卫生
# ══════════════════════════════════════════════════════════════
SB = "B 数据卫生"


def check_counts(sec, c):
    if c is None:
        return
    try:
        live, total = _q(c, "SELECT count(*) FILTER (WHERE is_latest), count(*) "
                            "FROM boshi.memories")[0]
        edges = _q(c, "SELECT count(*) FROM boshi.graph_edges")[0][0]
    except Exception as ex:
        fail(sec, f"计数失败：{ex}")
        return
    ok(sec, f"记忆 {live} 条有效 / {total} 行，图谱边 {edges}")
    try:
        rows = _q(c, "SELECT COALESCE(topic,'(无)'), count(*) FROM boshi.memories "
                     "WHERE is_latest GROUP BY 1 ORDER BY 2 DESC LIMIT 8")
        ok(sec, "主题分布 " + " ".join(f"{t}={n}" for t, n in rows))
    except Exception:
        pass


def check_nulls(sec, c):
    if c is None:
        return
    try:
        n_ts = _q(c, "SELECT count(*) FROM boshi.memories WHERE created_at IS NULL")[0][0]
        # 心智模型页按设计不写 embedding（派生数据，不参与召回），不算异常
        n_emb = _q(c, "SELECT count(*) FROM boshi.memories WHERE embedding IS NULL "
                      "AND COALESCE(topic,'') <> 'mental_model'")[0][0]
    except Exception as ex:
        fail(sec, f"空值检查失败：{ex}")
        return
    if n_emb:
        fail(sec, f"embedding 为空 {n_emb} 行（这些行永远检索不到）",
             "python ~/.boshi/backfill_slots.py")
    else:
        ok(sec, "embedding 无空值（心智模型页除外，按设计不嵌入）")
    if n_ts:
        warn(sec, f"created_at 为空 {n_ts} 行（时间窗过滤会漏掉它们）", "boshi doctor --fix-ts")
    else:
        ok(sec, "created_at 无空值")


def check_slots_fill(sec, c):
    """槽位是全库统一抽的，抽空率突然上升说明规则回归或写入绕过了 slots。"""
    if c is None:
        return
    try:
        rows = _q(c, """SELECT count(*) FILTER (WHERE slots IS NULL),
                               count(*) FILTER (WHERE slots IS NOT NULL AND slots::text <> '{}')
                        FROM boshi.memories
                        WHERE is_latest AND COALESCE(topic,'') NOT IN ('mental_model')""")
        null_s, filled = rows[0]
    except Exception:
        return
    if filled:
        ok(sec, f"槽位已抽 {filled} 行（{null_s} 行无槽位，旧数据）")


def check_mental(sec):
    try:
        import mental_model as mm
    except Exception as ex:
        warn(sec, f"心智模型模块不可用：{ex}")
        return
    if not mm.enabled():
        warn(sec, "心智模型已关闭（BOSHI_MENTAL_MODEL=0）")
        return
    t0 = time.time()
    try:
        pages = mm.load_pages()
    except Exception as ex:
        fail(sec, f"心智模型读取失败：{ex}")
        return
    ms = (time.time() - t0) * 1000
    if not pages:
        warn(sec, "心智模型页为空", "boshi model --rebuild")
        return
    oldest = min((p.get("generated_at") or 0) for p in pages.values())
    newest = max((p.get("generated_at") or 0) for p in pages.values())
    n_ev = sum(len(p.get("evidence_ids") or []) for p in pages.values())
    ok(sec, f"心智模型 {len(pages)} 页 / 证据 {n_ev} 条 / 读 {ms:.0f}ms / 最新 {_age(newest)}")
    if oldest and _NOW - float(oldest) > mm.STALE_HOURS * 3600:
        warn(sec, f"最旧一页 {_age(oldest)}（超过 {mm.STALE_HOURS}h 阈值，注入的是旧结论）",
             "boshi model --rebuild")


def check_experience(sec):
    try:
        import experience
        st = experience.stats()
        by = st.get("by_status") or {}
        ok(sec, f"经验层 {st.get('total', 0)} 条（{', '.join(f'{k}={v}' for k, v in by.items())}）")
        if st.get("total") and not by.get("promoted"):
            warn(sec, "一条经验都没晋升（promoted=0，晋升机制可能没跑起来）")
    except Exception as ex:
        warn(sec, f"经验层检查跳过：{ex}")


def check_freshness(sec, c):
    if c is None:
        return
    try:
        last = _q(c, "SELECT max(created_at) FROM boshi.memories")[0][0]
        n24 = _q(c, "SELECT count(*) FROM boshi.memories WHERE created_at > %s",
                 [_NOW - 86400])[0][0]
    except Exception:
        return
    if not last:
        return
    ok(sec, f"最近写入 {_age(last.timestamp())}，24h 内 {n24} 条")
    if _NOW - float(last.timestamp()) > 86400 * 2:
        warn(sec, "超过 48h 没有新记忆写入（自动同步可能停了）",
             "检查插件 sync_turn / boshi auto_sync_session.py")


# ══════════════════════════════════════════════════════════════
# C 检索自检
# ══════════════════════════════════════════════════════════════
SC = "C 检索自检"


def check_slots(sec):
    try:
        import slots
        s = slots.extract("把 11437 的池深调到 100，改 D:\\llama\\pg_bridge.py 第 1829 行，"
                          "版本 0.1.44.1，端口 8188，跑 boshi doctor --fast")
        got = {k: len(v) for k, v in s.items() if isinstance(v, list) and v}
        if s.get("ports") and s.get("versions") and s.get("files"):
            ok(sec, "槽位抽提正常 " + " ".join(f"{k}={v}" for k, v in got.items()))
        else:
            fail(sec, f"槽位抽提缺项：{got}", "pytest ~/.boshi/tests/ -k slots")
    except Exception as ex:
        fail(sec, f"槽位模块不可用：{ex}")


def check_fm_stats(sec, c):
    """字段级匹配是否真在起作用（近 3 天）：queries 有、hits=0 说明规则没命中过。"""
    if c is None:
        return
    try:
        rows = _q(c, "SELECT field, sum(queries), sum(hits), round(sum(gain_sum)::numeric,2) "
                     "FROM boshi.fm_stats WHERE day >= current_date - 3 GROUP BY 1 ORDER BY 1")
    except Exception:
        return
    if not rows:
        return
    ok(sec, "字段匹配近3天 " + " ".join(f"{f}:{q}查/{h}中/+{g}" for f, q, h, g in rows))


def check_search(sec):
    import boshi_core
    import pg_bridge as pb
    for q, kind in (("11437 精排池深 100 的延迟", "锚点"), ("上次说好的那个方案怎么定的", "纯中文")):
        t0 = time.time()
        try:
            res = boshi_core.search(q, top_k=3)
            ms = (time.time() - t0) * 1000
        except Exception as ex:
            fail(sec, f"检索失败「{q}」：{ex}")
            continue
        items = res.get("results") if isinstance(res, dict) else res
        n = len(items or [])
        if n == 0:
            fail(sec, f"检索无结果「{q}」（{ms:.0f}ms）")
            continue
        st = pb.rerank_status()
        pool_expect = st["pool_anchor"] if pb._has_anchor(q) else st["pool"]
        if st["enabled"] and not st["ok"]:
            warn(sec, f"检索「{q}」{n} 条 / {ms:.0f}ms，但精排已降级")
        else:
            ok(sec, f"检索「{q}」{n} 条 / {ms:.0f}ms / {kind}池深 {pool_expect}")
        if ms > 6000:
            warn(sec, f"检索耗时 {ms:.0f}ms > 6s（会话注入有超时保护，会丢上下文）")


# ══════════════════════════════════════════════════════════════
# 修复项
# ══════════════════════════════════════════════════════════════
def fix_ts():
    """回填 created_at 为空的历史行：取同 profile 最近一条有效时间，meta 打 derived 标记。

    原始时刻不可还原（那 4 行是 opencode 早期会话，正文里没有日期），回填只是
    「不让它掉出时间窗」的估算值，meta.created_at_derived=true 留痕可追溯。
    """
    import pg_bridge as pb
    try:
        c = pb._conn()
    except Exception as ex:
        print(f"❌ 连不上 PG：{ex}")
        return 1
    try:
        ids = [r[0] for r in _q(c, "SELECT id FROM boshi.memories WHERE created_at IS NULL")]
        if not ids:
            print("created_at 没有空值，无需回填")
            return 0
        cur = c.cursor()
        for mid in ids:
            # 注意：UPDATE 不带 RETURNING，psycopg2 的 fetchall 会抛
            # "the last operation didn't produce records" —— 这里直接 execute，不取结果。
            cur.execute("""UPDATE boshi.memories m SET
                             created_at = COALESCE(
                               (SELECT max(created_at) FROM boshi.memories
                                 WHERE profile = m.profile AND created_at IS NOT NULL),
                               (SELECT min(created_at) FROM boshi.memories
                                 WHERE created_at IS NOT NULL)),
                             meta = COALESCE(meta, '{}'::jsonb)
                                    || '{"created_at_derived": true}'::jsonb
                           WHERE id = %s""", [mid])
        c.commit()
        print(f"✅ 回填 created_at {len(ids)} 行（meta.created_at_derived=true 留痕）")
        return 0
    except Exception as ex:
        try:
            c.rollback()
        except Exception:
            pass
        print(f"❌ 回填失败：{ex}")
        return 1


# ══════════════════════════════════════════════════════════════
# 主流程
# ══════════════════════════════════════════════════════════════
def main(argv):
    fast = "--fast" in argv
    if "--fix-ts" in argv:
        return fix_ts()
    t_start = time.time()
    print("\n" + "=" * 62)
    print("  🩺  伯仕体检（PG 主线）")
    print("=" * 62)

    c = check_pg(SA)
    check_plugin(SA)
    check_tables(SA, c)
    check_rerank(SA)
    check_embed(SA)

    check_counts(SB, c)
    check_nulls(SB, c)
    check_slots_fill(SB, c)
    check_mental(SB)
    check_experience(SB)
    check_freshness(SB, c)

    if not fast:
        check_slots(SC)
        check_fm_stats(SC, c)
        check_search(SC)

    if c is not None:
        try:
            c.close()
        except Exception:
            pass

    print()
    sec_now = None
    for lvl, sec, msg, hint in _R:
        if sec != sec_now:
            print(f"── {sec} ──")
            sec_now = sec
        icon = {"ok": "✓", "warn": "!", "fail": "✗"}[lvl]
        print(f"  {icon} {msg}")
        if hint and lvl != "ok":
            print(f"      → {hint}")
    n_fail = sum(1 for r in _R if r[0] == "fail")
    n_warn = sum(1 for r in _R if r[0] == "warn")
    n_ok = len(_R) - n_fail - n_warn
    print("\n" + "-" * 62)
    print(f"  共 {len(_R)} 项：✓ {n_ok}   ! {n_warn}   ✗ {n_fail}"
          f"   （{(time.time() - t_start) * 1000:.0f}ms）")
    print("  " + ("🎉 全绿，没有静默降级" if not n_fail and not n_warn else
                  ("🔴 有失败项，检索能力已在打折，按 → 修" if n_fail else "🟡 无致命项，有需关注")))
    print("-" * 62 + "\n")
    return 1 if n_fail else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
