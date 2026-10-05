"""会话历史确定性回放与反序列化引擎（D98）。

从 .jsonl 会话记录中重建：
1. 内核历史消息列表 `list[Message]`（供模型在断点处继续推理）；
2. 终端时间线 `TimelineComponent`（供用户在启动后看到完整的过往对话气泡与工具卡片）。
"""

from __future__ import annotations

import contextlib
import json
import logging
from collections.abc import Callable
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
    """读取有效记录；旧记录的未知时间只在读视图补 null，不改原文件。"""
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
                        data.setdefault("timestamp", data.get("ts"))
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
    #: 最近一次追加的 assistant 消息下标；`turn_finished` 记录靠它把摘要**回刻**上去（F-33）。
    last_assistant_index: int | None = None

    #: ★ CHANGE-052：与 ``messages`` **下标一一对应**的行号表（`transcript.jsonl` 的行号）。
    #:
    #: 为什么与 `messages` 平行存放、最后统一盖戳：本函数有 7 处构造消息
    #: （user / assistant / tool / 自愈补的 assistant…），逐处在 `Message(...)` 里
    #: 加参数迟早漏一处 —— 而漏掉的那条**没有任何症状**，只是行号出现一个洞：
    #: 该轮的行区间 min/max 因此偏小，模型照它 `fs_read` 会读到**错的段落**。
    #: 平行存放只需在每次 append 旁多一行，且天然覆盖以后新增的构造点。
    lines: list[int | None] = []

    for record in records:
        event_type = record.get("type")
        role = record.get("role")
        content = record.get("content", "")
        # ★ CHANGE-052：本记录在 `transcript.jsonl` 里的行号（`write_step` 自始就写它）。
        #   取不到（手工构造的记录、极老格式）就是 None ⇒ 该消息不带行号，不报错。
        raw_line = record.get("line")
        line_no = int(raw_line) if isinstance(raw_line, int) else None

        # 忽略初始化头与系统内部元数据记录
        if event_type == "anamnesis_ref":
            continue

        if event_type in ("session_init", "meta", "checkpoint", "session_rewind"):
            continue

        if role == "user" or event_type in ("user_prompt", "UserPromptSubmit"):
            if content:
                messages.append(Message(role="user", blocks=[TextBlock(text=str(content))]))
                lines.append(line_no)
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
                lines.append(line_no)
                last_assistant_index = len(messages) - 1
            elif last_assistant_index is not None:
                # 该轮的 assistant 没有正文/工具块（空回答），摘要就挂在最后一个有块的上
                pass
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
                    lines.append(line_no)

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
                lines.append(line_no)

        elif event_type == "turn_finished":
            # ★ F-33：把本轮摘要**回刻**到对应 assistant 消息的 `meta` 上。
            #
            # 不回刻的后果很重：`/resume` 之后压缩索引的每一条目都只能是
            # 「（本轮无摘要）」—— 而“每轮做了什么”正是压缩后最容易丢的信息。
            # 实测：真实会话磁盘上有 5 条带摘要的记录，重建出的 182 条消息里带摘要的 **0** 条。
            summary = record.get("turn_summary") or record.get("content")
            if (
                isinstance(summary, str)
                and summary.strip()
                and last_assistant_index is not None
            ):
                target = messages[last_assistant_index]
                # 来源与摘要同源落盘 ⇒ 回刻时必须一起带回。
                # 只带回摘要会把 Q-D 裁定的「来源自证」抹成 None
                # （实测：磁盘 55 条带来源 → 重建后 0 条）。
                meta_update: dict[str, Any] = {"turn_summary": summary.strip()}
                source = record.get("summary_source")
                if isinstance(source, str) and source:
                    meta_update["summary_source"] = source
                messages[last_assistant_index] = target.model_copy(
                    update={"meta": target.meta.model_copy(update=meta_update)}
                )

    # ★ CHANGE-052：统一盖行号戳 —— 放在最后是为了与上面的"平行表"配合，
    #   并保证 F-33 的回刻（它 `model_copy` 了 meta）不会与盖戳互相覆盖：
    #   回刻先做、盖戳后做，两者改的是 meta 的不同字段。
    if any(number is not None for number in lines):
        messages = [
            message
            if number is None
            else message.model_copy(
                update={
                    "meta": message.meta.model_copy(update={"transcript_line": number})
                }
            )
            for message, number in zip(messages, lines, strict=True)
        ]

    return messages



