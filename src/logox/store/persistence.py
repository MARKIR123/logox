"""会话实时持久化总线订阅者 (D98)。

用户输入和工具结果随事件追加 JSONL；模型正文/推理增量先存内存，
在 ModelRequestFinished 时合并写出。内核取消/异常终态会交付已有可见内容，
进程被强制结束或断电时，当前尚未完成的请求增量仍可能丢失；不承诺断电持久性。
"""

from __future__ import annotations

import logging
from typing import Any

from logox.context.storage import SessionTranscriptWriter
from logox.kernel import events as ev

logger = logging.getLogger(__name__)

__all__ = ["SessionPersistenceSubscriber"]


class SessionPersistenceSubscriber:
    """会话总线持久化订阅者。"""

    def __init__(self, writer: SessionTranscriptWriter) -> None:
        self.writer = writer
        self._current_text: list[str] = []
        self._current_reasoning: list[str] = []
        self._tool_calls: dict[str, dict[str, Any]] = {}
        self._step_counter = 0

    async def handle(self, event: ev.AnyEvent) -> None:
        """事件总线订阅入口。"""
        self.apply(event)

    def apply(self, event: ev.AnyEvent) -> None:
        """同步处理事件（供测试与离线回放）。"""
        if isinstance(event, ev.UserPromptSubmit):
            self._current_text.clear()
            self._current_reasoning.clear()
            self._step_counter = 0
            self.writer.write_step(
                turn=event.turn,
                step=self._step_counter,
                role="user",
                event_type="user_prompt",
                content=event.text,
                timestamp=event.ts,
            )
        elif isinstance(event, ev.ModelDelta):
            if event.kind == "text":
                self._current_text.append(event.delta)
            elif event.kind == "reasoning":
                self._current_reasoning.append(event.delta)
        elif isinstance(event, ev.ModelRequestFinished):
            self._step_counter += 1
            full_text = "".join(self._current_text)
            full_reasoning = "".join(self._current_reasoning)
            meta: dict[str, Any] = {
                "input_tokens": event.usage.input_tokens,
                "output_tokens": event.usage.output_tokens,
                "duration_ms": event.duration_ms,
                "stop_reason": event.stop_reason,
                "raw_stop_reason": event.raw_stop_reason,
            }
            extra: dict[str, Any] = {}
            if full_reasoning:
                extra["reasoning"] = full_reasoning
            if event.tool_calls:
                extra["tool_calls"] = event.tool_calls

            self.writer.write_step(
                turn=event.turn,
                step=self._step_counter,
                role="assistant",
                event_type="model_output",
                timestamp=event.ts,
                content=full_text,
                meta=meta,
                **extra,
            )
            self._current_text.clear()
            self._current_reasoning.clear()
        elif isinstance(event, ev.ToolCallRequested):
            self._tool_calls[event.call_id] = {
                "name": event.name,
                "args": event.args,
            }
        elif isinstance(event, ev.ToolCallFinished):
            self._step_counter += 1
            call_info = self._tool_calls.pop(event.call_id, {})
            tool_name = call_info.get("name", "tool")
            content_str = event.content if event.content else (event.result_digest or str(event.error_kind or "ok"))
            blob_path = self.writer.save_tool_blob(event.call_id, content_str, force=True)

            self.writer.write_step(
                turn=event.turn,
                step=self._step_counter,
                role="tool",
                event_type="tool_result",
                timestamp=event.ts,
                content=content_str,
                tool_name=tool_name,
                call_id=event.call_id,
                blob_file=blob_path,
                is_error=not event.ok,
                meta={
                    "duration_ms": event.duration_ms,
                    "args": call_info.get("args"),
                    # ★ D139：展示提示也要落盘 —— 否则 `/resume` 回放出来的 `edit` 卡片
                    #   又只剩一行统计（D138 的教训：**两条到达路径都要带上**）。
                    #   `model_dump()` 之后是纯 dict，回放侧不需要认识工具层/界面层的任何类型。
                    "display": event.display.model_dump() if event.display else None,
                },
            )
        elif isinstance(event, ev.CompactionFinished):
            # ★ D156 / F-54：压缩**落盘**。在这之前压缩过程完全不进 transcript，
            #   于是"跑没跑、压掉多少、给模型的行号对不对"事后无从查证
            #   （F-53 那个 bug 正是因为没有现场、只能靠人造实验才发现）。
            self._step_counter += 1
            self.writer.write_step(
                turn=event.turn,
                step=self._step_counter,
                role="system",
                event_type="compaction",
                timestamp=event.ts,
                content="",
                meta={
                    "strategy": event.strategy,
                    "tokens_before": event.tokens_before,
                    "tokens_after": event.tokens_after,
                    "message_count_after": event.message_count_after,
                    "pruned_count": event.pruned_count,
                    "folded_turns": event.folded_turns,
                    "degraded": event.degraded,
                },
            )
        elif isinstance(event, ev.CheckpointCreated):
            self._step_counter += 1
            self.writer.write_step(
                turn=event.turn,
                step=self._step_counter,
                role="system",
                event_type="checkpoint",
                timestamp=event.ts,
                content="",
                path=event.path,
                before_hash=event.before_hash,
                after_hash=event.after_hash,
                meta={"files": event.files},
            )
        elif isinstance(event, ev.RewindPerformed):
            self._step_counter += 1
            self.writer.write_step(
                turn=event.to_turn,
                step=self._step_counter,
                role="system",
                event_type="session_rewind",
                timestamp=event.ts,
                content="",
                to_turn=event.to_turn,
                meta={"restored": event.restored, "deleted": event.deleted, "conflicts": event.conflicts},
            )
        elif isinstance(event, ev.TurnFinished):
            if event.turn_summary:
                self._step_counter += 1
                self.writer.write_step(
                    turn=event.turn,
                    step=self._step_counter,
                    role="system",
                    event_type="turn_finished",
                    timestamp=event.ts,
                    # ⚠️ 这里**刻意不写** ``content``（用户裁定 · 方案 A）。
                    #
                    # ``TurnFinished`` 事件只有 ``turn_summary``，没有 ``content``
                    # （见 ``kernel/events.py``；那边有个 ``content`` 属于
                    # ``ToolCallFinished``，不是这个事件）—— 所以早先那句
                    # ``content=event.turn_summary`` 是**从同一份数据抄出来的副本**。
                    #
                    # 代价不是磁盘（实测占全会话文件 0.054%），而是**双事实来源**：
                    # 两个字段被约定"必须永远相等"，却没有任何机制保证 ——
                    # 一旦分叉，读取方会静默偏向 ``turn_summary``，``content``
                    # 变成陈旧数据而**不报错**。（这就是本项目登记过的 F-03 反模式。）
                    #
                    # 实测依据：扫全部历史会话文件 **68/68** 条 ``turn_finished``
                    # 都是双字段，**0 条**是"只有 content"的老格式 ——
                    # 读侧那三处 ``or record.get("content")`` 回退**从未生效过**。
                    #
                    # 处置次序：**先停写、后停读**。读侧回退暂时保留（它在读路径上、
                    # 不写任何数据、不产生冗余），留一个弃用窗口。
                    # 别再把这一行加回来 —— ``tests/unit/test_store.py``
                    # 有守卫用例盯着它。
                    turn_summary=event.turn_summary,
                    reason=event.reason,
                    duration_ms=event.duration_ms,
                    tool_call_count=event.tool_call_count,
                    # ★ 用户裁定 Q-D：摘要来源要落盘 —— 否则列表里分不清
                    # 「模型自己写的」与「补写/自动生成的」。
                    summary_source=getattr(event, "summary_source", None),
                    summary_reason=getattr(event, "summary_reason", None),
                )
            self._current_text.clear()
            self._current_reasoning.clear()

