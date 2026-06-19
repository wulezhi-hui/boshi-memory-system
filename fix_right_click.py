"""修改书库整理工具的右键菜单和相关功能"""
import shutil, os

target = r"D:\书库整理工具\书库整理分类.pyw"
backup = target + ".bak"

# 先备份
shutil.copy2(target, backup)
print(f"✅ 已备份到 {backup}")

with open(target, 'r', encoding='utf-8') as f:
    content = f.read()

# 1. 修改文件级右键：_reclassify_file → _reclassify_files([p])
old = 'menu.add_command(label="🔄 重新分类", command=lambda p=path: self._reclassify_file(p))'
new = 'menu.add_command(label="🔄 重新分类", command=lambda p=path: self._reclassify_files([p]))'
if old in content:
    content = content.replace(old, new, 1)
    print("✅ 1. 文件右键：_reclassify_file → _reclassify_files([p])")
else:
    print("❌ 1. 未找到: " + old[:50])

# 2. 修改目录级右键：去掉精细/快速，改为重新分类
old2 = '''                menu.add_separator()
                menu.add_command(
                    label="🔍 精细整理本目录所有书籍",
                    command=lambda d=full_dir: self._reclassify_directory(d, use_quick=False))
                menu.add_command(
                    label="⚡ 快速分类本目录所有书籍",
                    command=lambda d=full_dir: self._reclassify_directory(d, use_quick=True))'''

new2 = '''                menu.add_separator()
                menu.add_command(
                    label="🔄 重新分类本目录所有书籍",
                    command=lambda d=full_dir: self._reclassify_directory(d))'''

if old2 in content:
    content = content.replace(old2, new2, 1)
    print("✅ 2. 目录右键：精细/快速 → 重新分类")
else:
    print("❌ 2. 未找到目录右键菜单")

# 3. 修改 _reclassify_file → _reclassify_files
old3 = '''    def _reclassify_file(self, file_path):
        """对单个文件重新分类（在当前工作模式下）"""
        self.log(f"\\n🔄 重新分类：{os.path.basename(file_path)}")
        self.files_to_process = [file_path]
        thread = Thread(target=self.process_batch)
        thread.daemon = True
        thread.start()'''

new3 = '''    def _reclassify_files(self, file_paths):
        """重新分类（单文件或批量，走精细整理模块）"""
        mode = "单文件" if len(file_paths) == 1 else f"批量({len(file_paths)}本)"
        self.log(f"\\n🔄 {mode}重新分类：{os.path.basename(file_paths[0])}")
        self.files_to_process = list(file_paths)
        thread = Thread(target=self.process_batch)
        thread.daemon = True
        thread.start()'''

if old3 in content:
    content = content.replace(old3, new3, 1)
    print("✅ 3. _reclassify_file → _reclassify_files")
else:
    print("❌ 3. 未找到 _reclassify_file")

# 4. 修改 _reclassify_directory - 去掉 use_quick 参数
old4 = 'def _reclassify_directory(self, dir_path, use_quick=False):\n        """对目录下所有已入库的书籍重新分类"""\n        mode = "⚡ 快速分类" if use_quick else "🔍 精细整理"\n        self.log(f"\\n{mode}：{os.path.basename(dir_path)}")'
new4 = 'def _reclassify_directory(self, dir_path):\n        """对目录下所有已入库的书籍重新分类"""\n        self.log(f"\\n🔄 目录重新分类：{os.path.basename(dir_path)}")'

if old4 in content:
    content = content.replace(old4, new4, 1)
    print("✅ 4. _reclassify_directory：去掉 use_quick")
else:
    print("❌ 4. 未找到 _reclassify_directory")

# 5. 修改 process_batch - 去掉快速路径
old5 = '''            is_fast = self.work_mode.get() == "快速分类"
            max_workers = 3 if is_fast else 1

            mode_label = "⚡ 快速分类（3线程并发）" if is_fast else "🔍 精细整理（单线程）"
            self.log(f"\\n📊 {mode_label}：{len(pending)} 个文件")

            self.root.after(0, lambda p=len(pending): self.progress_label.config(text=f"已处理 0 / 待处理 {p}"))

            if is_fast:
                self._process_batch_ai_classify(pending, temp_dir, output_dir)
            else:
                self._process_batch_sequential(pending, temp_dir, output_dir)'''

