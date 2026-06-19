#!/usr/bin/env python3
"""伯仕全身体检 v1 — 身心一体自检"""

import json, os, sqlite3, subprocess, ast, sys

R = []
def ok(m): R.append("  ✅ " + m)
def warn(m): R.append("  ⚠️  " + m)
def fail(m): R.append("  ❌ " + m)

HH = os.path.expanduser("~/AppData/Local/hermes")
CFG = os.path.join(HH, "config.yaml")
CHROMA = os.path.expanduser("~/.boshi/chroma_db/chroma.sqlite3")
PLUGIN = os.path.join(HH, "plugins", "boshi", "__init__.py")
SAFE = "D:/boshi-safeguard"

def yg(path, *keys):
    """yaml get: yg('config.yaml', 'memory', 'provider')"""
    try:
        with open(path) as f:
            ls = f.readlines()
    except:
        return None
    want = len(keys)
    di = -1
    for line in ls:
        s = line.strip()
        if not s or s.startswith("#"):
            continue
        ind = len(line) - len(line.lstrip())
        k = s.split(":")[0].strip()
        if di == -1:
            if ind != 0:
                continue
            if k == keys[0]:
                di = 0
                if want == 1:
                    return s.split(":", 1)[1].strip() if ":" in s else None
        else:
            if ind <= di:
                return None
            if ind == di + 2 and k == keys[len([x for x in [di] if False])]:
                pass
            sub_depth = 0
            tmp_di = di
            for c in range(1, want):
                if ind == tmp_di + 2 and k == keys[c]:
                    if c == want - 1:
                        if ":" in s:
                            return ":".join(s.split(":")[1:]).strip()
                        return None
                    tmp_di = ind
                    sub_depth = c
            if sub_depth:
                di = tmp_di
    return None

def yg2(path, *keys):
    """更简单的 yaml 取值：行号扫描法"""
    try:
        with open(path) as f:
            lines = f.readlines()
    except:
        return None
    # 先找顶层 key
    for i, line in enumerate(lines):
        if line.strip() == keys[0] + ":":
            # 在后续行找子 key
            for j in range(i+1, len(lines)):
                nl = lines[j]
                if not nl.strip() or nl.strip().startswith("#"):
                    continue
                nind = len(nl) - len(nl.lstrip())
                if nind <= 0:
                    break
                nk = nl.strip().split(":")[0].strip()
                if nk == keys[1]:
                    if ":" in nl.strip():
                        val = ":".join(nl.strip().split(":")[1:]).strip()
                        if len(keys) == 2:
                            return val
                        # 有三层，继续
                        for k in range(j+1, len(lines)):
                            kl = lines[k]
                            if not kl.strip():
                                continue
                            kind = len(kl) - len(kl.lstrip())
                            if kind <= nind:
                                break
                            kk = kl.strip().split(":")[0].strip()
                            if kk == keys[2]:
                                if ":" in kl.strip():
                                    return ":".join(kl.strip().split(":")[1:]).strip()
                                return None
                    return None
    return None

print("\n" + "=" * 55)
print("  🩺  伯仕全身体检")
print("=" * 55 + "\n")

print("─── 一、身体健康（Hermes 框架） ───\n")

print("【1.1 关键配置】")
p = yg2(CFG, "memory", "provider")
me = yg2(CFG, "memory", "memory_enabled")
ue = yg2(CFG, "memory", "user_profile_enabled")
ok("memory.provider = boshi") if p == "boshi" else fail(f"memory.provider = {p}")
ok("memory_enabled = false") if me == "false" else fail(f"memory_enabled = {me}")
ok("user_profile_enabled = false") if ue == "false" else fail(f"user_profile_enabled = {ue}")

cm = yg2(CFG, "compression", "model")
cp = yg2(CFG, "compression", "provider")
ok(f"压缩模型 = {cm} / {cp}（本地安全）") if cm == "qwen3:8b" and cp == "ollama" else warn(f"压缩模型 = {cm} / {cp}")

vp = yg2(CFG, "auxiliary", "vision", "provider")
vb = yg2(CFG, "auxiliary", "vision", "base_url")
ok("辅助视觉 = glm-ocr via ollama") if vp == "openai" and vb and "11434" in vb else warn(f"辅助视觉 = {vp} / {vb}")

print("\n【1.2 Gateway】")
try:
    r = subprocess.run(["hermes", "gateway", "list"], capture_output=True, text=True, timeout=10)
    ok("Gateway 运行中") if "✓" in r.stdout else warn("Gateway 状态异常")
except: warn("Gateway 查询失败")

print("\n【1.3 外部依赖】")
try:
    r = subprocess.run(["curl", "-so", "/dev/null", "-w", "%{http_code}", "http://localhost:11434/api/tags"],
                      capture_output=True, text=True, timeout=5)
    ok("Ollama 在线") if r.stdout == "200" else fail("Ollama 无响应")
except: fail("Ollama 不可达")

try:
    r = subprocess.run(["hermes", "config", "show"], capture_output=True, text=True, timeout=10)
    ok("DeepSeek 已配置") if "deepseek" in r.stdout.lower() else warn("DeepSeek 异常")
except: warn("DeepSeek 检查失败")

print("\n【1.4 备份 cron】")
try:
    r = subprocess.run(["hermes", "cron", "list"], capture_output=True, text=True, timeout=10)
    ok("备份 cron 存活") if "chroma_db" in r.stdout else warn("备份 cron 未找到")
