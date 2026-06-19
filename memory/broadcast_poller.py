"""
汇聚广播轮询器 — 监听工作台（7681）的回复并推送到微信
====================================================
每秒检查 live_turns.json，发现新消息就通过 Gateway 的 send API 发到微信。
"""

import json
import logging
import os
import subprocess
import time

# 配置
LIVE_TURNS_FILE = os.path.expanduser("~/.boshi/memory/live_turns.json")
LAST_SENT_FILE = os.path.expanduser("~/.boshi/memory/last_broadcast_ts.txt")
POLL_INTERVAL = 2  # 秒

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(message)s",
    handlers=[
        logging.StreamHandler(),
    ],
)
log = logging.getLogger("boshi-broadcast")


def get_last_sent_ts() -> float:
    try:
        with open(LAST_SENT_FILE) as f:
            return float(f.read().strip())
    except (FileNotFoundError, ValueError, OSError):
        return 0.0


def save_last_sent_ts(ts: float):
    with open(LAST_SENT_FILE, 'w') as f:
        f.write(str(ts))


def send_to_weixin(message: str) -> bool:
    """通过 Hermes CLI 的 send 命令发送到微信"""
    try:
        cmd = f'hermes send -t weixin "{message[:2000]}"'
        result = subprocess.run(cmd, shell=True, capture_output=True, timeout=15, text=True)
        if result.returncode == 0:
            log.info("已发送到微信 ✅")
            return True
        else:
            # 试试直接发消息
            log.warning("hermes send 失败，尝试其他方式...")
            # fallback: 直接写文件等我来轮询
            return False
    except subprocess.TimeoutExpired:
        log.warning("发送超时")
        return False
    except Exception as e:
        log.error(f"发送异常: {e}")
        return False


def poll():
    """主循环"""
    last_ts = get_last_sent_ts()
    log.info(f"广播轮询器启动，上次发送时间戳: {last_ts}")

    while True:
        try:
            if not os.path.exists(LIVE_TURNS_FILE):
                time.sleep(POLL_INTERVAL)
                continue

            with open(LIVE_TURNS_FILE, encoding='utf-8') as f:
                turns = json.load(f)

            # 检查工作台的最新消息
            latest = turns.get("boshi-workstation-latest")
            if latest and latest["timestamp"] > last_ts:
                user_msg = latest.get("user_msg", "")
                assistant_msg = latest.get("assistant_msg", "")

                log.info(f"发现工作台新消息: {user_msg[:30]}...")

                # 发送到微信
                text = f"🧑 **乐之(工作台)**: {user_msg}\n\n🦄 **伯仕**: {assistant_msg}"
                if send_to_weixin(text):
                    last_ts = latest["timestamp"]
                    save_last_sent_ts(last_ts)

        except json.JSONDecodeError:
            pass
        except Exception as e:
            log.debug(f"轮询异常: {e}")

        time.sleep(POLL_INTERVAL)


if __name__ == '__main__':
    poll()
