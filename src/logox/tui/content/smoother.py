"""流式文字平滑器（D186 / D199）。

模型分片先进入正文/推理缓冲，UI ticker 每帧分别调用一次 step。小积压逐字释放，
大积压每通道最多 16 字符，减小突发跳块；600 字合成突发需 50 步排空，不能承诺
200ms 追平。结束、失败或取消时 flush_all 排空，避免丢失末尾正文和推理。

所有操作在主事件循环执行。它不改变网络分片，不独立保证终端帧率或写出时延；
若模型结束时仍有积压，最终一帧可能一次展示剩余内容。
"""

from __future__ import annotations


class StreamSmoother:
    """流式打字机缓动插值器（双通道：正文与思考）。

    线程模型：所有操作均在主事件循环线程执行，无锁高吞吐。
    """

    def __init__(self) -> None:
        self._text_chunks: list[str] = []
        self._text_buffer: str = ""
        self._reasoning_chunks: list[str] = []
        self._reasoning_buffer: str = ""

    # -- 生产者写入 (Feed) ------------------------------------------------ #

    def feed_text(self, delta: str) -> None:
        """喂入正文增量片段。"""
        if delta:
            self._text_chunks.append(delta)

    def feed_reasoning(self, delta: str) -> None:
        """喂入思考推理增量片段。"""
        if delta:
            self._reasoning_chunks.append(delta)

    # -- 消费者步进 (Step) ------------------------------------------------ #

    @staticmethod
    def _compute_step(remaining: int) -> int:
        """根据当前积压字符数计算本帧释放步长 (Elastic Token Lerp)。"""
        if remaining <= 0:
            return 0
        if remaining <= 8:
            return 1
        if remaining <= 30:
            return (remaining + 5) // 6
        if remaining <= 80:
            return min(16, (remaining + 3) // 4)
        return min(16, remaining)

    def _sync_text_buffer(self) -> None:
        if self._text_chunks:
            self._text_buffer += "".join(self._text_chunks)
            self._text_chunks.clear()

    def _sync_reasoning_buffer(self) -> None:
        if self._reasoning_chunks:
            self._reasoning_buffer += "".join(self._reasoning_chunks)
            self._reasoning_chunks.clear()

    def step_text(self) -> str:
        """按弹性打字机插值释出正文本帧增量。无积压时返回空字符串。"""
        self._sync_text_buffer()
        if not self._text_buffer:
            return ""
        step = self._compute_step(len(self._text_buffer))
        chunk = self._text_buffer[:step]
        self._text_buffer = self._text_buffer[step:]
        return chunk

    def step_reasoning(self) -> str:
        """按弹性打字机插值释出思考推理本帧增量。无积压时返回空字符串。"""
        self._sync_reasoning_buffer()
        if not self._reasoning_buffer:
            return ""
        step = self._compute_step(len(self._reasoning_buffer))
        chunk = self._reasoning_buffer[:step]
        self._reasoning_buffer = self._reasoning_buffer[step:]
        return chunk

    # -- 终态排空 (Flush) ------------------------------------------------- #

    def flush_all_text(self) -> str:
        """瞬时排空所有积压正文（终态调用）。"""
        self._sync_text_buffer()
        if not self._text_buffer:
            return ""
        res = self._text_buffer
        self._text_buffer = ""
        return res

    def flush_all_reasoning(self) -> str:
        """瞬时排空所有积压思考推理（终态调用）。"""
        self._sync_reasoning_buffer()
        if not self._reasoning_buffer:
            return ""
        res = self._reasoning_buffer
        self._reasoning_buffer = ""
        return res

    def flush_all(self) -> tuple[str, str]:
        """瞬时排空双通道全部文本，返回 (text, reasoning)。"""
        return self.flush_all_text(), self.flush_all_reasoning()

    # -- 状态检查 --------------------------------------------------------- #

    def has_pending(self) -> bool:
        """当前是否仍有尚未排空的正文或推理文本。"""
        return (
            bool(self._text_buffer)
            or bool(self._text_chunks)
            or bool(self._reasoning_buffer)
            or bool(self._reasoning_chunks)
        )

    def pending_text_len(self) -> int:
        """当前积压的正文字符数。"""
        self._sync_text_buffer()
        return len(self._text_buffer)

    def pending_reasoning_len(self) -> int:
        """当前积压的推理字符数。"""
        self._sync_reasoning_buffer()
        return len(self._reasoning_buffer)

    def clear(self) -> None:
        """清空所有缓冲。"""
        self._text_chunks.clear()
        self._text_buffer = ""
        self._reasoning_chunks.clear()
        self._reasoning_buffer = ""
