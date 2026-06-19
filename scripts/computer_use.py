#!/usr/bin/env python3
"""
伯仕 Computer Use 工具 — 一键模拟人类操作电脑
=============================================
调用方式：
    computer_use("打开Chrome")
    computer_use("打开D盘的书库")
    computer_use("把窗口拖到左边")
    computer_use("最小化所有窗口")
    computer_use("在桌面右键刷新")

路由优先级：CLI命令 > 键盘快捷键 > 固定坐标 > OCR识别
"""

import pyautogui
import subprocess
import time
import json
import os
import re

# ─── 配置 ───
SCREEN_W, SCREEN_H = 3440, 1440
pyautogui.FAILSAFE = True      # 鼠标移到左上角紧急停止
pyautogui.PAUSE = 0.05         # 操作间停顿

# ─── 桌面图标预设（3440x1440 默认Windows布局） ───
DESKTOP_ICONS = {}  # 运行时自动发现

# ─── 快捷键词典 ───
HOTKEYS = {
    "最小化所有窗口":     (['win', 'd'],),
    "回到桌面":          (['win', 'd'],),
    "显示桌面":          (['win', 'd'],),
    "打开运行":          (['win', 'r'],),
    "打开设置":          (['win', 'i'],),
    "打开文件资源管理器":  (['win', 'e'],),
    "打开资源管理器":     (['win', 'e'],),
    "打开我的电脑":       (['win', 'e'],),
    "切换窗口":          (['alt', 'tab'],),
    "关闭当前窗口":       (['alt', 'f4'],),
    "关闭窗口":          (['alt', 'f4'],),
    "全屏":              (['f11'],),
    "任务管理器":        (['ctrl', 'shift', 'esc'],),
    "锁定电脑":          (['win', 'l'],),
    "截图":              (['win', 'shift', 's'],),
    "重命名":            (['f2'],),
    "全选":              (['ctrl', 'a'],),
    "复制":              (['ctrl', 'c'],),
    "粘贴":              (['ctrl', 'v'],),
    "剪切":              (['ctrl', 'x'],),
    "撤销":              (['ctrl', 'z'],),
    "保存":              (['ctrl', 's'],),
    "查找":              (['ctrl', 'f'],),
    "刷新":              (['f5'],),
}

# ─── CLI命令映射 ───
CLI_COMMANDS = {
    # 浏览器
    "打开chrome":      "start chrome",
    "打开edge":        "start msedge",
    "打开火狐":        "start firefox",
    # 文件管理
    "打开d盘":         "explorer D:\\",
    "打开c盘":         "explorer C:\\",
    "打开e盘":         "explorer E:\\",
    # 目录快捷
    "打开桌面":        "explorer %USERPROFILE%\\Desktop",
    "打开下载":        "explorer %USERPROFILE%\\Downloads",
    "打开文档":        "explorer %USERPROFILE%\\Documents",
}

# ─── 窗口操作（快捷键实现） ───
WINDOW_ACTIONS = {
    "窗口最大化":      ['win', 'up'],
    "窗口最小化":      ['win', 'down'],
    "窗口靠左":        ['win', 'left'],
    "窗口靠右":        ['win', 'right'],
    "窗口靠左上":      ['win', 'left', 'up'],
    "窗口靠右上":      ['win', 'right', 'up'],
    "窗口靠左下":      ['win', 'left', 'down'],
    "窗口靠右下":      ['win', 'right', 'down'],
}

# ─── 右键菜单 ───
RIGHT_CLICK_ITEMS = {
    "桌面右键":        "desktop",
    "刷新桌面":        "desktop",
}

# ─── 工具函数 ───

def _run(cmd, timeout=10):
    """执行系统命令"""
    try:
        subprocess.run(cmd, shell=True, timeout=timeout, check=False)
        return True
    except Exception:
        return False

def _hotkey(*keys):
    """模拟快捷键"""
    pyautogui.hotkey(*keys)
    time.sleep(0.2)

def _click(x, y, button='left', clicks=1):
    """鼠标点击"""
    pyautogui.moveTo(x, y, duration=0.1)
    pyautogui.click(x, y, button=button, clicks=clicks)
    time.sleep(0.3)

def _right_click(x, y):
    """右键点击"""
    _click(x, y, button='right')

def _double_click(x, y):
    """双击"""
    _click(x, y, clicks=2)

def _drag(x1, y1, x2, y2, duration=0.3):
    """拖拽"""
    pyautogui.moveTo(x1, y1, duration=0.1)
    pyautogui.drag(x2 - x1, y2 - y1, duration=duration)
    time.sleep(0.2)

def _type(text):
    """键盘输入"""
    pyautogui.write(text, interval=0.02)

def _press(key):
    """按单个键"""
    pyautogui.press(key)

