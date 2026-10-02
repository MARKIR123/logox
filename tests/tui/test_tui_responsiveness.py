"""D199: workload, complete display and reading position regressions."""
from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from logox.kernel import events as ev
from logox.kernel.loop import SimpleContextBuilder
from logox.tui.content.cards import CardContext
from logox.tui.content.markdown import MarkdownRenderCache, parse_markdown, render_markdown
from logox.tui.content.timeline import ActiveStatus, Block, render_blocks
from logox.tui.render.ansi import split_styled_lines, text_to_ansi
from logox.tui.render.app import InlineApp, TimelineComponent
from logox.tui.render.fullscreen import FullscreenApp
from logox.tui.render.keys import Key
from logox.tui.render.screen import Screen
from logox.tui.render.terminal import FakeTerminal
from logox.tui.theme import load_theme
from tests.tui.test_render_inline_app import _Runtime
from tests.unit.kernel_support import Pause, install, text_chunks


def component() -> TimelineComponent:
    return TimelineComponent(load_theme("logox-dark").palette)


def assert_full_display(timeline: TimelineComponent, width: int = 80) -> None:
    full = render_blocks(
        timeline.buffer.visible_blocks, CardContext(palette=timeline.palette, width=width),
        expand_tools=timeline.buffer.expand_tools,
        expand_reasoning=timeline.buffer.expand_reasoning,
        show_track=timeline.buffer.show_track, active_status=timeline.buffer.active_status,
    )
    assert [text_to_ansi(row) for row in timeline.render(width)] == [
        text_to_ansi(row) for row in split_styled_lines(full)
    ]


def test_new_prompt_does_not_reparse_cached_history() -> None:
    timeline = component()
    for index in range(100):
        timeline.buffer.add_user(f"user {index}")
        timeline.buffer.add_assistant("**answer** `code`\n\nparagraph")
    timeline.render(80)
    original = render_blocks
    touched = []

    def count(blocks, *args, **kwargs):
        touched.extend(blocks)
        return original(blocks, *args, **kwargs)

    timeline.buffer.add_user("NEW_PROMPT")
    with patch("logox.tui.content.timeline.render_blocks", count):
        timeline.render(80)
    assert [block.text for block in touched] == ["NEW_PROMPT"]
    assert_full_display(timeline)


def test_completed_long_last_answer_requires_no_repeat_layout() -> None:
    timeline = component()
    timeline.buffer.add_assistant("**answer** `code`\n\n" * 1000)
    timeline.render(80)
    with patch("logox.tui.content.timeline.render_blocks", wraps=render_blocks) as renderer:
        timeline.render(80)
        timeline.render(80)
    assert renderer.call_count == 0


def test_older_tool_finish_is_visible_without_another_new_tool() -> None:
    timeline = component()
    timeline.buffer.start_tool(call_id="old", name="grep")
    for index in range(30):
        timeline.buffer.add_notice(f"later {index}")
    timeline.render(80)
    timeline.buffer.finish_tool(call_id="old", ok=True, duration_ms=400)
    assert "0.4s" in "\n".join(row.plain for row in timeline.render(80))
    assert_full_display(timeline)


def test_older_reasoning_change_and_equal_plain_restyle_are_not_stale() -> None:
    timeline = component()
    timeline.buffer.start_reasoning()
    answer = timeline.buffer.add_assistant("`same`")
    timeline.buffer.add_user("later")
    timeline.buffer.expand_reasoning = True
    before = timeline.render(80)
    timeline.buffer.update_reasoning("older reasoning changed")
    answer.text = "**same**"
    after = timeline.render(80)
    assert [text_to_ansi(row) for row in before] != [text_to_ansi(row) for row in after]
    assert_full_display(timeline)


