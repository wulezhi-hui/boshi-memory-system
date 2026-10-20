"""会话事实层（P3 / 2026-10-20）—— 零模型滚动累积「决策 + 待办」。

为什么单独一个模块：
  - 插件（`plugins/boshi/__init__.py`）只做接线，逻辑放这里能被 `tests/` 直接
    import 单测，不用为了测一个纯函数去 stub Hermes 的 `agent.memory_provider`。
  - 与 `slots.py` 同源：条目全部来自 `slots.extract()` 抽到的**原文片段**，
    本模块只做搬运与去重，**不生成、不改写、不总结**（乐之的铁律：语料/记忆
    不用模型生成内容）。

为什么需要它（而不是每轮再存一条 conversation）：
  `task_conclusion` 只在 `on_session_end` / `on_pre_compress` 写，而本机压缩被
  billion-context 代理接管、CLI 会话常被强杀 → 钩子几乎不触发（2026-10-20 实测
  全库仅 1 条）。于是事实层等于空的：下个会话问「上次那个定下来没有」召回不到。
  这里改由 `sync_turn` 每轮**门控**写入：
    - 当轮用户/助手消息里抽到 decisions / open_questions 才写，没抽到整轮跳过；
    - 累积写入 `(session_id, topic='session_fact')`，靠 `upsert=True` 只留 1 条有效行，
      旧行 `is_latest=false` 留痕（P3 版本化）。
  门控是必要的：若无条件每轮写，事实层就是 `conversation` 的复制品，库会被滚脏。
"""

from __future__ import annotations

import json
from typing import Any, Dict, List, Optional

FACT_TOPIC = "session_fact"
FACT_CAP = 10          # 每个小节最多留多少条，超出丢最旧的

# 只累积语义型槽位。files/commands/ports/versions 是「话题特征」不是「事实结论」，
# 累积起来既长又吵，且它们在 conversation 行的 slots 列里本来就有。
FACT_SECTIONS = (("decisions", "决策"), ("open_questions", "待办"))

_HEADERS = {label: key for key, label in FACT_SECTIONS}     # "决策" → "decisions"


def _norm(s: str) -> str:
    """比较用的归一化：压空白、去首尾。不改字符内容（原文保真）。"""
    return " ".join(str(s).split())


def items_from_slots(slots_meta: Optional[Dict[str, Any]]) -> Dict[str, List[str]]:
    """从 `slots.to_meta()` 的返回里取条目。

    `slots_meta["slots"]` 是 **JSON 字符串**（Chroma 只收标量，见 slots.to_meta）。
    解析失败/无槽位 → 返回空 dict（调用方据此整轮跳过写入）。
    """
    raw = (slots_meta or {}).get("slots")
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except Exception:
            return {}
    if not isinstance(raw, dict):
        return {}
    out: Dict[str, List[str]] = {}
    for key, _label in FACT_SECTIONS:
        vals = raw.get(key) or []
        got = [_norm(v) for v in vals if _norm(v)]
        if got:
            out[key] = got
    return out


def parse(content: Optional[str]) -> Dict[str, List[str]]:
    """把已存的 session_fact 正文解析回 {key: [条目]}（与 render 互逆）。"""
    out: Dict[str, List[str]] = {k: [] for k, _ in FACT_SECTIONS}
    cur = None
    for line in (content or "").splitlines():
        s = line.strip()
        if s.endswith("："):
            # 已知小节才切换；未知小节（"杂项："）必须**清空** cur，否则它下面的
            # 条目会被错误地算进上一个小节（实测会把杂项条目并进「决策」）。
            cur = _HEADERS.get(s[:-1])
            continue
        if cur and s.startswith("- "):
            v = _norm(s[2:])
            if v:
                out[cur].append(v)
    return out


def render(items: Dict[str, List[str]]) -> str:
    """渲染成写库正文。空小节不出现（避免两行空标题）。

    条目先过 `_norm`：一行一条是解析的前提，条目内换行/连续空格压成单空格
    （否则 `parse` 只能读到第一行，后半句永久丢失）。
    """
    parts = []
    for key, label in FACT_SECTIONS:
        vals = [_norm(v) for v in (items.get(key) or []) if _norm(v)]
        if not vals:
            continue
        parts.append(label + "：")
        parts.extend("- " + v for v in vals)
    return "\n".join(parts)


def merge(prev: Optional[str], new_items: Dict[str, List[str]],
          cap: int = FACT_CAP) -> Optional[str]:
    """把本轮抽到的条目并入已有正文。

    返回 None 的两种情况（都表示**不该写库**）：
      1. 本轮没有抽到任何条目 → 整轮跳过；
      2. 抽到的条目全都已在旧正文里 → 内容没变化，重写只会白白 churn 一行。
    条目顺序：旧的在前、新的在后（时间序），超过 `cap` 丢最旧的——这是「滚动
    事实」的语义：留着最近的结论，不是留全部清单。
    """
    if not new_items:
        return None
    old = parse(prev)
    new_norm = {k: [_norm(v) for v in (new_items.get(k) or []) if _norm(v)]
                for k, _ in FACT_SECTIONS}
    if not any(new_norm.values()):
        return None
    old_all = set()
    for key, _label in FACT_SECTIONS:
        old_all.update(old.get(key) or [])
    changed = any(v not in old_all for vals in new_norm.values() for v in vals)

    # 跨小节去重：「决策」优先占位。slots 的模式会**同时命中同一句**
    # （实测「决定事实层只在抽到决策或待办时才写库」被抽进 decisions 与
    # open_questions 两边），不去重就会在正文里把一句话列两遍——那正是
    # 「垃圾注入」的来源，宁可少一节也不能重复。
    placed = set()
    out: Dict[str, List[str]] = {}
    for key, _label in FACT_SECTIONS:
        seq: List[str] = []
        for v in (old.get(key) or []) + new_norm.get(key, []):
            if v in placed:           # 已在前面的小节里出现 → 跳过
                continue
            seq.append(v)
            placed.add(v)
        if len(seq) > cap:            # 超上限丢**最旧**的（滚动事实语义）
            seq = seq[-cap:]
        out[key] = seq
    if not changed:                   # 全是旧条目 → 内容没变化，不该写新版本
        return None
    text = render(out)
    return text or None
