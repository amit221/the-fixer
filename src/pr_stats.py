"""
GitHub PR statistics: count merged/open/closed PRs matching a label and branch pattern.

Uses REST Search + per-PR GET /pulls for accurate merge state and head ref.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import shutil
import sys
import time
from collections import defaultdict
from dataclasses import dataclass
from typing import Any

import requests

logger = logging.getLogger(__name__)

DEFAULT_BRANCH_REGEX = r"^fix/issue-\d+$"
SEARCH_SORT = "updated"
SEARCH_ORDER = "desc"
PER_PAGE = 100
MAX_RETRIES = 5
GITHUB_API = "https://api.github.com"


def _format_label_for_query(label: str) -> str:
    """Return label token for GitHub search `q` (quote if needed)."""
    if not label:
        return ""
    if any(c in label for c in ' "\t\n:'):
        return '"' + label.replace('"', '\\"') + '"'
    return label


def _build_search_q(state: str, author: str, label: str | None) -> str:
    """state is 'open' or 'closed'. If label is None, search by author only (no label:)."""
    base = f"is:pr is:{state} author:{author}"
    if not label:
        return base
    lt = _format_label_for_query(label)
    return f"{base} label:{lt}"


def _repo_from_repository_url(url: str) -> tuple[str, str]:
    parts = url.rstrip("/").split("/")
    return parts[-2], parts[-1]


def _repo_from_issue_item(item: dict[str, Any]) -> tuple[str, str] | None:
    """
    Search `/search/issues` items usually include `repository_url`; some payloads
    only expose `repository` or `html_url` — resolve owner/repo for GET /pulls.
    """
    url = item.get("repository_url")
    if isinstance(url, str) and url.strip():
        try:
            return _repo_from_repository_url(url)
        except (IndexError, ValueError):
            pass

    repo = item.get("repository")
    if isinstance(repo, dict):
        fn = repo.get("full_name")
        if isinstance(fn, str) and "/" in fn:
            owner, _, name = fn.partition("/")
            if owner and name:
                return owner, name

    html = item.get("html_url")
    if isinstance(html, str):
        m = re.match(r"https?://github\.com/([^/]+)/([^/]+)/(?:pull|issues)/", html)
        if m:
            return m.group(1), m.group(2)
    return None


def _api_headers(token: str) -> dict[str, str]:
    return {
        "Accept": "application/vnd.github.v3+json",
        "Authorization": f"Bearer {token}",
    }


def github_get(
    url: str,
    *,
    token: str,
    params: dict[str, Any] | None = None,
    timeout: float = 30.0,
) -> requests.Response:
    headers = _api_headers(token)
    last_exc: Exception | None = None
    for attempt in range(MAX_RETRIES):
        try:
            resp = requests.get(url, headers=headers, params=params, timeout=timeout)
            if resp.status_code == 429:
                wait = int(resp.headers.get("Retry-After", min(2**attempt, 60)))
                logger.warning("GitHub 429, sleeping %ss", wait)
                time.sleep(wait)
                continue
            if resp.status_code == 403 and "rate limit" in (resp.text or "").lower():
                wait = min(2**attempt, 60)
                logger.warning("GitHub 403 rate limit, sleeping %ss", wait)
                time.sleep(wait)
                continue
            resp.raise_for_status()
            return resp
        except requests.RequestException as e:
            last_exc = e
            logger.warning("GitHub request attempt %d failed: %s", attempt + 1, e)
            if attempt < MAX_RETRIES - 1:
                time.sleep(min(2**attempt, 30))
    assert last_exc is not None
    raise last_exc


def fetch_authenticated_login(token: str) -> str:
    r = github_get(f"{GITHUB_API}/user", token=token)
    data = r.json()
    login = data.get("login")
    if not login or not isinstance(login, str):
        raise RuntimeError("GET /user did not return login")
    return login


def paginate_search_issues(
    q: str,
    *,
    token: str,
    budget: int | None,
    already_fetched: int,
) -> tuple[list[dict[str, Any]], int, int, int]:
    """
    Returns (items, total_count, items_fetched_this_query, pages_used).
    Stops when no items, budget exhausted, or GitHub 1000 cap per query.
    """
    items: list[dict[str, Any]] = []
    total_count = 0
    page = 1
    fetched_this = 0

    while True:
        remaining = None if budget is None else max(0, budget - already_fetched - fetched_this)
        if remaining is not None and remaining <= 0:
            break

        per = min(PER_PAGE, remaining if remaining is not None else PER_PAGE)
        if per <= 0:
            break

        params = {
            "q": q,
            "sort": SEARCH_SORT,
            "order": SEARCH_ORDER,
            "per_page": per,
            "page": page,
        }
        r = github_get(f"{GITHUB_API}/search/issues", token=token, params=params)
        data = r.json()
        if page == 1:
            total_count = int(data.get("total_count", 0))
        batch = data.get("items") or []
        if not batch:
            break

        for it in batch:
            if budget is not None and already_fetched + fetched_this >= budget:
                break
            items.append(it)
            fetched_this += 1
            if len(items) >= 1000:
                break

        if len(items) >= 1000:
            break
        if len(batch) < per:
            break
        if budget is not None and already_fetched + fetched_this >= budget:
            break
        page += 1

    return items, total_count, fetched_this, page


def fetch_pull(
    owner: str,
    repo: str,
    number: int,
    *,
    token: str,
) -> dict[str, Any]:
    url = f"{GITHUB_API}/repos/{owner}/{repo}/pulls/{number}"
    r = github_get(url, token=token)
    return r.json()


def resolve_label(cli_label: str | None, *, no_label: bool) -> str | None:
    """
    Label for GitHub Search. If ``no_label`` is True, do not filter by label (branch regex only).
    Otherwise require --label or IYNX_STATS_LABEL / IYNX_PR_LABEL.
    """
    if no_label:
        return None
    raw_stats = os.environ.get("IYNX_STATS_LABEL")
    raw_pr = os.environ.get("IYNX_PR_LABEL")
    if cli_label and cli_label.strip():
        return cli_label.strip()
    if raw_stats and str(raw_stats).strip():
        return str(raw_stats).strip()
    if raw_pr and str(raw_pr).strip():
        return str(raw_pr).strip()
    raise ValueError(
        "Set IYNX_STATS_LABEL or IYNX_PR_LABEL, pass --label, or use --no-label "
        "to match PRs by author + branch only (no label in search)."
    )


def no_label_from_env() -> bool:
    v = os.environ.get("IYNX_STATS_NO_LABEL", "").strip().lower()
    return v in ("1", "true", "yes", "on")


def resolve_branch_regex(cli_regex: str | None) -> tuple[re.Pattern[str], str]:
    raw = os.environ.get("IYNX_STATS_BRANCH_REGEX")
    if cli_regex and cli_regex.strip():
        return re.compile(cli_regex.strip()), "cli"
    if raw and str(raw).strip():
        return re.compile(str(raw).strip()), "env"
    return re.compile(DEFAULT_BRANCH_REGEX), "default"


def resolve_author(cli_author: str | None, token: str) -> str:
    raw = os.environ.get("IYNX_STATS_AUTHOR")
    if cli_author and cli_author.strip():
        return cli_author.strip()
    if raw and str(raw).strip():
        return str(raw).strip()
    return fetch_authenticated_login(token)


@dataclass
class Counts:
    total: int = 0
    merged: int = 0
    open: int = 0
    closed_unmerged: int = 0


@dataclass
class StatsResult:
    author: str
    label: str | None
    branch_pattern: str
    branch_pattern_source: str
    counts: Counts
    by_repo: dict[str, Counts]
    limits: dict[str, Any]


def compute_stats(
    *,
    token: str,
    label: str | None,
    branch_re: re.Pattern[str],
    branch_pattern_source: str,
    author: str,
    max_items: int | None,
) -> StatsResult:
    open_items, open_total, open_fetched, _ = paginate_search_issues(
        _build_search_q("open", author, label),
        token=token,
        budget=max_items,
        already_fetched=0,
    )

    closed_items, closed_total, closed_fetched, _ = paginate_search_issues(
        _build_search_q("closed", author, label),
        token=token,
        budget=max_items,
        already_fetched=open_fetched,
    )
    fetched_total = open_fetched + closed_fetched

    search_total_count = open_total + closed_total
    search_truncated = (open_total > 1000) or (closed_total > 1000)

    max_fetchable = min(open_total, 1000) + min(closed_total, 1000)
    user_capped = (
        max_items is not None
        and fetched_total >= max_items
        and fetched_total < max_fetchable
    )

    kept: list[tuple[str, dict[str, Any]]] = []
    seen: set[tuple[str, str, int]] = set()
    skipped_no_repo = 0
    skipped_branch_mismatch = 0

    for it in open_items + closed_items:
        parsed = _repo_from_issue_item(it)
        if not parsed:
            skipped_no_repo += 1
            continue
        owner, name = parsed
        num = int(it["number"])
        key = (owner, name, num)
        if key in seen:
            continue
        seen.add(key)
        pr = fetch_pull(owner, name, num, token=token)
        head = pr.get("head") or {}
        ref = head.get("ref")
        if not ref or not isinstance(ref, str):
            continue
        if not branch_re.search(ref):
            skipped_branch_mismatch += 1
            continue
        kept.append((f"{owner}/{name}", pr))

    by_repo: dict[str, Counts] = defaultdict(Counts)
    totals = Counts()

    for full_name, pr in kept:
        merged_at = pr.get("merged_at")
        state = pr.get("state")
        c = by_repo[full_name]
        totals.total += 1
        c.total += 1
        if merged_at:
            totals.merged += 1
            c.merged += 1
        elif state == "open":
            totals.open += 1
            c.open += 1
        else:
            totals.closed_unmerged += 1
            c.closed_unmerged += 1

    return StatsResult(
        author=author,
        label=label,
        branch_pattern=branch_re.pattern,
        branch_pattern_source=branch_pattern_source,
        counts=totals,
        by_repo=dict(by_repo),
        limits={
            "search_total_count": search_total_count,
            "search_open_total": open_total,
            "search_closed_total": closed_total,
            "search_items_fetched": fetched_total,
            "search_truncated": search_truncated,
            "user_capped": user_capped,
            "skipped_no_repo": skipped_no_repo,
            "skipped_branch_mismatch": skipped_branch_mismatch,
            "label_filter": label is not None,
        },
    )


def _use_color(no_color_flag: bool) -> bool:
    if no_color_flag:
        return False
    if os.environ.get("NO_COLOR"):
        return False
    return sys.stdout.isatty()


def _ansi(code: str) -> str:
    return f"\033[{code}m"


def render_table(result: StatsResult, *, use_color: bool) -> str:
    c = result.counts
    lab = result.label if result.label is not None else "(no label filter)"
    lines = [
        f"author={result.author} label={lab} branch_source={result.branch_pattern_source}",
        "",
        f"{'merged':>8} {'open':>8} {'closed_u':>8} {'total':>8}",
        f"{c.merged:>8} {c.open:>8} {c.closed_unmerged:>8} {c.total:>8}",
    ]
    if use_color:
        out = []
        for i, line in enumerate(lines):
            if i == 2:
                out.append(_ansi("1;36") + line + _ansi("0"))
            else:
                out.append(line)
        return "\n".join(out)
    return "\n".join(lines)


def _trunc(s: str, max_len: int) -> str:
    if len(s) <= max_len:
        return s
    if max_len <= 1:
        return "…"
    return s[: max_len - 1] + "…"


def render_card(result: StatsResult, *, use_color: bool, width: int | None = None) -> str:
    """Box-drawn card; width follows terminal (or ~56) so stats are not truncated."""
    try:
        term_cols = shutil.get_terminal_size().columns
    except OSError:
        term_cols = 80
    if width is None:
        width = min(max(term_cols, 52), 72)
    inner_w = max(width - 4, 44)
    c = result.counts
    title = "iynx · PR stats"
    lab_disp = result.label if result.label is not None else "(none — author+branch only)"
    label_line = _trunc(f"label: {lab_disp}", inner_w)
    branch_line = _trunc(
        f"branch: {result.branch_pattern} ({result.branch_pattern_source})",
        inner_w,
    )
    hdr = f"{'merged':>7}  {'open':>6}  {'unmerged':>8}  {'total':>6}"
    row = f"{c.merged:>7}  {c.open:>6}  {c.closed_unmerged:>8}  {c.total:>6}"
    lines = [title, label_line, branch_line, "", hdr, row]
    top = "╭" + "─" * inner_w + "╮"
    bot = "╰" + "─" * inner_w + "╯"
    body = []
    for line in lines:
        pad = inner_w - len(line)
        body.append("│" + line + " " * max(pad, 0) + "│")
    card = "\n".join([top] + body + [bot])
    if use_color:
        parts = card.split("\n")
        parts[0] = _ansi("1;36") + parts[0] + _ansi("0")
        return "\n".join(parts)
    return card


def result_to_json(result: StatsResult) -> dict[str, Any]:
    out: dict[str, Any] = {
        "schema_version": 1,
        "author": result.author,
        "label": result.label,
        "branch_pattern": result.branch_pattern,
        "branch_pattern_source": result.branch_pattern_source,
        "counts": {
            "total": result.counts.total,
            "merged": result.counts.merged,
            "open": result.counts.open,
            "closed_unmerged": result.counts.closed_unmerged,
        },
        "limits": result.limits,
    }
    if result.by_repo:
        out["by_repo"] = {
            k: {
                "total": v.total,
                "merged": v.merged,
                "open": v.open,
                "closed_unmerged": v.closed_unmerged,
            }
            for k, v in sorted(result.by_repo.items())
        }
    return out


def _emit_diagnostics(result: StatsResult, *, verbose: bool) -> None:
    lim = result.limits
    if verbose:
        print(
            f"[stats] author={result.author!r} label={result.label!r} "
            f"branch_regex={result.branch_pattern!r} ({result.branch_pattern_source})",
            file=sys.stderr,
        )
        print(f"[stats] limits={json.dumps(lim, ensure_ascii=False)}", file=sys.stderr)
    if result.counts.total > 0:
        return
    st = int(lim.get("search_total_count") or 0)
    sbm = int(lim.get("skipped_branch_mismatch") or 0)
    snr = int(lim.get("skipped_no_repo") or 0)
    if st == 0:
        if result.label is None:
            print(
                "hint: GitHub search found no PRs for this author (no label filter). "
                f"Confirm the token user ({result.author}) opened the PRs. "
                "Use --author LOGIN if needed.",
                file=sys.stderr,
            )
        else:
            print(
                "hint: GitHub search found no PRs for this author + label. "
                "Confirm each PR has the label "
                f"{result.label!r} (exact spelling), and that the token user "
                f"({result.author}) is the PR author. "
                "For PRs without labels, try: python stats.py --no-label\n"
                "Use --author LOGIN if needed.",
                file=sys.stderr,
            )
    elif sbm > 0:
        if result.label is None:
            print(
                "hint: PRs matched author, but none matched the head-branch regex "
                f"{result.branch_pattern!r}. "
                'Try: python stats.py --no-label --branch-regex ".*"',
                file=sys.stderr,
            )
        else:
            print(
                "hint: PRs matched author + label, but none matched the head-branch regex "
                f"{result.branch_pattern!r}. "
                "To count by label only (any branch), run:\n"
                f'  python stats.py --branch-regex ".*" --label {result.label!r}',
                file=sys.stderr,
            )
    elif snr > 0:
        print(
            f"hint: {snr} search hits had no usable repository URL (unexpected). "
            "Try again with --verbose and report an issue.",
            file=sys.stderr,
        )


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="GitHub PR statistics (optional label + branch pattern).",
    )
    p.add_argument(
        "--format",
        choices=("json", "table", "card", "share"),
        default="card",
        help="Output format (card and share are the same).",
    )
    p.add_argument("--no-color", action="store_true", help="Disable ANSI colors.")
    lg = p.add_mutually_exclusive_group()
    lg.add_argument(
        "--label",
        default=None,
        metavar="NAME",
        help="Label filter (overrides env).",
    )
    lg.add_argument(
        "--no-label",
        action="store_true",
        help="Do not filter by label in GitHub search (author + branch regex only). "
        "Use for PRs that have fix branches but no label.",
    )
    p.add_argument("--branch-regex", default=None, help="Head branch regex (overrides env).")
    p.add_argument("--author", default=None, help="PR author login (overrides env / API).")
    p.add_argument(
        "--max",
        type=int,
        default=None,
        metavar="N",
        help="Max search items to fetch (open then closed).",
    )
    p.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="Print search diagnostics to stderr (counts, skips, hints).",
    )
    return p.parse_args(argv)


def run(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    token = os.environ.get("GITHUB_TOKEN")
    if not token or not str(token).strip():
        print("GITHUB_TOKEN is required.", file=sys.stderr)
        return 1
    try:
        no_lab = bool(args.no_label) or no_label_from_env()
        label = resolve_label(args.label, no_label=no_lab)
        branch_re, branch_src = resolve_branch_regex(args.branch_regex)
        author = resolve_author(args.author, str(token).strip())
    except ValueError as e:
        print(str(e), file=sys.stderr)
        return 1

    try:
        result = compute_stats(
            token=str(token).strip(),
            label=label,
            branch_re=branch_re,
            branch_pattern_source=branch_src,
            author=author,
            max_items=args.max,
        )
    except requests.HTTPError as e:
        print(f"GitHub API error: {e}", file=sys.stderr)
        return 2
    except requests.RequestException as e:
        print(f"Network error: {e}", file=sys.stderr)
        return 2
    except RuntimeError as e:
        print(str(e), file=sys.stderr)
        return 2

    lim = result.limits
    if lim.get("search_truncated"):
        print(
            "warning: GitHub Search returned more than 1,000 matches; "
            "counts may omit older PRs.",
            file=sys.stderr,
        )
    if lim.get("user_capped"):
        print(
            "info: stopped early due to --max before exhausting matching search results.",
            file=sys.stderr,
        )

    _emit_diagnostics(result, verbose=args.verbose)

    use_color = _use_color(args.no_color)
    fmt = args.format
    if fmt == "json":
        print(json.dumps(result_to_json(result), indent=2, ensure_ascii=False))
    elif fmt == "table":
        print(render_table(result, use_color=use_color))
    else:
        print(render_card(result, use_color=use_color))
    return 0


def main() -> None:
    logging.basicConfig(level=logging.WARNING)
    raise SystemExit(run())


if __name__ == "__main__":
    main()
