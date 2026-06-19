#!/usr/bin/env python3
"""CloakBrowser 封装 — 给 Hermes 的 stealth 浏览器工具
用法:
  python cloak_bridge.py navigate "https://www.toutiao.com"
  python cloak_bridge.py evaluate "document.title"
  python cloak_bridge.py screenshot

依赖: cloakbrowser (pip install cloakbrowser)
"""
import sys, json, base64, time, os
from cloakbrowser import launch

BROWSER = None
PAGE = None

def get_browser():
    global BROWSER
    if BROWSER is None:
        BROWSER = launch(
            headless=True,
            humanize=True,        # 人类化鼠标轨迹+键盘时间
        )
    return BROWSER

def get_page():
    global PAGE
    if PAGE is None:
        b = get_browser()
        PAGE = b.new_page()
        # 注入反检测脚本
        PAGE.add_init_script("""
            // 抹掉 navigator.webdriver
            Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
            // 模拟 window.chrome
            window.chrome = { runtime: {} };
            // 随机化 Canvas 指纹
            const origToDataURL = HTMLCanvasElement.prototype.toDataURL;
            HTMLCanvasElement.prototype.toDataURL = function(type) {
                const orig = origToDataURL.call(this, type);
                return orig.slice(0, -10) + Math.random().toString(36).slice(2,12);
            };
            // 模拟 plugins 数组
            Object.defineProperty(navigator, 'plugins', {
                get: () => [1,2,3,4,5]
            });
            // 模拟 languages
            Object.defineProperty(navigator, 'languages', {
                get: () => ['zh-CN', 'zh', 'en']
            });
        """)
    return PAGE

def cmd_navigate(url):
    page = get_page()
    page.goto(url, wait_until='networkidle')
    print(json.dumps({"ok": True, "url": page.url, "title": page.title()}))

def cmd_evaluate(expr):
    page = get_page()
    result = page.evaluate(expr)
    print(json.dumps({"ok": True, "result": str(result)[:3000]}))

def cmd_screenshot(path=None):
    page = get_page()
    if not path:
        path = "/tmp/cloak_screenshot.png"
    page.screenshot(path=path)
    print(json.dumps({"ok": True, "path": path}))

def cmd_click(selector):
    page = get_page()
    page.click(selector)
    print(json.dumps({"ok": True}))

def cmd_type(selector, text):
    page = get_page()
    page.fill(selector, text)
    print(json.dumps({"ok": True}))

def cmd_get_cookies(domain=None):
    page = get_page()
    cookies = page.context.cookies()
    if domain:
        cookies = [c for c in cookies if domain in c.get('domain', '')]
    print(json.dumps({"ok": True, "cookies": len(cookies), "data": cookies[:5]}))

def cmd_close():
    global BROWSER, PAGE
    if BROWSER:
        BROWSER.close()
        BROWSER = None
        PAGE = None
    print(json.dumps({"ok": True}))

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("用法: cloak_bridge.py <命令> [参数...]")
        print("命令: navigate, evaluate, screenshot, click, type, get_cookies, close")
        sys.exit(1)
    
    cmd = sys.argv[1]
    args = sys.argv[2:]
    
    cmds = {
        "navigate": lambda: cmd_navigate(args[0]),
        "evaluate": lambda: cmd_evaluate(args[0]),
        "screenshot": lambda: cmd_screenshot(args[0] if args else None),
        "click": lambda: cmd_click(args[0]),
        "type": lambda: cmd_type(args[0], args[1]),
        "get_cookies": lambda: cmd_get_cookies(args[0] if args else None),
        "close": cmd_close,
    }
    
    if cmd in cmds:
        cmds[cmd]()
    else:
        print(json.dumps({"ok": False, "error": f"未知命令: {cmd}"}))