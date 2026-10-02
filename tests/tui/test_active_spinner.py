"""动态动子与状态耗时指示器测试（方案 A，Braille Spinner + ActiveStatus + Timer）。

验证短暂状态与持久历史解耦、状态机事件流转、纯函数算帧以及缓存命中不变量。
"""

import time
import unittest
from unittest.mock import MagicMock

from rich.text import Text

from logox.kernel import events as ev
from logox.tui.content.cards import CardContext
from logox.tui.content.timeline import (
    SPINNER_FRAMES,
    ActiveStatus,
    Block,
    TimelineBuffer,
    format_timer,
    render_active_spinner,
    render_blocks,
    render_cached,
)
from logox.tui.render.app import TimelineComponent
from logox.tui.theme import load_theme

PALETTE = load_theme("logox-dark").palette
WIDTH = 80


class ActiveSpinnerPureFunctionTests(unittest.TestCase):
    """纯函数帧与文本计算测试。"""

    def test_format_timer_less_than_minute(self) -> None:
        self.assertEqual(format_timer(0.0), "0.0s")
        self.assertEqual(format_timer(1.23), "1.2s")
        self.assertEqual(format_timer(9.99), "10.0s")
        self.assertEqual(format_timer(59.9), "59.9s")

    def test_format_timer_over_minute(self) -> None:
        self.assertEqual(format_timer(60.0), "1m00s")
        self.assertEqual(format_timer(65.4), "1m05s")
        self.assertEqual(format_timer(125.0), "2m05s")

    def test_spinner_frames_rotation(self) -> None:
        """验证随时间递增，盲文动子帧按顺序顺时针旋转。"""
        status = ActiveStatus(kind="thinking", started_at=0.0)
        chars = []
        for i in range(len(SPINNER_FRAMES)):
            # 10 fps: 0.1s 一帧
            now = i * 0.1
            rendered = render_active_spinner(status, PALETTE, now=now).plain
            frame_char = SPINNER_FRAMES[i]
            self.assertIn(frame_char, rendered)
            chars.append(frame_char)
        self.assertEqual(tuple(chars), SPINNER_FRAMES)

    def test_spinner_labels_by_kind(self) -> None:
        """验证不同阶段类型的默认文案。"""
        now = 100.0

        # 思考
        s1 = ActiveStatus(kind="thinking", started_at=100.0)
        t1 = render_active_spinner(s1, PALETTE, now=now).plain
        self.assertIn("正在思考", t1)
        self.assertIn("(0.0s)", t1)

        # 推理
        s2 = ActiveStatus(kind="reasoning", started_at=98.5)
        t2 = render_active_spinner(s2, PALETTE, now=now).plain
        self.assertIn("思考中", t2)
        self.assertIn("(1.5s)", t2)

        # 生成
        s3 = ActiveStatus(kind="generating", started_at=97.0)
        t3 = render_active_spinner(s3, PALETTE, now=now).plain
        self.assertIn("正在生成", t3)
        self.assertIn("(3.0s)", t3)

        # 工具调用
        s4 = ActiveStatus(kind="tool", tool_name="shell", started_at=95.0)
        t4 = render_active_spinner(s4, PALETTE, now=now).plain
        self.assertIn("执行工具 shell", t4)
        self.assertIn("(5.0s)", t4)

    def test_header_injection_when_no_prior_model_block(self) -> None:
        """若没有前序模型块，主动补齐 ✦ Logox 角色头部。"""
        status = ActiveStatus(kind="thinking", started_at=0.0)
        without_header = render_active_spinner(status, PALETTE, has_model_header=False, now=0.0).plain
        with_header = render_active_spinner(status, PALETTE, has_model_header=True, now=0.0).plain

        self.assertTrue(without_header.startswith("✦ Logox\n▎ "))
        self.assertTrue(with_header.startswith("▎ "))
        self.assertNotIn("✦ Logox", with_header)


class RenderBlocksSpinnerIntegrationTests(unittest.TestCase):
    """验证 render_blocks 在各类块序列尾部附着动子的连贯性。"""

    def setUp(self) -> None:
        self.context = CardContext(palette=PALETTE, width=WIDTH)

    def test_empty_blocks_renders_spinner_with_role_header(self) -> None:
        status = ActiveStatus(kind="thinking", started_at=10.0)
        out = render_blocks([], self.context, active_status=status, now=10.5).plain
        self.assertIn("✦ Logox\n▎ ", out)
        self.assertIn("正在思考 (0.5s)", out)

    def test_after_user_block_renders_spinner_with_role_header(self) -> None:
        blocks = [Block(kind="user", text="帮我写一个测试")]
        status = ActiveStatus(kind="thinking", started_at=10.0)
        out = render_blocks(blocks, self.context, active_status=status, now=10.8).plain
        self.assertIn("▌ 帮我写一个测试", out)
        self.assertIn("✦ Logox\n▎ ", out)
        self.assertIn("正在思考 (0.8s)", out)

    def test_after_assistant_block_connects_green_rail_seamlessly(self) -> None:
        blocks = [Block(kind="assistant", text="这是第一句回答。")]
        status = ActiveStatus(kind="generating", started_at=10.0)
        out = render_blocks(blocks, self.context, active_status=status, now=11.2).plain
        self.assertIn("✦ Logox\n", out)
        self.assertIn("▎ 这是第一句回答。", out)
        # 验证双轨是紧密相连的，没有脱节的无前缀空行
        self.assertIn("▎ 这是第一句回答。\n▎ ", out)
        self.assertIn("正在生成 (1.2s)", out)


