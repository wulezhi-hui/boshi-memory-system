"""
伯仕网络代理智能路由器 v2
根据目标网站区域自动选择直连或代理
使用 curl 命令，代理控制更精确
"""
import os
import sys
import subprocess
import socket
import json

# ========== 配置 ==========
PROXY_HOST = "127.0.0.1"
PROXY_HTTP_PORT = 17891

# 已知的需要代理才能访问的网站（被墙）
WALLED_SITES = {
    'huggingface.co', 'github.com', 'youtube.com', 'google.com',
    'googleapis.com', 'openai.com', 'anthropic.com', 'x.com',
    'twitter.com', 'facebook.com', 'instagram.com', 'reddit.com',
    'medium.com', 'arxiv.org', 'stackoverflow.com', 'docker.com',
    'npmjs.com', 'pypi.org', 'cloudflare.com', 'vercel.com',
    'netlify.com', 'heroku.com', 'supabase.com', 'stripe.com',
    'openclaw.ai', 'nousresearch.com',
}

# 已知国内直连网站（应强制绕过代理）
DOMESTIC_SITES = {
    'toutiao.com', 'ixigua.com', 'douyin.com', 'tiktok.com',
    'zhihu.com', 'bilibili.com', 'b23.tv', 'csdn.net',
    'juejin.cn', 'jianshu.com', 'weibo.com', 'qq.com',
    'baidu.com', 'aliyun.com', 'taobao.com', 'tmall.com',
    'jd.com', '163.com', '126.com', 'sina.com.cn',
    'sohu.com', 'ifeng.com', 'people.com.cn', 'xinhuanet.com',
    'gov.cn', 'edu.cn', 'org.cn', 'com.cn',
    'jikejun.com', 'jishuzhan.net', 'macin.top', 'freedidi.com',
    'coolai.top', 'codersera.com',
}

def is_proxy_available(host=PROXY_HOST, port=PROXY_HTTP_PORT, timeout=3):
    """检测本地 HTTP 代理端口是否监听"""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(timeout)
        result = s.connect_ex((host, port))
        s.close()
        return result == 0
    except Exception:
        return False

def get_domain(url):
    """从 URL 提取域名"""
    from urllib.parse import urlparse
    hostname = urlparse(url).hostname or url
    parts = hostname.lower().split('.')
    if len(parts) >= 2:
        return '.'.join(parts[-2:])
    return hostname.lower()

def should_use_proxy(url):
    """
    判断访问该 URL 是否需要代理
    返回: (use_proxy: bool, reason: str)
    """
    domain = get_domain(url)
    
    # 国内网站 → 强制直连
    for d in DOMESTIC_SITES:
        if domain.endswith(d):
            return (False, f"国内网站 {domain} → 直连")
    
    # 已知被墙网站 → 需要代理（检测代理是否可用）
    for w in WALLED_SITES:
        if domain.endswith(w):
            if is_proxy_available():
                return (True, f"被墙网站 {domain} → 代理")
            else:
                return (False, f"被墙网站 {domain} 但代理未开启 → 直连（可能失败）")
    
    # 未知网站 → 先试直连
    return (None, f"未知网站 {domain} → 先试直连")

def fetch_url(url, timeout=15, force_direct=False, force_proxy=False):
    """
    使用 curl 智能获取 URL
    
    参数:
        url: 目标地址
        timeout: 超时秒数
        force_direct: 强制直连（绕过代理）
        force_proxy: 强制走代理
    
    返回:
        (content: str or None, info: dict)
    """
    # 决定策略
    if force_direct:
        use_proxy = False
        reason = "强制直连"
    elif force_proxy:
        use_proxy = True
        reason = "强制代理"
    else:
        use_proxy, reason = should_use_proxy(url)
        if use_proxy is None:
            use_proxy = False
            reason = "未知网站，先试直连"
    
    # 构建 curl 命令
    cmd = [
        'curl', '-s', '-L',  # 静默模式，跟随重定向
        '--max-time', str(timeout),
        '-H', 'User-Agent: Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36',
        '-H', 'Accept: text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
        '-H', 'Accept-Language: zh-CN,zh;q=0.9,en;q=0.8',
    ]
    
    if use_proxy:
        cmd.extend(['--proxy', f'http://{PROXY_HOST}:{PROXY_HTTP_PORT}'])
    else:
        cmd.extend(['--noproxy', '*'])  # 强制绕过所有代理
    
    cmd.append(url)
    
    info = {
        'url': url,
        'proxy_used': use_proxy,
        'reason': reason,
        'status': None,
        'error': None,
        'content_length': 0,
    }
    
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout + 5)
        
        if result.returncode == 0:
            info['status'] = 200
            info['content_length'] = len(result.stdout)
            return result.stdout, info
        else:
            # 如果直连失败且代理可用，自动重试代理
            if not use_proxy and is_proxy_available() and not force_direct:
                info['fallback'] = '直连失败，尝试代理重试...'
                cmd2 = cmd.copy()
                # 替换 --noproxy '*' 为 --proxy
                try:
                    idx = cmd2.index('--noproxy')
                    cmd2[idx:idx+2] = ['--proxy', f'http://{PROXY_HOST}:{PROXY_HTTP_PORT}']
                except ValueError:
                    pass
                
                result2 = subprocess.run(cmd2, capture_output=True, text=True, timeout=timeout + 5)
                if result2.returncode == 0:
                    info['status'] = 200
                    info['content_length'] = len(result2.stdout)
                    info['proxy_used'] = True
                    info['reason'] = '直连失败后代理重试成功'
                    return result2.stdout, info
            
            info['error'] = result.stderr[:200] if result.stderr else f'curl exit code {result.returncode}'
            return None, info
            
    except subprocess.TimeoutExpired:
        info['error'] = 'timeout'
        return None, info
    except Exception as e:
        info['error'] = str(e)
        return None, info


def test_url(url):
    """测试访问某个 URL"""
    use_proxy, reason = should_use_proxy(url)
    print(f"\n目标: {url}")
    print(f"策略: {reason}")
    print(f"代理可用: {'是' if is_proxy_available() else '否'}")
    
    content, info = fetch_url(url)
    if content:
        print(f"结果: HTTP {info['status']} ✓ ({info['content_length']} bytes)")
        print(f"实际使用代理: {'是' if info.get('proxy_used') else '否'}")
    else:
        print(f"结果: 失败 - {info['error']}")
    return info


if __name__ == '__main__':
    if len(sys.argv) > 1:
        test_url(sys.argv[1])
    else:
        print("伯仕网络代理智能路由器 v2")
        print(f"代理状态: {'监听中' if is_proxy_available() else '未监听'}")
        print("")
        test_url("https://www.toutiao.com/article/7651505444959158826/")
        test_url("https://huggingface.co/Hcompany/Holo-3.1-35B-A3B-GGUF/resolve/main/mmproj.f16.gguf")
