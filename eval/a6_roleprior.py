# -*- coding: utf-8 -*-
"""A6：问题式 query 下"角色先验"重排实验（不改代码，纯离线重排）

背景：A5 发现 top5 漏召的 3 条里，2 条是被**同主题的用户提问**顶掉——
用户提问和"提问式 query"语义最近，而真正的答案是 assistant_conclusion，
语义上离 question 稍远。故试：查询是问句时，对 role='user' 的候选加距离惩罚。

评测集直接复用 a5_eval.CASES（35 条，scope=all）。
用法: python a6_roleprior.py
"""
import json
import os
import re
import sys
import time

HERE = r"C:/Users/wulezhi/.boshi"
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, "eval"))
os.chdir(HERE)
import pg_bridge

CASES = [
    ("218 router 启动清单 17 条 一个模型两个条目", "70cd3d35"),
    ("P4 中医知识库 tensor 32B-VL n_slots 4 验收", "55cc3251"),
    ("禁用 QQ 右键菜单 DisabledShellExtensions 共几项", "4b17de7c"),
    ("Xeon E5-2696 v4 GRD 软件编码 远程桌面 对比", "38ff6ff1"),
    ("embedding 服务 11436 隐藏窗口 自动拉起", "a5ad3de9"),
    ("218 vLLM v0.2.2-post2 驱动 595.91.07 CUDA13.0 复刻", "abb74ac7"),
    ("ComfyUI Manager 启动 9 次报错 model-list.json", "c6db5b3f"),
    ("DSH 550K 上下文 改的是 models.dev limit.context", "e9264528"),
    ("辞典 E26 主件漏 768 词条 九界", "4062f4c8"),
    ("agnes-ai 7 个视觉模型哪几个能看图", "d2ccef88"),
    ("Qwen3.8-Flash-Next MTP Q8_0 4.14GB 自包含", "3691a63e"),
    ("本机驱动 610.47 CUDA UMD 13.3 能跑哪个预编译包", "fd922a5f"),
    ("llama 20.4 tok/s 内核 7.0 gdm 停 595.84", "e5b61c3d"),
    ("wsl_update.msi 删除 拒绝访问 脚本假阳性", "c08f0272"),
    ("MTP N=4 N≥7 有损 DFlash2 无损 md5 逐字相同", "bf3e2bc0"),
    ("195 个月 缴到退休 够不够 25 年 要不要补", "91492b69"),
    ("218 加载 occamy-1.0-Q6_K 34.6B 代理到 53653", "1f682f5d"),
    ("电源睡眠 900 秒 0x384 电池 0x2D0", "dd6c29c4"),
    ("PLC 巴汉辞典 dila TEI 庄春江 本机有没有中巴方向", "224e4305"),
    ("格桑舞 ref2va 参考图 首帧 灰斑 贴脸融合失败", "5775c9f8"),
    ("Qwythos-9B 加载失败 ctx 512 根因", "7d944c53"),
    ("192K 和 64K 对比 30K 上下文 llama-server timing", "66b6de8f"),
    ("opencode v2.0.16 不提供 @opencode/plugin 服务端插件", "0a07f5a4"),
    ("/data 机械盘顺序读 109 MB/s tcm 144 MB/s", "e79e3fe9"),
    ("思考模式会不会降低 tok/s", "0ce918be"),
    ("218 双 2080Ti 各 22528 MiB 总显存 44G", "90161ac0"),
    ("strata expert arena 39 GiB 2 MiB 连续内存块", "08e4fa8e"),
    ("自在吟 MV 19 镜 5165 帧 215.25s 定稿", "59fdee0a"),
    ("llm-compressor compressed-tensors 在 vLLM 原生支持吗", "6859b441"),
    ("strata 0.1.40.4 双卡 layer split across GPUs", "9335d44b"),
    ("218 strata 0.1.43.1 升到 0.1.44.1 全过程", "3462e8fb"),
    ("opencode 免费模型调不通 重启路由器 换公网出口 IP 恢复", "e83a096c"),
    ("魔搭 HF 断点续传 状态文件 upload_state", "162c7454"),
    ("Qwen-Image-2.1-Turbo 8 步 CFG 必须 1.0", "1b7390dd"),
    ("Qwen3.5-27B 和 Qwen3.6-27B 架构 64 层 48 层", "94f91246"),
]

