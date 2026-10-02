from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from logox.anamnesis.archives import ArchiveStore
from logox.anamnesis.coordinator import AnamesisCoordinator
from logox.anamnesis.io import FileLock
from logox.anamnesis.models import MemoryChange
from logox.anamnesis.sources import SourceCollector
from logox.config.schema import AnamesisConfig
from logox.store.manager import SessionManager


class Foundations(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.home, self.project = self.root / "home", self.root / "project"
        self.home.mkdir()
        self.project.mkdir()
        self.store = ArchiveStore(self.home, self.project)

    def change(self, **overrides):
        return MemoryChange.model_validate(
            {
                "entry_id": "direction",
                "scope": "project",
                "action": "add",
                "new_value": "只读研究",
                "source_ids": ["user1"],
                "rationale": "用户明确要求",
                "analysis_record_id": "a1",
                "status": "explicit",
                **overrides,
            }
        )

    def test_commit_then_replace_and_revert_with_evidence(self):
        first = self.store.commit(
            self.store.load("project"), [self.change()], verify=lambda _: True, awake=lambda: False
        )
        current = self.store.load("project")
        self.assertTrue(current.managed)
        self.assertEqual(current.entries[0].value, "只读研究")
        second = self.store.commit(
            current,
            [self.change(action="replace", old_value="只读研究", new_value="只读并给计划")],
            verify=lambda _: True,
            awake=lambda: False,
        )
        self.store.revert(second)
        self.assertEqual(self.store.load("project").entries[0].value, "只读研究")
        self.assertNotEqual(first, second)
        self.assertFalse((self.project / "LOGOX.md").exists())

    def test_wake_or_invalid_source_never_starts_commit(self):
        snapshot = self.store.load("project")
        with self.assertRaises(ValueError):
            self.store.commit(snapshot, [self.change()], verify=lambda _: False, awake=lambda: False)
        with self.assertRaises(InterruptedError):
            self.store.commit(snapshot, [self.change()], verify=lambda _: True, awake=lambda: True)
        self.assertFalse((self.project / "ANAMNESIS.md").exists())

    def test_manual_file_and_changed_base_are_not_overwritten(self):
        snapshot = self.store.load("project")
        target = self.project / "ANAMNESIS.md"
        target.write_text("人工补充", encoding="utf-8")
        with self.assertRaises(ValueError):
            self.store.commit(snapshot, [self.change()], verify=lambda _: True, awake=lambda: False)
        self.assertEqual(target.read_text(encoding="utf-8"), "人工补充")
        self.assertFalse(self.store.load("project").managed)

    def test_recover_replaced_file_when_metadata_write_failed(self):
        from logox.anamnesis.io import write_json

        def fail_metadata(path, data):
            if path.parent.name == "archives":
                raise OSError("disk full")
            write_json(path, data)

        with (
            patch("logox.anamnesis.archives.write_json", side_effect=fail_metadata),
            self.assertRaises(OSError),
        ):
            self.store.commit(
                self.store.load("project"), [self.change()], verify=lambda _: True, awake=lambda: False
            )
        self.assertTrue((self.project / "ANAMNESIS.md").exists())
        recovered = self.store.recover_pending()
        self.assertIn("committed", recovered[0])
        self.assertTrue(self.store.load("project").managed)

    def test_budget_and_duplicate_entries_reject_without_partial_file(self):
        tiny = ArchiveStore(self.home, self.project, project_tokens=2)
        with self.assertRaises(ValueError):
            tiny.commit(tiny.load("project"), [self.change()], verify=lambda _: True, awake=lambda: False)
        with self.assertRaises(ValueError):
            self.store.commit(
                self.store.load("project"),
                [self.change(), self.change()],
                verify=lambda _: True,
                awake=lambda: False,
            )
        self.assertFalse((self.project / "ANAMNESIS.md").exists())

    def test_recent_submission_wins_and_busy_window_is_skipped(self):
        root = self.home / "anamnesis"
        a = AnamesisCoordinator(root, self.project, window_id="a")
        b = AnamesisCoordinator(root, self.root, window_id="b")
        a.record_submission()
        a.update(eligible=True, busy=False)
        b.record_submission()
        b.update(eligible=True, busy=False)
        self.assertIsNone(a.claim())
        self.assertIsNotNone(b.claim())
        self.assertIsNone(a.claim(manual=True))
        b.finish()
        b.update(eligible=False, busy=True)
        self.assertIsNotNone(a.claim())
        a.unregister()
        b.unregister()

    def test_lock_mutual_exclusion_with_real_child_and_exit_release(self):
        path = self.home / "running.lock"
        code = "from pathlib import Path; from logox.anamnesis.io import FileLock; import sys; x=FileLock(Path(sys.argv[1])); print(x.acquire(), flush=True)"
        lock = FileLock(path)
        self.assertTrue(lock.acquire())
        result = subprocess.run(
            [sys.executable, "-c", code, str(path)], capture_output=True, text=True, timeout=15
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "False")
        lock.close()
        result = subprocess.run(
            [sys.executable, "-c", code, str(path)], capture_output=True, text=True, timeout=15
        )
        self.assertEqual(result.stdout.strip(), "True")
        self.assertTrue(lock.acquire())
        lock.close()

    def test_sources_project_identity_rewind_and_partial_tail(self):
        sessions = self.home / "sessions"
        directory = SessionManager(sessions).get_project_dir(self.project)
        directory.mkdir(parents=True)
        path = directory / "chat.jsonl"
        lines = [
            {"role": "user", "type": "user_prompt", "turn": 1, "content": "偏好甲"},
            {"role": "user", "type": "user_prompt", "turn": 2, "content": "偏好乙"},
        ]
        path.write_text("".join(json.dumps(x, ensure_ascii=False) + "\n" for x in lines), encoding="utf-8")
        collector = SourceCollector(sessions, self.project)
        refs = collector.collect()
        self.assertEqual(len(refs), 2)
        self.assertEqual(refs[1].line, 2)
        self.assertEqual(refs[1].project_id, collector.project_id)
        with path.open("a", encoding="utf-8") as handle:
            handle.write('{"type":"session_rewind","to_turn":2}\n{"role":')
        self.assertFalse(collector.verify(refs[1].source_id))
        self.assertTrue(collector.verify(refs[0].source_id))
        remaining = collector.collect()
        self.assertEqual([r.kind for r in remaining], ["user_message", "history_revision"])
        self.assertTrue(collector.issues)

    def test_long_user_message_split_and_generated_code_excluded(self):
        directory = SessionManager(self.home / "sessions").get_project_dir(self.project)
        directory.mkdir(parents=True)
        (directory / "chat.jsonl").write_text(
            json.dumps({"role": "user", "content": "x" * 5000}) + "\n", encoding="utf-8"
        )
        collector = SourceCollector(self.home / "sessions", self.project)
        refs = collector.collect()
        self.assertEqual("".join(r.content for r in refs), "x" * 5000)
        (self.project / "ANAMNESIS.md").write_text("generated", encoding="utf-8")
        (self.project / "code.py").write_text("pass", encoding="utf-8")
        legacy = self.project / "docs" / "modules" / "LEGACY"
        legacy.mkdir(parents=True)
        (legacy / "old.md").write_text("obsolete", encoding="utf-8")
        self.assertEqual(collector.code_files(), [self.project / "code.py"])

    def test_configuration_validates_times_and_has_no_duration_cap(self):
        cfg = AnamesisConfig()
        self.assertEqual(cfg.idle_seconds, 1800)
        self.assertFalse(any("duration" in f for f in type(cfg).model_fields))
        for overrides in ({"sleep_start": "24:00"}, {"sleep_end": "00:00"}, {"idle_seconds": 0}):
            with self.assertRaises(ValueError):
                AnamesisConfig(**overrides)
