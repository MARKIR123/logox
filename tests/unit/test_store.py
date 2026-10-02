"""M8 会话持久化、多会话分桶与回放测试 (MODULE_store.md)。"""

from __future__ import annotations

from types import SimpleNamespace

import json
import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from logox.kernel.messages import Message, ToolUseBlock
from logox.store.manager import SessionManager
from logox.store.replay import reconstruct_messages, replay_session
from logox.store.slug import slugify_cwd, unslug_cwd_hint


class FakeBuffer:
    def __init__(self) -> None:
        self.users: list[str] = []
        self.deltas: list[str] = []
        self.tools: list[tuple[str, str]] = []

    def add_user(self, text: str) -> None:
        self.users.append(text)

    def add_delta(self, text: str) -> None:
        self.deltas.append(text)

    def flush_delta(self) -> None:
        pass

    def start_tool(self, call_id: str, name: str, args_summary: str = "") -> None:
        self.tools.append((call_id, name))

    def finish_tool(self, call_id: str, ok: bool) -> None:
        pass


class FakeTimeline:
    def __init__(self) -> None:
        self.buffer = FakeBuffer()


class FakeLoop:
    def __init__(self) -> None:
        self.history: list[Message] = []


class SlugTests(unittest.TestCase):
    def test_slugify_cwd_windows_and_posix(self) -> None:
        slug_win = slugify_cwd(Path(r"G:\hz\codes\Logox"))
        self.assertTrue(slug_win.startswith("--G-hz-codes-Logox--_"))
        self.assertEqual(len(slug_win.rsplit("_", 1)[1]), 8)

        slug_posix = slugify_cwd("/home/user/project_repo")
        self.assertTrue(slug_posix.startswith("--home-user-project-repo--_"))
        self.assertEqual(len(slug_posix.rsplit("_", 1)[1]), 8)

    def test_slug_collision_defense_with_hash(self) -> None:
        # 路径字母相同但结构不同，依靠 sha256 短哈希防碰撞
        slug1 = slugify_cwd("a/b-c")
        slug2 = slugify_cwd("a-b/c")
        self.assertNotEqual(slug1, slug2)

    def test_unslug_hint(self) -> None:
        slug = slugify_cwd(r"C:\workspace\my_project")
        hint = unslug_cwd_hint(slug)
        self.assertIn("workspace-my-project", hint)


class SessionManagerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = TemporaryDirectory()
        self.base_dir = Path(self.temp_dir.name)
        self.mgr = SessionManager(base_sessions_dir=self.base_dir)
        self.cwd = Path(self.temp_dir.name) / "workspaces" / "project1"
        self.cwd.mkdir(parents=True, exist_ok=True)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_empty_workspace_returns_empty_and_no_recent(self) -> None:
        sessions = self.mgr.list_sessions(self.cwd)
        self.assertEqual(sessions, [])
        self.assertIsNone(self.mgr.find_most_recent(self.cwd))

    def test_create_session_and_list_ordered_by_mtime(self) -> None:
        s1 = self.mgr.create_session(self.cwd, initial_title="第一个任务：重构状态栏")
        time.sleep(0.02)
        s2 = self.mgr.create_session(self.cwd, initial_title="第二个任务：修复权限弹窗")

        sessions = self.mgr.list_sessions(self.cwd)
        self.assertEqual(len(sessions), 2)
        # 最近活跃排在最前面
        self.assertEqual(sessions[0].session_id, s2.session_id)
        self.assertEqual(sessions[1].session_id, s1.session_id)

        recent = self.mgr.find_most_recent(self.cwd)
        assert recent is not None
        self.assertEqual(recent.session_id, s2.session_id)

    def test_extract_summary_cleans_prompts(self) -> None:
        raw = "```python\n# 这是一个超长的测试代码指令\n```\n帮我写一个快速排序算法并在 main 函数里测试它"
        summary = self.mgr.extract_summary(raw, max_len=15)
        self.assertLessEqual(len(summary), 18)
        self.assertNotIn("\n", summary)
        self.assertNotIn("```", summary)

    def test_extract_summary_does_not_truncate_by_default(self) -> None:
        long_prompt = "我现在要把 logox项目写入我的简历，目前是这样的，需要完善各项技能与经历描述"
        summary = self.mgr.extract_summary(long_prompt)
        self.assertEqual(summary, long_prompt)
        self.assertNotIn("...", summary)

    def test_scan_session_metadata_prioritizes_turn_summary(self) -> None:
        s = self.mgr.create_session(self.cwd, initial_title="待更新")
        with open(s.file_path, "a", encoding="utf-8") as f:
            f.write(json.dumps({"turn": 1, "role": "user", "type": "user_prompt", "content": "你好"}) + "\n")
            f.write(json.dumps({"turn": 1, "role": "system", "type": "turn_finished", "turn_summary": "向用户问好并介绍自身能力"}) + "\n")
            f.write(json.dumps({"turn": 2, "role": "user", "type": "user_prompt", "content": "帮我写简历"}) + "\n")
            f.write(json.dumps({"turn": 2, "role": "system", "type": "turn_finished", "turn_summary": "梳理项目架构并提炼简历难点与排查案例"}) + "\n")

        info = self.mgr.scan_session_metadata(s.file_path, cwd=str(self.cwd))
        self.assertIsNotNone(info)
        # 因为第一轮是简单问候，自动提取第二轮有实质意义的摘要
        self.assertEqual(info.title_summary, "梳理项目架构并提炼简历难点与排查案例")

    def test_scan_session_metadata_reads_jsonl_content(self) -> None:
        s = self.mgr.create_session(self.cwd, initial_title="待更新")
        with open(s.file_path, "a", encoding="utf-8") as f:
            f.write(json.dumps({"turn": 1, "role": "user", "type": "user_prompt", "content": "实现多会话持久化"}) + "\n")
            f.write(json.dumps({"turn": 1, "role": "assistant", "type": "model_output", "content": "好的已开始"}) + "\n")
            f.write(json.dumps({"turn": 2, "role": "user", "type": "user_prompt", "content": "再加一个单元测试"}) + "\n")
        info = self.mgr.scan_session_metadata(s.file_path, cwd=str(self.cwd))
        self.assertIsNotNone(info)
        self.assertEqual(info.title_summary, "实现多会话持久化")
        self.assertEqual(info.turn_count, 2)

    def test_delete_session_soft_moves_to_trash(self) -> None:
        s = self.mgr.create_session(self.cwd, initial_title="待删除的会话")
        self.assertTrue(s.file_path.exists())

        dest = self.mgr.delete_session(s.file_path, soft=True)
        self.assertFalse(s.file_path.exists())
        self.assertTrue(dest.exists())
        self.assertEqual(dest.parent.name, ".trash")

        # 验证 list_sessions 不再包含已删除会话
        sessions = self.mgr.list_sessions(self.cwd)
        self.assertEqual(len(sessions), 0)

    def test_delete_session_hard_removes_file(self) -> None:
        s = self.mgr.create_session(self.cwd, initial_title="彻底销毁的会话")
        dest = self.mgr.delete_session(s.file_path, soft=False)
        self.assertFalse(s.file_path.exists())
        self.assertFalse(dest.exists())

    def test_delete_session_not_found_raises(self) -> None:
        fake_path = self.cwd / "non_existent.jsonl"
        with self.assertRaises(FileNotFoundError):
            self.mgr.delete_session(fake_path)


from logox.tui.content.timeline import TimelineBuffer

