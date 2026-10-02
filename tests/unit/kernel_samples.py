"""共享测试样本：每个事件类型一个实例。

放在独立模块里，是为了让不同测试文件（事件总线 / 度量归约 / 时间线）共用同一份
样本，避免出现"某个测试漏了新增事件类型"的盲区。
"""

from __future__ import annotations

from logox.kernel import events as ev

__all__ = ["SESSION", "all_event_samples", "sample_of"]

SESSION = "s-samples"


def all_event_samples() -> list[ev.Event]:
    """全部 22 个事件类型各一个样本实例。"""
    usage = ev.Usage(input_tokens=10, output_tokens=5, cached_input_tokens=3)
    common = {"session_id": SESSION}
    return [
        ev.SessionStart(**common, cwd="/w", provider="openai-compatible", model="m"),
        ev.SessionEnd(**common, reason="user_quit", duration_ms=12),
        ev.UserPromptSubmit(**common, text="你好", text_chars=2),
        ev.ContextBuilt(**common, message_count=3, token_estimate=120),
        ev.ModelRequestStarted(**common, provider="p", model="m", token_estimate=120, request_index=0),
        ev.ModelDelta(**common, kind="reasoning", delta="思考", request_index=1),
        ev.ModelRequestFinished(**common, usage=usage, duration_ms=1000, first_token_ms=200),
        ev.TurnFinished(**common, turn_index=2, duration_ms=1500, tool_call_count=1, usage=usage),
        ev.ToolCallRequested(**common, call_id="c1", name="read", args={"path": "a.py"}, readonly=True),
        ev.PermissionRequested(**common, call_id="c1", prompt="允许？", rule_id="r1", risk="high"),
        ev.PermissionResolved(**common, call_id="c1", decision="allow", remember="session"),
        ev.ToolCallStarted(**common, call_id="c1", concurrent_group=0),
        ev.ToolCallFinished(
            **common,
            call_id="c1",
            ok=True,
            duration_ms=5,
            change_stat=ev.ChangeStat(kind="modify", added=8, removed=3),
        ),
        ev.CompactionStarted(**common, strategy="layered", tokens_before=1000, message_count_before=20),
        ev.CompactionFinished(**common, tokens_after=200, message_count_after=4, degraded=True),
        ev.CheckpointCreated(**common, files=["a.py", "b.py"], truncated_count=1),
        ev.RewindPerformed(**common, to_turn=1, restored=["a.py"], deleted=["new.py"], conflicts=["c.py"]),
        ev.QueueChanged(**common, depth=2, action="enqueued"),
        ev.RetryScheduled(**common, attempt=2, delay_s=2.0, reason="rate limit"),
        ev.ErrorOccurred(**common, category="network", message="超时", retryable=True),
        ev.McpServerStateChanged(**common, server="filesystem", state="ready", tool_count=6),
        ev.SubscriberQuarantined(**common, subscriber="telemetry", failures=3, last_error="boom"),
    ]


def sample_of(event_type: type[ev.Event]) -> ev.Event:
    """按类型取样本（找不到即报错，避免静默漏测）。"""
    for sample in all_event_samples():
        if type(sample) is event_type:
            return sample
    raise KeyError(f"没有 {event_type.__name__} 的样本")
