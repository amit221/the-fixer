"""
Orchestrator: discover repos, clone in Docker, run Cursor CLI, create PRs.

SAFETY: All repo execution (clone, npm test, etc.) runs inside Docker.
The host only runs: discovery (HTTP), docker commands, and writing bootstrap/config.
"""

from __future__ import annotations

import json
import logging
import os
import random
import shlex
import shutil
import stat
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path

# Ensure src is on path when run as script
sys.path.insert(0, str(Path(__file__).resolve().parent))

from bootstrap import write_bootstrap
from discovery import RepoInfo, fetch_repo_by_full_name, fetch_repo_candidates
from github_repo_checks import (
    find_first_suitable_open_issue,
    get_token_login,
    repo_has_contributing_guide,
    user_has_pr_to_repo,
    validate_open_non_pr_issue,
)
from workflow_progress import ProgressWriter, progress_writer_from_env

# Project root (parent of src/)
PROJECT_ROOT = Path(__file__).resolve().parent.parent
WORKSPACE = PROJECT_ROOT / "workspace"
SKILLS_DIR = PROJECT_ROOT / "skills"
DOCKER_IMAGE = "iynx-agent:latest"
# Monorepo installs + agent work often exceed 10 minutes; override with IYNX_DOCKER_RUN_TIMEOUT (seconds).
DOCKER_RUN_TIMEOUT = 3600.0

# Discovery defaults (change here; no env vars).
DISCOVERY_POOL_SIZE = 100
DISCOVERY_MIN_STARS = 50
DISCOVERY_MAX_REPO_AGE_DAYS = 30  # None = no created:> filter
DISCOVERY_MAX_PAGES = 5
DISCOVERY_PER_PAGE = 30
DISCOVERY_LANGUAGE: str | None = None
REQUIRE_CONTRIBUTING_GUIDE = True
SKIP_REPOS_WITH_USER_PRS = True

CURSOR_AGENT_MODEL = "composer-2"


def _docker_run_timeout_seconds() -> float:
    """Host-side cap for each `docker run` (clone, cursor phases, etc.)."""
    raw = (os.environ.get("IYNX_DOCKER_RUN_TIMEOUT") or "").strip()
    if raw:
        try:
            return float(raw)
        except ValueError:
            logger.warning("Invalid IYNX_DOCKER_RUN_TIMEOUT %r; using %.0fs", raw, DOCKER_RUN_TIMEOUT)
    return DOCKER_RUN_TIMEOUT


def _cursor_agent_model() -> str:
    """CLI `--model`; env IYNX_CURSOR_MODEL overrides module default."""
    m = (os.environ.get("IYNX_CURSOR_MODEL") or "").strip()
    return m if m else CURSOR_AGENT_MODEL


def _cursor_permissive_cli_flags() -> list[str]:
    """
    Headless Docker runs should not block on per-command approval.

    Uses Cursor Agent flags from CLI docs: --yolo (--force alias), --approve-mcps,
    --sandbox disabled. Opt out with IYNX_CURSOR_PERMISSIVE=0.
    """
    raw = (os.environ.get("IYNX_CURSOR_PERMISSIVE") or "1").strip().lower()
    if raw in ("0", "false", "no", "off"):
        return []
    return ["--yolo", "--approve-mcps", "--sandbox", "disabled"]


def _cursor_extra_cli_args() -> list[str]:
    """Optional extra cursor-agent args, shell-quoted later (posix shlex)."""
    extra = (os.environ.get("IYNX_CURSOR_EXTRA_ARGS") or "").strip()
    if not extra:
        return []
    return shlex.split(extra, posix=True)

# If True, Docker re-runs test_command from .iynx/context.json after the fix.
VERIFY_TESTS_AFTER_FIX = False

# Optional: IYNX_TARGET_REPO=owner/name (or https://github.com/owner/repo) and IYNX_TARGET_ISSUE=N
logger = logging.getLogger(__name__)


def _docker_allocate_tty() -> bool:
    """
    When True, `docker run` gets `-t` so the container process has a pseudo-TTY.
    Without a TTY, many CLIs (Node, etc.) fully buffer stdout when piped, so the host
    sees no [docker] lines until the buffer fills or the process exits.
    """
    raw = os.environ.get("IYNX_DOCKER_TTY", "1").strip().lower()
    return raw not in ("0", "false", "no", "off")


def _docker_trace_enabled() -> bool:
    """Extra `[iynx-docker]` timestamp lines inside container shells (default on)."""
    raw = (os.environ.get("IYNX_DOCKER_TRACE") or "1").strip().lower()
    return raw not in ("0", "false", "no", "off")


def _docker_xtrace_enabled() -> bool:
    """When True, container bash snippets run with `set -x` (very noisy)."""
    raw = (os.environ.get("IYNX_DOCKER_XTRACE") or "0").strip().lower()
    return raw in ("1", "true", "yes", "on")


