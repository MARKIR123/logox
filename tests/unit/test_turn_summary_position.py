"""D135 第二步的用例：**位置契约**（摘要 = 最终答复的最后一行）+ 三层兜底 + 来源标记。

为什么这个文件独立存在
====================
旧的标签契约相关用例在 `test_turn_summary_tag.py`（它们守的是"标签被剥干净"，属**过渡期**兼容）。
这里守的是**新契约**：

* 主路径 —— 末尾行（零成本、零标记）；
* 兜底 2 —— 末尾行不合规时**再调一次模型**补写（用户裁定 Q-D）；
* 兜底 3 —— 本地确定性生成；
* **来源标记** —— 用户裁定 Q-D：`/resume` 列表与历史消息的 meta 都要能看出摘要怎么来的。

⚠️ 有一条贯穿全篇的不变量：**任何一层都不得修改正文**。
"""

from __future__ import annotations

import unittest

from logox.kernel.messages import Message, TextBlock
from logox.kernel.summary import (
    INTERRUPT_SUMMARY_QUESTION_CHARS,
    deterministic_summary,
    extract_trailing_summary,
    interrupted_summary,
    is_substantive_line,
    normalize_model_summary,
    render_turn_transcript,
)
from tests.unit.kernel_support import (
    StubTool,
    chunks,
    install,
    text_chunks,
    tool_chunks,
    usage_chunk,
)

SUMMARY = "修好了登录重定向并补了 2 条用例"


