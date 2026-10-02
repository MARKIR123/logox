"""流式打字机缓动插值引擎与背压治理测试套件 (D186)."""

from __future__ import annotations

import unittest
from typing import Any

from rich.text import Text

from logox.kernel import events as ev
from logox.tui.content.smoother import StreamSmoother
from logox.tui.content.timeline import Block, TimelineBuffer
from logox.tui.render.app import TimelineComponent
from logox.tui.render.fullscreen import FullscreenLayout
from logox.tui.render.terminal import Terminal


class DummyTerminal(Terminal):
    def __init__(self, cols: int = 80, rows: int = 24) -> None:
        self._cols = cols
        self._rows = rows
        self.writes: list[str] = []

    @property
    def columns(self) -> int:
        return self._cols

    @property
    def rows(self) -> int:
        return self._rows

    def write(self, text: str) -> None:
        self.writes.append(text)


class DummyComponent:
    def render(self, width: int) -> list[Text]:
        return [Text("dummy")]


class DummyStatusComponent:
    def render(self, width: int) -> list[Text]:
        return [Text("status")]


class TestStreamSmoother(unittest.TestCase):
    def test_smoother_single_char_smooth_draining(self) -> None:
        """测试少量积压字符（<= 8）时逐字微步释出，呈现细腻打字机质感。"""
        smoother = StreamSmoother()
        smoother.feed_text("Hello")
        self.assertTrue(smoother.has_pending())
        self.assertEqual(smoother.pending_text_len(), 5)

        drained = []
        for _ in range(5):
            ch = smoother.step_text()
            self.assertEqual(len(ch), 1)
            drained.append(ch)

        self.assertEqual("".join(drained), "Hello")
        self.assertEqual(smoother.step_text(), "")
        self.assertFalse(smoother.has_pending())

    def test_smoother_burst_adaptation_ceiling(self) -> None:
        """突发释放有视觉步长上限，同时完整排空且比逐字更快。"""
        smoother = StreamSmoother()
        original = "x" * 200
        smoother.feed_text(original)

        steps = []
        while smoother.has_pending():
            chunk = smoother.step_text()
            if not chunk:
                break
            steps.append(chunk)

        self.assertGreater(len(steps[0]), 1)
        self.assertTrue(all(len(step) <= 16 for step in steps))
        self.assertLessEqual(len(steps), 40)
        self.assertEqual("".join(steps), original)

    def test_smoother_flush_all_instant(self) -> None:
        """测试 flush_all 毫秒级瞬时排空所有缓冲区，零延迟对齐终态。"""
        smoother = StreamSmoother()
        smoother.feed_text("Residual text")
        smoother.feed_reasoning("Residual reasoning")
        self.assertTrue(smoother.has_pending())

        text, reasoning = smoother.flush_all()
        self.assertEqual(text, "Residual text")
        self.assertEqual(reasoning, "Residual reasoning")
        self.assertFalse(smoother.has_pending())
        self.assertEqual(smoother.step_text(), "")
        self.assertEqual(smoother.step_reasoning(), "")

    def test_smoother_cjk_multi_byte_safety(self) -> None:
        """测试中文全角字符、特殊标点与 Emoji 字符切片零乱码、零截断。"""
        smoother = StreamSmoother()
        cjk_text = "你好，世界！🚀 这是一个打字机测试。🌟✨"
        smoother.feed_text(cjk_text)

        chunks = []
        while smoother.has_pending():
            chunk = smoother.step_text()
            chunks.append(chunk)

        self.assertEqual("".join(chunks), cjk_text)

    def test_smoother_reasoning_and_text_dual_channels(self) -> None:
        """测试思考推理与正文双通道独立缓冲与平滑步进。"""
        smoother = StreamSmoother()
        smoother.feed_reasoning("Thinking...")
        smoother.feed_text("Answer...")

        self.assertEqual(smoother.pending_reasoning_len(), 11)
        self.assertEqual(smoother.pending_text_len(), 9)

        r_step = smoother.step_reasoning()
        t_step = smoother.step_text()
        self.assertTrue(len(r_step) > 0)
        self.assertTrue(len(t_step) > 0)

        # 剩余排空
        r_rest = smoother.flush_all_reasoning()
        t_rest = smoother.flush_all_text()
        self.assertEqual(r_step + r_rest, "Thinking...")
        self.assertEqual(t_step + t_rest, "Answer...")


