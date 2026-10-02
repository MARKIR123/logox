"""M8 检查点快照与时空穿梭回滚系统单元测试 (MODULE_rewind.md)。"""

from __future__ import annotations

import asyncio
import hashlib
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

from logox.context.storage import SessionTranscriptWriter
from logox.kernel import events as ev
from logox.kernel.bus import EventBus
from logox.kernel.messages import Message, TextBlock
from logox.kernel.registry import ToolRegistry
from logox.kernel.scheduler import AllowAllDecider, Scheduler
from logox.kernel.turn import Turn
from logox.providers.base import ToolCallEvent
from logox.store.blob import BlobStore
from logox.store.checkpoint import CheckpointTracker
from logox.store.persistence import SessionPersistenceSubscriber
from logox.store.replay import (
    filter_rewound_records,
    load_session_records,
    replay_session,
)
from logox.store.rewind import check_conflicts, execute_rewind
from logox.tools.fs_edit import EditTool
from logox.tools.fs_write import WriteTool
from logox.tui.commands import resolve
from logox.tui.render.commands import CommandRunner


class BlobStoreTests(unittest.TestCase):
    def test_put_bytes_and_two_level_sharding(self) -> None:
        with TemporaryDirectory() as tmp:
            store = BlobStore(Path(tmp))
            data = b"def add(a, b):\n    return a + b\n"
            expected_hash = hashlib.sha256(data).hexdigest()

            h1 = store.put_bytes(data)
            self.assertEqual(h1, expected_hash)

            # 验证两级分片结构
            shard_dir = Path(tmp) / expected_hash[:2]
            blob_file = shard_dir / expected_hash[2:]
            self.assertTrue(shard_dir.is_dir())
            self.assertTrue(blob_file.is_file())
            self.assertEqual(blob_file.read_bytes(), data)

            # 幂等写入测试
            h2 = store.put_bytes(data)
            self.assertEqual(h2, expected_hash)

    def test_put_file_and_get_bytes(self) -> None:
        with TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            store = BlobStore(tmp_path / "blobs")
            sample_file = tmp_path / "sample.py"
            sample_file.write_text("print('hello')", encoding="utf-8")

            h = store.put_file(sample_file)
            self.assertTrue(store.has(h))
            self.assertEqual(store.get_bytes(h), b"print('hello')")

            # 不存在的哈希与文件
            self.assertFalse(store.has("0" * 64))
            self.assertIsNone(store.get_bytes("0" * 64))
            with self.assertRaises(FileNotFoundError):
                store.put_file(tmp_path / "non_existent.py")

    def test_restore_to_file(self) -> None:
        with TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            store = BlobStore(tmp_path / "blobs")
            h = store.put_bytes(b"recovered content")

            target = tmp_path / "subdir" / "restored.txt"
            ok = store.restore_to_file(h, target)
            self.assertTrue(ok)
            self.assertTrue(target.is_file())
            self.assertEqual(target.read_bytes(), b"recovered content")


class CheckpointTrackerTests(unittest.TestCase):
    def test_extract_checkpoints_and_session_rewind(self) -> None:
        records = [
            {"type": "user_prompt", "turn": 1, "content": "创建 main.py 文件"},
            {"type": "checkpoint", "turn": 1, "path": "main.py", "before_hash": None, "after_hash": "hash_v1"},
            {"type": "user_prompt", "turn": 2, "content": "修改 main.py 并增加 utils.py"},
            {"type": "checkpoint", "turn": 2, "path": "main.py", "before_hash": "hash_v1", "after_hash": "hash_v2"},
            {"type": "checkpoint", "turn": 2, "path": "utils.py", "before_hash": None, "after_hash": "hash_u1"},
            {"type": "user_prompt", "turn": 3, "content": "重构 utils.py"},
            {"type": "checkpoint", "turn": 3, "path": "utils.py", "before_hash": "hash_u1", "after_hash": "hash_u2"},
        ]

        cps = CheckpointTracker.extract_turn_checkpoints(records)
        self.assertEqual(len(cps), 3)
        self.assertEqual(cps[0].turn, 1)
        self.assertEqual(len(cps[0].files), 1)
        self.assertIn("创建 main.py", cps[0].user_prompt)

        self.assertEqual(cps[1].turn, 2)
        self.assertEqual(len(cps[1].files), 2)

        # 模拟执行了 session_rewind to_turn=2
        records.append({"type": "session_rewind", "to_turn": 2})
        cps_after_rewind = CheckpointTracker.extract_turn_checkpoints(records)
        self.assertEqual(len(cps_after_rewind), 1)
        self.assertEqual(cps_after_rewind[0].turn, 1)

    def test_extract_checkpoints_with_pure_conversation_and_turn_summary(self) -> None:
        """测试包含纯对话（无物理文件修改）时的检查点与 turn_summary 提取。"""
        records = [
            {"type": "user_prompt", "turn": 1, "content": "请分析架构设计"},
            {"type": "turn_finished", "turn": 1, "content": "详细分析了架构分层与总线机制", "turn_summary": "详细分析了架构分层与总线机制"},
            {"type": "user_prompt", "turn": 2, "content": "创建 config.toml"},
            {"type": "checkpoint", "turn": 2, "path": "config.toml", "before_hash": None, "after_hash": "hash_cfg"},
            {"type": "turn_finished", "turn": 2, "content": "创建了配置文件", "turn_summary": "创建了配置文件"},
        ]
        cps = CheckpointTracker.extract_turn_checkpoints(records)
        self.assertEqual(len(cps), 2)
        # 轮次 1：纯对话
        self.assertEqual(cps[0].turn, 1)
        self.assertEqual(cps[0].files, [])
        self.assertEqual(cps[0].turn_summary, "详细分析了架构分层与总线机制")
        # 轮次 2：修改了文件
        self.assertEqual(cps[1].turn, 2)
        self.assertEqual(len(cps[1].files), 1)
        self.assertEqual(cps[1].turn_summary, "创建了配置文件")


