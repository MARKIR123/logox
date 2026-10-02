"""双水位线上下文修剪与历史摘要压缩引擎 (Compaction Engine)。

实现机制：
1. 双水位线防颠簸 (High/Low Watermark)：
   - 高水位（CHANGE-005）：``窗口 − reserve``（reserve 默认 32,768，且 ≤ 窗口/4）
   - 低水位：``窗口 × 0.50``（定位是**兜底**：判"只剪工具结果就够了"）
   - 触发判据取**真实用量**与**估算**里较大的那个（见 ``context_tokens_of``）
2. 阶段 1：超长工具返回修剪 (Tool Output Pruning)
   - 保护范围 = **最后一轮全部** ∪ 全局最近 M 条（CHANGE-005 收窄，原为“最近 K 轮”）
   - 更早的 ToolResult 强制落盘至独立文件，并**盖上 `archived` 标记**后植入指针
     —— 标记使修剪**幂等**：同一批历史裁两次，第二次是空操作，
     历史中前部的字节因此保持稳定（前缀缓存不被它打断）
3. 阶段 2：首尾锚点滑动窗口与扁平分段表单调追加 (Flat Segmented Page Table)
   - 锁定 System + LOGOX.md + 首轮任务目标；
   - 锁定最近 K 轮对话尾部窗口；
   - 中间历史平铺追加至 FoldedEpoch 列表，一步直达，杜绝指针套娃。
4. 极小窗口：所有历史轮次只保留摘要，工具原文归档；仍超高水位时本地汇总。
   - 当前轮次与系统提示保持，历史首尾原文锚点取消；失败保留摘要并暂停。
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from logox.context.storage import SessionTranscriptWriter
from logox.context.tokens import TokenEstimator
from logox.kernel.events import Usage
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
    "context_tokens_of",
    "split_turn_spans",
    "INDEX_MARKER",
    "MEMO_MARKER",
    "TRUNCATE_MARKER",
    "is_index_message",
    "format_messages_for_summary",
    "MEMO_SYSTEM_PROMPT",
]


#: 归档索引消息的首行标记（单一事实来源）。
INDEX_MARKER = "[历史归档索引]"
#: 阶段 3 全局工作状态备忘录的首行标记。
MEMO_MARKER = "[历史全局工作状态备忘录]"
#: 旧会话兼容标记：当前压缩不再生成机械硬截断。
TRUNCATE_MARKER = "[安全机械硬截断]"

#: 阶段 3 全局工作状态备忘录系统提示词。
MEMO_SYSTEM_PROMPT = """你是一个专业的代码与会话上下文压缩引擎。
你的任务是将提供的早期历史会话（包含用户的核心目标、长提问、报错信息以及助手的操作摘要）提炼为一份精简的 Markdown 格式【历史全局工作状态备忘录】。

请严格遵循以下结构输出，不要输出任何多余的寒暄或前言后语：
## 1. 核心目标与技术背景
- 阐明用户最初始的核心任务、要解决的问题以及项目整体背景。

## 2. 已完成的关键技术决策与代码修改清单
- 列出历史上已确认的核心架构决定、修改/创建的文件路径、修改的核心函数名与修复的 Bug。

## 3. 当前上下文已知的重要事实与参数
- 记录会话中确立的关键环境变量、配置项、测试结果或重要技术约束。

## 4. 当前未解决的遗留问题与待办清单
- 列出当前尚未完成的任务、待验证项或遗留问题。

