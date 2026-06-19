#!/usr/bin/env python3
"""Check current chroma_bridge DB state"""
import sys
sys.path.insert(0, "/c/Users/Administrator/.boshi")
import chroma_bridge

client = chroma_bridge._get_client()
ef = chroma_bridge._get_embedding_function()
col = client.get_or_create_collection("boshi_memory", embedding_function=ef)

all_data = col.get()
print("Total entries:", len(all_data["ids"]))

if all_data["metadatas"]:
    for i, m in enumerate(all_data["metadatas"][:10]):
        src = m.get("source", "?")
        topic = m.get("topic", "?")
        tier = m.get("tier", "MISSING")
        ts = str(m.get("timestamp", "?"))[:19] if m.get("timestamp") else "?"
        print(f"  {i}: src={src} topic={topic} tier={tier} ts={ts}")

    if all_data["documents"]:
        print(f"Sample doc: {all_data['documents'][0][:100]}")
    else:
        print("No documents stored")

# Check existing tiers
tiers = set()
sources = set()
for m in all_data["metadatas"]:
    if m:
        if "tier" in m:
            tiers.add(m["tier"])
        if "source" in m:
            sources.add(m["source"])
print(f"Existing tiers: {tiers}")
print(f"Existing sources: {sources}")