new5 = '''            self.log(f"\\n📊 🔍 精细整理：{len(pending)} 个文件")

            self.root.after(0, lambda p=len(pending): self.progress_label.config(text=f"已处理 0 / 待处理 {p}"))

            self._process_batch_sequential(pending, temp_dir, output_dir)'''

if old5 in content:
    content = content.replace(old5, new5, 1)
    print("✅ 5. process_batch：去掉快速路径")
else:
    print("❌ 5. 未找到 process_batch 快速路径")

# 6. 删除 _process_batch_ai_classify
old6 = '''    def _process_batch_ai_classify(self, pending, temp_dir, output_dir):
        """AI批量分类:提取书名 → 拆分压缩/非压缩 → 批量AI → 预处理+移动文件"""
        total = len(pending)
        batch_size = self.batch_size_var.get()
        success = 0
        fail = 0
        uncategorized = 0

        self.root.after(0, lambda t=total: self.progress_label.config(text=f"已处理 0 / 待处理 {t}"))

        # ====== Phase 1: 轻量提取书名,拆分压缩/非压缩 ======
        ai_entries, compressed_entries = self._scan_batch_entries(pending)'''

# Find end of this function - next def at same indentation level
start = content.find(old6)
if start >= 0:
    # Find next top-level def
    rest = content[start + len(old6):]
    next_def = rest.find('\n    def ')
    if next_def > 0:
        end_pos = start + len(old6) + next_def
        content = content[:start] + content[end_pos:]
        print("✅ 6. 删除 _process_batch_ai_classify")
    else:
        print("❌ 6. 找不到函数结尾")
else:
    print("❌ 6. 未找到 _process_batch_ai_classify")

# 7. 删除 _process_batch_concurrent
old7 = '''    def _process_batch_concurrent(self, pending, temp_dir, output_dir, max_workers=3):'''
start = content.find(old7)
if start >= 0:
    rest = content[start + len(old7):]
    next_def = rest.find('\n    def ')
    if next_def > 0:
        end_pos = start + len(old7) + next_def
        content = content[:start] + content[end_pos:]
        print("✅ 7. 删除 _process_batch_concurrent")
    else:
        print("❌ 7. 找不到函数结尾")
else:
    print("❌ 7. 未找到 _process_batch_concurrent")

# 写入
with open(target, 'w', encoding='utf-8') as f:
    f.write(content)

# 验证
with open(target, 'r', encoding='utf-8') as f:
    final = f.read()

verifies = [
    ("_reclassify_files([p])" in final, "文件右键调用正确"),
    ('label="🔄 重新分类本目录所有书籍"' in final, "目录右键菜单正确"),
    ('def _reclassify_files(self, file_paths):' in final, "_reclassify_files 已定义"),
    ('def _reclassify_directory(self, dir_path):' in final, "_reclassify_directory 无 use_quick"),
    ('self._process_batch_sequential(pending' in final, "process_batch 只用精细路径"),
    ('_process_batch_ai_classify' not in final, "_process_batch_ai_classify 已删除"),
    ('_process_batch_concurrent' not in final, "_process_batch_concurrent 已删除"),
    ('_reclassify_file(self, file_path)' not in final, "_reclassify_file 已删除"),
    ('use_quick=True' not in final and 'use_quick=False' not in final, "无残留 use_quick"),
    ('is_fast = self.work_mode' not in final, "is_fast 已删除"),
]

print("\n=== 验证结果 ===")
all_ok = True
for ok, msg in verifies:
    status = "✅" if ok else "❌"
    if not ok:
        all_ok = False
    print(f"  {status} {msg}")

if all_ok:
    print("\n🎉 所有修改成功！")
else:
    print("\n⚠️ 部分验证未通过，请检查")