字数要求：紧凑精炼，突出重点，避免冗长废话。"""


def is_index_message(message: Message) -> bool:
    """这条消息是不是归档索引、全局状态备忘录或硬截断标记？

    判据 = 有某个文本块以 INDEX_MARKER、MEMO_MARKER 或 TRUNCATE_MARKER 开头。
    """
    for block in message.blocks:
        text = getattr(block, "text", None)
        if isinstance(text, str) and (
            text.startswith(INDEX_MARKER)
            or text.startswith(MEMO_MARKER)
            or text.startswith(TRUNCATE_MARKER)
        ):
            return True
    return False


def format_messages_for_summary(messages: list[Message]) -> str:
    """把需要被压缩的历史消息序列格式化为供 LLM 阅读提炼的纯文本转录。"""
    lines: list[str] = []
    for msg in messages:
        role_label = {"user": "用户", "tool": "工具返回"}.get(msg.role, "助手")
        text = msg.text.strip()
        if any(text.startswith(marker) for marker in (INDEX_MARKER, MEMO_MARKER, TRUNCATE_MARKER)):
            if text:
                lines.append(f"【历史载体】: {text}")
            continue
        summary = getattr(msg.meta, "turn_summary", "") if msg.meta else ""
        # Users remain verbatim; assistant summaries are the contract-defined carrier.
        if summary and msg.role == "assistant":
            lines.append(f"【{role_label}】: {text if MEMO_MARKER in text else summary}")
        elif text:
            lines.append(f"【{role_label}】: {text}")
        for block in msg.blocks:
            if type(block).__name__ == "ToolResultBlock":
                content = getattr(block, "content", "")
                pointer = getattr(block, "blob_path", None)
                if content or pointer:
                    lines.append(f"【工具返回 {block.id}】: {content}" + (f"\n归档指针: {pointer}" if pointer else ""))
    return "\n\n".join(lines)


def context_tokens_of(usage: Usage | None) -> int | None:
    """从厂商上报的用量里取「本次请求喂进去的上下文总量」。

    为什么要单独一个函数：因为**厂商口径不同** ——
    Anthropic 的 ``input_tokens`` 不含缓存读/写，OpenAI 兼容的 ``prompt_tokens`` 含。
    适配器把统一后的口径填在 ``Usage.context_tokens`` 里（D9），这里只负责取。

    缺失 → ``None``（**不猜、不返回 0**）：调用方退回纯估算。
    """
    if usage is None:
        return None
    value = usage.context_tokens
    if value is None or value <= 0:
        return None
    return value


@dataclass
class FoldedEpoch:
    """单个已归档历史区间的**账目**（审计线索：报告 / 压缩现场 dump）。

    ★ D187：这里**只记不可推导的事实**（折了哪些轮、对应哪些行），
    **不记载荷的副本** —— 原先的 ``summary`` 字段已删除。

    为什么删 `summary`（它的来历与三重缺陷）
    ----------------------------------------
    它**曾经是模型可见的载体**：``CHANGE-052`` 之前，归档索引会把 epoch 摘要
    渲染进去，所以压缩必须把摘要存在账本里。改成"逐轮 ``user`` 逐字 +
    ``assistant`` 摘要"之后，摘要**逐条挂在消息上**（且带 ``（已归档 · 行 A~B）``
    前缀）⇒ 账本这份成了副本：

    1. **有损**：拼接用 ``；``，而摘要**自身就含 ``；``**（实测 60 轮 / 115 个分号）
       ⇒ 切不回各轮；
    2. **是子集而非超集**：缺轮号与行号（消息上每条都有）；
    3. **必然漂移且无人能发现**：两份不一致时没有机制判定谁对
       —— 同类问题见 ``CHANGE-052`` 删掉索引里的 epoch 汇总。

    ⚠️ 这也解释了**为什么没有 ``summary_count``**：每条折叠产物的 assistant
    都必带 ``turn_summary``（含兜底与"（本轮无助手回复）"）⇒ 条数恒等于
    ``to_turn - from_turn + 1``，是**可推导**的，存下来又是副本。
    """

    epoch_id: int
    from_turn: int
    to_turn: int
    start_line: int
    end_line: int


@dataclass
class CompactionResult:
    """修剪压缩结果封装。"""

    messages: list[Message]
    tokens_before: int
    tokens_after: int
    pruned_count: int
    epochs: list[FoldedEpoch] = field(default_factory=list)
    #: 本次折叠的**边界**（输入消息列表的下标）：0 = 没折叠，只做了工具修剪。
    #: 调用方（``HierarchicalContextBuilder``）靠它把折叠结果记进缓存并前推游标。
    folded_from_index: int = 0
    # ---- ★ D156：为可观测性补的两个事实字段 ----
    #: 本次折叠掉的**轮数**（0 = 只做了工具修剪）
    folded_turns: int = 0
    #: 折叠时是否用了**确定性兜底摘要**（某几轮缺 `turn_summary`）——
    #: 对应 `CompactionFinished.degraded`（"摘要失败 → 降级")
    degraded: bool = False
    #: 最终策略：none / prune / prune+fold / summary-only / local-memo / archive-blocked。
    strategy: str = "prune"


def split_turn_spans(messages: list[Message]) -> list[tuple[int, int]]:
    """把消息序列切成**轮次区间** ``[start, end)``。

    轮次定义（与内核一致）：一条 ``role == "user"`` 的消息开启一个新轮次，
    直到下一条 ``user`` 为止。

    为什么按这个边界切是**安全的**：一批 ``tool_use`` 与它的 ``tool_result``
    必然落在同一个轮次内（``tool`` 消息前面一定是请求它的 ``assistant``，
    中间不可能插进 ``user``）。所以按轮切 ⇒ **永远不会拆散配对**。

    首条 ``user`` 之前的消息（理论上不存在）归入第一个区间。
    """
    starts = [
        index
        for index, message in enumerate(messages)
        if message.role == "user"
        # 归档索引（D4）也是 `user` 角色，但它**不是真实轮次起点**。
        # 把它算成一轮会产生"幽灵轮次"，把轮次编号与行号全部算错。
        and message.meta.source != "compaction"
    ]
    if not starts:
        return [(0, len(messages))] if messages else []
    spans: list[tuple[int, int]] = []
    if starts[0] > 0:
        spans.append((0, starts[0]))
    for position, start in enumerate(starts):
        end = starts[position + 1] if position + 1 < len(starts) else len(messages)
        spans.append((start, end))
    return spans


def _clip(text: str, limit: int) -> str:
    """压成单行并截断（索引条目里的提问节选用）。"""
    flat = " ".join(text.split())
    return flat if len(flat) <= limit else flat[: limit - 1] + "…"


def _clip_text(text: str, limit: int) -> str:
    """按**字符数**截断供人读的文本（保留换行与缩进，不做单行挤压）。

    与 ``_excerpt`` 的区别：``_excerpt`` 给的是“首尾各一点”（用于判断要不要读原文），
    而这里要的是“**这段代码长什么样**”—— 所以保留原格式，只在尾部说明截断了多少。
    """
    if len(text) <= limit:
        return text
    return (
        f"{text[:limit]}\n"
        f"…（后续 {len(text) - limit} 字已截断，需要时自行 fs_read）"
    )


#: 工具结果短于这个长度就**不值得归档**（归档指针本身也要占几十字符）。
#: ⚠️ 与旧配置字段 `max_tool_result_chars`（8000）**不是一回事**：
#: 那个字段描述的是「单个结果回灌上限」，从未被任何代码读取（D157 已删除）。
PRUNE_MIN_CHARS = 150


def _excerpt(text: str, head: int = 200, tail: int = 80) -> str:
    """**确定性节选**：首部 + 尾部（不是语义摘要）。

    为什么不做语义摘要：那需要额外调一次模型，而压缩在长会话里会反复发生，
    成本会叠上去；而“首部 + 尾部”已经足够让模型判断
    **要不要去 ``fs_read`` 原文** —— 那正是索引的用途。
    """
    flat = " ".join(text.split())
    if len(flat) <= head + tail:
        return flat
    return f"{flat[:head]}…（中略 {len(flat) - head - tail} 字）…{flat[-tail:]}"


def _fallback_summary(turns: list[Message]) -> str:
    """某一轮没有 ``turn_summary`` 时的**确定性兜底**（绝不调模型）。

    什么时候会遇到：

    * 老会话（``turn_summary`` 契约引入之前写的）根本没有该字段；
    * 模型这一轮没按契约输出标签；
    * 轮次被中断 / 失败，内核只写了占位摘要。

    取什么：**优先用户提问** —— 索引的用途是让未来的模型判断
    “要不要去 `fs_read` 原文”，而“那一轮要做什么”在没有“做了什么”的情况下，
    是次优但可用的信息（而且它**来自历史原文，不依赖模型复述**）。
    """
    questions = [m.text.strip() for m in turns if m.role == "user" and m.text.strip()]
    if questions:
        return "；".join(f"提问：{_clip(q, 40)}" for q in questions[:3])
    answers = [m.text.strip() for m in turns if m.role == "assistant" and m.text.strip()]
    if answers:
        return f"回答节选：{_clip(answers[0], 60)}"
    return "（该轮无可读内容）"


class Compactor:
    """双水位线上下文压缩执行器。"""

    def __init__(
        self,
        *,
        window_capacity: int = 128_000,
        # ★ CHANGE-005 裁定 1：阈值改成「窗口 − reserve」。
        #
        # `reserve` 的**物理含义**是“给回答 + 给增量估算误差留的空间”，
        # 不是“给钱包留的额度”。默认 32,768 = 默认 max_tokens(16,384) + 等量余量。
        # 旧写法 `min(窗口×0.75, 80_000)` 会把 1M 窗口的触发点压到 **8%**。
        # 对照：Claude Code 在 1M 窗口下约 **967K** 才触发；pi 用 `窗口 − 16,384`。
        reserve_tokens: int = 32_768,
        # 低水位 = 窗口的 50%，定位是**兜底**：判“只剪工具结果就够了”。
        # 因为折叠很彻底（打回锚点 + 索引 + 最近 K 轮），它一般用不上。
        low_watermark_ratio: float = 0.50,
        # 可选的**成本闸门**（默认 None = 不封顶）：只能压低水位线，不能抬高。
        max_budget_tokens: int | None = None,
        target_budget_tokens: int | None = None,
        keep_recent_tool_results: int = 2,
        keep_recent_turns: int = 2,
        # ★ CHANGE-005 裁定 4：压缩后把“最近碰过的文件”**重新读回来**。
        #
        # 为什么必须主动重读，而不是“让模型自己想起来”：工具结果被卸载后，
        # 模型手上的工作集就只剩一个路径 —— 它很可能**不会**主动去读，
        # 于是接着往下写，写出来的东西和文件现状对不上。
        # Claude Code 的逆向资料把这步叫 **file rehydration**，并明确称其为
        # “*the key insight*”，口径是**重读最近 5 个文件**。
        # 0 = 关闭。
        rehydrate_files: int = 5,
        rehydrate_max_chars: int = 2000,
        estimator: TokenEstimator | None = None,
        #: ★ D158：κ 的分桶键（`f"{provider}/{model}"`）—— 不同分词器的偏差不能互相污染
        model_key: str = "",
        transcript_writer: SessionTranscriptWriter | None = None,
    ) -> None:
        # ★ D159：水位线计算抽成 `_recompute_watermarks()`，这样 `/model` 换窗口时
        #   可以重算，而不必重建整个 Compactor（同一件事只有一处实现）。
        self._reserve_requested = reserve_tokens
        self.low_watermark_ratio = low_watermark_ratio
        self._max_budget_tokens = max_budget_tokens
        self._target_budget_tokens = target_budget_tokens
        self.window_capacity = window_capacity
        self.reserve_tokens = 0
        self.high_watermark = 0
        self.low_watermark = 0
        self._recompute_watermarks()
        self.keep_recent_tool_results = keep_recent_tool_results
        self.keep_recent_turns = keep_recent_turns
        self.rehydrate_files = rehydrate_files
        self.rehydrate_max_chars = rehydrate_max_chars
        #: 当前折叠前缀里的“工作集快照”（路径 → 内容节选）。每次折叠重算。
        self._working_set: list[tuple[str, str]] = []
        self.estimator = estimator or TokenEstimator()
        self.model_key = model_key
        self.writer = transcript_writer

        self.epochs: list[FoldedEpoch] = []
        #: 上一次折叠的轮数与是否降级（D156：报告用；每次折叠前重置）
        self._last_folded_turns: int = 0
        self._last_degraded: bool = False
        self._last_tail_len: int = 0
        self._next_epoch_id = 1

    def reset(self) -> None:
        """清空折叠账本（页表）。

        ⚠️ **必须与压缩缓存一起清。** ``epochs`` 描述的就是"缓存里那段已折叠区间"，
        缓存被丢弃（``/resume`` / ``/rewind`` / ``/new``）而账本留着，页表里就会混进
        **指向已不存在区间**的陈条目 —— 而它不会报错，只会让模型看到一段虚假的历史。
        调用方是 ``HierarchicalContextBuilder._invalidate_cache()``（单一入口）。
        """
        self.epochs = []
        self._next_epoch_id = 1
        # 工作集快照与账本同生共死：它们都描述"缓存里那段已折叠区间"
        self._working_set = []

    def _recompute_watermarks(self) -> None:
        """按当前窗口重算 reserve / 高水位 / 低水位（**唯一实现**，D159）。"""
        # 小窗口保护：reserve 不得超过窗口的四分之一，否则 32k 窗口下它会把窗口吃光
        self.reserve_tokens = min(self._reserve_requested, self.window_capacity // 4)
        high = self.window_capacity - self.reserve_tokens
        low = int(self.window_capacity * self.low_watermark_ratio)
        if self._max_budget_tokens is not None:
            high = min(high, self._max_budget_tokens)
        if self._target_budget_tokens is not None:
            low = min(low, self._target_budget_tokens)
        self.high_watermark = high
        self.low_watermark = low

    def set_window_capacity(self, window_capacity: int) -> None:
        """窗口变化时热更新（`/model` 切到别的模型 —— D159）。

        为什么必须做：换到**窗口更小**的模型后，仍按旧窗口算出的高水位会**高于真实窗口**，
        于是压缩永远不会触发 —— 而请求会直接撞上厂商的 400。
        """
        if window_capacity <= 0:
            return
        self.window_capacity = window_capacity
        self._recompute_watermarks()

    def should_compact(self, current_tokens: int) -> bool:
        """是否跨过高水位线，需要触发压缩。"""
        return current_tokens >= self.high_watermark

    def _execute_stage1_and_2(
        self,
        messages: list[Message],
        *,
        force: bool = False,
        system_prompt: str = "",
        last_usage: Usage | None = None,
        current_tokens: int | None = None,
    ) -> tuple[bool, CompactionResult | None, list[Message], int, int, int, int]:
        """执行触发判定、阶段 1（工具修剪）与阶段 2（首尾滑动窗口折叠）。

        返回：
            (is_done, early_result, working_messages, tokens_before, tokens_after, pruned_count, folded_from)
        若无需压缩或阶段 1 即可满足要求，is_done=True 且 early_result 包含完整返回结果。
        """
        clean_messages = self._strip_reasoning(messages)
        self._last_degraded = False
        self._last_folded_turns = 0
        if current_tokens is not None and current_tokens > 0:
            tokens_before = current_tokens
        else:
            estimated = self.estimator.estimate_messages(
                clean_messages, system_prompt=system_prompt, model_key=self.model_key
            )
            real = context_tokens_of(last_usage)
            tokens_before = max(estimated, real) if real is not None else estimated

        if not force and not self.should_compact(tokens_before):
            return (
                True,
                CompactionResult(
                    messages=clean_messages,
                    tokens_before=tokens_before,
                    tokens_after=tokens_before,
                    pruned_count=0,
                    epochs=list(self.epochs),
                    strategy="none",
                ),
                clean_messages,
                tokens_before,
                tokens_before,
                0,
                0,
            )

        pruned_count = 0
        working_messages = list(clean_messages)

        # ------------------------------------------------------------------ #
        # 阶段 1：超长工具返回修剪 (Tool Output Pruning)
        # ------------------------------------------------------------------ #
        working_messages, tool_pruned = self._prune_tool_results(working_messages)
        pruned_count += tool_pruned

        tokens_now = self.estimator.estimate_messages(
            working_messages, system_prompt=system_prompt, model_key=self.model_key
        )

        if not force and tokens_now <= self.low_watermark:
            return (
                True,
                CompactionResult(
                    messages=working_messages,
                    tokens_before=tokens_before,
                    tokens_after=tokens_now,
                    pruned_count=pruned_count,
                    epochs=list(self.epochs),
                    strategy="prune",
                ),
                working_messages,
                tokens_before,
                tokens_now,
                pruned_count,
                0,
            )

        # ------------------------------------------------------------------ #
        # 阶段 2：逐轮历史折叠与分段表追加 (Sliding Window)
        # ------------------------------------------------------------------ #
        working_messages, window_pruned, folded_from = self._apply_sliding_window(
            working_messages
        )
        pruned_count += window_pruned

        tokens_after = self.estimator.estimate_messages(
            working_messages, system_prompt=system_prompt, model_key=self.model_key
        )

        return (
            False,
            None,
            working_messages,
            tokens_before,
            tokens_after,
            pruned_count,
            folded_from,
        )

    def compact(
        self,
        messages: list[Message],
        *,
        force: bool = False,
        system_prompt: str = "",
        last_usage: Usage | None = None,
        current_tokens: int | None = None,
        memo_text: str | None = None,
        allow_stage3: bool = True,
    ) -> CompactionResult:
        """执行压缩修剪流程（同步版本）。

        若经过阶段 1 和阶段 2 之后 Token 占用仍高于 low_watermark，则启动阶段 3：
        - 若提供了 memo_text，整合为全局工作状态备忘录；
        - 先使用逐轮摘要；同步入口不发模型请求，超预算交由内核暂停。
        """
        is_done, early, working_messages, tokens_before, tokens_after, pruned_count, folded_from = (
            self._execute_stage1_and_2(
                messages,
                force=force,
                system_prompt=system_prompt,
                last_usage=last_usage,
                current_tokens=current_tokens,
            )
        )
        if is_done and early is not None:
            return early

        strategy = "prune+fold" if folded_from > 0 else "prune"
        degraded = self._last_degraded

        if allow_stage3 and tokens_after > self.low_watermark:
            extreme, boundary, archive_ok = self._summary_only_view(self._strip_reasoning(messages))
            if archive_ok and (boundary > 0 or extreme != self._strip_reasoning(messages)):
                pruned_count += sum(isinstance(block, ToolResultBlock) and not block.archived for message in messages for block in message.blocks)
                working_messages, folded_from = extreme, boundary
                self._last_tail_len = len(self._strip_reasoning(messages)) - boundary
                tokens_after = self.estimator.estimate_messages(working_messages, system_prompt=system_prompt, model_key=self.model_key)
                strategy = "summary-only"
                if tokens_after >= self.high_watermark and memo_text and memo_text.strip():
                    candidate, _ = self._apply_memo_compaction(working_messages, memo_text, self._last_tail_len, folded_from)
                    candidate_tokens = self.estimator.estimate_messages(candidate, system_prompt=system_prompt, model_key=self.model_key)
                    if candidate_tokens < tokens_after:
                        working_messages, tokens_after, strategy = candidate, candidate_tokens, "local-memo"
            elif not archive_ok:
                working_messages, folded_from = self._strip_reasoning(messages), 0
                tokens_after = self.estimator.estimate_messages(working_messages, system_prompt=system_prompt, model_key=self.model_key)
                strategy, degraded = "archive-blocked", True

        return CompactionResult(
            messages=working_messages,
            tokens_before=tokens_before,
            tokens_after=tokens_after,
            pruned_count=pruned_count,
            epochs=list(self.epochs),
            folded_from_index=self._source_boundary(messages, folded_from),
            folded_turns=self._last_folded_turns,
            degraded=degraded,
            strategy=strategy,
        )

    async def compact_async(
        self,
        messages: list[Message],
        *,
        force: bool = False,
        system_prompt: str = "",
        last_usage: Usage | None = None,
        current_tokens: int | None = None,
        memo_summarizer: Any | None = None,
        allow_stage3: bool = True,
    ) -> CompactionResult:
        """执行压缩修剪流程（异步版本，支持模型调用生成阶段 3 备忘录）。"""
        is_done, early, working_messages, tokens_before, tokens_after, pruned_count, folded_from = (
            self._execute_stage1_and_2(
                messages,
                force=force,
                system_prompt=system_prompt,
                last_usage=last_usage,
                current_tokens=current_tokens,
            )
        )
        if is_done and early is not None:
            return early

        strategy = "prune+fold" if folded_from > 0 else "prune"
        degraded = self._last_degraded

        if allow_stage3 and tokens_after > self.low_watermark:
            extreme, boundary, archive_ok = self._summary_only_view(self._strip_reasoning(messages))
            if archive_ok and (boundary > 0 or extreme != self._strip_reasoning(messages)):
                pruned_count += sum(isinstance(block, ToolResultBlock) and not block.archived for message in messages for block in message.blocks)
                working_messages = extreme
                folded_from = boundary
                self._last_tail_len = len(self._strip_reasoning(messages)) - boundary
                tokens_after = self.estimator.estimate_messages(
                    working_messages, system_prompt=system_prompt, model_key=self.model_key)
                strategy = "summary-only"
                self._last_folded_turns = len(split_turn_spans(messages)) - 1
                if tokens_after >= self.high_watermark and callable(memo_summarizer):
                    tail_len = self._last_tail_len
                    historical = working_messages[:-tail_len] if tail_len else working_messages
                    tail = working_messages[-tail_len:] if tail_len else []
                    mandatory_tokens = self.estimator.estimate_messages(tail, system_prompt=system_prompt, model_key=self.model_key)
                    target_tokens = max(1, self.high_watermark - mandatory_tokens - 128)
                    # No model can solve a current turn that already consumes the whole budget.
                    if mandatory_tokens < self.high_watermark:
                        try:
                            res = memo_summarizer(historical, target_tokens=target_tokens)
                            memo_text = await res if asyncio.iscoroutine(res) else res
                        except Exception as exc:
                            logger.warning("本地全量历史压缩失败，保留逐轮摘要：%s", exc)
                            memo_text = None
                        if isinstance(memo_text, str) and memo_text.strip():
                            candidate, _ = self._apply_memo_compaction(working_messages, memo_text, tail_len, folded_from)
                            candidate_tokens = self.estimator.estimate_messages(candidate, system_prompt=system_prompt, model_key=self.model_key)
                            if candidate_tokens < tokens_after:
                                working_messages, tokens_after = candidate, candidate_tokens
                                strategy = "local-memo"
                        if strategy != "local-memo":
                            degraded = True
            elif not archive_ok:
                # Archive failure must not let stage 2 silently discard tool payloads.
                working_messages = self._strip_reasoning(messages)
                folded_from = 0
                tokens_after = self.estimator.estimate_messages(working_messages, system_prompt=system_prompt, model_key=self.model_key)
                strategy = "archive-blocked"
                degraded = True

        return CompactionResult(
            messages=working_messages,
            tokens_before=tokens_before,
            tokens_after=tokens_after,
            pruned_count=pruned_count,
            epochs=list(self.epochs),
            folded_from_index=self._source_boundary(messages, folded_from),
            folded_turns=self._last_folded_turns,
            degraded=degraded,
            strategy=strategy,
        )

    @staticmethod
    def _source_boundary(messages: list[Message], clean_boundary: int) -> int:
        """Translate a filtered view boundary back to the original history cursor."""
        if clean_boundary <= 0:
            return 0
        kept = [i for i, message in enumerate(messages)
                if message.role in ("user", "system") or any(not isinstance(block, ReasoningBlock) for block in message.blocks)]
        return kept[clean_boundary] if clean_boundary < len(kept) else len(messages)

    def _strip_reasoning(self, messages: list[Message]) -> list[Message]:
        """源头阻断：过滤发往模型 API 的所有思考块。"""
        res = []
        for msg in messages:
            new_blocks = [b for b in msg.blocks if not isinstance(b, ReasoningBlock)]
            if new_blocks:
                res.append(msg.model_copy(update={"blocks": new_blocks}))
            elif msg.role in ("user", "system"):
                res.append(msg)
        return res

    def _protected_tool_indices(self, messages: list[Message]) -> set[int]:
        """哪些工具结果必须**完整保留**：全局最近的 ``keep_recent_tool_results`` 条。

        ⚠️ 这里**刻意不按轮次保护**（CHANGE-005 实测修正了原方案）。
        原方案是“**最后一轮全部** ∪ 最近 M 条”，但实测发现它让**回合内复检无事可做** ——
        因为“最后一轮”就是**正在进行的那一轮**，而它的工具结果恰恰是把窗口
        撑破的那一批，却全部免剪：

        ```
        窗口 4000、高水位 3000，一个回合里连做 4 次工具调用
        视图轨迹：4308 → 5455 → 6602 → 7749 → 8896   ← 一路涨到 3 倍窗口，必 400
        ```

        改成“只看最近 M 条”后（同一个用例）：

        ```
        视图轨迹：4308 → 5455 → 5636 → 5817 → 5998   ← 每步只涨 181
        ```

        这就是 Claude Code 说的 **hot tail**："a small, **recent** window of
        tool results that remain fully visible"，其余进 cold storage。

        两道安全线使这个规则不会粗暴地抢走模型手上的东西：

        1. **只有超过高水位时才会卸载** —— 正常情况下一个工具结果都不会被动；
        2. 卸载后的位置仍然给出**索引指针**（落盘路径 + 行数 + 确定性节选），
           模型需要精确内容时用一次 `fs_read` 就能取回。
        """
        if self.keep_recent_tool_results <= 0:
            return set()
        tool_indices = [
            index
            for index, message in enumerate(messages)
            if any(isinstance(block, ToolResultBlock) for block in message.blocks)
        ]
        return set(tool_indices[-self.keep_recent_tool_results :])

    def _prune_tool_results(self, messages: list[Message]) -> tuple[list[Message], int]:
        """把保留窗口之外的旧工具结果换成**摘要 + 索引**。

        “摘要”是**确定性节选**（首部 + 尾部），**不是语义摘要** ——
        语义摘要要额外调模型，而“这段输出大致在讲什么”已经足够让模型判断
        **要不要去 ``fs_read`` 原文**，那正是索引的用途。
        “索引” = 落盘路径 + 原始行数/字节数。
        """
        protected = self._protected_tool_indices(messages)
        pruned = 0
        new_messages: list[Message] = []

        for index, message in enumerate(messages):
            if index in protected:
                new_messages.append(message)
                continue

            new_blocks: list[Any] = []
            for block in message.blocks:
                if (
                    not isinstance(block, ToolResultBlock)
                    or block.archived  # ★ 已归档：跳过（这就是“幂等”的落点）
                    or len(block.content) <= PRUNE_MIN_CHARS
                ):
                    new_blocks.append(block)
                    continue

                # ★ F-17：持久化订阅者在工具结束时就写过一次了，这里只要“确认它在”。
                #   无条件重写会让同一份输出在**每个折叠回合**被覆盖一次，而产出零差异。
                blob_path = None
                if self.writer is not None:
                    blob_path = self.writer.blob_path_of(block.id)
                    if blob_path is None:
                        blob_path = self.writer.save_tool_blob(
                            block.id, block.content, force=True
                        )
                    if blob_path is None:
                        # 写盘失败时保留原文，不能把不存在的归档当成可恢复数据。
                        new_blocks.append(block)
                        continue

                line_count = len(block.content.splitlines()) or 1
                byte_size = len(block.content.encode("utf-8", errors="replace"))
                status = "成功" if block.ok else "失败"
                location = blob_path or "（未能落盘）"
                new_blocks.append(
                    ToolResultBlock(
                        id=block.id,
                        ok=block.ok,
                        archived=True,  # ★ 盖章：下次不会再动它
                        content=(
                            f"[工具输出已归档 · {location} · 共 {line_count} 行 / {byte_size} 字节"
                            f" · 行 1~{line_count} · 执行状态：{status}]"
                            f" 内容节选（确定性节选，非语义摘要）：{_excerpt(block.content)}"
                            f" 需要精确内容时用 fs_read 读取上述文件。"
                        ),
                    )
                )
                pruned += 1

            new_messages.append(Message(role=message.role, blocks=new_blocks, meta=message.meta))

        return new_messages, pruned

    def _apply_sliding_window(
        self, messages: list[Message]
    ) -> tuple[list[Message], int, int]:
        """**逐轮折叠**中间历史：每轮压成 ``[user 全文, assistant 摘要]``（CHANGE-052）。

        返回 ``(新消息列表, 被修剪条数, 折叠边界)``。折叠边界是输入列表的下标，
        ``0`` 表示**没有折叠**（调用方据此决定要不要更新缓存）。

        折叠后的结构：

        ``[user 全文, 摘要] × N · [归档索引 user] · [最近 K 轮]``

        ★ 相对 D4（``[锚点] · [锚点摘要] · [索引] · [尾窗]``）的三处变化：

        1. **锚点概念删除** —— 第 1 轮与其余折叠轮**处理逻辑完全相同**（用户裁定原话：
           "就没有第一轮锚点这个概念了，因为第一轮的处理逻辑和后面被压缩的轮次一模一样"）；
        2. **按轮成对** —— 不再是"所有摘要挤在一条索引消息里"，也不再受
           `summaries[:3]` 的数量上限（实测它丢掉了 21 条摘要里的 18 条）；
        3. **工具块整段丢弃** —— 用户裁定"其中的工具调用去掉，这部分信息已经压缩在摘要中"。
        4. ★ **D187：账本不再记载荷副本** —— 那一版的索引里还有"epoch 摘要汇总"，
           于是每条摘要同时存在两处（索引里一份、各轮消息上一份）；现已删净，
           摘要**只在各轮消息上**（`FoldedEpoch.summary` 字段一并删除）。

        ⚠️ **两条必须守住的不变量**：

        * **配对**：``tool_use`` 与 ``tool_result`` 必须**成对消失**。本函数只重发
          用户文本与摘要，两者的工具块**都不进产物** ⇒ 剩余消息里都是 0，真空成立。
          边界落在**轮次起点**（`spans[-K][0]`）⇒ 永不把一对切到两侧（否则厂商 400）。
        * **前缀不进 meta**：``（已归档 · 行 A~B）`` 只进**渲染文本**，
          ``meta.turn_summary`` 保持模型原话（见 `_folded_pair` 的说明）。
        """
        spans = split_turn_spans(messages)
        # ★ 跳过**已经折叠过**的轮次。
        #
        # 为什么必须跳：折叠后的视图里，每个折叠轮是 ``[user 全文, assistant 摘要]``，
        # 而那条 user 的 `meta.source` 仍是 `session`（**原样复用**的原文）⇒
        # `split_turn_spans` 会把它当成一个**新的轮次起点**。
        # 于是第二次 `build()`（视图 = 缓存前缀 + 新历史）会把**已经折好的轮再折一次**：
        # 摘要被摘要、`epochs` 每次 build 都追加一条（实测 T-2 报 `2 != 1`）。
        #
        # 判据：该轮里只要有一条 assistant 是**折叠产物**（`source="compaction"`），
        # 这一轮就已经折过了。这与 `split_turn_spans` 排除索引消息是同一个思路：
        # **靠来源标记识别，不靠位置或条数猜测**。
        already = 0
        while already < len(spans) and self._is_folded_turn(messages, spans[already]):
            already += 1
        remaining = spans[already:]
        # 至少要留得下"最近 K 轮"，否则没什么可折的
        if len(remaining) <= self.keep_recent_turns:
            return messages, 0, 0

        tail_start = remaining[-self.keep_recent_turns][0]
        tail_window = messages[tail_start:]
        # ★ 折叠**全部**前面的轮（含第 1 轮）—— 没有锚点特例
        fold_spans = remaining[: -self.keep_recent_turns]
        if not fold_spans:
            return messages, 0, 0

        folded: list[Message] = []
        degraded = False
        for turn_index, (start, end) in enumerate(fold_spans, 1):
            pair, used_fallback = self._folded_pair(
                messages[start:end], turn_index=turn_index
            )
            folded.extend(pair)
            degraded = degraded or used_fallback

        # 账本（审计线索：报告 / 压缩现场 dump 用；**不再渲染进索引**）
        # ★ D187：**只记区间，不记摘要正文** —— 正文的唯一载体是折叠产出的
        #   assistant 消息（模型看得到的那一份，带行号前缀）。
        range_start = self.epochs[-1].to_turn + 1 if self.epochs else 1
        self._record_epoch(
            messages[fold_spans[0][0] : fold_spans[-1][1]],
            from_turn=range_start,
            to_turn=range_start + len(fold_spans) - 1,
        )
        self._last_folded_turns = len(fold_spans)
        self._last_degraded = degraded
        self._last_tail_len = len(tail_window)
        # ★ 重读工作集（裁定 4）：必须在 `_render_index()` 之前，索引里要带上它
        self._working_set = self._rehydrate_working_set(
            messages[fold_spans[0][0] : fold_spans[-1][1]]
        )
        # ★ 保留**此前已折叠的前缀**（含之前那些成对的 `[user, 摘要]`），
        #   但**去掉旧索引** —— 它马上会被新索引取代。
        #
        # ⚠️ 这一步漏掉的症状（实测，被 T-1「缓存可丢弃性」用例抓住）：
        #   第二次 build 时 `fold_spans` 里只有**新折**的轮，早先折好的对不在其中 ⇒
        #   视图从 `[初始目标, 摘要, …3 轮…, 索引, 尾部]`（11 条）塌成
        #   `[第1问, 摘要, 索引, 尾部]`（7 条）—— **最初的目标与几轮历史凭空消失**，
        #   而增量路径与"丢缓存重算"的结果从此不再一致。
        #   旧实现靠 `_head_anchor()` 顺带保住这段（它返回 `messages[:first_turn_end]`）；
        #   锚点删除后，这份责任必须显式接住。
        head_prefix = [
            m for m in messages[: remaining[0][0]] if not m.text.startswith(INDEX_MARKER)
        ]
        compacted_list = [
            *head_prefix,
            *folded,
            self._render_index(),
            *tail_window,
        ]
        return compacted_list, len(messages) - len(tail_window), tail_start

    def _summary_only_view(self, messages: list[Message]) -> tuple[list[Message], int, bool]:
        """Archive historical tools before replacing every old turn with its summary."""
        spans = split_turn_spans(messages)
        boundary = spans[-1][0] if spans else 0
        archived_messages: list[Message] = []
        for message in messages:
            blocks = []
            for block in message.blocks:
                if not isinstance(block, ToolResultBlock) or block.archived:
                    blocks.append(block)
                    continue
                if self.writer is None:
                    return messages, 0, False
                pointer = self.writer.blob_path_of(block.id) or self.writer.save_tool_blob(block.id, block.content, force=True)
                if pointer is None:
                    return messages, 0, False
                blocks.append(block.model_copy(update={"archived": True, "content": f"[工具原文归档: {pointer}]"}))
            archived_messages.append(message.model_copy(update={"blocks": blocks}))
        messages = archived_messages
        if boundary == 0:
            return messages, 0, True
        # Already compacted carriers are durable state. Do not rewrite their index
        # merely because audit/state records have added lines to the transcript.
        if all(message.meta.source == "compaction" for message in messages[:boundary]):
            return messages, boundary, True
        summaries: list[Message] = []
        for start, end in spans[:-1]:
            turn = messages[start:end]
            # Existing summary-only/memo prefixes are already valid carriers: reuse verbatim.
            if all(message.meta.source == "compaction" for message in turn):
                summaries.extend(message for message in turn if message.role == "assistant" and not message.text.startswith(INDEX_MARKER))
                continue
            carriers: list[str] = []
            pointers: list[str] = []
            for message in turn:
                text = message.text.strip()
                if text.startswith(MEMO_MARKER):
                    carriers.append(text)
                elif message.role == "assistant" and message.meta.turn_summary:
                    carriers.append(message.meta.turn_summary)
                for block in message.blocks:
                    if not isinstance(block, ToolResultBlock):
                        continue
                    pointers.append(block.content)
            if not carriers:
                # Existing compacted prefixes may contain multiple historical summaries.
                carriers = [m.text.strip() for m in turn if m.meta.source == "compaction" and m.role == "assistant" and m.text.strip()]
            if not carriers:
                carriers = [_fallback_summary(turn)]
            body = f"【历史第 {len(summaries) + 1} 轮摘要】" + "\n".join(carriers)
            if pointers:
                body += "\n工具原文归档：\n" + "\n".join(pointers)
            summaries.append(Message(role="assistant", blocks=[TextBlock(text=body)],
                meta=MessageMeta(source="compaction", turn_summary=body)))
        pointer = (f"原始对话：{self.writer.log_file} · " + (f"行 1~{self.writer.current_line}" if self.writer.current_line else "行号未知")) if self.writer is not None else "原始对话仍保留在会话历史中"
        index = Message(role="user", blocks=[TextBlock(text=f"{INDEX_MARKER} 历史只保留逐轮摘要；{pointer}")], meta=MessageMeta(source="compaction"))
        return [*summaries, index, *messages[boundary:]], boundary, True

    def _apply_memo_compaction(
        self, messages: list[Message], memo_text: str, tail_len: int, folded_from: int
    ) -> tuple[list[Message], int]:
        """One local memo replaces all historical carriers; no original-question anchor."""
        if tail_len <= 0 or len(messages) <= tail_len:
            return messages, folded_from
        memo_msg = Message(role="assistant", blocks=[TextBlock(text=f"{MEMO_MARKER}\n\n{memo_text.strip()}\n")],
            meta=MessageMeta(source="compaction", turn_summary=memo_text.strip()))
        return [memo_msg, *messages[-tail_len:]], folded_from

    @staticmethod
    def _is_index_message(message: Message) -> bool:
        """这条消息是不是**归档索引**？（委托给 `is_index_message`，单一判据）

        `INDEX_MARKER` 是单一事实来源 —— `_render_index()` 用它渲染、
        `HierarchicalContextBuilder._folded_head()` 用它认出头部末尾、
        这里用它把**旧索引**从保留的前缀里剔掉。
        """
        return is_index_message(message)

    @staticmethod
    def _is_folded_turn(messages: list[Message], span: tuple[int, int]) -> bool:
        """这一轮是不是**已经折叠过**的（视图里是 ``[user 全文, assistant 摘要]``）？

        判据：轮内存在 **`source="compaction"` 的 assistant** —— 那是 `_folded_pair`
        产出的摘要消息。**靠来源标记识别**，与 `split_turn_spans` 排除索引消息同源。

        ⚠️ 不能用"轮内只有 2 条消息"判断：退化轮（本来就是 `[user, assistant]`）
        会被误判成"已折叠"从而**永远不再折**。
        """
        start, end = span
        return any(
            message.role == "assistant" and message.meta.source == "compaction"
            for message in messages[start:end]
        )

    def _folded_pair(
        self, turn_messages: list[Message], *, turn_index: int
    ) -> tuple[list[Message], bool]:
        """把**一轮**压成 ``[user 全文, assistant 摘要]``。

        返回 ``(两条消息, 是否用了兜底摘要)``。

        取值规则：
        * **提问**：该轮首条 user 消息**原样复用**（逐字，绝不改写）；
        * **摘要**：该轮 assistant 消息的 ``meta.turn_summary``（含**级联** ——
          上一轮折叠产出的摘要消息本身带 ``turn_summary``，于是"再折叠"不会丢东西）；
        * 没有摘要 ⇒ `_fallback_summary`（优先取用户提问，纯本地、不调模型）。

        ★ 退化轮（**只有 user、没有 assistant 的轮** —— 实测存在：大段粘贴的孤立片段、
        重复提交的消息）：**仍产出成对**（assistant 位置放兜底摘要）。用户裁定"甲"。
        代价是"提问"可能与上一行的 user 重复 —— **值得**，因为它保证了角色交替
        （相邻同角色要靠 adapter 归一化，能少一处就少一处）。
        """
        questions = [
            m for m in turn_messages if m.role == "user" and m.meta.source != "compaction"
        ]
        paraphrases = [m.meta.turn_summary for m in turn_messages if m.meta.turn_summary]
        has_assistant = any(
            m.role == "assistant" and m.meta.source != "compaction" for m in turn_messages
        )
        if paraphrases:
            summary, used_fallback = paraphrases[0], False
        elif has_assistant:
            # 有助手回复但没摘要 ⇒ 走确定性兜底（优先取用户提问）
            summary, used_fallback = _fallback_summary(turn_messages), True
        else:
            # ★ **退化轮**（只有 user、没有 assistant —— 实测存在：大段粘贴的孤立片段、
            #   重复提交的消息，以及被中断在第一句的轮次）。
            #
            #   这里**不能用 `_fallback_summary`**：它的首选就是"提问：<用户原话>"，
            #   而该轮的 user 消息**已经逐字保留在上一行** ⇒ 结果是**同一句话出现两次**，
            #   一分钱没压、还多花一份 token（实测：压缩后比压缩前**更长**）。
            #   所以退化轮给一句**不重复**的说明。
            summary, used_fallback = "（本轮无助手回复）", True

        question = questions[0] if questions else None
        if question is None:
            # 理论上不会发生（轮次由 user 消息界定）；真发生时也不能造出孤儿摘要
            return [], False

        # 行号：该轮消息行号的 min/max（免疫 F-32 —— 不再依赖"轮次号→行号"的推断）
        numbers = [
            m.meta.transcript_line
            for m in turn_messages
            if m.meta.transcript_line is not None
        ]
        # 视图里已有的折叠轮：沿用它记录的区间（原文已不在视图里，必须跨折叠存活）
        archived = [m.meta for m in turn_messages if m.meta.archived_from_line is not None]
        prefix = ""
        from_line = to_line = None
        if archived:
            from_line = min(m.archived_from_line for m in archived if m.archived_from_line)
            to_line = max(m.archived_to_line for m in archived if m.archived_to_line)
        elif numbers:
            from_line, to_line = min(numbers), max(numbers)

        if from_line is not None and to_line is not None:
            prefix = f"（已归档 · 行 {from_line}~{to_line}）"
        else:
            prefix = "（已归档）"

        return [
            question,
            Message(
                role="assistant",
                blocks=[TextBlock(text=f"{prefix}{summary}")],
                meta=MessageMeta(
                    source="compaction",
                    # ⚠️ 前缀**不写进 meta**（见模块 docstring 与 CHANGE-052 §5-I2）
                    turn_summary=summary,
                    summary_source=getattr(question.meta, "summary_source", None)
                    or ("deterministic" if used_fallback else None),
                    archived_from_line=from_line,
                    archived_to_line=to_line,
                ),
            ),
        ], used_fallback

    def _record_epoch(
        self,
        folded_slice: list[Message],
        *,
        from_turn: int,
        to_turn: int,
    ) -> FoldedEpoch:
        """把本次折叠记进账本（**审计线索**：报告 / 压缩现场 dump）。

        ★ CHANGE-052 的变化：**行号改由 `MessageMeta.transcript_line` 算**
        （不再问 `writer.turn_lines_of()`）—— 后者依赖"轮次号→行号"的正则映射，
        而压缩器的轮次号是**视图相对**的、和 transcript 的 `session` 级编号
        **不同源** ⇒ 查出来的区间指向**别的轮次**（实测区间查询 100% 返回 None）。
        `transcript_line` 由 replay 从记录的 `"line"` 字段带来，**免疫该问题**。

        ★ D187：**不再接收 `summaries`** —— 账本不记载荷的副本（见 `FoldedEpoch`）。
        """
        numbers = [
            m.meta.transcript_line
            for m in folded_slice
            if m.meta.transcript_line is not None
        ]
        archived_from = [
            m.meta.archived_from_line
            for m in folded_slice
            if m.meta.archived_from_line is not None
        ]
        archived_to = [
            m.meta.archived_to_line
            for m in folded_slice
            if m.meta.archived_to_line is not None
        ]
        if archived_from and archived_to:
            # 视图里已有折叠轮：沿用它自己记录的区间（跨折叠存活）
            start_line, end_line = min(archived_from), max(archived_to)
        elif numbers:
            start_line, end_line = min(numbers), max(numbers)
        else:
            start_line = end_line = 0

        epoch = FoldedEpoch(
            epoch_id=self._next_epoch_id,
            from_turn=from_turn,
            to_turn=to_turn,
            start_line=start_line,
            end_line=end_line,
        )
        self._next_epoch_id += 1
        self.epochs.append(epoch)
        return epoch

    def _render_index(self) -> Message:
        """渲染**归档索引**（页脚 = 折叠区与完整区的边界标记）。

        为什么是 `user` 而不是 `system`：中立模型声明了 `system` 角色，而 Anthropic 的
        `messages` 不接受它 —— 用 `user` 就绕开了整个问题；相邻同角色由适配器归一化
        （D7）。**同时这也让顶层 system prompt 保持逐字稳定**，前缀缓存不会被打碎。

        ★ CHANGE-052 重写。它现在只剩四件事（旧的"epoch 摘要汇总"**已删除**）：

        1. **边界标记** + 一句"以上是摘要不是原文"的元说明 —— 索引是最显眼的位置，
           而模型必须知道那一段 assistant 是**我们替换过的**，不是它真的说过的话；
        2. **会话目录的绝对路径**（一次）—— ⚠️ 这是修一个**从未可用过**的承诺：
           旧版只写 `transcript.jsonl` 这个**文件名**，而模型的 cwd 是仓库目录、
           会话文件在 `~/.logox/sessions/...`。实测**模型看到的全部内容里
           没有任何会话目录路径** ⇒ 即使给出行号它也无从 `fs_read`；
        3. **工作集快照**；
        4. 下方每轮的"（已归档 · 行 A~B）"所需的**公共前缀说明**。

        ⚠️ epoch 摘要**不得**再出现在这里 —— 摘要已经按轮保留在各自的 assistant
        消息上，两处都有就是**双事实来源**（本项目已登记过 F-03 类反模式）。
        """
        lines = [
            f"{INDEX_MARKER}（折叠区到此结束，下面是完整的最近轮次）",
            "以上 assistant 消息已替换为**摘要**（原文不在上下文里），不是逐字原文。",
        ]
        session_dir = getattr(self.writer, "session_dir", None) if self.writer else None
        if session_dir is not None:
            lines.append(f"完整日志目录：{session_dir}")
            lines.append("  · 对话原文：transcript.jsonl（上方“行 A~B”即该文件的行号）")
            blob_dir = getattr(self.writer, "blob_dir", session_dir / "tools")
            lines.append(f"  · 工具输出目录：{blob_dir}（文件 tool_<call_id>.log，路径分隔符替换为下划线，按需 fs_read 读取）")
        else:
            lines.append("（未接入 transcript 写入器：无法给出日志路径）")
        lines.extend(self._render_working_set())
        return Message(
            role="user",
            blocks=[TextBlock(text="\n".join(lines))],
            meta=MessageMeta(source="compaction"),
        )

    def _render_working_set(self) -> list[str]:
        """把重读回来的工作集渲染成索引里的一个附加段（裁定 4）。"""
        if not self._working_set:
            return []
        out = [
            "",
            "[工作集快照] 折叠前你正在碰的文件 —— 已替你重新读回，"
            "免去逐个个 fs_read。内容可能被截断，需要完整或精确内容时再自己读。",
        ]
        for path, content in self._working_set:
            out.append(f"· {path}")
            out.append(content)
        return out

    def _rehydrate_working_set(
        self, middle_slice: list[Message]
    ) -> list[tuple[str, str]]:
        """从被折叠的区间里找出**最近碰过的文件**并重读（裁定 4）。

        取哪些：工具调用参数里出现过的路径，按“**最后一次出现**的时候”排序 ——
        越近的越靠前，取前 ``rehydrate_files`` 个，且**在磁盘上真实存在**的
        （已删掉的文件直接跳过，不留悬空引用）。

        怎么取路径：只看参数里的 ``path`` / ``file_path`` / ``filePath`` 键，
        **不猜**整个字典（否则一个碰巧像路径的字符串就会把无关文件拉进来）。
        这样做与工具名无关：以后新增任何“对某个文件干活”的工具都自动适用。

        **绝不抛异常**：压缩本身不该因为一次文件 IO 失败而中断 ——
        读不到就当这个文件不存在。
        """
        if self.rehydrate_files <= 0:
            return []
        last_seen: dict[str, int] = {}
        for order, message in enumerate(middle_slice):
            for block in message.blocks:
                if not isinstance(block, ToolUseBlock):
                    continue
                for key in ("path", "file_path", "filePath"):
                    raw = block.input.get(key)
                    if isinstance(raw, str) and raw:
                        last_seen[raw] = order
        if not last_seen:
            return []

        out: list[tuple[str, str]] = []
        for raw, _order in sorted(last_seen.items(), key=lambda kv: kv[1], reverse=True):
            if len(out) >= self.rehydrate_files:
                break
            try:
                path = Path(raw)
                if not path.is_file():
                    continue
                text = path.read_text(encoding="utf-8", errors="replace")
            except OSError:  # pragma: no cover - 磁盘/权限/编码异常一律当成“读不到”
                continue
            out.append((raw.replace("\\", "/"), _clip_text(text, self.rehydrate_max_chars)))
        return out

