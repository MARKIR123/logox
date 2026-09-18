"""会话实时持久化总线订阅者 (D98)。

将用户输入、大模型响应增量、工具调用与结果实时落盘到该会话的 .jsonl 文件中。
保证即使用户中途 Ctrl+C 或发生崩溃，过往对话依然 100% 安全落盘。
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
            blob_path = self.writer.save_tool_blob(event.call_id, content_str)

            self.writer.write_step(
                turn=event.turn,
                step=self._step_counter,
                role="tool",
                event_type="tool_result",
                content=content_str,
                tool_name=tool_name,
                call_id=event.call_id,
                blob_file=blob_path,
                is_error=not event.ok,
                meta={"duration_ms": event.duration_ms, "args": call_info.get("args")},
            )
        elif isinstance(event, ev.CheckpointCreated):
            self._step_counter += 1
            self.writer.write_step(
                turn=event.turn,
                step=self._step_counter,
                role="system",
                event_type="checkpoint",
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
                    content=event.turn_summary,
                    turn_summary=event.turn_summary,
                    reason=event.reason,
                    duration_ms=event.duration_ms,
                    tool_call_count=event.tool_call_count,
                )
            self._current_text.clear()
            self._current_reasoning.clear()

