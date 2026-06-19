#!/usr/bin/env python3
"""手动生成 batches.json 替代 compute-batches.mjs"""
import json, os

SCAN_PATH = r"D:\书库整理工具\.understand-anything\intermediate\scan-result.json"
BATCH_PATH = r"D:\书库整理工具\.understand-anything\intermediate\batches.json"

with open(SCAN_PATH, 'r', encoding='utf-8') as f:
    scan = json.load(f)

# 提取 Python 代码文件（有 import 关系的）
files = [f for f in scan['files'] if f['type'] == 'code' or f['type'] == 'script']
code_files = [f for f in files if f['path'].endswith('.py')]

# 按功能分组，每个文件单独一个批次（Python项目文件数少，每批1个）
batches = []
for idx, f in enumerate(code_files):
    path = f['path'].replace('\\', '/')
    batches.append({
        "batchIndex": idx,
        "files": [{
            "path": path,
            "language": "Python",
            "sizeLines": f['lines'] or 300,
            "fileCategory": f['type']
        }],
        "batchImportData": {},
        "neighborMap": {}
    })

result = {
    "batches": batches,
    "meta": {
        "totalBatches": len(batches),
        "totalFiles": len(code_files),
        "batchStrategy": "one-per-file"
    }
}

with open(BATCH_PATH, 'w', encoding='utf-8') as f:
    json.dump(result, f, ensure_ascii=False, indent=2)

print(f"✅ batches.json 已生成: {len(batches)} 批次, {len(code_files)} 文件")
for b in batches:
    print(f"  批次 {b['batchIndex']}: {b['files'][0]['path']}")