except: warn("cron 查询失败")

print("\n【1.5 内置记忆】")
for fp, nm in [(os.path.join(HH, "memories", "MEMORY.md"), "MEMORY.md"),
               (os.path.join(HH, "memories", "USER.md"), "USER.md")]:
    if os.path.exists(fp):
        s = os.path.getsize(fp)
        ok(f"{nm} = {s}B（占位）") if s < 200 else warn(f"{nm} = {s}B（残留）")

print("\n─── 二、灵魂健康（伯仕记忆系统） ───\n")
print("【2.1 ChromaDB】")
if os.path.exists(CHROMA):
    ok(f"ChromaDB 存在（{os.path.getsize(CHROMA)//1024//1024}MB）")
    try:
        c = sqlite3.connect(CHROMA)
        cu = c.cursor()
        ok("完整性 ok") if cu.execute("PRAGMA integrity_check").fetchone()[0] == "ok" else fail("完整性异常")
        e = cu.execute("SELECT COUNT(*) FROM embeddings").fetchone()[0]
        m = cu.execute("SELECT COUNT(*) FROM embedding_metadata").fetchone()[0]
        ok(f"嵌入 {e} / 元数据 {m}")
        o = cu.execute("""SELECT COUNT(*) FROM embedding_metadata m LEFT JOIN embeddings e ON m.id=e.id WHERE e.id IS NULL""").fetchone()[0]
        ok("孤立记录 = 0") if o == 0 else warn(f"孤立 = {o}")
        st = cu.execute("""SELECT COUNT(*) FROM embedding_metadata WHERE key IN ("created_at","last_mentioned","collected_at","updated_at","last_decay") AND string_value IS NOT NULL""").fetchone()[0]
        ok("时间戳全 float ✅") if st == 0 else fail(f"字符串时间戳 {st} ⚠️")
        q = cu.execute("SELECT COUNT(*) FROM embeddings_queue").fetchone()[0]
        ok(f"队列积压 = {q}（正常）") if q < 50 else warn(f"队列积压 = {q}（需关注）") if q < 500 else fail(f"队列积压 = {q} ⚠️")
        c.close()
    except Exception as ex: fail(f"ChromaDB 异常：{ex}")
else: fail("ChromaDB 不存在！")

print("\n【2.2 知识图谱】")
kg = os.path.expanduser("~/.boshi/memory/knowledge_graph.json")
if os.path.exists(kg):
    try:
        json.load(open(kg))
        ok(f"知识图谱 {os.path.getsize(kg)//1024}KB")
    except: fail("知识图谱损坏")
else: warn("知识图谱不存在")

print("\n【2.3 Boshi 插件】")
if os.path.exists(PLUGIN):
    try:
        ast.parse(open(PLUGIN).read())
        ok("语法正确")
        src = open(PLUGIN).read()
        ok("自愈值正确") if '"memory.memory_enabled", "false"' in src else fail("自愈值异常")
    except SyntaxError as ex: fail(f"语法错误：{ex}")
else: fail("插件文件缺失")

print("\n【2.4 时间戳归一化】")
bp = os.path.expanduser("~/.boshi/chroma_bridge.py")
if os.path.exists(bp):
    ok("_normalize_metadata 就绪") if "_normalize_metadata" in open(bp).read() else fail("归一化缺失")
else: fail("chroma_bridge.py 缺失")

print("\n【2.5 备份系统】")
if os.path.exists(os.path.join(SAFE, "deploy.py")):
    ok("备份包存在")
    try:
        r = subprocess.run(["python3", os.path.join(SAFE, "deploy.py"), "--check"], capture_output=True, text=True, timeout=15)
        ok("deploy 自检通过") if "🎉" in r.stdout else warn("deploy 异常")
    except: warn("deploy 执行失败")
else: fail("备份包缺失")
bd = os.path.join(SAFE, "data", "chroma_db", "chroma.sqlite3")
ok(f"备份数据 {os.path.getsize(bd)//1024//1024}MB") if os.path.exists(bd) else warn("备份数据未同步")
sd = os.path.join(SAFE, "snapshots")
if os.path.isdir(sd):
    ss = sorted(os.listdir(sd))
    ok(f"快照 {len(ss)} 个") if ss else warn("快照为空")
else: warn("快照目录缺失")

print("\n【2.6 工作台】")
try:
    r = subprocess.run(["curl", "-so", "/dev/null", "-w", "%{http_code}", "http://127.0.0.1:7681"], capture_output=True, text=True, timeout=3)
    ok("工作台在线") if r.stdout and r.stdout != "000" else warn("工作台未运行")
except: warn("工作台检查失败")

# 报告
print("\n" + "=" * 55)
print("  📋 体检报告")
print("=" * 55)
e = [r for r in R if r.startswith("  ❌")]
w = [r for r in R if r.startswith("  ⚠️")]
print(f"\n  总 {len(R)} 项 | ✅ {len(R)-len(e)-len(w)} | ⚠️ {len(w)} | ❌ {len(e)}")
if e:
    print("\n  🔴 需处理：")
    for x in e: print(f"     {x}")
if w:
    print("\n  🟡 需关注：")
    for x in w: print(f"     {x}")
if not e and not w:
    print("\n  🎉 身心俱佳，无需干预")
elif not e:
    print("\n  🟢 无严重问题")
    
print("\n" + "=" * 55)
# 退出码：0=健康 1=有错误
sys.exit(1 if e else 0)
