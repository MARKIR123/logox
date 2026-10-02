"""Check live rows AND preserved history under Windows Terminal's ED(2) semantics."""

import pytest

from logox.tui.render.keys import Key
from logox.tui.render.terminal import FakeTerminal, PosixTerminal, Win32Terminal

from .screen_emulator import ScreenEmulator
from .test_render_inline_app import make_app


def paint(app, terminal, emulator):
    app.screen.render_now()
    for data, _, _ in terminal.writes:
        emulator.feed(data)
    terminal.reset()
    screen = app.screen
    rows = screen.frame_text().split("\n")
    expected = rows[screen._previous_viewport_top : screen._previous_viewport_top + terminal.rows]
    assert emulator.visible == [row.rstrip() for row in expected] + [""] * (terminal.rows - len(expected))
    assert emulator.cursor_row == screen._hardware_cursor_row - screen._previous_viewport_top


def app_with_card(kind, long_tail=False):
    app, terminal, _ = make_app(80, 18)
    buffer = app.timeline.buffer
    buffer.add_user("Inspect this project")
    if kind == "reasoning":
        buffer.add_reasoning("\n".join(f"reason {i}" for i in range(45)))
        buffer.expand_reasoning = True
    else:
        buffer.start_tool(call_id="read", name="read", args_text='{"path": "app.py"}')
        buffer.finish_tool(
            call_id="read", ok=True, duration_ms=1, payload="\n".join(f"output {i}" for i in range(45))
        )
        buffer.expand_tools = True
    buffer.add_assistant("\n\n".join(f"Answer paragraph {i}" for i in range(30 if long_tail else 1)))
    app.timeline.invalidate()
    emulator = ScreenEmulator(80, 18, erase_all_to_scrollback=True)
    paint(app, terminal, emulator)
    return app, terminal, emulator


@pytest.mark.parametrize("kind", ["reasoning", "tool"])
@pytest.mark.parametrize("long_tail", [False, True])
def test_card_relayout_does_not_archive_old_editor_and_status(kind, long_tail):
    app, terminal, emulator = app_with_card(kind, long_tail)
    saved_history = list(emulator.scrollback)
    # Growth can legitimately scroll new card rows; repaint must never archive the dock.
    for _ in range(4):
        before = (
            app.timeline.buffer.expand_reasoning if kind == "reasoning" else app.timeline.buffer.expand_tools
        )
        app.press(Key("t" if kind == "reasoning" else "o", ctrl=True))
        after = (
            app.timeline.buffer.expand_reasoning if kind == "reasoning" else app.timeline.buffer.expand_tools
        )
        assert before != after
        paint(app, terminal, emulator)
        assert emulator.scrollback[: len(saved_history)] == saved_history
        added = emulator.scrollback[len(saved_history) :]
        assert not any("test-model" in row or row.startswith(("╭", "╰")) for row in added)
        if long_tail:
            assert emulator.scrollback == saved_history
    # Hardware cursor tracking must still work on the following incremental frame.
    history_after_toggles = list(emulator.scrollback)
    app.press("x")
    paint(app, terminal, emulator)
    assert emulator.scrollback == history_after_toggles
    assert "x" in app.editor.text


@pytest.mark.parametrize("dimensions", [(63, 18), (80, 24), (80, 12)])
def test_resize_repaint_does_not_archive_the_previous_page(dimensions):
    app, terminal, emulator = app_with_card("tool", long_tail=True)
    saved_history = list(emulator.scrollback)
    width, height = dimensions
    # Retain stale cells across resize. Full terminal reflow is deliberately not modeled.
    old_grid = emulator.grid
    emulator.columns, emulator.rows = width, height
    emulator.grid = [(row[:width] + [" "] * width)[:width] for row in old_grid[:height]]
    emulator.grid += [[" "] * width for _ in range(height - len(emulator.grid))]
    emulator.cursor_row = min(emulator.cursor_row, height - 1)
    emulator.cursor_col = min(emulator.cursor_col, width - 1)
    terminal.resize(width, height)
    paint(app, terminal, emulator)
    assert emulator.scrollback == saved_history
    app.press("x")
    paint(app, terminal, emulator)
    assert emulator.scrollback == saved_history


@pytest.mark.parametrize("driver", [FakeTerminal, PosixTerminal, Win32Terminal])
def test_terminal_clear_erases_in_place_without_archiving_or_losing_history(driver):
    # Invoke the real driver's clear method without acquiring native console handles.
    terminal = FakeTerminal(columns=20, rows=4)
    emulator = ScreenEmulator(20, 4, erase_all_to_scrollback=True)
    emulator.scrollback = ["older conversation"]
    emulator.feed("old answer\r\nold editor\r\nold status")
    driver.clear(terminal)
    emulator.feed(terminal.output)
    assert emulator.visible == [""] * 4
    assert emulator.cursor_row == emulator.cursor_col == 0
    assert emulator.scrollback == ["older conversation"]


def test_emulator_distinguishes_erase_all_from_in_place_erase():
    emulator = ScreenEmulator(20, 4, erase_all_to_scrollback=True)
    emulator.feed("answer\r\neditor\r\nstatus\x1b[2J\x1b[H")
    assert emulator.scrollback == ["answer", "editor", "status"]
    emulator.feed("new answer\r\nnew editor\x1b[H\x1b[0J")
    assert emulator.visible == [""] * 4
    assert emulator.scrollback == ["answer", "editor", "status"]