class TrailingSummaryTests(unittest.TestCase):
    """末尾行判定：**取最后一个有实质内容的行**，且不合规要留下原因。"""

    def test_last_line_is_the_summary(self) -> None:
        verdict = extract_trailing_summary(f"正文若干行\n\n{SUMMARY}")
        self.assertEqual(verdict.summary, SUMMARY)
        self.assertEqual(verdict.reason, "ok")

    def test_trailing_blank_lines_are_skipped(self) -> None:
        """末尾空行不算"最后一行"（用户裁定 Q-B）。"""
        verdict = extract_trailing_summary(f"正文\n\n{SUMMARY}\n\n   \n")
        self.assertEqual(verdict.summary, SUMMARY)

    def test_trailing_symbol_line_is_skipped(self) -> None:
        """★ 空行与**纯符号行**都要跳过继续往前找（用户裁定 Q-B）。

        实测 25% 的技术性长回答以结构行结尾 —— 若判据是"最后一行"，摘要永远取不到。
        """
        for tail in ("---", "===", "***", "|", "___"):
            with self.subTest(tail=tail):
                verdict = extract_trailing_summary(f"正文\n\n{SUMMARY}\n\n{tail}")
                self.assertEqual(verdict.summary, SUMMARY, f"末尾 {tail} 时没有往前找")

    def test_table_and_list_tails_are_rejected(self) -> None:
        """末尾是表格 / 编号列表 ⇒ 拒绝（这些行**有**实质内容，不能当摘要）。"""
        for tail in ("| 文件 | 状态 |", "1. 改了 builder.py", "2) 改了 kernel"):
            with self.subTest(tail=tail):
                verdict = extract_trailing_summary(f"正文\n\n{tail}")
                self.assertIsNone(verdict.summary)
                self.assertEqual(verdict.reason, "structural")

    def test_code_block_tail_is_rejected(self) -> None:
        """★ 末尾在代码块里 ⇒ 拒绝。

        代码行又短又常以字母开头，前缀判据挡不住 —— 靠**围栏计数**才精确。
        """
        verdict = extract_trailing_summary("正文\n\n```python\nreturn result\n```")
        self.assertIsNone(verdict.summary)
        self.assertEqual(verdict.reason, "inside_code_block")

    def test_only_too_short_is_rejected_by_length(self) -> None:
        """★ CHANGE-052：**长度上限已删除，只剩"过短"这条长度类判定。**

        旧行为（D160）：`> 300 字 ⇒ "too_long" 拒绝` —— 用户裁定删除它：
        「摘要多少**完全靠契约约束以及模型的自觉**，本身每轮做的事就是有多有少」。
        """
        long_line = "很长的一句本应是正文的话" * 30  # 400 字
        verdict = extract_trailing_summary(f"正文\n\n{long_line}")
        self.assertEqual(verdict.reason, "ok", "长摘要不再被拒（上限已删除）")
        self.assertEqual(verdict.summary, long_line, "一个字都不许改")

        verdict = extract_trailing_summary("正文\n\n好了")
        self.assertEqual(verdict.reason, "too_short")

    def test_no_length_upper_bound(self) -> None:
        """★ CHANGE-052 的核心契约：**没有上限这一档**。

        ================ ==================== ===============
        末尾行长度        处理                 原因标记
        ================ ==================== ===============
        ≥ 8             **原样收下，一字不改**   ``ok``
        < 8             拒绝（太短）           ``too_short``
        ================ ==================== ===============

        历史沿革（保留）：60 字（提示词）→ 100 收下并截断 → 300 拒收（D160）→ 删除。
        **同一件事曾有过四个数，每个数都自称"上限"。**

        为什么最终全删（用户原话：「不要有上限，但是一定要在契约强调要输出简洁的摘要，
        因此摘要我们从系统硬截断约束，变成了**模型软约束**」）：

        * **上限防的东西，结构守卫已经防住** —— 正文段落的结尾极少恰好是
          "一句像摘要的单行"（`|`/`-`/`#`/围栏/编号开头、代码块内，都已拦）；
        * **上限误伤的东西很实在** —— 实测 `summary_reason` 里 4 轮
          `too_long → l2_rejected → l3`：模型写了摘要、被字数否决，
          于是换成"抓正文第一行"。
        """
        # 远超旧上限（300）与旧硬上限，仍原样收下
        for length in (8, 300, 301, 1000, 5000):
            with self.subTest(length=length):
                line = "字" * length
                verdict = extract_trailing_summary(f"正文\n\n{line}")
                self.assertEqual(verdict.reason, "ok")
                self.assertEqual(verdict.summary, line, "一个字都不许改")

        # 下限仍然生效（"好了"、"完成"不是摘要）
        verdict = extract_trailing_summary("正文\n\n完成")
        self.assertEqual((verdict.reason, verdict.summary), ("too_short", None))

    def test_structural_guards_still_reject_paragraphs(self) -> None:
        """★ 上限删除后，**"不误收正文段落"的责任全落在三道结构守卫上** —— 必须守住。

        这条用例是 `test_no_length_upper_bound` 的**安全网**：一旦有人以为
        "既然不限长，那结构判据也可以删"，正文的最后一行就会被当成摘要收下。
        """
        cases = {
            "structural_table": "正文\n\n| 项 | 值 |",
            "structural_list": "正文\n\n- 第一条要点",
            "structural_heading": "正文\n\n## 结论",
            "structural_ordered": "正文\n\n1. 第一步",
            "inside_code_block": "正文\n\n```python\nreturn result\n```",
        }
        for name, text in cases.items():
            with self.subTest(case=name):
                verdict = extract_trailing_summary(text)
                self.assertIsNone(verdict.summary, f"{name} 必须被拒绝")
                self.assertNotEqual(verdict.reason, "ok")

    def test_long_summary_keeps_its_tail(self) -> None:
        """★ **回归守卫（D160 的核心）**：长摘要的**尾部必须完整保留**。

        为什么要单独守"尾部"：这是本次改动要修的具体缺陷 ——
        落盘时被截断的是**尾部**，而契约要求把"踩坑与待办"放在尾部。
        实测真例：某一轮的摘要原文结尾是
        "…并发现 `keep_recent_turns` 配置仍无人读"（一个待办），
        被截到 100 字后**恰好把这段砍掉**，只留下前面"做了什么"。
        """
        tail = "并发现 keep_recent_turns 配置仍无人读"
        real = "策略本身扎实（优先卸工具输出、写入时摘要、显式幂等）；" + "细节" * 30 + "；" + tail
        self.assertGreater(len(real), 100, "该夹具必须长于旧上限，否则守不住回归")
        verdict = extract_trailing_summary(f"正文若干行\n\n{real}")
        self.assertEqual(verdict.reason, "ok")
        self.assertEqual(verdict.summary, real)
        self.assertTrue(verdict.summary.endswith(tail), "尾部（待办）必须还在")

    def test_the_real_world_140_char_summary_is_kept_whole(self) -> None:
        """★ 回归夹具：**用户真机上的那一行**（140 字，无句末标点）⇒ 现在**原样收下**。

        它在旧口径下是 `truncated`（被砍到 100 字 + "…"）；现在必须是 `ok` 且一字不差。
        """
        real = (
            "已讲清上下文管理四层（tokens/memory/compaction/builder）与关键取舍，"
            "并核出两处断线：`record_usage` 无人调用致 EMA 校准失效、"
            "`keep_recent_turns`/`compact_threshold` 配置无人读，待你定修哪个"
        )
        self.assertEqual(len(real), 140)
        verdict = extract_trailing_summary(f"正文若干行\n\n{real}")
        self.assertEqual(verdict.reason, "ok")
        self.assertEqual(verdict.summary, real, "真机那一条现在必须完整保留")

    def test_pure_symbol_lines_are_not_substantive(self) -> None:
        for probe in ("", "   ", "---", "***", "|", "==="):
            with self.subTest(probe=probe):
                self.assertFalse(is_substantive_line(probe))
        self.assertTrue(is_substantive_line("改了 builder.py"))