def _display_of(meta: object) -> dict:
    """从记录的 `meta` 里取出展示提示（不是 dict 就当作没有）。"""
    if isinstance(meta, dict):
        display = meta.get("display")
        if isinstance(display, dict):
            return display
    return {}


def _has_rich_display(meta: object) -> bool:
    """记录里是否带着"自带渲染器"的展示提示（diff / lines / table / error）。"""
    return _display_of(meta).get("kind") in ("diff", "lines", "table", "error")


def _attach_replayed_diff(buffer: Any, *, meta: object, call_id: str) -> None:
    """把记录里的 `diff` 展示提示重新挂成 diff 块（D139）。

    为什么值得单独一段：`/resume` 之后"我上次到底改了什么"是最常被回看的信息之一。
    只把文本内容带回来、不带 diff，等于**把最有价值的那部分留在磁盘上**。
    """
    display = _display_of(meta)
    if display.get("kind") != "diff":
        return
    payload = display.get("payload")
    if not isinstance(payload, dict):
        return
    hunks = payload.get("hunks")
    if not isinstance(hunks, list) or not hunks or not hasattr(buffer, "attach_diff"):
        return
    buffer.attach_diff(call_id=call_id, path=str(payload.get("path", "")), hunks=hunks)



def _finish_tool_compat(
    buffer: Any,
    *,
    call_id: str,
    ok: bool,
    duration_ms: int,
    payload: str,
) -> None:
    """用**尽可能多**的信息调用 `finish_tool`，实现缺参数时**逐级降级**。

    为什么要这么写：`replay.py` 面对的是"任意实现了缓冲区协议的对象"——
    TUI 的真缓冲、测试里的 FakeBuffer、以及未来可能的老版本缓冲。
    它们支持的参数集合不同，而我们**不想**因为多了个 `payload` 就让回放整个炸掉。
    降级顺序：`duration_ms + payload` → `duration_ms` → 什么都不带。

    代价（明确登记）：如果 `finish_tool` **内部**真的抛 `TypeError`（而不是签名不匹配），
    这里会把它当成"签名不匹配"再试一次 —— 最多多做两次无用调用。这是刻意的取舍：
    回放路径**宁可少显示一点信息，也不能整屏历史崩掉**。
    """
    for extra in (
        {"duration_ms": duration_ms, "payload": payload},
        {"duration_ms": duration_ms},
        {},
    ):
        try:
            buffer.finish_tool(call_id=call_id, ok=ok, **extra)
            return
        except TypeError:
            continue


