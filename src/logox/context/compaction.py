"""双水位线上下文修剪与无损换页压缩引擎 (Compaction Engine)。

实现机制：
1. 双水位线防颠簸 (High/Low Watermark)：
   - 触发高水位：min(window_capacity * 0.75, 80,000)
   - 目标低水位：min(window_capacity * 0.50, 40,000)
2. 阶段 1：超长工具返回修剪 (Tool Output Pruning)
   - 保留最近 2 轮完整工具返回；
   - 更早的 ToolResult 强制落盘至独立文件，并掏空 Payload 植入指针。
3. 阶段 2：首尾锚点滑动窗口与扁平分段表单调追加 (Flat Segmented Page Table)
   - 锁定 System + LOGOX.md + 首轮任务目标；
   - 锁定最近 K 轮对话尾部窗口；
   - 中间历史平铺追加至 FoldedEpoch 列表，一步直达，杜绝指针套娃。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import List, Optional, Tuple

from logox.context.storage import SessionTranscriptWriter
from logox.context.tokens import TokenEstimator, estimate_message_tokens
from logox.kernel.messages import (
    Message,
    MessageMeta,
    ReasoningBlock,
    TextBlock,
    ToolResultBlock,
    ToolUseBlock,
)

logger = logging.getLogger(__name__)

__all__ = [
    "Compactor",
    "CompactionResult",
    "FoldedEpoch",
]


@dataclass
class FoldedEpoch:
    """单个已归档历史区间的索引记录。"""

    epoch_id: int
    from_turn: int
    to_turn: int
    start_line: int
    end_line: int
    summary: str


@dataclass
class CompactionResult:
    """修剪压缩结果封装。"""

    messages: list[Message]
    tokens_before: int
    tokens_after: int
    pruned_count: int
    epochs: list[FoldedEpoch] = field(default_factory=list)


class Compactor:
    """双水位线上下文压缩执行器。"""

    def __init__(
        self,
        *,
        window_capacity: int = 128_000,
        high_watermark_ratio: float = 0.75,
        low_watermark_ratio: float = 0.50,
        max_budget_tokens: int = 80_000,
        target_budget_tokens: int = 40_000,
        keep_recent_tool_turns: int = 2,
        keep_recent_turns: int = 4,
        estimator: Optional[TokenEstimator] = None,
        transcript_writer: Optional[SessionTranscriptWriter] = None,
    ) -> None:
        self.window_capacity = window_capacity
        self.high_watermark = min(int(window_capacity * high_watermark_ratio), max_budget_tokens)
        self.low_watermark = min(int(window_capacity * low_watermark_ratio), target_budget_tokens)
        self.keep_recent_tool_turns = keep_recent_tool_turns
        self.keep_recent_turns = keep_recent_turns
        self.estimator = estimator or TokenEstimator()
        self.writer = transcript_writer

        self.epochs: list[FoldedEpoch] = []
        self._next_epoch_id = 1

    def should_compact(self, current_tokens: int) -> bool:
        """是否跨过高水位线，需要触发压缩。"""
        return current_tokens >= self.high_watermark

    def compact(
        self,
        messages: list[Message],
        *,
        force: bool = False,
        system_prompt: str = "",
        current_turn: int = 1,
    ) -> CompactionResult:
        """执行压缩修剪流程。

        如果当前预估 Token < high_watermark 且非 force，直接返回原始列表。
        """
        # 发给 API 的消息中天然不含 ReasoningBlock
        clean_messages = self._strip_reasoning(messages)
        tokens_before = self.estimator.estimate_messages(
            clean_messages, system_prompt=system_prompt
        )

        if not force and not self.should_compact(tokens_before):
            return CompactionResult(
                messages=clean_messages,
                tokens_before=tokens_before,
                tokens_after=tokens_before,
                pruned_count=0,
                epochs=list(self.epochs),
            )

        pruned_count = 0
        working_messages = list(clean_messages)

        # ------------------------------------------------------------------ #
        # 阶段 1：超长工具返回修剪 (Tool Output Pruning)
        # ------------------------------------------------------------------ #
        working_messages, tool_pruned = self._prune_tool_results(
            working_messages, current_turn=current_turn
        )
        pruned_count += tool_pruned

        tokens_now = self.estimator.estimate_messages(
            working_messages, system_prompt=system_prompt
        )

        # 如果回落至低水位线以下，阶段 1 即可完成任务（force 模式下继续执行阶段 2 折叠）
        if not force and tokens_now <= self.low_watermark:
            return CompactionResult(
                messages=working_messages,
                tokens_before=tokens_before,
                tokens_after=tokens_now,
                pruned_count=pruned_count,
                epochs=list(self.epochs),
            )

        # ------------------------------------------------------------------ #
        # 阶段 2：首尾锚点滑动窗口与分段表追加 (Anchor + Sliding Window)
        # ------------------------------------------------------------------ #
        working_messages, window_pruned = self._apply_sliding_window(
            working_messages, current_turn=current_turn
        )
        pruned_count += window_pruned

        tokens_after = self.estimator.estimate_messages(
            working_messages, system_prompt=system_prompt
        )

        return CompactionResult(
            messages=working_messages,
            tokens_before=tokens_before,
            tokens_after=tokens_after,
            pruned_count=pruned_count,
            epochs=list(self.epochs),
        )

    def _strip_reasoning(self, messages: list[Message]) -> list[Message]:
        """源头阻断：过滤发往模型 API 的所有思考块。"""
        res = []
        for msg in messages:
            new_blocks = [b for b in msg.blocks if not isinstance(b, ReasoningBlock)]
            if new_blocks:
                res.append(Message(role=msg.role, blocks=new_blocks, meta=msg.meta))
            elif msg.role in ("user", "system"):
                res.append(msg)
        return res

    def _prune_tool_results(
        self, messages: list[Message], *, current_turn: int
    ) -> Tuple[list[Message], int]:
        """修剪距离当前回合超过 keep_recent_tool_turns 的旧工具返回。"""
        pruned = 0
        new_messages = []

        # 估算每个工具消息距离尾部的距离（后出现的工具属于较近轮次）
        tool_indices = [
            i for i, m in enumerate(messages)
            if any(isinstance(b, ToolResultBlock) for b in m.blocks)
        ]
        # 保留最后 keep_recent_tool_turns 个工具交互消息不变
        keep_indices = set(tool_indices[-self.keep_recent_tool_turns :]) if tool_indices else set()

        for idx, msg in enumerate(messages):
            if idx in keep_indices or not any(isinstance(b, ToolResultBlock) for b in msg.blocks):
                new_messages.append(msg)
                continue

            # 修剪较老的 ToolResultBlock
            new_blocks = []
            for block in msg.blocks:
                if isinstance(block, ToolResultBlock) and len(block.content) > 150:
                    # 如果有日志追加器，强制落盘完整输出
                    blob_path = None
                    if self.writer is not None:
                        blob_path = self.writer.save_tool_blob(
                            block.id, block.content, force=True
                        )

                    line_count = block.content.count("\n") + 1
                    byte_size = len(block.content.encode("utf-8", errors="replace"))

                    disk_hint = (
                        f"已完整落盘至: {blob_path}。"
                        if blob_path
                        else "原输出超长已被截断。"
                    )
                    compact_content = (
                        f"[工具输出已压缩换页至磁盘: {disk_hint} (原始大小 {byte_size} 字节, {line_count} 行)。"
                        f"执行状态: {'成功' if block.ok else '失败'}。"
                        f"提示：若后续排查需要精确定位，可用 fs_read 读取上述文件。]"
                    )
                    new_blocks.append(
                        ToolResultBlock(id=block.id, ok=block.ok, content=compact_content)
                    )
                    pruned += 1
                else:
                    new_blocks.append(block)

            new_messages.append(Message(role=msg.role, blocks=new_blocks, meta=msg.meta))

        return new_messages, pruned

    def _apply_sliding_window(
        self, messages: list[Message], *, current_turn: int
    ) -> Tuple[list[Message], int]:
        """首尾双锚点滑动窗口，中间历史平铺折叠追加进分段页表。"""
        # 如果消息总数本身就不多，无需滑动窗口
        if len(messages) <= (1 + self.keep_recent_turns):
            return messages, 0

        # 首部锚点：第一条用户消息（承载初始核心目标）
        head_anchor = [messages[0]]
        # 尾部窗口：最近 K 条消息
        tail_window = messages[-self.keep_recent_turns :]
        # 中间待折叠的消息
        middle_slice = messages[1 : -self.keep_recent_turns]

        if not middle_slice:
            return messages, 0

        # 从中间消息中提取代表性文本用于生成阶段摘要
        turn_summaries = []
        user_queries = []
        assistant_summaries = []
        for m in middle_slice:
            if m.meta and getattr(m.meta, "turn_summary", None):
                turn_summaries.append(m.meta.turn_summary)
            if m.role == "user" and m.text:
                user_queries.append(m.text[:40].replace("\n", " "))
            elif m.role == "assistant" and m.text:
                assistant_summaries.append(m.text[:40].replace("\n", " "))

        if turn_summaries:
            topic_desc = f"核心进展：{'; '.join(turn_summaries[:3])}"
        elif user_queries:
            topic_desc = f"探讨与执行：{', '.join(user_queries[:2])}"
        else:
            topic_desc = "一系列中间调试与工具交互步骤"

        start_l = 1
        end_l = max(1, len(middle_slice) * 2)
        if self.writer:
            start_l = max(1, self.writer.current_line - len(messages) * 2)
            end_l = self.writer.current_line

        epoch = FoldedEpoch(
            epoch_id=self._next_epoch_id,
            from_turn=max(1, current_turn - len(messages) // 2),
            to_turn=max(1, current_turn - 1),
            start_line=start_l,
            end_line=end_l,
            summary=topic_desc,
        )
        self._next_epoch_id += 1
        self.epochs.append(epoch)

        # 渲染扁平分段表单例消息
        table_lines = [
            "[历史会话分段归档索引 (详细原始记录见 transcript.jsonl)]\n"
            "用户初始目标保持完好，中间历史步骤已归档至磁盘，最新上下文保持连贯："
        ]
        for ep in self.epochs:
            table_lines.append(
                f"- [Epoch {ep.epoch_id}: Turn {ep.from_turn}~{ep.to_turn} | "
                f"行 {ep.start_line}~{ep.end_line}] {ep.summary}"
            )
        table_lines.append(
            "\n【按需查阅指引】：若后续分析需要回忆前期交互中的特定细节，"
            "请直接使用 `fs_read` 工具读取 transcript.jsonl 的指定行区间。"
        )

        placeholder = Message(
            role="system",
            blocks=[TextBlock(text="\n".join(table_lines))],
            meta=MessageMeta(source="compaction"),
        )

        compacted_list = head_anchor + [placeholder] + tail_window
        return compacted_list, len(middle_slice)
