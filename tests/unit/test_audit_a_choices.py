"""End-to-end regression checks for the accepted audit A choices."""

import asyncio
import hashlib
import json
import threading
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from logox.app import Runtime
from logox.config.schema import LogoxConfig, McpServerConfig
from logox.context.builder import HierarchicalContextBuilder
from logox.context.storage import SessionTranscriptWriter
from logox.kernel.bus import EventBus
from logox.kernel.loop import KernelLoop
from logox.kernel.registry import ToolRegistry
from logox.kernel.scheduler import AllowAllDecider, Scheduler
from logox.kernel.turn import Turn, TurnStatus
from logox.mcp.client import McpClient
from logox.mcp.models import McpConnectionState
from logox.permissions.decider import HierarchicalPermissionDecider
from logox.permissions.engine import PermissionEngine
from logox.permissions.models import Decision
from logox.providers.base import ToolCallEvent
from logox.store.blob import BlobStore
from logox.store.rewind import execute_rewind
from logox.tools.base import ToolContext, ToolResult
from logox.tools.fs_glob import GlobTool
from logox.tools.fs_grep import GrepTool
from logox.tools.fs_read import ReadArgs, ReadTool
from logox.tools.fs_write import WriteTool
from logox.tools.shell import _BoundedOutput, _capture_stream, truncate_output


def run(coro):
    return asyncio.run(coro)


@pytest.mark.parametrize(
    "args",
    [
        {"path": "a.py", "cwd": "../"},
        {"path": "a.py", "file_path": ".env"},
        {"cwd": ".", "command": "cat .env"},
        {"path": ".", "command": "git show .git/config"},
    ],
)
def test_every_path_and_command_is_audited(tmp_path, args):
    assert PermissionEngine(tmp_path).evaluate("shell", args).decision == Decision.ASK


def test_real_scheduler_read_and_search_risk_checks(tmp_path):
    (tmp_path / "a.py").write_text("PUBLIC_NEEDLE", encoding="utf-8")
    (tmp_path / ".env").write_text("SECRET_NEEDLE", encoding="utf-8")
    registry = ToolRegistry()
    registry.register_all([ReadTool(), GrepTool(), GlobTool()])
    scheduler = Scheduler(
        EventBus(session_id="audit"),
        registry,
        HierarchicalPermissionDecider(cwd=tmp_path, wait_headless=False),
        cwd=tmp_path,
    )
    turn = Turn(turn_index=1, history=[])

    def call(name, **args):
        return ToolCallEvent(call_id=name, name=name, arguments=args)

    good = run(scheduler.run_batch(turn, [call("read", path="a.py")]))
    assert good[0].ok and "PUBLIC_NEEDLE" in good[0].content
    denied = run(scheduler.run_batch(turn, [call("read", path=".env")]))
    assert not denied[0].ok and "SECRET_NEEDLE" not in denied[0].content
    searched = run(scheduler.run_batch(turn, [call("grep", pattern="NEEDLE", path=".")]))
    assert (
        searched[0].ok
        and "PUBLIC_NEEDLE" in searched[0].content
        and "SECRET_NEEDLE" not in searched[0].content
    )
    denied = run(scheduler.run_batch(turn, [call("glob", pattern=".env*", path=".")]))
    assert not denied[0].ok


def test_explicit_deny_precedes_readonly_auto_allow(tmp_path):
    engine = PermissionEngine(tmp_path)
    engine.load_persisted(allow_rules=["deny read(*)"])
    # Use the supported rule API directly, independent of display parsing.
    from logox.permissions.models import PermissionRule, RuleScope

    engine.session_rules.append(
        PermissionRule(tool_name="read", pattern="*", decision=Decision.DENY, scope=RuleScope.SESSION)
    )
    assert engine.evaluate("read", {"path": "a.py"}, readonly=True).decision == Decision.DENY


def test_snapshot_failure_prevents_write(tmp_path):
    file = tmp_path / "a.py"
    file.write_text("BEFORE", encoding="utf-8")
    registry = ToolRegistry()
    registry.register(WriteTool())
    blobs = MagicMock()
    blobs.put_file.side_effect = OSError("disk full")
    scheduler = Scheduler(
        EventBus(session_id="audit"), registry, AllowAllDecider(), cwd=tmp_path, blob_store=blobs
    )
    result = run(
        scheduler.run_batch(
            Turn(turn_index=1, history=[]),
            [ToolCallEvent(call_id="w", name="write", arguments={"path": "a.py", "content": "AFTER"})],
        )
    )
    assert not result[0].ok and file.read_text(encoding="utf-8") == "BEFORE"


