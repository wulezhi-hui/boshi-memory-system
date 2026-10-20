"""
伯仕记忆系统 — Hermes Memory Provider 插件
=============================================
以 Hermes 原生 memory provider 方式接入伯仕记忆系统（boshi）。

与 MCP 方式（boshi_mcp_server.py）的区别：
  - MCP    : agent 主动调用工具（boshi_search / boshi_save ...）
  - 插件    : Hermes 每轮自动 prefetch（记忆预取）+ sync_turn（自动存储）
             + system_prompt_block（画像/热区注入），无需 agent 记得调用

安装：
  cp -r plugins/boshi $HERMES_HOME/plugins/boshi/
  # config.yaml:
  #   memory:
  #     provider: boshi

接口实现参照 Hermes 的 MemoryProvider ABC：
  agent/memory_provider.py（hermes-agent 源码）

数据层复用 ~/.boshi/boshi_core.py（与 MCP Server / CLI 共享同一底层）。
"""

from __future__ import annotations

import json
import logging
import os
import sys
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from agent.memory_provider import MemoryProvider, RecallStatus, is_trivial_prompt
from tools.registry import tool_error

logger = logging.getLogger(__name__)

# 伯仕部署目录（与 boshi_core.py / boshi_mcp_server.py 一致）
BOSHI_HOME = Path.home() / ".boshi"

# ── 诊断日志（2026-10-10 排查"🦄 图标不出现"时加；BOSHI_DIAG_LOG=0 可关闭）──
# 为什么需要：召回/写入都在 try/except 里，失败默认静默；而"图标出不出现"取决于
# ① prefetch 是否有内容 ② recall_status 是否非 None —— 只有把这两个事实打出来才能定位。
_DIAG = os.environ.get("BOSHI_DIAG_LOG", "1") != "0"
_diag_seen: set = set()


def _diag(msg: str, *args) -> None:
    if _DIAG:
        logger.info("boshi[诊断] " + msg, *args)


def _diag_once(key: str, msg: str, *args) -> None:
    if _DIAG and key not in _diag_seen:
        _diag_seen.add(key)
        logger.info("boshi[诊断] " + msg, *args)


def _ensure_runtime_deps() -> bool:
    """确保伯仕数据层需要的第三方依赖可导入（PG 后端要 psycopg，Chroma 后端要 chromadb）。

    背景（2026-10-10 事故）：WebUI 的会话 worker 跑在**裸的 tools python** 上
    （`hermes/tools/python-3.14.7...`，site-packages 为空），于是 psycopg/chromadb
    都找不到 → `is_available()` 返回 False → **整个记忆 provider 被停用**：
    无召回、🦄 图标不出现、对话不入库，而启动日志看不出异常。

    修法：把**与当前解释器同版本**的 Hermes venv site-packages 挂到 sys.path 末尾
    （版本必须匹配，否则原生扩展 ABI 不符会崩；挂载失败会自动回滚）。
    """
    try:
        import psycopg  # noqa: F401
        return True
    except Exception:  # noqa: BLE001
        pass
    home = os.environ.get("HERMES_HOME") or str(Path.home() / "AppData" / "Local" / "hermes")
    ver = "python%d.%d" % sys.version_info[:2]
    cands = []
    for base in (Path(home) / "hermes-agent" / ".venv", Path(home) / "hermes-agent" / "venv"):
        cands.append(base / ("Lib/site-packages" if os.name == "nt" else "lib/%s/site-packages" % ver))
    for p in cands:
        if not p.is_dir() or str(p) in sys.path:
            continue
        sys.path.append(str(p))          # append：不抢当前环境已有的包
        try:
            import psycopg  # noqa: F401
            _diag_once("deps", "当前环境缺依赖 → 已挂载 %s（psycopg=%s、chromadb 等随其可用）",
                       p, getattr(psycopg, "__version__", "?"))
            return True
        except Exception:  # noqa: BLE001
            try:
                sys.path.remove(str(p))  # 回滚，避免污染
            except ValueError:
                pass
    _diag_once("deps", "当前环境缺 psycopg，且未找到可用的备选 site-packages（候选：%s）",
               "、".join(str(c) for c in cands))
    return False

# ── 召回质量过滤（2026-10-11 用户反馈：召回里混进"好了""在h盘"这类超短碎片）──
# 规则：去空白后短于阈值（默认 5 字符）→ 丢弃；去掉标点/数字/空白后没有实质字词 → 丢弃。
#   （实测噪声样本："好了"2 / "在h盘"3 / "开搞"2 / "启动了"3 / "开始"2 → 阈值 5 全覆盖；
#     "重启网关了"(5) 这类仍保留，避免误伤有效短信息）
# 只作用于**召回注入**（prefetch），不影响显式检索工具（boshi_search 仍返回全部）。
# 可用 BOSHI_RECALL_MIN_LEN 调整阈值。
_RECALL_MIN_LEN = int(os.environ.get("BOSHI_RECALL_MIN_LEN", "5"))
# 每 query 召回缓存 TTL（秒）。设 0 等价于不缓存。
_PCACHE_TTL = int(os.environ.get("BOSHI_PCACHE_TTL", "300"))


def _worth_recalling(text: str) -> bool:
    """该条内容是否值得注入召回上下文（滤掉超短/无信息量碎片）。"""
    import re
    s = (text or "").strip()
    if len(s) < _RECALL_MIN_LEN:
        return False
    core = re.sub(r"[\s\W\d_]+", "", s, flags=re.UNICODE)   # 去掉空白/标点/数字后剩下的实词
    return len(core) >= 2