class DisplayHintPipelineTests(unittest.TestCase):
    """★ **D139**：展示提示（diff）要**两条到达路径**都覆盖：实时事件流 + `/resume` 回放。

    （D138 的教训：只修一条路径 = 用户看到"一半好一半坏"，那是最难查的 bug。）
    """

    def _display(self) -> dict:
        return {
            "kind": "diff",
            "payload": {
                "path": "sample.py",
                "hunks": [
                    {
                        "header": "@@ -1,1 +1,1 @@",
                        "lines": [["del", "old"], ["add", "new"]],
                    }
                ],
            },
        }

    def test_replayed_record_restores_the_diff_block(self) -> None:
        from logox.store.replay import replay_into_timeline

        records = [
            {"type": "user_prompt", "role": "user", "content": "改一下"},
            {
                "type": "tool_result",
                "role": "tool",
                "tool": "edit",
                "call_id": "c1",
                "content": "已成功编辑文件 sample.py（+1 -1 行）",
                "meta": {
                    "duration_ms": 7,
                    "args": {"path": "sample.py", "old_string": "old", "new_string": "new"},
                    "display": self._display(),
                },
            },
        ]
        timeline = SimpleNamespace(buffer=TimelineBuffer())
        replay_into_timeline(records, timeline)
        kinds = [b.kind for b in timeline.buffer.blocks]
        self.assertIn("diff", kinds, "回放没把 diff 恢复出来（D139）")
        diff_block = next(b for b in timeline.buffer.blocks if b.kind == "diff")
        self.assertEqual(diff_block.hunks[0].lines[0], ("del", "old"))

    def test_replay_builds_the_full_args_text(self) -> None:
        from logox.store.replay import replay_into_timeline
        from logox.tui.format import format_args

        records = [
            {
                "type": "tool_result",
                "role": "tool",
                "tool": "edit",
                "call_id": "c1",
                "content": "ok",
                "meta": {"duration_ms": 1, "args": {"path": "sample.py", "old_string": "老", "new_string": "新"}},
            }
        ]
        timeline = SimpleNamespace(buffer=TimelineBuffer())
        replay_into_timeline(records, timeline, format_args=format_args)
        card = next(b for b in timeline.buffer.blocks if b.kind == "tool")
        self.assertIn("old_string", card.args_text)

    def test_rich_display_suppresses_the_duplicate_plain_text(self) -> None:
        """有自带渲染器的展示提示时，卡片不再重复铺一遍同样的文本。"""
        from logox.store.replay import replay_into_timeline

        records = [
            {
                "type": "tool_result",
                "role": "tool",
                "tool": "edit",
                "call_id": "c1",
                "content": "已成功编辑文件 sample.py（+1 -1 行）",
                "meta": {"duration_ms": 7, "args": {}, "display": self._display()},
            }
        ]
        timeline = SimpleNamespace(buffer=TimelineBuffer())
        replay_into_timeline(records, timeline)
        card = next(b for b in timeline.buffer.blocks if b.kind == "tool")
        self.assertEqual(card.payload, "")
        self.assertEqual(len([b for b in timeline.buffer.blocks if b.kind == "diff"]), 1)


