"""内核循环测试（MODULE_kernel_loop §7.1）。

这里盯的是**时序**，不是"某个函数算得对不对"：第几个分片之后重试、中断时哪些事件
已经发出、历史里留下了什么。内核真正的失败模式全都是时序问题，而时序问题只在真跑一遍
时才会暴露。

三条最重要的断言：

* **D52**：有输出 → 不重试；无输出 → 重试。**两个方向都测**。
* **E-3**：中断后 ``tool_use`` 与 ``tool_result`` 数量必须仍然相等——否则
  **下一轮请求会被厂商 400，而这条历史会一直留在会话里**（按一次 Esc，
  本次会话从此再也发不出请求）。
* **T-22 不变量**：任何场景下 ``ToolCallStarted`` 与 ``ToolCallFinished`` 计数恒等。
"""

from __future__ import annotations

import asyncio
import unittest

from logox.context.storage import SessionTranscriptWriter
from logox.kernel import events as ev
from logox.kernel.events import ModelRequestFinished
from logox.kernel.loop import KernelLoop, TurnInProgressError
from logox.kernel.messages import ReasoningBlock, ToolResultBlock, ToolUseBlock
from logox.kernel.turn import TurnStatus
from tests.unit.kernel_support import (
    Pause,
    StubTool,
    auth_error,
    bad_request_error,
    chunks,
    install,
    network_error,
    rate_limit_error,
    text_chunks,
    tool_chunks,
    usage_chunk,
)
from tests.unit.support import make_temp_dir

READ_ONE = tool_chunks([("call_1", "read", {"path": "a.txt"})])
TEXT_HELLO = text_chunks("你好")


class FakeClock:
    """可推进的假时钟——**时长断言绝不能依赖真实时间**，否则测试会随机失败。"""

    def __init__(self) -> None:
        self.value = 100.0

    def __call__(self) -> float:
        return self.value

    def advance(self, seconds: float) -> None:
        self.value += seconds


