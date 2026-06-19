"""IPv6 连通性测试 — 检测本机 IPv6 公网可达性"""
import http.server
import socket
import json
import sys
import os

PORT = 8765

class Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        info = {
            "status": "ok",
            "host": "伯仕主身",
            "ipv6_addr": socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET6)[0][4][0],
            "path": self.path,
        }
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps(info, ensure_ascii=False).encode("utf-8"))

    def log_message(self, format, *args):
        pass

if __name__ == "__main__":
    server = http.server.HTTPServer(("::", PORT), Handler)
    server.socket.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 0)
    print(f"📡 IPv6 测试服务: http://[::]:{PORT}")
    print(f"🌐 用你的手机开热点，试试访问：")
    print()
    
    # 列出所有非 fe80 的 IPv6 地址
    import subprocess
    result = subprocess.run(
        ["ipconfig"], capture_output=True, text=True, shell=True
    )
    for line in result.stdout.split("\n"):
        if "IPv6 Address" in line or "Temporary IPv6" in line:
            addr = line.split(":")[-1].strip()
            if not addr.startswith("fe80"):
                print(f"   http://[{addr}]:{PORT}/")
    
    print()
    print("🚀 服务已启动，按 Ctrl+C 停止")
    server.serve_forever()
