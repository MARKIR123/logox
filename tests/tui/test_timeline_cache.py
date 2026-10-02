"""D56 前缀缓存的正确性与失效用例。

这个文件存在的唯一理由：**"改过的块没更新"是这里最危险的失败模式**
——用户会看到过时的信息，却没有任何提示。因此它做两件事：

1. 逐条覆盖**会让缓存失效的途径**（工具卡片完成、推理变长、展开状态、宽度变化……）；
2. 用一条**对拍断言**兜底：缓存渲染必须与"绕开缓存的全量渲染"**逐字符相等**。

对拍是刻意选的口径：它不问"哪一处该失效"，只问"结果对不对"。
于是将来任何人新增了一种修改块的途径却忘了失效缓存，只要它影响渲染结果，
第 2 条就会失败——**不需要有人想到去补那一条用例**。

⚠️ 缓存逻辑在 D85 之后住在 :class:`~logox.tui.render.app.TimelineComponent` 里
（原来挂在 Textual 的 `TimelineView` 上，随旧界面一起删掉了）。
本文件因此不再需要任何界面框架，也不再需要 `run_test()`。
"""

from __future__ import annotations

import unittest

from logox.kernel.events import ChangeStat
from logox.tui.content.cards import CardContext
from logox.tui.content.timeline import TimelineBuffer, render_blocks
from logox.tui.render.app import TimelineComponent
from logox.tui.theme import load_theme

PALETTE = load_theme("logox-dark").palette
WIDTH = 116


def _component() -> TimelineComponent:
    return TimelineComponent(PALETTE)


def _render(component: TimelineComponent, width: int = WIDTH) -> str:
    """组件这一帧的**纯文本**（缓存路径）。"""
    return "\n".join(row.plain for row in component.render(width))


def _uncached(component: TimelineComponent, width: int = WIDTH) -> str:
    """**绕开缓存**的全量渲染（对拍用的正确答案）。"""
    context = CardContext(palette=PALETTE, width=width)
    rendered = render_blocks(
        component.buffer.visible_blocks,
        context,
        expand_tools=component.buffer.expand_tools,
        expand_reasoning=component.buffer.expand_reasoning,
    )
    return rendered.plain


def _norm(text: str) -> str:
    """去掉末尾换行再比。

    为什么要归一：`render_blocks` 的输出以换行**结尾**（那是行终止符），
    而组件切行时会丢掉它——**一行都不少**，只是"末尾还有没有那个换行符"的差别。
    对拍要抓的是"缓存渲染 != 全量渲染"（也就是显示了过时内容），
    不该被这种表示差异绊倒。
    """
    return text.rstrip("\n")

def _fill(buffer: TimelineBuffer, count: int) -> None:
    """灌入 ``count`` 个块（三种形态混排，贴近真实会话）。"""
    for index in range(count):
        if index % 3 == 0:
            buffer.add_assistant(f"第 {index} 段回答，带 some English 与 `code`。" * 3)
        elif index % 3 == 1:
            buffer.start_tool(call_id=f"c{index}", name="read", args_summary=f"src/a{index}.py")
            buffer.finish_tool(call_id=f"c{index}", ok=True, duration_ms=100)
        else:
            buffer.add_user(f"第 {index} 个问题")


def _blocks_rendered(component: TimelineComponent, width: int = WIDTH) -> int:
    """这一帧**一共渲染了多少个块**（确定性口径，与机器负载无关）。

    做法是把 :func:`render_blocks` 临时换成会计数的版本：缓存路径会跳过前缀，
    于是"这一帧碰了几个块"直接量得出来。用耗时断言会随负载抖动，
    而本项目已经因为那个原因删掉过一条计时用例。
    """
    import logox.tui.content.timeline as timeline_module

    original = timeline_module.render_blocks
    total = 0

    def counting(blocks, context, **kwargs):  # noqa: ANN001, ANN003
        nonlocal total
        total += len(blocks)
        return original(blocks, context, **kwargs)

    timeline_module.render_blocks = counting  # type: ignore[assignment]
    try:
        component.render(width)
    finally:
        timeline_module.render_blocks = original  # type: ignore[assignment]
    return total


