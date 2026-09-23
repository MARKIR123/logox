"""Token 估算、真实锚点计量与 EMA 校准（D141 / D158）。

三层东西，按"依赖谁"排：

1. :func:`estimate_text_tokens` 等**纯函数** —— 双频字符加权（零依赖，启动 <300ms）。
2. :class:`TokenEstimator` —— 带**按模型分桶**的校准系数 κ，用真实用量 EMA 自校准。
3. :class:`TokenLedger` —— **锚点 + 增量**计量（D158，参照 Pi）：
   厂商实测的"上一发上下文总量"是**精确锚点**，只对锚点之后的新增消息做估算，
   于是误差从"整个上下文的估算误差"缩小到"锚点之后那一小段的估算误差"。

为什么需要锚点（而不是继续用 `max(估算, 真实)`）：见 `MODULE_context_tokens.md`；
一句话 —— 两者取大是**钝**的（分不清"我的估算偏保守"与"真实值已过期"），
而锚点把"精确已知"与"需要估算"两段**分开**，语义清晰且误差可见。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from logox.kernel.messages import (
    ContentBlock,
    Message,
    ReasoningBlock,
    TextBlock,
    ToolResultBlock,
    ToolUseBlock,
)

__all__ = [
    "Anchor",
    "Prediction",
    "TokenEstimator",
    "TokenLedger",
    "estimate_block_tokens",
    "estimate_message_tokens",
    "estimate_text_tokens",
]

# CJK 区域：汉字、全角标点、日韩字符
_CJK_PATTERN = re.compile(r"[\u4e00-\u9fff\u3000-\u303f\uff00-\uffef]")


def estimate_text_tokens(text: str, calibration_factor: float = 1.0) -> int:
    """双频字符加权估算文本 Token 数。

    - CJK 汉字与全角符号：权重 1.0 (约 1 字符 = 1 Token)
    - ASCII 与代码英文、数字、标点：权重 0.28 (约 3.5 ~ 4 字符 = 1 Token)
    """
    if not text:
        return 0

    cjk_count = len(_CJK_PATTERN.findall(text))
    ascii_count = len(text) - cjk_count

    raw_tokens = cjk_count * 1.0 + ascii_count * 0.28
    return max(1, int(raw_tokens * calibration_factor))


def estimate_block_tokens(block: ContentBlock, calibration_factor: float = 1.0) -> int:
    """估算单块内容所消耗的 Token 数。

    注意：根据 M7 架构裁决，ReasoningBlock 在发往模型时被过滤，
    但在本地度量时可按实际文本统计。
    """
    if isinstance(block, TextBlock):
        return estimate_text_tokens(block.text, calibration_factor)
    if isinstance(block, ReasoningBlock):
        return estimate_text_tokens(block.text, calibration_factor)
    if isinstance(block, ToolUseBlock):
        # name + input json 字符串
        input_str = str(block.input)
        return 8 + estimate_text_tokens(block.name + input_str, calibration_factor)
    if isinstance(block, ToolResultBlock):
        # 协议外壳约 6 tokens + content
        return 6 + estimate_text_tokens(block.content, calibration_factor)
    return 1


def estimate_message_tokens(
    message: Message,
    calibration_factor: float = 1.0,
    *,
    include_reasoning: bool = False,
) -> int:
    """估算整条消息的 Token 开销。

    默认 include_reasoning=False，严格契约发往 API 时的无思考链状态。
    每条消息附带基础协议角色开销（约 4 tokens）。
    """
    tokens = 4  # role + metadata 封装开销
    for block in message.blocks:
        if isinstance(block, ReasoningBlock) and not include_reasoning:
            continue
        tokens += estimate_block_tokens(block, calibration_factor)
    return tokens


class TokenEstimator:
    """自适应 Token 估算器（**κ 按 `(provider, model)` 分桶**，D158）。

    κ 是"真实/预测"的比值经 EMA 平滑后的**系统性偏差补偿系数**。它补偿的不只是分词差异，
    还包括估算器**看不见**的部分（工具 schema、消息外壳）—— 所以 κ 常态大于 1 是正常的。

    **为什么必须分桶**：不同模型的分词器不同、同一段文本的 token 数就不同；单例会串味
    （`/model` 一切换，κ 就开始用别的模型学到的偏差去修正新模型）。

    ⚠️ 已知局限（登记在文档里，不假装解决）：一个标量 κ 只能整体缩放，
    无法同时修正"中文密集"与"英文密集"两种会话 —— 所以它只在**该会话的平均构成**上准。
    锚点把系统性偏差消掉之后，κ 只负责增量那一小段，这个局限的影响面已大幅缩小。
    """

    def __init__(self, initial_calibration: float = 1.0) -> None:
        self._default_factor = initial_calibration
        self._factors: dict[str, float] = {}
        self._samples: dict[str, int] = {}

    # -- 只读视图（兼容旧的 `calibration_factor` 读法：指向默认桶）------------- #
    @property
    def calibration_factor(self) -> float:
        return self._default_factor

    @property
    def sample_count(self) -> int:
        """全部桶的样本总数（观测用）。"""
        return sum(self._samples.values())

    def factor_for(self, model_key: str = "") -> float:
        """该模型当前的 κ（没有样本时是初始值 1.0）。"""
        return self._factors.get(model_key, self._default_factor)

    def samples_for(self, model_key: str = "") -> int:
        return self._samples.get(model_key, 0)

    # -- 估算（model_key 可选，默认落到默认桶）-------------------------------- #
    def estimate_text(self, text: str, *, model_key: str = "") -> int:
        return estimate_text_tokens(text, self.factor_for(model_key))

    def estimate_message(
        self, message: Message, *, include_reasoning: bool = False, model_key: str = ""
    ) -> int:
        return estimate_message_tokens(
            message, self.factor_for(model_key), include_reasoning=include_reasoning
        )

    def estimate_messages(
        self,
        messages: list[Message],
        *,
        system_prompt: str = "",
        include_reasoning: bool = False,
        model_key: str = "",
    ) -> int:
        factor = self.factor_for(model_key)
        total = estimate_text_tokens(system_prompt, factor) if system_prompt else 0
        for msg in messages:
            total += estimate_message_tokens(
                msg, factor, include_reasoning=include_reasoning
            )
        return total

    # -- 校准 ------------------------------------------------------------------ #
    def calibrate(
        self,
        estimated_tokens: int,
        actual_tokens: int,
        alpha: float = 0.3,
        *,
        model_key: str = "",
    ) -> float:
        """用真实用量校准该模型的 κ。

        ⚠️ **校准目标必须对齐**：``estimated_tokens`` 必须是"对**实际发出去的那份列表**的预测"
        （有锚点时 = 锚点 + 增量估算）。若拿"全量纯估算"当分母，分子里那份精确锚点会把比值
        拉向 1.0 ⇒ **κ 几乎不再更新**（校准目标与估计器职责不对齐）。
        """
        if estimated_tokens <= 0 or actual_tokens <= 0:
            return self.factor_for(model_key)

        ratio = actual_tokens / estimated_tokens
        # 限制单次校准波动范围在 [0.5, 2.0]，防止极端特化请求导致系数震荡
        bounded_ratio = max(0.5, min(2.0, ratio))

        current = self.factor_for(model_key)
        updated = (1.0 - alpha) * current + alpha * bounded_ratio
        self._factors[model_key] = updated
        self._samples[model_key] = self.samples_for(model_key) + 1
        return updated


# --------------------------------------------------------------------------- #
# 锚点计量（D158，参照 Pi 的 anchor + delta）
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class Anchor:
    """一次厂商实测：这份上下文（前缀 + 到第 ``sent_count`` 条消息为止）实测占多少 token。

    字段的含义与约束：

    * ``sent_count`` —— 该次请求**实际发送**的消息条数。本项目的 ``Usage.context_tokens``
      是**输入侧**总量（不含助手这次的回复）⇒ 锚点之后的新增段**自然包含那条回复**，
      既不会重复计数也不会漏计（这条是 off-by-one 的高发区，有用例盯着）。
    * ``generation`` —— 锚点诞生时的历史代际。任何**历史重写**都会 +1，
      于是旧锚点自动失效（不靠"记得重置"，靠对不上就作废）。
    * ``prefix_digest`` —— 系统提示的指纹。前缀变了（例如项目记忆刷新），
      锚点描述的那段前缀就不是现在这段了。
    * ``model_key`` —— 换了模型 ⇒ 分词器与窗口都变了。
    """

    generation: int
    sent_count: int
    tokens: int
    model_key: str
    prefix_digest: str


@dataclass(frozen=True)
class Prediction:
    """一次预测的结果。``reason`` **永远非空**（可观测性：不可观测的兜底 = 下次还得再查一遍）。"""

    tokens: int
    used_anchor: bool
    reason: str


class TokenLedger:
    """上下文规模的"账本"：锚点（精确）+ 增量（估算）。

    状态机（详见 `MODULE_context_tokens.md` §4）::

        有锚点：total = anchor.tokens + Σ estimate(锚点之后的消息)      ← 误差只来自增量
        无锚点：total = estimate(系统提示 + 全部消息)                    ← 保守退化

        请求成功且厂商上报用量 → 重建锚点（精确）
        任何历史重写            → 代际 +1、锚点作废（**绝不把估算值写回锚点**）

    **为什么不把估算值写回锚点**：那会把估算误差**永久固化**到"精确"的那一侧，
    之后再也分不清哪部分测过、哪部分猜的 —— 比没有锚点更糟。
    """

    def __init__(self) -> None:
        self.generation = 0
        self.anchor: Anchor | None = None
        self.last_reason: str = "no_usage_yet"
        #: 已消费的 usage 对象（**身份**比对，防同一份用量被重复记账/重复校准）。
        #: 用 `is` 而不是 `==`：内核每次请求都把新的 `outcome.usage` 赋给
        #: `_last_request_usage`；未上报时它保持不变 ⇒ 用身份天然跳过。
        self._consumed: object | None = None
        #: 上一次 build 实际发出的 (消息条数, 该列表的预测 token 数)
        self._pending: tuple[int, int] | None = None
        #: 观测计数（供 `/status` 或测试检查）
        self.calibrated_samples = 0
        self.adopted_anchors = 0

    # -- 失效 ------------------------------------------------------------------ #
    def note_rewrite(self) -> None:
        """历史被重写（压缩折叠 / 工具修剪 / 再水化 / rewind / resume / new）。

        **唯一正确的失效方式**：作废 ⇒ 降级为纯估算；等下一次请求回来自然恢复精确。
        """
        self.generation += 1
        self.anchor = None
        self._pending = None
        self.last_reason = "rewrite_invalidated"

    # -- 记账 ------------------------------------------------------------------ #
    def note_build(self, *, sent_count: int, predicted_tokens: int) -> None:
        """记下"这一次 build 发出的列表规模与其预测值"。

        等 usage 回来时，它就是**锚点覆盖范围**与**校准分母**的来源。
        """
        self._pending = (sent_count, predicted_tokens)

    # -- 预测 ------------------------------------------------------------------ #
    def predict(
        self,
        messages: list[Message],
        *,
        system_prompt: str,
        estimator: TokenEstimator,
        model_key: str,
        prefix_digest: str,
    ) -> Prediction:
        """预测"把这份列表发出去会占多少 token"。"""
        anchor = self.anchor
        reason = ""
        if anchor is None:
            reason = "no_usage_yet"
        elif anchor.generation != self.generation:
            reason = "anchor_stale_generation"
        elif anchor.model_key != model_key:
            reason = "model_switched"
        elif anchor.prefix_digest != prefix_digest:
            reason = "prefix_changed"
        elif anchor.sent_count > len(messages):
            # 正常只增不减；变短说明发生了**没被登记的裁剪** ⇒ 放弃锚点（保守）
            reason = "sent_count_shrank"

        if reason:
            total = estimator.estimate_messages(
                messages, system_prompt=system_prompt, model_key=model_key
            )
            self.last_reason = reason
            return Prediction(tokens=total, used_anchor=False, reason=reason)

        assert anchor is not None  # for type checkers
        trailing = estimator.estimate_messages(
            messages[anchor.sent_count :], model_key=model_key
        )
        self.last_reason = "anchor+delta"
        return Prediction(
            tokens=anchor.tokens + trailing, used_anchor=True, reason="anchor+delta"
        )

    # -- 锚点重建 + 校准 ------------------------------------------------------- #
    def reconcile(
        self,
        *,
        usage: object | None,
        estimator: TokenEstimator,
        model_key: str,
        prefix_digest: str,
    ) -> str:
        """厂商用量回来后：**重建锚点**并**校准 κ**。返回原因（永远非空）。"""
        if usage is None:
            return "no_usage"
        if usage is self._consumed:  # 同一份用量（例如请求未上报时保持不动）
            return "usage_already_consumed"
        self._consumed = usage

        raw = getattr(usage, "context_tokens", None)
        if raw is None or int(raw) <= 0:
            # 口径缺失就**不猜**：`context_tokens` 是唯一跨厂商统一的口径（D9）
            return "context_tokens_missing"
        measured = int(raw)

        if self._pending is None:
            return "no_pending_prediction"
        sent_count, predicted = self._pending
        if predicted > 0:
            # 校准：分母 = 对**实际发出去的那份列表**的预测（对齐估计器职责）
            estimator.calibrate(predicted, measured, model_key=model_key)
            self.calibrated_samples += 1

        self.anchor = Anchor(
            generation=self.generation,
            sent_count=sent_count,
            tokens=measured,
            model_key=model_key,
            prefix_digest=prefix_digest,
        )
        self.adopted_anchors += 1
        return "anchor_rebuilt"
