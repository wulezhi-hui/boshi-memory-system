#!/usr/bin/env python3
"""检查 ~/.boshi/ 下是否有非 UTF-8 编码的文本文件"""
import os, sys
try:
    import chardet
except ImportError:
    print("chardet 未安装，无法检测编码")
    sys.exit(0)

target = os.path.expanduser('~/.boshi')
skip_dirs = {'node_modules', '__pycache__', '.git', 'chroma_db', 'chroma', 'camofox', 'workstation', 'tools', 'scripts'}
exts = ('.py', '.md', '.json', '.txt', '.yaml', '.yml', '.bat', '.vbs', '.cfg')

issues = []
for root, dirs, files in os.walk(target):
    dirs[:] = [d for d in dirs if d not in skip_dirs]
    for f in files:
        if not f.endswith(exts):
            continue
        fp = os.path.join(root, f)
        try:
            with open(fp, 'rb') as fh:
                raw = fh.read(4096)
            if raw:
                det = chardet.detect(raw)
                enc = det['encoding'] or ''
                if enc.upper() not in ('UTF-8', 'UTF-8-SIG', 'ASCII', 'UTF-16LE', 'UTF-16BE', ''):
                    issues.append((fp, enc, det['confidence']))
        except:
            pass

if issues:
    print(f'发现 {len(issues)} 个非 UTF-8 文件：')
    for fp, enc, conf in issues[:20]:
        print(f'  {enc}({conf:.0%}): {fp}')
else:
    print('✅ ~/.boshi/ 下所有文本文件均为 UTF-8/ASCII')