class DisplayHintRoundTripTests(unittest.TestCase):
    """★ **D139 往返**：工具给的 diff 提示 → 写盘 → `/resume` 回放 → 还是 diff 块。"""

    def setUp(self) -> None:
        from logox.context.storage import SessionTranscriptWriter
        from logox.store.persistence import SessionPersistenceSubscriber

        self.temp_dir = TemporaryDirectory()
        self.log_file = Path(self.temp_dir.name) / "transcript.jsonl"
        self.writer = SessionTranscriptWriter(log_file=self.log_file)
        self.subscriber = SessionPersistenceSubscriber(self.writer)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_display_survives_the_round_trip(self) -> None:
        import json

        from logox.kernel import events as ev
        from logox.store.replay import load_session_records, replay_into_timeline

        self.subscriber.apply(
            ev.ToolCallRequested(
                session_id="test",
                turn=1,
                call_id="c1",
                name="edit",
                args={"path": "sample.py", "old_string": "old", "new_string": "new"},
            )
        )
        self.subscriber.apply(
            ev.ToolCallFinished(
                session_id="test",
                turn=1,
                call_id="c1",
                ok=True,
                duration_ms=5,
                content="已成功编辑文件 sample.py（+1 -1 行）",
                change_stat=ev.ChangeStat(kind="modify", added=1, removed=1),
                display=ev.ToolDisplay(
                    kind="diff",
                    payload={
                        "path": "sample.py",
                        "hunks": [{"header": "@@ -1 +1 @@", "lines": [["del", "old"], ["add", "new"]]}],
                    },
                ),
            )
        )
        if hasattr(self.writer, "flush"):
            self.writer.flush()

        raw = [
            json.loads(line)
            for line in self.log_file.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        tool_records = [r for r in raw if r.get("role") == "tool"]
        self.assertEqual(len(tool_records), 1)
        self.assertEqual(
            tool_records[0]["meta"]["display"]["kind"], "diff", "写盘时没带上展示提示"
        )

        # 回放同一份记录 → 仍然是一张带 diff 的工具卡
        records = load_session_records(self.log_file)
        timeline = SimpleNamespace(buffer=TimelineBuffer())
        replay_into_timeline(records, timeline)
        self.assertIn("diff", [b.kind for b in timeline.buffer.blocks])


class ReplayToolCardPayloadTests(unittest.TestCase):
    """★ **F-43 第二半（D138）**：`/resume` 回放出来的工具卡必须**也能展开看到输出**。

    背景：新会话的卡片由事件流驱动，`Ctrl+O` 能展开（D136 已修）；
    但 `/resume` 走的是**另一条路径** —— `replay_into_timeline()` 从 transcript 记录**重建**卡片。
    那条路径以前：① 不传 `payload`（展开后永远空白）；② 摘要用 `str(meta["args"])`（原始字典 dump）。
    用户报障原话："还是看不到"、"折不折叠的内容都是一样的" —— 正是这条路径。
    """

    def _records(self) -> list[dict]:
        return [
            {"type": "user_prompt", "role": "user", "content": "查一下 git 状态"},
            {
                "type": "tool_result",
                "role": "tool",
                "tool": "shell",
                "call_id": "c1",
                "content": "第 1 行：45 个文件\n第 2 行：docs/ 被忽略",
                "meta": {"duration_ms": 1300, "args": {"command": "git status --short"}},
            },
        ]

    def test_replayed_tool_card_carries_the_output(self) -> None:
        from logox.store.replay import replay_into_timeline

        timeline = SimpleNamespace(buffer=TimelineBuffer())
        replay_into_timeline(self._records(), timeline)
        block = next(b for b in timeline.buffer.blocks if b.kind == "tool")
        self.assertIn("45 个文件", block.payload, "回放没把工具输出带进卡片（F-43 第二半复发）")

    def test_replayed_summary_is_not_a_raw_dict_dump(self) -> None:
        from logox.store.replay import replay_into_timeline
        from logox.tui.format import summarize_args

        timeline = SimpleNamespace(buffer=TimelineBuffer())
        replay_into_timeline(self._records(), timeline, summarize=summarize_args)
        block = next(b for b in timeline.buffer.blocks if b.kind == "tool")
        self.assertEqual(block.args_summary, "git status --short")
        self.assertNotIn("{'command'", block.args_summary, "回放卡片上不该摊原始字典")

    def test_replay_without_a_summarizer_still_works(self) -> None:
        """非 TUI 消费者（脚本）不传摘要函数时，回放**不能炸**。"""
        from logox.store.replay import replay_into_timeline

        timeline = SimpleNamespace(buffer=TimelineBuffer())
        count = replay_into_timeline(self._records(), timeline)
        self.assertGreaterEqual(count, 1)

class ReplayTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = TemporaryDirectory()
        self.file_path = Path(self.temp_dir.name) / "test_session.jsonl"

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_replay_session_restores_history_and_timeline(self) -> None:
        with open(self.file_path, "w", encoding="utf-8") as f:
            f.write(json.dumps({"type": "session_init", "session_id": "test"}) + "\n")
            f.write(json.dumps({"turn": 1, "role": "user", "type": "user_prompt", "content": "列出当前目录"}) + "\n")
            f.write(
                json.dumps(
                    {
                        "turn": 1,
                        "role": "assistant",
                        "type": "model_output",
                        "content": "正在为您读取",
                        "tool": "read",
                        "call_id": "c1",
                    }
                )
                + "\n"
            )
            f.write(
                json.dumps(
                    {
                        "turn": 1,
                        "role": "tool",
                        "call_id": "c1",
                        "content": "file1.txt\nfile2.txt",
                    }
                )
                + "\n"
            )

        loop = FakeLoop()
        timeline = FakeTimeline()
        msg_count, tl_count = replay_session(self.file_path, kernel_loop=loop, timeline=timeline)

        self.assertEqual(msg_count, 3)
        self.assertEqual(len(loop.history), 3)
        self.assertEqual(loop.history[0].role, "user")
        self.assertEqual(loop.history[1].role, "assistant")
        self.assertEqual(loop.history[2].role, "tool")

        self.assertEqual(timeline.buffer.users, ["列出当前目录"])
        self.assertEqual(timeline.buffer.deltas, ["正在为您读取"])
        self.assertEqual(timeline.buffer.tools, [("c1", "read")])

    def test_reconstruct_messages_self_heals_missing_assistant_tool_calls(self) -> None:
        """测试历史会话中 assistant 缺少 tool_calls 时自愈补齐（防止大模型 400 错误）。"""
        records = [
            {"turn": 1, "role": "user", "type": "user_prompt", "content": "查一下总线"},
            # assistant 记录没有 tool_calls（早期持久化记录或被拆分）
            {"turn": 1, "role": "assistant", "type": "model_output", "content": "我来查看代码"},
            # 紧随其后的两个工具调用
            {"turn": 1, "role": "tool", "type": "tool_result", "content": "ok", "tool": "glob", "call_id": "c_glob", "meta": {"args": {"pattern": "*"}}},
            {"turn": 1, "role": "tool", "type": "tool_result", "content": "bus code", "tool": "read", "call_id": "c_read", "meta": {"args": {"path": "bus.py"}}},
            # 最终回答
            {"turn": 1, "role": "assistant", "type": "model_output", "content": "总线实现如下..."},
        ]
        messages = reconstruct_messages(records)
        self.assertEqual(len(messages), 5)
        self.assertEqual(messages[0].role, "user")
        self.assertEqual(messages[1].role, "assistant")
        self.assertEqual(messages[2].role, "tool")
        self.assertEqual(messages[3].role, "tool")
        self.assertEqual(messages[4].role, "assistant")

        # 核心断言：前置 assistant 消息已被自愈补齐了对应的两个 ToolUseBlock
        tool_uses = messages[1].blocks_of(ToolUseBlock)
        self.assertEqual(len(tool_uses), 2)
        self.assertEqual(tool_uses[0].id, "c_glob")
        self.assertEqual(tool_uses[0].name, "glob")
        self.assertEqual(tool_uses[0].input, {"pattern": "*"})
        self.assertEqual(tool_uses[1].id, "c_read")
        self.assertEqual(tool_uses[1].name, "read")
        self.assertEqual(tool_uses[1].input, {"path": "bus.py"})

        # 断言转换为 OpenAI 兼容消息时，合规且不抛 400 格式错
        from logox.providers.openai_compat import OpenAICompatProvider
        p = OpenAICompatProvider()
        oai_msgs = [entry for m in messages for entry in p._convert_message(m, model="deepseek-chat")]
        self.assertEqual(oai_msgs[1]["role"], "assistant")
        self.assertIn("tool_calls", oai_msgs[1])
        self.assertEqual(len(oai_msgs[1]["tool_calls"]), 2)
        self.assertEqual(oai_msgs[2]["role"], "tool")
        self.assertEqual(oai_msgs[2]["tool_call_id"], "c_glob")
        self.assertEqual(oai_msgs[3]["role"], "tool")
        self.assertEqual(oai_msgs[3]["tool_call_id"], "c_read")


class CliSessionResolutionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = TemporaryDirectory()
        self.base_dir = Path(self.temp_dir.name)
        self.cwd = self.base_dir / "my_project"
        self.cwd.mkdir(parents=True, exist_ok=True)

        class Paths:
            sessions = self.base_dir / "sessions"

        self.paths = Paths()
        self.mgr = SessionManager(base_sessions_dir=self.paths.sessions)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_format_time_ago(self) -> None:
        from logox.cli import _format_time_ago

        now = 1700000000.0
        self.assertEqual(_format_time_ago(now - 10, now), "刚刚")
        self.assertEqual(_format_time_ago(now - 300, now), "5分钟前")

    def test_resolve_session_empty_cwd_returns_none(self) -> None:
        import argparse

        from logox.cli import _resolve_session

        # 既没指定 -c 也没 -r
        args_none = argparse.Namespace(continue_session=False, resume_session=False)
        self.assertIsNone(_resolve_session(args_none, self.cwd, self.paths))

        # 指定了 -c 但没有任何历史
        args_c = argparse.Namespace(continue_session=True, resume_session=False)
        self.assertIsNone(_resolve_session(args_c, self.cwd, self.paths))

        # 指定了 -r 但没有任何历史 -> 直接返回 None (拉起新对话)
        args_r = argparse.Namespace(continue_session=False, resume_session=True)
        self.assertIsNone(_resolve_session(args_r, self.cwd, self.paths))

    def test_resolve_session_continue_picks_most_recent(self) -> None:
        import argparse

        from logox.cli import _resolve_session

        _ = self.mgr.create_session(self.cwd, initial_title="旧会话")
        time.sleep(0.02)
        s2 = self.mgr.create_session(self.cwd, initial_title="新会话")

        # 默认无参数启动：自动加载最近一次活跃会话
        args_default = argparse.Namespace(new_session=False, continue_session=False, resume_session=False)
        resolved = _resolve_session(args_default, self.cwd, self.paths)
        self.assertEqual(resolved, s2.file_path)

        # 显式 -n / --new：开启新会话（返回 None）
        args_new = argparse.Namespace(new_session=True, continue_session=False, resume_session=False)
        resolved_new = _resolve_session(args_new, self.cwd, self.paths)
        self.assertIsNone(resolved_new)

        # 显式 -c：加载最近一次
        args_c = argparse.Namespace(new_session=False, continue_session=True, resume_session=False)
        resolved_c = _resolve_session(args_c, self.cwd, self.paths)
        self.assertEqual(resolved_c, s2.file_path)

    def test_resolve_session_resume_default_enter_picks_recent(self) -> None:
        import argparse
        import io
        from unittest.mock import patch

        from logox.cli import _resolve_session

        _ = self.mgr.create_session(self.cwd, initial_title="旧会话")
        time.sleep(0.02)
        s2 = self.mgr.create_session(self.cwd, initial_title="新会话")

        args = argparse.Namespace(continue_session=False, resume_session=True)

        # 模拟用户在控制台直接敲 Enter
        with patch("sys.stdin", io.StringIO("\n")), patch("sys.stdin.isatty", return_value=True):
            resolved = _resolve_session(args, self.cwd, self.paths)
            self.assertEqual(resolved, s2.file_path)

    def test_resolve_session_resume_choose_number_and_new(self) -> None:
        import argparse
        import io
        from unittest.mock import patch

        from logox.cli import _resolve_session

        s1 = self.mgr.create_session(self.cwd, initial_title="旧会话")
        time.sleep(0.02)
        _ = self.mgr.create_session(self.cwd, initial_title="新会话")

        args = argparse.Namespace(continue_session=False, resume_session=True)

        # 用户选 2 (旧会话)
        with patch("sys.stdin", io.StringIO("2\n")), patch("sys.stdin.isatty", return_value=True):
            resolved = _resolve_session(args, self.cwd, self.paths)
            self.assertEqual(resolved, s1.file_path)

        # 用户选 n (新会话)
        with patch("sys.stdin", io.StringIO("n\n")), patch("sys.stdin.isatty", return_value=True):
            resolved = _resolve_session(args, self.cwd, self.paths)
            self.assertIsNone(resolved)


class SessionPersistenceSubscriberTests(unittest.TestCase):
    """测试会话持久化订阅者实时落盘与大模型 reasoning 支持。"""

    def setUp(self) -> None:
        self.temp_dir = TemporaryDirectory()
        self.log_file = Path(self.temp_dir.name) / "transcript.jsonl"
        from logox.context.storage import SessionTranscriptWriter
        from logox.store.persistence import SessionPersistenceSubscriber

        self.writer = SessionTranscriptWriter(log_file=self.log_file)
        self.subscriber = SessionPersistenceSubscriber(self.writer)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_full_turn_with_reasoning_and_tool_call(self) -> None:
        from logox.kernel import events as ev

        # 1. 用户输入
        self.subscriber.apply(
            ev.UserPromptSubmit(session_id="test", turn=1, text="读取代码并分析", text_chars=7)
        )

        # 2. 模型输出思考流和正文流
        self.subscriber.apply(
            ev.ModelDelta(session_id="test", turn=1, kind="reasoning", delta="我需要先调用工具", request_index=0)
        )
        self.subscriber.apply(
            ev.ModelDelta(session_id="test", turn=1, kind="text", delta="好的，我来看看", request_index=0)
        )

        # 3. 请求结束（关键点：含 reasoning，不能抛 TypeError）
        self.subscriber.apply(
            ev.ModelRequestFinished(
                session_id="test",
                turn=1,
                usage=ev.Usage(input_tokens=100, output_tokens=50),
                duration_ms=450,
            )
        )

        # 4. 工具请求与完成
        self.subscriber.apply(
            ev.ToolCallRequested(
                session_id="test",
                turn=1,
                call_id="call_123",
                name="read",
                args={"path": "README.md"},
            )
        )
        self.subscriber.apply(
            ev.ToolCallFinished(
                session_id="test",
                turn=1,
                call_id="call_123",
                ok=True,
                duration_ms=12,
            )
        )

        # 5. 回合结束
        self.subscriber.apply(
            ev.TurnFinished(
                session_id="test",
                turn=1,
                turn_index=1,
                duration_ms=500,
                tool_call_count=1,
                usage=ev.Usage(input_tokens=100, output_tokens=50),
            )
        )

        # 验证 JSONL 文件内容
        records = [json.loads(line) for line in self.log_file.read_text(encoding="utf-8").splitlines()]
        self.assertEqual(len(records), 3)

        # 记录 1: user
        self.assertEqual(records[0]["role"], "user")
        self.assertEqual(records[0]["content"], "读取代码并分析")

        # 记录 2: assistant，必须带 reasoning 与 meta
        self.assertEqual(records[1]["role"], "assistant")
        self.assertEqual(records[1]["content"], "好的，我来看看")
        self.assertEqual(records[1]["reasoning"], "我需要先调用工具")
        self.assertEqual(records[1]["meta"]["input_tokens"], 100)

        # 记录 3: tool
        self.assertEqual(records[2]["role"], "tool")
        self.assertEqual(records[2]["tool"], "read")
        self.assertEqual(records[2]["call_id"], "call_123")

    def test_turn_finished_writes_the_summary_exactly_once(self) -> None:
        """**漂移守卫**：`turn_finished` 记录里不得出现 `content`（用户裁定 · 方案 A）。

        守的是什么
        ----------
        `TurnFinished` 事件只有 `turn_summary` 一个摘要字段。早先的写入方却多写了
        一句 `content=event.turn_summary` —— 那不是第二条信息，而是**从同一份数据
        抄出来的副本**，于是磁盘上出现两个"必须永远相等"的字段（双事实来源，
        本项目登记的 F-03 反模式）。一旦分叉，读取方会静默偏向 `turn_summary`，
        `content` 变成陈旧数据**而没有任何地方报错**。

        为什么要有这条用例而不是只改代码
        ---------------------------------
        那一行看着"很自然"（其他记录类型都带 content），**很容易被顺手加回来**。
        实测它占了全会话文件 0.054%、又不影响任何断言，所以**改回去不会有测试变红**。
        没有守卫的话，这个决定会在一两周内被无声撤销。

        同时钉住读侧仍然兼容（回退分支**不得**被顺手删掉）：本用例只断言
        **写侧不再产生** `content`，不断言"没有 content 的记录读不出来"。
        """
        from logox.kernel import events as ev

        self.subscriber.apply(
            ev.TurnFinished(
                session_id="test",
                turn=1,
                turn_index=1,
                duration_ms=500,
                tool_call_count=2,
                usage=ev.Usage(input_tokens=100, output_tokens=50),
                turn_summary="改了 fs_edit.py 并补 3 条用例",
                summary_source="model_last_line",
            )
        )

        records = [
            json.loads(line)
            for line in self.log_file.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        self.assertEqual(len(records), 1, "应当只写了一条 turn_finished")

        record = records[0]
        self.assertEqual(record["type"], "turn_finished")
        # ① 摘要在且只有一份
        self.assertEqual(record["turn_summary"], "改了 fs_edit.py 并补 3 条用例")
        # ② ★ 不得有派生副本 —— 这一条就是守卫本身
        self.assertNotIn(
            "content",
            record,
            "`content` 是 `turn_summary` 的派生副本（双事实来源），不得再写回磁盘",
        )
        # ③ 独立信息必须都还在（别把有用的字段一起删了）
        self.assertEqual(record["summary_source"], "model_last_line")
        self.assertEqual(record["reason"], "completed")
        self.assertEqual(record["tool_call_count"], 2)


if __name__ == "__main__":
    unittest.main()
