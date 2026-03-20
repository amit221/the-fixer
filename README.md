# Iynx — GitHub Contribution Agent

<p align="center">
  <img src="lynx_logo.png" alt="Cybernetic lynx head logo in blue and teal, with circuit patterns and a code symbol in one eye." width="200" />
</p>

<p align="center">
  <a href=".github/workflows/ci.yml"><img src="https://img.shields.io/badge/CI-GitHub_Actions-2088FF?logo=githubactions&logoColor=white" alt="CI: GitHub Actions (see workflow file)" /></a>
  <img src="https://img.shields.io/badge/python-3.10%20%7C%203.11%20%7C%203.12-3776AB?logo=python&logoColor=white" alt="Python 3.10–3.12" />
  <img src="https://img.shields.io/badge/tests-pytest-0A9EDC?logo=pytest&logoColor=white" alt="pytest" />
  <img src="https://img.shields.io/badge/coverage-min%2078%25-31C653" alt="Coverage: minimum 78% (pytest-cov, pyproject.toml)" />
  <img src="https://img.shields.io/badge/branch_coverage-enabled-2ea043" alt="Branch coverage enabled (coverage.py)" />
  <img src="https://img.shields.io/badge/lint%20%2F%20format-ruff-261220?logo=ruff&logoColor=white" alt="Ruff lint and format" />
  <img src="https://img.shields.io/badge/Docker-required-2496ED?logo=docker&logoColor=white" alt="Docker required for agent runs" />
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

- Docker (the agent image runs as user `dev`, UID **1000**, so test stacks that refuse root—e.g. embedded Postgres—work after `docker build`; rebuild if you still use an older root-based image)
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
# Inside the container, scripts also emit `[iynx-docker] …` timestamp lines at each phase
# (clone, bootstrap, cursor-agent, PR) so you can follow progress; disable with IYNX_DOCKER_TRACE=0.
# Cursor phases use `--output-format stream-json` and `--stream-partial-output` by default
# so agent/tool progress appears as NDJSON lines (see env table to switch to `text` if logs are too noisy).

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
| `IYNX_DOCKER_TTY` | No | If `1` (default), `docker run -t` for streamed steps so Cursor CLI output is line-buffered to the host; set `0` if `-t` fails (e.g. some CI) |
| `IYNX_DOCKER_TRACE` | No | If `1` (default), every Docker shell step prints `[iynx-docker]` timestamp lines (clone, bootstrap, `cursor-agent`, verify, PR) so `docker logs` / host `[docker]` lines show clear phases; set `0` to silence |
| `IYNX_DOCKER_XTRACE` | No | If `1`, runs those shells with `set -x` (very noisy; for deep debugging only) |
| `IYNX_CURSOR_OUTPUT_FORMAT` | No | Cursor `--print` format: `stream-json` (default, NDJSON as the agent runs), `json`, or `text` (final answer only; quiet logs) |
| `IYNX_CURSOR_STREAM_PARTIAL` | No | With `stream-json`, if `1` (default) passes `--stream-partial-output` for smaller text deltas; set `0` to disable |
| `IYNX_DOCKER_RUN_TIMEOUT` | No | Max seconds per `docker run` (clone, each Cursor phase, etc.); default `3600`. Raise for huge `pnpm install` / long agent runs; lower to fail fast |
| `IYNX_CURSOR_PERMISSIVE` | No | If `1` (default), passes `--yolo`, `--approve-mcps`, and `--sandbox disabled` to `cursor-agent` so headless runs do not stall on approvals (see [Cursor CLI parameters](https://cursor.com/docs/cli/reference/parameters)). Set `0` for stricter behavior; phases that used `--force` still append it when permissive is off |
| `IYNX_CURSOR_MODEL` | No | Overrides the default Cursor model (`composer-2` in code) |
| `IYNX_CURSOR_EXTRA_ARGS` | No | Extra `cursor-agent` flags (space-separated, POSIX-quoted), appended after built-in flags |

Discovery rules (stars, age, pool size, CONTRIBUTING requirement, optional post-fix test re-run) live as **constants** in `src/orchestrator.py` — edit there to tune behavior; the Cursor model default can be overridden with `IYNX_CURSOR_MODEL`.

*Without `GITHUB_TOKEN`, discovery is rate-limited (60 req/hr) and PR creation will fail.

## Project Structure

```
iynx/
├── Dockerfile           # Cursor CLI + gh + git + jq + Node.js 22; `USER dev` (UID 1000)
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
2. **Pick one repo**: One repo is chosen **uniformly at random** from the filtered list (so repeated runs are not stuck on the same top search hit).
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