class InterruptSummaryTests(unittest.TestCase):
    """★ CHANGE-052：异常终止轮次的摘要 = **用户问题 + 异常说明**。

    用户裁定：「如果有被中断的轮次，摘要应该是用户问题 + 该轮次被异常中断」。

    为什么值得看护：折叠之后该轮**原文整段消失**，"第 7 轮被中断"没说这一轮
    想干什么 —— 模型接着干活时不知道"用户当时要的那个东西"还需不需要做。
    """

    def test_question_comes_first_then_the_reason(self) -> None:
        self.assertEqual(
            interrupted_summary("帮我把输入框颜色改一下", "本轮被中断"),
            "提问：帮我把输入框颜色改一下；本轮被中断",
        )

    def test_question_is_flattened_to_one_line(self) -> None:
        """★ 必须单行：用户在编辑框里可以贴多段（真机实测有 690 字的粘贴），
        而摘要是"一行标签" —— 嵌了换行会污染折叠结构与落盘记录。
        """
        summary = interrupted_summary("第一行\n\n第二行\t带制表符", "本轮被中断")
        self.assertNotIn("\n", summary)
        self.assertNotIn("\t", summary)
        self.assertEqual(summary, "提问：第一行 第二行 带制表符；本轮被中断")

    def test_long_question_is_truncated_to_the_label_bound(self) -> None:
        """★ 兜底标签**自带**上限（与"摘要不设上限"不矛盾，见常量说明）。

        那条裁定管的是**模型写的**摘要（有契约约束它写短）；
        本函数是**我们代写的标签**，没有任何东西约束它。
        """
        summary = interrupted_summary("字" * 5000, "本轮被中断")
        self.assertEqual(
            len(summary), INTERRUPT_SUMMARY_QUESTION_CHARS + len("提问：；本轮被中断")
        )
        self.assertTrue(summary.startswith("提问：字"))
        self.assertTrue(summary.endswith("；本轮被中断"))
        self.assertIn("…", summary, "截断处要有可见标记")

    def test_without_question_only_the_reason_remains(self) -> None:
        """没有提问时不得产出「提问：；xxx」这种空壳。"""
        self.assertEqual(interrupted_summary("", "本轮被中断"), "本轮被中断")
        self.assertEqual(interrupted_summary("   \n ", "本轮被中断"), "本轮被中断")