def _docker_trace_helpers() -> str:
    """
    Bash prefix for Docker-run scripts: defines _iynx_log (real or no-op).
    Lines go to stdout so the host's [docker] stream shows them.
    """
    if not _docker_trace_enabled():
        return "_iynx_log() { :; }\n"
    lines: list[str] = []
    if _docker_xtrace_enabled():
        lines.append("set -x")
    lines.extend(
        [
            r"_iynx_ts() { date -u '+%Y-%m-%dT%H:%M:%SZ'; }",
            r'_iynx_log() { printf "%s\n" "[iynx-docker] $(_iynx_ts) $*"; }',
            r'_iynx_log "shell_start pid=$$ pwd=$(pwd) user=$(id -un 2>/dev/null || echo ?)"',
        ]
    )
    return "\n".join(lines) + "\n"


def _flush_logging_handlers() -> None:
    """Push streamed docker lines to the console immediately (StreamHandler buffers)."""
    for handler in logging.root.handlers:
        try:
            handler.flush()
        except Exception:
            pass


def _cursor_print_output_flags() -> list[str]:
    """
    Flags for `cursor-agent --print` so stdout is useful while the agent runs.

    Per Cursor CLI docs: `text` only prints the final answer; `stream-json` emits
    NDJSON events as the session progresses; `--stream-partial-output` adds
    smaller text deltas (only valid with stream-json).

    Env:
      IYNX_CURSOR_OUTPUT_FORMAT — text | json | stream-json (default: stream-json)
      IYNX_CURSOR_STREAM_PARTIAL — 1/0; when 1 and format is stream-json, pass
        --stream-partial-output (default: 1)
    """
    fmt = (os.environ.get("IYNX_CURSOR_OUTPUT_FORMAT") or "stream-json").strip().lower()
    if fmt not in ("text", "json", "stream-json"):
        logger.warning("Unknown IYNX_CURSOR_OUTPUT_FORMAT %r; using stream-json", fmt)
        fmt = "stream-json"
    flags: list[str] = ["--output-format", fmt]
    if fmt == "stream-json":
        raw = (os.environ.get("IYNX_CURSOR_STREAM_PARTIAL") or "1").strip().lower()
        if raw not in ("0", "false", "no", "off"):
            flags.append("--stream-partial-output")
    return flags


def _notify_progress(
    pw: ProgressWriter | None,
    repo_full: str | None,
    phase: str,
    status: str,
    *,
    issue: int | None = None,
    detail: str | None = None,
    exit_code: int | None = None,
) -> None:
    """Human `[iynx]` log line plus optional JSONL row for supervising agents."""
    rf = repo_full or "-"
    parts = [f"phase={phase}", f"status={status}", f"repo={rf}"]
    if issue is not None:
        parts.append(f"issue={issue}")
    if detail:
        parts.append(detail)
    if exit_code is not None:
        parts.append(f"exit_code={exit_code}")
    logger.info("[iynx] %s", " ".join(parts))
    if pw is not None:
        pw.emit(
            phase=phase,
            status=status,
            repo=repo_full,
            issue=issue,
            detail=detail,
            exit_code=exit_code,
        )


def _parse_owner_repo_string(raw: str) -> tuple[str, str] | None:
    s = raw.strip().rstrip("/")
    if not s:
        return None
    if "github.com" in s:
        # https://github.com/obra/superpowers or .../superpowers.git
        path = s.split("github.com", 1)[-1].lstrip("/:")
        parts = path.split("/")
        if len(parts) >= 2:
            owner, name = parts[0], parts[1].removesuffix(".git")
            if owner and name:
                return owner, name
        return None
    if s.count("/") == 1:
        owner, name = s.split("/", 1)
        owner, name = owner.strip(), name.strip()
        if owner and name:
            return owner, name
    return None


def parse_cli_target_repo_and_issue() -> tuple[tuple[str, str] | None, int | None]:
    """
    argv[1] = owner/repo or GitHub URL; argv[2] = optional issue number override.
    """
    if len(sys.argv) < 2:
        return None, None
    pair = _parse_owner_repo_string(sys.argv[1])
    issue_override: int | None = None
    if len(sys.argv) >= 3:
        try:
            issue_override = int(sys.argv[2].strip())
        except ValueError:
            logger.warning("Invalid issue number %r; ignoring override", sys.argv[2])
    return pair, issue_override


def resolve_target_repo_from_env_or_argv(
    token: str | None,
) -> tuple[RepoInfo | None, int | None]:
    """
    Explicit target from IYNX_TARGET_REPO (+ optional IYNX_TARGET_ISSUE) or sys.argv.

    Returns (repo, issue_override) where issue_override may force a specific issue.
    """
    env_repo = os.environ.get("IYNX_TARGET_REPO", "").strip()
    env_issue_raw = os.environ.get("IYNX_TARGET_ISSUE", "").strip()
    argv_pair, argv_issue = parse_cli_target_repo_and_issue()

    owner_name: tuple[str, str] | None = None
    issue_override: int | None = None

    if argv_pair:
        owner_name = argv_pair
        issue_override = argv_issue
    elif env_repo:
        owner_name = _parse_owner_repo_string(env_repo)
        if env_issue_raw:
            try:
                issue_override = int(env_issue_raw)
            except ValueError:
                logger.warning("Invalid IYNX_TARGET_ISSUE %r; ignoring", env_issue_raw)

    if not owner_name:
        return None, None

    owner, name = owner_name
    repo = fetch_repo_by_full_name(owner, name, token=token)
    return repo, issue_override