def test_reasoning_steps_once_per_frame() -> None:
    timeline = component()
    timeline.buffer.add_reasoning_delta("r" * 120)
    timeline.buffer.add_delta("t" * 120)
    smoother = timeline.buffer.smoother
    with patch.object(smoother, "step_reasoning", wraps=smoother.step_reasoning) as step:
        assert timeline.step()
    assert step.call_count == 1


@pytest.mark.parametrize("surface", [InlineApp, FullscreenApp])
def test_accepted_prompt_is_painted_before_context_build(surface) -> None:
    async def run():
        bundle = install([[Pause(.01), *text_chunks("done")]])
        observed = []

        class ProbeBuilder(SimpleContextBuilder):
            def build(self, history, **kwargs):
                observed.append("PROMPT_ECHO" in app.frame_text())
                return super().build(history, **kwargs)

        bundle.kernel._builder = ProbeBuilder("sys")
        runtime = SimpleNamespace(
            kernel=bundle.kernel, bus=bundle.bus, model="mock",
            config=SimpleNamespace(provider=SimpleNamespace(thinking_effort="auto")),
        )
        app = surface(runtime=runtime, terminal=FakeTerminal(columns=100, rows=30))
        app.screen.on_defer = lambda _delay, _callback: None
        app._loop = asyncio.get_running_loop()
        app.editor.handle_input(Key("P", char="PROMPT_ECHO"))
        app.editor.handle_input(Key("enter"))
        await asyncio.sleep(.05)
        for turn in bundle.kernel._turns:
            await bundle.kernel.wait(turn)
        await bundle.bus.aclose()
        assert observed and all(observed)
        assert sum(b.kind == "user" and b.text == "PROMPT_ECHO" for b in app.timeline.buffer.blocks) == 1

    asyncio.run(run())


def app_with_history(surface):
    app = surface(runtime=_Runtime(), terminal=FakeTerminal(columns=80, rows=20))
    app.screen.on_defer = lambda _delay, _callback: None
    app.timeline.buffer.start_tool(call_id="old", name="grep")
    for index in range(60):
        app.timeline.buffer.add_notice(f"HISTORY_{index}")
    app.screen.render_now()
    return app


def test_inline_old_card_and_new_tool_do_not_erase_or_replay_history() -> None:
    app = app_with_history(InlineApp)
    app.terminal.reset()
    app.timeline.buffer.finish_tool(call_id="old", ok=True, duration_ms=400)
    app.screen.render_now()
    app.timeline.buffer.start_tool(call_id="new", name="read")
    app.screen.render_now()
    assert "\x1b[3J" not in app.terminal.output
    assert "HISTORY_0" not in app.terminal.output
    assert "read" in app.frame_text()


def test_inline_resize_repaints_visible_tail_without_replaying_history() -> None:
    app = app_with_history(InlineApp)
    app.terminal.reset()
    app.terminal.resize(90, 18)
    app.screen.render_now()
    assert "\x1b[3J" not in app.terminal.output
    assert "HISTORY_0" not in app.terminal.output
    assert "HISTORY_59" in app.terminal.output


@pytest.mark.parametrize("update", ["tool", "text", "resize", "dock", "older", "overlay", "overlay_hide"])
def test_fullscreen_keeps_reading_content_across_updates(update) -> None:
    app = app_with_history(FullscreenApp)
    if update == "overlay_hide":
        from tests.tui.test_fullscreen_layout import DummyOverlayComponent
        app.screen.show_overlay(DummyOverlayComponent(), width=80, anchor="bottom", push=True)
        app.screen.render_now()
    app.root.scroll_offset = 20
    app.screen.render_now()
    anchor = app.root.get_viewport_anchor(80)
    if update == "tool":
        asyncio.run(app._on_event(ev.ToolCallRequested(session_id="s", call_id="new", name="read")))
    elif update == "text":
        app.timeline.buffer.add_assistant("new content\n\n" * 10)
    elif update == "resize":
        app.terminal.resize(100, 25)
    elif update == "dock":
        app.editor.handle_input(Key("x", char="x\n" * 5))
    elif update == "older":
        app.timeline.buffer.finish_tool(call_id="old", ok=True, duration_ms=4, payload="line\n" * 20)
        app.timeline.buffer.blocks[0].expanded = True
    elif update == "overlay":
        from tests.tui.test_fullscreen_layout import DummyOverlayComponent
        app.screen.show_overlay(DummyOverlayComponent(), width=80, anchor="bottom", push=True)
    else:
        app.screen.hide_overlay()
    app.screen.render_now()
    assert app.root.get_viewport_anchor(app.terminal.columns) == anchor