class RewindEngineTests(unittest.TestCase):
    def test_conflict_detection_and_execute_rewind(self) -> None:
        with TemporaryDirectory() as tmp:
            cwd = Path(tmp)
            store = BlobStore(cwd / ".blobs")

            # 初始状态：创建 a.txt
            content_v1 = b"version 1"
            content_v2 = b"version 2"
            content_b = b"file b content"

            h_v1 = store.put_bytes(content_v1)
            h_v2 = store.put_bytes(content_v2)
            h_b = store.put_bytes(content_b)

            file_a = cwd / "a.txt"
            file_b = cwd / "b.txt"

            file_a.write_bytes(content_v2)
            file_b.write_bytes(content_b)

            records = [
                {"type": "checkpoint", "turn": 1, "path": "a.txt", "before_hash": None, "after_hash": h_v1},
                {"type": "checkpoint", "turn": 2, "path": "a.txt", "before_hash": h_v1, "after_hash": h_v2},
                {"type": "checkpoint", "turn": 2, "path": "b.txt", "before_hash": None, "after_hash": h_b},
            ]

            # 1. 无外部冲突
            conflicts = check_conflicts(records, to_turn=2, cwd=cwd)
            self.assertEqual(conflicts, [])

            # 2. 发生外部修改冲突 (External Drift)
            file_a.write_bytes(b"modified by human in vs code")
            conflicts = check_conflicts(records, to_turn=2, cwd=cwd)
            self.assertEqual(len(conflicts), 1)
            self.assertEqual(conflicts[0].path, "a.txt")
            self.assertEqual(conflicts[0].expected_hash, h_v2)

            # force=False 拒绝执行
            res_refused = execute_rewind(records, to_turn=2, cwd=cwd, blob_store=store, force=False)
            self.assertFalse(res_refused.success)
            self.assertEqual(file_a.read_bytes(), b"modified by human in vs code")

            # force=True 强制覆盖
            res_forced = execute_rewind(records, to_turn=2, cwd=cwd, blob_store=store, force=True)
            self.assertTrue(res_forced.success)
            # a.txt 恢复至轮次 2 修改前的 h_v1
            self.assertEqual(file_a.read_bytes(), content_v1)
            # b.txt 是在轮次 2 新建的，应被安全删除
            self.assertFalse(file_b.exists())
            self.assertIn("a.txt", res_forced.restored_files)
            self.assertIn("b.txt", res_forced.deleted_files)


