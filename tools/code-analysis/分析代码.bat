@echo off
chcp 65001 >nul
title 代码深度分析工具箱 🦄

echo ====================================
echo   伯仕代码深度分析工具箱 v1.0
echo ====================================
echo.

if "%1"=="" (
    echo 用法:
    echo   %~nx0 ^<项目路径^>
    echo.
    echo 示例:
    echo   %~nx0 C:\Users\Administrator\.boshi\memory
    echo.
    pause
    exit /b 1
)

set PROJECT=%~1
echo 📁 分析项目: %PROJECT%
echo.

:: === Phase 1: 代码统计 ===
echo === Phase 1: 代码统计 (pygount) ===
pygount --format=summary --folders-to-skip="__pycache__,.git,venv,.venv" "%PROJECT%"
echo.

:: === Phase 2: 代码质量 ===
echo === Phase 2: 代码质量 (ruff) ===
ruff check "%PROJECT%" --select=E,F,W,N,UP --ignore=E501 --statistics 2>nul
if %errorlevel% neq 0 (
    echo   发现需修复的问题
) else (
    echo   ✅ 无问题
)
echo.

:: === Phase 3: 复杂度分析 ===
echo === Phase 3: 复杂度分析 (radon) ===
radon cc "%PROJECT%" -s 2>nul | head -15
echo.

:: === Phase 4: 安全扫描 ===
echo === Phase 4: 安全扫描 ===
python "%USERPROFILE%\.boshi\tools\code-analysis\security_scan.py" "%PROJECT%"
echo.

:: === Phase 5: 趋势追踪 ===
echo === Phase 5: 趋势追踪 ===
python "%USERPROFILE%\.boshi\tools\code-analysis\trend_tracker.py" "%PROJECT%"
echo.

:: === Phase 6: 增量标记 ===
echo === Phase 6: 缓存分析状态 ===
python "%USERPROFILE%\.boshi\tools\code-analysis\incremental_cache.py" "%PROJECT%"
echo.

echo ====================================
echo   ✅ 分析完成!
echo ====================================
echo.
echo 📁 趋势历史: %USERPROFILE%\.boshi\tools\code-analysis\.metrics\history.csv
echo 📁 最新报告: %USERPROFILE%\.boshi\tools\code-analysis\.metrics\latest_report.md
echo 📁 安全报告: %USERPROFILE%\.boshi\tools\code-analysis\.cache\security_report.json
echo.
pause