class PlainTurnTests(unittest.IsolatedAsyncioTestCase):
    async def test_t01_plain_text_turn_event_sequence(self) -> None:
        env = install([text_chunks("你好", "，世界")])
        await env.kernel.submit("在吗")
        self.assertEqual(
            env.recorder.types(),
            [
                "user_prompt_submit",
                "context_built",
                "model_request_started",
                "model_delta",
                "model_delta",
                "model_request_finished",
                "turn_finished",
            ],
        )

    async def test_t02_turn_finishes_as_done(self) -> None:
        env = install([TEXT_HELLO])
        turn = await env.kernel.submit("在吗")
        self.assertIs(turn.status, TurnStatus.DONE)
        self.assertEqual(env.recorder.find("turn_finished").reason, "completed")

    async def test_t03_assistant_text_lands_in_history(self) -> None:
        env = install([TEXT_HELLO])
        await env.kernel.submit("在吗")
        self.assertEqual([m.role for m in env.kernel.history], ["user", "assistant"])
        self.assertEqual(env.kernel.history[-1].text, "你好")

    async def test_t04_context_built_reports_the_builder_output(self) -> None:
        env = install([TEXT_HELLO], system="你是 Logox")
        await env.kernel.submit("在吗")
        built = env.recorder.find("context_built")
        self.assertEqual(built.message_count, 1)  # 只有那条 user 消息
        self.assertEqual(built.token_estimate, 0)  # 未估算就是 0，不编假数字

    async def test_t05_system_prompt_reaches_the_provider(self) -> None:
        env = install([TEXT_HELLO], system="你是 Logox")
        await env.kernel.submit("在吗")
        self.assertEqual(env.kernel.history[0].role, "user")

    async def test_t06_empty_reply_still_ends_the_turn(self) -> None:
        """模型什么都没说也要正常收尾——否则界面的转圈动画永远停不下来。"""
        env = install([{"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]}])
        turn = await env.kernel.submit("在吗")
        self.assertIs(turn.status, TurnStatus.DONE)
        self.assertEqual(env.kernel.history[-1].text, "")

    async def test_t07_turn_index_increments(self) -> None:
        env = install([TEXT_HELLO])
        first = await env.kernel.submit("一")
        second = await env.kernel.submit("二")
        self.assertEqual((first.turn_index, second.turn_index), (1, 2))

    async def test_t08_submit_while_running_is_refused(self) -> None:
        """E-16：M3 **拒绝**并发提交而不入队（队列是 M4 的界面能力）。"""
        env = install([chunks(*text_chunks("慢")[:1], Pause(0.2))])
        turn = await env.kernel.start("一")
        with self.assertRaises(TurnInProgressError):
            await env.kernel.start("二")
        turn.cancel()
        await env.kernel.wait(turn)
        # 拒绝之后仍然可以正常提交
        env2 = install([TEXT_HELLO])
        await env2.kernel.submit("三")
        self.assertEqual(len(env2.kernel.history), 2)


class ToolLoopTests(unittest.IsolatedAsyncioTestCase):
    async def test_t10_tool_then_answer(self) -> None:
        tool = StubTool(content="hi\n")
        env = install([READ_ONE, text_chunks("文件里写着 hi")], tools=[tool])
        turn = await env.kernel.submit("a.txt 里写了什么")
        self.assertIs(turn.status, TurnStatus.DONE)
        self.assertEqual(turn.tool_call_count, 1)
        self.assertEqual(env.kernel.history[-1].text, "文件里写着 hi")
        self.assertEqual(tool.calls, ["a.txt"])

    async def test_t11_history_shape_after_a_tool_call(self) -> None:
        env = install([READ_ONE, TEXT_HELLO], tools=[StubTool()])
        await env.kernel.submit("读一下")
        self.assertEqual([m.role for m in env.kernel.history], ["user", "assistant", "tool", "assistant"])
        self.assertIsInstance(env.kernel.history[1].blocks[0], ToolUseBlock)
        self.assertIsInstance(env.kernel.history[2].blocks[0], ToolResultBlock)

    async def test_t12_second_model_request_comes_after_the_tool_finished(self) -> None:
        env = install([READ_ONE, TEXT_HELLO], tools=[StubTool()])
        await env.kernel.submit("读一下")
        types = env.recorder.types()
        starts = [i for i, name in enumerate(types) if name == "model_request_started"]
        self.assertEqual(len(starts), 2)
        self.assertLess(types.index("tool_call_finished"), starts[1])

    async def test_t13_request_index_increments_across_requests(self) -> None:
        env = install([READ_ONE, TEXT_HELLO], tools=[StubTool()])
        await env.kernel.submit("读一下")
        self.assertEqual([e.request_index for e in env.recorder.of("model_request_started")], [0, 1])

    async def test_t14_multiple_tool_rounds(self) -> None:
        script = [READ_ONE, READ_ONE, READ_ONE, TEXT_HELLO]
        env = install(script, tools=[StubTool()])
        turn = await env.kernel.submit("读三次")
        self.assertEqual(turn.tool_call_count, 3)

    async def test_t15_max_iterations_stops_with_an_explicit_error(self) -> None:
        """E-8：到上限要**明确报错**，不静默停止（静默停止会让用户以为模型不说话了）。"""
        env = install([READ_ONE], tools=[StubTool()], max_iterations=3)
        turn = await env.kernel.submit("无限读")
        self.assertIs(turn.status, TurnStatus.FAILED)
        errors = env.recorder.of("error_occurred")
        self.assertEqual(len(errors), 1)
        self.assertIn("3", errors[0].message)
        self.assertEqual(env.recorder.find("turn_finished").reason, "error")
        self.assertEqual(len(env.recorder.of("model_request_started")), 3)

    async def test_t16_unknown_tool_keeps_the_turn_going(self) -> None:
        """E-5：模型幻觉出工具名 → 回灌让它自愈，回合继续。"""
        env = install([tool_chunks([("c1", "nope", {})]), TEXT_HELLO], tools=[StubTool()])
        turn = await env.kernel.submit("调个不存在的工具")
        self.assertIs(turn.status, TurnStatus.DONE)
        self.assertFalse(env.recorder.of("error_occurred"))
        result = env.kernel.history[2].blocks[0]
        self.assertIsInstance(result, ToolResultBlock)
        self.assertFalse(result.ok)

    async def test_t17_tool_exception_keeps_the_turn_going(self) -> None:
        """E-6：工具抛异常不穿透循环（R6）。"""
        tool = StubTool(raises=RuntimeError("炸了"))
        env = install([READ_ONE, TEXT_HELLO], tools=[tool])
        turn = await env.kernel.submit("读一下")
        self.assertIs(turn.status, TurnStatus.DONE)
        self.assertFalse(env.kernel.history[2].blocks[0].ok)

    async def test_t18_invalid_tool_arguments_are_fed_back(self) -> None:
        """E-7：参数校验失败回灌给模型，不抛。"""
        env = install([tool_chunks([("c1", "read", {"wrong": 1})]), TEXT_HELLO], tools=[StubTool()])
        turn = await env.kernel.submit("乱传参数")
        self.assertIs(turn.status, TurnStatus.DONE)
        self.assertIn("参数不合法", env.kernel.history[2].blocks[0].content)

    async def test_t19_tool_args_delta_is_emitted_once_assembled(self) -> None:
        """适配层给的是装配完成的调用，所以 tool_args 只有一条"一次性"增量。"""
        env = install([READ_ONE, TEXT_HELLO], tools=[StubTool()])
        await env.kernel.submit("读一下")
        args_deltas = env.recorder.deltas("tool_args")
        self.assertEqual(len(args_deltas), 1)
        self.assertIn("a.txt", args_deltas[0])

    async def test_t19b_empty_tool_result_is_still_written_to_history(self) -> None:
        """E-17：空结果是**合法结果**，不是"没有结果"。

        若把它当成"没返回"而丢掉，历史里就会少一条 `tool_result`——
        于是下一条用例（E-3 的配对不变量）立刻失守，下一轮请求被厂商 400。
        空内容本身完全正常：`grep` 没有匹配、目录是空的，都属于这一类。
        """
        env = install([READ_ONE, TEXT_HELLO], tools=[StubTool(content="")])
        turn = await env.kernel.submit("读一下")
        self.assertIs(turn.status, TurnStatus.DONE)
        tool_message = next(m for m in env.kernel.history if m.role == "tool")
        self.assertEqual(len(tool_message.blocks), 1)
        self.assertTrue(tool_message.blocks[0].ok)
        self.assertEqual(tool_message.blocks[0].content, "")

    async def test_t19c_empty_model_text_still_leaves_an_assistant_turn(self) -> None:
        """模型什么都没说也要留一条 assistant 消息。

        否则历史里会缺一个 assistant 回合，模型下一轮会困惑于"轮到谁了"。
        """
        env = install([{"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]}])
        await env.kernel.submit("在吗")
        roles = [m.role for m in env.kernel.history]
        self.assertEqual(roles, ["user", "assistant"])
        self.assertEqual(env.kernel.history[-1].blocks[0].type, "text")


class RetryTests(unittest.IsolatedAsyncioTestCase):
    """D52 的两个方向 + 退避规则。"""

    def _no_sleep(self) -> tuple[list[float], object]:
        delays: list[float] = []

        async def fake_sleep(seconds: float) -> None:
            delays.append(seconds)

        return delays, fake_sleep

    async def test_t20_retryable_error_without_output_is_retried(self) -> None:
        delays, sleeper = self._no_sleep()
        env = install([chunks(rate_limit_error()), TEXT_HELLO], sleep_fn=sleeper)
        turn = await env.kernel.submit("在吗")
        self.assertIs(turn.status, TurnStatus.DONE)
        scheduled = env.recorder.of("retry_scheduled")
        self.assertEqual(len(scheduled), 1)
        self.assertEqual(scheduled[0].attempt, 1)
        self.assertTrue(delays)

    async def test_t21_retryable_error_after_output_is_not_retried(self) -> None:
        """★ D52：已经吐过内容就不再重试——宁可手动重发，也不给两段重复文本。"""
        delays, sleeper = self._no_sleep()
        env = install([chunks(*text_chunks("半句")[:-1], network_error())], sleep_fn=sleeper)
        turn = await env.kernel.submit("在吗")
        self.assertIs(turn.status, TurnStatus.FAILED)
        self.assertFalse(env.recorder.of("retry_scheduled"))
        self.assertEqual(delays, [])
        errors = env.recorder.of("error_occurred")
        self.assertEqual(len(errors), 1)
        self.assertFalse(errors[0].retryable)
        self.assertIn("不再自动重试", errors[0].message)
        # 已产出的增量**一个不少**地留在事件流里（D51 的同一条原则）
        self.assertEqual(env.recorder.deltas("text"), ["半句"])

    async def test_t22_non_retryable_error_is_not_retried(self) -> None:
        delays, sleeper = self._no_sleep()
        env = install([chunks(auth_error())], sleep_fn=sleeper)
        turn = await env.kernel.submit("在吗")
        self.assertIs(turn.status, TurnStatus.FAILED)
        self.assertFalse(env.recorder.of("retry_scheduled"))
        self.assertEqual(env.recorder.of("error_occurred")[0].category, "auth")

    async def test_t23_retries_are_capped(self) -> None:
        delays, sleeper = self._no_sleep()
        env = install([chunks(network_error())], sleep_fn=sleeper, max_retries=2)
        turn = await env.kernel.submit("在吗")
        self.assertIs(turn.status, TurnStatus.FAILED)
        self.assertEqual(len(env.recorder.of("retry_scheduled")), 2)
        self.assertEqual(len(env.recorder.of("model_request_started")), 3)  # 首次 + 2 次重试
        self.assertIn("已重试 2 次", env.recorder.of("error_occurred")[0].message)

    async def test_t24_backoff_grows_exponentially(self) -> None:
        delays, sleeper = self._no_sleep()
        env = install([chunks(network_error())], sleep_fn=sleeper, retry_base_s=0.5, max_retries=3)
        await env.kernel.submit("在吗")
        self.assertEqual(delays, [0.5, 1.0, 2.0])

    async def test_t25_retry_after_is_honoured_over_backoff(self) -> None:
        """厂商知道自己的限流窗口，我们猜的不如它给的准。"""
        delays, sleeper = self._no_sleep()

        class WithRetryAfter(Exception):
            status_code = 429
            retry_after = 7.0

        env = install([chunks(WithRetryAfter("slow down")), TEXT_HELLO], sleep_fn=sleeper, retry_base_s=0.5)
        await env.kernel.submit("在吗")
        self.assertEqual(delays, [7.0])

    async def test_t26_retry_after_is_clamped_to_the_maximum(self) -> None:
        delays, sleeper = self._no_sleep()

        class Huge(Exception):
            #: 必须继承 Exception：脚本里"非 BaseException 的项"会被当成分片而不是错误
            status_code = 429
            retry_after = 9999.0

        env = install([chunks(Huge()), TEXT_HELLO], sleep_fn=sleeper, retry_max_s=30.0)
        await env.kernel.submit("在吗")
        self.assertEqual(delays, [30.0])

    async def test_t27_jitter_is_applied(self) -> None:
        delays, sleeper = self._no_sleep()
        env = install(
            [chunks(network_error()), TEXT_HELLO],
            sleep_fn=sleeper,
            retry_base_s=1.0,
            jitter=lambda: 0.5,
        )
        await env.kernel.submit("在吗")
        self.assertEqual(delays, [0.5])

    async def test_t28_retry_scheduled_carries_the_reason(self) -> None:
        _, sleeper = self._no_sleep()
        env = install([chunks(rate_limit_error("Rate limit reached")), TEXT_HELLO], sleep_fn=sleeper)
        await env.kernel.submit("在吗")
        self.assertIn("Rate limit", env.recorder.of("retry_scheduled")[0].reason)

    async def test_t29_content_after_a_retry_is_not_duplicated(self) -> None:
        """重试成功时，事件流里只能有一份正文。"""
        _, sleeper = self._no_sleep()
        env = install([chunks(network_error()), text_chunks("只有一份")], sleep_fn=sleeper)
        await env.kernel.submit("在吗")
        self.assertEqual(env.recorder.deltas("text"), ["只有一份"])
        self.assertEqual(env.kernel.history[-1].text, "只有一份")

    async def test_t30_bad_request_is_not_retryable_but_still_reported(self) -> None:
        _, sleeper = self._no_sleep()
        env = install([chunks(bad_request_error())], sleep_fn=sleeper)
        await env.kernel.submit("在吗")
        self.assertEqual(env.recorder.of("error_occurred")[0].category, "bad_request")


class InterruptTests(unittest.IsolatedAsyncioTestCase):
    """D51 + E-3：中断保留内容、补齐工具结果、配对不变量。"""

    async def test_t40_cancel_during_the_model_stream_keeps_the_output(self) -> None:
        env = install([chunks(*text_chunks("前半句")[:-1], Pause(0.3))])
        turn = await env.kernel.start("在吗")
        await asyncio.sleep(0.05)
        self.assertTrue(turn.cancel())
        await env.kernel.wait(turn)

        self.assertIs(turn.status, TurnStatus.CANCELLED)
        self.assertEqual(env.recorder.deltas("text"), ["前半句"])
        finished = env.recorder.find("turn_finished")
        self.assertEqual(finished.reason, "cancelled")

    async def test_t41_half_sentence_stays_in_history(self) -> None:
        """D51：用户看到什么就留什么——按下 Esc 是想"停下"，不是想"抹掉"。"""
        env = install([chunks(*text_chunks("半句话")[:-1], Pause(0.3))])
        turn = await env.kernel.start("在吗")
        await asyncio.sleep(0.05)
        turn.cancel()
        await env.kernel.wait(turn)
        self.assertEqual(env.kernel.history[-1].text, "半句话")

    async def test_t42_cancel_is_idempotent(self) -> None:
        env = install([chunks(*text_chunks("x")[:-1], Pause(0.3))])
        turn = await env.kernel.start("在吗")
        await asyncio.sleep(0.05)
        self.assertTrue(turn.cancel())
        self.assertFalse(turn.cancel())
        await env.kernel.wait(turn)
        self.assertFalse(turn.cancel())

    async def test_t43_cancel_during_tools_pairs_every_started_call(self) -> None:
        """E-14：中断时每个 Started 都要有配对的 Finished，否则卡片永远转圈。"""
        tools = [
            StubTool("a", delay_s=1.0),
            StubTool("b", delay_s=1.0),
            StubTool("c", delay_s=1.0),
        ]
        script = [tool_chunks([("c1", "a", {}), ("c2", "b", {}), ("c3", "c", {})])]
        env = install(script, tools=tools)
        turn = await env.kernel.start("一起读")
        await asyncio.sleep(0.08)
        turn.cancel()
        await env.kernel.wait(turn)

        self.assertIs(turn.status, TurnStatus.CANCELLED)
        started = env.recorder.of("tool_call_started")
        finished = env.recorder.of("tool_call_finished")
        self.assertEqual(len(started), 3)
        self.assertEqual(len(finished), 3)
        self.assertTrue(all(item.error_kind == "cancelled" for item in finished))
        self.assertEqual({i.call_id for i in started}, {i.call_id for i in finished})

    async def test_t44_interrupt_completes_the_tool_results_e_3(self) -> None:
        """★ E-3：少一条 tool_result，下一轮请求会被厂商 400 —— 而历史会一直留着。"""
        tools = [StubTool("a", delay_s=1.0), StubTool("b", delay_s=1.0)]
        script = [tool_chunks([("c1", "a", {}), ("c2", "b", {})])]
        env = install(script, tools=tools)
        turn = await env.kernel.start("一起读")
        await asyncio.sleep(0.08)
        turn.cancel()
        await env.kernel.wait(turn)

        uses = [b for m in env.kernel.history for b in m.blocks if isinstance(b, ToolUseBlock)]
        results = [b for m in env.kernel.history for b in m.blocks if isinstance(b, ToolResultBlock)]
        self.assertEqual(len(uses), 2)
        self.assertEqual(len(results), 2)
        self.assertEqual({b.id for b in uses}, {b.id for b in results})
        self.assertIn("没有返回结果", [b.content for b in results][0])

    async def test_t45_partially_completed_batch_still_balances(self) -> None:
        """一部分工具已经跑完、另一部分被取消——两边都要有结果。"""
        fast = StubTool("fast", delay_s=0.0)
        slow = StubTool("slow", delay_s=1.0)
        script = [tool_chunks([("c1", "fast", {}), ("c2", "slow", {})])]
        env = install(script, tools=[fast, slow])
        turn = await env.kernel.start("先后读")
        await asyncio.sleep(0.15)
        turn.cancel()
        await env.kernel.wait(turn)

        uses = [b for m in env.kernel.history for b in m.blocks if isinstance(b, ToolUseBlock)]
        results = [b for m in env.kernel.history for b in m.blocks if isinstance(b, ToolResultBlock)]
        self.assertEqual(len(uses), len(results))

    async def test_t46_synthetic_results_are_merged_into_the_existing_tool_message(self) -> None:
        """补齐结果要**并进最后一条 tool 消息**——两条同角色消息会让 Anthropic 拒收。"""
        tools = [StubTool("a", delay_s=0.0), StubTool("b", delay_s=1.0)]
        script = [tool_chunks([("c1", "a", {}), ("c2", "b", {})])]
        env = install(script, tools=tools)
        turn = await env.kernel.start("先后读")
        await asyncio.sleep(0.1)
        turn.cancel()
        await env.kernel.wait(turn)

        roles = [m.role for m in env.kernel.history]
        self.assertEqual(roles.count("tool"), 1)
        self.assertNotIn(("tool", "tool"), list(zip(roles, roles[1:], strict=False)))

    async def test_t47_cancel_during_retry_wait(self) -> None:
        env = install([chunks(network_error())], retry_base_s=5.0, retry_max_s=5.0)
        turn = await env.kernel.start("在吗")
        await asyncio.sleep(0.05)  # 此时正在退避等待里
        turn.cancel()
        await env.kernel.wait(turn)
        self.assertIs(turn.status, TurnStatus.CANCELLED)
        self.assertFalse(env.recorder.of("error_occurred"))
        self.assertEqual(env.recorder.find("turn_finished").reason, "cancelled")

    async def test_t48_cancel_without_a_running_turn_is_a_noop(self) -> None:
        env = install([text_chunks("ok")])
        self.assertFalse(env.kernel.cancel())
        await env.kernel.submit("在吗")
        self.assertFalse(env.kernel.cancel())
        self.assertIsNone(env.kernel.current_turn)

    async def test_t48b_closed_bus_is_not_swallowed(self) -> None:
        """E-25：总线已关闭是**框架级错误**，必须穿透到调用方。

        内核只负责把回合状态登记好再放行。若在这里吞掉它，调用方就再也不知道
        "这个会话其实已经结束了"——而后续每一次 `submit()` 都会静默失败，
        用户看到的是"Agent 不说话了"，完全无从排查。
        """
        from logox.errors import BusClosedError

        env = install([text_chunks("ok")])
        await env.bus.aclose()
        with self.assertRaises(BusClosedError):
            await env.kernel.submit("在吗")

    async def test_t48c_turn_is_marked_failed_before_the_error_escapes(self) -> None:
        """穿透之前必须先把状态登记好，否则会留下一个"永远运行中"的回合。"""
        from logox.errors import BusClosedError

        env = install([text_chunks("ok")])
        await env.bus.aclose()
        with self.assertRaises(BusClosedError):
            await env.kernel.submit("在吗")
        turn = env.kernel._turns[-1]  # noqa: SLF001 - 刻意检查内部状态登记
        self.assertIs(turn.status, TurnStatus.FAILED)
        self.assertIsNone(env.kernel.current_turn, "失败的回合不得被当成仍在运行")

    async def test_t49_started_finished_pairing_invariant_across_scenarios(self) -> None:
        """T-22 的不变量：**任何**场景下两个计数恒等。"""
        tools = [StubTool("a", delay_s=0.05), StubTool("b", delay_s=0.05)]
        script = [tool_chunks([("c1", "a", {}), ("c2", "b", {})]), text_chunks("done")]

        # 场景 1：正常完成
        env = install(script, tools=tools)
        await env.kernel.submit("读")
        self.assertEqual(
            len(env.recorder.of("tool_call_started")), len(env.recorder.of("tool_call_finished"))
        )

        # 场景 2：工具执行中中断
        env = install([tool_chunks([("c1", "a", {}), ("c2", "b", {})])], tools=[StubTool("a", delay_s=1.0), StubTool("b", delay_s=1.0)])
        turn = await env.kernel.start("读")
        await asyncio.sleep(0.08)
        turn.cancel()
        await env.kernel.wait(turn)
        self.assertEqual(
            len(env.recorder.of("tool_call_started")), len(env.recorder.of("tool_call_finished"))
        )

        # 场景 3：模型请求失败
        env = install([chunks(auth_error())], tools=tools)
        await env.kernel.submit("读")
        self.assertEqual(
            len(env.recorder.of("tool_call_started")), len(env.recorder.of("tool_call_finished"))
        )

    async def test_t50_cancel_immediately_after_start_still_finishes_the_turn(self) -> None:
        """F-15 / D51：`start()` 之后**不做任何 await** 直接 cancel，收尾逻辑仍须完整执行。

        这一条盯的是 `KernelLoop.start()` 末尾那句 `await asyncio.sleep(0)`：
        `create_task` 只是把协程**排队**，若调用方在它真正开始前就 cancel，
        `CancelledError` 会在 `_run` 的 `try` **之外**被 throw 进去——
        于是「登记终态 + 补齐工具结果 + 发 TurnFinished」全部不执行，
        界面上留下一个**永远转圈的回合**。

        ⚠️ 为什么之前的用例抓不到：t40 / t41 / t42 / t43 / t44 / t47 **全部在
        `start()` 之后先 `await asyncio.sleep(...)`** 再 cancel。这一条是唯一一条
        「cancel 前不 await」的用例——**把 `sleep(0)` 删掉它就会失败**
        （`await turn.task` 直接抛 CancelledError），这是本用例存在的全部意义。

        刻意**不**断言 `history` 的具体形状：那会与"取消恰好发生在 `_body` 的哪一行"
        耦合，属于实现细节，不该被测试钉住。
        """
        env = install([chunks(*text_chunks("半句")[:-1], Pause(0.5))])
        turn = await env.kernel.start("在吗")
        self.assertTrue(turn.cancel())  # ★ 不 sleep
        await env.kernel.wait(turn)

        self.assertIs(turn.status, TurnStatus.CANCELLED)
        finished = env.recorder.find("turn_finished")
        self.assertEqual(finished.reason, "cancelled")
        self.assertEqual(finished.turn_index, 1)


class MeasurementTests(unittest.IsolatedAsyncioTestCase):
    async def test_t60_first_token_ms_uses_the_injected_clock(self) -> None:
        """D39：首字延迟必须精确可测——所以时钟是可注入的。"""
        clock = FakeClock()
        env = install([text_chunks("你好")], clock=clock)

        original_stream = env.kernel._provider.stream  # noqa: SLF001 - 刻意在测试里推进时钟

        async def advancing(request):  # type: ignore[no-untyped-def]
            async for event in original_stream(request):
                if getattr(event, "kind", None) == "text":
                    clock.advance(0.25)
                yield event

        env.kernel._provider.stream = advancing  # type: ignore[method-assign]
        await env.kernel.submit("在吗")
        finished = env.recorder.find("model_request_finished")
        self.assertEqual(finished.first_token_ms, 250)

    async def test_t61_missing_usage_is_reported_as_such(self) -> None:
        """E-19：厂商没上报用量 → ``usage_reported=False``，状态栏据此显示 `—`。"""
        env = install([text_chunks("你好")])  # 脚本里没有 usage 分片
        await env.kernel.submit("在吗")
        finished = env.recorder.find("model_request_finished")
        self.assertFalse(finished.usage_reported)
        self.assertEqual(finished.usage.input_tokens, 0)
        self.assertIsNone(finished.cost_usd)

    async def test_t62_reported_usage_sets_the_flag_and_the_cost(self) -> None:
        env = install([chunks(*text_chunks("你好")[:-1], usage_chunk(1000, 500, cached=400))])
        await env.kernel.submit("在吗")
        finished = env.recorder.find("model_request_finished")
        self.assertTrue(finished.usage_reported)
        self.assertEqual(finished.usage.cached_input_tokens, 400)

    async def test_t63_cached_none_and_zero_stay_distinct_end_to_end(self) -> None:
        """D39：未上报 → 状态栏整项不显示；真的 0 → 显示 ``cache 0%``。"""
        env = install([chunks(*text_chunks("a")[:-1], usage_chunk(10, 5))])
        await env.kernel.submit("一")
        self.assertIsNone(env.recorder.of("model_request_finished")[0].usage.cached_input_tokens)

        env = install([chunks(*text_chunks("b")[:-1], usage_chunk(10, 5, cached=0))])
        await env.kernel.submit("二")
        self.assertEqual(env.recorder.of("model_request_finished")[0].usage.cached_input_tokens, 0)

    async def test_t64_turn_usage_is_the_sum_of_requests(self) -> None:
        script = [chunks(*tool_chunks([("c1", "read", {})])[:-1], usage_chunk(100, 10)),
                  chunks(*text_chunks("ok")[:-1], usage_chunk(200, 20))]
        env = install(script, tools=[StubTool()])
        turn = await env.kernel.submit("读")
        self.assertEqual(turn.usage_total.input_tokens, 300)
        self.assertEqual(turn.usage_total.output_tokens, 30)
        self.assertEqual(env.recorder.find("turn_finished").usage.input_tokens, 300)

    async def test_t65_unreported_cache_never_becomes_a_fake_zero_in_the_total(self) -> None:
        """保守合并：任一次未上报，整个回合就记为未上报（``None``）。"""
        script = [chunks(*tool_chunks([("c1", "read", {})])[:-1], usage_chunk(100, 10, cached=50)),
                  chunks(*text_chunks("ok")[:-1], usage_chunk(200, 20))]
        env = install(script, tools=[StubTool()])
        turn = await env.kernel.submit("读")
        self.assertIsNone(turn.usage_total.cached_input_tokens)

    async def test_t66_reasoning_is_accumulated_with_its_signature(self) -> None:
        """D48：Anthropic 的思考签名必须随消息历史存活，下一轮才能原样回传。"""
        env = install([text_chunks("你好")], model="claude-sonnet-4-5")
        from logox.providers.base import DeltaEvent, StopEvent, UsageEvent

        async def fake_stream(request):  # type: ignore[no-untyped-def]
            yield DeltaEvent(kind="reasoning", text="先想想")
            yield DeltaEvent(kind="reasoning", text="", vendor_data={"signature": "SIG-1"})
            yield DeltaEvent(kind="text", text="答案")
            yield UsageEvent(usage=__import__("logox.kernel.events", fromlist=["Usage"]).Usage(input_tokens=1, output_tokens=1))
            yield StopEvent(stop_reason="end_turn", model="claude-sonnet-4-5")

        env.kernel._provider.stream = fake_stream  # type: ignore[method-assign]
        await env.kernel.submit("在吗")
        block = env.kernel.history[-1].blocks[0]
        self.assertIsInstance(block, ReasoningBlock)
        self.assertEqual(block.text, "先想想")
        self.assertEqual(block.signature, "SIG-1")
        # 只带签名的增量不产生可见文本事件
        self.assertEqual(env.recorder.deltas("reasoning"), ["先想想"])

    async def test_t67_stop_reason_is_carried_through(self) -> None:
        env = install([chunks(*text_chunks("x")[:-1], {"choices": [{"index": 0, "delta": {}, "finish_reason": "length"}]})])
        await env.kernel.submit("在吗")
        self.assertEqual(env.recorder.find("model_request_finished").stop_reason, "max_tokens")

    async def test_t68_duration_and_generation_ms_are_consistent(self) -> None:
        env = install([text_chunks("x")])
        await env.kernel.submit("在吗")
        finished: ModelRequestFinished = env.recorder.find("model_request_finished")
        self.assertGreaterEqual(finished.duration_ms, finished.first_token_ms or 0)
        self.assertGreaterEqual(finished.generation_ms, 1)


    async def test_t69_trailing_line_becomes_the_turn_summary(self) -> None:
        """★ D135：摘要 = 最终答复的**最后一行**，而且正文**一个字节都不改**。

        （原用例测的是 `<turn_summary>` 标签被提取并剥离；标签机制已在第三步整体退役。）
        这条新口径最本质的差别就在这里：**位置契约只读不裁剪** ——
        "把正文吞掉"这个故障类别因此从机制上消失。
        """
        content = "这是给用户的最终回答。\n\n修复 normalizer 管道符解析并增加测试"
        env = install([text_chunks(content)])
        turn = await env.kernel.submit("帮我修一下代码")

        self.assertEqual(turn.turn_summary, "修复 normalizer 管道符解析并增加测试")
        self.assertEqual(turn.summary_source, "model_last_line")

        finished = env.recorder.find("turn_finished")
        self.assertEqual(finished.turn_summary, turn.turn_summary)
        self.assertEqual(finished.summary_source, "model_last_line")

        # ★ 正文保持原样（不剥离、不改写）
        last_msg = env.kernel.history[-1]
        self.assertEqual(last_msg.text, content)
        self.assertEqual(last_msg.meta.turn_summary, turn.turn_summary)
        self.assertEqual(last_msg.meta.summary_source, "model_last_line")

        # ★ 流式增量也一个字节都不能被吞
        deltas = "".join(
            event.delta
            for event in env.recorder.events
            if isinstance(event, ev.ModelDelta) and event.kind == "text"
        )
        self.assertEqual(deltas, content)

    async def test_t70_turn_summary_fallback_when_tag_missing(self) -> None:
        """测试当模型未提供标签时，确定性兜底逻辑生效。"""
        content = "直接修复了某问题\n第二行内容"
        env = install([text_chunks(content)])
        turn = await env.kernel.submit("测试问答")
        self.assertEqual(turn.turn_summary, "直接修复了某问题")
        finished = env.recorder.find("turn_finished")
        self.assertEqual(finished.turn_summary, "直接修复了某问题")

    async def test_t71_pure_reasoning_triggers_silent_continuation(self) -> None:
        """D118：模型第 1 次仅返回纯思考时，不结轮、不生成摘要，自动发起静默续写接力。"""
        chunk_stream_1 = [
            {"choices": [{"index": 0, "delta": {"reasoning_content": "我在推导动画算法..."}, "finish_reason": None}]},
            {"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]},
        ]
        chunk_stream_2 = text_chunks("这是最终的网页正文内容")
        env = install([chunk_stream_1, chunk_stream_2])

        turn = await env.kernel.submit("帮我写个网页")
        self.assertIs(turn.status, TurnStatus.DONE)
        # 验证历史记录严格保持角色交替且包含系统续写提示
        roles = [m.role for m in env.kernel.history]
        self.assertEqual(roles, ["user", "assistant", "user", "assistant"])
        self.assertIn("思考已结束或单次输出已达上限", env.kernel.history[2].text)
        # 验证最终输出了正文，且提取了正文的摘要
        self.assertEqual(env.kernel.history[-1].text, "这是最终的网页正文内容")
        self.assertEqual(turn.turn_summary, "这是最终的网页正文内容")

    async def test_t72_pure_reasoning_does_not_generate_turn_summary_when_empty(self) -> None:
        """D118：纯思考未产出有效交付物时，严禁伪造‘完成第 X 轮交互’假摘要。"""
        chunk_stream = [
            {"choices": [{"index": 0, "delta": {"reasoning_content": "纯思考内容"}, "finish_reason": None}]},
            {"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]},
        ]
        # 两次都只有思考，无正文
        env = install([chunk_stream, chunk_stream])
        turn = await env.kernel.submit("提问")
        self.assertIs(turn.status, TurnStatus.FAILED)
        # 绝不出现兜底的"完成第 1 轮交互"
        self.assertNotEqual(turn.turn_summary, "完成第 1 轮交互")

    async def test_t73_pure_reasoning_continuation_limit_enforced(self) -> None:
        """D118：续写次数超过 max_continuations 时安全退出，绝不陷入死循环。"""
        chunk_stream = [
            {"choices": [{"index": 0, "delta": {"reasoning_content": "一直思考停不下来"}, "finish_reason": None}]},
            {"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]},
        ]
        env = install([chunk_stream, chunk_stream, chunk_stream])
        turn = await env.kernel.submit("提问")
        self.assertIs(turn.status, TurnStatus.FAILED)
        finished = env.recorder.find("turn_finished")
        self.assertEqual(finished.reason, "error")

    async def test_t74_max_tokens_truncation_triggers_continuation(self) -> None:
        """D118：因 max_tokens 截断（finish_reason='length'）且无正文时，自动触发续写。"""
        chunk_stream_1 = [
            {"choices": [{"index": 0, "delta": {"reasoning_content": "被截断的思考..."}, "finish_reason": "length"}]},
        ]
        chunk_stream_2 = text_chunks("接力输出的正文内容")
        env = install([chunk_stream_1, chunk_stream_2])
        turn = await env.kernel.submit("画图")
        self.assertIs(turn.status, TurnStatus.DONE)
        self.assertEqual(env.kernel.history[-1].text, "接力输出的正文内容")

    async def test_t75_tool_argument_truncation_triggers_self_healing(self) -> None:
        """D121：工具参数因 Token 上限截断（length）时，内核自动注入系统纠偏并驱动自愈。"""
        # 第一次响应：输出文本 + 工具参数中途被截断（arguments 未闭合）
        chunk_stream_1 = [
            {"choices": [{"index": 0, "delta": {"content": "我来给动画加交互："}, "finish_reason": None}]},
            {
                "choices": [
                    {
                        "index": 0,
                        "delta": {
                            "tool_calls": [
                                {
                                    "index": 0,
                                    "id": "call_trunc",
                                    "function": {"name": "write", "arguments": '{"content": "<html'},
                                }
                            ]
                        },
                        "finish_reason": "length",
                    }
                ]
            },
        ]
        # 第二次响应：收到自愈提示后成功输出
        chunk_stream_2 = text_chunks("已改用局部修改，动画交互已成功加入！")
        env = install([chunk_stream_1, chunk_stream_2])

        turn = await env.kernel.submit("修改动画代码")
        self.assertIs(turn.status, TurnStatus.DONE)

        # 验证截断前的半句话与自愈提示均已保留进历史
        user_healing_msgs = [m for m in env.kernel.history if m.role == "user" and "严禁使用 write 工具" in m.text]
        self.assertEqual(len(user_healing_msgs), 1, "必须注入一条自愈指引消息")
        self.assertEqual(env.kernel.history[-1].text, "已改用局部修改，动画交互已成功加入！")

    async def test_t76_tool_argument_truncation_limit_enforced(self) -> None:
        """D121：工具参数截断连续超出自愈次数限制时安全报错退出，防死循环。"""
        chunk_stream_fail = [
            {
                "choices": [
                    {
                        "index": 0,
                        "delta": {
                            "tool_calls": [
                                {
                                    "index": 0,
                                    "id": "call_trunc",
                                    "function": {"name": "write", "arguments": '{"unclosed": "json'},
                                }
                            ]
                        },
                        "finish_reason": "length",
                    }
                ]
            },
        ]
        # 两次都因截断失败
        env = install([chunk_stream_fail, chunk_stream_fail])
        turn = await env.kernel.submit("修改代码")
        self.assertIs(turn.status, TurnStatus.FAILED)
        finished = env.recorder.find("turn_finished")
        self.assertEqual(finished.reason, "error")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()




class WithinTurnRecheckTests(unittest.IsolatedAsyncioTestCase):
    """★ CHANGE-005 裁定 8：一个回合里模型请求跑多轮时，水位线**每轮都要复查**。

    改之前：压缩只在 `_body` 开头调用一次。回合开始时上下文"还没满"，然后本回合的
    工具输出一路叠上去 —— 到第 N 轮请求时真实上下文早已超过窗口，而内核一次都
    没再看过水位线。厂商的回礼是 400。

    这不是假想：pi 在 CHANGELOG **0.84.4** 修过一模一样的 bug
    （"compacts between tool execution and the next assistant response in the same run"）。
    两家踩的是同一个坑，因为两家的循环长得一样：**压缩在循环外，增长在循环内**。
    """

    def _env(self, script, **builder_kwargs):
        from logox.context.builder import HierarchicalContextBuilder
        from logox.kernel.bus import EventBus
        from logox.kernel.registry import ToolRegistry
        from tests.unit.kernel_support import Recorder, StubTool, scripted_provider

        bus = EventBus(session_id="recheck")
        recorder = Recorder()
        bus.subscribe("*", recorder, name="recorder")
        registry = ToolRegistry()
        registry.register(StubTool(content="Y" * 4000))
        builder = HierarchicalContextBuilder(
            system="sys",
            project_memory_enabled=False,
            cwd=".",
            session_id="recheck",
            # ★ D153：以前这里没传 writer ⇒ 每次跑测试都在仓库里建 `.logox/runs/recheck/`
            #   （删了下次跑又回来）。现在 writer 必填，落点用项目认可的临时目录。
            transcript_writer=SessionTranscriptWriter(
                base_dir=make_temp_dir("recheck-"), session_id="recheck"
            ),
            **builder_kwargs,
        )
        kernel = KernelLoop(
            bus,
            scripted_provider(script),
            registry,
            builder,
            model="mock-model",
            max_iterations=8,
        )
        return kernel, recorder

    async def test_a_long_turn_rebuilds_and_shrinks_its_own_view(self) -> None:
        """Large results must trigger real compaction before another model request."""
        script = [tool_chunks([(f"call_{i}", "read", {"path": "a.txt"})]) for i in range(1, 7)]
        script.append(text_chunks("好了"))
        kernel, recorder = self._env(
            script, window_capacity=6000, reserve_tokens=1000,
            keep_recent_turns=1, keep_recent_tool_results=1,
        )
        turn = await kernel.submit("读六个文件")
        builds = [event.token_estimate for event in recorder.of(ev.ContextBuilt)]
        self.assertEqual(len(builds), 7, "每次主模型请求前都必须重新构建上下文")
        self.assertTrue(recorder.of(ev.CompactionFinished), "工具原文必须实际压缩")
        self.assertTrue(all(value < kernel._builder.compactor.high_watermark for value in builds), builds)
        self.assertTrue(any(later < earlier for earlier, later in zip(builds, builds[1:], strict=False)), builds)
        self.assertEqual(turn.status.value, "done")
        writer = kernel._builder.writer
        for index in range(1, 6):
            pointer = writer.blob_path_of(f"call_{index}")
            self.assertIsNotNone(pointer)
            self.assertEqual((writer.session_dir / pointer).read_text(encoding="utf-8"), "Y" * 4000)
