#!/usr/bin/env python3
"""
WeChat Broadcast Poller
Polls live_turns.json and forwards new messages to WeChat via hermes send.
"""
import json
import os
import subprocess
import time
import sys
import hashlib

LIVE_TURNS = os.path.expanduser("~/.boshi/memory/live_turns.json")
LOG_FILE = os.path.expanduser("~/weixin_broadcast_poll.log")
SEEN_FILE = os.path.expanduser("~/.boshi/memory/live_seen.txt")

def log(msg):
    ts = time.strftime("%Y-%m-%d %H:%M:%S")
    with open(LOG_FILE, "a", encoding="utf-8") as f:
        f.write(f"[{ts}] {msg}\n")
    print(f"[{ts}] {msg}", flush=True)

def get_content_hash():
    """Get a hash of current live_turns.json content."""
    try:
        with open(LIVE_TURNS, "r", encoding="utf-8") as f:
            return hashlib.md5(f.read().encode()).hexdigest()
    except (FileNotFoundError, json.JSONDecodeError):
        return ""

def load_seen():
    """Load the set of seen content hashes."""
    seen = set()
    if os.path.exists(SEEN_FILE):
        with open(SEEN_FILE, "r", encoding="utf-8") as f:
            seen = set(line.strip() for line in f if line.strip())
    return seen

def save_seen(hash_val):
    """Append hash to seen file."""
    with open(SEEN_FILE, "a", encoding="utf-8") as f:
        f.write(hash_val + "\n")

def read_live_turns():
    """Read and parse live_turns.json."""
    try:
        with open(LIVE_TURNS, "r", encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError) as e:
        log(f"Error reading live_turns.json: {e}")
        return {}

log("WeChat Broadcast Poller starting...")
log(f"Watching: {LIVE_TURNS}")

seen = load_seen()
log(f"Loaded {len(seen)} seen hashes")

while True:
    try:
        current_hash = get_content_hash()
        if current_hash and current_hash not in seen:
            data = read_live_turns()
            workstation_data = data.get("boshi-workstation-latest", {})
            if workstation_data:
                user_msg = workstation_data.get("user_msg", "")
                assistant_msg = workstation_data.get("assistant_msg", "")
                timestamp = workstation_data.get("timestamp", 0)
                
                log(f"New broadcast detected! user_msg={user_msg[:50]}...")
                
                # Format message for WeChat
                wechat_msg = f"🧑 **乐之(工作台)**: {user_msg}\n\n🦄 **伯仕**: {assistant_msg}"
                
                # Send via hermes CLI
                result = subprocess.run(
                    ["hermes", "send", "-t", "weixin", wechat_msg],
                    capture_output=True,
                    text=True,
                    timeout=30,
                    cwd=os.path.expanduser("~")
                )
                log(f"hermes send stdout: {result.stdout[:200]}")
                log(f"hermes send stderr: {result.stderr[:200]}")
                log(f"hermes send returncode: {result.returncode}")
                
                if result.returncode == 0:
                    log("✅ Broadcast sent to WeChat!")
                else:
                    log(f"❌ Failed to send broadcast (rc={result.returncode})")
            
            seen.add(current_hash)
            save_seen(current_hash)
            log(f"Recorded seen hash: {current_hash[:12]}...")
        
    except Exception as e:
        log(f"Error in poll loop: {e}")
    
    time.sleep(3)