class _SameAsFullMixin:
    """对拍断言：**缓存渲染 == 绕开缓存的全量渲染**。"""

    def assertSameAsFull(self, component: TimelineComponent, *, width: int = WIDTH, note: str = "") -> None:
        got = _norm(_render(component, width))
        expected = _norm(_uncached(component, width))
        self.assertEqual(
            got,
            expected,
            f"缓存渲染与全量渲染不一致{('：' + note) if note else ''}",
        )


class RowSplitCacheTests(_SameAsFullMixin, unittest.TestCase):
    """D126：**切行**这一步也必须只处理尾部，并且结果与整体切分逐行一致。

    为什么它值得单独一组用例：D56 缓存的是**一段文本**，而帧要的是**行列表**，
    中间那次"把整段文本切成行"以前每帧都对整段做——它的代价与会话长度成正比。
    实测：40 条消息的会话里，光这一步每帧就要 ~28ms（打字明显跟不上手，
    且会话越长越卡）。这一组守住"只切尾部"这个性质，以及它的正确性。
    """

    def test_prefix_rows_are_reused_when_only_the_tail_changes(self) -> None:
        """流式期间（只长最后一块）不应重新切前缀的行。"""
        from logox.kernel import events as ev

        component = _component()
        _fill(component.buffer, 50)
        component.render(WIDTH)  # 建缓存
        before = component.prefix_row_reuses
        for _ in range(3):
            component.ingest(ev.ModelDelta(session_id="s", request_index=0, kind="text", delta="增量"))
            component.render(WIDTH)
        self.assertGreater(component.prefix_row_reuses, before, "前缀的行没有被复用（每帧都在重切全量）")

    def test_incremental_split_matches_a_fresh_render(self) -> None:
        """★ 对拍：边流式边渲染（走增量切分）与一次性渲染（走整体切分）逐行相同。

        失败模式是"行被劈错/错位"——不会报错，只会让屏幕上的内容错行，
        所以这里只问结果，不问路径（与 D56 的那条对拍同一个口径）。
        """
        from logox.kernel import events as ev

        stepwise = _component()
        _fill(stepwise.buffer, 30)
        for piece in ("第一段回答", "第二段回答", "第三段回答"):
            stepwise.ingest(ev.ModelDelta(session_id="s", request_index=0, kind="text", delta=piece))
            stepwise.render(WIDTH)

        fresh = _component()
        _fill(fresh.buffer, 30)
        for piece in ("第一段回答", "第二段回答", "第三段回答"):
            fresh.ingest(ev.ModelDelta(session_id="s", request_index=0, kind="text", delta=piece))
        fresh.render(WIDTH)

        self.assertEqual(
            _render(stepwise),
            _render(fresh),
            "增量切分的行与整体切分的行不一致（内容错位）",
        )
        # 再与"绕开缓存的全量渲染"对拍一次。
        # ⚠️ 先清掉活跃状态行：`_uncached` 不带活跃状态（它只管块），
        # 不对齐的话这个对比会恒假（与切行本身无关）。
        stepwise.buffer.clear_active_status()
        self.assertSameAsFull(stepwise)


