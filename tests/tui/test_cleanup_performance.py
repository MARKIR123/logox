"""No-change work, invalidation, cursor ownership and whole-turn interaction guards."""

from __future__ import annotations

import asyncio
import random
from unittest.mock import patch

import pytest
from rich.text import Text

from logox.kernel import events as ev
from logox.tui.content.anamnesis import AnamesisCard
from logox.tui.content.cards import CardContext
from logox.tui.content.smoother import StreamSmoother
from logox.tui.content.timeline import Block, RenderResult, TimelineRenderCache, render_blocks, render_cached
from logox.tui.render.ansi import split_styled_lines, text_to_ansi
from logox.tui.render.app import InlineApp, TimelineComponent
from logox.tui.render.component import fit_lines
from logox.tui.render.components.editor import Editor
from logox.tui.render.components.text import CURSOR_MARKER
from logox.tui.render.fullscreen import FullscreenApp
from logox.tui.render.keys import Key
from logox.tui.render.screen import Screen
from logox.tui.render.terminal import FakeTerminal
from logox.tui.theme import load_theme
from tests.tui.test_render_inline_app import _Runtime


def ansi(rows):
    return [text_to_ansi(row) for row in rows]


def context(width=60):
    return CardContext(palette=load_theme().palette, width=width)


def test_unchanged_timeline_reuses_segments_ranges_and_flat_rows():
    timeline = TimelineComponent(load_theme().palette)
    for index in range(100):
        timeline.buffer.add_user(str(index))
        timeline.buffer.add_assistant("**中文** paragraph")
    first = timeline.render(60)
    segments, ranges = timeline._cache.segments, timeline.block_ranges
    assert timeline.render(60) is first
    assert timeline._cache.segments is segments
    assert timeline.block_ranges is ranges
    timeline.buffer.blocks[2].text = "CHANGED_OLD_CARD"
    assert "CHANGED_OLD_CARD" in "\n".join(row.plain for row in timeline.render(60))
    assert timeline._cache.segments is not segments
    assert ansi(timeline.render(60)) == ansi(
        split_styled_lines(render_blocks(timeline.buffer.blocks, context()))
    )


@pytest.mark.parametrize("segmented", [False, True])
def test_cached_display_matches_full_across_structural_and_in_place_changes(segmented):
    rng = random.Random(103)
    blocks = [Block(kind="user", text="起点"), Block(kind="assistant", text="**中文** answer")]
    cache = TimelineRenderCache()
    for _ in range(100):
        op = rng.randrange(5)
        if op == 0 or not blocks:
            blocks.append(Block(kind=rng.choice(["user", "assistant", "notice"]), text="新增 中文 `code`"))
        elif op == 1:
            blocks[rng.randrange(len(blocks))].text += "\nchange"
        elif op == 2:
            del blocks[rng.randrange(len(blocks))]
        elif op == 3:
            blocks.reverse()
        else:
            blocks[rng.randrange(len(blocks))] = Block(kind="reasoning", text="thought", state="ok")
        width = rng.choice([15, 60])
        ctx = context(width)
        expand = rng.choice([True, False])
        cached = render_cached(blocks, cache, context=ctx, expand_reasoning=expand, segmented=segmented)
        full = render_blocks(blocks, ctx, expand_reasoning=expand)
        assert ansi(split_styled_lines(cached.text)) == ansi(split_styled_lines(full))
        assert len(cache.entries) == len(blocks)
        assert len(cache.segments) == len(blocks)
        assert cache.line_offsets[-1] == cache.newline_count
    render_cached([], cache, context=context(), segmented=segmented)
    assert not cache.blocks and not cache.entries and not cache.segments and not cache.markdown


def test_segmented_and_compatibility_calls_can_share_cache():
    blocks = [Block(kind="assistant", text="paragraph\n\n**中文**")]
    cache = TimelineRenderCache()
    for segmented in (True, False, True, False):
        result = render_cached(blocks, cache, context=context(), segmented=segmented)
        assert ansi(split_styled_lines(result.text)) == ansi(
            split_styled_lines(render_blocks(blocks, context()))
        )


def test_empty_segment_stays_empty_on_cache_hit():
    timeline = TimelineComponent(load_theme().palette)
    result = RenderResult(segments=[(None, Text())])
    assert timeline._split_rows(result) == []
    assert timeline._split_rows(result) == []


