"""伯仕 P2：写入侧槽位化抽提（零模型、纯规则、可复现）。

设计见 docs/伯仕记忆系统v7.2_P2写入侧槽位化_设计草案.md
定位：
  - **只做搬运与归类**，不生成、不改写任何内容（用户铁律：不用模型生成注解）
  - 抽提结果只用于**检索增强**（进 BM25 的文档文本），不作事实来源
  - 抽不到就是空，绝不猜

对外接口：
  extract(text) -> dict          槽位字典（statement 永远有，其余可能为空）
  doc_text(content, slots) -> str  检索用文档文本：statement + 各槽位值的拼接
  to_meta(slots, raw_len) -> dict 写库用的 metadata 片段（slots 为 JSON 字符串，
                                  这样 Chroma / PG 两端都能原样携带）
"""
from __future__ import annotations

import json
import re
from typing import Any, Dict, List

# ── 路径 ───────────────────────────────────────────────────────────────
# 盘符路径：负向断言挡掉 `https://` 里的 `s:/`（实测第一版把 https 当盘符）
RE_WIN = re.compile(r"(?<![A-Za-z0-9_.])([A-Za-z]:[\\/](?:[^\s\"'()\[\],;，。、）\]]+))")
# POSIX 绝对路径只在明确的扩展名上认，避免把 `and/or` 之类当路径
RE_POSIX = re.compile(
    r"(?<![\w:./])/(?:[\w.\-]+/)*[\w.\-]+\.(?:py|md|json|ya?ml|toml|ps1|sh|bat|txt|db|log|cpp|h|gguf|safetensors)\b")
RE_REL = re.compile(
    r"(?<![\w/])([\w\-]+(?:/[\w\-]+)+\.(?:py|md|json|ya?ml|toml|ps1|sh|bat|db|log))\b")

RE_FLAG = re.compile(r"(?<![\w-])--[A-Za-z][\w-]*")
# 带单位的数字；不匹配 `47.6`（单位在 _norm 里要求紧邻或单空格，且 MB/GB 必须大写）
RE_NUM = re.compile(r"(\d+(?:\.\d+)?\s?(?:GB|MB|KB|MiB|GiB|ms|tok/s|t/s|°C|%))")
RE_CMD = re.compile(
    r"(?:^|\n)\s{0,4}\$?\s?((?:python|pip|uv|git|curl|wget|netstat|schtasks|nvidia-smi"
    r"|llama-server|ollama|psql|docker|node|npm)\b[^\n]{0,160})")
RE_EP = re.compile(r"(?:\b\d{1,3}(?:\.\d{1,3}){3}):(\d{2,5})\b")
# 裸端口：中文里最常见的写法是「端口 8199」「8199 做反推」「监听 8648」，
# 只有 IP:port 那种英文写法时才带点号。2026-10-19 实测漏抓，补关键词邻近的裸端口。
RE_EP_KW = re.compile(r"(?:端口|端口号|监听|port|PORT|listen)")
# 端口值只在关键词所在分句里找，避免把同一行后面的 build 号/上下文也算成端口
_RE_CLAUSE = re.compile(r"[^。；;，,\n]{0,40}")
# 裸数字：只在明确量词后取（build 11118 / ctx 131072 / 上下文 131072），
# 不做全库裸整数，否则日期、ID、随机数全进倒排。
RE_NUM_KW = re.compile(r"(?:build|ctx|上下文长度|上下文|阈值|上限)[^\d\n]{0,4}(\d{2,7})(?![\d.])")
# 版本号：v1.2 / 1.2.3；负向断言挡住 IP 片段与带单位的数字
RE_VER = re.compile(r"(?<![\w.])(v?\d+\.\d+(?:\.\d+)?)(?![\d.\w])")
RE_UNIT_TAIL = re.compile(r"^\s?(?:GB|MB|KB|MiB|GiB|ms|s|tok/s|t/s|°C|%)")

