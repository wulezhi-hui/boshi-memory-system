# -*- coding: utf-8 -*-
"""开闸风险探针：auto 门关掉的动机是「BM25 不辨否定」（源码注释：实测把
「不是佛教微调，是通用的」顶到第一）。弱证据降权只在主 token df=0 时生效，
**否定句的主 token 是会命中的**，所以这个风险没有被 C 方案覆盖。

这里用少量否定句看：门关（auto=纯向量）vs 门开（score=向量+关键词）
分别把什么顶到第一。人工判读哪个更对。
"""
import os
import sys

HERE = r"C:/Users/wulezhi/.boshi"
sys.path.insert(0, HERE)
os.chdir(HERE)
import pg_bridge

QS = [
    "不是硬盘的问题，是群晖系统提示存储池损毁怎么办",
    "别管显存不足，我问的是代理连不上外网",
    "不是歌词整理那件事，是分镜怎么排",
    "不用改模型，是问 ComfyUI 里节点下拉找不到",
]
for q in QS:
    print("=" * 78)
    print("Q:", q, " anchor=", pg_bridge._has_anchor(q))
    for mode in ("auto", "score"):
        items = pg_bridge.search_memory(q, top_k=3, fusion=mode, scope="all")
        print(f"  [{mode}]")
        for i, it in enumerate(items, 1):
            txt = (it.get("content") or "").replace("\n", " ")[:58]
            print(f"   {i}. {str(it['id'])[:8]} ch={','.join(it.get('channels') or [])} "
                  f"fused={it.get('fused')} {txt}")