class SchedulerInterceptionTests(unittest.TestCase):
    def test_scheduler_captures_snapshots(self) -> None:
        async def run_test() -> None:
            with TemporaryDirectory() as tmp:
                cwd = Path(tmp)
                store = BlobStore(cwd / ".blobs")
                bus = EventBus(session_id="test-scheduler-snapshots")

                captured_checkpoints: list[ev.CheckpointCreated] = []

                async def on_checkpoint(event: ev.CheckpointCreated) -> None:
                    captured_checkpoints.append(event)

                bus.subscribe(ev.CheckpointCreated, on_checkpoint, name="test-listener")


                registry = ToolRegistry()
                registry.register(WriteTool())
                registry.register(EditTool())

                scheduler = Scheduler(
                    bus,
                    registry,
                    AllowAllDecider(),
                    cwd=cwd,
                    blob_store=store,
                )

                turn = Turn(turn_index=1)

                # 1. 测试 WriteTool 新建文件
                call_write = ToolCallEvent(
                    call_id="call_1",
                    name="write",
                    arguments={"path": "hello.py", "content": "x = 1\n"},
                )
                res_write = await scheduler.run_batch(turn, [call_write])
                self.assertTrue(res_write[0].ok)
                await bus.drain()

                self.assertEqual(len(captured_checkpoints), 1)
                cp1 = captured_checkpoints[0]
                self.assertEqual(cp1.path, "hello.py")
                self.assertIsNone(cp1.before_hash)
                self.assertTrue(store.has(cp1.after_hash))

                # 2. 测试 EditTool 编辑现有文件
                turn2 = Turn(turn_index=2)
                call_edit = ToolCallEvent(
                    call_id="call_2",
                    name="edit",
                    arguments={"path": "hello.py", "old_string": "x = 1\n", "new_string": "x = 2\n"},
                )

                res_edit = await scheduler.run_batch(turn2, [call_edit])
                self.assertTrue(res_edit[0].ok)
                await bus.drain()

                self.assertEqual(len(captured_checkpoints), 2)
                cp2 = captured_checkpoints[1]
                self.assertEqual(cp2.path, "hello.py")
                self.assertEqual(cp2.before_hash, cp1.after_hash)
                self.assertTrue(store.has(cp2.after_hash))
                self.assertNotEqual(cp2.before_hash, cp2.after_hash)

        asyncio.run(run_test())


class PersistenceAndReplayTests(unittest.TestCase):
    def test_persistence_and_replay_filtering(self) -> None:
        with TemporaryDirectory() as tmp:
            cwd = Path(tmp)
            log_file = cwd / "transcript.jsonl"
            writer = SessionTranscriptWriter(log_file=log_file)
            subscriber = SessionPersistenceSubscriber(writer)

            subscriber.apply(ev.UserPromptSubmit(session_id="s1", turn=1, text="第一句", text_chars=3))
            subscriber.apply(
                ev.CheckpointCreated(
                    session_id="s1", turn=1, files=["a.py"], path="a.py", before_hash=None, after_hash="hash1"
                )
            )
            subscriber.apply(ev.UserPromptSubmit(session_id="s1", turn=2, text="第二句", text_chars=3))
            subscriber.apply(
                ev.CheckpointCreated(
                    session_id="s1", turn=2, files=["a.py"], path="a.py", before_hash="hash1", after_hash="hash2"
                )
            )

            records = load_session_records(log_file)
            self.assertEqual(len(records), 4)

            # 写入 session_rewind
            subscriber.apply(
                ev.RewindPerformed(
                    session_id="s1", turn=2, to_turn=2, restored=["a.py"], deleted=[], conflicts=[]
                )
            )
            records_updated = load_session_records(log_file)
            self.assertEqual(len(records_updated), 5)

            filtered = filter_rewound_records(records_updated)
            turns = [r.get("turn") for r in filtered]
            self.assertEqual(turns, [1, 1])

            class FakeLoop:
                def __init__(self) -> None:
                    self.history: list[Message] = []

            fake_loop = FakeLoop()
            msg_count, _ = replay_session(log_file, kernel_loop=fake_loop, timeline=None)
            self.assertEqual(msg_count, 1)
            self.assertEqual(fake_loop.history[0].blocks[0].text, "第一句")  # type: ignore[attr-defined]


class CommandRewindTests(unittest.TestCase):
    def test_rewind_command_no_checkpoints(self) -> None:
        async def run_test() -> None:
            notices: list[str] = []

            class FakeHost:
                def __init__(self) -> None:
                    self.runtime = type("FakeRuntime", (), {"list_checkpoints": lambda *a, **kw: []})()
                    self.theme = type("FakeTheme", (), {"palette": None})()
                    self.effort = "off"
                    self.content_width = 80
                    self.content_rows = 24

                def notice(self, msg: str, *, token: str = "text_muted") -> None:
                    notices.append(msg)

            host = FakeHost()
            runner = CommandRunner(host)  # type: ignore[arg-type]
            await runner.run(resolve("/rewind"))
            self.assertTrue(any("没有代码修改记录" in n for n in notices))

        asyncio.run(run_test())

    def test_undo_command_triggers_rewind(self) -> None:
        async def run_test() -> None:
            notices: list[str] = []
            rewound_turns: list[int] = []

            from logox.store.checkpoint import FileSnapshot, TurnCheckpoint

            class FakeHost:
                def __init__(self) -> None:
                    fake_cp = TurnCheckpoint(
                        turn=3,
                        files=[FileSnapshot(path="app.py", before_hash="h0", after_hash="h1")],
                        user_prompt="修改代码",
                    )
                    self.runtime = type(
                        "FakeRuntime",
                        (),
                        {
                            "list_checkpoints": lambda *a, **kw: [fake_cp],
                            "check_rewind_conflicts": lambda *a, **kw: [],
                        },
                    )()

                    self.theme = type("FakeTheme", (), {"palette": None})()
                    self.effort = "off"
                    self.content_width = 80
                    self.content_rows = 24

                def notice(self, msg: str, *, token: str = "text_muted") -> None:
                    notices.append(msg)

                def refresh_status(self) -> None:
                    pass

                async def rewind(self, to_turn: int, *, force: bool = False) -> Any:
                    rewound_turns.append(to_turn)
                    return type("Result", (), {"success": True, "message": f"成功回滚至轮次 {to_turn}"})()

            host = FakeHost()
            runner = CommandRunner(host)  # type: ignore[arg-type]
            await runner.run(resolve("/undo"))
            self.assertEqual(rewound_turns, [3])
            self.assertTrue(any("成功回滚至轮次 3" in n for n in notices))

        asyncio.run(run_test())