def _read_json_file(path: Path) -> dict | None:
    try:
        if path.exists():
            data = json.loads(path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else None
    except (json.JSONDecodeError, OSError):
        return None
    return None


def load_pr_draft(iynx_dir: Path, issue_num: int) -> tuple[str, str]:
    default_title = f"fix: resolve issue #{issue_num}"
    default_body = f"Fixes #{issue_num}\n\n(AI-assisted contribution)"
    data = _read_json_file(iynx_dir / "pr-draft.json")
    if not data:
        return default_title, default_body
    title = data.get("title")
    body = data.get("body")
    if not isinstance(title, str) or not title.strip():
        title = default_title
    if not isinstance(body, str) or not body.strip():
        body = default_body
    return title.strip(), body


def load_chosen_issue(iynx_dir: Path) -> tuple[int | None, str | None]:
    """
    Read .iynx/chosen-issue.json from the issue-selection phase.

    Expected shape: {"issue": <int or null>, "reason": "<string>"}
    """
    data = _read_json_file(iynx_dir / "chosen-issue.json")
    if not data:
        return None, None
    issue = data.get("issue")
    reason = data.get("reason")
    reason_s = reason.strip() if isinstance(reason, str) and reason.strip() else None
    if issue is None:
        return None, reason_s
    if isinstance(issue, int) and issue > 0:
        return issue, reason_s
    return None, reason_s


def discover_repos_for_run(token: str | None) -> list[RepoInfo]:
    """
    Search GitHub, then apply CONTRIBUTING and 'already contributed' filters.

    Returns every candidate in the search pool that passes filters (see module constants).
    """
    pool_size = min(DISCOVERY_POOL_SIZE, 100)
    candidates = fetch_repo_candidates(
        token=token,
        pool_size=pool_size,
        min_stars=DISCOVERY_MIN_STARS,
        max_age_days=DISCOVERY_MAX_REPO_AGE_DAYS,
        language=DISCOVERY_LANGUAGE,
        max_pages=DISCOVERY_MAX_PAGES,
        per_page=min(DISCOVERY_PER_PAGE, 100),
    )
    login = get_token_login(token) if SKIP_REPOS_WITH_USER_PRS and token else None
    if SKIP_REPOS_WITH_USER_PRS and token and not login:
        logger.warning("Could not resolve GitHub login; skipping 'already contributed' filter")

    filtered: list[RepoInfo] = []
    for repo in candidates:
        if REQUIRE_CONTRIBUTING_GUIDE:
            if not repo_has_contributing_guide(repo.owner, repo.name, token):
                logger.debug("Skip %s: no CONTRIBUTING guide", repo.full_name)
                continue
        if SKIP_REPOS_WITH_USER_PRS and login:
            if user_has_pr_to_repo(login, repo.owner, repo.name, token):
                logger.debug("Skip %s: user already has PRs", repo.full_name)
                continue
        filtered.append(repo)

    return filtered


def _maybe_verify_tests(dest: Path) -> bool:
    """Optional second run of test_command from .iynx/context.json inside Docker."""
    if not VERIFY_TESTS_AFTER_FIX:
        return True
    ctx = _read_json_file(dest / ".iynx" / "context.json")
    if not ctx:
        logger.warning(
            "VERIFY_TESTS_AFTER_FIX set but no valid .iynx/context.json; skipping verify"
        )
        return True
    cmd = ctx.get("test_command")
    if not isinstance(cmd, str) or not cmd.strip():
        logger.warning("No test_command in context.json; skipping verify")
        return True
    iynx = dest / ".iynx"
    iynx.mkdir(parents=True, exist_ok=True)
    script = iynx / "verify-tests.sh"
    helpers = _docker_trace_helpers()
    script.write_text(
        "#!/usr/bin/env bash\n"
        "set -euo pipefail\n"
        f"{helpers}"
        '_iynx_log "verify_tests: cwd=$(pwd) starting"\n'
        "cd /home/dev/workspace\n"
        '_iynx_log "verify_tests: running test_command from context.json"\n'
        + cmd.strip()
        + "\n"
        '_iynx_log "verify_tests: success"\n',
        encoding="utf-8",
        newline="\n",
    )
    try:
        script.chmod(0o755)
    except OSError:
        pass
    r = _docker_run(
        ["bash", "/home/dev/workspace/.iynx/verify-tests.sh"],
        env={
            "GH_TOKEN": os.environ.get("GITHUB_TOKEN"),
            "GITHUB_TOKEN": os.environ.get("GITHUB_TOKEN"),
            "GIT_TERMINAL_PROMPT": "0",
        },
        mount=f"{dest.absolute()}:/home/dev/workspace",
        workdir="/home/dev/workspace",
    )
    if r.returncode != 0:
        logger.error("Verify tests failed: %s", r.stderr or r.stdout)
        return False
    return True


def _rmtree_retry_chmod(func, path, exc):
    """Windows: git packfiles are often read-only; chmod then retry delete."""
    if not isinstance(exc, PermissionError):
        raise exc
    try:
        os.chmod(path, stat.S_IWRITE)
        func(path)
    except OSError:
        raise exc from None


def _remove_workspace_dir(path: Path) -> None:
    """Remove a prior clone; Windows uses rmdir /s /q to avoid rare rmtree ENOTEMPTY."""
    if not path.exists():
        return
    if os.name == "nt":
        r = subprocess.run(
            ["cmd", "/c", "rmdir", "/s", "/q", str(path.resolve())],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=300,
        )
        if r.returncode != 0 and path.exists():
            shutil.rmtree(path, onexc=_rmtree_retry_chmod)
    else:
        shutil.rmtree(path, onexc=_rmtree_retry_chmod)


def _docker_run_stream(
    cmd: list[str], timeout: float | None = None
) -> subprocess.CompletedProcess:
    """Run docker with merged stdout/stderr streamed to the host logger line-by-line."""
    lines: list[str] = []

    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        bufsize=1,
    )

    def _drain() -> None:
        if proc.stdout is None:
            return
        try:
            for raw in proc.stdout:
                line = raw.rstrip("\r\n")
                lines.append(line)
                if line:
                    logger.info("[docker] %s", line)
                    _flush_logging_handlers()
        except Exception:
            pass

    if timeout is None:
        timeout = _docker_run_timeout_seconds()
    reader = threading.Thread(target=_drain, daemon=True)
    reader.start()
    try:
        proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        proc.kill()
        try:
            proc.wait(timeout=60)
        except Exception:
            pass
        reader.join(timeout=30)
        out = "\n".join(lines)
        if out:
            out += "\n"
        raise subprocess.TimeoutExpired(cmd, timeout, output=out, stderr=None) from None
    reader.join(timeout=timeout + 60)
    out = "\n".join(lines)
    if out:
        out += "\n"
    return subprocess.CompletedProcess(cmd, proc.returncode, out, "")


