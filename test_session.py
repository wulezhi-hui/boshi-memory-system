#!/usr/bin/env python3
"""测试 Gateway session API"""
import json, urllib.request, sys

payload = json.dumps({
    "model": "hermes-agent",
    "messages": [{"role": "user", "content": "你好，测试session"}],
    "stream": False,
    "max_tokens": 100
}).encode()

req = urllib.request.Request(
    "http://127.0.0.1:8642/v1/chat/completions",
    data=payload,
    headers={
        "Content-Type": "application/json",
        "X-Hermes-Session-Id": "boshi-cross-test"
    },
    method="POST"
)

try:
    with urllib.request.urlopen(req, timeout=60) as resp:
        print("Status:", resp.status)
        body = resp.read().decode()
        print("Body:", body[:500])
except Exception as e:
    print(f"Error: {e}")
    if hasattr(e, 'read'):
        print("Body:", e.read().decode()[:500])