DECIDE = ("决定", "改为", "选用", "采用", "不再", "放弃", "换成", "定下来", "结论是", "结论：")
# 追加的高精度决策标记（2026-10-19 实测：400 条样本逐条人工看过，只留经得起看的）。
# 被否掉的：`就按`（“就按排错三步抓”是流程不是决策）、`切成`（“切成小块”是切割动作）、
# `保留`/`方案`/`口径`/`只用`（宽正则里占 40%+ 命中，但绝大多数不是决策）。
DECIDE_RE = (
    re.compile(r"改用"),
    # 「参数已经定了」「直接定了」「定为 X」「A 方案定了」；
    # 挡掉「判定为可疑」「指定了路径」「规定了格式」「固定了」——这些「定了」不是取舍。
    re.compile(r"(?<![判认指规固])定为|(?<![指认规判固])定了"),
)
# P3.1 拿掉的两条（实测 13:19 首条事实层就坏在这两条上）：
#   `我建议`——助手列选项的固定句式，不是决策（「我建议用 A」= 菜单）；
#   `切(?:换)?到`——「手动切到 bili-deepseek 就能继续用」是备选方案。
# 真取舍词留在 DECIDE 元组里（决定/改为/选用/采用/放弃/不再/换成/定下来/结论是）。
OPEN_Q = ("待", "未合并", "未解决", "需要确认", "TODO", "下一步", "还需", "还没")
LESSON = ("铁律", "教训", "坑", "注意", "必须", "禁止", "踩过", "别忘")
ENV = ("端口", "显存", "CUDA", "Python", "版本不", "机器", "系统是")
_SKIP_PREFIX = ("|", "---", "```")

_SENT_CAP = 200       # statement 截断
_RE_BULLET = re.compile(r"^\s*(?:[-*+•]|\d+[.)、]|[（(]\d+[)）])\s*\S")  # 列表项起始行
# 出现这些词说明「还没决定」，不是决策（「还没决定用哪个」曾被记成决策）
_DECIDE_NEG = ("还没决定", "未决定", "没定", "尚未决定", "待定", "不确定", "没想好")
_ITEM_CAP = 180       # 句子级槽位单条截断
_LIST_CAP = 8         # 每个槽位最多留多少条
_STMT_MIN = 24        # statement 首选长度下限（太短的首句对检索没价值）
# 这些词后面跟的小数是「参数值」不是版本号（阈值 0.85 / 阻尼 0.5 / 温度 0.7）
_VER_VALUE_CTX = ("阈值", "参数", "系数", "阻尼", "温度", "比例", "概率", "权重", "占比", "得分", "分数")

_RE_MD_HEAD = re.compile(r"^\s*#{1,6}\s")
_RE_RULE = re.compile(r"^\s*[-=_*~]{3,}\s*$")
_RE_TABLE = re.compile(r"^\s*\|.*\|\s*$")
_RE_BOLD_ONLY = re.compile(r"^\s*\*\*(.{1,40}?)\*\*\s*$")
# 编号项（`一、`/`（一）`/`(1)`/`1. `）；`3.5 GB` 这种小数用 (?!\d) 挡掉
_RE_NUM_ITEM = re.compile(
    r"^\s*(?:[-*+>]\s+)*(?:[一二三四五六七八九十]{1,3}[、．]"
    r"|（[一二三四五六七八九十\d]{1,3}）|\([一二三四五六七八九十\d]{1,3}\)"
    r"|\d{1,2}[、．)]\s)")