class DeterministicFallbackTests(unittest.TestCase):
    """第 3 层兜底：不调模型、**永不失败**。"""

    def test_skips_short_headings_and_takes_a_real_sentence(self) -> None:
        """★ D135-4：**必须过滤过短的行**。

        真机实测里本兜底取到了 `"一句话"`（3 字，模型写的小标题）⇒ 摘要字段等于没有信息。
        现在过短的行会被跳过，取到真正那句话。
        """
        message = Message(role="assistant", blocks=[TextBlock(text=f"一句话\n\n{SUMMARY}")])
        self.assertEqual(deterministic_summary(message), SUMMARY)

    def test_falls_back_to_tool_count_when_no_line_is_long_enough(self) -> None:
        message = Message(role="assistant", blocks=[TextBlock(text="结论\n过程")])
        self.assertEqual(deterministic_summary(message, tool_call_count=3), "执行了 3 次工具操作并完成")

    def test_skips_symbol_lines_and_uses_the_type_bound_only(self) -> None:
        """D160：降级摘要**不再硬砍到 60 字**（CHANGE-052 起连上限也没有了）。

        从前这里写死 60，理由是与提示词契约"保持一致"；但那个契约已经不设数字了 ——
        降级路径硬留一个更短的上限，只会让"最后一道防线"比它兜的东西更残缺。
        """
        long_line = "字" * 200
        message = Message(role="assistant", blocks=[TextBlock(text=f"---\n{long_line}")])
        summary = deterministic_summary(message)
        self.assertEqual(summary, long_line, "原样保留")
        self.assertFalse(summary.endswith("..."), "不再加截断省略号")

    def test_no_truncation_at_all(self) -> None:
        """★ CHANGE-052：兜底摘要**也不再截断**（连"安全上限"都没有了）。

        用户裁定：「不要有上限，但是一定要在契约强调要输出简洁的摘要」——
        "简洁"整体转由**契约软约束**承担，不再有系统硬截断。

        ⚠️ 登记风险：本兜底抓的是**正文里的一行**、不受契约约束 ——
        遇到无换行的超长段落会整段返回（该轮几乎没被压缩）。接受，
        因为它只在"模型未按契约给出摘要"时才走到（罕见降级路径）。
        """
        huge = "字" * 5000
        message = Message(role="assistant", blocks=[TextBlock(text=huge)])
        summary = deterministic_summary(message)
        self.assertEqual(summary, huge, "一个字都不许砍")
        self.assertFalse(summary.endswith("..."), "不再加截断省略号")

    def test_never_crashes_on_empty_text(self) -> None:
        message = Message(role="assistant", blocks=[TextBlock(text="   ")])
        self.assertEqual(deterministic_summary(message, tool_call_count=2), "执行了 2 次工具操作并完成")
        self.assertIn("第 3 轮", deterministic_summary(message, turn_index=3))


class ModelFallbackNormalisationTests(unittest.TestCase):
    """第 2 层的返回也要过校验（模型写摘要的执行力并不更好）。"""

    def test_strips_polite_prefix(self) -> None:
        self.assertEqual(normalize_model_summary(f"摘要：{SUMMARY}"), SUMMARY)

    def test_rejects_noise(self) -> None:
        for probe in ("", "   ", "---", "好", "# 标题"):
            with self.subTest(probe=probe):
                self.assertIsNone(normalize_model_summary(probe))

    def test_collapses_multiple_lines_into_one(self) -> None:
        got = normalize_model_summary(f"第一行是摘要主体\n第二行是补充说明")
        self.assertIsNotNone(got)
        assert got is not None
        self.assertNotIn("\n", got)

    def test_transcript_is_bounded(self) -> None:
        messages = [Message(role="user", blocks=[TextBlock(text="字" * 5000)])]
        transcript = render_turn_transcript(messages, per_message_chars=100, total_chars=300)
        self.assertLessEqual(len(transcript), 320)


