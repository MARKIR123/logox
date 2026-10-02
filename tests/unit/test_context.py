"""M7 上下文管理、记忆体系与换页压缩单元测试 (tests/unit/test_context.py)。"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from logox.context import (
    Compactor,
    HierarchicalContextBuilder,
    SessionTranscriptWriter,
    TokenEstimator,
    estimate_message_tokens,
    estimate_text_tokens,
    find_project_memory,
)
from logox.context.compaction import is_index_message
from logox.kernel import events as ev
from logox.kernel.bus import EventBus
from logox.kernel.loop import KernelLoop
from logox.kernel.messages import (
    Message,
    ReasoningBlock,
    TextBlock,
    ToolResultBlock,
    ToolUseBlock,
)
from logox.kernel.registry import ToolRegistry
from logox.permissions.decider import HierarchicalPermissionDecider
from logox.providers.base import (
    ChatRequest,
    DeltaEvent,
    Provider,
    StopEvent,
    ToolCallEvent,
)
from logox.tools.base import Tool, ToolSpec
from tests.unit.support import make_temp_dir

# =========================================================================== #
# 1. Token 估算器与动态校准测试
# =========================================================================== #

def test_token_estimator_cjk_ascii_weighting():
    # 纯英文字符: 40 个字符 -> 40 * 0.28 = 11.2 -> 11 tokens
    ascii_text = "abcdefghijklmnopqrstuvwxyz0123456789!@#$"
    assert estimate_text_tokens(ascii_text) == 11

    # 纯中文字符: 11 个汉字 -> 11 * 1.0 = 11 tokens
    cjk_text = "这是一段纯中文测试文本"
    assert estimate_text_tokens(cjk_text) == 11

    # 中英混合代码
    mixed_text = "def test_func():\n    # 运行测试\n    return True\n"
    tokens = estimate_text_tokens(mixed_text)
    assert 10 <= tokens <= 25


def test_estimate_message_excludes_reasoning_by_default():
    msg = Message(
        role="assistant",
        blocks=[
            ReasoningBlock(text="这是一段极其漫长的思维链推理过程，耗费大量字数..."),
            TextBlock(text="你好，世界！"),
        ],
    )
    # 默认发往 API 时不计入思考链
    without_reasoning = estimate_message_tokens(msg, include_reasoning=False)
    with_reasoning = estimate_message_tokens(msg, include_reasoning=True)

    assert without_reasoning < with_reasoning
    # "你好，世界！" 6 字符 (6 tokens) + role 4 tokens = 10 tokens
    assert without_reasoning == 10


def test_token_estimator_ema_calibration():
    estimator = TokenEstimator(initial_calibration=1.0)
    assert estimator.calibration_factor == 1.0

    # 假设静态估算 100，实际大模型返回 150 (ratio = 1.5)
    # EMA: factor = (1 - 0.3) * 1.0 + 0.3 * 1.5 = 0.7 + 0.45 = 1.15
    new_factor = estimator.calibrate(estimated_tokens=100, actual_tokens=150, alpha=0.3)
    assert pytest.approx(new_factor, 0.001) == 1.15


# =========================================================================== #
# 2. LOGOX.md 分层长期记忆 (Top-Down 拓扑组装) 测试
# =========================================================================== #


def test_find_project_memory_top_down(tmp_path: Path):
    # 构造目录树结构：
    # tmp_path (Git 根目录) /
    #   ├── .git/
    #   ├── LOGOX.md (Root 记忆)
    #   └── sub_module/
    #       ├── LOGOX.md (Submodule 记忆)
    #       └── cwd/ (当前工作目录)
    git_dir = tmp_path / ".git"
    git_dir.mkdir()

    root_logox = tmp_path / "LOGOX.md"
    root_logox.write_text("Rule 0: Root Global Rule", encoding="utf-8")

    sub_dir = tmp_path / "sub_module"
    sub_dir.mkdir()
    sub_logox = sub_dir / "LOGOX.md"
    sub_logox.write_text("Rule 1: Submodule Specific Rule", encoding="utf-8")

    cwd_dir = sub_dir / "cwd"
    cwd_dir.mkdir()

    # 从最底层的 cwd 开始探测
    memory = find_project_memory(cwd_dir)

    # 必须找到 2 个记忆源
    assert len(memory.sources) == 2

    # 黄金拓扑规则：Root (depth=0) 在最前，Submodule (depth=1) 在最后！
    assert memory.sources[0].depth == 0
    assert memory.sources[0].path == root_logox
    assert "Rule 0" in memory.sources[0].content

    assert memory.sources[1].depth == 1
    assert memory.sources[1].path == sub_logox
    assert "Rule 1" in memory.sources[1].content

    # 渲染 Markdown 检查层级标注
    rendered = memory.render_system_prompt_block()
    assert "Project Root" in rendered
    assert "Submodule Depth 1" in rendered
    # 验证顺从注意力曲线：Root 规则排在前面，Sub 规则排在后面
    assert rendered.index("Rule 0") < rendered.index("Rule 1")


def test_find_project_memory_spec_priority(tmp_path: Path):
    """测试 D119 规范文档优先级：LOGOX.md > AGENTS.md > CLAUDE.md。"""
    git_dir = tmp_path / ".git"
    git_dir.mkdir()

    # 1. 只有 AGENTS.md
    proj_a = tmp_path / "proj_a"
    proj_a.mkdir()
    (proj_a / "AGENTS.md").write_text("Rule from AGENTS.md", encoding="utf-8")
    mem_a = find_project_memory(proj_a)
    assert len(mem_a.sources) == 1
    assert "Rule from AGENTS.md" in mem_a.sources[0].content

    # 2. 只有 CLAUDE.md
    proj_b = tmp_path / "proj_b"
    proj_b.mkdir()
    (proj_b / "CLAUDE.md").write_text("Rule from CLAUDE.md", encoding="utf-8")
    mem_b = find_project_memory(proj_b)
    assert len(mem_b.sources) == 1
    assert "Rule from CLAUDE.md" in mem_b.sources[0].content

    # 3. 三者同时存在，LOGOX.md 优先级最高
    proj_c = tmp_path / "proj_c"
    proj_c.mkdir()
    (proj_c / "LOGOX.md").write_text("Rule from LOGOX.md", encoding="utf-8")
    (proj_c / "AGENTS.md").write_text("Rule from AGENTS.md", encoding="utf-8")
    (proj_c / "CLAUDE.md").write_text("Rule from CLAUDE.md", encoding="utf-8")
    mem_c = find_project_memory(proj_c)
    assert len(mem_c.sources) == 1
    assert "Rule from LOGOX.md" in mem_c.sources[0].content
    assert "Rule from AGENTS.md" not in mem_c.sources[0].content

    # 4. 同时存在 AGENTS.md 和 CLAUDE.md，AGENTS.md 优先于 CLAUDE.md
    proj_d = tmp_path / "proj_d"
    proj_d.mkdir()
    (proj_d / "AGENTS.md").write_text("Rule from AGENTS.md", encoding="utf-8")
    (proj_d / "CLAUDE.md").write_text("Rule from CLAUDE.md", encoding="utf-8")
    mem_d = find_project_memory(proj_d)
    assert len(mem_d.sources) == 1
    assert "Rule from AGENTS.md" in mem_d.sources[0].content
    assert "Rule from CLAUDE.md" not in mem_d.sources[0].content


# =========================================================================== #
# 3. 会话日志追加器与超大工具 Blob 外置存储测试
# =========================================================================== #


def test_session_transcript_writer(tmp_path: Path):
    writer = SessionTranscriptWriter(base_dir=tmp_path, session_id="test_sess_01")

    # 写入 3 行步数
    l1 = writer.write_step(turn=1, step=1, role="user", event_type="user_prompt", content="Hello")
    l2 = writer.write_step(
        turn=1,
        step=2,
        role="assistant",
        event_type="tool_use",
        tool_name="shell",
        call_id="c1",
    )
    l3 = writer.write_step(
        turn=1,
        step=3,
        role="tool",
        event_type="tool_result",
        call_id="c1",
        content="Success",
    )

    assert l1 == 1
    assert l2 == 2
    assert l3 == 3
    assert writer.current_line == 3

    # 测试超大工具结果独立落盘
    huge_output = "Error Traceback Line\n" * 200  # 约 4KB
    blob_path = writer.save_tool_blob("call_huge_01", huge_output)
    assert blob_path is not None
    assert blob_path.startswith("tools/")
    assert blob_path.endswith("/tool_call_huge_01.log")

    # 校验磁盘文件内容
    saved_file = writer.session_dir / blob_path
    assert saved_file.is_file()
    assert saved_file.read_text(encoding="utf-8") == huge_output


# =========================================================================== #
# 4. 双水位线防颠簸与分段页表压缩测试
# =========================================================================== #


def test_compactor_pruning_and_sliding_window(tmp_path: Path):
    """保留窗口之外的旧工具结果被换成**摘要 + 索引**（CHANGE-004）。

    ⚠️ **新规则下"旧"的判据是轮次**：最近 `keep_recent_turns` 轮里的工具结果
    一律**完整保留**（否则会把模型刚拿到、正要用的那几条也剪掉）。
    所以 fixture 必须**多于**保留轮数，才能观察到修剪 —— 之前那份只有 1 轮的
    fixture 在新规则下根本不会触发修剪（保守但正确）。

    这里刻意把"阶段 1 修剪完就够了"的尺寸调出来：修剪后降到低水位之下，
    因此不会再走阶段 2 的折叠 —— 这样才看得到被修剪后的工具块本身。
    """
    writer = SessionTranscriptWriter(base_dir=tmp_path, session_id="compact_sess")
    compactor = Compactor(
        window_capacity=10_000,
        # 用「绝对覆盖」把两条水位线钉死在用例需要的值上
        # （新公式里 high = 窗口 − reserve，测试需要的是精确值）
        max_budget_tokens=3000,     # 高水位强制设为 3000
        target_budget_tokens=1000,  # 低水位强制设为 1000
        keep_recent_tool_results=1,
        keep_recent_turns=2,
        transcript_writer=writer,
    )

    def turn(index: int, *, size: int) -> list[Message]:
        return [
            Message(role="user", blocks=[TextBlock(text=f"第{index}轮提问")]),
            Message(
                role="assistant",
                blocks=[ToolUseBlock(id=f"c{index}", name="fs_read", input={"path": f"{index}.py"})],
            ),
            Message(
                role="tool",
                blocks=[ToolResultBlock(id=f"c{index}", content=chr(65 + index) * size, ok=True)],
            ),
            Message(role="assistant", blocks=[TextBlock(text=f"第{index}轮结论")]),
        ]

    messages = [Message(role="user", blocks=[TextBlock(text="初始核心目标：重构系统")])]
    for index in range(1, 5):
        # 前两轮工具输出很大（会被修剪），后两轮很小（不达修剪阈值）
        messages.extend(turn(index, size=8000 if index <= 2 else 200))

    # ⚠️ **不要用 force=True**：force 的语义是“两个阶段都做”（/compact 就是这样），
    #   它会**跳过“阶段 1 够了就停”的提前返回**。本用例只想看阶段 1。
    #   这里靠 fixture 自然越过高水位（4760 > 3000）。
    result = compactor.compact(messages)

    assert result.pruned_count > 0
    assert result.tokens_after < result.tokens_before

    blocks = {
        b.id: b
        for m in result.messages
        for b in m.blocks
        if isinstance(b, ToolResultBlock)
    }
    # c1 / c2 在保留窗口之外 → 换成「索引 + 摘要节选」
    assert "工具输出已归档" in blocks["c1"].content
    assert "内容节选" in blocks["c1"].content
    assert "行 1~" in blocks["c1"].content
    assert len(blocks["c1"].content) < 8000, "应该变小"
    # c4 是最近一条工具结果 → **必须完整保留**
    assert blocks["c4"].content == "E" * 200
    # 且裁剪后应降到低水位之下 → 阶段 2 不执行（本用例只看阶段 1）
    assert not compactor.epochs, "该 fixture 应当只触发工具修剪，不触发折叠"


def test_monotonic_interval_merging_recompaction(tmp_path: Path):
    """验证二次/多次压缩时，分段页表单调平铺追加，绝无指针套娃。

    ⚠️ `keep_recent_turns` 的语义是**轮次**（CHANGE-002 / D2），不是消息条数。
    下面刻意把每个轮次写成不等长，就是为了不靠"每轮刚好几条"这种巧合。
    """
    compactor = Compactor(
        window_capacity=5000,
        keep_recent_turns=2,
    )

    # 第一次压缩：保留 锚点 + 最近 2 轮，折叠中间的轮 2~轮 3
    m1 = [
        Message(role="user", blocks=[TextBlock(text="初始目标")]),  # 轮 1（锚点）
        Message(role="user", blocks=[TextBlock(text="中间轮次 1")]),  # 轮 2 ┐
        Message(role="assistant", blocks=[TextBlock(text="中间回答 1")]),
        Message(role="user", blocks=[TextBlock(text="中间轮次 2")]),  # 轮 3 ┘
        Message(role="assistant", blocks=[TextBlock(text="中间回答 2")]),
        Message(role="user", blocks=[TextBlock(text="最新提问")]),  # 轮 4（保留）
        Message(role="assistant", blocks=[TextBlock(text="最新回答")]),
        Message(role="user", blocks=[TextBlock(text="最新提问 2")]),  # 轮 5（保留）
        Message(role="assistant", blocks=[TextBlock(text="最新回答 2")]),
    ]
    r1 = compactor.compact(m1, force=True)
    assert len(compactor.epochs) == 1
    assert compactor.epochs[0].epoch_id == 1

    # 模拟继续对话并触发第二次压缩
    m2 = list(r1.messages) + [
        Message(role="user", blocks=[TextBlock(text="中间轮次 3")]),
        Message(role="assistant", blocks=[TextBlock(text="中间回答 3")]),
        Message(role="user", blocks=[TextBlock(text="最新提问 3")]),
        Message(role="assistant", blocks=[TextBlock(text="最新回答 3")]),
    ]
    r2 = compactor.compact(m2, force=True)

    # 必须平铺为 2 个独立的 Epoch，绝不是套娃！
    assert len(compactor.epochs) == 2
    assert compactor.epochs[0].epoch_id == 1
    assert compactor.epochs[1].epoch_id == 2

    # 找到归档索引（用 INDEX_MARKER 定位 —— 单一事实来源；`source=compaction`
    # 在 CHANGE-052 之后**同时标记所有折叠轮摘要**，不再唯一）
    indexes = [
        m for m in r2.messages if is_index_message(m)
    ]
    assert len(indexes) == 1, "索引必须恰好一条"
    # ★ CHANGE-052：轮次区间**不再写进索引**（摘要按轮保留），所以查**账本**
    ranges = [(e.from_turn, e.to_turn) for e in compactor.epochs]
    assert ranges == [(1, 3), (4, 5)], f"两次折叠必须覆盖连续且不重叠的区间，实际 {ranges}"


# =========================================================================== #
# 5. HierarchicalContextBuilder 整体契约测试
# =========================================================================== #


def test_hierarchical_context_builder(tmp_path: Path):
    # 创建假 .git 目录作为项目根节点
    (tmp_path / ".git").mkdir()
    logox_file = tmp_path / "LOGOX.md"
    logox_file.write_text("Docs-First Principle", encoding="utf-8")

    builder = HierarchicalContextBuilder(
        system="You are a helpful coding assistant.",
        cwd=tmp_path,
        session_id="test_builder",
        # D153：writer 现在必填，且**必须显式落点**（绝不写 cwd）
        transcript_writer=SessionTranscriptWriter(
            base_dir=tmp_path / "sessions", session_id="test_builder"
        ),
    )

    history = [
        Message(role="user", blocks=[TextBlock(text="Help me build M7")]),
        Message(
            role="assistant",
            blocks=[
                ReasoningBlock(text="Secret deep thinking"),
                TextBlock(text="Sure thing!"),
            ],
        ),
    ]

    bundle = builder.build(history)

    # 1. 长期记忆必须注入 System Prompt
    assert "Docs-First Principle" in bundle.system
    assert "Project Root" in bundle.system

    # 2. 发给模型的 messages 中，ReasoningBlock 必须被过滤！
    assert len(bundle.messages) == 2
    assistant_blocks = bundle.messages[1].blocks
    assert not any(isinstance(b, ReasoningBlock) for b in assistant_blocks)
    assert any(isinstance(b, TextBlock) for b in assistant_blocks)

    # 3. 记忆源清单必须记录该路径
    assert len(bundle.memory_sources) == 1
    assert "LOGOX.md" in bundle.memory_sources[0]


# =========================================================================== #
# 6. KernelLoop HITL 轮次续期测试
# =========================================================================== #


class _MockContinuationDecider(HierarchicalPermissionDecider):
    def __init__(self, allow_continuation_times: int = 1) -> None:
        super().__init__(wait_headless=False)
        self.allowed_times = allow_continuation_times
        self.ask_count = 0

    async def ask_continuation(self, turn, iteration: int) -> bool:
        self.ask_count += 1
        return self.ask_count <= self.allowed_times


class _LoopbackProvider(Provider):
    name = "mock"

    def __init__(self) -> None:
        self.call_count = 0

    async def stream(self, request: ChatRequest):
        self.call_count += 1
        # 始终请求调用工具 dummy_tool
        yield DeltaEvent(kind="text", text=f"Iteration {self.call_count}")
        yield ToolCallEvent(call_id=f"c_{self.call_count}", name="dummy_tool", arguments={})
        yield StopEvent(stop_reason="tool_use")


class _DummyTool(Tool):
    spec = ToolSpec(name="dummy_tool", description="Dummy", readonly=True)

    async def run(self, args: dict) -> str:
        return "ok"


def test_kernel_loop_hitl_continuation_extended():
    async def _run():
        bus = EventBus(session_id="test_continuation")
        provider = _LoopbackProvider()
        tools = ToolRegistry()
        tools.register(_DummyTool())

        # 允许续期 1 次
        decider = _MockContinuationDecider(allow_continuation_times=1)
        builder = HierarchicalContextBuilder(
            system="sys",
            # D153：用项目认可的临时目录助手（落在 <repo>/.test-tmp/），绝不碰仓库的 .logox
            transcript_writer=SessionTranscriptWriter(
                base_dir=make_temp_dir("hitl-"), session_id="hitl"
            ),
        )

        # max_iterations = 2
        loop = KernelLoop(
            bus=bus,
            provider=provider,
            registry=tools,
            context_builder=builder,
            decider=decider,
            max_iterations=2,
        )

        events_captured = []

        async def _on_event(e):
            events_captured.append(e)

        bus.subscribe("*", _on_event, name="collector")

        # 执行一个必定尝试死循环调用工具的 Turn
        await loop.submit("start infinite loop")

        # 验证：循环初次跑满 2 次，续期 1 次又跑 2 次，总共 4 次后终止
        assert decider.ask_count >= 1
        # 最终必定触发 ErrorOccurred 并标记 failed
        error_events = [e for e in events_captured if isinstance(e, ev.ErrorOccurred)]
        assert len(error_events) == 1
        assert "已停止本回合" in error_events[0].message

    asyncio.run(_run())


def test_sliding_window_compaction_uses_turn_summary():
    """验证滑动窗口折叠时优先汇聚真实的 turn_summary 构建分段表。"""
    from logox.kernel.messages import MessageMeta

    compactor = Compactor(
        window_capacity=1000,
        max_budget_tokens=500,   # 高水位
        target_budget_tokens=300,  # 低水位
        keep_recent_turns=2,
    )

    msgs = [
        Message(role="user", blocks=[TextBlock(text="初始全局大任务")]),  # 轮 1（锚点）
        Message(role="user", blocks=[TextBlock(text="继续下一步")]),  # 轮 2 ┐
        Message(
            role="assistant",
            blocks=[TextBlock(text="步骤 1 完成")],
            meta=MessageMeta(turn_summary="排查了 bus.py 的事件流"),
        ),
        Message(role="user", blocks=[TextBlock(text="再下一步")]),  # 轮 3 ┘
        Message(
            role="assistant",
            blocks=[TextBlock(text="步骤 2 完成")],
            meta=MessageMeta(turn_summary="修复了 normalizer 管道符"),
        ),
        Message(role="user", blocks=[TextBlock(text="最近问题 1")]),  # 轮 4（保留）
        Message(role="assistant", blocks=[TextBlock(text="最近回答 1")]),
        Message(role="user", blocks=[TextBlock(text="最近问题 2")]),  # 轮 5（保留）
        Message(role="assistant", blocks=[TextBlock(text="最近回答 2")]),
    ]

    result = compactor.compact(msgs, force=True)
    assert len(result.epochs) == 1
    epoch = result.epochs[0]
    # ★ D187：账本**只记不可推导的事实** —— 区间与行号；**不记摘要副本**。
    #   下面那两句摘要断言挪到"模型看得到的消息"上（见本用例末尾），
    #   因为那才是摘要的**唯一**载体。留一条守卫防止副本悄悄长回来。
    assert not hasattr(epoch, "summary"), "账本不得再持有摘要副本（双事实来源）"
    assert (epoch.from_turn, epoch.to_turn) == (1, 3), "本次折叠应覆盖第 1~3 轮"

    # ★ CHANGE-052：行号与轮次区间**不再渲染进索引**（摘要按轮保留）——
    #   所以这两条断言改成查**折叠轮自己**：
    #   ① 没接 transcript writer ⇒ 行号必须诚实降级为「（已归档）」，不得写假数字；
    #   ② 每轮摘要必须带上该轮的行号前缀（这里是降级形态）。
    folded = [
        m for m in result.messages
        if m.role == "assistant" and m.meta.source == "compaction"
    ]
    # ★ CHANGE-052：折叠区含**第 1 轮**（锚点特例已删）⇒ 5 轮里折 1~3、保留 4~5
    assert len(folded) == 3, f"应当折出 3 轮（第 1~3 轮），实际 {len(folded)}"
    assert all(m.text.startswith("（已归档）") for m in folded), "无行号时必须降级为（已归档）"
    assert "行号未知" not in " ".join(m.text for m in folded), "不得声称'行号未知'却写在正文里"

    # ★ D187：这两句摘要（原先断言在 `epoch.summary` 上）改查**模型看得到的消息**
    #   —— 账本已不存正文，摘要的唯一载体就是这里。
    folded_text = " ".join(m.text for m in folded)
    assert "排查了 bus.py 的事件流" in folded_text
    assert "修复了 normalizer 管道符" in folded_text



def test_turn_summary_contract_requires_errors_and_fixes():
    """★ CHANGE-005 裁定 6：摘要契约里必须有「错误与修法」这一桶。

    为什么值得一条看护：摘要一旦生成，就会在折叠后**代替原文**。
    而"报错与修法"是最容易在这一步丢掉、却又最需要的信息 ——
    丢了之后，后来的模型会把同一个坑**再踩一遍**。
    """
    from logox.context.builder import TURN_SUMMARY_PROMPT_INSTRUCTION

    text = TURN_SUMMARY_PROMPT_INSTRUCTION
    assert "踩了错" in text, "契约必须显式要求写“错在哪、怎么修的”"
    assert "怎么修" in text
    # ★ D135 第二步：定位机制改成**位置**（最后一行），不再是标签
    assert "最后一行" in text, "契约必须写清「摘要 = 最终答复的最后一行」"
    assert "不加标签" in text
    # ★ D160：定位机制改成**位置**，不再是标签
    assert "字数不设上限" in text, "长度上限必须由用户裁定显式推翻，不能悄悄回来"


def test_turn_summary_contract_demands_density_not_a_char_cap():
    """**D160（用户裁定）**：契约要"尽可能少但说清"，**不要硬字数上限**。

    ⚠️ 这条守卫**推翻**了它自己的上一版 —— 上一版写的是
    ``assert "60 字" in text, "长度上限不能被这次改动悄悄放宽"``。

    为什么推翻（实测驱动，37 条真实摘要）：

    * **49%（18 条）超过 60 字** —— 那个上限近一半没被遵守，说明它与
      "产物 + 踩坑修法 + 待办"这三项内容要求**互相矛盾**；
    * **27%（10 条）以「…」结尾** —— 被 `SUMMARY_MAX_CHARS=100` 截断过；
    * 而**截断砍掉的永远是尾部**，契约恰恰要求把"踩坑与待办"放在尾部
      ⟹ **系统性地删掉最有价值的那部分**。

    用户原话："每轮做的事就是有多有少，我们应该限制模型在尽可能少的文本下把话说清楚。"
    ⟹ 短由**契约**管（可被遵守），长由**类型判定**管（不丢信息）。
    """
    from logox.context.builder import TURN_SUMMARY_PROMPT_INSTRUCTION

    text = TURN_SUMMARY_PROMPT_INSTRUCTION
    # ① 旧口径必须消失：不许再出现具体字数
    assert "60 字" not in text, "旧的硬字数上限必须彻底移除（含示范与兜底口径）"
    # ② 新口径必须在：密度原则 + 自检方法
    assert "尽可能少的字" in text, "必须给出密度要求（而不是字数）"
    assert "删掉" in text and "还明白" in text, "必须给一个可执行的**自检方法**"
    # ③ 优先级必须在：空间不够时从下往上砍
    assert "优先级" in text, "必须写明内容优先级（否则模型无法在变长时取舍）"
    for marker in ("①", "②", "③", "④"):
        assert marker in text, f"优先级第 {marker} 档缺失"
    # ④ 最该保留的那一条：产物永不省略
    assert "永不省略" in text, "「产物」是最高优先级，必须显式写"
    # ⑤ ★ D160 后续：必须说清"段落 ≠ 一行"
    #
    # 用户第二次报"摘要又出现了莫名其妙的换行" ⇒ 说明"一行"这个词**误导了读的人**
    # （他以为换行 = 数据坏了）。而 300 字的上限在 100 列终端上**物理上不可能是"一行"**。
    # 所以措辞必须改成"一个段落"，并**明确告诉模型折行是正常的** ——
    # 否则模型会为了"挤进一行"而删掉该说的话。
    assert "一个段落" in text, "必须改成「段落」（『一行』在物理上做不到）"
    assert "折成好几行" in text or "正常的显示效果" in text, (
        "必须显式说明：屏幕上的折行是正常显示，不是错误"
    )


def test_turn_summary_contract_示例覆盖了错误与修法():
    """示范里要真的有一条带“踩坑 + 修法”的样例 —— 只写规则不给例子，模型多半不照做。"""
    from logox.context.builder import TURN_SUMMARY_PROMPT_INSTRUCTION

    # ★ D135 第二步：示范不再是「标签行」，而是「正文若干行 + 最后一行摘要」里的**那一行**
    # 示范区里「摘要行的缩进」比标签行深一级（标签 3 空格、摘要 5 空格）——
    # 这个缩进差就是"摘要 = 最后一行"的可解析信号，用它取示例而不是靠关键词猜。
    examples = [
        line.strip()
        for line in TURN_SUMMARY_PROMPT_INSTRUCTION.splitlines()
        if line.startswith("     ")
        and line.strip()
        and not line.strip().startswith(("……", "---"))
    ]
    assert len(examples) >= 3, f"示范少了，实际 {len(examples)} 条"
    assert any("修好" in line or "修复" in line for line in examples), (
        f"示范里必须有一条“踩坑 + 修法”，实际：{examples}"
    )
