"""Unit tests for workflow JSONL progress."""

import json
from pathlib import Path

import pytest

from workflow_progress import ProgressWriter, default_progress_path, progress_writer_from_env


def test_emit_writes_valid_jsonl(tmp_path: Path) -> None:
    p = tmp_path / "p.jsonl"
    w = ProgressWriter(path=p, run_id="abc", enabled=True)
    w.emit(
        phase="phase1_context",
        status="started",
        repo="o/r",
        issue=None,
        detail=None,
        exit_code=None,
    )
    lines = p.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 1
    obj = json.loads(lines[0])
    assert obj["phase"] == "phase1_context"
    assert obj["status"] == "started"
    assert obj["run_id"] == "abc"
    assert obj["repo"] == "o/r"
    assert "ts" in obj


def test_disabled_emits_nothing(tmp_path: Path) -> None:
    p = tmp_path / "p.jsonl"
    w = ProgressWriter(path=p, run_id="x", enabled=False)
    w.emit(
        phase="discovery",
        status="completed",
        repo=None,
        issue=None,
        detail=None,
        exit_code=None,
    )
    assert not p.exists()


def test_env_empty_disables(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("IYNX_PROGRESS_JSONL", "")
    w = progress_writer_from_env(run_id="r", project_root=tmp_path)
    assert w.enabled is False


def test_env_custom_path(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    custom = tmp_path / "custom.jsonl"
    monkeypatch.setenv("IYNX_PROGRESS_JSONL", str(custom))
    w = progress_writer_from_env(run_id="z", project_root=tmp_path)
    assert w.enabled is True
    assert w.path == custom


def test_default_progress_path(tmp_path: Path) -> None:
    assert default_progress_path(tmp_path) == tmp_path / ".iynx-run-progress.jsonl"
