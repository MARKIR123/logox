"""分层上下文构建器 (HierarchicalContextBuilder)。

实现内核 ContextBuilder 协议契约：
1. 载入分层项目记忆 (LOGOX.md) 并 Top-Down (Root -> Cwd) 注入系统人设；
2. 发往模型 API 时源头过滤 ReasoningBlock（零思考链 Token 消耗）；
3. 遵循高低水位线执行无损换页压缩 (Tool Pruning + Sliding Window)；
4. 动态结合大模型真实 Usage 进行 Token 估算自适应校准。
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, List, Optional

from logox.context.compaction import CompactionResult, Compactor
from logox.context.memory import ProjectMemory, find_project_memory
from logox.context.storage import SessionTranscriptWriter
from logox.context.tokens import TokenEstimator, TokenLedger
from logox.kernel.events import Usage
from logox.kernel.loop import CompactionReport, ContextBundle
from logox.kernel.messages import Message, ReasoningBlock

logger = logging.getLogger(__name__)

__all__ = ["HierarchicalContextBuilder"]


TURN_SUMMARY_PROMPT_INSTRUCTION = """## 回合交付与摘要规范 (Turn Summary Contract)
当且仅当你已完成本轮所有工具调用、准备输出最终答复（Final Answer）时，必须在**最终答复的最后一行**写一句摘要。
它就是正文的最后一行：不加标签、不加前缀、不加引号，前后也不需要任何分隔线。

这段摘要会被多处复用：对话过长时，中间轮次的完整过程会被丢弃、只留摘要；
它也会出现在会话列表与回滚列表里给用户看 —— 同时它还显示在对话末尾，所以别写“无”这类空话。
因此它要同时做到两件事 —— 说清这一轮做了什么，并且让人一眼读懂。

1. 位置（严格执行）：
   - ✅ **整条回复的最后一行**，而且最后一个字符就是它的末尾。摘要之后不得再有任何内容 ——
     包括空行、`---` 分隔线、代码块或表格。
   - ✅ 只出现在最终答复里（即这一步不再发起任何工具调用）。
   - ❌ 不要写在正文中间，也不要在调用工具的中间步骤里写。
   - ❌ 不要用 `<turn_summary>` 这类标签 —— 那只是普通正文，不会被识别。

2. 格式与长度：**一个段落**（它必须占据正文末尾的**连续若干行**，不可换行）。
   **参考字数在200字左右，无硬性规定** —— 因为每一轮做的事有多有少。但"不设上限"**不等于**"可以啰嗦"：
   「**用尽可能少的字把话说清楚**」是硬要求。自检方法：
   删掉某个短语之后，读的人还明白这一轮发生了什么吗？
   还明白 → 它本来就多余，不该写；不明白 → 它必须留着。

3. 内容（**优先级从高到低；空间不够时，从下往上砍**）：
   - ① **产物**：文件名 / 函数名 / 类名 / 命令 / 报错关键字 —— **永不省略**。
     不要写"优化了代码""处理了一下"这类看不出结果的表述。
   - ② **踩了错就要写修法**：错在哪、怎么修的。这一条为什么排这么前：
     摘要会自动代替被丢弃的原文，而**报错与修法是最容易在这一步丢掉、却又最需要的东西** ——
     写下来的话，后来的模型就不会把同一个坑再踩一遍。
   - ③ **待办、风险、未验证项**：有就在句末补半句；没有就不写，不要为了凑格式写"无"。
   - ④ **过程叙述**（"读了 A 又读了 B"）：**永远不写** —— 写结论，不写过程。
     写"确认 X 不是原因"，不写"排查了 X"。

