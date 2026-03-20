# Issue Fix Workflow

Reusable workflow for fixing open-source issues: triage, implement, test, and submit.

## Repository

When the user names a repo, treat it as the work target before picking an issue or cloning anything else.

**Accepted forms**

- `owner/repo` (e.g. `python/cpython`)
- HTTPS or SSH clone URL
- Path to an existing local clone

**What to do**

1. Resolve to a local directory: use the given path if it exists and is a git repo; otherwise clone into a sensible location (e.g. workspace subfolder or the user’s stated directory).
2. `cd` there for all subsequent commands (search, edit, test, commit).
3. If the user also gives an issue or PR link, confirm it belongs to the same `owner/repo` (or explain the mismatch).

If no repo is given, assume the current workspace root or ask once for `owner/repo` / URL / path.

## Run status (supervising agents)

Long Iynx runs (`python run.py`) stream work through Docker and Cursor. A supervising agent or host process should **not** assume instant completion.

**Cadence:** Use a **scheduler (or timer) that checks run status every 60 seconds** until the run finishes or the user stops it. Sixty seconds is the default interval unless the user specifies another.

**What to check**

- **Progress file:** `.iynx-run-progress.jsonl` at the Iynx project root (or the path in `IYNX_PROGRESS_JSONL` if set). Each line is one JSON event: `phase`, `status`, `repo`, `issue`, `detail`, `exit_code`.
- **Done:** The run is finished when you see `phase` `run_complete` (read `detail` for `pr_created` vs `no_pr`, and `exit_code`).
- **Between polls:** You can still read the latest line or tail new lines since the last check; avoid tight loops.

**Implementation note:** The host may later ship a built-in scheduler that performs this 60s poll; until then, follow the same cadence when implementing supervision yourself.

## Quick Checklist

- [ ] Pick a suitable issue
- [ ] Reproduce and locate root cause
- [ ] Implement fix (preserve public APIs)
- [ ] Add regression tests
- [ ] Run format, lint, tests (per repo conventions)
- [ ] Prepare PR with conventional title and issue linkage

## 1. Issue Selection

When the orchestrator asks you to choose (issue-selection phase), pick **one** open, non-PR issue you can complete in a single focused change, or decline with `issue: null` if none fit.

Prefer issues that are:

- Scoped to one package or module
- Reproducible with unit tests or clear steps

## 2. Root Cause and Implementation

- Search the codebase for the relevant code paths
- Minimize changes; avoid touching public APIs unless required
- Use keyword-only args for new parameters: `*, new_param: str = "default"`
- Follow existing patterns in the file you modify

## 3. Testing

- Add tests mirroring the repo structure (e.g. `tests/unit_tests/`, `tests/`, `__tests__/`)
- Avoid mocks when possible; test real behavior
- Check the repo's test config (e.g. `pyproject.toml`, `jest.config.js`) for conventions

## 4. Quality Gates

Before submitting, run the repo's standard checks. Common patterns:

**Python (ruff, pytest):**
```bash
uv run ruff format .
uv run ruff check . --fix
uv run pytest tests/ -v
```

**Node/JS:**
```bash
npm run lint
npm test
```

**Make-based:**
```bash
make format
make lint
make test
```

Check `CONTRIBUTING.md`, `README.md`, or CI config for the actual commands.

## 5. PR Requirements

- **Title**: Follow the repo's convention (often `type(scope): description`, e.g. `fix(cli): resolve bug`)
- **Body**: Include `Fixes #ISSUE_NUMBER` to auto-close the issue
- **Disclaimer**: Mention AI involvement if the repo asks for it
- **Scope**: Use scopes the repo's PR lint allows (check `.github/workflows/` or contributing docs)

## Adapting to a Repo

Each repo has its own structure. Before starting:

1. Read `CONTRIBUTING.md` or the contributing guide linked from the README
2. Check `.github/PULL_REQUEST_TEMPLATE.md` for PR expectations
3. Inspect `.github/workflows/` for lint/test commands
4. Run a quick `make help` or `npm run` to see available scripts