def test_read_runs_off_event_loop_and_cancel_signals_worker(tmp_path, monkeypatch):
    entered, release, observed = threading.Event(), threading.Event(), []
    loop_thread = threading.get_ident()
    tool = ReadTool()

    def worker(args, ctx):
        observed.append(threading.get_ident())
        entered.set()
        release.wait(3)
        observed.append(ctx.is_cancelled())
        return ToolResult(ok=True)

    monkeypatch.setattr(tool, "_run_sync", worker)

    async def exercise():
        task = asyncio.create_task(tool.run(ReadArgs(path="a.py"), ToolContext(cwd=tmp_path)))
        assert await asyncio.to_thread(entered.wait, 2)
        try:
            assert observed[0] != loop_thread
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        finally:
            release.set()

    run(exercise())
    assert observed[-1] is True


def test_failed_append_does_not_commit_line_and_retry_is_exact(tmp_path, monkeypatch):
    import builtins

    writer = SessionTranscriptWriter(log_file=tmp_path / "session.jsonl")
    original = builtins.open

    def broken(path, mode="r", *args, **kwargs):
        if mode == "a":
            raise OSError("disk full")
        return original(path, mode, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(builtins, "open", broken)
        assert writer.write_step(turn=1, step=1, role="user", event_type="user") is None
        assert writer.current_line == 0 and writer.turn_lines == {}
    assert writer.write_step(turn=1, step=1, role="user", event_type="user") == 1
    assert json.loads(writer.log_file.read_text())["line"] == 1


def test_damaged_tail_is_separated_from_next_record(tmp_path):
    file = tmp_path / "session.jsonl"
    file.write_text('{"partial":', encoding="utf-8")
    writer = SessionTranscriptWriter(log_file=file)
    assert writer.write_step(turn=2, step=1, role="user", event_type="user") == 2
    rows = file.read_text(encoding="utf-8").splitlines()
    assert len(rows) == 2 and json.loads(rows[1])["line"] == 2


def test_session_blob_namespace_prevents_same_id_collision(tmp_path):
    a = SessionTranscriptWriter(log_file=tmp_path / "a.jsonl")
    b = SessionTranscriptWriter(log_file=tmp_path / "b.jsonl")
    pa = a.save_tool_blob("same", "FIRST", force=True)
    pb = b.save_tool_blob("same", "SECOND", force=True)
    assert pa != pb
    assert (tmp_path / pa).read_text() == "FIRST"
    assert (tmp_path / pb).read_text() == "SECOND"
    assert a.blob_path_of("same") == pa and b.blob_path_of("same") == pb


def checkpoint(path, before, after):
    return {"type": "checkpoint", "turn": 1, "path": path, "before_hash": before, "after_hash": after}


@pytest.mark.parametrize("path", ["../outside.txt", "", "."])
def test_rewind_rejects_invalid_scope_before_writing(tmp_path, path):
    root = tmp_path / "root"
    root.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_bytes(b"OUTSIDE")
    blobs = BlobStore(tmp_path / "blobs")
    result = execute_rewind(
        [checkpoint(path, None, hashlib.sha256(b"OUTSIDE").hexdigest())], 1, root, blobs, force=True
    )
    assert not result.success and outside.read_bytes() == b"OUTSIDE"


def test_rewind_rejects_corrupted_snapshot_before_any_write(tmp_path):
    file = tmp_path / "a.py"
    file.write_bytes(b"AFTER")
    blobs = BlobStore(tmp_path / "blobs")
    before = blobs.put_bytes(b"BEFORE")
    blobs._blob_path(before).write_bytes(b"CORRUPT")
    result = execute_rewind(
        [checkpoint("a.py", before, hashlib.sha256(b"AFTER").hexdigest())], 1, tmp_path, blobs, force=True
    )
    assert not result.success and file.read_bytes() == b"AFTER"


def test_partial_rewind_can_retry_without_force(tmp_path, monkeypatch):
    blobs = BlobStore(tmp_path / "blobs")
    before = blobs.put_bytes(b"BEFORE")
    after = hashlib.sha256(b"AFTER").hexdigest()
    for name in ["a.py", "b.py"]:
        (tmp_path / name).write_bytes(b"AFTER")
    records = [checkpoint(name, before, after) for name in ["a.py", "b.py"]]
    restore = blobs.restore_to_file
    with monkeypatch.context() as patch:
        patch.setattr(
            blobs, "restore_to_file", lambda h, path: False if path.name == "b.py" else restore(h, path)
        )
        partial = execute_rewind(records, 1, tmp_path, blobs)
        assert not partial.success and partial.restored_files == ["a.py"]
    retry = execute_rewind(records, 1, tmp_path, blobs)
    assert retry.success and (tmp_path / "a.py").read_bytes() == (tmp_path / "b.py").read_bytes() == b"BEFORE"


def test_hash_must_be_hex_even_if_length_is_64(tmp_path):
    store = BlobStore(tmp_path / "blobs")
    with pytest.raises(ValueError):
        store.get_bytes("/" * 64)


@pytest.mark.parametrize("encoding", ["utf-8", "gbk", "latin-1"])
@pytest.mark.parametrize("chunk", [1, 3, 8192, 65536])
def test_bounded_stream_preserves_decoding_and_exact_truncation(encoding, chunk):
    text = ("中文开始" + "A" * 20000 + "中文结束") if encoding != "latin-1" else "é" + "A" * 20000 + "é"
    raw = text.encode(encoding)

    class Stream:
        position = 0

        async def read(self, size):
            result = raw[self.position : self.position + chunk]
            self.position += len(result)
            return result

    captured = run(_capture_stream(Stream()))
    assert captured.render() == truncate_output(text)
    assert len(captured.head) <= 8000 and len(captured.tail) <= 8000


def test_bounded_combination_matches_stdout_rstrip_and_stderr():
    stdout = _BoundedOutput()
    stderr = _BoundedOutput()
    text = "START" + " " * 18000 + "END" + " " * 20000
    for i in range(0, len(text), 130):
        stdout.append(text[i : i + 130])
    stderr.append("ERROR" + "E" * 9000)
    combined = _BoundedOutput()
    combined.extend(stdout.rstrip())
    combined.append("\n\n[stderr]:\n")
    combined.extend(stderr)
    assert combined.render() == truncate_output(text.rstrip() + "\n\n[stderr]:\n" + "ERROR" + "E" * 9000)


def test_error_marker_detected_in_discarded_middle_and_across_chunks():
    output = _BoundedOutput()
    output.append("A" * 10000 + "+ Categ")
    output.append("oryInfo" + "B" * 10000)
    assert output.error_marker and "CategoryInfo" not in output.render()[0]


def test_over_budget_turn_stops_before_provider(tmp_path):
    provider = MagicMock()
    builder = HierarchicalContextBuilder(
        system="SYSTEM" * 1000,
        cwd=tmp_path,
        window_capacity=256,
        reserve_tokens=64,
        transcript_writer=SessionTranscriptWriter(log_file=tmp_path / "s.jsonl"),
    )
    bus = EventBus(session_id="audit")
    events = []

    async def record(event):
        events.append(event)

    bus.subscribe("*", record, name="recorder")
    kernel = KernelLoop(bus, provider, ToolRegistry(), builder, model="small")
    turn = run(kernel.submit("CURRENT_REQUEST"))
    assert turn.status == TurnStatus.FAILED
    provider.stream.assert_not_called()
    assert any(getattr(event, "type", "") == "error_occurred" and "暂停" in event.message for event in events)
    assert any("CURRENT_REQUEST" in m.text for m in kernel.history)


def test_mcp_http_is_rejected_before_stdio_spawn(tmp_path, monkeypatch):
    import mcp.client.stdio

    spawn = MagicMock()
    monkeypatch.setattr(mcp.client.stdio, "stdio_client", spawn)
    client = McpClient(
        "http",
        McpServerConfig(name="http", transport="http", url="http://localhost:1234", command="unused"),
        cwd=tmp_path,
    )
    with pytest.raises(RuntimeError, match="尚未实现"):
        run(client.ensure_connected())
    spawn.assert_not_called()
    assert client.state == McpConnectionState.OFFLINE


def test_cancelled_mcp_start_closes_entered_contexts(tmp_path, monkeypatch):
    import mcp
    import mcp.client.stdio

    opened = asyncio.Event()
    closed = []

    @asynccontextmanager
    async def transport(params):
        try:
            yield (None, None)
        finally:
            closed.append("transport")

    class Session:
        def __init__(self, *args):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            closed.append("session")

        async def initialize(self):
            opened.set()
            await asyncio.Event().wait()

    monkeypatch.setattr(mcp.client.stdio, "stdio_client", transport)
    monkeypatch.setattr(mcp, "ClientSession", Session)
    client = McpClient("stdio", McpServerConfig(name="stdio", command="unused"), cwd=tmp_path)

    async def exercise():
        task = asyncio.create_task(client.ensure_connected())
        await opened.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert client._session is None and client._exit_stack is None
        assert client.state == McpConnectionState.STOPPED
        await client.close()
        await client.close()

    run(exercise())
    assert closed == ["session", "transport"]


def test_runtime_window_for_static_model_windows(tmp_path):
    registry = MagicMock()
    spec = SimpleNamespace(
        base_url="http://localhost:11434/v1",
        window_for=lambda m: 32768 if m in ("qwen:27b", "qwen:latest") else (16384 if m == "qwen" else None),
    )
    registry.spec.return_value = spec
    registry.window_for = lambda p, m: spec.window_for(m)
    runtime = Runtime(
        bus=MagicMock(),
        kernel=MagicMock(),
        reducer=MagicMock(),
        theme=MagicMock(),
        config=LogoxConfig(),
        provider_name="ollama",
        model="qwen:27b",
        cwd=tmp_path,
        tools=[],
        registry=registry,
    )
    assert runtime.window_for("qwen:27b") == 32768
    assert runtime.window_for("qwen") == 16384
    assert runtime.window_for("unknown") is None


def test_runtime_window_for_fallback_and_zero_network(tmp_path):
    registry = MagicMock()
    registry.window_for.return_value = 65536
    runtime = Runtime(
        bus=MagicMock(),
        kernel=MagicMock(),
        reducer=MagicMock(),
        theme=MagicMock(),
        config=LogoxConfig(),
        provider_name="ollama",
        model="qwen:8b",
        cwd=tmp_path,
        tools=[],
        registry=registry,
    )
    # 纯内存同步调用，不产生异步任务，直接返回静态窗口
    assert runtime.window_for("qwen:8b") == 65536
    registry.window_for.assert_called_once_with("ollama", "qwen:8b")


def test_explicit_deny_also_precedes_sensitive_prompt(tmp_path):
    from logox.permissions.models import PermissionRule, RuleScope

    engine = PermissionEngine(tmp_path)
    engine.session_rules.append(
        PermissionRule(tool_name="read", pattern="*", decision=Decision.DENY, scope=RuleScope.SESSION)
    )
    assert engine.evaluate("read", {"path": ".env"}, readonly=True).decision == Decision.DENY


def test_mcp_close_during_handshake_cancels_connection(tmp_path, monkeypatch):
    import mcp
    import mcp.client.stdio

    opened = asyncio.Event()
    closed = []

    @asynccontextmanager
    async def transport(params):
        try:
            yield None, None
        finally:
            closed.append("transport")

    class Session:
        def __init__(self, *args):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            closed.append("session")

        async def initialize(self):
            opened.set()
            await asyncio.Event().wait()

    monkeypatch.setattr(mcp.client.stdio, "stdio_client", transport)
    monkeypatch.setattr(mcp, "ClientSession", Session)
    client = McpClient("stdio", McpServerConfig(name="stdio", command="unused"), cwd=tmp_path)

    async def exercise():
        task = asyncio.create_task(client.ensure_connected())
        await opened.wait()
        await client.close()
        assert task.cancelled()
        assert client.state == McpConnectionState.STOPPED and client._session is None
        assert client._connect_task is None

    run(exercise())
    assert closed == ["session", "transport"]


def test_fixed_timeline_width_renders_once_with_or_without_overlay(monkeypatch):
    from logox.tui.render.screen import Screen
    from tests.tui.test_fullscreen_layout import DummyOverlayComponent, make_layout

    layout, terminal, timeline, _, _ = make_layout(columns=80, rows=24)
    for index in range(19):
        timeline.buffer.add_notice(f"LINE{index}")
    render = timeline.render
    calls = []

    def counted(width):
        calls.append(width)
        return render(width)

    monkeypatch.setattr(timeline, "render", counted)
    layout.render(80)
    assert calls == [79]
    calls.clear()
    screen = Screen(terminal)
    layout.screen = screen
    screen.show_overlay(DummyOverlayComponent(rows=10), width=80, anchor="bottom", push=True)
    layout.render(80)
    assert calls == [79]
    calls.clear()
    layout.restore_viewport_anchor(None, 0, 60)
    assert calls == [59]


def test_timeline_prefix_count_is_reused_without_scanning():
    from rich.text import Text

    from logox.tui.content.cards import CardContext
    from logox.tui.content.timeline import TimelineBuffer, TimelineRenderCache, render_cached
    from logox.tui.theme import load_theme

    buffer = TimelineBuffer()
    buffer.add_user("FIRST\nSECOND")
    buffer.add_assistant("TAIL")
    cache = TimelineRenderCache()
    context = CardContext(palette=load_theme("logox-dark").palette, width=80)
    blocks = buffer.visible_blocks
    render_cached(blocks, cache, context=context)
    assert cache.newline_count == cache.text.plain.count("\n")

    class CountForbidden(str):
        def count(self, *args):
            raise AssertionError("cached prefix must not be scanned again")

    class GuardedText(Text):
        @property
        def plain(self):
            return CountForbidden(super().plain)

    cache.text = GuardedText(cache.text.plain)
    result = render_cached(blocks, cache, context=context)
    assert result.prefix_hit


def test_new_model_planning_ignores_old_metric(tmp_path):
    builder = MagicMock()
    builder.plan.return_value = SimpleNamespace(tokens_before=10000, message_count_before=4)
    runtime = Runtime(
        bus=MagicMock(),
        kernel=SimpleNamespace(history=[object()], _last_request_usage=object()),
        reducer=SimpleNamespace(metrics=SimpleNamespace(context_tokens=1)),
        theme=MagicMock(),
        config=LogoxConfig(),
        provider_name="ollama",
        model="small",
        cwd=tmp_path,
        tools=[],
        context_builder=builder,
    )
    runtime.apply_compact = AsyncMock(return_value="COMPACTED")
    assert run(runtime.eager_compact_if_needed()) == "COMPACTED"
    assert builder.plan.call_args.kwargs["last_usage"] is None


def test_real_mcp_close_during_start_leaves_no_child(tmp_path):
    import os
    import sys

    marker = tmp_path / "child-pid.txt"
    code = f"import os,time;from pathlib import Path;Path({str(marker)!r}).write_text(str(os.getpid()));time.sleep(300)"
    client = McpClient(
        "slow",
        McpServerConfig(name="slow", command=sys.executable, args=["-c", code], startup_timeout_s=30),
        cwd=tmp_path,
    )

    def alive(pid):
        if sys.platform == "win32":
            import ctypes

            dll = ctypes.WinDLL("kernel32", use_last_error=True)
            dll.OpenProcess.restype = ctypes.c_void_p
            dll.OpenProcess.argtypes = [ctypes.c_ulong, ctypes.c_int, ctypes.c_ulong]
            dll.GetExitCodeProcess.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_ulong)]
            dll.CloseHandle.argtypes = [ctypes.c_void_p]
            handle = dll.OpenProcess(0x1000, False, pid)
            if not handle:
                return False
            try:
                status = ctypes.c_ulong()
                return bool(dll.GetExitCodeProcess(handle, ctypes.byref(status))) and status.value == 259
            finally:
                dll.CloseHandle(handle)
        try:
            os.kill(pid, 0)
            return True
        except ProcessLookupError:
            return False

    async def exercise():
        task = asyncio.create_task(client.ensure_connected())

        async def wait_started():
            while not marker.exists():
                await asyncio.sleep(0.01)

        try:
            await asyncio.wait_for(wait_started(), 5)
            pid = int(marker.read_text())
            await asyncio.wait_for(client.close(), 10)
            assert task.cancelled()
            assert not alive(pid)
            assert client._connect_task is None and client._session is None
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            await client.close()

    run(exercise())


