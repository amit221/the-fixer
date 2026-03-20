"""Unit tests for orchestrator helpers (no Docker)."""

from __future__ import annotations

import json
import logging
import subprocess
import sys
from pathlib import Path
from unittest.mock import ANY, MagicMock, patch

import pytest

import orchestrator
from discovery import RepoInfo


def test_read_json_file_missing(tmp_path: Path) -> None:
    assert orchestrator._read_json_file(tmp_path / "none.json") is None


def test_read_json_file_invalid(tmp_path: Path) -> None:
    p = tmp_path / "x.json"
    p.write_text("{bad", encoding="utf-8")
    assert orchestrator._read_json_file(p) is None


def test_read_json_file_not_dict(tmp_path: Path) -> None:
    p = tmp_path / "x.json"
    p.write_text("[1]", encoding="utf-8")
    assert orchestrator._read_json_file(p) is None


def test_read_json_file_os_error(tmp_path: Path) -> None:
    p = tmp_path / "x.json"
    p.write_text("{}", encoding="utf-8")
    with patch.object(Path, "read_text", side_effect=OSError("denied")):
        assert orchestrator._read_json_file(p) is None


def test_load_pr_draft_malformed_json_uses_defaults(tmp_path: Path) -> None:
    iynx = tmp_path / ".iynx"
    iynx.mkdir()
    (iynx / "pr-draft.json").write_text("{", encoding="utf-8")
    title, body = orchestrator.load_pr_draft(iynx, 5)
    assert "#5" in title
    assert "Fixes #5" in body


def test_load_pr_draft_empty_title_body_fallback(tmp_path: Path) -> None:
    iynx = tmp_path / ".iynx"
    iynx.mkdir()
    (iynx / "pr-draft.json").write_text(
        json.dumps({"title": "  ", "body": 123}),
        encoding="utf-8",
    )
    title, body = orchestrator.load_pr_draft(iynx, 9)
    assert "#9" in title
    assert "Fixes #9" in body


def test_load_chosen_issue_missing(tmp_path: Path) -> None:
    iynx = tmp_path / ".iynx"
    iynx.mkdir()
    assert orchestrator.load_chosen_issue(iynx) == (None, None)


def test_load_chosen_issue_picked(tmp_path: Path) -> None:
    iynx = tmp_path / ".iynx"
    iynx.mkdir()
    (iynx / "chosen-issue.json").write_text(
        json.dumps({"issue": 42, "reason": " small fix "}),
        encoding="utf-8",
    )
    num, reason = orchestrator.load_chosen_issue(iynx)
    assert num == 42
    assert reason == "small fix"


def test_load_chosen_issue_declined(tmp_path: Path) -> None:
    iynx = tmp_path / ".iynx"
    iynx.mkdir()
    (iynx / "chosen-issue.json").write_text(
        json.dumps({"issue": None, "reason": "all too large"}),
        encoding="utf-8",
    )
    assert orchestrator.load_chosen_issue(iynx) == (None, "all too large")


def test_load_chosen_issue_invalid_number(tmp_path: Path) -> None:
    iynx = tmp_path / ".iynx"
    iynx.mkdir()
    (iynx / "chosen-issue.json").write_text(
        json.dumps({"issue": 0, "reason": "bad"}),
        encoding="utf-8",
    )
    num, reason = orchestrator.load_chosen_issue(iynx)
    assert num is None
    assert reason == "bad"


def test_load_skill_prompt_missing_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(orchestrator, "SKILLS_DIR", tmp_path)
    assert orchestrator.load_skill_prompt() == ""