def test_fit_cache_checks_only_new_rows_and_resize():
    screen = Screen(FakeTerminal())
    rows = [Text("中文 **"), Text("old")]
    with patch("logox.tui.render.screen.fit_lines", wraps=fit_lines) as checked:
        assert ansi(screen._fit_rows(rows, 4)) == ansi(fit_lines(rows, 4))
        assert checked.call_count == 2
        screen._fit_rows(list(rows), 4)
        assert checked.call_count == 2
        screen._fit_rows([rows[0], Text("new")], 4)
        assert checked.call_count == 3
        screen._fit_rows(rows, 5)
        assert checked.call_count == 5
    screen._fit_rows([], 5)
    assert screen._fit_sources == screen._fit_pieces == []


@pytest.mark.parametrize("width", [1, 3, 6, 20])
def test_fit_cache_preserves_styles_and_never_mutates_source(width):
    screen = Screen(FakeTerminal())
    row = Text("中文abcdef", style="bold on blue")
    row.stylize("red", 1, 5)
    expected = ansi(fit_lines([row], width))
    before = text_to_ansi(row)
    assert ansi(screen._fit_rows([row], width)) == expected
    assert ansi(screen._fit_rows([row], width)) == expected
    assert text_to_ansi(row) == before


def test_fit_cache_does_not_lose_reused_ime_cursor_marker():
    screen = Screen(FakeTerminal())
    row = Text("中文" + CURSOR_MARKER + "input")
    for _ in range(3):
        output = screen._fit_rows([row], 50)
        assert screen._extract_cursor(output, 20) == (0, 4)
        assert CURSOR_MARKER not in output[0].plain
        assert CURSOR_MARKER in row.plain


def test_scrollbar_reuses_unchanged_visible_rows_and_invalidates_style():
    app = FullscreenApp(runtime=_Runtime(), terminal=FakeTerminal(columns=60, rows=20))
    for index in range(50):
        app.timeline.buffer.add_user(f"user {index}")
        app.timeline.buffer.add_assistant("answer")
    app.screen.on_defer = lambda *_: None
    app.screen.render_now()
    previous = list(app.root._scrollbar_rows)
    misses = app.screen.stats["ansi_misses"]
    app.editor.handle_input(Key("x", char="x"))
    app.screen.render_now()
    assert all(old[4] is new[4] for old, new in zip(previous, app.root._scrollbar_rows, strict=True))
    assert app.screen.stats["ansi_misses"] - misses < app.terminal.rows // 2
    old_ansi = ansi([entry[4] for entry in previous])
    app.apply_theme("logox-light")
    app.screen.render_now()
    assert ansi([entry[4] for entry in app.root._scrollbar_rows]) != old_ansi
    app.clear_timeline()
    app.screen.render_now()
    assert not app.root._scrollbar_rows and app.root._mark_ranges is None


def test_completion_leaves_history_browsing_and_preserves_accepted_draft():
    editor = Editor()
    editor.history = ["/mo"]
    editor.set_text("original draft")
    editor.handle_input(Key("up"))
    assert editor.text == "/mo"
    assert editor.apply_completion("/model ")
    editor.handle_input(Key("down"))
    assert editor.text == "/model "
    editor.handle_input(Key("up"))
    editor.handle_input(Key("down"))
    assert editor.text == "/model "


@pytest.mark.parametrize("surface", [InlineApp, FullscreenApp])
@pytest.mark.parametrize("reason", ["completed", "error", "cancelled"])
def test_busy_is_whole_turn_not_single_request(surface, reason):
    async def check():
        runtime = _Runtime()
        runtime.reload_resources = lambda: (_ for _ in ()).throw(AssertionError("busy reload must not run"))
        app = surface(runtime=runtime, terminal=FakeTerminal())
        app.screen.on_defer = lambda *_: None
        await app._on_event(
            ev.ModelRequestStarted(
                session_id="test-inline", provider="mock", model="mock", token_estimate=1, request_index=0
            )
        )
        await app._on_event(
            ev.ModelRequestFinished(
                session_id="test-inline", usage=ev.Usage(input_tokens=0, output_tokens=0), duration_ms=1
            )
        )
        assert app.busy
        await app.commands._cmd_reload("")
        assert "等这一轮结束" in app.frame_text()
        await app._on_event(ev.ToolCallRequested(session_id="test-inline", call_id="new", name="read"))
        assert app.busy
        await app._on_event(
            ev.TurnFinished(
                session_id="test-inline",
                turn_index=1,
                duration_ms=1,
                tool_call_count=1,
                usage=ev.Usage(input_tokens=0, output_tokens=0),
                reason=reason,
            )
        )
        assert not app.busy
        await runtime.bus.aclose()

    asyncio.run(check())