class KernelPositionContractTests(unittest.IsolatedAsyncioTestCase):
    """内核侧：主路径取末尾行、L2 只在值得时才触发、来源要落进 meta。"""

    async def test_trailing_line_is_used_without_extra_requests(self) -> None:
        """★ 主路径：模型把摘要写在最后一行 ⇒ **零额外请求**、来源标记为 model_last_line。"""
        script = [chunks(*text_chunks(f"正文若干行\n\n{SUMMARY}"))]
        env = install(script)
        turn = await env.kernel.submit("做点事")

        self.assertEqual(turn.turn_summary, SUMMARY)
        self.assertEqual(turn.summary_source, "model_last_line")
        self.assertEqual(len(env.recorder.of("model_request_started")), 1, "主路径不该多发请求")

        assistant = env.kernel.history[-1]
        self.assertEqual(assistant.meta.turn_summary, SUMMARY)
        self.assertEqual(assistant.meta.summary_source, "model_last_line")

    async def test_model_fallback_fires_when_the_tail_is_unusable(self) -> None:
        """★ 第 2 层：末尾行不合规 **且** 这一轮调过工具 ⇒ 再调一次模型补写。

        同时守两条"旁路"约束：**不上屏**（没有额外的 request_started 事件）、
        **不进历史**（历史长度只多了这一轮该有的那几条）。
        """
        script = [
            chunks(*tool_chunks([("c1", "read", {})]), usage_chunk(100, 10)),
            chunks(*text_chunks("好"), usage_chunk(200, 20)),  # 太短 ⇒ 不合规
            chunks(*text_chunks(SUMMARY), usage_chunk(30, 8)),  # ← 补写请求的脚本
        ]
        env = install(script, tools=[StubTool()], model_summary_fallback=True)
        turn = await env.kernel.submit("读一下")

        self.assertEqual(turn.turn_summary, SUMMARY)
        self.assertEqual(turn.summary_source, "model_fallback")
        self.assertEqual(
            len(env.recorder.of("model_request_started")), 2, "补写那次不该出现在事件流里"
        )
        # 补写的用量也要记账（否则费用统计说谎）
        self.assertEqual(turn.usage_total.input_tokens, 100 + 200 + 30)

    async def test_fallback_is_skipped_for_trivial_answers(self) -> None:
        """★ 成本闸门：两字回答且没调工具 ⇒ **不值得**再发一次请求，直接本地生成。"""
        script = [chunks(*text_chunks("好"))]
        env = install(script, model_summary_fallback=True)
        turn = await env.kernel.submit("在吗")

        self.assertEqual(turn.summary_source, "deterministic")
        self.assertEqual(len(env.recorder.of("model_request_started")), 1, "不该为两字回答多发请求")

    async def test_reason_chain_records_which_layer_failed(self) -> None:
        """★★ **D135-4 的核心**：链路诊断必须说清"卡在哪一层"。

        为什么值得一条用例：用户真机上一次 `deterministic` **谁都说不出原因** ——
        L2 的结果（成功/被拒/报错/被闸门跳过）**没有任何痕迹**、日志文件也是空的。
        于是"模型不配合"与"我们拒得太死"这两种完全不同的根因无法区分。
        现在 `summary_reason` 把三层串成一条链。

        ★ CHANGE-052：触发 L1 拒绝的手段从"**超长**（>300 字）"改成"**结构行**"
        —— 上限删除后，超长不再被拒（用户裁定）；而结构判据仍会拒。
        """
        # 末尾是表格行 ⇒ L1 拒（structural）；用了工具 ⇒ L2 触发；补写太短 ⇒ l2_rejected
        script = [
            chunks(*tool_chunks([("c1", "read", {})]), usage_chunk(100, 10)),
            chunks(*text_chunks("表格如下：\n\n| 项 | 值 |"), usage_chunk(200, 20)),
            chunks(*text_chunks("好"), usage_chunk(30, 8)),  # ← 补写太短 ⇒ 被校验拒
        ]
        env = install(script, tools=[StubTool()], model_summary_fallback=True)
        turn = await env.kernel.submit("读一下")
        self.assertEqual(turn.summary_source, "deterministic")
        self.assertEqual(turn.summary_reason, "structural → l2_rejected → l3")
        self.assertEqual(
            env.recorder.find("turn_finished").summary_reason, turn.summary_reason,
            "链路诊断必须落进事件（否则又只能在内存里看一眼）",
        )

    async def test_reason_chain_records_a_timeout(self) -> None:
        """补写抛异常 ⇒ 链路里要写明异常类型（而不是静默降级）。"""
        script = [
            chunks(*tool_chunks([("c1", "read", {})]), usage_chunk(100, 10)),
            # ★ CHANGE-052：用**结构行**（表格）触发 L1 拒绝；超长已不再被拒
            chunks(*text_chunks("表格如下：\n\n| 项 | 值 |"), usage_chunk(200, 20)),
            [RuntimeError("provider 挂了")],
        ]
        env = install(script, tools=[StubTool()], model_summary_fallback=True)
        turn = await env.kernel.submit("读一下")
        # ⚠️ 适配层把厂商错误包成**事件**（`ProviderErrorEvent`）而不是异常 ——
        # L2 一开始没处理它，于是"厂商报错"被**误诊成"补写被校验拒"**。
        # 这条断言同时守住"错误类别要出现在链路里"。
        self.assertIn("l2_error:", turn.summary_reason or "")
        self.assertNotIn("l2_rejected", turn.summary_reason or "", "厂商报错不该被误诊为校验拒绝")

    async def test_long_summary_is_kept_whole_not_truncated(self) -> None:
        """★ **D160 的行为反转**：真机那条 140 字摘要现在**原样收下**（从前被砍到 100）。

        为什么值得一条端到端用例（而不是只测 `extract_trailing_summary`）：
        这是**内核到落盘**的整条链。截断若发生在任何一环（提取 / 打包 / 事件 / 落盘），
        只测纯函数是抓不到的。
        """
        long_summary = "已讲清上下文管理四层与关键取舍，并核出两处断线：校准失效、配置无人读，待你定修哪个" * 3
        # ⚠️ 用 chr(10) 拼换行：在脚本里写 `\n` 会被转义层吃掉（本轮已踩四次）
        body = "正文若干行" + chr(10) * 2 + long_summary
        env = install([chunks(*text_chunks(body))])
        turn = await env.kernel.submit("问一下")
        self.assertEqual(turn.summary_source, "model_last_line")
        self.assertEqual(turn.summary_reason, "ok", "超长摘要不再被标记为截断 —— 因为它不再被截断")
        self.assertEqual(turn.turn_summary, long_summary, "必须逐字保留（含结尾那个「个」）")
        self.assertGreater(len(turn.turn_summary or ""), 100, "该夹具必须长于旧上限，否则守不住回归")

    async def test_deterministic_fallback_when_the_retry_also_fails(self) -> None:
        """★ 第 3 层：连补写都失败（这里让补写请求抛错）⇒ 本地生成，**回合仍然成功**。"""
        script = [
            chunks(*tool_chunks([("c1", "read", {})]), usage_chunk(100, 10)),
            chunks(*text_chunks("好"), usage_chunk(200, 20)),
            [RuntimeError("provider 挂了")],  # ← 补写请求失败
        ]
        env = install(script, tools=[StubTool()], model_summary_fallback=True)
        turn = await env.kernel.submit("读一下")

        self.assertEqual(turn.summary_source, "deterministic")
        self.assertTrue(turn.turn_summary, "第 3 层必须给出一个可用的摘要")
        self.assertEqual(turn.status.value, "done", "摘要失败绝不能把回合搞失败")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()

