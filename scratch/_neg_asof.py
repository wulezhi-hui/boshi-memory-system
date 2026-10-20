
import sys; sys.path.insert(0,"C:/Users/wulezhi/.boshi")
import pg_bridge as p
WHERE={"created_at":{"$lte":1792461898.0}}
q=["别管显存不足，我问的是代理连不上外网",
   "不是歌词整理那件事，是分镜怎么排",
   "存储池损毁怎么办",
   "不用改模型，是问 ComfyUI 里节点下拉找不到"]
for x in q:
    a=p._has_anchor(x)
    print("Q:", x, "anchor=", a)
    for m in ("auto","vector"):
        it=p.search_memory(x, top_k=3, fusion=m, scope="all", where=WHERE)
        print("  ", m, [ (str(i["id"])[:8], i.get("channels"), round(i["score"],3)) for i in it])
        print("      ", (it[0]["content"] or "")[:70].replace("\n"," ") if it else "")
