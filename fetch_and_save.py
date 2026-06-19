#!/usr/bin/env python3
"""爬取头条收藏中的AI技术文章并存入知识库"""
import json, urllib.request, websocket, time, select, os, re

COOKIE_FILE = r"C:\Users\Administrator\.boshi\360_tt_cookie.txt"
KB_DIR = r"D:\ObsidianVault\虚拟寺院知识库\技术"
os.makedirs(KB_DIR, exist_ok=True)

with open(COOKIE_FILE, 'r') as f:
    COOKIE_STR = f.read().strip()

# AI技术文章列表（排除佛教/哲学类）
articles = [
    ("7649709146966704694", "Github近240万star，3个超神的GitHub开源项目"),
    ("7649751210567680527", "56.2k Star！这个Skill给Hermes和Claude Code装上项目大脑"),
    ("7649612153594511899", "本地+云端，免费模型用到你爽"),
    ("7649684171397186098", "全模态API无限期免费，谁在掀翻AI时代的成本大山"),
    ("7648395916214288931", "24.6k星！AI的记忆问题被这个团队解决了"),
    ("7648306988731957800", "LLM context一夜暴涨4000星-开源压缩层让agent少烧60-95%token"),
    ("7638465385704079907", "部署Hermes Agent后配置SOUL USER AGENTS核心文件"),
    ("7646937954216100390", "EasyAIoT把所有摄像头接进来AI实时盯着画面"),
    ("7647336149589000704", "DeepSeek上线专家模式-这套提示词榨干它全部潜能"),
    ("7647040580555407899", "估计90%用户都不知道Hermes是有辅模型的"),
    ("7646310367374737954", "OpenClaw避坑指南先装这10个Skills再谈生产力"),
    ("7646039056186409524", "DeepSeek最强提示词一句话AI自动替你多想五步"),
    ("7646239566382318132", "Hermes接BurpSuite法力无边"),
    ("7646240820873527854", "browser-use-AI Agent终于能像人一样上网冲浪了"),
]

def sanitize_filename(title):
    import re
    name = re.sub(r'[\\/:*?"<>|]', '_', title)[:80]
    return name.strip('_. ')

def fetch_article_http(article_id, title):
    """用HTTP请求获取头条文章"""
    url = f"https://www.toutiao.com/article/{article_id}/"
    headers = {
        "Cookie": COOKIE_STR,
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/132.0.0.0 Safari/537.36",
        "Referer": "https://www.toutiao.com/",
    }
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            html = resp.read().decode('utf-8', errors='replace')
            return html
    except Exception as e:
        print(f"  ❌ HTTP获取失败: {e}")
        return None

def extract_content_from_html(html):
    """从HTML中提取正文内容"""
    import re
    # 尝试提取文章内容
    patterns = [
        r'<div[^>]*class="[^"]*article-content[^"]*"[^>]*>(.*?)</div>',
        r'<article[^>]*>(.*?)</article>',
        r'<div[^>]*class="[^"]*content[^"]*"[^>]*>(.*?)</div>',
        r'data-layout="text"[^>]*>(.*?)</div>',
    ]
    
    for pat in patterns:
        match = re.search(pat, html, re.DOTALL)
        if match:
            content = match.group(1)
            # 去除HTML标签
            content = re.sub(r'<[^>]+>', '', content)
            content = re.sub(r'&nbsp;', ' ', content)
            content = re.sub(r'\s+', ' ', content).strip()
            if len(content) > 100:
                return content
    
    return None

# 先试试HTTP方式
print(f"共 {len(articles)} 篇AI技术文章\n")
success = 0
for aid, title in articles:
    print(f"[{success+1}/{len(articles)}] {title[:50]}...")
    html = fetch_article_http(aid, title)
    if html:
        content = extract_content_from_html(html)
        if content and len(content) > 100:
            filename = sanitize_filename(title) + ".md"
            filepath = os.path.join(KB_DIR, filename)
            md_content = f"# {title}\n\n> 来源: 头条收藏\n> 链接: https://www.toutiao.com/article/{aid}/\n> 抓取时间: 2026-06-11\n\n{content}\n"
            with open(filepath, 'w', encoding='utf-8') as f:
                f.write(md_content)
            print(f"  ✅ 已保存 ({len(content)}字) -> {filename}")
            success += 1
        else:
            print(f"  ⚠️ 正文提取失败，标记为待处理")
    else:
        print(f"  ❌ 获取失败")
    time.sleep(0.5)

print(f"\n✅ 完成! 成功 {success}/{len(articles)} 篇")