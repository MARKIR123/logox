"""会话历史确定性回放与反序列化引擎（D98）。

从 .jsonl 会话记录中重建：
1. 内核历史消息列表 `list[Message]`（供模型在断点处继续推理）；
2. 终端时间线 `TimelineComponent`（供用户在启动后看到完整的过往对话气泡与工具卡片）。
"""

from __future__ import annotations

import contextlib
import json
import logging
from pathlib import Path
from typing import Any

from logox.kernel.messages import (
    Message,
    ReasoningBlock,
    TextBlock,
    ToolResultBlock,
    ToolUseBlock,
)

logger = logging.getLogger(__name__)

__all__ = [
    "filter_rewound_records",
    "load_session_records",
    "reconstruct_messages",
    "replay_into_timeline",
    "replay_session",
]


def load_session_records(file_path: Path | str) -> list[dict[str, Any]]:
    """读取指定会话 .jsonl 文件中的所有有效记录。"""
    path = Path(file_path)
    if not path.is_file():
        return []

    records: list[dict[str, Any]] = []
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    data = json.loads(line)
                    if isinstance(data, dict):
                        records.append(data)
                except json.JSONDecodeError:
                    continue
    except OSError as exc:
        logger.warning("读取会话文件 %s 失败：%s", path, exc)

    return records


def reconstruct_messages(
    records: list[dict[str, Any]],
    session_dir: Path | str | None = None,
) -> list[Message]:
    """将会话记录逐条装配为中立的 Message 消息列表。

    协议守卫（Protocol Invariant Guard）：
    1. 还原 assistant 消息时，恢复其包含的 TextBlock、ReasoningBlock 与全部 ToolUseBlock；
    2. 还原 tool 结果消息时，确保其与紧跟的前序 assistant 消息的 tool_calls 严格对齐；
       若历史记录因异常中断或历史格式缺陷导致 assistant 未登记该 tool_call，
       自动自愈（Self-Healing）向前序 assistant 补齐对应的 ToolUseBlock，
       彻底避免 OpenAI / DeepSeek 端点报 400（'Messages with role tool must be a response to a preceding message with tool_calls'）。
    """
    messages: list[Message] = []

    for record in records:
        event_type = record.get("type")
        role = record.get("role")
        content = record.get("content", "")

        # 忽略初始化头与系统内部元数据记录
        if event_type in ("session_init", "meta", "checkpoint", "session_rewind"):
            continue

        if role == "user" or event_type in ("user_prompt", "UserPromptSubmit"):
            if content:
                messages.append(Message(role="user", blocks=[TextBlock(text=str(content))]))
        elif role == "assistant":
            blocks: list[Any] = []
            # 推理内容（如果有）
            reasoning = record.get("reasoning")
            if isinstance(reasoning, str) and reasoning:
                blocks.append(ReasoningBlock(text=reasoning))
            # 文本正文
            if content:
                blocks.append(TextBlock(text=str(content)))

            # 1. 结构化 tool_calls 列表
            raw_tool_calls = record.get("tool_calls")
            if isinstance(raw_tool_calls, list):
                for tc in raw_tool_calls:
                    if isinstance(tc, dict) and tc.get("id"):
                        blocks.append(
                            ToolUseBlock(
                                id=str(tc["id"]),
                                name=str(tc.get("name", "tool")),
                                input=tc.get("arguments", {}) if isinstance(tc.get("arguments"), dict) else {},
                            )
                        )

            # 2. 兼容历史记录的单工具调用字段
            tool_name = record.get("tool")
            call_id = record.get("call_id")
            if tool_name and call_id and not any(isinstance(b, ToolUseBlock) and b.id == str(call_id) for b in blocks):
                args = record.get("meta", {}).get("args", {}) if isinstance(record.get("meta"), dict) else {}
                blocks.append(ToolUseBlock(id=str(call_id), name=str(tool_name), input=args))

            if blocks:
                messages.append(Message(role="assistant", blocks=blocks))
        elif role == "tool":
            call_id = record.get("call_id", "")
            is_error = bool(record.get("is_error", False))
            tool_name = record.get("tool") or record.get("tool_name", "tool")
            tool_args = record.get("meta", {}).get("args", {}) if isinstance(record.get("meta"), dict) else {}

            tool_content = str(content or "")
            blob_rel = record.get("blob")
            if blob_rel and session_dir:
                blob_file = Path(session_dir) / blob_rel
                if blob_file.is_file():
                    with contextlib.suppress(OSError):
                        tool_content = blob_file.read_text(encoding="utf-8", errors="replace")

            if call_id:
                # ★ 自愈守卫（Self-Healing Guard）：
                # 检查前一个消息是否为 assistant 并包含该 call_id
                target_assistant: Message | None = None
                if messages and messages[-1].role == "assistant":
                    target_assistant = messages[-1]
                elif len(messages) >= 2 and messages[-1].role == "tool":
                    # 连续 tool 调用（多工具调用批次），向上寻找承载该批调用的 assistant
                    for prev_msg in reversed(messages):
                        if prev_msg.role == "assistant":
                            target_assistant = prev_msg
                            break
                        elif prev_msg.role != "tool":
                            break

                if target_assistant is not None:
                    existing_call_ids = {b.id for b in target_assistant.blocks_of(ToolUseBlock)}
                    if str(call_id) not in existing_call_ids:
                        target_assistant.blocks.append(
                            ToolUseBlock(id=str(call_id), name=str(tool_name), input=tool_args)
                        )
                else:
                    # 如果前面完全没有 assistant（异常破坏历史），补一个 assistant 消息作为容器
                    target_assistant = Message(
                        role="assistant",
                        blocks=[ToolUseBlock(id=str(call_id), name=str(tool_name), input=tool_args)],
                    )
                    messages.append(target_assistant)

                messages.append(
                    Message(
                        role="tool",
                        blocks=[
                            ToolResultBlock(
                                id=str(call_id),
                                ok=not is_error,
                                content=tool_content,
                            )
                        ],
                    )
                )

    return messages


