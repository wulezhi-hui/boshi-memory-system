#!/usr/bin/env python3
"""
安全扫描集成 — ruff (S规则) + bandit (深度) + pip-audit (依赖)
输出统一的 JSON 格式安全报告
"""
import json
import os
import subprocess
import sys
from pathlib import Path


def run_ruff_safety(project_dir):
    """Ruff 安全规则扫描（超快）"""
    print("  🛡️  Ruff 安全扫描...")
    try:
        r = subprocess.run(
            ["ruff", "check", "--select=S,PTH", "--ignore=E501",
             "--output-format=json", str(project_dir)],
            capture_output=True, text=True, timeout=60
        )
        if r.stdout.strip():
            issues = json.loads(r.stdout)
            return [{
                "tool": "ruff",
                "rule": i.get("code", ""),
                "file": i.get("filename", ""),
                "line": i.get("location", {}).get("row", 0),
                "message": i.get("message", ""),
                "severity": "high" if any(s in i.get("code", "")
                                          for s in ["S1", "S6"]) else "medium",
            } for i in issues]
    except Exception as e:
        return [{"tool": "ruff", "error": str(e)}]
    return []


def run_bandit(project_dir):
    """Bandit 源码安全扫描"""
    print("  🛡️  Bandit 深度扫描...")
    try:
        r = subprocess.run(
            ["bandit", "-r", str(project_dir), "-f", "json"],
            capture_output=True, text=True, timeout=120
        )
        if r.stdout.strip():
            data = json.loads(r.stdout)
            results = []
            for issue in data.get("results", []):
                sev = issue.get("issue_severity", "LOW").lower()
                results.append({
                    "tool": "bandit",
                    "rule": issue.get("test_id", ""),
                    "file": issue.get("filename", ""),
                    "line": issue.get("line_number", 0),
                    "message": issue.get("issue_text", ""),
                    "severity": sev,
                })
            return results
    except Exception as e:
        return [{"tool": "bandit", "error": str(e)}]
    return []


def run_pip_audit(project_dir):
    """pip-audit 依赖漏洞扫描"""
    req_file = os.path.join(project_dir, "requirements.txt")
    if not os.path.exists(req_file):
        print("  ℹ️  未找到 requirements.txt，跳过依赖扫描")
        return []

    print("  🛡️  依赖漏洞扫描...")
    try:
        r = subprocess.run(
            ["pip-audit", "-r", req_file, "--json"],
            capture_output=True, text=True, timeout=120
        )
        if r.stdout.strip():
            data = json.loads(r.stdout)
            vulns = data.get("vulnerabilities", [])
            return [{
                "tool": "pip-audit",
                "rule": v.get("id", ""),
                "file": v.get("package", {}).get("name", ""),
                "line": 0,
                "message": f"{v.get('package', {}).get('name', '')}@{v.get('package', {}).get('version', '')}: {v.get('description', '')[:200]}",
                "severity": v.get("severity", "medium").lower(),
            } for v in vulns]
    except Exception as e:
        return [{"tool": "pip-audit", "error": str(e)}]
    return []


def scan(project_dir):
    """全量安全扫描"""
    report = {
        "project": project_dir,
        "timestamp": __import__('datetime').datetime.now().isoformat(),
        "findings": [],
        "summary": {},
    }

    findings = []
    findings.extend(run_ruff_safety(project_dir))
    findings.extend(run_bandit(project_dir))
    findings.extend(run_pip_audit(project_dir))
    report["findings"] = findings

    # 汇总
    by_severity = {}
    by_tool = {}
    for f in findings:
        sev = f.get("severity", "unknown")
        by_severity[sev] = by_severity.get(sev, 0) + 1
        tool = f.get("tool", "unknown")
        by_tool[tool] = by_tool.get(tool, 0) + 1

    report["summary"] = {
        "total": len(findings),
        "by_severity": by_severity,
        "by_tool": by_tool,
    }

    return report


def print_report(report):
    """友好输出报告"""
    s = report["summary"]
    print(f"\n📋 安全扫描报告")
    print(f"   项目: {report['project']}")
    print(f"   时间: {report['timestamp'][:19]}")
    print(f"   总发现: {s['total']} 个")
    print(f"   按严重度: {s.get('by_severity', {})}")
    print(f"   按工具: {s.get('by_tool', {})}")

    if report["findings"]:
        print(f"\n   详情:")
        for f in report["findings"]:
            if "error" in f:
                print(f"     ❌ [{f['tool']}] {f['error']}")
            else:
                emoji = {"high": "🔴", "medium": "🟡", "low": "ℹ️", "critical": "🔥"}
                e = emoji.get(f['severity'], '•')
                print(f"     {e} [{f['tool']}/{f['rule']}] {f['message'][:120]}")
                print(f"        {f['file']}:{f['line']}")


if __name__ == "__main__":
    target = sys.argv[1] if len(sys.argv) > 1 else "."
    report = scan(target)
    print_report(report)

    # 保存 JSON
    out_dir = Path.home() / ".boshi" / "tools" / "code-analysis" / ".cache"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_file = out_dir / "security_report.json"
    with open(out_file, 'w', encoding='utf-8') as f:
        json.dump(report, f, ensure_ascii=False, indent=2)
    print(f"\n📁 JSON 报告已保存: {out_file}")
