"""Tests for pr_stats (mocked HTTP)."""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest

import pr_stats


def test_format_label_for_query_quotes() -> None:
    assert pr_stats._format_label_for_query("bug") == "bug"
    assert pr_stats._format_label_for_query("a b") == '"a b"'
    assert '"' in pr_stats._format_label_for_query('say "hi"')


def test_build_search_q() -> None:
    q = pr_stats._build_search_q("open", "octocat", "iynx")
    assert "is:pr" in q and "is:open" in q
    assert "author:octocat" in q
    assert "label:iynx" in q


def test_repo_from_repository_url() -> None:
    assert pr_stats._repo_from_repository_url(
        "https://api.github.com/repos/foo/bar"
    ) == ("foo", "bar")


def test_repo_from_issue_item_fallbacks() -> None:
    assert pr_stats._repo_from_issue_item(
        {"number": 1, "html_url": "https://github.com/foo/bar/pull/99"}
    ) == ("foo", "bar")
    assert pr_stats._repo_from_issue_item(
        {"number": 1, "repository": {"full_name": "a/b"}}
    ) == ("a", "b")
    assert pr_stats._repo_from_issue_item({"number": 1}) is None


def test_resolve_label_cli_wins(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("IYNX_STATS_LABEL", raising=False)
    monkeypatch.delenv("IYNX_PR_LABEL", raising=False)
    assert pr_stats.resolve_label("from-cli") == "from-cli"


def test_resolve_label_env_chain(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("IYNX_STATS_LABEL", "stats-l")
    monkeypatch.setenv("IYNX_PR_LABEL", "pr-l")
    assert pr_stats.resolve_label(None) == "stats-l"
    monkeypatch.delenv("IYNX_STATS_LABEL", raising=False)
    assert pr_stats.resolve_label(None) == "pr-l"


def test_resolve_label_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("IYNX_STATS_LABEL", raising=False)
    monkeypatch.delenv("IYNX_PR_LABEL", raising=False)
    with pytest.raises(ValueError):
        pr_stats.resolve_label(None)


def test_resolve_branch_regex() -> None:
    r, src = pr_stats.resolve_branch_regex(None)
    assert src == "default"
    assert r.pattern == pr_stats.DEFAULT_BRANCH_REGEX


def test_bucket_counts() -> None:
    result = pr_stats.StatsResult(
        author="a",
        label="l",
        branch_pattern=pr_stats.DEFAULT_BRANCH_REGEX,
        branch_pattern_source="default",
        counts=pr_stats.Counts(),
        by_repo={},
        limits={},
    )
    # exercise render paths
    s = pr_stats.render_card(result, use_color=False)
    assert "iynx" in s
    assert pr_stats.render_table(result, use_color=False)


def test_run_exits_without_token(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    assert pr_stats.run(["--format", "json", "--label", "x"]) == 1


def test_compute_stats_mocked(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GITHUB_TOKEN", "tok")

    search_open = {
        "total_count": 1,
        "items": [
            {
                "number": 1,
                "repository_url": "https://api.github.com/repos/o/r",
            }
        ],
    }
    search_closed = {"total_count": 0, "items": []}
    pull = {
        "state": "open",
        "merged_at": None,
        "head": {"ref": "fix/issue-42"},
    }

    def fake_get(url: str, **_kwargs: object) -> MagicMock:
        m = MagicMock()
        m.status_code = 200
        m.headers = {}
        m.raise_for_status = MagicMock()
        if url.endswith("/user"):
            m.json.return_value = {"login": "octocat"}
        elif "/search/issues" in url:
            if "is:open" in str(_kwargs.get("params", {}).get("q", "")):
                m.json.return_value = search_open
            else:
                m.json.return_value = search_closed
        elif "/pulls/1" in url:
            m.json.return_value = pull
        else:
            m.json.return_value = {}
        return m

    with patch.object(pr_stats.requests, "get", side_effect=fake_get):
        r = pr_stats.compute_stats(
            token="tok",
            label="lb",
            branch_re=pr_stats.resolve_branch_regex(None)[0],
            branch_pattern_source="default",
            author="octocat",
            max_items=None,
        )
    assert r.counts.total == 1
    assert r.counts.open == 1
    assert r.limits["search_items_fetched"] == 1
    assert r.limits["search_truncated"] is False


def test_result_to_json_omits_empty_by_repo() -> None:
    r = pr_stats.StatsResult(
        author="a",
        label="l",
        branch_pattern=r"^fix/issue-\d+$",
        branch_pattern_source="default",
        counts=pr_stats.Counts(),
        by_repo={},
        limits={"search_total_count": 0, "search_items_fetched": 0},
    )
    d = pr_stats.result_to_json(r)
    assert "by_repo" not in d


def test_json_output_schema(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.setenv("GITHUB_TOKEN", "tok")
    monkeypatch.setenv("NO_COLOR", "1")

    def fake_get(url: str, **_kwargs: object) -> MagicMock:
        m = MagicMock()
        m.status_code = 200
        m.headers = {}
        m.raise_for_status = MagicMock()
        if url.endswith("/user"):
            m.json.return_value = {"login": "octocat"}
        elif "/search/issues" in url:
            q = _kwargs.get("params", {}).get("q", "")
            if "is:open" in str(q):
                m.json.return_value = {
                    "total_count": 0,
                    "items": [],
                }
            else:
                m.json.return_value = {"total_count": 0, "items": []}
        return m

    with patch.object(pr_stats.requests, "get", side_effect=fake_get):
        code = pr_stats.run(["--format", "json", "--label", "lb"])
    assert code == 0
    out = capsys.readouterr().out
    data = json.loads(out)
    assert data["schema_version"] == 1
    assert "limits" in data
