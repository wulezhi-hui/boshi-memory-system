"""
项目日志入库 — 从 md 文件提取 → ChromaDB 索引 + 关联边
"""
import sys, os, time
sys.path.insert(0, os.path.expanduser("~/.boshi"))

from chroma_bridge import add_memory, search_memory
from knowledge_graph import add_relation

now = time.time()

# ── 项目定义 ──
projects = {
    "伯仕记忆系统": {
        "status": "active",
        "description": "基于 ChromaDB+Hermes 的本地 AI 记忆系统，四层架构（热温冷全量）",
        "docs": [
            "C:/Users/Administrator/Desktop/伯仕技术文档/伯仕工作日志.md",
            "C:/Users/Administrator/Desktop/伯仕技术文档/伯仕记忆系统v5.7_技术架构文档.md",
            "C:/Users/Administrator/Desktop/伯仕技术文档/伯仕能力自检清单.md",
            "C:/Users/Administrator/.boshi/chroma_bridge.py",
            "C:/Users/Administrator/.boshi/knowledge_graph.py",
            "C:/Users/Administrator/.boshi/user_profile.py",
            "C:/Users/Administrator/AppData/Local/hermes/plugins/boshi/__init__.py",
        ],
        "work_logs": [
            {"date": "2026-06-11", "action": "Supermemory借鉴P0-P2完成", "detail": "版本链/不可变日志 + 用户画像层 + 混合搜索 + auto_forget + 知识图谱关系"},
            {"date": "2026-06-11", "action": "CN Desktop污染修复", "detail": "补齐10587条isLatest+2101条type，重建ChromaDB collection，where过滤恢复"},
            {"date": "2026-06-10", "action": "ChromaDB索引修复+hot.json清理", "detail": "8227条metadata损坏，逐批清理重建，全量恢复"},
            {"date": "2026-06-09", "action": "Hermes升级恢复", "detail": "tiered_memory.py恢复到v3.0"},
            {"date": "2026-06-01", "action": "hot.json彻底迁移", "detail": "_save_hot改批量写ChromaDB，1446条迁移"},
            {"date": "2026-06-01", "action": "时间戳归一化修复", "detail": "_normalize_metadata统一float时间戳"},
            {"date": "2026-05-30", "action": "ChromaDB Rust崩溃恢复", "detail": "HNSW compactor段错误，改分批30条+2秒间隔"},
            {"date": "2026-05-26", "action": "v4.0统一Chroma存储", "detail": "三套存储统一为纯Chroma"},
            {"date": "2026-05-23", "action": "sync_turn实时写入+prefetch2天", "detail": "每轮对话即时写ChromaDB，跨入口扩展"},
        ],
    },
    "虚拟寺院项目": {
        "status": "paused",
        "description": "UE5+PCG程序化生成虚拟寺院，AI僧人，古建筑技术调研",
        "docs": [
            "D:/ObsidianVault/虚拟寺院知识库/",
        ],
        "work_logs": [
            {"date": "2026-06-11", "action": "知识库回顾", "detail": "盘点虚拟寺院知识库+Understand Anything工具调研安装"},
            {"date": "2026-06-10", "action": "Supermemory调研对照", "detail": "分析Supermemory架构对虚拟寺院项目的借鉴意义"},
        ],
    },
    "书库整理工具": {
        "status": "paused",
        "description": "D盘书库批量分类整理工具，AI分类+路径校验+去重",
        "docs": [
            "D:/书库整理工具/",
        ],
        "work_logs": [
            {"date": "2026-06-11", "action": "架构文档盘点", "detail": "书库整理工具完整架构文档，4837行主程序+1020行引擎"},
        ],
    },
    "Supermemory借鉴项目": {
        "status": "active",
        "description": "从Supermemory架构借鉴，升级伯仕记忆系统的版本链/画像/图谱/自动遗忘能力",
        "docs": [
            "C:/Users/Administrator/Desktop/Supermemory.txt",
            "C:/Users/Administrator/Desktop/Supermemory分析.txt",
            "C:/Users/Administrator/Desktop/Supermemory项目深度解析.txt",
        ],
        "work_logs": [
            {"date": "2026-06-11", "action": "P0-P2全部完成", "detail": "版本链+deprecate+user_profile+hybrid_search+auto_forget+knowledge_graph，28/28测试通过"},
            {"date": "2026-06-11", "action": "R18流程建立", "detail": "Ruff扫描→独立审查→功能测试→修复验证，写入boshi-work-rules"},
            {"date": "2026-06-10", "action": "深度调研", "detail": "13章完整对比分析，三层架构模型（记忆引擎+用户画像+混合搜索）"},
        ],
    },
}

