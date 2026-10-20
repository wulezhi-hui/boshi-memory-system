
import sys, time, os; sys.path.insert(0,"C:/Users/wulezhi/.boshi")
import pg_bridge as p
sys.path.insert(0,"C:/Users/wulezhi/.boshi/eval")
from cases_a9 import CASES
qs=[q for k,g,a,q in CASES if k=="overlap"][:10]
p.search_memory("预热 嵌入 模型", top_k=3, fusion="auto", scope="all")
def run():
    t=time.perf_counter()
    for q in qs: p.search_memory(q, top_k=5, fusion="auto", scope="all")
    return (time.perf_counter()-t)/len(qs)*1000
os.environ["BOSHI_AUTO_KW_NOANCHOR"]="0"; off=[]; on=[]
for i in range(3):
    off.append(run())
    os.environ["BOSHI_AUTO_KW_NOANCHOR"]="1"; on.append(run())
    os.environ["BOSHI_AUTO_KW_NOANCHOR"]="0"
import statistics as st
print("off(门关) ms/q:", [round(x,1) for x in off], "median", round(st.median(off),1))
print("on (门开) ms/q:", [round(x,1) for x in on], "median", round(st.median(on),1))
print("delta ms:", round(st.median(on)-st.median(off),1))