# 助手结论中常见的"纯工具噪音"标记：这些前缀/模式表示该输出主要是
# 工具回显（JSON/状态码/路径列表），不是面向用户的结论，不值得入库。
_TOOL_NOISE_MARKERS = (
    "```json",
    "```bash",
    "{\"",
    'success": true',
    'exit_code',
    "Tool result",
    "工具结果",
    "已执行",
)


_SLOTS_MOD = None
_FACT_MOD = None


def _boshi_slots():
    """按文件路径加载 ~/.boshi/slots.py（P2 零模型槽位抽提器）。

    不用 `import slots`：那是第三方同名模块的高危名字；按路径加载可控且不污染 sys.path。
    """
    global _SLOTS_MOD
    if _SLOTS_MOD is not None:
        return _SLOTS_MOD
    import importlib.util

    p = BOSHI_HOME / "slots.py"
    spec = importlib.util.spec_from_file_location("boshi_slots", str(p))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    _SLOTS_MOD = mod
    return mod


def _boshi_session_fact():
    """按文件路径加载 ~/.boshi/session_fact.py（P3 事实层滚动累积，零模型）。

    同样避免 `import session_fact` 这种可能撞名的裸导入；按路径加载也便于
    `tests/` 直接单测同一个文件，两边跑的是同一份逻辑。
    """
    global _FACT_MOD
    if _FACT_MOD is not None:
        return _FACT_MOD
    import importlib.util

    p = BOSHI_HOME / "session_fact.py"
    spec = importlib.util.spec_from_file_location("boshi_session_fact", str(p))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    _FACT_MOD = mod
    return mod


def _write_slots_enabled() -> bool:
    """P2 写入模式开关：True=原文全存+抽槽位；False=回滚到旧的截断行为。

    关键：**截断与否只看这个开关**，不看抽提结果。抽提失败（slots 为空）只是
    少一份结构化元数据，不该顺带把原文砍掉——那样零槽位的那 12% 长文会被
    静默截断，直接违背 P2「原文全存」的目标（2026-10-19 复核发现并修正）。
    """
    return os.environ.get("BOSHI_WRITE_SLOTS", "1") != "0"


def _slots_meta(text: str) -> Dict[str, Any]:
    """P2：抽槽位并返回写库 metadata 片段（slots=JSON 字符串 + raw_len）。

    `BOSHI_WRITE_SLOTS=0` → 返回 {}，调用方回落到旧行为（截断 800 + 不写槽位），
    即一键回滚，不需要改代码。抽提失败同样返回 {}，绝不阻断记忆写入。
    """
    if not _write_slots_enabled():
        return {}
    try:
        mod = _boshi_slots()
        return mod.to_meta(mod.extract(text), len(text))
    except Exception as e:            # 抽提是增强项，坏了不能影响主流程
        logger.debug("slots 抽提失败（跳过，按旧逻辑写入）: %s", e)
        return {}


def _is_capture_worthy_conclusion(text: str) -> bool:
    """判断助手输出是否值得存入记忆（assistant_conclusion）。

    设计意图：思考推理过程不记，但"输出到屏幕的结论"应该记——
    那是智能体推理完之后的成果。规则：
      - 纯工具回显（JSON/代码块/exit_code）不算结论，跳过；
      - 纯格式噪音（长度 < 12 且无标点，如 "OK" / "done"）跳过；
      - 其余非空输出视为结论。
    """
    if not text:
        return False
    stripped = text.strip()
    if not stripped:
        return False
    # 纯工具回显/代码块开头 → 跳过
    first_line = stripped.splitlines()[0][:40] if stripped.splitlines() else ""
    for marker in _TOOL_NOISE_MARKERS:
        if marker in first_line:
            return False
    # 超短无标点输出（OK/done/已好）无记忆价值
    if len(stripped) < 12 and not any(ch in stripped for ch in "。！？!?，,；;："):
        return False
    return True


def _resolve_plugin_profile() -> str:
    """当前 profile 的记忆归属标识。

    Hermes 的 profile 是进程级隔离的（每个 profile 一个 gateway 进程），
    所以此处解析出的标识即"写入方"，用于记忆打标与归属检索：
      BOSHI_PROFILE 环境变量 > get_hermes_home() 推导 > "default"
    """
    env = (os.environ.get("BOSHI_PROFILE") or "").strip()
    if env:
        return env
    home = ""
    try:
        from hermes_constants import get_hermes_home  # Hermes 内部工具（同进程可导入）
        home = str(get_hermes_home())
    except Exception:
        home = os.environ.get("HERMES_HOME", "")
    norm = home.replace("\\", "/").rstrip("/")
    if norm:
        name = norm.rsplit("/", 1)[-1]
        parent = norm.rsplit("/", 2)[-2] if "/" in norm else ""
        if parent == "profiles" and name:
            return name
    return "default"