# ── 写入 ──
added_registry = 0
added_logs = 0
added_relations = 0

for proj_name, proj_data in projects.items():
    print(f"\n📂 {proj_name} ({proj_data['status']})")

    # 1. 项目注册表
    existing_reg = search_memory(proj_name, top_k=1, where={"type": "project_registry", "project": proj_name})
    if existing_reg and existing_reg[0].get("score", 0) < 0.5:
        print(f"  已存在注册，跳过")
    else:
        reg_id = f"project_registry_{proj_name.replace(' ','_')}"
        add_memory(
            content=f"{proj_name}: {proj_data['description']}",
            metadata={
                "type": "project_registry",
                "project": proj_name,
                "status": proj_data["status"],
                "description": proj_data["description"][:200],
                "doc_count": len(proj_data["docs"]),
                "log_count": len(proj_data["work_logs"]),
                "tier": "hot",
                "heat": 50.0,
                "created_at": now,
            },
            memory_id=reg_id,
        )
        added_registry += 1
        print(f"  注册表 ✅")

    # 2. 文档索引（type=doc_reference，正文留md文件）
    for doc_path in proj_data["docs"]:
        doc_name = os.path.basename(doc_path.rstrip("/\\"))
        doc_id = f"doc_ref_{proj_name.replace(' ','_')}_{doc_name.replace('.','_')[:40]}"
        existing_doc = search_memory(doc_id, top_k=1, where={"type": "doc_reference"})
        if existing_doc and existing_doc[0].get("score", 0) < 0.3:
            continue
        add_memory(
            content=f"[{proj_name}] {doc_name} → {doc_path}",
            metadata={
                "type": "doc_reference",
                "project": proj_name,
                "doc_name": doc_name,
                "doc_path": doc_path,
                "tier": "warm",
                "heat": 15.0,
                "created_at": now,
            },
            memory_id=doc_id,
        )

    # 3. 工作日志条目
    for log in proj_data["work_logs"]:
        log_id = f"project_log_{proj_name.replace(' ','_')}_{log['date']}_{log['action'][:20].replace(' ','_')}"
        existing_log = search_memory(log_id[:60], top_k=1, where={"type": "project_log", "project": proj_name})
        if existing_log and existing_log[0].get("score", 0) < 0.3:
            continue

        add_memory(
            content=f"[{proj_name}] {log['date']} {log['action']}: {log['detail']}",
            metadata={
                "type": "project_log",
                "project": proj_name,
                "date": log["date"],
                "action_type": log["action"][:30],
                "tier": "warm",
                "heat": 20.0,
                "created_at": now,
            },
            memory_id=log_id,
        )
        added_logs += 1

        # 4. 关联边：project_log → project_registry
        add_relation(
            from_id=log_id,
            to_id=f"project_registry_{proj_name.replace(' ','_')}",
            rel_type="related",
            reason=f"{log['date']} {log['action']}",
            confidence=0.8,
            metadata={
                "entity_a": f"log:{log['action'][:20]}",
                "entity_b": f"project:{proj_name}",
                "source": "manual_index",
            },
        )
        added_relations += 1

    print(f"  文档索引: {len(proj_data['docs'])} 个 | 工作日志: {len(proj_data['work_logs'])} 条")

# ── 5. 跨项目关联边 ──
# 伯仕记忆系统 ↔ Supermemory借鉴
add_relation("project_registry_伯仕记忆系统", "project_registry_Supermemory借鉴项目",
             "extends", "Supermemory架构借鉴升级伯仕记忆系统", 0.9)
add_relation("project_registry_虚拟寺院项目", "project_registry_伯仕记忆系统",
             "related", "虚拟寺院知识库由伯仕记忆系统管理", 0.7)
add_relation("project_registry_书库整理工具", "project_registry_伯仕记忆系统",
             "related", "书库整理工具由伯仕+小乐协作开发", 0.7)
added_relations += 3

# ── 6. work_log 汇总（给 boshi_search 搜到） ──
add_memory(
    content=f"伯仕工作日志（4个项目，共{sum(len(p['work_logs']) for p in projects.values())}条记录）详见 C:/Users/Administrator/Desktop/伯仕技术文档/伯仕工作日志.md",
    metadata={
        "type": "work_log",
        "source": "manual_index",
        "tier": "warm",
        "heat": 30.0,
        "created_at": now,
    },
    memory_id="work_log_summary_20260611",
)

print(f"\n{'='*40}")
print(f"入库完成:")
print(f"  项目注册表: {added_registry} 个")
print(f"  工作日志: {added_logs} 条")
print(f"  关联边: {added_relations} 条")
print(f"{'='*40}")