class CoreContractTests(_SameAsFullMixin, unittest.TestCase):
    """D56 的核心契约：只渲染尾部、且结果与全量一致。"""

    def test_long_session_only_renders_the_tail(self) -> None:
        """★ 500 块的会话里，每帧只重渲染**最后一块**（缓存的核心断言）。

        为什么断言"块数"而不是"耗时"：耗时断言会随机器负载抖动（M3 的教训
        ——掐表判断并发在满负载下偶发失败，最后改成了确定性的口径）。
        缓存覆盖的块数是**确定性的工作量口径**。
        """
        component = _component()
        _fill(component.buffer, 500)

        component.render(WIDTH)  # 第一次：建立缓存
        for _ in range(10):
            component.render(WIDTH)

        total = len(component.buffer.visible_blocks)
        self.assertEqual(component._cache.covers, total, "未变化的最后一块也应缓存")  # noqa: SLF001
        self.assertGreaterEqual(component.prefix_hits, 5, "重复渲染应当命中缓存")
        self.assertSameAsFull(component)

    def test_cached_render_is_faster_than_full(self) -> None:
        """★★ 每帧的工作量必须与**变化量**成正比，而不是与**会话长度**成正比。

        这是"流畅"的可执行定义，也是整个 D56 缓存存在的理由：
        500 块的会话里追加一个字，不该重算 500 行的 Markdown。

        为什么断言**块数**而不是耗时：耗时断言会随机器负载抖动——本项目已经吃过
        这个亏（M3 掐表判断并发，在满负载下偶发失败，最后改成确定性的闸门）。
        "渲染了多少块"表达的是同一件事（工作量），但它不依赖机器。
        """
        component = _component()
        _fill(component.buffer, 500)
        component.render(WIDTH)  # 建缓存

        self.assertEqual(_blocks_rendered(component), 0, "没有变化时无需重排任何消息块")

        # 冷启动（全新组件）才是全量——把它作为对照，证明"1"不是测量误差
        fresh = _component()
        _fill(fresh.buffer, 500)
        self.assertEqual(_blocks_rendered(fresh), 500, "首次渲染本来就是全量")

    def test_warm_render_does_not_invalidate_the_cache(self) -> None:
        """纯重复渲染**不得**作废缓存——否则每帧都要重算全部块。"""
        component = _component()
        _fill(component.buffer, 20)
        component.render(WIDTH)
        before = component.cache_invalidations
        for _ in range(5):
            component.render(WIDTH)
        self.assertEqual(component.cache_invalidations, before)

    def test_empty_buffer(self) -> None:
        component = _component()
        self.assertEqual(_render(component), "")

    def test_single_unchanged_block_is_cached(self) -> None:
        """D199：最后块也可复用，但正文变化必须让它重新渲染。"""
        component = _component()
        component.buffer.add_assistant("只有一块")
        component.render(WIDTH)
        self.assertEqual(component._cache.covers, 1)  # noqa: SLF001
        self.assertSameAsFull(component)