def _docker_run(
    args: list[str],
    env: dict | None = None,
    mount: str | None = None,
    workdir: str | None = None,
    entrypoint: str | None = None,
    stream_logs: bool = True,
) -> subprocess.CompletedProcess:
    """Run a command inside the agent Docker container.

    When stream_logs is True (default), container stdout/stderr are merged and each
    line is logged as INFO with an ``[docker]`` prefix so long Cursor phases stay visible.
    """
    cmd = ["docker", "run", "--rm"]
    if stream_logs and _docker_allocate_tty():
        cmd.append("-t")
    if entrypoint:
        cmd.extend(["--entrypoint", entrypoint])
    if mount:
        cmd.extend(["-v", mount])
    if workdir:
        cmd.extend(["-w", workdir])
    for k, v in (env or {}).items():
        if v is not None:
            cmd.extend(["-e", f"{k}={v}"])
    cmd.append(DOCKER_IMAGE)
    cmd.extend(args)
    if stream_logs:
        return _docker_run_stream(cmd)
    return subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=_docker_run_timeout_seconds(),
    )


def clone_repo(repo: RepoInfo) -> Path:
    """
    Clone repo into workspace via Docker. Host never runs git.
    """
    dest = WORKSPACE / f"{repo.owner}-{repo.name}"
    _remove_workspace_dir(dest)
    dest.mkdir(parents=True, exist_ok=True)

    helpers = _docker_trace_helpers()
    br = shlex.quote(repo.default_branch)
    url = shlex.quote(repo.clone_url)
    clone_script = (
        f"{helpers}"
        f'_iynx_log "git_clone branch={shlex.quote(repo.default_branch)} url={shlex.quote(repo.clone_url)}"\n'
        f"exec git clone --progress --depth 1 --branch {br} {url} /home/dev/workspace\n"
    )
    # Clone inside container; mount empty dir, clone into it (never run git on host)
    result = _docker_run(
        ["-c", clone_script],
        env={"GIT_TERMINAL_PROMPT": "0"},
        mount=f"{dest.absolute()}:/home/dev/workspace",
        workdir="/home/dev/workspace",
        entrypoint="bash",
    )
    if result.returncode != 0:
        raise RuntimeError(f"git clone failed: {result.stderr or result.stdout}")

    return dest


