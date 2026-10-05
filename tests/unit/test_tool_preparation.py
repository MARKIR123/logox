"""Read-only preparation keeps UI scheduling and cannot commit after cancellation."""

from __future__ import annotations

import asyncio
import threading
from unittest.mock import patch

import pytest

from logox.errors import ErrorCategory
from logox.tools.base import ToolContext
from logox.tools.fs_edit import EditArgs, EditTool
from logox.tools.fs_write import WriteArgs, WriteTool


@pytest.mark.parametrize("operation", ["edit", "write"])
@pytest.mark.parametrize("cancel_kind", ["task", "cooperative"])
def test_cancel_during_preparation_does_not_modify_source(tmp_path, operation, cancel_kind):
    target = tmp_path / "source.txt"
    target.write_bytes(b"old\n")
    stopped = threading.Event()
    entered, release, finished = threading.Event(), threading.Event(), threading.Event()
    tool = EditTool() if operation == "edit" else WriteTool()
    name = "_prepare" if operation == "edit" else "_existing_state"
    original = getattr(tool, name)
    args = (
        EditArgs(path="source.txt", old_string="old", new_string="new")
        if operation == "edit"
        else WriteArgs(path="source.txt", content="new\n")
    )
    ctx = ToolContext(cwd=tmp_path, is_cancelled=stopped.is_set)

    def prepare(*args):
        entered.set()
        try:
            if not release.wait(3):
                raise AssertionError("event loop could not release read-only preparation")
            return original(*args)
        finally:
            finished.set()

    async def check():
        with patch.object(tool, name, prepare):
            task = asyncio.create_task(tool.run(args, ctx))
            try:
                assert await asyncio.to_thread(entered.wait, 3)
                # The event loop reaches here while the worker is blocked, not after it ends.
                assert not finished.is_set()
                if cancel_kind == "task":
                    task.cancel()
                else:
                    stopped.set()
                release.set()
                if cancel_kind == "task":
                    with pytest.raises(asyncio.CancelledError):
                        await task
                else:
                    result = await task
                    assert not result.ok and result.error.category == ErrorCategory.CANCELLED
                assert await asyncio.to_thread(finished.wait, 3)
            finally:
                release.set()
                if not task.done():
                    task.cancel()
                await asyncio.gather(task, return_exceptions=True)

    asyncio.run(check())
    assert target.read_bytes() == b"old\n"
    assert list(tmp_path.iterdir()) == [target]


@pytest.mark.parametrize("operation", ["edit", "write"])
def test_preparation_failure_does_not_publish_success_or_write(tmp_path, operation):
    target = tmp_path / "source.txt"
    target.write_bytes(b"old\n")
    tool = EditTool() if operation == "edit" else WriteTool()
    name = "_prepare" if operation == "edit" else "_existing_state"
    args = (
        EditArgs(path="source.txt", old_string="old", new_string="new")
        if operation == "edit"
        else WriteArgs(path="source.txt", content="new\n")
    )
    with patch.object(tool, name, side_effect=OSError("read failed")):
        if operation == "edit":
            with pytest.raises(OSError):
                asyncio.run(tool.run(args, ToolContext(cwd=tmp_path)))
        else:
            result = asyncio.run(tool.run(args, ToolContext(cwd=tmp_path)))
            assert not result.ok and result.error.category == ErrorCategory.TOOL_FAILURE
    assert target.read_bytes() == b"old\n"


def test_edit_does_not_overwrite_change_made_during_preparation(tmp_path):
    target = tmp_path / "source.txt"
    target.write_bytes(b"old\n")
    tool = EditTool()
    original = tool._prepare

    def prepare(*args):
        prepared = original(*args)
        target.write_bytes(b"external edit\n")
        return prepared

    with patch.object(tool, "_prepare", prepare):
        result = asyncio.run(
            tool.run(
                EditArgs(path="source.txt", old_string="old", new_string="new"), ToolContext(cwd=tmp_path)
            )
        )
    assert not result.ok and result.error.category == ErrorCategory.BAD_REQUEST
    assert "未覆盖外部修改" in result.content
    assert target.read_bytes() == b"external edit\n"
