#!/usr/bin/env python3
"""保存 hot_topics.json 中的 5 个项目到 ChromaDB"""
import sys, os, json
sys.path.insert(0, os.path.expanduser('~/.boshi'))
from chroma_bridge import add_memories_batch

with open(os.path.expanduser('~/.boshi/memory/hot_topics.json')) as f:
    ht = json.load(f)

entries = []
for zone_name, items in [('central', ht.get('central', [])), ('side', ht.get('side', []))]:
    for item in items:
        content = f"项目: {item['title']}\n状态: {item.get('status','')}\n关键点: {' | '.join(item.get('key_points',[]))}\n下一步: {item.get('next','')}"
        meta = {
            'project': item['title'],
            'type': 'project',
            'status': item.get('status', ''),
            'zone': zone_name,
            'created_at': item.get('created', ''),
            'last_mentioned': item.get('last_mentioned', ''),
            'mention_count': item.get('mention_count', 0),
            'source': 'project_archive',
        }
        entries.append({'content': content, 'metadata': meta})

if entries:
    count = add_memories_batch(entries)
    print(f'已保存 {count} 个项目到 ChromaDB ✅')
else:
    print('无项目需要保存')
