import socket, json

s = socket.socket(socket.AF_INET6, socket.SOCK_STREAM)
s.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 0)
s.bind(("::", 8765))
s.listen(5)

print("IPv6 测试服务启动在 :8765")
print()
print("请在手机上（关WiFi，开流量）访问：")
print()
print("  http://[240e:381:9fee:da01:2449:f32f:9c14:3979]:8765/")
print()
print('看到 {"status":"ok","host":"伯仕主身"} 就是通了 ✅')
print()

resp_body = json.dumps({"status": "ok", "host": "伯仕主身"}, ensure_ascii=False)
resp = (
    "HTTP/1.1 200 OK\r\n"
    "Content-Type: application/json; charset=utf-8\r\n"
    "Access-Control-Allow-Origin: *\r\n"
    f"Content-Length: {len(resp_body.encode('utf-8'))}\r\n"
    "\r\n"
    f"{resp_body}"
).encode("utf-8")

while True:
    conn, addr = s.accept()
    data = conn.recv(4096)
    if data:
        print(f"收到连接来自: {addr[0]}")
        conn.send(resp)
    conn.close()