def test_load_skill_prompt_reads(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(orchestrator, "SKILLS_DIR", tmp_path)
    (tmp_path / "issue-fix-workflow.md").write_text("skill-body", encoding="utf-8")
    assert orchestrator.load_skill_prompt() == "skill-body"


@patch("orchestrator.subprocess.Popen")
def test_docker_run_stream_adds_tty_by_default(mock_popen: MagicMock) -> None:
    mock_proc = MagicMock()
    mock_proc.stdout = []
    mock_proc.wait = MagicMock(return_value=None)
    mock_proc.returncode = 0
    mock_popen.return_value = mock_proc
    orchestrator._docker_run(["echo", "hi"], entrypoint="bash")
    cmd = mock_popen.call_args[0][0]
    assert "-t" in cmd


@patch("orchestrator.subprocess.Popen")
def test_docker_run_stream_no_tty_when_disabled(
    mock_popen: MagicMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("IYNX_DOCKER_TTY", "0")
    mock_proc = MagicMock()
    mock_proc.stdout = []
    mock_proc.wait = MagicMock(return_value=None)
    mock_proc.returncode = 0
    mock_popen.return_value = mock_proc
    orchestrator._docker_run(["echo", "hi"], entrypoint="bash")
    cmd = mock_popen.call_args[0][0]
    assert "-t" not in cmd


@patch("orchestrator.subprocess.run")
def test_docker_run_command_shape(mock_run: MagicMock) -> None:
    mock_run.return_value = MagicMock(returncode=0)
    orchestrator._docker_run(
        ["echo", "x"],
        env={"A": "1", "B": None},
        mount="host:guest",
        workdir="/w",
        entrypoint="bash",
        stream_logs=False,
    )
    cmd = mock_run.call_args[0][0]
    assert cmd[0:2] == ["docker", "run"]
    assert "--rm" in cmd
    assert "bash" in cmd
    assert "iynx-agent:latest" in cmd
    assert "-e" in cmd and "A=1" in cmd
    assert "B=" not in " ".join(cmd)


@patch("orchestrator._docker_run")
def test_clone_repo_success(
    mock_docker: MagicMock, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(orchestrator, "WORKSPACE", tmp_path)
    mock_docker.return_value = MagicMock(returncode=0, stderr="", stdout="")
    repo = RepoInfo(
        owner="o",
        name="n",
        full_name="o/n",
        clone_url="https://github.com/o/n.git",
        stars=1,
        language=None,
        description=None,
        default_branch="main",
    )
    dest = orchestrator.clone_repo(repo)
    assert dest == tmp_path / "o-n"
    mock_docker.assert_called_once()
    inner = mock_docker.call_args[0][0]
    assert inner[0] == "-c"
    assert "git clone" in inner[1]
    assert "--progress" in inner[1]


@patch("orchestrator._docker_run")
def test_clone_repo_failure(
    mock_docker: MagicMock, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(orchestrator, "WORKSPACE", tmp_path)
    mock_docker.return_value = MagicMock(returncode=1, stderr="clone failed", stdout="")
    repo = RepoInfo(
        owner="o",
        name="n",
        full_name="o/n",
        clone_url="https://github.com/o/n.git",
        stars=1,
        language=None,
        description=None,
        default_branch="main",
    )
    with pytest.raises(RuntimeError, match="git clone failed"):
        orchestrator.clone_repo(repo)


@patch("orchestrator._docker_run")
def test_maybe_verify_tests_runs_script(
    mock_docker: MagicMock, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(orchestrator, "VERIFY_TESTS_AFTER_FIX", True)
    d = tmp_path / "repo"
    d.mkdir()
    iynx = d / ".iynx"
    iynx.mkdir()
    (iynx / "context.json").write_text(json.dumps({"test_command": "pytest -q"}), encoding="utf-8")
    mock_docker.return_value = MagicMock(returncode=0, stderr="", stdout="ok")
    assert orchestrator._maybe_verify_tests(d) is True
    mock_docker.assert_called_once()


@patch("orchestrator._docker_run")
def test_maybe_verify_tests_skips_when_disabled(
    mock_docker: MagicMock, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(orchestrator, "VERIFY_TESTS_AFTER_FIX", False)
    assert orchestrator._maybe_verify_tests(tmp_path) is True
    mock_docker.assert_not_called()


@patch("orchestrator._docker_run")
def test_maybe_verify_tests_no_context_json(
    mock_docker: MagicMock, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(orchestrator, "VERIFY_TESTS_AFTER_FIX", True)
    d = tmp_path / "repo"
    d.mkdir()
    assert orchestrator._maybe_verify_tests(d) is True
    mock_docker.assert_not_called()


@patch("orchestrator._docker_run")
def test_maybe_verify_tests_empty_test_command(
    mock_docker: MagicMock, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(orchestrator, "VERIFY_TESTS_AFTER_FIX", True)
    d = tmp_path / "repo"
    d.mkdir()
    iynx = d / ".iynx"
    iynx.mkdir()
    (iynx / "context.json").write_text(json.dumps({"test_command": ""}), encoding="utf-8")
    assert orchestrator._maybe_verify_tests(d) is True
    mock_docker.assert_not_called()


@patch("orchestrator._docker_run")
def test_maybe_verify_tests_fails_return_false(
    mock_docker: MagicMock, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(orchestrator, "VERIFY_TESTS_AFTER_FIX", True)
    d = tmp_path / "repo"
    d.mkdir()
    iynx = d / ".iynx"
    iynx.mkdir()
    (iynx / "context.json").write_text(json.dumps({"test_command": "pytest"}), encoding="utf-8")
    mock_docker.return_value = MagicMock(returncode=1, stderr="fail", stdout="")
    assert orchestrator._maybe_verify_tests(d) is False


def test_rmtree_retry_chmod_permission_then_ok() -> None:
    func = MagicMock()
    path = "/x"
    exc = PermissionError("denied")
    with patch("orchestrator.os.chmod"):
        orchestrator._rmtree_retry_chmod(func, path, exc)
    func.assert_called_once_with(path)


def test_rmtree_retry_chmod_non_permission_raises() -> None:
    with pytest.raises(ValueError):
        orchestrator._rmtree_retry_chmod(MagicMock(), "/p", ValueError("other"))


@patch("orchestrator.fetch_repo_candidates")
@patch("orchestrator.repo_has_contributing_guide", return_value=True)
@patch("orchestrator.user_has_pr_to_repo", return_value=False)
@patch("orchestrator.get_token_login", return_value="alice")
def test_discover_repos_for_run_returns_all_filtered(
    _gl: MagicMock,
    _pr: MagicMock,
    _contrib: MagicMock,
    mock_fetch: MagicMock,
) -> None:
    repos_data = [RepoInfo("a", f"r{i}", f"a/r{i}", "u", i, None, None, "main") for i in range(5)]
    mock_fetch.return_value = repos_data
    out = orchestrator.discover_repos_for_run(token="tok")
    assert len(out) == 5
    assert out[0].name == "r0"


@patch("orchestrator.fetch_repo_candidates")
@patch("orchestrator.repo_has_contributing_guide", return_value=False)
def test_discover_repos_skips_without_contributing(
    mock_contrib: MagicMock,
    mock_fetch: MagicMock,
) -> None:
    mock_fetch.return_value = [
        RepoInfo("a", "r0", "a/r0", "u", 1, None, None, "main"),
    ]
    assert orchestrator.discover_repos_for_run(token="t") == []


@patch("orchestrator.fetch_repo_candidates")
@patch("orchestrator.repo_has_contributing_guide", return_value=True)
@patch("orchestrator.user_has_pr_to_repo", return_value=True)
@patch("orchestrator.get_token_login", return_value="u")
def test_discover_repos_skips_already_contributed(
    _gl: MagicMock,
    _pr: MagicMock,
    _c: MagicMock,
    mock_fetch: MagicMock,
) -> None:
    mock_fetch.return_value = [
        RepoInfo("a", "r0", "a/r0", "u", 1, None, None, "main"),
    ]
    assert orchestrator.discover_repos_for_run(token="t") == []


def test_main_requires_cursor_key(monkeypatch: pytest.MonkeyPatch) -> None:
    """Do not mock sys.exit — a no-op mock lets main() continue into real discovery (network)."""
    monkeypatch.delenv("CURSOR_API_KEY", raising=False)
    with pytest.raises(SystemExit) as exc:
        orchestrator.main()
    assert exc.value.code == 1


def test_cursor_print_output_flags_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("IYNX_CURSOR_OUTPUT_FORMAT", raising=False)
    monkeypatch.delenv("IYNX_CURSOR_STREAM_PARTIAL", raising=False)
    assert orchestrator._cursor_print_output_flags() == [
        "--output-format",
        "stream-json",
        "--stream-partial-output",
    ]


def test_cursor_print_output_flags_text_omits_partial(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("IYNX_CURSOR_OUTPUT_FORMAT", "text")
    assert orchestrator._cursor_print_output_flags() == ["--output-format", "text"]


def test_cursor_print_output_flags_stream_json_partial_off(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("IYNX_CURSOR_OUTPUT_FORMAT", "stream-json")
    monkeypatch.setenv("IYNX_CURSOR_STREAM_PARTIAL", "0")
    assert orchestrator._cursor_print_output_flags() == ["--output-format", "stream-json"]


def test_cursor_print_output_flags_invalid_falls_back(
    caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("IYNX_CURSOR_OUTPUT_FORMAT", "bogus")
    with caplog.at_level(logging.WARNING):
        flags = orchestrator._cursor_print_output_flags()
    assert flags == ["--output-format", "stream-json", "--stream-partial-output"]
    assert any("bogus" in r.getMessage() for r in caplog.records)


@patch("orchestrator._docker_run")
def test_run_cursor_phase_adds_model_and_force(
    mock_docker: MagicMock, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CURSOR_API_KEY", "k")
    monkeypatch.setenv("GITHUB_TOKEN", "g")
    monkeypatch.setenv("IYNX_CURSOR_PERMISSIVE", "0")
    mock_docker.return_value = MagicMock(returncode=0)
    (tmp_path / "iynx.cursor-agent").write_text("#!/bin/bash\necho\n", encoding="utf-8")
    orchestrator.run_cursor_phase(tmp_path, "do work", force=True)
    mock_docker.assert_called_once()
    inner = mock_docker.call_args[0][0]
    assert isinstance(inner, list)
    assert inner[0] == "-c"
    bash_script = inner[1]
    assert "cursor-agent" in bash_script
    assert "--force" in bash_script
    assert orchestrator.CURSOR_AGENT_MODEL in bash_script
    assert "--output-format" in bash_script
    assert "stream-json" in bash_script
    assert "--stream-partial-output" in bash_script
    assert "[iynx-docker]" in bash_script
    assert "cursor_phase:" in bash_script


@patch("orchestrator._docker_run")
def test_run_cursor_phase_permissive_yolo_by_default(
    mock_docker: MagicMock, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("IYNX_CURSOR_PERMISSIVE", raising=False)
    monkeypatch.delenv("IYNX_CURSOR_EXTRA_ARGS", raising=False)
    monkeypatch.setenv("CURSOR_API_KEY", "k")
    mock_docker.return_value = MagicMock(returncode=0)
    (tmp_path / "iynx.cursor-agent").write_text("#!/bin/bash\necho\n", encoding="utf-8")
    orchestrator.run_cursor_phase(tmp_path, "hello", force=False)
    bash_script = mock_docker.call_args[0][0][1]
    assert "--yolo" in bash_script
    assert "--approve-mcps" in bash_script
    assert "--sandbox" in bash_script and "disabled" in bash_script


@patch("orchestrator.discover_repos_for_run", return_value=[])
def test_main_runs_discovery_when_key_present(
    _disc: MagicMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CURSOR_API_KEY", "x")
    monkeypatch.setenv("IYNX_PROGRESS_JSONL", "")
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    with pytest.raises(SystemExit) as exc:
        orchestrator.main()
    assert exc.value.code == 2


@patch("orchestrator.run_one_repo", return_value=False)
@patch("orchestrator.discover_repos_for_run")
def test_main_runs_only_first_repo(
    mock_disc: MagicMock, mock_run: MagicMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CURSOR_API_KEY", "x")
    monkeypatch.setenv("IYNX_PROGRESS_JSONL", "")
    mock_disc.return_value = [
        RepoInfo("a", "r0", "a/r0", "u", 1, None, None, "main"),
        RepoInfo("a", "r1", "a/r1", "u", 1, None, None, "main"),
    ]
    with pytest.raises(SystemExit) as exc:
        orchestrator.main()
    assert exc.value.code == 2
    mock_run.assert_called_once()
    assert mock_run.call_args[0][0].name == "r0"
    assert mock_run.call_args.kwargs.get("progress") is not None


@patch("orchestrator.find_first_suitable_open_issue", return_value=99)
@patch("orchestrator.clone_repo", side_effect=RuntimeError("clone failed"))
def test_run_one_repo_runtime_error_no_retry(_clone: MagicMock, _issue: MagicMock) -> None:
    repo = RepoInfo(
        owner="o",
        name="n",
        full_name="o/n",
        clone_url="https://github.com/o/n.git",
        stars=1,
        language=None,
        description=None,
        default_branch="main",
    )
    assert orchestrator.run_one_repo(repo, max_retries=1) is False


@patch("orchestrator.time.sleep", return_value=None)
@patch("orchestrator.find_first_suitable_open_issue", return_value=99)
@patch("orchestrator.clone_repo", side_effect=subprocess.TimeoutExpired(cmd="git", timeout=1))
def test_run_one_repo_timeout(_clone: MagicMock, _issue: MagicMock, _sleep: MagicMock) -> None:
    repo = RepoInfo(
        owner="o",
        name="n",
        full_name="o/n",
        clone_url="https://github.com/o/n.git",
        stars=1,
        language=None,
        description=None,
        default_branch="main",
    )
    assert orchestrator.run_one_repo(repo, max_retries=1) is False


@patch("orchestrator.find_first_suitable_open_issue", return_value=None)
@patch("orchestrator.clone_repo")
def test_run_one_repo_skips_clone_when_no_preflight_issue(
    mock_clone: MagicMock, _issue: MagicMock
) -> None:
    repo = RepoInfo(
        owner="o",
        name="n",
        full_name="o/n",
        clone_url="https://github.com/o/n.git",
        stars=1,
        language=None,
        description=None,
        default_branch="main",
    )
    assert orchestrator.run_one_repo(repo, max_retries=1) is False
    mock_clone.assert_not_called()


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("", None),
        ("  ", None),
        ("https://github.com/acme/proj", ("acme", "proj")),
        ("https://github.com/acme/proj.git", ("acme", "proj")),
        ("acme/proj", ("acme", "proj")),
        ("acme/proj/", ("acme", "proj")),
        ("bad", None),
        ("a/b/c", None),
    ],
)
def test_parse_owner_repo_string(raw: str, expected: tuple[str, str] | None) -> None:
    assert orchestrator._parse_owner_repo_string(raw) == expected


def test_parse_cli_target_repo_and_issue_no_args(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "argv", ["orchestrator"])
    pair, issue = orchestrator.parse_cli_target_repo_and_issue()
    assert pair is None and issue is None


def test_parse_cli_target_repo_and_issue_with_repo_and_issue(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(sys, "argv", ["orchestrator", "o/r", "12"])
    pair, issue = orchestrator.parse_cli_target_repo_and_issue()
    assert pair == ("o", "r")
    assert issue == 12


def test_parse_cli_target_repo_and_issue_invalid_issue_number(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(sys, "argv", ["orchestrator", "o/r", "nope"])
    pair, issue = orchestrator.parse_cli_target_repo_and_issue()
    assert pair == ("o", "r")
    assert issue is None


@patch("orchestrator.fetch_repo_by_full_name")
def test_resolve_target_from_env_invalid_issue(
    mock_fetch: MagicMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sys, "argv", ["prog"])
    monkeypatch.setenv("IYNX_TARGET_REPO", "env/o")
    monkeypatch.setenv("IYNX_TARGET_ISSUE", "not-int")
    repo = RepoInfo(
        owner="env",
        name="o",
        full_name="env/o",
        clone_url="u",
        stars=1,
        language=None,
        description=None,
        default_branch="main",
    )
    mock_fetch.return_value = repo
    out_repo, issue = orchestrator.resolve_target_repo_from_env_or_argv("tok")
    assert out_repo == repo
    assert issue is None


@patch("orchestrator.fetch_repo_by_full_name")
@patch("orchestrator.parse_cli_target_repo_and_issue")
def test_resolve_target_prefers_argv_over_env(
    mock_parse: MagicMock, mock_fetch: MagicMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("IYNX_TARGET_REPO", "ignored/ignored")
    mock_parse.return_value = (("cli", "repo"), 7)
    r = RepoInfo(
        owner="cli",
        name="repo",
        full_name="cli/repo",
        clone_url="u",
        stars=1,
        language=None,
        description=None,
        default_branch="main",
    )
    mock_fetch.return_value = r
    out, issue = orchestrator.resolve_target_repo_from_env_or_argv("t")
    assert out == r
    assert issue == 7
    mock_fetch.assert_called_once_with("cli", "repo", token="t")


@patch("orchestrator.fetch_repo_candidates")
@patch("orchestrator.repo_has_contributing_guide", return_value=True)
@patch("orchestrator.user_has_pr_to_repo", return_value=False)
@patch("orchestrator.get_token_login", return_value=None)
def test_discover_repos_skips_pr_filter_when_login_unresolved(
    _gl: MagicMock,
    _pr: MagicMock,
    _contrib: MagicMock,
    mock_fetch: MagicMock,
    caplog: pytest.LogCaptureFixture,
) -> None:
    mock_fetch.return_value = [
        RepoInfo("a", "r0", "a/r0", "u", 1, None, None, "main"),
    ]
    with caplog.at_level(logging.WARNING, logger="orchestrator"):
        out = orchestrator.discover_repos_for_run(token="tok")
    assert len(out) == 1
    assert any("Could not resolve GitHub login" in r.message for r in caplog.records)


@patch("orchestrator._docker_run")
@patch("orchestrator._maybe_verify_tests", return_value=True)
@patch("orchestrator.run_cursor_phase")
@patch("orchestrator.clone_repo")
@patch("orchestrator.validate_open_non_pr_issue", return_value=42)
def test_run_one_repo_success_with_issue_override(
    _val: MagicMock,
    mock_clone: MagicMock,
    mock_phase: MagicMock,
    _verify: MagicMock,
    mock_docker: MagicMock,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(orchestrator, "WORKSPACE", tmp_path)
    monkeypatch.setenv("GITHUB_TOKEN", "g")
    dest = tmp_path / "o-n"
    dest.mkdir(parents=True)
    mock_clone.return_value = dest
    mock_phase.return_value = MagicMock(returncode=0)
    mock_docker.return_value = MagicMock(returncode=0)
    repo = RepoInfo(
        owner="o",
        name="n",
        full_name="o/n",
        clone_url="https://github.com/o/n.git",
        stars=1,
        language=None,
        description=None,
        default_branch="main",
    )
    assert orchestrator.run_one_repo(repo, max_retries=1, issue_override=42) is True
    assert mock_phase.call_count == 3
    mock_docker.assert_called_once()


@patch("orchestrator.validate_open_non_pr_issue", return_value=None)
@patch("orchestrator.clone_repo")
def test_run_one_repo_skips_when_issue_override_invalid(
    mock_clone: MagicMock, _val: MagicMock
) -> None:
    repo = RepoInfo(
        owner="o",
        name="n",
        full_name="o/n",
        clone_url="https://github.com/o/n.git",
        stars=1,
        language=None,
        description=None,
        default_branch="main",
    )
    assert orchestrator.run_one_repo(repo, max_retries=1, issue_override=99) is False
    mock_clone.assert_not_called()


@patch("orchestrator.run_one_repo", return_value=True)
@patch("orchestrator.resolve_target_repo_from_env_or_argv")
def test_main_explicit_target_without_issue_override(
    mock_resolve: MagicMock, mock_run: MagicMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CURSOR_API_KEY", "k")
    monkeypatch.setenv("IYNX_PROGRESS_JSONL", "")
    repo = RepoInfo(
        owner="o",
        name="n",
        full_name="o/n",
        clone_url="u",
        stars=1,
        language=None,
        description=None,
        default_branch="main",
    )
    mock_resolve.return_value = (repo, None)
    orchestrator.main()
    mock_run.assert_called_once_with(repo, issue_override=None, progress=ANY)


@patch("orchestrator.run_one_repo", return_value=False)
@patch("orchestrator.resolve_target_repo_from_env_or_argv")
def test_main_explicit_target_with_issue_override(
    mock_resolve: MagicMock, mock_run: MagicMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CURSOR_API_KEY", "k")
    monkeypatch.setenv("IYNX_PROGRESS_JSONL", "")
    repo = RepoInfo(
        owner="o",
        name="n",
        full_name="o/n",
        clone_url="u",
        stars=1,
        language=None,
        description=None,
        default_branch="main",
    )
    mock_resolve.return_value = (repo, 5)
    with pytest.raises(SystemExit) as exc:
        orchestrator.main()
    assert exc.value.code == 2
    mock_run.assert_called_once_with(repo, issue_override=5, progress=ANY)


@patch("orchestrator.shutil.rmtree")
def test_remove_workspace_dir_posix(
    mock_rmtree: MagicMock, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(orchestrator.os, "name", "posix")
    p = tmp_path / "ws"
    p.mkdir()
    orchestrator._remove_workspace_dir(p)
    mock_rmtree.assert_called_once()


@patch("orchestrator._docker_run")
@patch("orchestrator.Path.chmod", side_effect=OSError("chmod"))
def test_maybe_verify_tests_ignores_chmod_error(
    _chmod: MagicMock,
    mock_docker: MagicMock,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(orchestrator, "VERIFY_TESTS_AFTER_FIX", True)
    d = tmp_path / "repo"
    d.mkdir()
    iynx = d / ".iynx"
    iynx.mkdir()
    (iynx / "context.json").write_text(json.dumps({"test_command": "pytest -q"}), encoding="utf-8")
    mock_docker.return_value = MagicMock(returncode=0, stderr="", stdout="")
    assert orchestrator._maybe_verify_tests(d) is True
    mock_docker.assert_called_once()
