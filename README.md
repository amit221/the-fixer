# Iynx — GitHub Contribution Agent

<p align="center">
  <img src="lynx_logo.png" alt="Cybernetic lynx head logo in blue and teal, with circuit patterns and a code symbol in one eye." width="200" />
</p>

An autonomous agent that discovers trendy GitHub repos, learns contribution guidelines, fixes issues, tests in Docker, and opens PRs. Uses **Cursor CLI** as the primary AI engine. All repo execution (clone, npm test, etc.) runs **inside Docker** for safety.

## Architecture

- **Host**: Runs discovery (GitHub API), Docker commands, and writes bootstrap/config. Never executes repo code.
- **Docker**: Cursor CLI, gh, git. Clone, fix, test, and PR creation happen inside the container.

## Cursor IDE: Superpowers (optional)

[Superpowers](https://github.com/obra/superpowers) adds shared agent skills (TDD, planning, debugging workflows). In Cursor Agent chat you can run `/add-plugin superpowers` or install **Superpowers** from the marketplace.

For a **local install** (same layout Cursor expects under `~/.cursor/plugins/local`), clone the repo and reload the editor:

```powershell
# Windows (PowerShell)
New-Item -ItemType Directory -Force -Path "$env:USERPROFILE\.cursor\plugins\local" | Out-Null
git clone --depth 1 https://github.com/obra/superpowers.git "$env:USERPROFILE\.cursor\plugins\local\superpowers"
```

```bash
# macOS / Linux
mkdir -p ~/.cursor/plugins/local
git clone --depth 1 https://github.com/obra/superpowers.git ~/.cursor/plugins/local/superpowers
```

Then **Developer: Reload Window** so rules/skills load. Update with `git pull` inside that clone.

## Prerequisites

- Docker
- Python 3.10+
- [Cursor CLI](https://cursor.com/docs/cli/overview) (installed in the image)
- `CURSOR_API_KEY` (from [Cursor settings](https://cursor.com/settings))
- `GITHUB_TOKEN` with `repo` scope (for discovery and PR creation)

## Setup

```bash
# Clone and enter project
cd iynx

# Create venv and install deps
python -m venv .venv
.venv\Scripts\activate   # Windows
# source .venv/bin/activate  # Linux/macOS

pip install -r requirements.txt

# Run unit tests (no Docker)
pytest tests/ -v

# With coverage (threshold in pyproject.toml)
pytest tests/ -v --cov=src --cov-report=term-missing

# Lint / format (Ruff)
ruff check src tests run.py
ruff format src tests run.py

# Copy env template and fill in secrets
copy .env.example .env
# Edit .env with CURSOR_API_KEY and GITHUB_TOKEN

# Build Docker image
docker build -t iynx-agent:latest .
```

## Usage

```bash
# Load env
set -a && source .env && set +a   # Linux/macOS
# Or on Windows: $env:CURSOR_API_KEY="..."; $env:GITHUB_TOKEN="..."

# Run the agent (discovers repos, fixes one issue, opens PR)
python run.py

# While Docker runs (clone, Cursor, tests, `gh`), lines from the container are streamed
# to the console as `INFO` log lines prefixed with `[docker]`.

# Target one repo (and optional issue number). Without argv[2], the agent lists open issues and picks one it can handle (Phase 2).
python run.py obra/superpowers
python run.py obra/superpowers 849
# Or: IYNX_TARGET_REPO=obra/superpowers IYNX_TARGET_ISSUE=849 python run.py
```

### Progress for supervising agents

While `python run.py` runs, the orchestrator writes **structured lifecycle events** so another process (or a Cursor agent watching the repo) can tell what finished or failed without parsing Docker prose.

- **Default file:** `.iynx-run-progress.jsonl` at the project root (gitignored). Each line is one JSON object (`ts`, `run_id`, `phase`, `status`, `repo`, `issue`, `detail`, `exit_code`).
- **Human logs:** Lines like `[iynx] phase=phase3_implement status=started repo=owner/name issue=42` mirror the same steps in the console.
- **Override path:** set `IYNX_PROGRESS_JSONL` to a file path. Set it to empty, `0`, or `false` to disable file output (logs still include `[iynx]` lines).

**Follow the log (PowerShell):** `Get-Content .iynx-run-progress.jsonl -Wait`  
**Follow the log (Unix):** `tail -f .iynx-run-progress.jsonl`

| `phase` | Meaning |
|---------|---------|
| `discovery` | Repo list after filters (or skipped if none) |
| `target_resolve` | Explicit `owner/repo` from argv/env |
| `preflight` | Open-issue checks before clone |
| `clone` / `bootstrap` | Docker clone and host bootstrap |
| `phase1_context` … `phase4_pr_draft` | Cursor phases |
| `verify_tests` | Optional post-fix test re-run (`skipped` if disabled in orchestrator) |
| `pr_create` | `gh` fork / push / `pr create` |
| `workflow` | Host/Docker timeout or unexpected error |
| `run_complete` | Final row: `detail` is `pr_created` or `no_pr`; `exit_code` matches process |

| `status` | Meaning |
|---------|---------|
| `started` | Step began |
| `completed` | Step succeeded |
| `failed` | Step failed |
| `skipped` | Step not applicable (e.g. no repos, verify disabled) |

**Process exit codes:** `0` = PR created; `1` = fatal config (e.g. missing `CURSOR_API_KEY`); `2` = run finished without a PR (discovery empty, preflight/phase/PR failure, etc.).

## Environment Variables

| Variable | Required | Description |
|----------|----------|-------------|
| `CURSOR_API_KEY` | Yes | Cursor CLI API key |
| `GITHUB_TOKEN` | Yes* | GitHub token (repo scope) for discovery and PRs |
| `IYNX_PROGRESS_JSONL` | No | Path to JSONL progress file; empty/`0`/`false` disables the file |

Discovery rules (stars, age, pool size, CONTRIBUTING requirement, Cursor model, optional post-fix test re-run) live as **constants** in `src/orchestrator.py` — edit there to tune behavior.

*Without `GITHUB_TOKEN`, discovery is rate-limited (60 req/hr) and PR creation will fail.

## Project Structure

```
iynx/
├── Dockerfile           # Cursor CLI + gh + git + jq
├── docker-compose.yml   # Optional local dev
├── src/
│   ├── orchestrator.py  # Main loop
│   ├── discovery.py     # GitHub Search API
│   ├── github_repo_checks.py  # CONTRIBUTING + author PR checks
│   ├── bootstrap.py    # Generate .cursor-agent per repo
│   ├── workflow_progress.py  # JSONL progress for agents
│   └── pr.py           # Fork + push + gh pr create
├── skills/
│   └── issue-fix-workflow.md
├── tests/               # pytest (discovery + GitHub checks)
├── workspace/           # Mount point (gitignored)
├── .env.example
└── README.md
```

## Flow

1. **Discovery**: Search GitHub (defaults in `orchestrator.py`: e.g. stars, repo age, pool size), then keep repos that have a CONTRIBUTING file and none of your prior PRs to that repo.
2. **Pick one repo**: The **first** repo in that filtered list is the only one processed this run.
3. **Issue preflight** (host, GitHub API): Require at least one **open** issue (PRs excluded). If none, skip **without cloning**. This only checks that the list is non-empty.
4. **Clone**: `git clone` inside Docker into `workspace/owner-repo/`.
5. **Bootstrap**: Generate `iynx.cursor-agent` from repo structure (Node/Python/Rust).
6. **Phase 1**: Cursor writes `.iynx/summary.md` and `.iynx/context.json` (`test_command`, `lint_command`) from the contribution guide.
7. **Phase 2 (issue selection)**: Cursor lists open issues (`gh issue list`), picks **one** it can handle (or declines), and writes `.iynx/chosen-issue.json`. The host validates the pick against the API. **Override:** pass issue number as argv[2] or `IYNX_TARGET_ISSUE` to skip this phase.
8. **Phase 3 (implement)**: Cursor implements the fix for the selected issue, runs tests, does not commit `.iynx/`.
9. **Verify** (optional): If `VERIFY_TESTS_AFTER_FIX` is enabled in `orchestrator.py`, Docker re-runs `test_command` from `context.json`.
10. **Phase 4 (PR draft)**: Cursor writes `.iynx/pr-draft.json` (`title`, `body`).
11. **PR**: Host writes `.iynx/pr-body.md`; `gh repo fork`, push, `gh pr create --body-file …`.

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md) for setup, tests, and PR guidelines.

## Security

Report security issues as described in [SECURITY.md](SECURITY.md).

## Safety

- Never run `npm install`, `pytest`, or any repo scripts on the host.
- All clone, fix, and test execution happens inside the Docker container.
- The host only performs HTTP (discovery), Docker commands, and file writes.
