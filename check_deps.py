import importlib.util as u
mods = ("chromadb", "onnxruntime", "transformers", "yaml")
missing = [m for m in mods if not u.find_spec(m)]
if missing:
    print("MISSING: " + ", ".join(missing))
    raise SystemExit(1)
print("boshi deps OK: " + ", ".join(mods))