class BoshiMemoryProvider(MemoryProvider):
    """Hermes 原生 memory provider，桥接 ~/.boshi 数据层（ChromaDB + bge-m3）。"""

    def __init__(self, config: Optional[Dict[str, Any]] = None) -> None:
        self._config = config or {}
        # 确立本进程的记忆归属标识：下游所有写入/检索经 chroma_bridge.resolve_profile()
        # 读取 BOSHI_PROFILE，从而自动带上本 profile 的标识（环境变量已显式设置则尊重之）
        self._profile = _resolve_plugin_profile()
        try:
            os.environ.setdefault("BOSHI_PROFILE", self._profile)
        except Exception:
            pass
        self._core = None          # boshi_core 模块（惰性导入）
        self._session_id = ""
        self._agent_context = "primary"
        # prefetch 缓存（queue_prefetch 后台线程写入，prefetch 读取）
        self._prefetch_cache = ""
        self._prefetch_count = 0
        self._prefetch_query = ""
        self._prefetch_time = 0.0  # 缓存写入时间戳
        # 每 query 一份召回缓存（2026-10-19 新增）：原先只有一个槽位、5 分钟内
        # 不校验 query 就直接返回——同一会话里第 2 轮起拿到的都是第 1 轮问题的
        # 召回结果。改为按 query 指纹缓存（LRU 8 条，TTL 300s）。
        self._pcache: Dict[str, tuple] = {}
        # 压缩提炼去重：记录上次 on_pre_compress 已提炼的消息 id 集合
        self._compressed_msg_ids: set = set()

    # ------------------------------------------------------------------
    # 基础
    # ------------------------------------------------------------------

    @property
    def name(self) -> str:
        return "boshi"

    def is_available(self) -> bool:
        """后端感知的可用性检查（2026-10-10 修，事故驱动）。

        - `backend = pg`     → 要求 `psycopg` 可导入。**缺它会让所有 PG 查询静默失败**：
          prefetch 拿不到记忆 → 🦄 图标消失、记忆也不写入（异常被吞在 debug 级）
        - `backend = chroma` → 检查 `chroma_db` 目录（原逻辑）

        注意：**不在这里探测数据库是否在线** —— PG 由 Windows 服务 / 钩子按需拉起，
        在这里判"不可用"会让 provider 被停用、图标消失。
        """
        if not (BOSHI_HOME / "boshi_core.py").exists():
            _diag_once("avail", "is_available=False（缺 ~/.boshi/boshi_core.py）")
            return False
        _ensure_runtime_deps()   # 裸环境（WebUI worker）先补齐 psycopg/chromadb 等
        try:
            if str(BOSHI_HOME) not in sys.path:
                sys.path.insert(0, str(BOSHI_HOME))
            import chroma_bridge
            if chroma_bridge.backend_name() == "pg":
                import psycopg  # noqa: F401  （PG 后端的运行时必需依赖）
                _diag_once("avail", "is_available=True（backend=pg，psycopg=%s）", psycopg.__version__)
                return True
            ok = (BOSHI_HOME / "chroma_db").is_dir()
            _diag_once("avail", "is_available=%s（backend=chroma）", ok)
            return ok
        except Exception as e:  # noqa: BLE001
            _diag_once("avail", "is_available=False（异常：%s）→ provider 会被停用，图标必然不出现", e)
            return False

    def unavailable_reason(self) -> str:
        reasons = []
        if not (BOSHI_HOME / "boshi_core.py").exists():
            reasons.append("未找到 ~/.boshi/boshi_core.py")
        try:
            if str(BOSHI_HOME) not in sys.path:
                sys.path.insert(0, str(BOSHI_HOME))
            import chroma_bridge
            if chroma_bridge.backend_name() == "pg":
                try:
                    import psycopg  # noqa: F401
                except Exception:
                    reasons.append(
                        "PG 后端需要 psycopg，但当前 Python 环境没装"
                        "（装法：uv pip install --python <hermes venv>/Scripts/python.exe \"psycopg[binary]\"）")
            elif not (BOSHI_HOME / "chroma_db").is_dir():
                reasons.append("未找到 ~/.boshi/chroma_db/ 目录")
        except Exception as e:  # noqa: BLE001
            reasons.append("无法解析伯仕后端：%s" % e)
        return "；".join(reasons)   # 可用时返回空串（原来会误报"未安装"）

    def get_config_schema(self) -> List[Dict[str, Any]]:
        return []  # 本地 provider，无需额外配置

    def initialize(self, session_id: str, **kwargs) -> None:
        """连接 boshi 数据层（惰性导入 boshi_core）。"""
        self._session_id = session_id
        self._agent_context = kwargs.get("agent_context", "primary")

        # 确保 ~/.boshi 在 sys.path 上，才能 import boshi_core
        home = str(BOSHI_HOME)
        if home not in sys.path:
            sys.path.insert(0, home)
        # 裸环境（WebUI 会话 worker 用的是空 site-packages 的 tools python）先补齐依赖
        _ensure_runtime_deps()

        # 尝试导入 boshi_core（会触发 chromadb 等依赖加载）
        try:
            import boshi_core  # type: ignore
            self._core = boshi_core
            logger.info("boshi memory provider initialized (session=%s, ctx=%s)", session_id, self._agent_context)
        except ImportError as e:
            # 依赖缺失时的诊断和提示
            missing = str(e).split("'")[1] if "'" in str(e) else "未知依赖"
            self._core = None
            logger.warning(
                "boshi provider 初始化失败：缺少依赖 '%s'\n"
                "解决方案：\n"
                "  1. 运行 'pip install %s' 安装缺失依赖\n"
                "  2. 或重新运行安装脚本：python ~/.boshi/install.py",
                missing, missing
            )
        except Exception as e:
            logger.warning("boshi provider initialize failed: %s", e)
            self._core = None

        # 起步自检：真跑一次检索，把"依赖缺失 / 后端不可用"这类**静默失忆**在会话一开始就暴露。
        # 2026-10-10 事故：Hermes venv 缺 psycopg → 查询全炸在惰性 import 上，
        # 异常被吞 → 无召回、🦄 图标不出现、sync_turn 不写库，而启动日志全绿。
        if self._core is not None:
            try:
                self._core.search("__boshi_selfcheck__", top_k=1)
            except Exception as e:  # noqa: BLE001
                logger.warning(
                    "boshi 起步自检失败：记忆读/写很可能不可用（图标不出现、对话不入库）。"
                    "错误：%s", e)

    def shutdown(self) -> None:
        self._core = None
        self._prefetch_cache = ""
        self._prefetch_count = 0
        self._prefetch_time = 0.0

    # ------------------------------------------------------------------
    # 系统提示词注入（静态）
    # ------------------------------------------------------------------

    def system_prompt_block(self) -> str:
        """注入伯仕记忆状态 + 用户画像热区（每次会话组装一次）。"""
        if self._core is None:
            return ""
        try:
            st = self._core.status()
            total = st.get("total_memories", 0)
            nodes = st.get("knowledge_graph", {}).get("nodes", 0)
            pf = self._core.profile()
            hot = pf.get("hot_topic", "")
        except Exception as e:
            logger.debug("boshi system_prompt_block failed: %s", e)
            return ""
        lines = [
            "# 伯仕记忆系统（boshi provider）",
            f"记忆库 {total} 条 · 知识图谱 {nodes} 节点",
        ]
        if hot:
            lines.append(f"热区话题：{hot}")
        lines.append("对话前自动召回相关记忆；重要事实请用 memory 工具写入，将自动同步到伯仕。")
        return "\n".join(lines)

    # ------------------------------------------------------------------
    # 记忆预取（每轮自动召回）
    # ------------------------------------------------------------------

    def _search_session_history(self, query: str, limit: int = 4) -> List[str]:
        """改进2：从 Hermes 会话库 FTS 检索相关历史消息（补充伯仕向量召回）。

        解决"关键信息埋在历史对话里、没进伯仕向量库"的问题：当前对话与
        历史会话主题相关时，把历史片段也注入召回，供 agent 联想。
        只读 state.db 的 messages_fts_trigram（FTS5 trigram，支持中文子串），
        查询词切 3+ 字符片段做 OR 匹配；无命中时用 LIKE 兜底。离线、轻量。
        """
        try:
            from hermes_constants import get_hermes_home
            db_path = get_hermes_home() / "state.db"
            if not db_path.exists():
                return []
            import sqlite3
            conn = sqlite3.connect(str(db_path))
            conn.row_factory = sqlite3.Row

            # 1) 切查询词为 3+ 字符的片段（trigram 最小粒度），支持中文
            raw = query.strip()
            terms = []
            for tok in raw.replace("?", " ").replace("？", " ").split():
                if len(tok) >= 3:
                    terms.append(tok)
            # 中文整句没有空格：按 3-4 字符滑动窗口切
            if not terms:
                cjk = [ch for ch in raw if "\u4e00" <= ch <= "\u9fff"]
                if len(cjk) >= 3:
                    for i in range(0, max(len(cjk) - 2, 1)):
                        win = "".join(cjk[i:i + 3])
                        if win not in terms:
                            terms.append(win)
                            if len(terms) >= 6:
                                break
            if not terms:
                conn.close()
                return []

            rows = []
            # 2) 优先 trigram FTS（中文子串检索）
            try:
                match_q = " OR ".join(f'"{t}"' for t in terms)
                rows = conn.execute(
                    "SELECT role, substr(content, 1, 500) AS snippet, timestamp "
                    "FROM messages_fts_trigram "
                    "WHERE messages_fts_trigram MATCH ? "
                    "ORDER BY rank LIMIT ?",
                    (match_q, limit * 6),
                ).fetchall()
            except Exception:
                rows = []
            # 3) 兜底：LIKE 粗匹配（FTS 空/异常时）
            if not rows:
                like_rows = []
                for t in terms[:3]:
                    like_rows = conn.execute(
                        "SELECT role, substr(content, 1, 500) AS snippet, timestamp "
                        "FROM messages "
                        "WHERE content LIKE ? AND role IN ('user','assistant') "
                        "ORDER BY id DESC LIMIT ?",
                        (f"%{t}%", limit * 3),
                    ).fetchall()
                    if like_rows:
                        break
                rows = like_rows
            conn.close()

            results: List[str] = []
            seen = set()
            for r in rows:
                content = (r["snippet"] or "").strip()
                if not content or content in seen:
                    continue
                seen.add(content)
                first_line = content.splitlines()[0][:40] if content.splitlines() else ""
                if any(marker in first_line for marker in _TOOL_NOISE_MARKERS):
                    continue
                results.append(f"[历史会话:{r['role']}] {content}")
                if len(results) >= limit:
                    break
            return results
        except Exception as e:
            logger.debug("boshi _search_session_history failed: %s", e)
            return []

    # ---- 每 query 召回缓存（LRU 8 × TTL 300s）---------------------------
    @staticmethod
    def _qkey(query: str) -> str:
        """query 指纹：忽略空白与大小写差异，避免同一句话的细微排版不同而重复检索。"""
        import hashlib
        norm = "".join((query or "").split()).lower()
        return hashlib.md5(norm.encode("utf-8", "ignore")).hexdigest()

    def _pcache_get(self, query: str):
        """命中返回 (text, count)，过期返回 None。"""
        k = self._qkey(query)
        item = self._pcache.get(k)
        if not item:
            return None
        ts, text, count = item
        if time.time() - ts > _PCACHE_TTL:
            self._pcache.pop(k, None)
            return None
        return text, count

    def _pcache_put(self, query: str, text: str, count: int) -> None:
        if not query:
            return
        self._pcache[self._qkey(query)] = (time.time(), text or "", int(count or 0))
        while len(self._pcache) > 8:                  # 简单 LRU：丢掉最早写入的
            oldest = min(self._pcache, key=lambda k: self._pcache[k][0])
            self._pcache.pop(oldest, None)

    def queue_prefetch(self, query: str, *, session_id: str = "") -> None:
        """后台线程检索，结果供下一轮 prefetch 消费（不阻塞对话）。

        改进2：在伯仕向量召回之外，并行检索 Hermes 历史会话 FTS，
        合并注入，让 agent 能联想"上次类似任务"的记录。

        改进3：缓存写入时间戳，prefetch 可在 5 分钟内消费缓存，
        避免 Hermes 8 秒超时被静默跳过。

        改进4：扩展查询——从当前查询提取实体名，用关联实体名再搜一次。
        解决"微调"→找不到"2080Ti推理框架配置"这类跨话题断联问题。
        """
        if is_trivial_prompt(query) or self._core is None:
            return

        def _run() -> None:
            try:
                # 主查询
                result = self._core.search(query, top_k=5, source="all")
                results = result.get("results", [])
                lines = []
                for x in results:
                    content = x.get("content", "")
                    if not content or not _worth_recalling(content):
                        continue          # 2026-10-11 过滤超短碎片
                    score = x.get("score", 0)
                    src = x.get("source", "")
                    lines.append(f"- [{score:.2f}|{src}] {content}")

                # 改进2：历史会话补充召回
                hist = [h for h in self._search_session_history(query) if _worth_recalling(h)]
                for h in hist:
                    lines.append(f"- [hist] {h}")

                # 改进4：扩展查询——从结果中提取可能的实体名，再搜一次
                expanded_lines = self._expand_query_search(query, lines)
                if expanded_lines:
                    lines.extend(expanded_lines[:3])  # 最多添加3条扩展结果

                self._prefetch_count = sum(1 for x in results if _worth_recalling(x.get("content") or "")) + len(hist)
                self._prefetch_query = query
                if lines:
                    self._prefetch_cache = "## 伯仕记忆召回\n" + "\n".join(lines[:10])
                else:
                    self._prefetch_cache = ""
                self._prefetch_time = time.time()
                # 同时写入每 query 缓存：prefetch 按当轮 query 精确取用，
                # 不再"5 分钟内一律返回第一轮的召回"。
                self._pcache_put(query, self._prefetch_cache, self._prefetch_count)
            except Exception as e:
                logger.warning("boshi queue_prefetch 失败（本轮不会有记忆注入、🦄 图标也不会出现）: %s", e)
                self._prefetch_cache = ""
                self._prefetch_time = 0.0

        threading.Thread(target=_run, daemon=True).start()

    def _expand_query_search(self, query: str, existing_lines: List[str]) -> List[str]:
        """改进4：扩展查询检索——从当前结果中提取实体名，用关联实体再搜。

        解决跨话题断联问题：
        - 当前消息说"微调" → 可能指"2080Ti微调"、"推理框架配置"
        - 从历史会话召回的记忆中提取实体名（如"2080Ti"、"llama"、"vllm"）
        - 用这些实体名扩展查询，召回更多相关记忆

        只搜索不存储，纯查询操作，无副作用。
        """
        # 如果 _core 未加载（chromadb 缺失），直接返回空
        if self._core is None:
            return []
        try:
            # 1. 从现有召回结果中提取可能的实体名（跳过已知噪音）
            NOISE_MARKERS = ("[transformers]", "PyTorch", "model", " Models", "GPU1", "CPU", "vector", "score", "content", "metadata")
            entity_candidates = set()
            for line in existing_lines:
                # 提取可能的实体名（中文专有名词、英文驼峰、缩写、数字+字母组合）
                # 正则匹配：
                # - 大写缩写（GPU、API、LLM、GGUF）- 至少2个大写字母
                # - 数字+字母组合（2080Ti、v0.20、H3）- 至少1个数字+1个字母
                # - 驼峰命名（OpenAI、ComfyUI）- 至少3字符
                # - 英文技术术语（CUDA、Turing）- 至少3字符
                import re
                patterns = [
                    r'(?<![a-zA-Z])([A-Z]{2,})(?![a-zA-Z])',  # 大写缩写：GPU、API、LLM、GGUF
                    r'(\d+[a-zA-Z]+[a-zA-Z0-9]*)',  # 数字+字母：2080Ti、v0.20、H3
                    r'([a-zA-Z]{3,}(?=[\u4e00-\u9fff]|[A-Z]))',  # 驼峰前缀：OpenAI、ComfyUI
                    r'(?<![a-zA-Z])([a-zA-Z]{3,})(?![a-zA-Z])',  # 纯英文单词（至少3字符）
                ]
                for pattern in patterns:
                    for match in re.finditer(pattern, line):
                        candidate = match.group(0).strip()
                        if (candidate and len(candidate) >= 3
                            and candidate not in NOISE_MARKERS
                            and not candidate.startswith('[')  # 排除标记
                            and not candidate.endswith(']')  # 排除标记
                            and not candidate.isnumeric()):  # 排除纯数字
                            entity_candidates.add(candidate)

            # 2. 如果提取到实体名，用它们扩展查询
            if entity_candidates:
                # 限制扩展查询数量，避免超时
                expanded_queries = list(entity_candidates)[:3]
                expanded_results = []
                seen = set(l.split("] ", 1)[-1][:50] for l in existing_lines if "] " in l)

                for entity in expanded_queries:
                    try:
                        # 用实体名作为查询词，搜向量库
                        ext_result = self._core.search(entity, top_k=2, source="vector")
                        for x in ext_result.get("results", []):
                            content = x.get("content", "")
                            if content and content not in seen:
                                score = x.get("score", 0)
                                expanded_results.append(f"- [{score:.2f}|vector] {content}")
                                seen.add(content[:50])
                    except Exception:
                        pass

                return expanded_results
        except Exception as e:
            logger.debug("boshi _expand_query_search failed: %s", e)
        return []

    def prefetch(self, query: str, *, session_id: str = "") -> str:
        """返回召回结果。优先读后台缓存（5分钟内）；无缓存或过期时同步检索兜底。

        改进2：同步兜底同样合并历史会话 FTS 召回。
        改进3：缓存带 TTL（300秒），避免 Hermes 8 秒超时被跳过。
        """
        if is_trivial_prompt(query) or self._core is None:
            _diag("prefetch 提前退出：trivial=%s, _core 已连=%s → 本轮无召回、无图标",
                  is_trivial_prompt(query), self._core is not None)
            return ""
        # 缓存：按当轮 query 取值（2026-10-19 改）。旧逻辑只认"5 分钟内"，不看
        # query，导致同一会话第 2 轮起注入的永远是第一次提问的召回结果。
        hit = self._pcache_get(query)
        if hit is not None:
            text, count = hit
            self._prefetch_count = count
            _diag("prefetch 命中 per-query 缓存：count=%s，%d 字符", count, len(text))
            return text
        # 兼容旧缓存字段：只有 query 完全一致时才复用（防止跨问题串味）
        if (self._prefetch_cache and self._prefetch_query == query
                and time.time() - self._prefetch_time < 300):
            _diag("prefetch 命中缓存：count=%s，%d 字符 → 图标应显示", self._prefetch_count, len(self._prefetch_cache))
            return self._prefetch_cache
        # 首轮/缓存过期：同步检索兜底（保证每轮都有记忆注入）
        try:
            result = self._core.search(query, top_k=5, source="all")
            results = result.get("results", [])
            self._prefetch_count = sum(1 for x in results if _worth_recalling(x.get("content") or ""))
            self._prefetch_query = query
            lines = []
            for x in results:
                content = x.get("content", "")
                if not content or not _worth_recalling(content):
                    continue          # 2026-10-11 过滤超短碎片
                score = x.get("score", 0)
                src = x.get("source", "")
                lines.append(f"- [{score:.2f}|{src}] {content}")
            hist = [h for h in self._search_session_history(query) if _worth_recalling(h)]
            for h in hist:
                lines.append(f"- [hist] {h}")
            self._prefetch_count += len(hist)
            self._prefetch_time = time.time()
            _diag("prefetch 实时检索：count=%s，%d 行内容", self._prefetch_count, len(lines))
            if not lines:
                _diag("prefetch 检索结果为空 → 无召回注入、🦄 图标不出现")
                return ""
            text = "## 伯仕记忆召回\n" + "\n".join(lines[:8])
            self._pcache_put(query, text, self._prefetch_count)
            return text
        except Exception as e:
            logger.warning("boshi prefetch 失败（本轮无记忆召回、🦄 图标不会出现）: %s", e)
            return ""

    def recall_status(self) -> Optional[RecallStatus]:
        """向 UI 展示上一轮召回条数。"""
        if self._prefetch_count > 0:
            st = RecallStatus("boshi", self._prefetch_count, glyph="🦄")
            _diag("recall_status 已交给 UI：%s %s — recalled %s memories"
                  "（若界面仍无显示，问题在 UI 渲染层，不在记忆系统）",
                  st.glyph, st.provider_label, st.count)
            return st
        _diag("recall_status 返回 None（count=0）→ 图标不会出现")
        return None

    # ------------------------------------------------------------------
    # 每轮自动存储
    # ------------------------------------------------------------------

    def sync_turn(
        self,
        user_content: str,
        assistant_content: str,
        *,
        session_id: str = "",
        messages: Optional[List[Dict[str, Any]]] = None,
    ) -> None:
        """每轮对话后存入伯仕记忆库（仅 primary 上下文）。

        记录两类内容（P2 起**原文全存**，槽位另存 slots 列）：
          1. 用户消息（topic=conversation，跳过 trivial）
          2. 助手结论（topic=assistant_conclusion）——
             即 agent 输出到屏幕的最终答复。思考推理过程不记，
             但推理的成果（结论/交付说明）是记忆的价值所在：
             新会话靠它知道"当时解决了什么、结论是什么"。
        `BOSHI_WRITE_SLOTS=0` 才回到旧的 500/800 字截断（唯一回滚开关）。
        """
        if self._core is None or self._agent_context != "primary":
            return
        try:
            # 1) 用户消息
            #    P2 升级（2026-10-19）：原文全存，不再截断 500 字——截断的 500 字
            #    加上 "…" 后原文长度信息也丢了，实测全库 266 条截断行只有 25 条
            #    能从 state.db 找回原文（9%），其余永久损失。槽位照旧抽（端口/路径/
            #    参数值），BM25 文档仍用原文，不改排序口径。
            #    `BOSHI_WRITE_SLOTS=0` → 回滚到旧行为（截断 500 + 无槽位）。
            #    P3（2026-10-20）：抽提结果提到 `u_slots_meta` / `a_slots_meta` 两个
            #    外层变量，供第 3 步（会话事实层）复用，避免同一段文本抽两遍。
            u_slots_meta: Dict[str, Any] = {}
            a_slots_meta: Dict[str, Any] = {}
            if user_content and not is_trivial_prompt(user_content):
                content = user_content.strip()
                slots_meta = u_slots_meta = _slots_meta(content)
                if not _write_slots_enabled() and len(content) > 500:
                    content = content[:500] + "…"   # 回滚路径：与改造前完全一致
                self._core.save(
                    content,
                    topic="conversation",
                    metadata={
                        "source": "hermes_plugin",
                        "session_id": session_id or self._session_id,
                        "role": "user",
                        **slots_meta,
                    },
                )
            # 2) 助手结论（新增）
            #    结论不套用 is_trivial_prompt：简短结论（如"已修复"）反而是
            #    高价值的精炼记忆，不该被问候语过滤器误杀。只要非空且非
            #    纯工具噪音即保存。
            #    P2（2026-10-19）：不再截断到 800 字——原文全存，另抽槽位存
            #    slots/raw_len。BM25 只索引「statement+槽位值」，长原文不进索引，
            #    既保住完整语义又不让半截 token 制造假匹配。`BOSHI_WRITE_SLOTS=0`
            #    回到旧行为（截断 800 + 无槽位）。
            asst = (assistant_content or "").strip()
            if asst and _is_capture_worthy_conclusion(asst):
                slots_meta = a_slots_meta = _slots_meta(asst)
                if not _write_slots_enabled() and len(asst) > 800:
                    asst = asst[:800] + "…"          # 回滚路径：与改造前完全一致
                self._core.save(
                    asst,
                    topic="assistant_conclusion",
                    metadata={
                        "source": "hermes_plugin",
                        "session_id": session_id or self._session_id,
                        "role": "assistant",
                        **slots_meta,
                    },
                )
            # 3) 会话事实层（P3，2026-10-20）：零模型门控 + 滚动累积
            #    只在当轮抽到「决策 / 待办」条目时才写；没抽到 → 整轮跳过。
            #    门控是必要的：无条件每轮写，事实层就是 conversation 的复制品，
            #    只会把库滚脏、把检索位占满。
            #    写 topic=session_fact，按 (session_id, topic) upsert：旧行
            #    is_latest=false 留痕（v5.8 不可变日志），读路径只看到最新一条。
            #    内容 = 本会话累积的决策/待办清单，全部是 slots 抽到的**原文片段**，
            #    零 LLM、零生成（乐之铁律）。
            sid_fact = session_id or self._session_id
            if sid_fact:
                try:
                    _fact = _boshi_session_fact()
                    # P3.1（2026-10-20）：**只采用户侧**。助手侧的话天然充满
                    # 「我建议 / 下一步 / 可以切到」这类词，采进来等于把「菜单」
                    # 记成「点好的菜」——13:19 首条事实层就是助手列的选项被判成
                    # 决策、我的待办提议被判成本会话待办。助手内容仍在
                    # assistant_conclusion 里，一个字都不丢，只是不进事实层。
                    new_items: Dict[str, List[str]] = dict(
                        _fact.items_from_slots(u_slots_meta))
                    if new_items:       # 零条目 → 整轮跳过，连读库都省（每轮一次额外 SELECT 没必要）
                        text = _fact.merge(
                            self._core.get_latest_by_topic(sid_fact, _fact.FACT_TOPIC),
                            new_items)
                        if text:    # None = 内容未变（条目全在旧正文里）→ 不写新版本
                            self._core.save(
                                text,
                                topic=_fact.FACT_TOPIC,
                                metadata={
                                    "source": "hermes_plugin",
                                    "session_id": sid_fact,
                                    "role": "system",
                                },
                                upsert=True,
                            )
                except Exception as e:      # 事实层是增强项，坏了不能拖累主写入
                    logger.debug("boshi session_fact 写入跳过: %s", e)
        except Exception as e:
            logger.warning("boshi sync_turn 写入失败（这轮对话没进记忆库）: %s", e)

    def on_session_end(self, messages: List[Dict[str, Any]]) -> None:
        """会话结束时提炼任务结论（改进1：结论提炼）。

        把整场会话的关键信息合并成一条结构化 task_conclusion 存入伯仕，
        解决"结论分散在多轮、单轮片段搜不到"的问题。规则式提炼：
          - 收集用户侧非闲聊、带实质内容的提问/指令（≤300 字符/条）
          - 收集助手侧非工具噪音的结论（≤400 字符/条）
          - 汇总成"主题 + 关键轮次 + 最终结论"的结构化条目
        离线执行（不调 LLM），失败不阻断会话收尾。
        """
        if self._core is None or self._agent_context != "primary":
            return
        try:
            user_parts: List[str] = []
            asst_parts: List[str] = []
            for m in messages or []:
                role = m.get("role", "")
                content = (m.get("content") or "").strip()
                if not content:
                    continue
                if role == "user" and not is_trivial_prompt(content):
                    # 补充过滤：短寒暄/收尾（"好的谢谢"、"OK"）不构成用户关注点
                    if len(content) <= 12 and not any(
                        ch in content for ch in "？?。！!，,：:；;"
                    ):
                        continue
                    c = content if len(content) <= 300 else content[:300] + "…"
                    if c not in user_parts:
                        user_parts.append(c)
                elif role == "assistant" and _is_capture_worthy_conclusion(content):
                    c = content if len(content) <= 400 else content[:400] + "…"
                    if c not in asst_parts:
                        asst_parts.append(c)
            # 至少要有实质内容才沉淀，避免空会话写垃圾
            if not user_parts and not asst_parts:
                return
            # 只保留最近若干条，控制条目体积
            if len(user_parts) > 6:
                user_parts = user_parts[-6:]
            if len(asst_parts) > 6:
                asst_parts = asst_parts[-6:]
            lines = []
            if user_parts:
                lines.append("【用户关注】")
                lines.extend(f"- {p}" for p in user_parts)
            if asst_parts:
                lines.append("【结论】")
                lines.extend(f"- {p}" for p in asst_parts)
            summary = "\n".join(lines)
            if len(summary) > 2000:
                summary = summary[:2000] + "…"
            self._core.save(
                summary,
                topic="task_conclusion",
                metadata={
                    "source": "hermes_plugin",
                    "session_id": self._session_id,
                    "role": "assistant",
                    "stage": "session_end",
                },
                upsert=True,   # P3：同会话事实层只留 1 条有效记录（旧行降级 is_latest=false）
            )
            logger.info(
                "boshi on_session_end: saved task_conclusion (user=%d asst=%d)",
                len(user_parts),
                len(asst_parts),
            )
        except Exception as e:
            logger.debug("boshi on_session_end failed: %s", e)

    def on_memory_write(
        self,
        action: str,
        target: str,
        content: str,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> None:
        """镜像：agent 用内置 memory 工具写记忆时，同步存入伯仕。"""
        if action != "add" or not content or self._core is None:
            return
        try:
            self._core.save(
                content,
                topic="memory_tool",
                metadata={
                    "source": "hermes_memory_mirror",
                    "target": target,
                },
            )
        except Exception as e:
            logger.warning("boshi on_memory_write 镜像失败（内置记忆没同步进伯仕）: %s", e)

    # ------------------------------------------------------------------
    # 工具（context-only：手动检索交给 MCP 的 boshi_search 等）
    # ------------------------------------------------------------------

    def get_tool_schemas(self) -> List[Dict[str, Any]]:
        return []

    def handle_tool_call(self, tool_name: str, args: Dict[str, Any], **kwargs) -> str:
        return tool_error(f"Unknown tool: {tool_name}")

    # ------------------------------------------------------------------
    # 会话边界
    # ------------------------------------------------------------------

    def on_session_switch(
        self,
        new_session_id: str,
        *,
        parent_session_id: str = "",
        reset: bool = False,
        rewound: bool = False,
        **kwargs,
    ) -> None:
        self._session_id = new_session_id
        if reset:
            self._prefetch_cache = ""
            self._prefetch_count = 0

    def on_pre_compress(self, messages: List[Dict[str, Any]]) -> str:
        """压缩前提炼任务结论 + 存用户消息（长期会话主路径）。

        用户习惯长期保持一个会话、靠上下文压缩续命（不 /new、不 exit），
        on_session_end 因此几乎不触发。压缩才是他的"会话阶段边界"：
        - 把即将被压缩丢弃的实质消息（用户关注 + 助手结论）提炼成一条
          task_conclusion 存入伯仕（与 on_session_end 同格式）；
        - 仅提炼"新增"消息（按消息 id 去重），避免同段内容反复入库；
        - 保留原行为：逐条存用户消息（topic=conversation）。
        返回空字符串（不注入压缩摘要，避免干扰 Hermes 原生压缩）。
        """
        if self._core is None or self._agent_context != "primary":
            return ""
        try:
            new_msgs = []
            for m in messages or []:
                mid = m.get("id")
                if mid is not None and mid in self._compressed_msg_ids:
                    continue
                new_msgs.append(m)

            user_parts: List[str] = []
            asst_parts: List[str] = []
            saved = 0
            for m in new_msgs:
                role = m.get("role", "")
                content = (m.get("content") or "").strip()
                if not content:
                    continue
                if role == "user" and not is_trivial_prompt(content):
                    # 补充过滤：短寒暄/收尾不构成关注点
                    if len(content) <= 12 and not any(
                        ch in content for ch in "？?。！!，,：:；;"
                    ):
                        continue
                    try:
                        self._core.save(
                            content,        # P2：原文全存（不再截断 500）
                            topic="conversation",
                            metadata={"source": "hermes_plugin", "role": "user",
                                      "stage": "pre_compress",
                                      **_slots_meta(content)},
                        )
                        saved += 1
                    except Exception:
                        break
                    c = content if len(content) <= 300 else content[:300] + "…"
                    if c not in user_parts:
                        user_parts.append(c)
                elif role == "assistant" and _is_capture_worthy_conclusion(content):
                    c = content if len(content) <= 400 else content[:400] + "…"
                    if c not in asst_parts:
                        asst_parts.append(c)

            # 记录已处理的消息 id（去重），但只记录有内容的消息
            for m in new_msgs:
                mid = m.get("id")
                if mid is not None:
                    self._compressed_msg_ids.add(mid)
            # 防止集合无限增长：保留最近 4000 条 id
            if len(self._compressed_msg_ids) > 4000:
                self._compressed_msg_ids = set(
                    list(self._compressed_msg_ids)[-3000:]
                )

            # 结论提炼（与 on_session_end 同格式，只有实质内容才存）
            if user_parts or asst_parts:
                lines = []
                if user_parts:
                    lines.append("【用户关注】")
                    lines.extend(f"- {p}" for p in user_parts[-6:])
                if asst_parts:
                    lines.append("【结论】")
                    lines.extend(f"- {p}" for p in asst_parts[-6:])
                summary = "\n".join(lines)
                if len(summary) > 2000:
                    summary = summary[:2000] + "…"
                try:
                    self._core.save(
                        summary,
                        topic="task_conclusion",
                        metadata={
                            "source": "hermes_plugin",
                            "session_id": self._session_id,
                            "role": "assistant",
                            "stage": "pre_compress",
                        },
                        upsert=True,   # P3：同上
                    )
                except Exception as e:
                    logger.debug("boshi on_pre_compress conclusion save failed: %s", e)
            logger.info(
                "boshi on_pre_compress: saved %d user msgs, conclusion user=%d asst=%d",
                saved,
                len(user_parts),
                len(asst_parts),
            )
        except Exception as e:
            logger.debug("boshi on_pre_compress failed: %s", e)
        return ""

    # ------------------------------------------------------------------
    # 备份
    # ------------------------------------------------------------------

    def backup_paths(self) -> List[str]:
        """声明 boshi 数据目录，纳入 hermes backup。"""
        return [str(BOSHI_HOME / "chroma_db"), str(BOSHI_HOME / "memory")]