def _is_heading(raw: str) -> bool:
    """标题行判定：只认**结构上明确是标题**的行，不猜语义。

    旧版 `_sentences` 用 `p.strip(" \\t-*#>")`，把 `## 一、你问的第一件事` 的
    `##` 削掉后，“一、你问的第一件事（结论 + 已修完）”就成了一句正常句子，
    statement 直接被标题占掉（实测第 3 条就是这么坏的）。这里在 strip 之前判。
    """
    s = raw.strip()
    if not s or _RE_RULE.match(s) or _RE_TABLE.match(s):
        return True
    if _RE_MD_HEAD.match(s):                       # markdown 标题：无歧义
        return True
    m = _RE_BOLD_ONLY.match(s)
    if m and not re.search(r"[。！？：；，]", m.group(1)):    # `**当前进度**`
        return True
    m = _RE_NUM_ITEM.match(s)
    if m:
        core = s[m.end():]
        # 编号本身不是标题：短且无句读的才算（`1. 先跑评测` 是列表项内容）
        if len(core) <= 30 and not re.search(r"[。！？：；，]", core):
            return True
    return False


SLOT_KEYS = ("files", "commands", "flags", "numbers", "ports", "versions",
             "decisions", "open_questions", "reusable_experience", "boundary_envs")


def _blocks(text: str) -> List[str]:
    """按标题行把正文切块；标题行整行丢弃。

    为什么用「切块」而不是「删行后合并」：删掉中间的标题行会让前后两行在拼接后
    不再连续，槽位值也就不是原文的**连续子串**了（实测严格子串率掉到 87%）。
    切块后块内一行不动，值永远是连续片段。
    """
    blocks: List[str] = []
    cur: List[str] = []
    for raw in (text or "").splitlines():
        if _is_heading(raw):
            if cur:
                blocks.append("\n".join(cur))
                cur = []
            continue
        if raw.strip():
            cur.append(raw)          # 原样保留：不 strip 标记、不做空白归一
    if cur:
        blocks.append("\n".join(cur))
    return blocks


def _split_items(seg: str) -> List[str]:
    """块内按「列表项起始行」再切：一行一条，正文行仍与下文合并。

    修的是 P3.1 实测的脏数据：markdown 无序列表
    ```
    - 第2条：日志路径…
    - 第3条：手动切到 bili-deepseek 就能用
    ```
    以前整块算「一句」，关键词出现在第 3 条，截断却从块首数 180 字 →
    第 2 条的日志路径被当成决策内容存进事实层。列表项天然是独立语义单元，
    按行切开后关键词只带走它自己那一行；而「当前状态一句话：」这类引出行
    （不以列表符开头）仍与下文合并，不会退回旧版 statement 全是引子的毛病。
    """
    items: List[str] = []
    cur: List[str] = []
    for line in (seg or "").splitlines():
        if _RE_BULLET.match(line):
            if cur:
                items.append("\n".join(cur))
                cur = []
        if line.strip():
            cur.append(line)
    if cur:
        items.append("\n".join(cur))
    return items


def _sentences(text: str) -> List[str]:
    """切句：块内先按列表项切，再按句末标点切，**正文不按换行切**。

    不按 `\\n` 切是因为 `当前状态一句话：` 这种引出行会被切成独立片段，旧版
    statement 平均只有 38.6 字、一半是这类引子。块内合并后，冒号引出行自然
    和下文连成一句；标题行、列表项行作为硬边界，不会污染句子。
    """
    out: List[str] = []
    for seg in _blocks(text):
        for item in _split_items(seg):
            for p in re.split(r"(?<=[。！？；])", item):
                p = p.strip("\n \t")
                if p:
                    out.append(p)
    return out


def _clip(s: str, cap: int = _ITEM_CAP) -> str:
    """按分句标点截断，避免切成半句话；截不动就硬截（截断处仍是原文连续子串）。"""
    if len(s) <= cap:
        return s
    cut = max(s.rfind(c, 0, cap) for c in "，、；：,;")
    return s[:cut] if cut >= cap // 2 else s[:cap]


