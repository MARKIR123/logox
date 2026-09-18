"""分层上下文构建器 (HierarchicalContextBuilder)。

实现内核 ContextBuilder 协议契约：
1. 载入分层项目记忆 (LOGOX.md) 并 Top-Down (Root -> Cwd) 注入系统人设；
2. 发往模型 API 时源头过滤 ReasoningBlock（零思考链 Token 消耗）；
3. 遵循高低水位线执行无损换页压缩 (Tool Pruning + Sliding Window)；
4. 动态结合大模型真实 Usage 进行 Token 估算自适应校准。
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, List, Optional

from logox.context.compaction import Compactor
from logox.context.memory import ProjectMemory, find_project_memory
from logox.context.storage import SessionTranscriptWriter
from logox.context.tokens import TokenEstimator
from logox.kernel.events import Usage
from logox.kernel.loop import ContextBundle
from logox.kernel.messages import Message, ReasoningBlock

logger = logging.getLogger(__name__)

__all__ = ["HierarchicalContextBuilder"]


TURN_SUMMARY_PROMPT_INSTRUCTION = """## 回合交付与摘要规范 (Turn Summary Contract)
当且仅当你已完成本轮的所有工具调用、准备向用户输出最终答复（Final Answer）时，必须在回复正文的最末尾追加一段 `<turn_summary>` 标签，用简明扼要的一句话（不超过 40 字）总结本轮完成的核心工作与涉及的关键文件。

1. 严格的时机限制：
   - ✅ 仅在向用户交付最终答复时输出（即本步不再发起任何工具调用时）。
   - ❌ 严禁在调用工具的中间思考或过渡话术中输出该标签。

2. 格式与内容结构：
   <turn_summary>操作动词 + 解决的具体问题/任务 + 涉及的关键文件与验证结果</turn_summary>

3. 场景示范：
   - 代码编写/修改：
     <turn_summary>修复 normalizer.py 的管道符解析 bug，并运行 pytest 验证通过</turn_summary>
   - 纯排查/未修改文件：
     <turn_summary>分析 bus.py 中的事件分发链路，确认无阻塞式 sleep 调用</turn_summary>
   - 概念问答/技术咨询：
     <turn_summary>详细解答虚拟内存分页换入机制与高低水位防颠簸算法原理</turn_summary>"""


class HierarchicalContextBuilder:
    """工业级分层上下文构建器。"""

    def __init__(
        self,
        system: str = "",
        *,
        cwd: str | Path | None = None,
        session_id: str = "default_session",
        window_capacity: int = 128_000,
        max_budget_tokens: int = 80_000,
        target_budget_tokens: int = 40_000,
        transcript_writer: Optional[SessionTranscriptWriter] = None,
        estimator: Optional[TokenEstimator] = None,
        skill_manager: Any = None,
    ) -> None:
        self.base_system = system
        self.skill_manager = skill_manager
        self.cwd = Path(cwd).resolve() if cwd else Path.cwd().resolve()
        self.session_id = session_id
        self.window_capacity = window_capacity

        self.estimator = estimator or TokenEstimator()
        self.writer = transcript_writer or SessionTranscriptWriter(
            session_id=session_id
        )

        self.compactor = Compactor(
            window_capacity=window_capacity,
            max_budget_tokens=max_budget_tokens,
            target_budget_tokens=target_budget_tokens,
            estimator=self.estimator,
            transcript_writer=self.writer,
        )

        #: 缓存的项目级记忆（启动时自动扫描一次）
        self.memory: ProjectMemory = find_project_memory(self.cwd)
        self._last_pruned_count = 0

    def refresh_memory(self) -> ProjectMemory:
        """重新扫描并刷新当前工作区的 LOGOX.md 记忆。"""
        self.memory = find_project_memory(self.cwd)
        return self.memory

    def record_usage(self, estimated_tokens: int, usage: Usage) -> None:
        """在收到大模型返回时，利用真实的 input_tokens 动态校准估算器。"""
        if usage.input_tokens > 0 and estimated_tokens > 0:
            new_factor = self.estimator.calibrate(
                estimated_tokens=estimated_tokens,
                actual_tokens=usage.input_tokens,
            )
            logger.debug("Token 估算动态校准系数更新为: %.3f", new_factor)

    def _assemble_system_prompt(self) -> str:
        """组装顶层系统人设：基础人设 + LOGOX.md 长期记忆 + 技能包渐进索引 + 回合摘要契约。"""
        parts = [self.base_system.rstrip()] if self.base_system.strip() else []
        memory_block = self.memory.render_system_prompt_block()
        if memory_block:
            parts.append(memory_block.strip())

        if self.skill_manager is not None and hasattr(self.skill_manager, "build_prompt_index"):
            skills_block = self.skill_manager.build_prompt_index()
            if skills_block:
                parts.append(skills_block.strip())

        # 注入回合摘要规范约束（末尾强注意力区间）
        parts.append(TURN_SUMMARY_PROMPT_INSTRUCTION.strip())

        if not parts:
            return ""
        return "\n\n".join(parts) + "\n"

    def build(self, history: list[Message]) -> ContextBundle:
        """组装供大模型调用的标准上下文包。"""
        # 1. 组装顶层 System Prompt
        full_system = self._assemble_system_prompt()

        # 2. 估算粗略轮次 (大致估算用户发送的提问数)
        user_turn_count = sum(1 for m in history if m.role == "user")
        current_turn = max(1, user_turn_count)

        # 3. 运行双水位线压缩器 (源头过滤思考链 + 工具换页修剪 + 滑动窗口)
        compaction = self.compactor.compact(
            history,
            system_prompt=full_system,
            current_turn=current_turn,
        )
        self._last_pruned_count = compaction.pruned_count

        # 4. 记忆源绝对路径列表
        memory_paths = [str(s.path).replace("\\", "/") for s in self.memory.sources]

        return ContextBundle(
            system=full_system,
            messages=compaction.messages,
            token_estimate=compaction.tokens_after,
            memory_sources=memory_paths,
            pruned_count=compaction.pruned_count,
        )

    def force_compact(self, history: list[Message]) -> ContextBundle:
        """手动强制执行一次上下文修剪压缩 (对应 /compact 命令)。"""
        full_system = self._assemble_system_prompt()
        user_turn_count = sum(1 for m in history if m.role == "user")
        current_turn = max(1, user_turn_count)

        compaction = self.compactor.compact(
            history,
            force=True,
            system_prompt=full_system,
            current_turn=current_turn,
        )
        self._last_pruned_count = compaction.pruned_count

        memory_paths = [str(s.path).replace("\\", "/") for s in self.memory.sources]

        return ContextBundle(
            system=full_system,
            messages=compaction.messages,
            token_estimate=compaction.tokens_after,
            memory_sources=memory_paths,
            pruned_count=compaction.pruned_count,
        )