def test_fullscreen_at_bottom_keeps_following_new_messages() -> None:
    app = app_with_history(FullscreenApp)
    app.timeline.buffer.add_notice("LATEST_MESSAGE")
    app.screen.render_now()
    assert app.root.scroll_offset == 0
    assert "LATEST_MESSAGE" in app.frame_text()


@pytest.mark.parametrize("source", [
    "heading\n\n**paragraph 中文** `code`\n\nnext",
    "```python\ndef f():\n    return 123\n```\n\nnext",
    "| header | 中文 |\n|---|---|\n| cell | **value** |\n| longer | next |",
])
def test_growing_markdown_cache_matches_full_styles_and_releases_old_versions(source) -> None:
    cache = MarkdownRenderCache()
    context = CardContext(palette=load_theme("logox-dark").palette)
    for length in range(1, len(source) + 1):
        text = source[:length]
        cached = render_markdown(text, 40, context, cache=cache)
        full = render_markdown(text, 40, context)
        assert [text_to_ansi(row) for row in cached] == [text_to_ansi(row) for row in full]
        assert len(cache.blocks) <= len(parse_markdown(text))


def test_clear_releases_message_and_row_caches() -> None:
    timeline = component()
    timeline.buffer.add_assistant("large answer\n\n" * 100)
    timeline.render(80)
    timeline.buffer.blocks.clear()
    assert timeline.render(80) == []
    assert timeline._cache.covers == 0
    assert timeline._block_rows == {}


@pytest.mark.parametrize("kind", ["notice", "raw", "divider"])
@pytest.mark.parametrize("source", ["", "**answer**\n\n```py\nprint(1)\n```"])
@pytest.mark.parametrize("show_track", [True, False])
def test_active_status_after_empty_block_matches_complete_display(kind, source, show_track):
    timeline = component()
    timeline.buffer.blocks = [Block(kind="assistant", text=source), Block(kind=kind)]
    timeline.buffer.show_track = show_track
    timeline.buffer.active_status = ActiveStatus(kind="generating", started_at=0)
    with patch("logox.tui.content.timeline.time.time", return_value=1):
        assert_full_display(timeline)


@pytest.mark.parametrize("source", [
    "**段落** 中文 `code`\n\n第二段",
    "```python\ndef f():\n    return 1\n```\n\nend",
    "| 名称 | 值 |\n|---|---|\n| 中 | **1** |\n| longer | next |",
])
def test_growing_assistant_rows_match_full_render_including_styles(source):
    timeline = component()
    answer = timeline.buffer.add_assistant("")
    for length in range(len(source) + 1):
        answer.text = source[:length]
        assert_full_display(timeline, width=40)


def test_inline_shrinking_past_physical_top_repaints_without_erasing_history():
    from rich.text import Text

    class Rows:
        lines = [Text(f"row {index}") for index in range(14)]

        def render(self, _width):
            return self.lines

    terminal = FakeTerminal(columns=80, rows=10)
    root = Rows()
    screen = Screen(terminal)
    screen.root = root
    screen.render_now()
    terminal.reset()
    root.lines = root.lines[5:]
    screen.render_now()
    assert "\x1b[H\x1b[0J" in terminal.output
    assert "\x1b[3J" not in terminal.output
    assert screen._previous_viewport_top == 0
    assert "row 5" in terminal.output