4. 示范（**摘要 = 最后一行**）：
   改代码：
     ……正文若干行……
     改 fs_edit.py 的空白归一化匹配并补 3 条用例；BOM 分支仍无用例
   含踩坑与修法：
     ……正文若干行……
     修好 resume 后摘要丢失：回刻到 assistant 的 meta；另发现双索引回归已一并修掉
   纯排查：
     ……正文若干行……
     确认 bus.py 的事件分发没有阻塞 sleep，瓶颈在 metrics 归约
   概念问答：
     ……正文若干行……
     讲清了分页换入机制与高低水位防颠簸的取舍
   ❌ 反例（末尾不是摘要，等于没写）：
     ……正文若干行……
     ---
     本轮改了 fs_edit.py"""


def _folded_head(messages: list[Message]) -> list[Message]:
    """折叠后的**头部**：从第 1 条到**归档索引**为止（含索引）。

    F-53 的教训：这段头部的长度是**可变**的（CHANGE-052 起 = ``[user, 摘要] × N + [索引]``），
    所以调用方**不能**用 `[:2]` 这种硬编码下标 —— 一旦形态变了，多出来的那些就被
    悄悄丢掉，而模型从此看不到"中间历史被折叠过、原文在哪、怎么按行号读回来"。

    ★ CHANGE-052：判据交给 `compaction.is_index_message()` ——
    **"文本以标记开头"**，不是"包含"。理由见那里的注释：新结构逐字保留用户提问，
    而用户/助手引用文档时正文里**很容易出现**这个词；用"包含"判定会提前截断头部，
    让缓存前缀不完整（⇒ `covered` 前进过多 ⇒ 中间折叠对被静默丢掉）。

    防御：万一没找到索引（理论上折叠成功就一定有），**只取第一条** ——
    宁可少缓存（下次重算，只是慢一点）也不能错配（那会变错）。
    """
    from logox.context.compaction import is_index_message

    for index, message in enumerate(messages):
        if is_index_message(message):
            return list(messages[: index + 1])
    return list(messages[:1])


@dataclass
class _FoldCache:
    """压缩缓存。**可以被随时丢掉** —— 丢了只是变慢，不会变错。

    :param covered: ``history`` 中前多少条已被 ``prefix`` 代表（单调不减）
    :param prefix: 已折叠部分的表示，固定为 ``[锚点, 锚点回答摘要, 归档索引]``（初始为空，共 3 条）
        —— 与 ``compaction.py`` 的 ``_head_anchor() + [_render_index()]`` 严格对应（F-53 修复后）
    :param refs: ``history[:covered]`` 的**对象引用**，用于自校验。
        存引用而不是哈希：``Message`` 没有 id 字段，而 ``id()`` 有地址复用风险；
        同时持引用还能防止这些对象被回收后地址被复用。
    """

    covered: int = 0
    prefix: list[Message] = field(default_factory=list)
    refs: list[Message] = field(default_factory=list)

    def is_valid_for(self, history: list[Message]) -> bool:
        """``history`` 是否仍是当初被折叠的那一份（逐条**身份**比对，O(covered)）。"""
        if not self.prefix:
            return True
        if len(history) < self.covered:
            return False
        return all(history[index] is self.refs[index] for index in range(self.covered))



#: 打开压缩上下文转储的环境变量（dev 用）。
#: 为什么用环境变量而不是配置项：`[context]` 的配置字段目前**压根没接进 builder**
#: （见 CHANGE-017），走配置文件会得到"设了没反应"的困惑；环境变量与既有的
#: `LOGOX_ALLOW_UNSAFE_TOOLS` 同风格，且天然只影响开发会话。
DUMP_COMPACTION_ENV = "LOGOX_DUMP_COMPACTION"


def dump_compaction_enabled() -> bool:
    """是否开启压缩上下文转储（环境变量 ``LOGOX_DUMP_COMPACTION=1``）。"""
    return os.environ.get(DUMP_COMPACTION_ENV, "").strip().lower() in ("1", "true", "yes", "on")



def _prefix_digest(system_prompt: str) -> str:
    """系统提示的指纹（D158）。

    锚点覆盖的是「系统提示 + 工具 schema + 到第 N 条消息为止的对话」。
    系统提示变了（例如项目记忆刷新），锚点描述的那段前缀就不是现在这段了 ⇒ 必须作废。

    ⚠️ **已知局限（登记，不假装解决）**：工具集在**会话中途**变化（如 MCP 动态装载）时，
    这里察觉不到。影响面被两点限制住：① 本项目的工具集在启动期装载，会话中途基本不变；
    ② 锚点每次请求都会被新的实测值**重建**，所以偏差最多影响一次预测。
    Pi 的做法是记录 `addedToolNames` 并补算新增工具的 schema —— 需要时再按那条路补齐。
    """
    import hashlib

    return hashlib.sha256(system_prompt.encode("utf-8", errors="replace")).hexdigest()[:16]


class HierarchicalContextBuilder:
    """工业级分层上下文构建器。"""

    def __init__(
        self,
        system: str = "",
        *,
        cwd: str | Path | None = None,
        session_id: str = "default_session",
        window_capacity: int = 128_000,
        # ★ CHANGE-005 裁定 1：阈值改成「窗口 − reserve」。
        #   `reserve` 是给「回答 + 增量估算误差」留的空间，不是成本偏好。
        #   默认 32,768 = 默认 max_tokens(16,384) + 等量余量。
        reserve_tokens: int = 32_768,
        #: 低水位比例（修剪后降到窗口的这个比例以下即算完成）
        low_watermark_ratio: float = 0.50,
        # ★ 默认 None = **不封顶**（CHANGE-004 / 用户裁定）：
        #   旧默认 80_000 会把 1M 窗口的压缩触发点压到 8%。
        #   它们保留为**可选**的成本闸门，由关心花费的用户显式打开。
        max_budget_tokens: int | None = None,
        target_budget_tokens: int | None = None,
        keep_recent_turns: int = 2,
        keep_recent_tool_results: int = 2,
        # ★ 压缩后重读工作集（CHANGE-005 裁定 4）——详见 `Compactor`
        rehydrate_files: int = 5,
        rehydrate_max_chars: int = 2000,
        # ★ D153：**必填**。以前这里是 `= None` + 下面那句 `or SessionTranscriptWriter(...)` 兜底，
        #   而兜底用的默认值又是相对路径 `.logox/runs` —— 于是"忘了传"既不报错也不打日志，
        #   只是在当前工作目录里悄悄多一个目录。库类**不该在没被告知时自己创建文件**。
        transcript_writer: SessionTranscriptWriter,
        #: ★ D157：关掉则**完全不读**项目 `LOGOX.md`（既不进 system，也不列来源）
        project_memory_enabled: bool = True,
        #: ★ D158：κ 的分桶键（`f"{provider}/{model}"`）。`/model` 切换后由装配根更新。
        model_key: str = "",
        #: ★ D156：dev 期把每次"真的发生了压缩"的上下文转储到磁盘（默认跟随环境变量）
        dump_compaction: bool | None = None,
        estimator: Optional[TokenEstimator] = None,
        skill_manager: Any = None,
    ) -> None:
        self.base_system = system
        self.skill_manager = skill_manager
        self.cwd = Path(cwd).resolve() if cwd else Path.cwd().resolve()
        self.session_id = session_id
        self.window_capacity = window_capacity

        self.estimator = estimator or TokenEstimator()
        self.model_key = model_key
        #: ★ D158：锚点账本（精确锚点 + 只估增量）。与 estimator 一样**跨 build 存活**。
        self.ledger = TokenLedger()
        self.writer = transcript_writer
        #: 压缩上下文转储开关与序号（D156）
        self.dump_compaction = dump_compaction_enabled() if dump_compaction is None else dump_compaction
        self._dump_seq = 0

        self.compactor = Compactor(
            window_capacity=window_capacity,
            reserve_tokens=reserve_tokens,
            low_watermark_ratio=low_watermark_ratio,
            max_budget_tokens=max_budget_tokens,
            target_budget_tokens=target_budget_tokens,
            # ⚠️ 两个“保留”的语义不同，不要混：
            #   `keep_recent_turns`        —— 保留多少个**完整轮次**（对话层）
            #   `keep_recent_tool_results` —— 全局保留最近多少**条工具结果**（工具层）
            keep_recent_turns=keep_recent_turns,
            keep_recent_tool_results=keep_recent_tool_results,
            rehydrate_files=rehydrate_files,
            rehydrate_max_chars=rehydrate_max_chars,
            estimator=self.estimator,
            transcript_writer=self.writer,
            model_key=model_key,
        )

        self.project_memory_enabled = project_memory_enabled
        #: 缓存的项目级记忆（启动时自动扫描一次）；开关关闭时是空记忆
        self.memory: ProjectMemory = (
            find_project_memory(self.cwd) if project_memory_enabled else ProjectMemory(sources=[], total_tokens=0)
        )
        self._last_pruned_count = 0
        #: 压缩缓存（D1/D3）—— 见 :meth:`_assemble` 与 :meth:`_absorb_fold`
        self._cache = _FoldCache()

    def set_model(self, *, model_key: str, window_capacity: int | None = None) -> None:
        """切换模型时更新**计量侧**的三样东西（D159）。

        `/model` 只改内核的 `_model` 是不够的：

        * `window_capacity` 变了 ⇒ 水位线要重算（换到更小窗口的模型后，
          旧窗口算出的高水位会高于真实窗口 ⇒ 压缩永不触发 ⇒ 请求直接撞 400）；
        * `model_key` 变了 ⇒ κ 要换桶（不能拿别的模型学到的偏差修正新模型），
          同时**锚点自动失效**（`predict()` 比 `model_key`，不靠人记得重置）。

        ``window_capacity=None`` 表示"查不到新窗口"（自定义模型名）⇒ 沿用旧窗口，
        只换 κ 桶与锚点，并在日志里留一句。
        """
        self.model_key = model_key
        self.compactor.model_key = model_key
        if window_capacity is not None and window_capacity > 0:
            self.window_capacity = window_capacity
            self.compactor.set_window_capacity(window_capacity)
        else:
            logger.debug("模型 %s 未查到上下文窗口，沿用当前的 %s", model_key, self.window_capacity)

    def refresh_memory(self) -> ProjectMemory:
        """重新扫描并刷新当前工作区的 LOGOX.md 记忆（开关关闭时保持空记忆）。"""
        self.memory = (
            find_project_memory(self.cwd)
            if self.project_memory_enabled
            else ProjectMemory(sources=[], total_tokens=0)
        )
        return self.memory

    def record_usage(self, estimated_tokens: int, usage: Usage) -> None:
        """在收到大模型返回时，利用真实的 input_tokens 动态校准估算器。"""
        if usage.input_tokens > 0 and estimated_tokens > 0:
            new_factor = self.estimator.calibrate(
                estimated_tokens=estimated_tokens,
                actual_tokens=usage.input_tokens,
            )
            logger.debug("Token 估算动态校准系数更新为: %.3f", new_factor)

    def _assemble_system_prompt(self) -> str:
        """组装顶层系统人设：基础人设 + LOGOX.md 长期记忆 + 技能包渐进索引 + 回合摘要契约。"""
        parts = [self.base_system.rstrip()] if self.base_system.strip() else []
        memory_block = self.memory.render_system_prompt_block()
        if memory_block:
            parts.append(memory_block.strip())

        if self.skill_manager is not None and hasattr(self.skill_manager, "build_prompt_index"):
            skills_block = self.skill_manager.build_prompt_index()
            if skills_block:
                parts.append(skills_block.strip())

        # 注入回合摘要规范约束（末尾强注意力区间）
        parts.append(TURN_SUMMARY_PROMPT_INSTRUCTION.strip())

        if not parts:
            return ""
        return "\n\n".join(parts) + "\n"

    def build(self, history: list[Message], *, last_usage: Usage | None = None) -> ContextBundle:
        """组装供大模型调用的标准上下文包（带压缩缓存，D1/D3）。

        ``last_usage`` 是**上一次请求厂商上报的真实用量**（CHANGE-005 裁定 1），
        用来做压缩触发判据 —— 见 ``Compactor.compact``。
        """
        return self._assemble(history, force=False, last_usage=last_usage)

    def force_compact(
        self, history: list[Message], *, last_usage: Usage | None = None
    ) -> ContextBundle:
        """手动强制执行一次上下文修剪压缩 (对应 /compact 命令)。"""
        return self._assemble(history, force=True, last_usage=last_usage)

    def _assemble(
        self, history: list[Message], *, force: bool, last_usage: Usage | None = None
    ) -> ContextBundle:
        """``build`` 与 ``force_compact`` 的**唯一实现**（单一事实来源）。

        缓存的工作方式（D1）：``history`` 是**全量只增**的源记录，而视图是
        ``缓存前缀 + history[游标:]``。折叠只把"游标之后"的部分吃进前缀，
        因此**同一段历史不会被反复折叠**——这正是页表不再重复堆积的原因。
        """
        full_system = self._assemble_system_prompt()

        # ★ 缓存自校验（D3）：history 被整体替换（/resume、/rewind、/new）时必须认出来。
        #   刻意**不**依赖"记得调 reset"——那要求三条路（以及将来任何新路径）都记得，
        #   而这个项目刚因为"忘了调第二次"踩过坑（prepare_runtime vs build_runtime）。
        #   自校验的失败方向是安全的：认不出来就**重算**（变慢），
        #   而不是拿旧的折叠前缀去配新的历史（变错）。
        if not self._cache.is_valid_for(history):
            self._invalidate_cache()

        view = self._cache.prefix + history[self._cache.covered:]

        # ⚠️ **必须无条件调用 compact()**，不能"先估算、再决定要不要调"。
        #   compact() 除了折叠，还负责 `_strip_reasoning` —— 把思考链在发往模型前
        #   **源头剔除**。那是省钱与兼容的硬要求；跳过它 = 把思考链发给厂商。
        #   （本次重构的第一版就是这么写的，被既有的
        #    `test_hierarchical_context_builder` 当场抓住。）
        #   水位线判定本来就在 compact() 内部，这里不应重复实现一遍。
        # ★ D158：先**对账**（用上一次请求的实测用量重建锚点 + 校准 κ），再**预测**。
        #   `_prepare()` 与 `plan()` 共用 —— 保证"内核在动手前拿到的数字"与
        #   "真正驱动压缩判据的数字"**是同一个**（否则事件里报的数会与分析结果不一致）。
        prediction = self._prepare(view, full_system, last_usage=last_usage)

        result = self.compactor.compact(
            view,
            force=force,
            system_prompt=full_system,
            last_usage=last_usage,
            # 锚点式判据（精确锚点 + 只估增量）——取代旧的 max(估算, 真实)（D158）
            current_tokens=prediction.tokens,
        )
        self._absorb_fold(result, history)
        self._last_pruned_count = result.pruned_count
        # ★ D158：记下"这一次实际发出多少条、预测多少 token"——
        #   下一次 reconcile 靠它确定锚点覆盖范围，并把它当作**校准分母**
        #   （校准目标必须对齐：分母要是"对实际发出那份列表的预测"）。
        self.ledger.note_build(
            sent_count=len(result.messages), predicted_tokens=result.tokens_after
        )

        memory_paths = [str(s.path).replace("\\", "/") for s in self.memory.sources]

        # ★ D156：把"压缩到底做了什么"翻译成**内核侧的中性报告**（纯数据，不做 IO）。
        #   内核拿到它才会发布 `CompactionFinished` —— 在补上这条线之前，
        #   时间线提示 / `compact_count` / `pre_compact` 钩子 / 事后查证四条线全是死的（F-54）。
        folded = result.folded_from_index > 0
        report: CompactionReport | None = None
        if folded or result.pruned_count:
            report = CompactionReport(
                tokens_before=result.tokens_before,
                tokens_after=result.tokens_after,
                message_count_before=len(view),
                message_count_after=len(result.messages),
                pruned_count=result.pruned_count,
                folded_turns=result.folded_turns,
                strategy="prune+fold" if folded else "prune",
                degraded=result.degraded,
            )
            # dev 期转储：**只在真的发生了压缩时**写（避免噪音）
            self._dump_compaction_context(
                before=view,
                after=result.messages,
                system=full_system,
                report=report,
                epochs=list(self.compactor.epochs),
            )

        return ContextBundle(
            system=full_system,
            messages=result.messages,
            token_estimate=result.tokens_after,
            memory_sources=memory_paths,
            pruned_count=result.pruned_count,
            compaction=report,
        )

    # -- dev 期压缩转储（D156） -------------------------------------------- #

    def _dump_compaction_context(
        self,
        *,
        before: list[Message],
        after: list[Message],
        system: str,
        report: CompactionReport,
        epochs: list[Any],
    ) -> None:
        """把"压缩前 / 压缩后"的完整上下文写到会话目录下的 `compaction/`（dev 调试用）。

        为什么要有它（用户裁定）：压缩是**不可逆**的——一旦发现"模型丢了某个关键约束"，
        没有现场就只能靠猜。留一份现场，就能直接回答三个问题：
        ① 压缩前是多少、压掉多少；② 压缩后模型**实际看到**的消息序列长什么样；
        ③ 归档索引里给模型的行号，去 `transcript.jsonl` 里读回来的是不是那段内容。

        落点：``<会话目录>/compaction/<序号>-<时间戳>.md`` 与同名 ``.json``
        （会话目录 = ``transcript.jsonl`` 所在目录 ⇒ 跟着会话走、好找）。

        原子写：先写 ``.tmp`` 再替换 —— 与 ``state.toml`` 同款，避免留下半截文件。
        """
        if not self.dump_compaction:
            return
        import json
        import time

        try:
            target_dir = Path(self.writer.session_dir) / "compaction"
            target_dir.mkdir(parents=True, exist_ok=True)
            self._dump_seq += 1
            stamp = time.strftime("%Y%m%dT%H%M%S")
            stem = f"{self._dump_seq:04d}-{stamp}"

            lines = [
                f"# 压缩现场 {stem}",
                "",
                "| 项 | 值 |",
                "|---|---|",
                f"| 策略 | `{report.strategy}` |",
                f"| tokens | {report.tokens_before:,} → {report.tokens_after:,} |",
                f"| 消息条数 | {report.message_count_before} → {report.message_count_after} |",
                f"| 修剪工具结果 | {report.pruned_count} 条 |",
                f"| 折叠轮数 | {report.folded_turns} |",
                f"| 降级（兜底摘要） | {report.degraded} |",
                "",
                "## 折叠账本（epochs）",
                "",
            ]
            for epoch in epochs:
                lines.append(
                    f"- 第 {epoch.from_turn}~{epoch.to_turn} 轮 · 行 {epoch.start_line}~{epoch.end_line}"
                    f" · {epoch.summary}"
                )
            lines += ["", "## system（逐字）", "", "```text", system.rstrip(), "```", ""]
            lines += ["## 压缩后模型实际看到的消息", ""]
            for index, message in enumerate(after):
                lines.append(f"### [{index}] {message.role}（source={message.meta.source}）")
                if message.meta.turn_summary:
                    lines.append(f"> turn_summary: {message.meta.turn_summary}")
                for block in message.blocks:
                    text = getattr(block, "text", None)
                    if text is None:
                        text = getattr(block, "content", "")
                    kind = type(block).__name__
                    lines += ["", f"``{kind}``", "", "```text", str(text).rstrip(), "```"]
                lines.append("")
            self._atomic_write(target_dir / f"{stem}.md", "\n".join(lines))

            payload = {
                "report": report.model_dump(),
                "epochs": [epoch.__dict__ for epoch in epochs],
                "before": [m.model_dump(mode="json") for m in before],
                "after": [m.model_dump(mode="json") for m in after],
            }
            self._atomic_write(
                target_dir / f"{stem}.json",
                json.dumps(payload, ensure_ascii=False, indent=2, default=str),
            )
            logger.debug("已转储压缩现场：%s", target_dir / stem)
        except Exception as exc:  # noqa: BLE001 - 调试设施**绝不能**影响正常压缩
            logger.warning("压缩现场转储失败（不影响压缩本身）：%s", exc)

    @staticmethod
    def _atomic_write(path: Path, text: str) -> None:
        """先写 ``.tmp`` 再替换（避免半截文件被后续工具读到）。"""
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(text, encoding="utf-8", newline="")
        tmp.replace(path)


    def _prepare(self, view: list[Message], full_system: str, *, last_usage: Usage | None = None):
        """对账 + 预测（`plan()` 与 `_assemble()` 共用，保证两者数字一致）。

        对账（`reconcile`）是**幂等**的：它用"对象身份"跳过已消费过的用量，
        所以 `plan()` 先跑一次、`_assemble()` 再跑一次不会重复校准，也不会重复建锚点。
        """
        digest = _prefix_digest(full_system)
        reconcile_reason = self.ledger.reconcile(
            usage=last_usage,
            estimator=self.estimator,
            model_key=self.model_key,
            prefix_digest=digest,
        )
        prediction = self.ledger.predict(
            view,
            system_prompt=full_system,
            estimator=self.estimator,
            model_key=self.model_key,
            prefix_digest=digest,
        )
        logger.debug(
            "上下文计量：%s / reconcile=%s / %s tokens",
            prediction.reason,
            reconcile_reason,
            prediction.tokens,
        )
        return prediction

    def plan(
        self, history: list[Message], *, last_usage: Usage | None = None, force: bool = False
    ):
        """预测"这次组装会不会发生压缩"（D167，供 `pre_compact` 钩子）。

        **只读语义**：不动历史、不改缓存；只有账本的对账是幂等的（见 `_prepare`）。
        成本：一次估算遍历（O(字符数)）—— 换来的是钩子能在**改动历史之前**介入。

        ``force=True``（手动 `/compact`）时判据换成"**有没有可做的活**"：
        手动压缩有意跳过水位线，所以不能再用 `should_compact` 判断 ——
        否则手动路径永远不会发 `CompactionStarted`，钩子在那条路径上失效。
        """
        from logox.kernel.loop import CompactionPlan

        full_system = self._assemble_system_prompt()
        if not self._cache.is_valid_for(history):
            self._invalidate_cache()
        view = self._cache.prefix + history[self._cache.covered:]
        prediction = self._prepare(view, full_system, last_usage=last_usage)
        if force:
            if not self._has_work_todo(view):
                return None
        elif not self.compactor.should_compact(prediction.tokens):
            return None
        return CompactionPlan(
            tokens_before=prediction.tokens, message_count_before=len(view)
        )

    def _has_work_todo(self, view: list[Message]) -> bool:
        """`force`（手动压缩）时"有没有可做的活"：可折叠的轮次 或 可归档的超长工具结果。

        为什么需要它：手动压缩**有意跳过水位线**，所以"会不会做事"不能再由水位线推断；
        没有这个判断，`/compact` 就不会发 `CompactionStarted`（钩子失效），
        或者反过来 —— 明明无事可做却发了个"开始"。
        """
        from logox.context.compaction import PRUNE_MIN_CHARS, split_turn_spans

        if len(split_turn_spans(view)) > 1 + self.compactor.keep_recent_turns:
            return True
        for message in view:
            for block in message.blocks:
                content = getattr(block, "content", None)
                if (
                    getattr(block, "archived", False) is False
                    and isinstance(content, str)
                    and len(content) > PRUNE_MIN_CHARS
                    and type(block).__name__ == "ToolResultBlock"
                ):
                    return True
        return False

    def _invalidate_cache(self) -> None:
        """丢弃缓存并清空页表账本。**必须是唯一的失效入口。**

        ★ D158：锚点也在这里作废（代际 +1）—— 锚点绑定的是"当时发出去的那段文本"，
        历史一旦重写，它描述的就是不存在的上下文（**不报错、只会算错**）。

        两件事必须一起做：``epochs`` 就是"这一段已折叠区间"的账本，
        只清缓存不清账本，页表里会留下**指向已不存在区间**的陈条目
        （新增 T-3 就是抓这个：删掉下面这行 `compactor.reset()` 会立刻变红）。
        """
        self._cache = _FoldCache()
        self.compactor.reset()
        self.ledger.note_rewrite()

    def _absorb_fold(self, result: CompactionResult, history: list[Message]) -> None:
        """把一次成功的折叠记进缓存：前缀 = ``[锚点, 锚点回答摘要, 归档索引]``，游标前推（D1）。

        推导：视图 = ``前缀 + history[covered:]``，记 ``P = len(前缀)``。
        折叠边界 ``tail_start`` 满足 ``tail_start >= P``（占位符落在锚点那一轮里），
        所以被吃掉的 history 部分恰好是 ``history[covered : covered + tail_start - P]``。
        """
        if result.folded_from_index <= 0:
            return  # 只做了工具修剪，没有折叠 —— 缓存不动

        # ★ D161（bug 修复）：折叠**重写了历史前缀** ⇒ 锚点必须作废。
        #   此前只有 `_invalidate_cache()` 会推进代际，而正常折叠不经过它 ⇒
        #   折叠之后那个锚点描述的是**已经不存在的列表**（它的前 sent_count 条已被换成
        #   [锚点, 摘要, 索引]），下一次预测会因此悄悄偏掉 —— 而且不报错。
        self.ledger.note_rewrite()
        consumed = result.folded_from_index - len(self._cache.prefix)
        if consumed < 0:  # pragma: no cover - 防御：边界不得落进前缀内部
            self._invalidate_cache()
            return
        covered = self._cache.covered + consumed
        self._cache = _FoldCache(
            covered=covered,
            # ★ F-53 修复（D156）：前缀 = **取到「归档索引」为止的头部**，而不是写死的 `[:2]`。
            #
            #   为什么不能写死条数：折叠后的头部长度**本来就是可变的** ——
            #     · 生产常态（助手消息带 `turn_summary`，D135）：`[锚点 user, 摘要 assistant, 归档索引]` = 3 条
            #     · 锚点轮没有摘要时：`[锚点 user, 归档索引]` = 2 条
            #     · 锚点那一轮有多条消息时：更长
            #   旧的 `[:2]` 在**生产形态下恰好把索引切掉**，而 `_render_index()` 全项目
            #   只有折叠那一处的调用点 ⇒ 从**第二次请求起**，被折叠的中间历史在模型眼里
            #   彻底消失（没有原文、没有摘要、也没有"可按行号 fs_read"的指引）。
            #   实测症状：折叠那次 9 条消息、下一次 8 条，差异正是那条索引。
            #   而单测没抓到，是因为**测试 fixture 的助手消息没有 `turn_summary`** ⇒
            #   头部恰好是 2 条，`[:2]` "碰巧"对上了（fixture 保真度问题，已补生产形态用例）。
            prefix=_folded_head(result.messages),
            refs=list(history[:covered]),
        )
