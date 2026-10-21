"""B 方案调研：4 条池外 + 4 条差一点点的 gold 正文 vs paraphrase 用词差异。

目的：判断「库内共现同义表」有没有可用素材——即 query 里的词与 gold 正文里的词
是否在语料其它文档里共现过（能搭桥）。
"""
import sys, json, re
sys.path.insert(0, r'C:/Users/wulezhi/.boshi')
import pg_bridge as P

TARGETS = {
    'd13aed83': '给一位尊者做的四视角人物图这次评了多少分',
    'a457b574': '旧卡配置不够时换参数还有救吗',
    'bc695685': '人物三视图在做成视频前第一步该做什么',
    '3f1be78e': '那个影片成品总长多少秒',
    '0d28d510': '注解排版里哪些部分该加粗',
    '89a147e0': '删掉计划任务为什么程序还在运行',
    'f6c71ce8': '系统升级内核那一步什么时候重启比较好',
    'f9b2f45f': '明明要求别加花纹画面上却多出雕刻纹理',
}

c = P._conn()
ids = []
for g in TARGETS:
    ids.append(g)
# 用 LIKE 找完整 id
rows = P._q(c, "SELECT id, content, topic, created_at FROM boshi.memories WHERE is_latest = true") \
    if hasattr(P, '_q') else None
print("has _q:", rows is not None)
