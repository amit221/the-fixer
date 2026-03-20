# Workflow progress & agent updates Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give parent processes (including a Cursor agent running the Iynx workflow) reliable, parseable lifecycle signals for each major step, success/failure, and final outcome—while keeping human-readable logs clear for the user.

**Architecture:** Introduce a small **append-only JSON Lines** progress stream (one JSON object per line) written to a configurable path under the project root by default. The orchestrator emits `started` / `completed` / `failed` events at phase boundaries (and key sub-steps like Docker clone). Standard logging stays the primary user-facing channel but gains consistent `[iynx]` phase summaries. Optional: map final outcome to **process exit codes** so shell-based agents can detect failure without parsing logs.

**Tech Stack:** Python 3.10+, stdlib (`json`, `logging`, `pathlib`, `time`, `os`); existing `src/orchestrator.py` flow.

---

## File structure

| File | Responsibility |
|------|------------------|
| `src/workflow_progress.py` (new) | Event schema, `emit(event: dict)`, default path resolution, thread-safe append (single writer per process is enough; document assumption). |
| `src/orchestrator.py` (modify) | Call `progress.emit(...)` at discovery, target resolution, preflight skip/continue, clone, phases 1–4, verify, PR, retries, timeouts, `main()` summary. |
| `tests/test_workflow_progress.py` (new) | Unit tests for JSONL format, env override path, disabled mode. |
| `README.md` (modify) | Document `IYNX_PROGRESS_JSONL`, event fields, how a supervising agent should `tail`/poll the file. |
| `.env.example` (modify) | Comment-only line for optional `IYNX_PROGRESS_JSONL`. |

**Event shape (each line is one JSON object):**

```json
{
  "ts": "2026-03-20T12:00:00.000Z",
  "run_id": "uuid-or-short-id",
  "phase": "phase3_implement",
  "status": "started",
  "repo": "owner/name",
  "issue": 230,
  "detail": null,
  "exit_code": null
}
```

**Phases (stable string constants):** `discovery`, `target_resolve`, `preflight`, `clone`, `bootstrap`, `phase1_context`, `phase2_issue_pick`, `phase3_implement`, `verify_tests`, `phase4_pr_draft`, `pr_create`, `run_complete`.

**Statuses:** `started`, `completed`, `failed`, `skipped` (e.g. no repos).

**`run_id`:** Generate once per `main()` (e.g. `uuid.uuid4().hex[:12]`) and pass into `run_one_repo` so all events for one invocation correlate.

**Env vars:**

- `IYNX_PROGRESS_JSONL` — if set to empty or `0`, disable file emission; if set to a path, append there; if unset, default to `{PROJECT_ROOT}/.iynx-run-progress.jsonl` (gitignored—add to `.gitignore`).

**Exit codes (`main()`):**

- `0` — PR created successfully (current happy path).
- `1` — configuration error (e.g. missing `CURSOR_API_KEY`) or unexpected crash.
- `2` — run finished but no PR (discovery empty, preflight skip, phase failure, PR failure, etc.). *Document as a behavioral change for scripts.*

---

### Task 1: Progress module (schema + append)

**Files:**

- Create: `src/workflow_progress.py`
- Test: `tests/test_workflow_progress.py`

- [ ] **Step 1: Write failing tests**

```python
# tests/test_workflow_progress.py
import json
import os
import tempfile
from pathlib import Path

import pytest

# After implementation, import from package path used by tests (see conftest / sys.path)
from workflow_progress import ProgressWriter, default_progress_path


def test_emit_writes_valid_jsonl():
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "p.jsonl"
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


def test_disabled_emits_nothing():
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "p.jsonl"
        w = ProgressWriter(path=p, run_id="x", enabled=False)
        w.emit(phase="discovery", status="completed", repo=None, issue=None, detail=None, exit_code=None)
        assert not p.exists()


def test_env_empty_disables(monkeypatch):
    monkeypatch.setenv("IYNX_PROGRESS_JSONL", "")
    # resolve_enabled() or factory should return disabled
    from workflow_progress import progress_writer_from_env

    with tempfile.TemporaryDirectory() as td:
        monkeypatch.chdir(td)
        w = progress_writer_from_env(run_id="r")
        assert w.enabled is False
```

Adjust imports to match how `tests/` load `src` (mirror existing tests).

- [ ] **Step 2: Run tests — expect FAIL**

Run: `pytest tests/test_workflow_progress.py -v`  
Expected: import / collection errors or failures until implementation exists.

- [ ] **Step 3: Implement `workflow_progress.py`**

```python
# src/workflow_progress.py — minimal sketch (implement fully with docstrings)
from __future__ import annotations

import json
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path

def default_progress_path(project_root: Path) -> Path:
    return project_root / ".iynx-run-progress.jsonl"

def progress_writer_from_env(*, run_id: str, project_root: Path) -> ProgressWriter:
    raw = os.environ.get("IYNX_PROGRESS_JSONL")
    if raw is not None and raw.strip() in ("", "0", "false", "False"):
        return ProgressWriter(path=default_progress_path(project_root), run_id=run_id, enabled=False)
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

    def emit(self, *, phase: str, status: str, repo: str | None, issue: int | None, detail: str | None, exit_code: int | None) -> None:
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
```

