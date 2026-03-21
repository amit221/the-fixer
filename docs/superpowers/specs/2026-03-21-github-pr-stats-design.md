# GitHub PR statistics & shareable console card

## Summary

Add a **GitHub-only** statistics path that counts pull requests **you** opened that match a **label** and a **head-branch** pattern, across **all repositories**. Provide multiple output formats, including a **card** (alias **share**) view optimized for **screenshots** and social sharing. **Orchestrator** must apply the same label when creating PRs so search results stay aligned with the workflow.

Local `.iynx-run-progress.jsonl` aggregates are **out of scope** for this feature.

---

## Goals

1. **Classify** matching PRs into: **merged**, **open**, **closed without merge** (and derived **total**).
2. **Identify** PRs with **both**:
   - A configurable **label** (applied at `gh pr create` time).
   - A configurable **head ref regex** (default matches existing branch naming `fix/issue-{n}`).
3. **Scope:** GitHub-wide for the authenticated user (same universe as “all my PRs,” then filtered).
4. **UX:** Machine-readable **JSON**, plain **table**, and a **card** / **share** terminal view (aliases; same renderer) with optional ANSI color and `--no-color`.

## Non-goals

- Run counts, phase breakdowns, or any reporting from local JSONL.
- Web UI, scheduled reports, or persistence of stats.
- GitHub Actions or CI minute statistics.
- GraphQL API (REST Search + REST PR details is enough for v1; revisit if rate limits bite).

---

## Configuration

| Variable | Required | Purpose |
|----------|----------|---------|
| `GITHUB_TOKEN` | Yes for stats CLI | Same token pattern as the rest of the project (`Bearer` to GitHub API). Use a token with **`repo`** scope if you need PRs in **private** repositories; otherwise search may omit them. |
| `IYNX_PR_LABEL` | Yes for **creating** labeled PRs from orchestrator | Passed to `gh pr create --label`. Document in README and `.env.example`. |
| `IYNX_STATS_LABEL` | Optional on stats CLI | Override label used when **querying** (defaults to `IYNX_PR_LABEL` if set, else must be passed via CLI or error). |
| `IYNX_STATS_BRANCH_REGEX` | Optional | Python regex for head ref; default `^fix/issue-\d+$`. |
| `IYNX_STATS_AUTHOR` | Optional | GitHub login to filter as PR author; default = **authenticated user** from `GET /user`. |

**Consistency rule:** If `IYNX_STATS_LABEL` is unset, use `IYNX_PR_LABEL`. If neither env var is set, **`--label` on the CLI** satisfies the label requirement. If **no** label is available from env or CLI, the stats command must **fail fast** with a clear message (no silent empty results).

**Label matching:** GitHub search uses the label **name** as in the UI; matching is **exact** for the `label:` qualifier (case and spelling must match the label applied at PR creation).

---

## Orchestrator change

**File:** `src/orchestrator.py` (PR creation script inside Docker).

- Read `IYNX_PR_LABEL` from the host environment and pass it into the container env (alongside existing `GH_TOKEN` / `GITHUB_TOKEN`).
- Append to `gh pr create`: `--label <value>` (shell-quote safely).
- If `IYNX_PR_LABEL` is **unset** or **empty**: **omit** `--label` (preserve today’s behavior for users who do not opt in). Document that **stats filtering by label** requires setting the variable for **new** PRs going forward; old PRs without the label will not appear in label-based stats.

---

## Stats CLI

**Suggested entry:** new module e.g. `src/pr_stats.py` and a thin runner `python -m pr_stats` or `stats.py` at repo root — follow whatever pattern exists after implementation planning (single entry point, documented in README).

### Flags

| Flag | Behavior |
|------|----------|
| `--format json` | See **JSON output contract** below. |
| `--format table` | Plain text rows/columns. |
| `--format card` \| `--format share` | Same Unicode “dashboard” renderer (aliases). |
| `--no-color` | Disable ANSI in `card`/`share`/`table` as applicable. |
| `--label` | Override label (else env chain above). |
| `--branch-regex` | Override branch regex. |
| `--author` | Override author login. |
| `--max` | Optional cap on **search items fetched** (stop pagination early). Sets `limits.user_capped` when the cap bites before the fetchable set is exhausted. Default: no cap. |

**Exit codes:** `0` success; `1` configuration or usage error; `2` GitHub API error after retries (optional distinction; document).

### JSON output contract (`--format json`)

Stable fields (semver: additive only until v2):

```json
{
  "schema_version": 1,
  "author": "octocat",
  "label": "iynx-fix",
  "branch_pattern_source": "default",
  "counts": {
    "total": 0,
    "merged": 0,
    "open": 0,
    "closed_unmerged": 0
  },
  "by_repo": {
    "owner/name": { "total": 0, "merged": 0, "open": 0, "closed_unmerged": 0 }
  },
  "limits": {
    "search_total_count": 0,
    "search_items_fetched": 0,
    "search_truncated": false,
    "user_capped": false
  }
}
```

