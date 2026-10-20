#!/usr/bin/env bash
# deploy.sh — 把开发源插件同步到 Hermes 安装副本（唯一正确方向：dev → installed）
#
# 为什么需要它：2026-10-20 23:11 的 P3.1 补丁丢失事故，根因是「有人直接改安装副本」，
# 下次从开发源部署就被整体覆盖。此后所有部署必须走本脚本，不允许手工 cp。
#
# 用法：
#   ./deploy.sh            # 跑单测 → 备份 → 同步 → 校验
#   ./deploy.sh --check    # 只比对，不写任何文件
#   ./deploy.sh --no-test  # 跳过单测（紧急时用，会打警告）
#   ./deploy.sh --force    # 安装副本存在开发源没有的改动时仍覆盖（默认拒绝）
#
# 退出码：0 成功 / 1 校验失败 / 2 前置条件不满足 / 3 检测到反向漂移被拒绝

set -uo pipefail

SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SRC_PLUGIN="$SRC_DIR/plugins/boshi"

MODE="apply"
RUN_TESTS=1
FORCE=0
for arg in "$@"; do
    case "$arg" in
        --check)   MODE="check" ;;
        --no-test) RUN_TESTS=0 ;;
        --force)   FORCE=1 ;;
        -h|--help) sed -n '2,20p' "${BASH_SOURCE[0]}"; exit 0 ;;
        *) echo "未知参数：$arg（用 --help）" >&2; exit 2 ;;
    esac
done

# ---- 定位安装副本 ----------------------------------------------------------
# Windows(git-bash): %LOCALAPPDATA%\hermes\plugins\boshi
# 其他平台: ~/.hermes/plugins/boshi
resolve_dst() {
    if [ -n "${BOSHI_PLUGIN_DST:-}" ]; then
        echo "$BOSHI_PLUGIN_DST"; return
    fi
    if [ -n "${LOCALAPPDATA:-}" ] && [ -d "$LOCALAPPDATA/hermes/plugins" ]; then
        echo "$LOCALAPPDATA/hermes/plugins/boshi"; return
    fi
    if [ -d "$HOME/.hermes/plugins" ]; then
        echo "$HOME/.hermes/plugins/boshi"; return
    fi
    echo ""
}

DST_PLUGIN="$(resolve_dst)"

echo "== 伯仕插件部署 =="
echo "  源  : $SRC_PLUGIN"
echo "  目标: ${DST_PLUGIN:-<未找到>}"
echo "  模式: $MODE$([ "$RUN_TESTS" = 1 ] || echo '（跳过单测）')"

[ -d "$SRC_PLUGIN" ] || { echo "❌ 开发源不存在：$SRC_PLUGIN" >&2; exit 2; }
[ -n "$DST_PLUGIN" ] && [ -d "$DST_PLUGIN" ] || {
    echo "❌ 未找到安装副本（可用 BOSHI_PLUGIN_DST=<path> 指定）" >&2; exit 2; }

# ---- 1. 反向漂移检测：安装副本里有开发源没有的改动 = 有人在改安装副本 --------------
DRIFT=0
while IFS= read -r rel; do
    [ -n "$rel" ] || continue
    if [ ! -f "$SRC_PLUGIN/$rel" ]; then
        echo "⚠️  安装副本多出文件：$rel"; DRIFT=1
    elif ! diff -q "$SRC_PLUGIN/$rel" "$DST_PLUGIN/$rel" >/dev/null 2>&1; then
        echo "⚠️  内容不一致：$rel"; DRIFT=1
    fi
done < <(cd "$DST_PLUGIN" && find . -type f ! -name '*.bak-*' ! -path './__pycache__/*' | sed 's|^\./||' | sort)

if [ "$DRIFT" = 1 ] && [ "$MODE" = "apply" ] && [ "$FORCE" = 0 ]; then
    echo ""
    echo "❌ 检测到安装副本与开发源不一致（见上）。这可能是「只改安装副本」的补丁，"
    echo "   直接覆盖会丢失它。请先 diff -u 逐个比对，把该留的改动并入开发源，"
    echo "   确认无可留内容后再用 --force 覆盖。" >&2
    exit 3
fi

if [ "$MODE" = "check" ]; then
    echo ""
    if [ "$DRIFT" = 0 ]; then echo "✅ 安装副本与开发源一致"; exit 0; fi
    echo "ℹ️  --check 模式，未写入任何文件"; exit 0
fi

# ---- 2. 单测（全绿才部署） -------------------------------------------------
if [ "$RUN_TESTS" = 1 ]; then
    PY="$SRC_DIR/venv/Scripts/python.exe"
    [ -x "$PY" ] || PY="$SRC_DIR/venv/bin/python"
    [ -x "$PY" ] || PY="python"
    echo ""
    echo "== 单测 =="
    if ! ( cd "$SRC_DIR" && "$PY" -m unittest discover -s tests -p "test_*.py" >/tmp/boshi_deploy_test.log 2>&1 ); then
        echo "❌ 单测失败，部署中止。日志：/tmp/boshi_deploy_test.log" >&2
        tail -20 /tmp/boshi_deploy_test.log >&2
        exit 1
    fi
    tail -3 /tmp/boshi_deploy_test.log | sed 's/^/  /'
else
    echo "⚠️  已跳过单测（--no-test）"
fi

# ---- 3. 备份安装副本 -------------------------------------------------------
TS="$(date +%Y%m%d-%H%M%S)"
for f in "$DST_PLUGIN"/*.py; do
    [ -f "$f" ] || continue
    cp "$f" "$f.bak-$TS"
done
echo ""
echo "== 备份 =="
echo "  已备份到 $DST_PLUGIN/*.py.bak-$TS"

# ---- 4. 同步 ---------------------------------------------------------------
rm -rf "$DST_PLUGIN/__pycache__"
cp -r "$SRC_PLUGIN/." "$DST_PLUGIN/"
rm -rf "$DST_PLUGIN/__pycache__"
echo "== 同步完成 =="

# ---- 5. 校验 ---------------------------------------------------------------
FAIL=0
while IFS= read -r rel; do
    [ -n "$rel" ] || continue
    diff -q "$SRC_PLUGIN/$rel" "$DST_PLUGIN/$rel" >/dev/null 2>&1 || { echo "❌ 不一致：$rel"; FAIL=1; }
done < <(cd "$SRC_PLUGIN" && find . -type f ! -path './__pycache__/*' | sed 's|^\./||' | sort)

# 守卫断言：事实层只采用户侧（P3.1），防止再次被两侧合并覆盖
GUARD_HITS=$(grep -c "items_from_slots(u_slots_meta)" "$DST_PLUGIN/__init__.py" 2>/dev/null || echo 0)
if [ "$GUARD_HITS" = "0" ]; then
    echo "❌ 守卫失败：安装副本里没有 items_from_slots(u_slots_meta)（事实层只采用户侧丢失）"
    FAIL=1
fi
if grep -q "for _m in (u_slots_meta, a_slots_meta)" "$DST_PLUGIN/__init__.py" 2>/dev/null; then
    echo "❌ 守卫失败：事实层仍在两侧合并"
    FAIL=1
fi

echo ""
[ "$FAIL" = 0 ] && echo "✅ 部署校验通过（$TS）" || { echo "❌ 部署校验失败" >&2; exit 1; }
echo "ℹ️  运行中的进程仍持有旧模块，重启 Hermes 网关/新开会话后生效。"