def run_cursor_phase(
    repo_path: Path,
    prompt: str,
    force: bool = False,
) -> subprocess.CompletedProcess:
    """
    Run Cursor CLI in container with workspace mounted.
    Optionally run bootstrap first.

    Uses stream-json + partial output by default so merged docker stdout shows
    live agent/tool progress (see _cursor_print_output_flags).
    """
    env = {
        "CURSOR_API_KEY": os.environ.get("CURSOR_API_KEY"),
        "GH_TOKEN": os.environ.get("GITHUB_TOKEN"),
        "GITHUB_TOKEN": os.environ.get("GITHUB_TOKEN"),
    }
    perm = _cursor_permissive_cli_flags()
    args = [
        "-p",
        *_cursor_print_output_flags(),
        "--trust",
        "--model",
        _cursor_agent_model(),
        *perm,
        *_cursor_extra_cli_args(),
    ]
    if force and not perm:
        args.append("--force")
    args.append(prompt)

    # Run bootstrap then agent (bootstrap installs deps; agent does the work)
    quoted = " ".join(shlex.quote(a) for a in args)
    helpers = _docker_trace_helpers()
    bootstrap_cmd = (
        f"{helpers}"
        f"set +e\n"
        f'_iynx_log "cursor_phase: tool_check $(command -v cursor-agent 2>/dev/null || echo missing)"\n'
        f'_iynx_log "cursor_phase: cursor-agent --version: $(cursor-agent --version 2>&1 | head -n 3 | tr "\\n" " ")"\n'
        f'_iynx_log "cursor_phase: bootstrap file check"\n'
        f"if [ -f iynx.cursor-agent ]; then\n"
        f'  _iynx_log "cursor_phase: running iynx.cursor-agent (stdout+stderr follow)"\n'
        f"  bash iynx.cursor-agent\n"
        f"  _bs=$?\n"
        f'  _iynx_log "cursor_phase: bootstrap finished exit_code=$_bs"\n'
        f"else\n"
        f'  _iynx_log "cursor_phase: no iynx.cursor-agent, skipping bootstrap"\n'
        f"fi\n"
        f'_iynx_log "cursor_phase: starting cursor-agent"\n'
        f"set +e\n"
        f"cursor-agent {quoted}\n"
        f"_ca=$?\n"
        f"set -e\n"
        f'_iynx_log "cursor_phase: cursor-agent finished exit_code=$_ca"\n'
        f"exit $_ca\n"
    )
    return _docker_run(
        ["-c", bootstrap_cmd],
        env=env,
        mount=f"{repo_path.absolute()}:/home/dev/workspace",
        workdir="/home/dev/workspace",
        entrypoint="bash",
    )


def load_skill_prompt() -> str:
    """Load issue-fix-workflow skill for injection into prompts."""
    path = SKILLS_DIR / "issue-fix-workflow.md"
    if path.exists():
        return path.read_text(encoding="utf-8")
    return ""


