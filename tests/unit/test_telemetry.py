"""事件日志（telemetry）的单元测试（D25）。

覆盖点：脱敏、JSONL 往返、批量 flush、**写盘失败绝不阻断会话**。
"""

from __future__ import annotations

import json
import unittest
from pathlib import Path

from logox.kernel.events import ModelDelta, parse_event
from logox.telemetry import EventLog, redact_text, session_log_path
from tests.unit.support import make_temp_dir, remove_temp_dir

SESSION = "s-telemetry"


class RedactionTests(unittest.TestCase):
    def test_api_key_patterns_are_masked(self) -> None:
        """脱敏：宁可多打码，也不能把密钥写进日志。"""
        cases = [
            ("key=" + "sk-" + "abcdefghijklmnop", "abcdefghijklmnop"),
            ('"api_key": "sk-1234567890abcdef"', "1234567890abcdef"),
            ('Authorization: "Bearer abcdefghijklmnop"', "abcdefghijklmnop"),
        ]
        for text, secret in cases:
            with self.subTest(text=text):
                masked = redact_text(text)
                self.assertNotIn(secret, masked)
                self.assertIn("***", masked)

    def test_ordinary_text_is_untouched(self) -> None:
        text = "把 config.py 里的超时改成 30 秒"
        self.assertEqual(redact_text(text), text)

    def test_short_prefix_is_preserved_for_troubleshooting(self) -> None:
        """保留极短前缀，便于判断"是哪个 key 泄露了"。"""
        masked = redact_text("sk-abcdefghijklmnop")
        self.assertTrue(masked.startswith("sk-abc"))


class EventLogTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.root = make_temp_dir("telemetry-")
        self.addCleanup(remove_temp_dir, self.root)

    def _log(self, **kwargs: object) -> EventLog:
        return EventLog(self.root / "logs" / f"{SESSION}.jsonl", **kwargs)  # type: ignore[arg-type]

    async def test_jsonl_roundtrip(self) -> None:
        """写入的事件必须能被 ``parse_event`` 还原（telemetry 是回放的唯一来源）。"""
        log = self._log()
        await log.start()
        try:
            for index in range(5):
                await log.handle(
                    ModelDelta(session_id=SESSION, kind="text", delta=str(index), request_index=0)
                )
        finally:
            await log.aclose()

        lines = [line for line in log.path.read_text(encoding="utf-8").splitlines() if line]
        self.assertEqual(len(lines), 5)
        restored = [parse_event(line) for line in lines]
        self.assertEqual([event.delta for event in restored], ["0", "1", "2", "3", "4"])  # type: ignore[attr-defined]

    async def test_aclose_flushes_remaining_buffer(self) -> None:
        """``aclose`` 必须**先 flush 再取消**，否则最后几条事件会丢。"""
        log = self._log()
        await log.start()
        await log.handle(ModelDelta(session_id=SESSION, kind="text", delta="last", request_index=0))
        self.assertFalse(log.path.exists(), "尚未到批量阈值时不应落盘")

        await log.aclose()
        self.assertTrue(log.path.exists())
        self.assertIn("last", log.path.read_text(encoding="utf-8"))

    async def test_events_are_redacted_by_default(self) -> None:
        log = self._log()
        await log.start()
        try:
            await log.handle(
                ModelDelta(
                    session_id=SESSION,
                    kind="text",
                    delta="Bearer " + "z" * 24,
                    request_index=0,
                )
            )
        finally:
            await log.aclose()
        content = log.path.read_text(encoding="utf-8")
        self.assertNotIn("z" * 24, content)

    async def test_redaction_can_be_disabled(self) -> None:
        log = self._log(redact=False)
        await log.start()
        try:
            await log.handle(
                ModelDelta(session_id=SESSION, kind="text", delta="Bearer " + "z" * 24, request_index=0)
            )
        finally:
            await log.aclose()
        self.assertIn("z" * 24, log.path.read_text(encoding="utf-8"))

    async def test_write_failure_does_not_raise(self) -> None:
        """E-9 / P-5：写盘失败只记日志并计数，**绝不阻断会话**。"""
        blocker = self.root / "blocker"
        blocker.write_text("我是一个文件，不是目录", encoding="utf-8")
        log = EventLog(blocker / "nested" / "log.jsonl")

        await log.handle(ModelDelta(session_id=SESSION, kind="text", delta="x", request_index=0))
        await log._flush()  # noqa: SLF001 - 直接触发一次 flush 以观察失败处理

        self.assertEqual(log.write_failures, 1)
        self.assertEqual(log.lines_written, 0)

    async def test_batch_flush_triggers_at_threshold(self) -> None:
        log = self._log(batch_size=3, flush_interval_s=60.0)
        await log.start()
        try:
            for index in range(3):
                await log.handle(
                    ModelDelta(session_id=SESSION, kind="text", delta=str(index), request_index=0)
                )
        finally:
            await log.aclose()
        self.assertEqual(log.lines_written, 3)

    async def test_lines_are_valid_json(self) -> None:
        log = self._log()
        await log.start()
        try:
            await log.handle(ModelDelta(session_id=SESSION, kind="reasoning", delta="思考", request_index=1))
        finally:
            await log.aclose()
        for line in log.path.read_text(encoding="utf-8").splitlines():
            payload = json.loads(line)
            self.assertEqual(payload["type"], "model_delta")
            self.assertEqual(payload["session_id"], SESSION)

    async def test_flush_with_empty_buffer_is_noop(self) -> None:
        log = self._log()
        await log._flush()  # noqa: SLF001
        self.assertFalse(log.path.exists())


class SessionLogPathTests(unittest.TestCase):
    def test_path_is_partitioned_by_date(self) -> None:
        """按日期分层，避免单目录堆积成千上万文件。"""
        path = session_log_path(Path("/logs"), "abc123", now=1730000000.0)
        self.assertEqual(path.suffix, ".jsonl")
        self.assertEqual(path.stem, "abc123")
        self.assertRegex(path.parent.name, r"^\d{4}-\d{2}-\d{2}$")

    def test_unsafe_session_id_is_sanitized(self) -> None:
        """会话 ID 会被过滤，避免路径穿越。"""
        path = session_log_path(Path("/logs"), "../../evil", now=1730000000.0)
        self.assertNotIn("..", path.name)
        self.assertNotIn("/", path.name)
        self.assertNotIn("\\", path.name)


if __name__ == "__main__":
    unittest.main()