def replay_into_timeline(records: list[dict[str, Any]], timeline: Any) -> int:
    """把历史对白回放到时间线渲染器中，返回回放的有效条数。"""
    if timeline is None:
        return 0

    buffer = getattr(timeline, "buffer", timeline)
    if buffer is None:
        return 0

    count = 0
    seen_calls: set[str] = set()

    for record in records:
        event_type = record.get("type")
        role = record.get("role")
        content = record.get("content", "")

        if event_type in ("session_init", "meta", "checkpoint", "session_rewind"):
            continue

        if role == "user" or event_type in ("user_prompt", "UserPromptSubmit"):
            if content:
                buffer.add_user(str(content))
                count += 1
        elif role == "assistant":
            reasoning = record.get("reasoning")
            if isinstance(reasoning, str) and reasoning and hasattr(buffer, "add_reasoning_delta"):
                buffer.add_reasoning_delta(reasoning)

            if content:
                import re
                clean_content = re.sub(
                    r"<turn_summary.*?>.*?(?:</turn_summary>|$)",
                    "",
                    str(content),
                    flags=re.DOTALL | re.IGNORECASE,
                ).rstrip()
                if clean_content:
                    buffer.add_delta(clean_content)
                    buffer.flush_delta()
                    count += 1

            # 兼容 assistant 记录内自带工具调用的老格式
            tool_name = record.get("tool")
            call_id = record.get("call_id")
            if tool_name and call_id and hasattr(buffer, "start_tool") and str(call_id) not in seen_calls:
                seen_calls.add(str(call_id))
                buffer.start_tool(call_id=str(call_id), name=str(tool_name), args_summary="")
                if hasattr(buffer, "finish_tool"):
                    duration = 0
                    meta = record.get("meta")
                    if isinstance(meta, dict) and "duration_ms" in meta:
                        duration = int(meta["duration_ms"] or 0)
                    try:
                        buffer.finish_tool(
                            call_id=str(call_id),
                            ok=not bool(record.get("is_error", False)),
                            duration_ms=duration,
                        )
                    except TypeError:
                        buffer.finish_tool(
                            call_id=str(call_id),
                            ok=not bool(record.get("is_error", False)),
                        )
                count += 1
        elif role == "tool":
            tool_name = record.get("tool") or record.get("tool_name", "tool")
            call_id = record.get("call_id")
            if tool_name and call_id and hasattr(buffer, "start_tool") and str(call_id) not in seen_calls:
                seen_calls.add(str(call_id))
                args_summary = ""
                meta = record.get("meta")
                if isinstance(meta, dict) and "args" in meta:
                    args_summary = str(meta["args"])
                buffer.start_tool(call_id=str(call_id), name=str(tool_name), args_summary=args_summary)
                if hasattr(buffer, "finish_tool"):
                    duration = 0
                    if isinstance(meta, dict) and "duration_ms" in meta:
                        duration = int(meta["duration_ms"] or 0)
                    try:
                        buffer.finish_tool(
                            call_id=str(call_id),
                            ok=not bool(record.get("is_error", False)),
                            duration_ms=duration,
                        )
                    except TypeError:
                        buffer.finish_tool(
                            call_id=str(call_id),
                            ok=not bool(record.get("is_error", False)),
                        )
                count += 1

    return count


def filter_rewound_records(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """剔除因 session_rewind 被截除的历史记录。"""
    active: list[dict[str, Any]] = []
    for r in records:
        if r.get("type") == "session_rewind":
            rewind_to = int(r.get("to_turn", 0))
            active = [item for item in active if int(item.get("turn", 0)) < rewind_to]
        else:
            active.append(r)
    return active


def replay_session(
    file_path: Path | str,
    kernel_loop: Any | None = None,
    timeline: Any | None = None,
) -> tuple[int, int]:
    """统一回放入口：同时恢复内核消息历史与时间线视图。

    返回 `(消息数, 时间线回放数)`。
    """
    records = load_session_records(file_path)
    if not records:
        return 0, 0

    session_dir = Path(file_path).parent
    filtered_records = filter_rewound_records(records)
    messages = reconstruct_messages(filtered_records, session_dir=session_dir)
    if kernel_loop is not None and hasattr(kernel_loop, "history"):
        kernel_loop.history.extend(messages)

    timeline_count = replay_into_timeline(filtered_records, timeline)
    return len(messages), timeline_count

