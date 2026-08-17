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
from pathlib import Path
from typing import Any, Dict, List, Optional

from agent.memory_provider import MemoryProvider, RecallStatus, is_trivial_prompt
from tools.registry import tool_error

logger = logging.getLogger(__name__)

# 伯仕部署目录（与 boshi_core.py / boshi_mcp_server.py 一致）
BOSHI_HOME = Path.home() / ".boshi"


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

    # ------------------------------------------------------------------
    # 基础
    # ------------------------------------------------------------------

    @property
    def name(self) -> str:
        return "boshi"

    def is_available(self) -> bool:
        """检查 ~/.boshi 是否已安装（boshi_core.py + chroma_db 目录）。"""
        return (BOSHI_HOME / "boshi_core.py").exists() and (BOSHI_HOME / "chroma_db").is_dir()

    def unavailable_reason(self) -> str:
        return (
            "未找到 ~/.boshi/boshi_core.py（或 chroma_db 目录）。"
            "请先安装伯仕记忆系统：curl -sL "
            "https://raw.githubusercontent.com/wulezhi-hui/boshi-memory-system/main/install.sh | bash"
        )

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
        try:
            import boshi_core  # type: ignore
            self._core = boshi_core
            logger.info("boshi memory provider initialized (session=%s, ctx=%s)", session_id, self._agent_context)
        except Exception as e:
            logger.warning("boshi provider initialize failed: %s", e)
            self._core = None

    def shutdown(self) -> None:
        self._core = None
        self._prefetch_cache = ""
        self._prefetch_count = 0

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

    def queue_prefetch(self, query: str, *, session_id: str = "") -> None:
        """后台线程检索，结果供下一轮 prefetch 消费（不阻塞对话）。"""
        if is_trivial_prompt(query) or self._core is None:
            return

        def _run() -> None:
            try:
                result = self._core.search(query, top_k=5, source="all")
                results = result.get("results", [])
                self._prefetch_count = len(results)
                self._prefetch_query = query
                if results:
                    lines = []
                    for x in results:
                        content = x.get("content", "")
                        if not content:
                            continue
                        score = x.get("score", 0)
                        src = x.get("source", "")
                        lines.append(f"- [{score:.2f}|{src}] {content}")
                    self._prefetch_cache = "## 伯仕记忆召回\n" + "\n".join(lines[:5])
                else:
                    self._prefetch_cache = ""
            except Exception as e:
                logger.debug("boshi queue_prefetch failed: %s", e)
                self._prefetch_cache = ""

        threading.Thread(target=_run, daemon=True).start()

    def prefetch(self, query: str, *, session_id: str = "") -> str:
        """返回召回结果。优先读后台缓存；无缓存时同步检索兜底。"""
        if is_trivial_prompt(query) or self._core is None:
            return ""
        if self._prefetch_cache:
            return self._prefetch_cache
        # 首轮无缓存：同步检索（保证每轮都有记忆注入）
        try:
            result = self._core.search(query, top_k=5, source="all")
            results = result.get("results", [])
            self._prefetch_count = len(results)
            self._prefetch_query = query
            if not results:
                return ""
            lines = []
            for x in results:
                content = x.get("content", "")
                if not content:
                    continue
                score = x.get("score", 0)
                src = x.get("source", "")
                lines.append(f"- [{score:.2f}|{src}] {content}")
            return "## 伯仕记忆召回\n" + "\n".join(lines[:5])
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
        """每轮对话后把用户消息存入伯仕记忆库（仅 primary 上下文）。"""
        if self._core is None or self._agent_context != "primary":
            return
        if not user_content or is_trivial_prompt(user_content):
            return
        try:
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
        except Exception as e:
            logger.debug("boshi sync_turn failed: %s", e)

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
        """压缩前把即将丢弃的用户消息存入伯仕。"""
        if self._core is None:
            return ""
        saved = 0
        for m in messages:
            if m.get("role") == "user":
                c = m.get("content", "")
                if isinstance(c, str) and c.strip() and not is_trivial_prompt(c):
                    try:
                        self._core.save(
                            c.strip()[:500],
                            topic="conversation",
                            metadata={"source": "hermes_plugin", "role": "user", "stage": "pre_compress"},
                        )
                        saved += 1
                    except Exception:
                        break
        return ""

    # ------------------------------------------------------------------
    # 备份
    # ------------------------------------------------------------------

    def backup_paths(self) -> List[str]:
        """声明 boshi 数据目录，纳入 hermes backup。"""
        return [str(BOSHI_HOME / "chroma_db"), str(BOSHI_HOME / "memory")]
