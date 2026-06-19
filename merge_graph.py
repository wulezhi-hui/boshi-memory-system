#!/usr/bin/env python3
"""合并所有batch-*.json为assembled-graph.json"""
import json, os, glob

INTERMEDIATE = r"D:\书库整理工具\.understand-anything\intermediate"

# 读取所有 batch 文件
all_nodes = []
all_edges = []
seen_ids = set()
seen_edges = set()

for fpath in sorted(glob.glob(os.path.join(INTERMEDIATE, "batch-*.json"))):
    with open(fpath, 'r', encoding='utf-8') as f:
        data = json.load(f)
    
    # 统一格式：支持 {"nodes":[...], "edges":[...]} 和直接列表
    nodes = data.get("nodes", data if isinstance(data, list) else [])
    edges = data.get("edges", [])
    
    for node in (nodes if isinstance(nodes, list) else []):
        nid = node.get("id")
        if nid and nid not in seen_ids:
            seen_ids.add(nid)
            all_nodes.append(node)
    
    for edge in (edges if isinstance(edges, list) else []):
        key = (edge.get("source"), edge.get("target"), edge.get("type"))
        if key not in seen_edges:
            seen_edges.add(key)
            all_edges.append(edge)

# 添加主程序节点
main_node = {
    "id": "file:书库整理分类.pyw",
    "type": "file",
    "name": "书库整理分类.pyw",
    "filePath": "书库整理分类.pyw",
    "summary": "书库整理工具主程序 — GUI + 业务逻辑编排，5118行单体应用",
    "tags": ["gui", "tkinter", "main", "entry-point"],
    "complexity": "high"
}
if main_node["id"] not in seen_ids:
    all_nodes.append(main_node)

graph = {
    "version": "1.0.0",
    "project": {
        "name": "书库整理工具",
        "languages": ["Python", "JSON", "Markdown", "SQLite"],
        "frameworks": ["tkinter"],
        "description": "使用AI对书籍进行自动分类整理的桌面工具",
        "analyzedAt": "2026-06-12T00:30:00+08:00"
    },
    "nodes": all_nodes,
    "edges": all_edges,
    "layers": [
        {
            "id": "layer:main-gui",
            "name": "主程序层",
            "description": "GUI主程序入口",
            "nodeIds": ["file:书库整理分类.pyw"]
        },
        {
            "id": "layer:classification-engine",
            "name": "分类引擎层",
            "description": "核心分类逻辑引擎和精细分类器",
            "nodeIds": ["file:classify_engine.py", "file:fine_classifier.py"]
        },
        {
            "id": "layer:fine-sorting",
            "name": "精细整理层",
            "description": "基于AI的精细整理引擎子包",
            "nodeIds": [
                "file:精细整理引擎/__init__.py",
                "file:精细整理引擎/classifier.py",
                "file:精细整理引擎/pipeline.py"
            ]
        },
        {
            "id": "layer:text-extraction",
            "name": "文本提取层",
            "description": "多格式文本提取模块",
            "nodeIds": [
                "file:精细整理引擎/extract_pdf.py",
                "file:精细整理引擎/extract_ebook.py",
                "file:精细整理引擎/extract_chm.py",
                "file:精细整理引擎/extract_office.py",
                "file:精细整理引擎/extract_text.py"
            ]
        },
        {
            "id": "layer:database",
            "name": "数据层",
            "description": "SQLite数据库封装",
            "nodeIds": ["file:book_db.py"]
        },
        {
            "id": "layer:utilities",
            "name": "工具层",
            "description": "辅助工具库和配置",
            "nodeIds": ["file:book_classifier_lib.py", "file:build_clc_database.py"]
        }
    ]
}

output_path = os.path.join(INTERMEDIATE, "assembled-graph.json")
with open(output_path, 'w', encoding='utf-8') as f:
    json.dump(graph, f, ensure_ascii=False, indent=2)

print(f"✅ 合并完成!")
print(f"   节点: {len(all_nodes)}")
print(f"   边: {len(all_edges)}")
print(f"   输出: {output_path}")
print(f"   大小: {os.path.getsize(output_path)} bytes")