class RuntimeRewindIntegrationTests(unittest.TestCase):
    def test_runtime_rewind_and_checkpoint_listing(self) -> None:
        async def run_test() -> None:
            with TemporaryDirectory() as tmp:
                cwd = Path(tmp)
                log_file = cwd / "test_session.jsonl"
                store = BlobStore(cwd / ".blobs")

                # 模拟写入两轮代码修改
                h1 = store.put_bytes(b"initial v1")
                h2 = store.put_bytes(b"modified v2")

                f = cwd / "code.py"
                f.write_bytes(b"modified v2")

                writer = SessionTranscriptWriter(log_file=log_file)
                subscriber = SessionPersistenceSubscriber(writer)

                subscriber.apply(ev.UserPromptSubmit(session_id="s1", turn=1, text="写代码v1", text_chars=5))
                subscriber.apply(ev.ModelDelta(session_id="s1", turn=1, kind="text", delta="已写v1", request_index=0))

                subscriber.apply(
                    ev.ModelRequestFinished(
                        session_id="s1",
                        turn=1,
                        usage=ev.Usage(input_tokens=10, output_tokens=5),
                        duration_ms=50,
                    )
                )
                subscriber.apply(
                    ev.CheckpointCreated(session_id="s1", turn=1, files=["code.py"], path="code.py", before_hash=None, after_hash=h1)
                )

                subscriber.apply(ev.UserPromptSubmit(session_id="s1", turn=2, text="改代码v2", text_chars=5))
                subscriber.apply(
                    ev.CheckpointCreated(session_id="s1", turn=2, files=["code.py"], path="code.py", before_hash=h1, after_hash=h2)
                )

                from logox.app import Runtime
                from logox.tui.metrics import MetricsReducer

                bus = EventBus(session_id="s1")
                reducer = MetricsReducer(context_window=200000)

                class FakeKernel:
                    def __init__(self) -> None:
                        self.history = [
                            Message(role="user", blocks=[TextBlock(text="写代码v1")]),
                            Message(role="assistant", blocks=[TextBlock(text="已写v1")]),
                            Message(role="user", blocks=[TextBlock(text="改代码v2")]),
                            Message(role="assistant", blocks=[TextBlock(text="已改v2")]),
                        ]

                runtime = Runtime(
                    bus=bus,
                    kernel=FakeKernel(),
                    reducer=reducer,
                    theme=None,
                    config=None,
                    provider_name="test",
                    model="test-model",
                    cwd=cwd,
                    tools=[],
                    resume_file=log_file,
                    blob_store=store,
                )

                # 1. 列表
                cps = runtime.list_checkpoints()
                self.assertEqual(len(cps), 2)
                self.assertEqual(cps[0].turn, 2)  # 倒序，轮次 2 排第一
                self.assertEqual(cps[1].turn, 1)

                # 2. 检查冲突
                conflicts = runtime.check_rewind_conflicts(2)
                self.assertEqual(conflicts, [])

                # 3. 回滚至轮次 2（撤销轮次 2，恢复至 h1）
                res = await runtime.rewind(2, force=False)
                self.assertTrue(res.success)
                self.assertEqual(f.read_bytes(), b"initial v1")
                # 内核历史只剩下轮次 1 的 2 条消息
                self.assertEqual(len(runtime.kernel.history), 2)
                self.assertEqual(runtime.kernel.history[0].blocks[0].text, "写代码v1")

        asyncio.run(run_test())


if __name__ == "__main__":
    unittest.main()