RE_Q = re.compile(r"(吗|呢|为什么|怎么|如何|多少|哪|是不是|要不要|能不能|有没有|啥|？|\?)")


def qstyle(s):
    return bool(RE_Q.search(s))


def roles_of(ids):
    c = pg_bridge._conn()
    with c.cursor() as cur:
        cur.execute("select id::text, coalesce(role,''), coalesce(topic,'') "
                    "from boshi.memories where id = ANY(%s::uuid[])",
                    (list(ids),))
        return {r[0]: (r[1], r[2]) for r in cur.fetchall()}


def rank_in(items, gold, n=5):
    ids = [str(x["id"])[:8] for x in items[:n]]
    return ids.index(gold) + 1 if gold in ids else None


# 变体：问句时对 role='user' 加 δ×(池内跨度) 距离惩罚；可选把 topic=conversation 一并罚
VARIANTS = [("base", 0.0, False), ("A_u0.5", 0.5, False), ("A_u1.0", 1.0, False),
            ("A_u1.5", 1.5, False), ("A_uc1.5", 1.5, True), ("A_uc3", 3.0, True)]
stat = {v[0]: {"top1": 0, "top3": 0, "top5": 0} for v in VARIANTS}
detail = {v[0]: [] for v in VARIANTS}

t0 = time.time()
_LOOP = CASES if os.environ.get("A6_NO_RUN") != "1" else []
for q, gold, _anchor in _LOOP:
    pool = pg_bridge.search_memory(q, top_k=20, fusion="auto", scope="all")
    if not pool:
        continue
    rmap = roles_of([x["id"] for x in pool])
    sc = [float(x["score"]) for x in pool]
    span = (max(sc) - min(sc)) or 0.001
    isq = True   # 变体2：不区分问句，全部加角色先验
    for name, delta, also_conv in VARIANTS:
        if delta == 0.0 or not isq:
            adj = list(sc)
        else:
            adj = []
            for x, s in zip(pool, sc):
                role, topic = rmap.get(str(x["id"]), ("", ""))
                pen = delta * span if role == "user" else 0.0
                if also_conv and topic == "conversation" and role != "assistant":
                    pen += 0.5 * delta * span
                adj.append(s + pen)
        order = [x for _, x in sorted(zip(adj, range(len(pool))),
                                      key=lambda t: (t[0], t[1]))]
        order = [pool[i] for i in sorted(range(len(pool)), key=lambda i: (adj[i], i))]
        r = rank_in(order, gold)
        if r == 1:
            stat[name]["top1"] += 1
        if r and r <= 3:
            stat[name]["top3"] += 1
        if r and r <= 5:
            stat[name]["top5"] += 1
        if r != rank_in(pool, gold):
            detail[name].append((gold, rank_in(pool, gold), r, q[:28]))

_RUN = os.environ.get("A6_NO_RUN") != "1"

if os.environ.get("A6_NO_RUN") == "1":
    raise SystemExit(0)
n = len(CASES)
print(f"question-style: {sum(1 for c in CASES if qstyle(c[0]))}/{n}   {time.time()-t0:.0f}s")
for name, delta, also in VARIANTS:
    s = stat[name]
    print(f"{name:6s} δ={delta} conv={also!s:5s}  top1={s['top1']:2d}/{n} ({s['top1']/n:.0%})  "
          f"top3={s['top3']:2d}  top5={s['top5']:2d}")
for name in detail:
    if detail[name]:
        print(f"\n[{name}] 名次变化 {len(detail[name])} 条:")
        for g, b, a, qq in detail[name]:
            print(f"   {g}  {b}→{a}  {qq}")

with open(os.path.join(HERE, "logs", "a6_roleprior_all.json"), "w", encoding="utf-8") as f:
    json.dump({"ts": time.strftime("%F %T"), "n": n, "stat": stat,
               "detail": {k: v for k, v in detail.items()}}, f, ensure_ascii=False, indent=1)