class TimelineBufferStateMachineTests(unittest.TestCase):
    """验证 TimelineBuffer 摄入各事件生命周期时 active_status 的状态流转。"""

    def test_event_lifecycle_transitions(self) -> None:
        buf = TimelineBuffer()
        self.assertIsNone(buf.active_status)

        # 1. 用户输入
        buf.ingest(ev.UserPromptSubmit(session_id="s1", text="你好", text_chars=2))
        self.assertIsNone(buf.active_status)
        self.assertEqual(len(buf.blocks), 1)

        # 2. 模型请求开始
        buf.ingest(
            ev.ModelRequestStarted(
                session_id="s1",
                provider="mock",
                model="test-model",
                token_estimate=100,
                request_index=0,
            )
        )
        self.assertIsNotNone(buf.active_status)
        assert buf.active_status is not None
        self.assertEqual(buf.active_status.kind, "thinking")
        self.assertEqual(buf.active_status.label, "正在思考")

        # 3. 收到推理片段
        buf.ingest(ev.ModelDelta(session_id="s1", kind="reasoning", delta="思考中...", request_index=0))
        self.assertEqual(buf.active_status.kind, "reasoning")
        self.assertEqual(buf.active_status.label, "思考中")

        # 4. 收到正文片段
        buf.ingest(ev.ModelDelta(session_id="s1", kind="text", delta="你好！", request_index=0))
        self.assertEqual(buf.active_status.kind, "generating")
        self.assertEqual(buf.active_status.label, "正在生成")

        # 5. 模型请求工具调用
        buf.ingest(
            ev.ToolCallRequested(
                session_id="s1",
                call_id="call-1",
                name="shell",
                args={"cmd": "ls"},
            )
        )
        self.assertEqual(buf.active_status.kind, "tool")
        self.assertIn("shell", buf.active_status.label)
        self.assertEqual(buf.active_status.tool_name, "shell")

        # 6. 工具调用完成
        buf.ingest(
            ev.ToolCallFinished(
                session_id="s1",
                call_id="call-1",
                ok=True,
                duration_ms=500,
            )
        )
        self.assertEqual(buf.active_status.kind, "thinking")
        self.assertEqual(buf.active_status.label, "处理结果中")

        # 7. 回合结束
        buf.ingest(
            ev.TurnFinished(
                session_id="s1",
                turn_index=1,
                duration_ms=1500,
                tool_call_count=1,
                usage=ev.Usage(input_tokens=10, output_tokens=20),
                reason="completed",
            )
        )
        self.assertIsNone(buf.active_status)

    def test_error_clears_active_status(self) -> None:
        buf = TimelineBuffer()
        buf.active_status = ActiveStatus(kind="thinking")
        buf.ingest(ev.ErrorOccurred(session_id="s1", category="network", message="timeout"))
        self.assertIsNone(buf.active_status)

    def test_add_user_clears_active_status(self) -> None:
        buf = TimelineBuffer()
        buf.active_status = ActiveStatus(kind="generating")
        buf.add_user("hello")
        self.assertIsNone(buf.active_status)


class TimelineComponentCacheInvarianceTests(unittest.TestCase):
    """验证动子刷新不破坏前缀缓存命中（D56 不变量）。"""

    def test_spinner_ticks_hit_prefix_cache(self) -> None:
        component = TimelineComponent(PALETTE)
        # 填充若干持久块
        component.buffer.add_user("问题一")
        component.buffer.add_assistant("回答一")
        component.buffer.add_user("问题二")
        component.buffer.add_assistant("回答二")

        # 启动活跃状态
        component.buffer.active_status = ActiveStatus(kind="generating", started_at=10.0)
        self.assertTrue(component.is_active)

        # 第 1 帧渲染：建立缓存
        r1 = component.render(WIDTH)
        self.assertTrue(any("正在生成" in line.plain for line in r1))

        # 模拟 5 个动子 tick 周期（每个周期时间前进 0.1s）
        prev_hits = component.prefix_hits
        for i in range(1, 6):
            r = component.render(WIDTH)
            self.assertTrue(any("正在生成" in line.plain for line in r))

        # 缓存必须持续命中，除最后一块外的历史块无需重算
        self.assertGreater(component.prefix_hits, prev_hits)

        # 回合结束：清除活跃状态
        component.buffer.clear_active_status()
        self.assertFalse(component.is_active)
        r_final = component.render(WIDTH)
        self.assertFalse(any("正在生成" in line.plain for line in r_final))


if __name__ == "__main__":
    unittest.main()