def _clip_at(s: str, pos: int, cap: int = _ITEM_CAP) -> str:
    """以命中词为锚截取（P3.1）。

    `_clip` 永远从**开头**数 180 字，长句里关键词在末尾时等于把无关的前半段
    存成了决策（实测首条事实层就是这么坏的）。这里以命中位置为锚取窗口，
    仍然只做截取、不改一个字，返回值仍是原文的连续子串。
    """
    if len(s) <= cap:
        return s
    start = max(0, min(pos - cap // 3, len(s) - cap))
    win = s[start:start + cap]
    cut = max(win.rfind(c) for c in "，、；：,;")
    if cut >= cap // 2:          # 后半段里能收到分句标点 → 提前收尾，别切半个词
        win = win[:cut]
    return win.strip()


def _hit_pos(s: str, keys=(), regexes=()) -> int:
    """命中词在句中的最早位置（多个关键词取最左）。没命中返回 0。"""
    pos = [s.find(k) for k in keys if k and k in s]
    for r in regexes:
        m = r.search(s)
        if m:
            pos.append(m.start())
    return min(pos) if pos else 0


def _clause_at(s: str, pos: int) -> str:
    """取命中位置所在的**分句**（逗号/句号/换行切），用于局部否定判断。

    整句判断会把「A 方案定了，Y 还没定」这种一句里既决策又待办的话整体否掉
    （实测：真决策被丢弃，只剩待办）。否定词只看命中词所在那一小段。
    """
    seps = "，,。；;！？\n"
    start = max((s.rfind(c, 0, pos) for c in seps), default=-1) + 1
    ends = [s.find(c, pos) for c in seps if s.find(c, pos) != -1]
    end = min(ends) if ends else len(s)
    return s[start:end]


def _statement(sents: List[str]) -> str:
    """statement：第一句「够长」的非标题句；过长时在分句标点处截断。

    `_STMT_MIN` 的作用是跳过「两件事都有结论了。」「当前状态一句话：」这类引子——
    它们不是标题（有句读），但对检索没价值。
    """
    pick = next((s for s in sents if len(s) >= _STMT_MIN), "")
    if not pick:
        pick = next((s for s in sents if len(s) >= 8), "")
    if not pick:
        pick = sents[0] if sents else ""
    return _clip(pick, _SENT_CAP)




def _dedup(seq: List[str], cap: int = _LIST_CAP) -> List[str]:
    seen, out = set(), []
    for v in seq:
        v = (v or "").strip()
        if v and v not in seen:
            seen.add(v)
            out.append(v)
    return out[:cap]


def _ports(text: str) -> List[str]:
    """端口：IP:port 直接取；中文口语「端口 8199、8200 做…」在关键词所在分句内扫描裸端口。"""
    out = list(RE_EP.findall(text))
    for m in RE_EP_KW.finditer(text):
        # 只取关键词往后的同一分句，避免把后面的 build 号/上下文长度也算成端口
        tail = text[m.start():m.start() + 40].split("\n")[0]
        tail = re.split(r"[。；;，,]", tail)[0]
        out.extend(re.findall(r"(?<![\d.])(\d{2,5})(?![\d.])", tail))
    return out


def _versions(text: str) -> List[str]:
    out = []
    for m in RE_VER.finditer(text):
        tok = m.group(1)
        tail = text[m.end():m.end() + 4]
        if RE_UNIT_TAIL.match(tail):        # `47.6 MB` 的 47.6 不是版本
            continue
        if tok.count(".") == 1 and len(tok.split(".")[0]) > 3:   # 太长的整数段多半是 IP/数字
            continue
        if m.start(1) and text[m.start(1) - 1] in "=＝~":        # `_KW_LENB=0.5` 是参数值不是版本
            continue
        ctx = re.split(r"[，,。；;、]", text[max(0, m.start(1) - 12):m.start(1)])[-1].strip()
        if any(w in ctx for w in _VER_VALUE_CTX):   # 「阈值 0.85」「阻尼 0.5」是参数值
            continue
        out.append(tok)
    return out


def extract(text: str) -> Dict[str, Any]:
    """零模型规则抽提。全部槽位都是**原文片段的引用**，不做任何改写。"""
    t = text or ""
    out: Dict[str, Any] = {k: [] for k in SLOT_KEYS}
    out["files"] = _dedup(RE_WIN.findall(t) + RE_POSIX.findall(t) + RE_REL.findall(t))
    out["commands"] = _dedup([m.group(1).strip() for m in RE_CMD.finditer(t)])
    out["flags"] = _dedup(RE_FLAG.findall(t))
    out["numbers"] = _dedup(RE_NUM.findall(t) + RE_NUM_KW.findall(t))
    out["ports"] = _dedup(_ports(t))
    out["versions"] = _dedup(_versions(t))

    sents = _sentences(t)
    for s in sents:
        if len(s) < 6 or any(s.startswith(k) for k in _SKIP_PREFIX):
            continue
        # 决策：命中词锚定截取（关键词在长句末尾时不再只留下无关的开头），
        # 且「还没决定 / 待定」不算决策——它是待办，交给下面的 OPEN_Q。
        # 否定判断只看命中词所在分句：一句里「A 定了，B 还没定」两件事都要留住。
        hit = _hit_pos(s, DECIDE, DECIDE_RE)
        if (any(k in s for k in DECIDE) or any(r.search(s) for r in DECIDE_RE)) \
                and not any(n in _clause_at(s, hit) for n in _DECIDE_NEG) \
                and len(out["decisions"]) < 3:
            out["decisions"].append(_clip_at(s, hit))
        if any(k in s for k in OPEN_Q) and len(out["open_questions"]) < 3:
            out["open_questions"].append(_clip_at(s, _hit_pos(s, OPEN_Q)))
        if any(k in s for k in LESSON) and len(out["reusable_experience"]) < 3:
            out["reusable_experience"].append(_clip_at(s, _hit_pos(s, LESSON)))
        if any(k in s for k in ENV) and len(out["boundary_envs"]) < 3:
            out["boundary_envs"].append(_clip_at(s, _hit_pos(s, ENV)))

    for k in SLOT_KEYS:
        out[k] = _dedup(out[k])
    out["statement"] = _statement(sents)
    return out


def filled_count(slots: Dict[str, Any]) -> int:
    """除 statement 外有几个槽位有值（用于观测/筛选）。"""
    if not slots:
        return 0
    return sum(1 for k in SLOT_KEYS if slots.get(k))


def doc_text(content: str, slots: Any) -> str:
    """BM25 用的文档文本：statement + 槽位值。

    为什么不用全文：长原文里的「顺带一提」会制造半截 token 假匹配（P1 期间
    `[2026-07-02] DeepSeek-R1…--tensor-s` 这种截断尾巴一度把榜首跑偏）。
    slots 为空/解析失败 → 回落到 content，行为与改造前一致（向后兼容）。
    """
    if not slots:
        return content or ""
    if isinstance(slots, str):
        try:
            slots = json.loads(slots)
        except Exception:
            return content or ""
    if not isinstance(slots, dict):
        return content or ""
    parts: List[str] = [slots.get("statement") or ""]
    for k in SLOT_KEYS:
        v = slots.get(k)
        if isinstance(v, (list, tuple)):
            parts.extend(str(x) for x in v)
        elif v:
            parts.append(str(v))
    seen, uniq = set(), []
    for p in (x.strip() for x in parts):
        if p and p not in seen:          # statement 常与某条 decision 是同一句，去重省 token
            seen.add(p)
            uniq.append(p)
    txt = "；".join(uniq)
    return txt or (content or "")


def to_meta(slots: Dict[str, Any], raw_len: int) -> Dict[str, Any]:
    """写库 metadata 片段。

    slots 用 **JSON 字符串**（不是 dict）：Chroma 的 metadata 只接受标量，
    dict 会直接抛错；PG 侧 add_memory 再 json.loads 回 jsonb 列。
    """
    return {
        "slots": json.dumps(slots, ensure_ascii=False, separators=(",", ":")),
        "raw_len": int(raw_len),
    }
