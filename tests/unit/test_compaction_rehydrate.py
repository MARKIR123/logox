"""压缩后的**工作集重读**（CHANGE-005 裁定 4）。

要解决的问题
============

工具结果被卸载后，模型手上的工作集（正在改的那几个文件）就只剩一个路径。
**它很可能不会主动去读** —— 于是接着往下写，写出来的东西和文件现状对不上。
这就是"省 token"变成"让模型自己想起来要读什么"的那个坑。

Claude Code 的逆向资料把这步叫 **file rehydration**，并且明确称其为
"*the key insight*"，口径是**重读最近 5 个文件**：

> The key insight is the file rehydration: the system re-reads what you were just
> working on, so you don't lose your place.
> File restoration — re-reads your 5 most recent files after summarizing.

所以本模块盯的是**取哪些、按什么顺序、读不到怎么办**，而不是"能不能读文件"。
"""

from __future__ import annotations

import unittest

from logox.context.compaction import Compactor, INDEX_MARKER, is_index_message
from logox.kernel.messages import (
    Message,
    TextBlock,
    ToolResultBlock,
    ToolUseBlock,
)
from tests.unit.support import make_temp_dir, remove_temp_dir

INDEX_SOURCE = "compaction"


class WorkingSetRehydrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = make_temp_dir("rehydrate-")

    def tearDown(self) -> None:
        remove_temp_dir(self.root)

    # ------------------------------------------------------------------ #
    # 脚手架
    # ------------------------------------------------------------------ #
    def _write(self, name: str, content: str) -> str:
        path = self.root / name
        path.write_text(content, encoding="utf-8")
        return str(path)

    def _history(self, paths: list[str]) -> list[Message]:
        """造一段"逐轮改文件"的历史：第 1 轮是锚点，最后一轮是尾部窗口。

        中间那些带 ``tool_use`` 的轮次会被折叠 —— 它们碰过的文件就是工作集。
        """
        messages = [
            Message(role="user", blocks=[TextBlock(text="初始目标")]),
            Message(role="assistant", blocks=[TextBlock(text="好的")]),
        ]
        for index, path in enumerate(paths, start=2):
            messages.append(
                Message(role="user", blocks=[TextBlock(text=f"第{index}轮")])
            )
            messages.append(
                Message(
                    role="assistant",
                    blocks=[
                        ToolUseBlock(id=f"call_{index}", name="edit", input={"path": path})
                    ],
                )
            )
            messages.append(
                Message(
                    role="tool",
                    blocks=[ToolResultBlock(id=f"call_{index}", content="ok", ok=True)],
                )
            )
            messages.append(Message(role="assistant", blocks=[TextBlock(text="改好了")]))
        # 尾部窗口（keep_recent_turns=1 时不被折）
        messages.append(Message(role="user", blocks=[TextBlock(text="继续")]))
        messages.append(Message(role="assistant", blocks=[TextBlock(text="完成")]))
        return messages

    def _index_text(self, compactor: Compactor, history: list[Message]) -> str:
        result = compactor.compact(history, force=True, system_prompt="sys")
        index = next(
            (
                m
                for m in result.messages
                if is_index_message(m)
            ),
            None,
        )
        assert index is not None, "强制压缩后必须产出一条归档索引消息"
        return index.text

    @staticmethod
    def _compactor(**kwargs: object) -> Compactor:
        return Compactor(window_capacity=10**9, keep_recent_turns=1, **kwargs)  # type: ignore[arg-type]

    # ------------------------------------------------------------------ #
    # 核心行为
    # ------------------------------------------------------------------ #
    def test_recently_touched_files_come_back_after_compaction(self) -> None:
        """★ 核心：折叠掉的那些轮里碰过的文件，内容要**重新出现在索引里**。

        改之前：模型只剩一个路径，只能自己想到去 ``fs_read``。
        """
        a = self._write("a.py", "def alpha():\n    return 1\n")
        b = self._write("b.py", "def beta():\n    return 2\n")

        text = self._index_text(self._compactor(), self._history([a, b]))

        self.assertIn("[工作集快照]", text)
        self.assertIn("def alpha():", text, "a.py 的内容必须被读回来")
        self.assertIn("def beta():", text, "b.py 的内容必须被读回来")

    def test_most_recent_file_comes_first(self) -> None:
        """顺序是**越近越靠前** —— 模型最后碰的那个最可能是它当下要改的。"""
        a = self._write("a.py", "AAA\n")
        b = self._write("b.py", "BBB\n")
        c = self._write("c.py", "CCC\n")

        text = self._index_text(self._compactor(), self._history([a, b, c]))

        self.assertLess(text.index("CCC"), text.index("BBB"))
        self.assertLess(text.index("BBB"), text.index("AAA"))

    def test_only_the_last_n_files_are_kept(self) -> None:
        """只带最近 N 个 —— 否则“重读”会变成第二份上下文。"""
        paths = [self._write(f"f{i}.py", f"CONTENT_{i}\n") for i in range(5)]

        text = self._index_text(
            self._compactor(rehydrate_files=2), self._history(paths)
        )

        self.assertIn("CONTENT_4", text, "最近的那个必须在")
        self.assertIn("CONTENT_3", text)
        self.assertNotIn("CONTENT_0", text, "超过 N 个的不能再带")

    def test_missing_files_are_skipped_without_raising(self) -> None:
        """文件已被删 / 路径不存在 → **静默跳过**，不留悬空引用。

        压缩本身不该因为一次文件 IO 失败而中断 —— 拿不到就当没有。
        """
        real = self._write("real.py", "REAL\n")
        ghost = str(self.root / "ghost.py")  # 故意不创建

        text = self._index_text(self._compactor(), self._history([ghost, real]))

        self.assertIn("REAL", text)
        self.assertNotIn("ghost.py", text, "读不到的文件不得写进索引")

    def test_long_files_are_truncated_with_an_explicit_marker(self) -> None:
        """超长文件要截断，并且**说清楚截了**（否则模型会把截断当成文件结尾）。"""
        big = self._write("big.py", "X" * 9000)

        text = self._index_text(
            self._compactor(rehydrate_max_chars=100), self._history([big])
        )

        self.assertIn("字已截断", text)
        self.assertIn("fs_read", text, "截断处要给出取回完整内容的办法")

    def test_can_be_switched_off(self) -> None:
        """``rehydrate_files=0`` → 完全不读盘（这是可以关掉的，不是硬编码行为）。"""
        path = self._write("a.py", "SHOULD_NOT_APPEAR\n")

        text = self._index_text(
            self._compactor(rehydrate_files=0), self._history([path])
        )

        self.assertNotIn("SHOULD_NOT_APPEAR", text)
        self.assertNotIn("[工作集快照]", text)

    def test_only_known_path_keys_are_trusted(self) -> None:
        """只认 ``path`` / ``file_path`` / ``filePath``，**不猜**整个参数字典。

        否则一个碰巧像路径的字符串（比如 grep 的 pattern）就会把无关文件拉进来。
        """
        real = self._write("real.py", "REAL_CONTENT\n")

        messages = [
            Message(role="user", blocks=[TextBlock(text="目标")]),
            Message(role="assistant", blocks=[TextBlock(text="好的")]),
            Message(role="user", blocks=[TextBlock(text="第2轮")]),
            Message(
                role="assistant",
                blocks=[
                    # 工具名与参数无关紧要，关键是键名
                    ToolUseBlock(id="c1", name="grep", input={"pattern": real}),
                    ToolUseBlock(id="c2", name="read", input={"file_path": real}),
                ],
            ),
            Message(
                role="tool",
                blocks=[
                    ToolResultBlock(id="c1", content="ok", ok=True),
                    ToolResultBlock(id="c2", content="ok", ok=True),
                ],
            ),
            Message(role="assistant", blocks=[TextBlock(text="好了")]),
            Message(role="user", blocks=[TextBlock(text="继续")]),
            Message(role="assistant", blocks=[TextBlock(text="完成")]),
        ]

        text = self._index_text(self._compactor(), messages)

        self.assertIn("REAL_CONTENT", text, "file_path 这个键必须能认出来")

    def test_reset_clears_the_snapshot_with_the_ledger(self) -> None:
        """``reset()`` 必须连快照一起清 —— 它与账本同属"缓存里那段区间"。"""
        path = self._write("a.py", "OLD_CONTENT\n")
        compactor = self._compactor()
        self._index_text(compactor, self._history([path]))
        self.assertTrue(compactor._working_set)  # noqa: SLF001

        compactor.reset()

        self.assertEqual(compactor._working_set, [])  # noqa: SLF001


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