class TestTimelineBufferSmootherIntegration(unittest.TestCase):
    def test_timeline_buffer_smooth_stepping_and_flush(self) -> None:
        """验证 TimelineBuffer 与 StreamSmoother 的集成：微步落块与终态排空。"""
        buf = TimelineBuffer()
        buf.add_delta("Python")

        self.assertTrue(buf.has_pending())
        # 首次 step 产生第 1 个字符
        blk1 = buf.step_delta()
        self.assertIsNotNone(blk1)
        self.assertEqual(len(buf.blocks), 1)
        self.assertEqual(buf.blocks[-1].text, "P")

        # 后续 step 续写该块
        blk2 = buf.step_delta()
        self.assertIsNotNone(blk2)
        self.assertEqual(len(buf.blocks), 1)
        self.assertEqual(buf.blocks[-1].text, "Py")

        # flush 瞬间排空剩余全部字符
        buf.flush_delta()
        self.assertEqual(buf.blocks[-1].text, "Python")
        self.assertFalse(buf.smoother.has_pending())

    def test_timeline_buffer_reasoning_stepping_and_flush(self) -> None:
        """验证推理思考链与 StreamSmoother 的集成。"""
        buf = TimelineBuffer()
        buf.add_reasoning_delta("Plan A")

        buf.step_reasoning()
        self.assertIsNotNone(buf._last_of("reasoning"))

        buf.flush_reasoning()
        r_blk = buf._last_of("reasoning")
        self.assertIsNotNone(r_blk)
        self.assertEqual(r_blk.text, "Plan A")


class TestFullscreenSinglePassRender(unittest.TestCase):
    def test_fullscreen_single_pass_no_cache_thrashing(self) -> None:
        """验证全屏模式在常规单屏或多屏状态下仅执行单次确定性渲染，不击穿缓存。"""
        from logox.tui.theme import load_theme

        term = DummyTerminal(cols=80, rows=24)
        palette = load_theme("logox-dark").palette
        timeline = TimelineComponent(palette=palette)
        editor = DummyComponent()
        status = DummyStatusComponent()
        layout = FullscreenLayout(term, timeline, editor, status)

        # 添加几条消息（小于一屏 24 行）
        timeline.buffer.add_user("Hello")
        timeline.buffer.add_assistant("Hi there!")

        # 首帧：无滚动条，以 width 渲染
        layout.render(80)
        self.assertFalse(layout._show_scrollbar)
        initial_invalidations = timeline.cache_invalidations

        # 第二帧：未满一屏，继续以 width 渲染，不应发生因宽度切换引起的额外 cache_invalidations
        layout.render(80)
        self.assertEqual(timeline.cache_invalidations, initial_invalidations)


class TestStreamTickerIntegration(unittest.TestCase):
    def test_timeline_step_method(self) -> None:
        """验证 TimelineComponent.step() 能正确驱动增量落块并返回布尔状态。"""
        tl = TimelineComponent(palette=None)
        tl.buffer.add_delta("Logox")

        committed = tl.step()
        self.assertTrue(committed)
        self.assertEqual(tl.buffer.blocks[-1].text, "L")

        # flush 提交剩余
        tl.flush()
        self.assertEqual(tl.buffer.blocks[-1].text, "Logox")
        # 再次 step 无新文本
        self.assertFalse(tl.step())


if __name__ == "__main__":
    unittest.main()