def run_one_repo(
    repo: RepoInfo,
    max_retries: int = 2,
    *,
    issue_override: int | None = None,
    progress: ProgressWriter | None = None,
) -> bool:
    """
    Full flow for one repo: clone, bootstrap, Cursor phases, PR.
    Returns True if PR was created, False otherwise.
    Retries with backoff on transient errors.

    If issue_override is set, that open (non-PR) issue is used. Otherwise the host
    only checks that at least one open non-PR issue exists; after clone and context
    gathering, the AI picks the issue to fix (writes .iynx/chosen-issue.json).

    If ``progress`` is set, JSONL events and ``[iynx]`` logs are emitted per phase.
    """
    skill = load_skill_prompt()
    dest = None
    token = os.environ.get("GITHUB_TOKEN")
    issue_num: int | None
    if issue_override is not None:
        issue_num = validate_open_non_pr_issue(repo.owner, repo.name, issue_override, token)
        if issue_num is None:
            logger.warning(
                "Issue #%s is not an open non-PR issue on %s (wrong number, closed, or a pull request); skipping (no clone)",
                issue_override,
                repo.full_name,
            )
            _notify_progress(
                progress,
                repo.full_name,
                "preflight",
                "failed",
                detail="invalid_issue_override",
            )
            return False
        logger.info("Preflight: %s — issue #%s (override)", repo.full_name, issue_num)
        _notify_progress(
            progress,
            repo.full_name,
            "preflight",
            "completed",
            issue=issue_num,
            detail="issue_override",
        )
    else:
        issue_num = None
        if find_first_suitable_open_issue(repo.owner, repo.name, token) is None:
            logger.warning(
                "No open (non-PR) issues found for %s; skipping (no clone). "
                "Pass issue number as argv[2] or set IYNX_TARGET_ISSUE.",
                repo.full_name,
            )
            _notify_progress(
                progress,
                repo.full_name,
                "preflight",
                "failed",
                detail="no_open_issues",
            )
            return False
        logger.info(
            "Preflight: %s — at least one open issue; AI will choose after context",
            repo.full_name,
        )
        _notify_progress(
            progress,
            repo.full_name,
            "preflight",
            "completed",
            detail="open_issues_exist",
        )

    for attempt in range(max_retries):
        try:
            if attempt > 0:
                backoff = min(30, 5 * (2**attempt))
                logger.info(
                    "Retry %d/%d for %s in %ds", attempt + 1, max_retries, repo.full_name, backoff
                )
                time.sleep(backoff)
            # 1. Clone (in Docker)
            _notify_progress(progress, repo.full_name, "clone", "started")
            dest = clone_repo(repo)
            _notify_progress(progress, repo.full_name, "clone", "completed")
            logger.info("Cloned %s to %s", repo.full_name, dest)

            iynx_dir = dest / ".iynx"
            iynx_dir.mkdir(parents=True, exist_ok=True)

            # 2. Write bootstrap and Cursor rules (host writes our files; no repo code execution)
            write_bootstrap(str(dest))
            rules_dir = dest / ".cursor" / "rules"
            rules_dir.mkdir(parents=True, exist_ok=True)
            (rules_dir / "issue-fix-workflow.md").write_text(skill, encoding="utf-8")
            _notify_progress(progress, repo.full_name, "bootstrap", "completed")

            # 3. Phase 1: CONTRIBUTING + structured context (agent writes .iynx/*)
            _notify_progress(progress, repo.full_name, "phase1_context", "started")
            phase1_prompt = f"""Read CONTRIBUTING.md (or the repo's primary contribution doc). The repository has a contribution guide.

Write two files under .iynx/ (create the directory if needed):
1) .iynx/summary.md — concise markdown: how to contribute, PR conventions, branch naming, test command, lint/format commands.
2) .iynx/context.json — valid JSON only, UTF-8, with this shape:
{{"test_command":"exact shell command to run tests from repo root","lint_command":null or string}}

Use the real test command from the repo (e.g. npm test, pytest, cargo test). If unknown, use null for test_command.
Do not commit these files.

Repo: {repo.full_name}
"""
            r1 = run_cursor_phase(dest, phase1_prompt)
            if r1.returncode != 0:
                logger.warning("Phase 1 failed: %s", r1.stderr or r1.stdout)
                _notify_progress(
                    progress,
                    repo.full_name,
                    "phase1_context",
                    "failed",
                    exit_code=r1.returncode,
                    detail="cursor_phase",
                )
            else:
                _notify_progress(progress, repo.full_name, "phase1_context", "completed")

            if issue_num is None:
                qrepo = shlex.quote(repo.full_name)
                phase2_prompt = f"""{skill}

You are selecting ONE GitHub issue to fix in {repo.full_name}.

Read .iynx/summary.md for how this repo expects contributions.

List open items (newest first). Prefer JSON with PR discrimination:
  gh issue list --repo {qrepo} --state open --limit 50 --json number,title,isPullRequest

Only consider rows where isPullRequest is false. If your gh version omits isPullRequest, treat numbers where `gh pr view <n>` succeeds as pull requests and skip them.
Optionally skim files in the repo to see if an issue is scoped enough to fix with a small change and verifiable tests.

Choose one issue you can handle well in this run, or choose none if every open issue is a poor fit (too vague, needs product decision, security-sensitive, far too large, etc.).

Write ONLY valid JSON to .iynx/chosen-issue.json (no markdown fence), UTF-8:
{{"issue": <positive integer>, "reason": "<brief why this issue>"}}
If none are appropriate:
{{"issue": null, "reason": "<brief why not>"}}

Do not commit this file.
"""
                _notify_progress(progress, repo.full_name, "phase2_issue_pick", "started")
                r2 = run_cursor_phase(dest, phase2_prompt, force=True)
                if r2.returncode != 0:
                    logger.warning("Phase 2 (issue selection) failed: %s", r2.stderr or r2.stdout)
                    _notify_progress(
                        progress,
                        repo.full_name,
                        "phase2_issue_pick",
                        "failed",
                        exit_code=r2.returncode,
                        detail="cursor_phase",
                    )
                else:
                    _notify_progress(progress, repo.full_name, "phase2_issue_pick", "completed")
                picked, pick_reason = load_chosen_issue(iynx_dir)
                if picked is None:
                    extra = pick_reason or "No valid selection in .iynx/chosen-issue.json."
                    logger.warning("No issue selected for %s — %s", repo.full_name, extra)
                    _notify_progress(
                        progress,
                        repo.full_name,
                        "phase2_issue_pick",
                        "failed",
                        detail="no_issue_selected",
                    )
                    return False
                validated = validate_open_non_pr_issue(repo.owner, repo.name, picked, token)
                if validated is None:
                    logger.warning(
                        "AI picked #%s but it is not an open issue on %s; aborting",
                        picked,
                        repo.full_name,
                    )
                    _notify_progress(
                        progress,
                        repo.full_name,
                        "phase2_issue_pick",
                        "failed",
                        detail="invalid_pick",
                    )
                    return False
                issue_num = validated
                logger.info(
                    "AI selected issue #%s for %s%s",
                    issue_num,
                    repo.full_name,
                    f" — {pick_reason}" if pick_reason else "",
                )

            assert issue_num is not None

            # 4. Phase 3: Implement fix
            phase3_prompt = f"""{skill}

Implement a fix for issue #{issue_num} in {repo.full_name}.

Read .iynx/summary.md and follow its contribution and PR conventions.
Read .iynx/context.json and run test_command before committing; if tests fail, fix until they pass. Do not commit if tests fail.
Do not add or commit anything under .iynx/ (keep it untracked).

Steps:
1. Read the issue: gh issue view {issue_num}
2. Find root cause and implement a minimal fix
3. Run test_command from .iynx/context.json (and lint if applicable)
4. Commit with a message matching repo conventions (e.g. fix: ... #{issue_num})

Create branch fix/issue-{issue_num} before committing.
"""
            _notify_progress(
                progress, repo.full_name, "phase3_implement", "started", issue=issue_num
            )
            r3 = run_cursor_phase(dest, phase3_prompt, force=True)
            if r3.returncode != 0:
                logger.warning("Phase 3 failed: %s", r3.stderr or r3.stdout)
                _notify_progress(
                    progress,
                    repo.full_name,
                    "phase3_implement",
                    "failed",
                    issue=issue_num,
                    exit_code=r3.returncode,
                    detail="cursor_phase",
                )
                if attempt < max_retries - 1:
                    continue
                return False
            _notify_progress(
                progress, repo.full_name, "phase3_implement", "completed", issue=issue_num
            )

            if not VERIFY_TESTS_AFTER_FIX:
                _notify_progress(
                    progress,
                    repo.full_name,
                    "verify_tests",
                    "skipped",
                    issue=issue_num,
                    detail="verify_disabled",
                )
            elif not _maybe_verify_tests(dest):
                logger.warning("Post-fix test verification failed; skipping PR")
                _notify_progress(
                    progress,
                    repo.full_name,
                    "verify_tests",
                    "failed",
                    issue=issue_num,
                )
                return False
            else:
                _notify_progress(
                    progress,
                    repo.full_name,
                    "verify_tests",
                    "completed",
                    issue=issue_num,
                )

            # 5. Phase 4: PR title/body JSON
            _notify_progress(
                progress, repo.full_name, "phase4_pr_draft", "started", issue=issue_num
            )
            phase4_prompt = f"""Read .iynx/summary.md, gh issue view {issue_num}, and the latest commit message/diff.
Write ONLY valid JSON to .iynx/pr-draft.json (no markdown fence) with keys "title" and "body".
The PR must follow repository PR conventions from the summary. Body should include: summary of changes, how to test, and a line "Fixes #{issue_num}".
Do not commit this file.
"""
            r4 = run_cursor_phase(dest, phase4_prompt, force=True)
            if r4.returncode != 0:
                logger.warning("Phase 4 failed: %s", r4.stderr or r4.stdout)
                _notify_progress(
                    progress,
                    repo.full_name,
                    "phase4_pr_draft",
                    "failed",
                    issue=issue_num,
                    exit_code=r4.returncode,
                    detail="cursor_phase",
                )
            else:
                _notify_progress(
                    progress,
                    repo.full_name,
                    "phase4_pr_draft",
                    "completed",
                    issue=issue_num,
                )

            branch = f"fix/issue-{issue_num}"
            pr_title, pr_body = load_pr_draft(iynx_dir, issue_num)
            (iynx_dir / "pr-body.md").write_text(pr_body, encoding="utf-8")

            qb = shlex.quote(branch)
            pr_title_q = shlex.quote(pr_title)
            upstream_url = f"https://github.com/{repo.owner}/{repo.name}.git"
            qu = shlex.quote(upstream_url)
            pr_helpers = _docker_trace_helpers()
            pr_script = (
                f"{pr_helpers}"
                "set -euo pipefail\n"
                '_iynx_log "pr_create: cd /home/dev/workspace"\n'
                "cd /home/dev/workspace\n"
                '_iynx_log "pr_create: gh auth setup-git"\n'
                "gh auth setup-git\n"
                '_iynx_log "pr_create: git checkout"\n'
                f"(git checkout -b {qb} 2>/dev/null || git checkout {qb})\n"
                '_iynx_log "pr_create: gh repo fork"\n'
                "(gh repo fork --remote=false || true)\n"
                '_iynx_log "pr_create: gh api user"\n'
                "LOGIN=$(gh api user -q .login)\n"
                '_iynx_log "pr_create: login=$LOGIN"\n'
                f'git remote set-url origin "https://github.com/${{LOGIN}}/{repo.name}.git"\n'
                f"(git remote set-url upstream {qu} 2>/dev/null || git remote add upstream {qu})\n"
                '_iynx_log "pr_create: git push"\n'
                f"git push -u origin {qb}\n"
                '_iynx_log "pr_create: gh pr create"\n'
                f"gh pr create --repo {shlex.quote(repo.full_name)} --title {pr_title_q} "
                f"--body-file /home/dev/workspace/.iynx/pr-body.md --base {shlex.quote(repo.default_branch)} "
                f'--head "${{LOGIN}}:{branch}"\n'
            )
            _notify_progress(progress, repo.full_name, "pr_create", "started", issue=issue_num)
            r5 = _docker_run(
                ["-c", pr_script],
                entrypoint="bash",
                env={
                    "GH_TOKEN": os.environ.get("GITHUB_TOKEN"),
                    "GITHUB_TOKEN": os.environ.get("GITHUB_TOKEN"),
                    "GIT_TERMINAL_PROMPT": "0",
                },
                mount=f"{dest.absolute()}:/home/dev/workspace",
                workdir="/home/dev/workspace",
            )
            if r5.returncode != 0:
                logger.error("PR creation failed: %s", r5.stderr or r5.stdout)
                _notify_progress(
                    progress,
                    repo.full_name,
                    "pr_create",
                    "failed",
                    issue=issue_num,
                    exit_code=r5.returncode,
                )
                return False
            _notify_progress(progress, repo.full_name, "pr_create", "completed", issue=issue_num)

            logger.info("PR created for %s issue #%s", repo.full_name, issue_num)
            return True

        except subprocess.TimeoutExpired as e:
            logger.error(
                "Timeout processing %s after %ss (docker/cursor); "
                "increase DOCKER_RUN_TIMEOUT in orchestrator.py if the run needs more time",
                repo.full_name,
                e.timeout,
            )
            _notify_progress(progress, repo.full_name, "workflow", "failed", detail="timeout")
            if attempt < max_retries - 1:
                continue
            return False
        except RuntimeError as e:
            logger.error("Runtime error for %s: %s", repo.full_name, e)
            _notify_progress(
                progress,
                repo.full_name,
                "workflow",
                "failed",
                detail=str(e)[:300],
            )
            if attempt < max_retries - 1:
                continue
            return False
        except Exception as e:
            logger.exception(
                "Unexpected error processing %s (attempt %d): %s", repo.full_name, attempt + 1, e
            )
            _notify_progress(
                progress,
                repo.full_name,
                "workflow",
                "failed",
                detail=f"unexpected:{type(e).__name__}",
            )
            if attempt < max_retries - 1:
                continue
            return False

    return False


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
    )

    if not os.environ.get("CURSOR_API_KEY"):
        logger.error("CURSOR_API_KEY is required")
        sys.exit(1)
    if not os.environ.get("GITHUB_TOKEN"):
        logger.warning("GITHUB_TOKEN recommended for discovery and PR creation")

    WORKSPACE.mkdir(parents=True, exist_ok=True)

    run_id = uuid.uuid4().hex[:12]
    pw = progress_writer_from_env(run_id=run_id, project_root=PROJECT_ROOT)

    token = os.environ.get("GITHUB_TOKEN")
    explicit, issue_override = resolve_target_repo_from_env_or_argv(token)
    if explicit is not None:
        repo = explicit
        if issue_override is not None:
            logger.info(
                "Explicit target %s (issue override #%s); skipping discovery",
                repo.full_name,
                issue_override,
            )
        else:
            logger.info("Explicit target %s; skipping discovery", repo.full_name)
        _notify_progress(pw, repo.full_name, "target_resolve", "completed")
        success_count = 1 if run_one_repo(repo, issue_override=issue_override, progress=pw) else 0
        logger.info("Done. %d PR(s) created.", success_count)
        _notify_progress(
            pw,
            None,
            "run_complete",
            "completed",
            detail="pr_created" if success_count else "no_pr",
            exit_code=0 if success_count else 2,
        )
        if success_count == 0:
            sys.exit(2)
        return

    repos = discover_repos_for_run(token=token)
    _notify_progress(
        pw,
        None,
        "discovery",
        "completed",
        detail=str(len(repos)),
    )
    logger.info("Discovered %d repo(s) after filters", len(repos))
    if not repos:
        _notify_progress(pw, None, "discovery", "skipped", detail="no_repos")
        logger.info("No qualifying repositories; nothing to do.")
        _notify_progress(
            pw,
            None,
            "run_complete",
            "completed",
            detail="no_pr",
            exit_code=2,
        )
        sys.exit(2)

    repo = random.choice(repos)
    logger.info("Selected %s (random of %d qualifying)", repo.full_name, len(repos))
    success_count = 1 if run_one_repo(repo, progress=pw) else 0
    logger.info("Done. %d PR(s) created.", success_count)
    _notify_progress(
        pw,
        repo.full_name,
        "run_complete",
        "completed",
        detail="pr_created" if success_count else "no_pr",
        exit_code=0 if success_count else 2,
    )
    if success_count == 0:
        sys.exit(2)


if __name__ == "__main__":
    main()
