"""流式文字平滑器（D186 / D199）。

模型分片先进入正文/推理缓冲，UI ticker 每帧分别调用一次 step。小积压逐字释放，
大积压每通道最多 16 字符，减小突发跳块；600 字合成突发需 50 步排空，不能承诺
200ms 追平。结束、失败或取消时 flush_all 排空，避免丢失末尾正文和推理。

所有操作在主事件循环执行。它不改变网络分片，不独立保证终端帧率或写出时延；
若模型结束时仍有积压，最终一帧可能一次展示剩余内容。
"""

from __future__ import annotations

from collections import deque


class _ChannelBuffer:
    """片段按顺序出队，只切本次需要的字符，不复制全部剩余文本。"""

    def __init__(self) -> None:
        self.chunks: deque[str] = deque()
        self.offset = 0
        self.remaining = 0

    def feed(self, delta: str) -> None:
        if delta:
            self.chunks.append(delta)
            self.remaining += len(delta)

    def take(self, count: int) -> str:
        count = min(count, self.remaining)
        self.remaining -= count
        pieces: list[str] = []
        while count:
            head = self.chunks[0]
            used = min(count, len(head) - self.offset)
            pieces.append(head[self.offset:self.offset + used])
            self.offset += used
            count -= used
            if self.offset == len(head):
                self.chunks.popleft()
                self.offset = 0
        return "".join(pieces)

    def flush(self) -> str:
        if self.offset:
            self.chunks[0] = self.chunks[0][self.offset:]
        text = "".join(self.chunks)
        self.clear()
        return text

    def clear(self) -> None:
        self.chunks.clear()
        self.offset = self.remaining = 0


class StreamSmoother:
    """双通道流式缓动；主事件循环写入与步进，无锁。"""

    def __init__(self) -> None:
        self._text = _ChannelBuffer()
        self._reasoning = _ChannelBuffer()

    def feed_text(self, delta: str) -> None:
        self._text.feed(delta)

    def feed_reasoning(self, delta: str) -> None:
        self._reasoning.feed(delta)

    @staticmethod
    def _compute_step(remaining: int) -> int:
        """根据积压计算步长；保持既有逐字与最多 16 字符的策略。"""
        if remaining <= 0:
            return 0
        if remaining <= 8:
            return 1
        if remaining <= 30:
            return (remaining + 5) // 6
        if remaining <= 80:
            return min(16, (remaining + 3) // 4)
        return min(16, remaining)

    def step_text(self) -> str:
        return self._text.take(self._compute_step(self._text.remaining))

    def step_reasoning(self) -> str:
        return self._reasoning.take(self._compute_step(self._reasoning.remaining))

    def flush_all_text(self) -> str:
        return self._text.flush()

    def flush_all_reasoning(self) -> str:
        return self._reasoning.flush()

    def flush_all(self) -> tuple[str, str]:
        return self.flush_all_text(), self.flush_all_reasoning()

    def has_pending(self) -> bool:
        return bool(self._text.remaining or self._reasoning.remaining)

    def pending_text_len(self) -> int:
        return self._text.remaining

    def pending_reasoning_len(self) -> int:
        return self._reasoning.remaining

    def clear(self) -> None:
        self._text.clear()
        self._reasoning.clear()