class TimelineMarkingTests(unittest.TestCase):
    """用户裁定 Q-D 的 UI 侧：兜底摘要在**历史里**要留下说明。"""

    def _rows(self, source: str) -> str:
        from logox.kernel import events as ev
        from logox.tui.content.timeline import TimelineBuffer

        buffer = TimelineBuffer()
        buffer.ingest(
            ev.TurnFinished(
                session_id="s",
                turn=1,
                turn_index=1,
                duration_ms=10,
                tool_call_count=0,
                usage=ev.Usage(input_tokens=1, output_tokens=1),
                reason="completed",
                turn_summary=SUMMARY,
                summary_source=source,
            )
        )
        return chr(10).join(block.text for block in buffer.blocks)

    def test_fallback_summaries_are_annotated(self) -> None:
        """补写 / 自动生成 ⇒ 淡色说明；否则用户会以为那是模型的原话。"""
        for source, expect in (("model_fallback", "由模型补写"), ("deterministic", "由本地自动生成")):
            with self.subTest(source=source):
                self.assertIn(expect, self._rows(source))

    def test_model_written_summaries_are_not_annotated(self) -> None:
        """模型自己写的摘要就显示在正文末尾，再加一行纯属噪声。"""
        for source in ("model_last_line", "model_tag"):
            with self.subTest(source=source):
                rows = self._rows(source)
                self.assertNotIn("本轮摘要", rows, "不该给模型自己写的摘要加说明")



if __name__ == "__main__":  # pragma: no cover
    unittest.main()
