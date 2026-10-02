"""Keep offline tests independent of real ancestor projects and API credentials."""

import os
import tempfile
from pathlib import Path

import pytest

import logox.paths as paths

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(autouse=True)
def isolate_test_environment(monkeypatch):
    original = paths._ancestors

    def ancestors(cwd):
        found = original(cwd)
        if Path(cwd).resolve().is_relative_to(ROOT):
            return [path for path in found if path.is_relative_to(ROOT)]
        return found

    monkeypatch.setattr(paths, "_ancestors", ancestors)
    temporary = ROOT / ".test-tmp"
    temporary.mkdir(exist_ok=True)
    monkeypatch.setattr(tempfile, "tempdir", str(temporary))
    for key in ("TEMP", "TMP"):
        monkeypatch.setenv(key, str(temporary))
    for key in tuple(os.environ):
        if key.endswith("_API_KEY"):
            monkeypatch.delenv(key, raising=False)
