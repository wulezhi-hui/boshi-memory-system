import os, sys

# Insert both paths the same way boshi plugin does
for p in ['C:/Users/Administrator/.boshi/memory', 'C:/Users/Administrator/.boshi']:
    p_norm = p.replace('\\', '/')
    if p_norm not in sys.path:
        sys.path.insert(0, p_norm)

print(f"Python: {sys.executable}")
print(f"sys.path[:3]: {sys.path[:3]}")

# Check file exists
for f in ['C:/Users/Administrator/.boshi/memory/tiered_memory.py',
          'C:/Users/Administrator/.boshi/chroma_bridge.py']:
    print(f"  {f}: exists={os.path.isfile(f)}")

# Test chroma_bridge
try:
    import chroma_bridge
    print("chroma_bridge: OK")
except Exception as e:
    print(f"chroma_bridge FAIL: {e}")

# Test tiered_memory
try:
    from tiered_memory import get_store, TieredMemory
    print("tiered_memory: OK")
except Exception as e:
    print(f"tiered_memory FAIL: {e}")
