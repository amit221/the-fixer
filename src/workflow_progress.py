"""
Append-only JSON Lines progress stream for supervising processes (e.g. Cursor agents).

Single-process writer assumption: no cross-process locking. Each orchestrator run uses one ProgressWriter.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path


def default_progress_path(project_root: Path) -> Path:
    return project_root / ".iynx-run-progress.jsonl"


def progress_writer_from_env(*, run_id: str, project_root: Path) -> ProgressWriter:
    raw = os.environ.get("IYNX_PROGRESS_JSONL")
    if raw is not None and raw.strip() in ("", "0", "false", "False"):
        return ProgressWriter(
            path=default_progress_path(project_root),
            run_id=run_id,
            enabled=False,
        )
    if raw is None or not raw.strip():
        path = default_progress_path(project_root)
    else:
        path = Path(raw.strip()).expanduser()
    return ProgressWriter(path=path, run_id=run_id, enabled=True)


class ProgressWriter:
    def __init__(self, path: Path, run_id: str, enabled: bool) -> None:
        self.path = path
        self.run_id = run_id
        self.enabled = enabled

    def emit(
        self,
        *,
        phase: str,
        status: str,
        repo: str | None,
        issue: int | None,
        detail: str | None,
        exit_code: int | None,
    ) -> None:
        if not self.enabled:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        rec = {
            "ts": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "run_id": self.run_id,
            "phase": phase,
            "status": status,
            "repo": repo,
            "issue": issue,
            "detail": detail,
            "exit_code": exit_code,
        }
        with self.path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
            f.flush()
