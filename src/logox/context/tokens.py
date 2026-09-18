"""Token 估算器与动态 Usage 校准器。

纯自研双频加权算法，零沉重依赖（无 tiktoken 二进制依赖），确保 Windows 启动 <300ms。
支持根据大模型实际返回的 Usage 进行 EMA 动态校准，越跑越准。
"""

from __future__ import annotations

import re
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
    "TokenEstimator",
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
    """自适应 Token 估算器。

    持有一个自适应校准系数 kappa，当收到 Provider 返回的真实 usage 时，
    使用 EMA (指数移动平均, alpha=0.3) 进行平滑自校准。
    """

    def __init__(self, initial_calibration: float = 1.0) -> None:
        self.calibration_factor = initial_calibration
        self._sample_count = 0

    def estimate_text(self, text: str) -> int:
        return estimate_text_tokens(text, self.calibration_factor)

    def estimate_message(self, message: Message, *, include_reasoning: bool = False) -> int:
        return estimate_message_tokens(
            message, self.calibration_factor, include_reasoning=include_reasoning
        )

    def estimate_messages(
        self,
        messages: list[Message],
        *,
        system_prompt: str = "",
        include_reasoning: bool = False,
    ) -> int:
        total = self.estimate_text(system_prompt) if system_prompt else 0
        for msg in messages:
            total += self.estimate_message(msg, include_reasoning=include_reasoning)
        return total

    def calibrate(self, estimated_tokens: int, actual_tokens: int, alpha: float = 0.3) -> float:
        """根据真实 API usage 校准动态系数。"""
        if estimated_tokens <= 0 or actual_tokens <= 0:
            return self.calibration_factor

        ratio = actual_tokens / estimated_tokens
        # 限制单次校准波动范围在 [0.5, 2.0]，防止极端特化请求导致系数震荡
        bounded_ratio = max(0.5, min(2.0, ratio))

        self.calibration_factor = (1.0 - alpha) * self.calibration_factor + alpha * bounded_ratio
        self._sample_count += 1
        return self.calibration_factor
