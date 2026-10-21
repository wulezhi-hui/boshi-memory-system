"""B 方案调研（零模型共现桥）：4 条池外 + 4 条差一点点的 gold，与 paraphrase 之间
是否存在「语料内共现桥」（query 词 t 与 gold 词 b 在其它文档里同现过）。

只读：只查库，不改任何东西。
"""
import sys, json, collections
sys.path.insert(0, r'C:/Users/wulezhi/.boshi')
import pg_bridge as P

TARGETS = [
    ('d13aed83', '给一位尊者做的四视角人物图这次评了多少分'),
    ('a457b574', '旧卡配置不够时换参数还有救吗'),
    ('bc695685', '人物三视图在做成视频前第一步该做什么'),
    ('3f1be78e', '那个影片成品总长多少秒'),
    ('0d28d510', '注解排版里哪些部分该加粗'),
    ('89a147e0', '删掉计划任务为什么程序还在运行'),
    ('f6c71ce8', '系统升级内核那一步什么时候重启比较好'),
    ('f9b2f45f', '明明要求别加花纹画面上却多出雕刻纹理'),
]

c = P._conn()
cond, params = P._where_sql({'isLatest': True}, 'all', 'default', False)
ent = P._kw_index(c, cond, params)
docs, postings = ent['docs'], ent['postings']
print('docs:', len(docs), 'vocab:', ent['vocab'], 'avgdl:', round(ent['avgdl'], 1))

by_prefix = {}
for idx, row in enumerate(docs):
    by_prefix.setdefault(str(row[0])[:8], []).append(idx)

toksets = [set(P._doc_tokens(row[4])) for row in docs]
df = {t: len(v) for t, v in postings.items()}

report = {}
for prefix, q in TARGETS:
    idxs = by_prefix.get(prefix) or []
    if not idxs:
        report[prefix] = {'error': 'gold not found by prefix'}
        continue
    gi = idxs[0]
    gold_content = docs[gi][1]
    gtoks = toksets[gi]
    qtoks = [t for t, w in P._kw_tokens(q)]
    direct = [t for t in qtoks if t in gtoks]
    miss = [t for t in qtoks if t not in gtoks]

    bridges = []
    for t in miss:
        if t not in postings:
            bridges.append({'q': t, 'df': 0, 'how': 'query token 不在词表'})
            continue
        owners = [i for i, _f in postings[t]]
        cnt = collections.Counter()
        for i in owners:
            for g in toksets[i]:
                if g in gtoks and len(g) >= 2:
                    cnt[g] += 1
        top = cnt.most_common(5)
        bridges.append({
            'q': t, 'df': len(owners), 'how': 'co-doc',
            'top': [(g, n, round(n / max(1, len(owners)), 3)) for g, n in top],
        })
    report[prefix] = {
        'q': q, 'gold_dl': docs[gi][3],
        'gold_head': gold_content[:260].replace('\n', ' '),
        'direct': direct, 'miss': miss, 'bridges': bridges,
    }

out = json.dumps(report, ensure_ascii=False, indent=1)
print(out[:14000])
with open(r'C:/Users/wulezhi/.boshi/logs/b_bridge_probe.json', 'w', encoding='utf-8') as f:
    f.write(out)
