#!/usr/bin/env python3
"""
代码质量趋势追踪 — 每次分析后记录指标到 CSV，下次自动对比退化
"""
import csv
import json
import os
import subprocess
from datetime import datetime, timezone
from pathlib import Path

METRICS_DIR = Path.home() / ".boshi" / "tools" / "code-analysis" / ".metrics"
HISTORY_CSV = METRICS_DIR / "history.csv"
REPORT_FILE = METRICS_DIR / "latest_report.md"

# 退化阈值（可调）
THRESHOLDS = {
    "issues": 5,           # issues 增加超过5个视为退化
    "avg_complexity": 0.3, # 平均复杂度上升超过0.3视为退化
    "high_severity": 1,    # 高危问题增加1个以上视为退化
}


def _get_git_commit(project_dir):
    try:
        r = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            capture_output=True, text=True, cwd=project_dir, timeout=5
        )
        if r.returncode == 0:
            return r.stdout.strip()
    except Exception:
        pass
    return "unknown"


def run_ruff(project_dir):
    """跑 ruff 扫描，返回 issues 列表"""
    try:
        r = subprocess.run(
            ["ruff", "check", "--select=E,F,W,N,UP,S", "--ignore=E501",
             "--output-format=json", str(project_dir)],
            capture_output=True, text=True, timeout=60
        )
        if r.stdout.strip():
            return json.loads(r.stdout)
    except (json.JSONDecodeError, subprocess.TimeoutExpired, FileNotFoundError):
        pass
    return []


def run_radon(project_dir):
    """跑 radon 复杂度分析"""
    try:
        r = subprocess.run(
            ["radon", "cc", str(project_dir), "--json", "--min", "A"],
            capture_output=True, text=True, timeout=60
        )
        if r.stdout.strip():
            return json.loads(r.stdout)
    except (json.JSONDecodeError, subprocess.TimeoutExpired, FileNotFoundError):
        pass
    return {}


def run_pip_audit(project_dir):
    """跑 pip-audit 依赖安全扫描"""
    req_file = os.path.join(project_dir, "requirements.txt")
    if not os.path.exists(req_file):
        return []
    try:
        r = subprocess.run(
            ["pip-audit", "-r", req_file, "--json"],
            capture_output=True, text=True, timeout=60
        )
        if r.stdout.strip():
            data = json.loads(r.stdout)
            return data.get("vulnerabilities", [])
    except Exception:
        pass
    return []


def extract_metrics(project_dir):
    """提取所有指标"""
    print("🔍 扫描 Ruff 问题...")
    ruff_issues = run_ruff(project_dir)

    print("🔍 分析复杂度...")
    radon_data = run_radon(project_dir)

    print("🔍 检查依赖安全...")
    vulns = run_pip_audit(project_dir)

    # 统计
    total_issues = len(ruff_issues)
    errors = len([i for i in ruff_issues if i.get("code", "").startswith("E")])
    warnings = len([i for i in ruff_issues if i.get("code", "").startswith("W")])
    security = len([i for i in ruff_issues if i.get("code", "").startswith("S")])

    # 复杂度统计
    complexities = []
    for file_funcs in radon_data.values():
        for item in file_funcs:
            complexities.append(item.get("complexity", 0))
    avg_complexity = round(sum(complexities) / len(complexities), 2) if complexities else 0
    max_complexity = max(complexities) if complexities else 0
    c_grade_up = len([c for c in complexities if c >= 11])  # C级及以上

    # 高危漏洞
    high_vulns = len([v for v in vulns if v.get("severity", "").lower() in ("high", "critical")])

    return {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "commit": _get_git_commit(project_dir),
        "issues": total_issues,
        "errors": errors,
        "warnings": warnings,
        "security_issues": security,
        "avg_complexity": avg_complexity,
        "max_complexity": max_complexity,
        "complex_c_funcs": c_grade_up,
        "dep_vulns": len(vulns),
        "high_severity": high_vulns,
    }


def append_history(metrics):
    METRICS_DIR.mkdir(parents=True, exist_ok=True)
    fieldnames = list(metrics.keys())
    file_exists = HISTORY_CSV.exists()

    with open(HISTORY_CSV, "a", newline="", encoding='utf-8') as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        if not file_exists:
            writer.writeheader()
        writer.writerow(metrics)


def compare_regression(metrics):
    """与上一次记录对比，返回退化报告"""
    if not HISTORY_CSV.exists():
        return "🆕 首次记录，无历史对比数据。"

    with open(HISTORY_CSV, encoding='utf-8') as f:
        rows = list(csv.DictReader(f))

    if len(rows) < 2:
        return "📊 只有一条历史记录，暂无法对比趋势。"

    prev = rows[-2]
    changes = []

    for key in ["issues", "avg_complexity", "high_severity", "dep_vulns"]:
        if key not in metrics or key not in prev:
            continue
        cur_val = float(metrics[key])
        prev_val = float(prev[key])
        delta = cur_val - prev_val
        threshold = THRESHOLDS.get(key, 0)

        if delta > threshold:
            changes.append(f"🔴 **{key}** 恶化: {prev_val:.1f} → {cur_val:.1f} (+{delta:.1f})")
        elif delta < -threshold:
            changes.append(f"🟢 **{key}** 改善: {prev_val:.1f} → {cur_val:.1f} ({delta:.1f})")

    if not changes:
        return "✅ 所有指标稳定，无明显退化。"

    return "\n".join(["### ⚠️ 质量变化", ""] + changes)


def generate_report(metrics, regression):
    """生成 Markdown 报告"""
    lines = [
        f"# 代码质量分析报告",
        f"**时间**: {metrics['timestamp'][:19]}",
        f"**提交**: {metrics['commit']}",
        "",
        "## 指标",
        f"| 指标 | 值 |",
        f"|------|----|",
        f"| Ruff 问题总数 | {metrics['issues']} |",
        f"| 其中错误(E) | {metrics['errors']} |",
        f"| 其中警告(W) | {metrics['warnings']} |",
        f"| 安全问题(S) | {metrics['security_issues']} |",
        f"| 平均复杂度 | {metrics['avg_complexity']} |",
        f"| 最大复杂度 | {metrics['max_complexity']} |",
        f"| 复杂函数(C级以上) | {metrics['complex_c_funcs']} |",
        f"| 依赖漏洞 | {metrics['dep_vulns']} |",
        f"| 高危问题 | {metrics['high_severity']} |",
        "",
        regression,
        "",
        "---",
        f"*报告由 trend_tracker.py 自动生成*",
    ]
    return "\n".join(lines)


def main(project_dir):
    print(f"📊 分析项目: {project_dir}\n")
    metrics = extract_metrics(project_dir)
    append_history(metrics)
    regression = compare_regression(metrics)
    report = generate_report(metrics, regression)

    METRICS_DIR.mkdir(parents=True, exist_ok=True)
    with open(REPORT_FILE, 'w', encoding='utf-8') as f:
        f.write(report)

    print("\n" + "=" * 50)
    print(report)
    print(f"\n📁 报告已保存: {REPORT_FILE}")
    print(f"📁 历史数据: {HISTORY_CSV}")


if __name__ == "__main__":
    import sys
    target = sys.argv[1] if len(sys.argv) > 1 else "."
    main(target)