def test_recursive_search_skips_alias_to_sensitive_file(tmp_path, monkeypatch):
    from pathlib import Path

    from logox.tools.fs_glob import GlobArgs, GlobTool
    from logox.tools.fs_grep import GrepArgs, GrepTool

    secret = tmp_path / ".env"
    secret.write_text("SECRET_NEEDLE", encoding="utf-8")
    alias = tmp_path / "ordinary.txt"
    alias.write_text("SECRET_NEEDLE", encoding="utf-8")
    original = Path.resolve

    def resolved(path, *args, **kwargs):
        return original(secret) if path == alias else original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "resolve", resolved)
    ctx = ToolContext(cwd=tmp_path)
    grep = run(GrepTool().run(GrepArgs(pattern="SECRET_NEEDLE"), ctx))
    glob = run(GlobTool().run(GlobArgs(pattern="*.txt"), ctx))
    assert grep.display.payload["count"] == 0
    assert "ordinary.txt" not in glob.display.payload["lines"]


def test_archive_index_uses_current_session_blob_directory(tmp_path):
    from logox.context.compaction import Compactor

    writer = SessionTranscriptWriter(log_file=tmp_path / "session.jsonl")
    pointer = writer.save_tool_blob("call_probe", "ARCHIVED_CONTENT", force=True)
    assert pointer is not None
    index = Compactor(transcript_writer=writer)._render_index().text
    assert str(writer.blob_dir) in index
    assert "tools/<call_id>.log" not in index
    assert (writer.session_dir / pointer).read_text(encoding="utf8") == "ARCHIVED_CONTENT"
    assert "tool_<call_id>.log" in index