def test_large_smoother_backlog_does_not_copy_unconsumed_head():
    source = "中文a" * 10000
    smoother = StreamSmoother()
    smoother.feed_text(source)
    prefix = []
    for _ in range(100):
        value = smoother.step_text()
        prefix.append(value)
        assert len(value) <= 16
        assert smoother._text.chunks[0] is source
    assert smoother.pending_text_len() == len(source) - sum(map(len, prefix))
    assert "".join(prefix) + smoother.flush_all_text() == source
    assert not smoother.has_pending()


def test_smoother_fragment_boundaries_interleave_flush_and_clear():
    smoother = StreamSmoother()
    expected, received = "", ""
    for fragment in ("中文a" * 30, "🚀b", "", "next" * 15):
        expected += fragment
        smoother.feed_text(fragment)
        smoother.feed_reasoning(fragment)
        received += smoother.step_text()
        assert smoother.pending_text_len() == len(expected) - len(received)
    assert received + smoother.flush_all_text() == expected
    assert smoother.flush_all_reasoning() == expected
    smoother.feed_text("old")
    smoother.feed_reasoning("old thought")
    smoother.clear()
    smoother.feed_text("new")
    assert smoother.flush_all() == ("new", "")


@pytest.mark.parametrize("surface", [InlineApp, FullscreenApp])
@pytest.mark.parametrize("key, expected_kinds", [("o", {"tool", "diff"}), ("t", {"reasoning", "anamnesis"})])
def test_expand_does_not_relayout_unaffected_history(surface, key, expected_kinds):
    app = surface(runtime=_Runtime(), terminal=FakeTerminal(columns=60, rows=20))
    app.screen.on_defer = lambda *_: None
    for index in range(30):
        app.timeline.buffer.add_user(f"user {index}")
        app.timeline.buffer.add_assistant("**answer** 中文")
    app.timeline.buffer.add_reasoning("thought")
    app.timeline.buffer.start_tool(call_id="tool", name="read", args_text="{}")
    app.timeline.buffer.blocks.append(Block(kind="anamnesis", card=AnamesisCard("run", phase="completed")))
    app.screen.render_now()
    touched = []

    def count(blocks, *args, **kwargs):
        touched.extend(block.kind for block in blocks)
        return render_blocks(blocks, *args, **kwargs)

    with patch("logox.tui.content.timeline.render_blocks", count):
        app._dispatch(Key(key, ctrl=True))
        app.screen.render_now()
    assert touched and set(touched) <= expected_kinds
    ctx = CardContext(
        palette=app.timeline.palette, width=60 if surface is InlineApp else 59, glyphs=app.timeline.glyphs
    )
    full = render_blocks(
        app.timeline.buffer.blocks,
        ctx,
        expand_tools=app.timeline.buffer.expand_tools,
        expand_reasoning=app.timeline.buffer.expand_reasoning,
    )
    assert ansi(app.timeline.render(ctx.width)) == ansi(split_styled_lines(full))


def test_replacing_card_at_same_revision_invalidates_cached_display():
    timeline = TimelineComponent(load_theme().palette)
    block = Block(kind="anamnesis", card=AnamesisCard("run", phase="completed", revision=7))
    timeline.buffer.blocks.append(block)
    first = ansi(timeline.render(60))
    block.card = AnamesisCard("run", phase="failed", reason="new failure", revision=7)
    assert ansi(timeline.render(60)) != first
    assert "new failure" in "\n".join(row.plain for row in timeline.render(60))


def test_changing_assistant_kind_releases_its_markdown_cache():
    timeline = TimelineComponent(load_theme().palette)
    block = timeline.buffer.add_assistant("**old**")
    timeline.render(60)
    assert timeline._cache.markdown
    block.kind = "notice"
    timeline.render(60)
    assert not timeline._cache.markdown
