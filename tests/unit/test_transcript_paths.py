"""会话日志路径卫生（D153 / CHANGE-023）。

这一组用例防的是**同一族**缺陷：库代码在没有明确告知的情况下，往当前工作目录（或仓库）里写东西。

背景（用户报障）：`logox chat` 每运行一次在仓库里建一个 `.logox/runs/chat-<pid>/`（实测积了 113 个空目录），
一条测试每跑一次建一个 `.logox/runs/recheck/`。共因是三处叠加：

1. `SessionTranscriptWriter(base_dir=".logox/runs")` —— **相对路径默认值**；
2. `HierarchicalContextBuilder` 在没传 writer 时**兜底构造**；
3. `logox chat` 与那条测试**恰好都没传**。

⇒ 于是"忘了传"既不报错也不打日志，只是悄悄多一个目录 —— 而**清理治不了**：删了下一次又会回来。
"""

from __future__ import annotations

import inspect
import unittest
from pathlib import Path

from logox.context.builder import HierarchicalContextBuilder
from logox.context.storage import SessionTranscriptWriter
from tests.unit.support import REPO_ROOT, make_temp_dir, remove_temp_dir


def _snapshot(root: Path) -> set[str]:
    """目录递归快照（相对路径集合）。目录不存在 ⇒ 空集。"""
    if not root.exists():
        return set()
    return {str(path.relative_to(root)) for path in root.rglob("*")}


class WriterTargetTests(unittest.TestCase):
    """T-1 / T-2：writer **必须**被明确告知落点，绝不允许静默 fallback。"""

    def test_t01_writer_requires_an_explicit_target(self) -> None:
        """不传 `base_dir` 也不传 `log_file` ⇒ 构造期 `ValueError`（不是"悄悄写 cwd"）。"""
        with self.assertRaises(ValueError) as caught:
            SessionTranscriptWriter()
        message = str(caught.exception)
        # 错误消息必须自解释：说清"怎么修"（传 paths.sessions 或具体 log_file）
        self.assertIn("base_dir", message)
        self.assertIn("log_file", message)

    def test_t02_no_cwd_pollution_on_the_error_path(self) -> None:
        """★ 关键：**抛错之前也不许建目录**（否则"炸了但仓库已经被写脏"）。"""
        cwd = Path.cwd()
        before = _snapshot(cwd / ".logox")
        with self.assertRaises(ValueError):
            SessionTranscriptWriter()
        self.assertEqual(_snapshot(cwd / ".logox"), before, "writer 在报错前动了工作目录")

    def test_t03_explicit_targets_land_where_asked(self) -> None:
        tmp = make_temp_dir("writer-target-")
        try:
            writer = SessionTranscriptWriter(base_dir=tmp / "sessions", session_id="sid")
            self.assertEqual(writer.log_file, (tmp / "sessions" / "sid" / "transcript.jsonl").resolve())
            self.assertTrue(writer.log_file.parent.is_dir())

            direct = tmp / "direct" / "t.jsonl"
            writer2 = SessionTranscriptWriter(log_file=direct)
            self.assertEqual(writer2.log_file, direct.resolve())
            self.assertTrue(direct.parent.is_dir())
        finally:
            remove_temp_dir(tmp)


class BuilderRequiresWriterTests(unittest.TestCase):
    """T-4：`HierarchicalContextBuilder` 不再提供兜底 writer。"""

    def test_t04_transcript_writer_has_no_default(self) -> None:
        """用**签名**断言：`transcript_writer` 没有默认值（忘传 ⇒ 构造期 TypeError）。"""
        parameters = inspect.signature(HierarchicalContextBuilder.__init__).parameters
        self.assertIn("transcript_writer", parameters)
        self.assertIs(
            parameters["transcript_writer"].default,
            inspect.Parameter.empty,
            "transcript_writer 又有默认值了 —— 兜底构造会让'忘传'重新变成静默写 cwd",
        )

    def test_t04b_builder_without_writer_raises(self) -> None:
        with self.assertRaises(TypeError):
            HierarchicalContextBuilder(system="sys")  # type: ignore[call-arg]


class RepoWorkspaceHygieneTests(unittest.TestCase):
    """T-5：跑一组代表性操作后，**仓库自己的 `.logox/` 不许有任何变化**（F-21 同族）。"""

    def test_t05_repo_logox_dir_is_untouched(self) -> None:
        before = _snapshot(REPO_ROOT / ".logox")
        tmp = make_temp_dir("hygiene-")
        try:
            writer = SessionTranscriptWriter(base_dir=tmp, session_id="hygiene")
            builder = HierarchicalContextBuilder(
                system="sys", cwd=tmp, session_id="hygiene", transcript_writer=writer
            )
            builder.build([])
        finally:
            remove_temp_dir(tmp)
        self.assertEqual(
            _snapshot(REPO_ROOT / ".logox"),
            before,
            "代表性操作污染了仓库的 .logox/ —— 这正是 D153 要根除的那类缺陷",
        )


if __name__ == "__main__":
    unittest.main()
