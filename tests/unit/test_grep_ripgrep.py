"""ripgrep 的产品边界与实际子进程生命周期（D201）。"""
from __future__ import annotations

import asyncio
import json
import sys
from unittest.mock import patch

import pytest

from logox.errors import ErrorCategory
from logox.tools.base import ToolContext
from logox.tools.fs_grep import GrepArgs, GrepTool


def search(folder, **kwargs):
    return asyncio.run(GrepTool().run(GrepArgs(**kwargs), ToolContext(cwd=folder)))


def test_missing_ripgrep_fails_explicitly_without_python_fallback(tmp_path):
    with patch("logox.tools.fs_grep.shutil.which", return_value=None):
        result = search(tmp_path, pattern="anything")
    assert not result.ok and result.error.category == ErrorCategory.TOOL_FAILURE
    assert "rg --version" in result.content


def test_unstartable_ripgrep_returns_a_tool_failure(tmp_path):
    with patch("logox.tools.fs_grep.shutil.which", return_value=str(tmp_path / "missing-rg.exe")):
        result = search(tmp_path, pattern="anything")
    assert not result.ok and result.error.category == ErrorCategory.TOOL_FAILURE
    assert "无法启动" in result.content


@pytest.mark.parametrize("problem", ["stderr", "unfinished"])
def test_failed_or_incomplete_output_cannot_be_reported_as_no_matches(tmp_path, monkeypatch, problem):
    if problem == "stderr":
        code = "import sys;sys.stderr.write('x'*200000+'\\nunsupported literal file: denied');sys.exit(2)"
    else:
        payload = json.dumps({"type": "begin", "data": {}}) + "\n"
        code = f"import sys;sys.stdout.write({payload!r})"
    monkeypatch.setattr("logox.tools.fs_grep._command", lambda *_: [sys.executable, "-c", code])
    monkeypatch.setattr("logox.tools.fs_grep.shutil.which", lambda _: sys.executable)
    result = search(tmp_path, pattern="anything")
    assert not result.ok and result.error.category == ErrorCategory.TOOL_FAILURE
    assert "未找到匹配" not in result.content
    assert len(result.content) < 4200


def test_unsupported_lookaround_is_a_model_correctable_error(tmp_path):
    (tmp_path / "code.txt").write_text("foobar", encoding="utf8")
    result = search(tmp_path, pattern="foo(?=bar)")
    assert not result.ok and result.error.category == ErrorCategory.BAD_REQUEST
    assert "ripgrep 不支持" in result.content


def test_unicode_paths_crlf_bom_and_long_json_event(tmp_path):
    (tmp_path / "中文 file.txt").write_bytes("first\r\nneedle 中文\r\n".encode())
    (tmp_path / "utf16.txt").write_bytes("needle 中文\n".encode("utf16"))
    (tmp_path / "long.txt").write_text("needle " + "x" * 300000, encoding="utf8")
    result = search(tmp_path, pattern="needle")
    assert result.ok and result.display.payload["count"] == 3
    assert "中文 file.txt:2: needle 中文" in result.content
    assert "utf16.txt:1: needle 中文" in result.content
    assert "…" in result.content and "\r" not in result.content
    assert len(result.content) < 400


def test_option_like_pattern_is_not_a_command_option(tmp_path):
    (tmp_path / "a.txt").write_text("--version", encoding="utf8")
    result = search(tmp_path, pattern="--version")
    assert result.ok and result.display.payload["count"] == 1


def test_configuration_cannot_change_search_engine_or_scope(tmp_path, monkeypatch):
    config = tmp_path / "rg-config"
    config.write_text("--pcre2\n--glob=!*.txt\n", encoding="utf8")
    monkeypatch.setenv("RIPGREP_CONFIG_PATH", str(config))
    (tmp_path / "a.txt").write_text("needle", encoding="utf8")
    assert search(tmp_path, pattern="needle").display.payload["count"] == 1
    assert not search(tmp_path, pattern="need(?=le)").ok


