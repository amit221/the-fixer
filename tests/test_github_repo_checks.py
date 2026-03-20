"""Tests for GitHub repo checks (mocked HTTP)."""

from unittest.mock import MagicMock, patch

import requests

import github_repo_checks as grc


@patch("github_repo_checks.requests.get")
def test_repo_has_contributing_guide_first_path_200(mock_get: MagicMock) -> None:
    ok = MagicMock()
    ok.status_code = 200
    mock_get.return_value = ok
    assert grc.repo_has_contributing_guide("o", "r", "tok") is True
    mock_get.assert_called_once()
    assert "CONTRIBUTING.md" in mock_get.call_args[0][0]


@patch("github_repo_checks.requests.get")
def test_repo_has_contributing_guide_tries_fallback(mock_get: MagicMock) -> None:
    n404 = MagicMock()
    n404.status_code = 404
    ok = MagicMock()
    ok.status_code = 200
    mock_get.side_effect = [n404, n404, ok]
    assert grc.repo_has_contributing_guide("o", "r", "tok") is True
    assert mock_get.call_count == 3


@patch("github_repo_checks.requests.get")
def test_get_token_login(mock_get: MagicMock) -> None:
    mock_get.return_value.json.return_value = {"login": "alice"}
    mock_get.return_value.raise_for_status = MagicMock()
    assert grc.get_token_login("tok") == "alice"


@patch("github_repo_checks.requests.get")
def test_user_has_pr_to_repo_true(mock_get: MagicMock) -> None:
    mock_get.return_value.json.return_value = {"total_count": 2}
    mock_get.return_value.raise_for_status = MagicMock()
    assert grc.user_has_pr_to_repo("alice", "o", "r", "tok") is True


@patch("github_repo_checks.requests.get")
def test_user_has_pr_to_repo_false(mock_get: MagicMock) -> None:
    mock_get.return_value.json.return_value = {"total_count": 0}
    mock_get.return_value.raise_for_status = MagicMock()
    assert grc.user_has_pr_to_repo("alice", "o", "r", "tok") is False


@patch("github_repo_checks.requests.get")
def test_find_first_suitable_open_issue_returns_first_non_pr(mock_get: MagicMock) -> None:
    mock_get.return_value.json.return_value = [
        {"number": 1, "pull_request": {}},
        {"number": 7, "title": "bug"},
    ]
    mock_get.return_value.raise_for_status = MagicMock()
    assert grc.find_first_suitable_open_issue("o", "r", "tok") == 7
    mock_get.assert_called_once()
    params = mock_get.call_args[1]["params"]
    assert "labels" not in params
    assert params["state"] == "open"


@patch("github_repo_checks.requests.get")
def test_find_first_suitable_open_issue_none_when_only_prs(mock_get: MagicMock) -> None:
    mock_get.return_value.json.return_value = [
        {"number": 1, "pull_request": {"url": "x"}},
    ]
    mock_get.return_value.raise_for_status = MagicMock()
    assert grc.find_first_suitable_open_issue("o", "r", "tok") is None


@patch("github_repo_checks.requests.get")
def test_find_first_suitable_open_issue_none_when_all_fail(mock_get: MagicMock) -> None:
    mock_get.side_effect = requests.RequestException("nope")
    assert grc.find_first_suitable_open_issue("o", "r", "tok") is None
    assert mock_get.call_count == 1


@patch("github_repo_checks.requests.get")
def test_find_first_suitable_open_issue_json_not_list(mock_get: MagicMock) -> None:
    mock_get.return_value.json.return_value = {}
    mock_get.return_value.raise_for_status = MagicMock()
    assert grc.find_first_suitable_open_issue("o", "r", "tok") is None


@patch("github_repo_checks.requests.get")
def test_find_first_suitable_open_issue_skips_non_dict_items(mock_get: MagicMock) -> None:
    mock_get.return_value.json.return_value = ["not-a-dict", 1]
    mock_get.return_value.raise_for_status = MagicMock()
    assert grc.find_first_suitable_open_issue("o", "r", "tok") is None


@patch("github_repo_checks.requests.get")
def test_find_first_suitable_open_issue_skips_invalid_number(mock_get: MagicMock) -> None:
    mock_get.return_value.json.return_value = [{"number": "bad"}]
    mock_get.return_value.raise_for_status = MagicMock()
    assert grc.find_first_suitable_open_issue("o", "r", "tok") is None


@patch("github_repo_checks.requests.get")
def test_validate_open_non_pr_issue_invalid_number(mock_get: MagicMock) -> None:
    assert grc.validate_open_non_pr_issue("o", "r", 0, "tok") is None
    mock_get.assert_not_called()


@patch("github_repo_checks.requests.get")
def test_validate_open_non_pr_issue_404(mock_get: MagicMock) -> None:
    mock_get.return_value.status_code = 404
    assert grc.validate_open_non_pr_issue("o", "r", 9, "tok") is None


@patch("github_repo_checks.requests.get")
def test_validate_open_non_pr_issue_closed(mock_get: MagicMock) -> None:
    mock_get.return_value.status_code = 200
    mock_get.return_value.json.return_value = {
        "state": "closed",
        "number": 9,
    }
    mock_get.return_value.raise_for_status = MagicMock()
    assert grc.validate_open_non_pr_issue("o", "r", 9, "tok") is None


@patch("github_repo_checks.requests.get")
def test_validate_open_non_pr_issue_is_pull_request(mock_get: MagicMock) -> None:
    mock_get.return_value.status_code = 200
    mock_get.return_value.json.return_value = {
        "state": "open",
        "number": 9,
        "pull_request": {"url": "x"},
    }
    mock_get.return_value.raise_for_status = MagicMock()
    assert grc.validate_open_non_pr_issue("o", "r", 9, "tok") is None


@patch("github_repo_checks.requests.get")
def test_validate_open_non_pr_issue_success(mock_get: MagicMock) -> None:
    mock_get.return_value.status_code = 200
    mock_get.return_value.json.return_value = {
        "state": "open",
        "number": 42,
    }
    mock_get.return_value.raise_for_status = MagicMock()
    assert grc.validate_open_non_pr_issue("o", "r", 42, "tok") == 42


@patch("github_repo_checks.requests.get")
def test_validate_open_non_pr_issue_request_error(mock_get: MagicMock) -> None:
    mock_get.side_effect = requests.RequestException("down")
    assert grc.validate_open_non_pr_issue("o", "r", 1, "tok") is None


@patch("github_repo_checks.requests.get")
def test_validate_open_non_pr_issue_json_not_dict(mock_get: MagicMock) -> None:
    mock_get.return_value.status_code = 200
    mock_get.return_value.json.return_value = []
    mock_get.return_value.raise_for_status = MagicMock()
    assert grc.validate_open_non_pr_issue("o", "r", 1, "tok") is None


@patch("github_repo_checks.requests.get")
def test_validate_open_non_pr_issue_number_not_int(mock_get: MagicMock) -> None:
    mock_get.return_value.status_code = 200
    mock_get.return_value.json.return_value = {
        "state": "open",
        "number": "1",
    }
    mock_get.return_value.raise_for_status = MagicMock()
    assert grc.validate_open_non_pr_issue("o", "r", 1, "tok") is None
