"""Offline frame benchmark. FakeTerminal, synthetic history, no model or user state.

Run from any directory with the repository venv. stdout is JSON; --profile emits
a short cProfile summary to stderr. Timings exclude physical terminal/network IO.
"""

from __future__ import annotations

import argparse
import cProfile
import io
import json
import pstats
import statistics
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from types import SimpleNamespace

from logox.kernel.bus import EventBus
from logox.tui.render.app import InlineApp
from logox.tui.render.fullscreen import FullscreenApp
from logox.tui.render.keys import Key
from logox.tui.render.terminal import FakeTerminal


def _Runtime():
    return SimpleNamespace(
        kernel=None,
        bus=EventBus(session_id="test-inline"),
        model="test-model",
        config=SimpleNamespace(provider=SimpleNamespace(thinking_effort="auto")),
    )


parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("--pairs", type=int, nargs="+", default=[100, 800, 3200])
parser.add_argument("--frames", type=int, default=15)
parser.add_argument("--profile", action="store_true")
args = parser.parse_args()
if args.frames < 1 or any(count < 0 for count in args.pairs):
    parser.error("frames must be positive and history pairs non-negative")

results = []
for count in args.pairs:
    for surface in (InlineApp, FullscreenApp):
        app = surface(runtime=_Runtime(), terminal=FakeTerminal(columns=100, rows=30))
        app.screen.on_defer = lambda *_: None
        for index in range(count):
            app.timeline.buffer.add_user(f"user {index} 中文")
            app.timeline.buffer.add_assistant("**answer** `code` 中文\n\nparagraph")
        app.screen.render_now()
        for scenario in ("typing", "stream"):
            durations = []
            for _ in range(args.frames):
                if scenario == "typing":
                    app.editor.handle_input(Key("x", char="x"))
                else:
                    app.timeline.buffer.add_delta("增量a")
                    app.timeline.step()
                start = time.perf_counter()
                app.screen.render_now()
                durations.append((time.perf_counter() - start) * 1000)
                app.terminal.reset()
            results.append(
                dict(
                    history_pairs=count,
                    mode=surface.__name__,
                    scenario=scenario,
                    median_ms=round(statistics.median(durations), 3),
                    max_ms=round(max(durations), 3),
                )
            )
        if args.profile and count == 800:
            profiler = cProfile.Profile()
            profiler.enable()
            for _ in range(5):
                app.editor.handle_input(Key("y", char="y"))
                app.screen.render_now()
                app.terminal.reset()
            profiler.disable()
            stream = io.StringIO()
            pstats.Stats(profiler, stream=stream).sort_stats("cumtime").print_stats(22)
            print(surface.__name__, stream.getvalue(), file=sys.stderr)
print(json.dumps(results, indent=2))