- [ ] **Step 4: Run tests — expect PASS**

Run: `pytest tests/test_workflow_progress.py -v`

- [ ] **Step 5: Commit**

```bash
git add src/workflow_progress.py tests/test_workflow_progress.py
git commit -m "feat: add workflow JSONL progress writer"
```

---

### Task 2: Wire orchestrator + user-facing log lines

**Files:**

- Modify: `src/orchestrator.py` (throughout `run_one_repo`, `main`, `discover_repos_for_run` call sites as needed)
- Modify: `.gitignore` — add `.iynx-run-progress.jsonl`

- [ ] **Step 1: Instantiate writer in `main()`**

- Generate `run_id` once.
- `pw = progress_writer_from_env(run_id=run_id, project_root=PROJECT_ROOT)`.
- Pass `pw` into `run_one_repo(..., progress=pw)` (add optional param defaulting to `None` for tests).

- [ ] **Step 2: Emit events at boundaries**

At minimum:

| Location | phase | status | notes |
|----------|-------|--------|--------|
| After discovery list built | `discovery` | `completed` | `detail` = count of repos |
| No repos | `discovery` | `skipped` | |
| Explicit target | `target_resolve` | `completed` | `repo` set |
| Preflight fail (no issues / bad override) | `preflight` | `failed` | `detail` short reason |
| Before clone | `clone` | `started` | |
| After clone | `clone` | `completed` | |
| After `write_bootstrap` | `bootstrap` | `completed` | |
| Before/after each `run_cursor_phase` | `phaseN_*` | `started` / `completed` or `failed` | include `exit_code` from `CompletedProcess` on failure |
| Verify tests | `verify_tests` | `completed` / `failed` | |
| PR docker run | `pr_create` | `started` / `completed` / `failed` | |
| End of `main` | `run_complete` | `completed` | `detail` = `pr_created` or `no_pr`; `exit_code` hint for shell |

- [ ] **Step 3: Add concise human log helper**

```python
def _log_phase(repo: str | None, phase: str, status: str, extra: str = "") -> None:
    suffix = f" {extra}" if extra else ""
    logger.info("[iynx] phase=%s status=%s repo=%s%s", phase, status, repo or "-", suffix)
```

Call `_log_phase` next to each `pw.emit` so terminal watchers see progress without parsing JSONL.

- [ ] **Step 4: Exit codes in `main()`**

- Track boolean `pr_created`.
- `sys.exit(2)` when finished without PR (and not already exited with `1`).
- Emit final JSONL with `phase=run_complete`, `status=completed`, `detail` explaining outcome.

- [ ] **Step 5: Integration smoke**

Run: `pytest tests/ -v`  
Expected: all pass.

- [ ] **Step 6: Commit**

```bash
git add src/orchestrator.py .gitignore
git commit -m "feat: emit workflow progress events and non-zero exit when no PR"
```

---

### Task 3: Documentation

**Files:**

- Modify: `README.md` (new subsection under Usage or Flow)
- Modify: `.env.example`

- [ ] **Step 1: README — document for supervising agents**

- Path to default JSONL file.
- `IYNX_PROGRESS_JSONL` semantics.
- Example: PowerShell `Get-Content .iynx-run-progress.jsonl -Wait` (or `tail -f` on Unix).
- Table of `phase` values and `status` values.
- Exit code table (`0` / `1` / `2`).

- [ ] **Step 2: `.env.example`**

Add commented line:

```bash
# Optional: JSONL progress log for agents (empty or 0 to disable)
# IYNX_PROGRESS_JSONL=.iynx-run-progress.jsonl
```

- [ ] **Step 3: Commit**

```bash
git add README.md .env.example
git commit -m "docs: workflow progress file and exit codes"
```

---

## Plan review loop

After implementation, optionally run your **plan-document-reviewer** prompt against this file; fix any gaps (e.g. missing `run_complete` on early `sys.exit(1)`).

---

## Execution handoff

**Plan complete and saved to `docs/superpowers/plans/2026-03-20-workflow-progress-and-agent-updates.md`. Two execution options:**

**1. Subagent-Driven (recommended)** — Dispatch a fresh subagent per task, review between tasks, fast iteration.

**2. Inline Execution** — Execute tasks in this session using executing-plans, batch execution with checkpoints.

**Which approach?**

- If **Subagent-Driven:** REQUIRED SUB-SKILL: `superpowers:subagent-driven-development` — fresh subagent per task + two-stage review.
- If **Inline:** REQUIRED SUB-SKILL: `superpowers:executing-plans` — batch execution with checkpoints for review.