class InvalidationTests(_SameAsFullMixin, unittest.TestCase):
    """D56 的 7 个失效触发条件：**每一条都同时做对拍**。"""

    def test_finishing_a_tool_card_shows_the_duration(self) -> None:
        """工具卡片完成：耗时是**后到**的，缓存不失效就永远显示不出来。"""
        component = _component()
        component.buffer.add_assistant("先垫一块，让卡片进缓存前缀")
        component.buffer.start_tool(call_id="c1", name="read", args_summary="a.py")
        component.render(WIDTH)  # 让卡片进入缓存前缀
        component.buffer.finish_tool(call_id="c1", ok=True, duration_ms=400)

        self.assertIn("0.4s", _render(component), "耗时是后到的，缓存必须失效才能显示出来")
        self.assertSameAsFull(component)

    def test_failing_a_tool_card_auto_expands(self) -> None:
        component = _component()
        component.buffer.add_assistant("垫一块")
        component.buffer.start_tool(call_id="c1", name="read", args_summary="a.py")
        component.render(WIDTH)
        component.buffer.finish_tool(call_id="c1", ok=False, error_kind="not_found", duration_ms=10)
        self.assertIn("not_found", _render(component))
        self.assertSameAsFull(component)

    def test_reasoning_growing(self) -> None:
        component = _component()
        component.buffer.add_assistant("垫一块")
        component.buffer.start_reasoning()
        component.render(WIDTH)
        component.buffer.add_reasoning_delta("想了一会儿")
        component.buffer.flush_reasoning()
        self.assertIn("思考", _render(component), "折叠态下应显示推理块摘要行")
        self.assertSameAsFull(component)

    def test_expand_all(self) -> None:
        """两路展开开关各自都要让缓存整体作废（D125-d 的**核心回归防线**）。

        ⚠️ 这条用例存在的唯一理由：拆开关时如果忘了把新维度加进
        `TimelineRenderCache.matches`，渲染结果会被判定为"参数没变"而**直接复用旧画面**
        —— 屏幕毫无变化、也没有报错。`assertSameAsFull` 会把这种"静默失效"抓成红色。
        """
        component = _component()
        component.buffer.add_assistant("垫一块")
        component.buffer.start_tool(call_id="c1", name="read", args_summary="a.py")
        component.buffer.finish_tool(call_id="c1", ok=True, duration_ms=1, change_stat=ChangeStat(kind="modify", added=3, removed=1))
        component.render(WIDTH)
        component.buffer.expand_tools = not component.buffer.expand_tools
        self.assertSameAsFull(component)
        component.buffer.expand_reasoning = not component.buffer.expand_reasoning
        self.assertSameAsFull(component)

    def test_fold_history(self) -> None:
        component = _component()
        _fill(component.buffer, 40)  # 超过 FOLD_THRESHOLD
        component.render(WIDTH)
        self.assertSameAsFull(component)

    def test_width_change(self) -> None:
        """宽度变了 → 折行位置全变 → 缓存必须整体作废。"""
        component = _component()
        _fill(component.buffer, 10)
        component.render(WIDTH)
        before = component.cache_invalidations
        component.render(WIDTH // 2)
        self.assertGreater(component.cache_invalidations, before)
        self.assertSameAsFull(component, width=WIDTH // 2)

    def test_appending_a_block(self) -> None:
        component = _component()
        _fill(component.buffer, 5)
        component.render(WIDTH)
        component.buffer.add_user("新的一条")
        component.render(WIDTH)
        self.assertSameAsFull(component)

    def test_clearing_the_buffer(self) -> None:
        component = _component()
        _fill(component.buffer, 5)
        component.render(WIDTH)
        component.buffer.blocks.clear()
        component.buffer.invalidate()
        component.render(WIDTH)
        self.assertSameAsFull(component)

    def test_streaming_delta_appends_to_the_same_block(self) -> None:
        """★ 流式正文必须**续在同一块**里（D76：用户第三次报障的真正原因）。

        每一帧都新起一块的话，屏幕上会变成"第一段单独占一行、第二段另起一行"
        ——而这正是当时那个"奇怪的换行"。
        """
        component = _component()
        for piece in ("我是 Logox，", "一个在终端里工作的助手。"):
            component.buffer.add_delta(piece)
            component.flush()
            component.render(WIDTH)
        self.assertEqual(
            [block.text for block in component.buffer.visible_blocks],
            ["我是 Logox，一个在终端里工作的助手。"],
            "流式增量被拆成了多块",
        )
        self.assertSameAsFull(component)

    def test_multiline_user_message_preserves_line_breaks_without_flattening(self) -> None:
        """★ CHANGE-054：用户多行提问在对话时间线中必须严格保留换行，绝不能被 reflow 压平。"""
        component = _component()
        component.buffer.add_user("第一行：你好\n第二行：我想问一个问题\n第三行：这是结束")
        output = _render(component)
        self.assertIn("第一行：你好", output)
        self.assertIn("第二行：我想问一个问题", output)
        self.assertIn("第三行：这是结束", output)
        # 验证各行是独立分行渲染，而不是被合并拼接在一行
        lines = [line.strip() for line in output.split("\n") if line.strip()]
        self.assertTrue(any("第一行：你好" in line for line in lines))
        self.assertTrue(any("第二行：我想问一个问题" in line for line in lines))
        self.assertTrue(any("第三行：这是结束" in line for line in lines))
        self.assertFalse(any("第一行：你好第二行" in line for line in lines))
        self.assertSameAsFull(component)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