- **`branch_pattern_source`:** Where the branch regex came from: **`default`** (built-in `^fix/issue-\d+$`), **`env`** (`IYNX_STATS_BRANCH_REGEX`), or **`cli`** (`--branch-regex`).
- **`by_repo`:** Omit or `{}` when empty; include when non-empty.
- **`limits.search_total_count`:** The **`total_count`** field from the first Search API response (total matches for the query on GitHub, may exceed what can be retrieved).
- **`limits.search_items_fetched`:** Number of **search result items actually retrieved** from the API across all pages (each item is a PR candidate **before** head-ref regex filtering). Maximum **1,000** (GitHub’s per-query retrieval cap), or fewer if **`--max`** stops pagination early.
- **`limits.search_truncated`:** Set `true` **iff** `search_total_count` **>** `1,000` (GitHub will not return more than 1,000 items; counts may omit older PRs). Set `false` when `search_total_count` ≤ 1,000 (all matching search hits were fetchable). Emit a **warning** on stderr for all formats when `true`; document in README.
- **`limits.user_capped`:** Set `true` **iff** pagination stopped because **`--max`** was reached before exhausting fetchable search items. Emit an **informational** message on stderr (distinct from the GitHub truncation warning). When `false`, either all fetchable items were read or GitHub returned fewer than `--max` items total.

---

## GitHub API strategy

1. **Resolve author login:** `GET /user` unless `--author` / `IYNX_STATS_AUTHOR` is set.
2. **Search:** `GET /search/issues` with query  
   `is:pr author:<login> label:<label>`  
   **States:** Results must include **open and closed** PRs so merged / open / closed-unmerged counts are possible. **Spike in implementation:** confirm whether the default search scope is open-only; if so, extend `q` (e.g. `is:open OR is:closed` with `is:pr`) or run **two** searches (`is:pr is:open …` and `is:pr is:closed …`) and **merge/deduplicate** by PR identity before filtering.  
   Use explicit **`sort`** and **`order`** query parameters (e.g. `sort=updated`, `order=desc`) so pagination order is stable across requests; document the chosen values in the implementation plan.  
   Paginate (`per_page` 100, follow `Link` header until done or `--max`).

   **GitHub Search hard cap:** At most **1,000** **items** can be **retrieved** per query, even if `total_count` is higher. Store `search_total_count` from the API and set `search_truncated` to **true** when `total_count` **>** 1,000. When `total_count` is **≤** 1,000, all matching search items are retrievable (`search_truncated` is **false**). Paginate until all fetchable items are read (or `--max` stops early). Apply head-ref regex **after** fetching. v1 does **not** require sharding queries (e.g. by date or repo); document as a known limitation and optional future `--since` or split strategies.
3. **Enrich (head ref):** For each search item, if the payload already includes the **head branch name** needed for regex matching (see GitHub REST “Issues” search item shape: `pull_request` URL and/or repository linkage), use it. **Otherwise** call **`GET /repos/{owner}/{repo}/pulls/{pull_number}`** once for that item. **Decision rule:** After a one-time spike against live API responses in the plan, lock “field present → no GET; else GET” in code comments and tests. Requirement: **correct head ref** for filtering.
4. **Filter:** Keep items whose **head ref** `ref` (branch name) matches `IYNX_STATS_BRANCH_REGEX`.
5. **Bucket:**
   - `merged` — `merged_at` is non-null.
   - `open` — `state == open`.
   - `closed` (unmerged) — `state == closed` and `merged_at` is null.

**Rate limits:** Search API has low quotas. Implement **pagination** only in v1. On **rate limiting**, follow GitHub’s signals: prefer **`429`** with `Retry-After` when present; handle **`403`** with abuse/rate-limit secondary limits per [GitHub docs](https://docs.github.com/en/rest/using-the-rest-api/rate-limits-for-the-rest-api). Sleep and retry with bounded attempts. Document that very large histories may need a **future** `--since` date filter (YAGNI for v1 unless review flags it).

---

## Card / share output

- **Stdlib only:** Unicode box-drawing + optional ANSI; no new required dependencies.
- **Default width:** Target ~**40–56** columns for screenshot readability; optional auto-detect terminal width.
- **Content:** Title (e.g. `iynx · PR stats`), filter one-liner (label + branch pattern), large aligned counts for merged / open / closed / total.
- **Color:** Emit ANSI colors when stdout is a TTY unless disabled. **`--no-color`** or a non-empty **`NO_COLOR`** environment variable disables ANSI (including for `table` where color is used). Use this for CI and golden snapshot tests.

---

## Testing

- **Unit tests** with mocked `requests`: search response pages, user endpoint, PR detail if needed; **regex filter** and **state bucketing** covered in isolation.
- **Snapshot or golden string** for `card` output with **ANSI stripped** or fixed `NO_COLOR=1` for stability.
- **Integration** tests optional (no live token in CI); document manual smoke: run CLI against real token with a test label.

---

## Documentation

- **README:** New section “PR statistics”; env vars; examples for `json` and `card`.
- **`.env.example`:** Comment lines for `IYNX_PR_LABEL`, optional stats overrides.

---

## Open points for implementation plan only

- Exact package/module layout and console script name (`python -m …`).
- Spike: confirm which fields on `/search/issues` items carry head ref; lock the enrich rule from section 3 accordingly.