def test_sensitive_files_are_skipped_recursively_but_explicit_target_works(tmp_path):
    (tmp_path / ".env.secret").write_text("needle", encoding="utf8")
    (tmp_path / ".ordinary").write_text("needle", encoding="utf8")
    (tmp_path / ".logox").mkdir()
    (tmp_path / ".logox" / "config.toml").write_text("needle", encoding="utf8")
    (tmp_path / ".tmp-build").mkdir()
    (tmp_path / ".tmp-build" / "a.txt").write_text("needle", encoding="utf8")
    result = search(tmp_path, pattern="needle")
    assert result.ok and result.display.payload["count"] == 1
    assert ".ordinary:1:" in result.content
    explicit = search(tmp_path, pattern="needle", path=".env.secret")
    assert explicit.ok and explicit.display.payload["count"] == 1


@pytest.mark.parametrize("stop", ["task", "context", "limit", "protocol"])
def test_running_process_is_reaped_on_every_exit_path(tmp_path, monkeypatch, stop):
    async def exercise():
        original = asyncio.create_subprocess_exec
        started = asyncio.Event()
        processes = []

        async def launch(*args, **kwargs):
            process = await original(*args, **kwargs)
            processes.append(process)
            started.set()
            return process

        path = str(tmp_path / "a.txt")
        records = [
            {"type": "begin", "data": {"path": {"text": path}}},
            {"type": "match", "data": {"path": {"text": path}, "lines": {"text": "needle\n"}, "line_number": 1}},
            {"type": "end", "data": {"binary_offset": None}},
        ]
        payload = "broken JSON\n" if stop == "protocol" else "".join(json.dumps(record) + "\n" for record in records)
        code = f"import sys,time;sys.stdout.write({payload!r});sys.stdout.flush();time.sleep(60)"
        monkeypatch.setattr("logox.tools.fs_grep._command", lambda *_: [sys.executable, "-u", "-c", code])
        monkeypatch.setattr("logox.tools.fs_grep.shutil.which", lambda _: sys.executable)
        monkeypatch.setattr(asyncio, "create_subprocess_exec", launch)
        flag = False
        ctx = ToolContext(cwd=tmp_path, is_cancelled=lambda: flag)
        task = asyncio.create_task(GrepTool().run(GrepArgs(pattern="needle", max_matches=1 if stop == "limit" else 10), ctx))
        try:
            await asyncio.wait_for(started.wait(), 5)
            # 工具尚在等待子进程时，主事件循环能处理其他工作。
            await asyncio.sleep(0)
            if stop == "task":
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await asyncio.wait_for(task, 5)
            elif stop == "context":
                flag = True
                result = await asyncio.wait_for(task, 5)
                assert result.error.category == ErrorCategory.CANCELLED
            else:
                result = await asyncio.wait_for(task, 5)
                if stop == "limit":
                    assert result.ok and result.display.payload["count"] == 1
                    assert result.display.payload["truncated"]
                else:
                    assert result.error.category == ErrorCategory.TOOL_FAILURE
            assert processes[0].returncode is not None
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            for process in processes:
                if process.returncode is None:
                    process.kill()
                await process.wait()

    asyncio.run(exercise())


def test_cancel_during_launch_reaps_process_after_handle_arrives(tmp_path, monkeypatch):
    async def exercise():
        original = asyncio.create_subprocess_exec
        launched = asyncio.Event()
        release = asyncio.Event()
        processes = []

        async def launch(*args, **kwargs):
            process = await original(*args, **kwargs)
            processes.append(process)
            launched.set()
            await release.wait()
            return process

        monkeypatch.setattr("logox.tools.fs_grep._command", lambda *_: [sys.executable, "-c", "import time;time.sleep(60)"])
        monkeypatch.setattr("logox.tools.fs_grep.shutil.which", lambda _: sys.executable)
        monkeypatch.setattr(asyncio, "create_subprocess_exec", launch)
        task = asyncio.create_task(GrepTool().run(GrepArgs(pattern="needle"), ToolContext(cwd=tmp_path)))
        try:
            await asyncio.wait_for(launched.wait(), 5)
            task.cancel()
            await asyncio.sleep(0)
            release.set()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(task, 5)
            assert processes[0].returncode is not None
        finally:
            release.set()
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            for process in processes:
                if process.returncode is None:
                    process.kill()
                await process.wait()

    asyncio.run(exercise())
