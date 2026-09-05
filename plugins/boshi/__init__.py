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


class BoshiMemoryProvider(MemoryProvider):
    """Hermes 原生 memory provider，桥接 ~/.boshi 数据层（ChromaDB + bge-m3）。"""

    def __init__(self, config: Optional[Dict[str, Any]] = None) -> None:
        self._config = config or {}
        self._core = None          # boshi_core 模块（惰性导入）
        self._session_id = ""
        self._agent_context = "primary"
        # prefetch 缓存（queue_prefetch 后台线程写入，prefetch 读取）
        self._prefetch_cache = ""
        self._prefetch_count = 0
        self._prefetch_query = ""
        self._prefetch_time = 0.0  # 缓存写入时间戳
        # 压缩提炼去重：记录上次 on_pre_compress 已提炼的消息 id 集合
        self._compressed_msg_ids: set = set()

    # ------------------------------------------------------------------
    # 基础
    # ------------------------------------------------------------------

    @property
    def name(self) -> str:
        return "boshi"

    def is_available(self) -> bool:
        """检查 ~/.boshi 是否已安装（boshi_core.py + chroma_db 目录）。"""
        has_code = (BOSHI_HOME / "boshi_core.py").exists()
        has_db = (BOSHI_HOME / "chroma_db").is_dir()
        return has_code and has_db

    def unavailable_reason(self) -> str:
        reasons = []
        if not (BOSHI_HOME / "boshi_core.py").exists():
            reasons.append("未找到 ~/.boshi/boshi_core.py")
        if not (BOSHI_HOME / "chroma_db").is_dir():
            reasons.append("未找到 ~/.boshi/chroma_db/ 目录")
        return "；".join(reasons) if reasons else "伯仕记忆系统未安装"

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
                    if not content:
                        continue
                    score = x.get("score", 0)
                    src = x.get("source", "")
                    lines.append(f"- [{score:.2f}|{src}] {content}")

                # 改进2：历史会话补充召回
                hist = self._search_session_history(query)
                for h in hist:
                    lines.append(f"- [hist] {h}")

                # 改进4：扩展查询——从结果中提取可能的实体名，再搜一次
                expanded_lines = self._expand_query_search(query, lines)
                if expanded_lines:
                    lines.extend(expanded_lines[:3])  # 最多添加3条扩展结果

                self._prefetch_count = len(results) + len(hist)
                self._prefetch_query = query
                if lines:
                    self._prefetch_cache = "## 伯仕记忆召回\n" + "\n".join(lines[:10])
                else:
                    self._prefetch_cache = ""
                self._prefetch_time = time.time()
            except Exception as e:
                logger.debug("boshi queue_prefetch failed: %s", e)
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
            return ""
        # 改进3：有缓存且 5 分钟内 → 毫秒级返回，不触发超时
        if self._prefetch_cache and time.time() - self._prefetch_time < 300:
            return self._prefetch_cache
        # 首轮/缓存过期：同步检索兜底（保证每轮都有记忆注入）
        try:
            result = self._core.search(query, top_k=5, source="all")
            results = result.get("results", [])
            self._prefetch_count = len(results)
            self._prefetch_query = query
            lines = []
            for x in results:
                content = x.get("content", "")
                if not content:
                    continue
                score = x.get("score", 0)
                src = x.get("source", "")
                lines.append(f"- [{score:.2f}|{src}] {content}")
            hist = self._search_session_history(query)
            for h in hist:
                lines.append(f"- [hist] {h}")
            self._prefetch_count += len(hist)
            self._prefetch_time = time.time()
            if not lines:
                return ""
            return "## 伯仕记忆召回\n" + "\n".join(lines[:8])
        except Exception as e:
            logger.debug("boshi prefetch failed: %s", e)
            return ""

    def recall_status(self) -> Optional[RecallStatus]:
        """向 UI 展示上一轮召回条数。"""
        if self._prefetch_count > 0:
            return RecallStatus("boshi", self._prefetch_count, glyph="🦄")
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

        记录两类内容：
          1. 用户消息（topic=conversation，≤500 字符，跳过 trivial）
          2. 助手结论（topic=assistant_conclusion，≤800 字符）——
             即 agent 输出到屏幕的最终答复。思考推理过程不记，
             但推理的成果（结论/交付说明）是记忆的价值所在：
             新会话靠它知道"当时解决了什么、结论是什么"。
        """
        if self._core is None or self._agent_context != "primary":
            return
        try:
            # 1) 用户消息（原有行为）
            if user_content and not is_trivial_prompt(user_content):
                content = user_content.strip()
                if len(content) > 500:
                    content = content[:500] + "…"
                self._core.save(
                    content,
                    topic="conversation",
                    metadata={
                        "source": "hermes_plugin",
                        "session_id": session_id or self._session_id,
                        "role": "user",
                    },
                )
            # 2) 助手结论（新增）
            #    结论不套用 is_trivial_prompt：简短结论（如"已修复"）反而是
            #    高价值的精炼记忆，不该被问候语过滤器误杀。只要非空且非
            #    纯工具噪音即保存。截断到 800 字符。
            asst = (assistant_content or "").strip()
            if asst and _is_capture_worthy_conclusion(asst):
                if len(asst) > 800:
                    asst = asst[:800] + "…"
                self._core.save(
                    asst,
                    topic="assistant_conclusion",
                    metadata={
                        "source": "hermes_plugin",
                        "session_id": session_id or self._session_id,
                        "role": "assistant",
                    },
                )
        except Exception as e:
            logger.debug("boshi sync_turn failed: %s", e)

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
            logger.debug("boshi on_memory_write failed: %s", e)

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
                            content[:500],
                            topic="conversation",
                            metadata={"source": "hermes_plugin", "role": "user", "stage": "pre_compress"},
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