def _screenshot(region=None):
    """截图"""
    return pyautogui.screenshot(region=region)

def _discover_desktop():
    """自动发现桌面图标位置（首次运行）"""
    global DESKTOP_ICONS
    if DESKTOP_ICONS:
        return
    
    # 任务栏区域：底部 (0, 1350, 3440, 90)
    tb = _screenshot(region=(0, 1350, 3440, 90))
    tb.save('/tmp/_taskbar.png')
    
    # 桌面图标区域：左侧从顶部开始的区域
    desktop = _screenshot()
    desktop.save('/tmp/_desktop_full.png')
    
    # 设置已知固定位置（根据3440x1440 Windows 10桌面图标默认布局）
    DESKTOP_ICONS = {
        # 任务栏（从左到右）
        "开始菜单":        (10, 1410),
        "搜索":            (80, 1410),
        "任务视图":        (160, 1410),
        # 常用图标
        "此电脑":          (30, 200),   # 桌面左上
        "回收站":          (30, 350),   # 桌面左上偏下
        "控制面板":        (30, 500),
        # 下次运行时自动发现
    }
    print(f"[computer_use] 桌面配置: {SCREEN_W}x{SCREEN_H}, {len(DESKTOP_ICONS)}个预设图标")

def _route(goal):
    """智能路由：选择最佳执行方式"""
    g = goal.lower().strip()
    
    # ── CLI命令（最高优先级）──
    for key, cmd in CLI_COMMANDS.items():
        if key in g or g.startswith(key.replace("打开", "开")):
            print(f"[computer_use] → CLI: {cmd}")
            if _run(cmd):
                return True, f"已执行: {cmd}"
            return False, f"CLI失败: {cmd}"
    
    # ── 键盘快捷键 ──
    for action, keys_list in HOTKEYS.items():
        if action in g:
            print(f"[computer_use] → 快捷键: {keys_list}")
            _hotkey(*keys_list[0])
            return True, f"已执行快捷键: {'+'.join(keys_list[0])}"
    
    # ── 窗口操作 ──
    for action, keys in WINDOW_ACTIONS.items():
        if action in g or action.replace("窗口", "").strip() in g:
            print(f"[computer_use] → 窗口操作: {'+'.join(keys)}")
            _hotkey(*keys)
            return True, f"已执行: {action}"
    
    # ── 右键菜单 ──
    if "右键" in g or "刷新" in g:
        # 桌面右键
        _right_click(SCREEN_W // 2, SCREEN_H // 2)
        time.sleep(0.3)
        if "刷新" in g:
            _press('e')  # 刷新在右键菜单里通常是E
            return True, "已刷新桌面"
        return True, "已执行右键"
    
    # ── 鼠标操作 ──
    # 双击桌面图标
    for icon_name, (ix, iy) in DESKTOP_ICONS.items():
        if icon_name in g:
            _double_click(ix, iy)
            return True, f"已双击桌面图标: {icon_name}"
    
    # ── 拖拽操作 ──
    if "拖" in g or "移" in g:
        # 默认把当前活动窗口拖到中间
        cx, cy = SCREEN_W // 2, 10  # 标题栏顶部
        _drag(cx, cy, cx, cy + 200)
        return True, "已拖拽窗口"
    
    # ── 键盘输入 ──
    if g.startswith("输入") or g.startswith("打字") or g.startswith("写入"):
        text = re.sub(r'^(输入|打字|写入)', '', g, count=1).strip()
        _type(text)
        return True, f"已输入: {text}"
    
    if "回车" in g or "enter" in g:
        _press('enter')
        return True, "已按回车"
    
    # ── 未识别指令 ──
    return False, f"无法识别指令: {goal}"


# ─── 主入口 ───

def computer_use(goal):
    """
    一键模拟人类操作电脑
    
    参数:
        goal: 自然语言指令，如 "打开Chrome" "最小化所有窗口" "双击桌面此电脑"
    
    返回:
        (success: bool, message: str)
    """
    # 首次运行自动发现桌面布局
    _discover_desktop()
    
    # 智能路由执行
    success, msg = _route(goal)
    
    if success:
        print(f"✅ {msg}")
    else:
        print(f"❌ {msg}")
    
    return success, msg


# ─── 命令行入口 ───
if __name__ == '__main__':
    import sys
    if len(sys.argv) > 1:
        goal = ' '.join(sys.argv[1:])
        computer_use(goal)
    else:
        print("用法: python computer_use.py '你的指令'")
        print("示例:")
        print("  python computer_use.py '打开Chrome'")
        print("  python computer_use.py '最小化所有窗口'")
        print("  python computer_use.py '打开D盘'")
        print("  python computer_use.py '刷新桌面'")
