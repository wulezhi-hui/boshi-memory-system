"""Test the boshi memory sync flow directly"""
import os, sys

# Same path setup as the boshi plugin
for p in ['C:/Users/Administrator/.boshi/memory', 'C:/Users/Administrator/.boshi']:
    p_norm = p.replace('\\', '/')
    if p_norm not in sys.path:
        sys.path.insert(0, p_norm)

from tiered_memory import get_store
store = get_store()

# Simulate storing a conversation turn
try:
    store.store_conversation(
        user_msg="测试消息：QQ能收到消息吗？",
        assistant_msg="能收到！消息收发正常。",
        session_id="test_qq_session",
        source="qqbot",
        user_id="lezhi"
    )
    print("store_conversation: ✅")
except Exception as e:
    print(f"store_conversation FAIL: {e}")
    import traceback
    traceback.print_exc()

# Check if stored
results = store.search_warm("QQ能收到消息", top_k=3)
print(f"Warm search results: {len(results)}")
for r in results:
    print(f"  - {r.get('content', '')[:80]}")