def replay_into_timeline(
    records: list[dict[str, Any]],
    timeline: Any,
    *,
    summarize: Callable[[Any], str] | None = None,
    format_args: Callable[[Any], str] | None = None,
) -> int:
    r"""把历史对白回放到时间线渲染器中，返回回放的有效条数。

    ★ **F-43 的第二半（D138）**：`/resume` 恢复会话走的是**这条路径** ——
    卡片不是由事件流驱动的，而是**从 transcript 记录重建**的。以前这里有两个缺口：

    1. `finish_tool()` **不传 `payload`** ⇒ 重放出来的卡片**展开后永远是空的**。
       用户的体感就是"工具卡展开看不到东西"（而新会话里其实已经有了）——
       这类"一半路径修了、另一半没修"的缺口，比完全没修更难查。
    2. `args_summary = str(meta["args"])` ⇒ 卡片上摊的是**原始字典**
       （`{'command': '& .\.venv\Scripts\python.exe -c "…'`），又长又难读。

    第 ② 点用**依赖注入**解决：`summarize` 由调用方（装配根 `app.py`）传入
    `logox.tui.format.summarize_args`。为什么不直接在 store 里 import 它 ——
    因为**分层红线**：`store` 不许依赖 `tui`（`tests/unit/test_kernel_port.py` 盯着）。
    传函数进来既守住了分层，又能让**老会话的记录**（当时没存摘要）也享受同样的摘要逻辑。
    """
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

        if event_type == "anamnesis_ref":
            run_id = record.get("run_id", "")
            if isinstance(run_id, str) and run_id.isascii() and run_id.isalnum():
                add_reference = getattr(buffer, "add_anamnesis_reference", None)
                if callable(add_reference):
                    add_reference(run_id)
            continue

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
                # ⚠️ D135 第三步：标签机制已**整体退役** —— 这里不再做任何剥离。
                # 老会话里若残留 `<turn_summary>` 标签，就让它**原样显示**
                # （用户裁定：不向下兼容，之前那些就让它去吧）。
                buffer.add_delta(str(content))
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
                    _finish_tool_compat(
                        buffer,
                        call_id=str(call_id),
                        ok=not bool(record.get("is_error", False)),
                        duration_ms=duration,
                        # 有自带渲染器的展示提示时不再重复铺原文（与实时路径同一规则）
                        payload="" if _has_rich_display(meta) else (str(content) if content else ""),
                    )
                    # ★ D139：把记录里的 diff 提示重新挂成 diff 块。
                    #   `attach_diff` 接受纯 dict 的 hunks，所以**这一层不需要认识界面类型**
                    #   （分层红线：store 不许 import tui）。
                    _attach_replayed_diff(buffer, meta=meta, call_id=str(call_id))
                count += 1
        elif role == "tool":
            tool_name = record.get("tool") or record.get("tool_name", "tool")
            call_id = record.get("call_id")
            if tool_name and call_id and hasattr(buffer, "start_tool") and str(call_id) not in seen_calls:
                seen_calls.add(str(call_id))
                meta = record.get("meta")
                args_summary = ""
                if isinstance(meta, dict) and "args" in meta:
                    raw_args = meta["args"]
                    # ① 优先用记录里存的摘要（如果当时存了 —— 所见即当时所见）
                    stored = meta.get("args_summary")
                    if isinstance(stored, str) and stored:
                        args_summary = stored
                    elif summarize is not None:
                        # ② 注入的摘要函数（TUI 传 summarize_args）—— 老记录也能受益
                        args_summary = summarize(raw_args)
                    else:
                        # ③ 最后的兜底：非 TUI 消费者（脚本/无界面）才走到这里
                        args_summary = str(raw_args)
                # ★ D139：展开态第一项「完整参数」——回放路径同样要有
                #   （否则 `/resume` 之后 `edit` 又只剩一行统计）。
                args_text = ""
                if isinstance(meta, dict) and "args" in meta and format_args is not None:
                    args_text = format_args(meta["args"])
                buffer.start_tool(
                    call_id=str(call_id),
                    name=str(tool_name),
                    args_summary=args_summary,
                    args_text=args_text,
                )
                if hasattr(buffer, "finish_tool"):
                    duration = 0
                    if isinstance(meta, dict) and "duration_ms" in meta:
                        duration = int(meta["duration_ms"] or 0)
                    # ★ F-43 第二半：`content` 就是**工具当时的输出**，
                    #   不传它卡片展开后就是空的（用户报障的原话："还是看不到"）。
                    _finish_tool_compat(
                        buffer,
                        call_id=str(call_id),
                        ok=not bool(record.get("is_error", False)),
                        duration_ms=duration,
                        # ★ D139：有自带渲染器的展示提示时不再重复铺原文（与实时路径同一规则）
                        payload="" if _has_rich_display(meta) else (str(content) if content else ""),
                    )
                    # ★ D139：把记录里的 diff 提示重新挂成 diff 块。
                    #   `attach_diff` 接受纯 dict 的 hunks，所以**这一层不需要认识界面类型**
                    #   （分层红线：store 不许 import tui）。
                    _attach_replayed_diff(buffer, meta=meta, call_id=str(call_id))
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
    *,
    summarize: Callable[[Any], str] | None = None,
    format_args: Callable[[Any], str] | None = None,
    context_builder: Any | None = None,
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
        if context_builder is not None:
            context_builder.restore_state(kernel_loop.history, filtered_records)
        if hasattr(kernel_loop, "_last_request_usage"):
            kernel_loop._last_request_usage = None

    timeline_count = replay_into_timeline(
        filtered_records, timeline, summarize=summarize, format_args=format_args
    )
    return len(messages), timeline_count

