#!/usr/bin/env python3
"""The pr-review-agent gate (issue #103, ADR 0055): the single writer of the
`pr-review-agent` check run.

Reads a PR's changed files, reviews, and issue comments; computes a
deterministic gate decision (declared-scope hard-fail, then the reviewer
agent's `pr-review-verdict:` marker); creates the check run via the Checks
API; and appends a decision record to a JSONL log (fail-open - logging
never blocks the check). The merge stays human: the gate only ever hard-fails
a declared-scope diff or a reviewer FAIL that no OWNER/MEMBER has cleared.

Stdlib-only (like scripts/stack_kick.py). Env-driven; exits 2 with a message
on missing env, 1 on API failure, 0 otherwise - the check STATE carries the
signal, so a legit failure verdict is still exit 0.

Modes (argv[1]):
    gate         PR events and /gate-override comments
    merge-group  merge_group runs: every PR in the group already passed the
                 gate at its own head; the group run verifies CI only

Env:
    REPO               owner/name (both modes)
    GITHUB_TOKEN       token with checks:write (both modes)
    PR_NUMBER          PR number (gate)
    HEAD_SHA           check-run target; gate: absent -> resolved from the
                       PR fetch; required when PR_REVIEW_GATE=disabled (all
                       reads are skipped); merge-group: required
    DRAFT              'true'/'false' (gate; absent -> from the PR fetch)
    PR_AUTHOR          PR author login (gate; absent -> from the PR fetch)
    EVENT_NAME         recorded in the log record
    VEGGIES_REVIEW_LOG decision-log path (default LOG_DEFAULT)
    PR_REVIEW_GATE     'disabled' -> skip all reads, report success, log
                       reasons ['disabled'], exit 0
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path

CONTEXT = "pr-review-agent"
VERDICT_RE = re.compile(r"^\s*pr-review-verdict:\s*(pass|fail)\s*$",
                        re.IGNORECASE | re.MULTILINE)
# Declared-scope roots: a changed path matching any of these voids the agent
# verdict (ADR 0055). Match = path equals the entry, starts with an entry
# ending in "/", or basename == "CODEOWNERS".
SCOPE_ROOTS = (
    "secrets/",
    ".github/workflows/",
    "terraform/",
    "agent-config/",
    "scripts/",
    "ansible/roles/egress/",
    "AGENTS.md",
    "CLAUDE.md",
    "veggies.yml",
    "cli/permission_envelope.py",
)
# Verdict markers are accepted only from reviews by these associations
# (the repo is public - anyone can submit a comment review).
TRUSTED_VERDICT_ASSOCIATIONS = frozenset({"OWNER", "MEMBER", "COLLABORATOR"})
# Human clearing acts (approval or /gate-override comment) only from these.
HUMAN_ASSOCIATIONS = frozenset({"OWNER", "MEMBER"})
OVERRIDE_PREFIX = "/gate-override"
LOG_DEFAULT = "/home/stacks/.local/state/veggies/veggie/pr-review-verdicts.jsonl"
TIMEOUT = 60
MAX_PAGES = 30  # files/reviews/comments pagination cap (100/page)

# The remediation line ending every red summary.
REMEDIATION = ("Cleared by a human APPROVED review on the current head, or "
               "an OWNER/MEMBER comment starting with /gate-override.")


def scope_hits(paths: list[str]) -> list[str]:
    """Sorted, de-duplicated list of CHANGED PATHS that fall in declared
    scope (not the matched roots). A path matches when it equals a SCOPE_ROOTS
    entry, starts with an entry that ends in '/', or its basename is
    'CODEOWNERS'."""
    hits = set()
    for path in paths:
        if os.path.basename(path) == "CODEOWNERS":
            hits.add(path)
            continue
        for root in SCOPE_ROOTS:
            if path == root or (root.endswith("/") and path.startswith(root)):
                hits.add(path)
                break
    return sorted(hits)


def latest_verdict(reviews: list[dict],
                   head_sha: str) -> tuple[str, str, str] | None:
    """(verdict, submitted_at, html_url) of the LATEST review carrying a
    VERDICT_RE marker line, or None. Only reviews that: are pinned to
    head_sha (review['commit_id'] == head_sha), have
    review['author_association'] in TRUSTED_VERDICT_ASSOCIATIONS, and have a
    body matching VERDICT_RE (group 1 lowercased). Latest = max by
    submitted_at (ISO 8601 strings compare correctly)."""
    candidates = []
    for review in reviews:
        if review.get("commit_id") != head_sha:
            continue
        if review.get("author_association") not in TRUSTED_VERDICT_ASSOCIATIONS:
            continue
        m = VERDICT_RE.search(review.get("body") or "")
        if m:
            candidates.append((m.group(1).lower(),
                               review.get("submitted_at") or "",
                               review.get("html_url") or ""))
    if not candidates:
        return None
    return max(candidates, key=lambda c: c[1])


def _iso_to_epoch(iso: str) -> float:
    """ISO 8601 ('...Z') -> epoch seconds."""
    return datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp()


def _clearing_act(reviews: list[dict], comments: list[dict],
                  head_sha: str, pr_author: str) -> tuple[float, str] | None:
    """(epoch_seconds, actor_login) of the newest human clearing act, or
    None. Two kinds:
    - an APPROVED review (review['state'] == 'APPROVED') pinned to head_sha,
      association in HUMAN_ASSOCIATIONS, and reviewer login != pr_author
      (GitHub rejects self-approvals; belt and braces);
    - an issue comment whose body starts with OVERRIDE_PREFIX and whose
      author_association is in HUMAN_ASSOCIATIONS."""
    acts: list[tuple[float, str]] = []
    for review in reviews:
        if review.get("state") != "APPROVED":
            continue
        if review.get("commit_id") != head_sha:
            continue
        if review.get("author_association") not in HUMAN_ASSOCIATIONS:
            continue
        login = (review.get("user") or {}).get("login") or ""
        if login == pr_author:
            continue
        acts.append((_iso_to_epoch(review["submitted_at"]), login))
    for comment in comments:
        if not (comment.get("body") or "").startswith(OVERRIDE_PREFIX):
            continue
        if comment.get("author_association") not in HUMAN_ASSOCIATIONS:
            continue
        login = (comment.get("user") or {}).get("login") or ""
        acts.append((_iso_to_epoch(comment["created_at"]), login))
    if not acts:
        return None
    return max(acts, key=lambda a: a[0])


def clearing_act_ts(reviews: list[dict], comments: list[dict],
                    head_sha: str, pr_author: str) -> float | None:
    """Epoch seconds of the newest human clearing act, or None (see
    _clearing_act for what counts)."""
    act = _clearing_act(reviews, comments, head_sha, pr_author)
    return act[0] if act else None


def matched_roots(paths: list[str]) -> list[str]:
    """Sorted SCOPE_ROOTS entries (plus 'CODEOWNERS') that the given changed
    paths matched - the scope paths' provenance, for summaries."""
    roots = set()
    for path in paths:
        if os.path.basename(path) == "CODEOWNERS":
            roots.add("CODEOWNERS")
        for root in SCOPE_ROOTS:
            if path == root or (root.endswith("/") and path.startswith(root)):
                roots.add(root)
    return sorted(roots)


def _evaluate(draft: bool, scope: list[str],
              verdict: tuple[str, str, str] | None,
              human_ts: float | None, head_commit_ts: float
              ) -> tuple[str, str | None, str, str, list[str], str | None]:
    """The gate decision plus its audit trail:
    (status, conclusion, title, summary, reasons, resolution). Order
    matters - draft first, then the declared-scope hard-fail, then the
    reviewer verdict; a human clearing act only rescues a RED state, never
    the no-verdict pending one."""
    if draft:
        return ("completed", "success", f"{CONTEXT}: draft",
                "The gate evaluates at the ready-for-review transition.",
                ["draft"], None)
    if scope:
        roots = ", ".join(f"`{r}`" for r in matched_roots(scope))
        cleared = human_ts is not None and human_ts >= head_commit_ts
        if cleared:
            return ("completed", "success", f"{CONTEXT}: human override",
                    "This PR touches the declared human-review scope "
                    f"(roots: {roots}), and a human cleared the "
                    "declared-scope diff at or after the head commit.",
                    ["declared-scope", "human-override"], "human-override")
        listing = "\n".join(f"- {p}" for p in scope)
        return ("completed", "failure", f"{CONTEXT}: declared-scope diff",
                "A reviewer agent may not clear this PR: it touches the "
                "declared human-review scope (ADR 0055).\n\n"
                f"Changed paths in declared scope:\n{listing}\n\n"
                f"Matched scope roots: {roots}\n\n"
                f"{REMEDIATION}",
                ["declared-scope"], None)
    if verdict and verdict[0] == "fail":
        _, submitted_at, url = verdict
        cleared = human_ts is not None and human_ts >= _iso_to_epoch(submitted_at)
        if cleared:
            return ("completed", "success", f"{CONTEXT}: human override",
                    "A human overrode the reviewer agent's fail verdict "
                    f"({url}) at or after the verdict was posted.",
                    ["verdict-fail", "human-override"], "human-override")
        return ("completed", "failure", f"{CONTEXT}: reviewer verdict: fail",
                f"The reviewer agent returned a FAIL verdict: {url}\n\n"
                f"{REMEDIATION}",
                ["verdict-fail"], None)
    if verdict and verdict[0] == "pass":
        return ("completed", "success", f"{CONTEXT}: reviewer verdict: pass",
                f"The reviewer agent passed this PR: {verdict[2]}",
                ["verdict-pass"], None)
    return ("in_progress", None, f"{CONTEXT}: awaiting reviewer verdict",
            "The reviewer posts its verdict as a review carrying a "
            "pr-review-verdict line (issue #102).",
            ["awaiting-verdict"], None)


def decide(draft: bool, scope: list[str],
           verdict: tuple[str, str, str] | None,
           human_ts: float | None,
           head_commit_ts: float) -> tuple[str, str | None, str, str]:
    """The whole state machine. Returns (status, conclusion, title, summary)
    for the check run: status is 'completed' or 'in_progress'; conclusion is
    'success'/'failure'/None. title is one short line; summary is the
    check-run output body (markdown, may be multi-line)."""
    status, conclusion, title, summary, _, _ = _evaluate(
        draft, scope, verdict, human_ts, head_commit_ts)
    return status, conclusion, title, summary


def decision_record(repo: str, pr: int | None, head_sha: str,
                    event: str | None, state: str, reasons: list[str],
                    verdict: str | None = None,
                    verdict_review_url: str | None = None,
                    human_actor: str | None = None,
                    resolution: str | None = None,
                    ts: float | None = None) -> dict:
    """One ADR 0055 decision-log record. state is 'success'|'failure'|
    'pending' (the check-run conclusion, 'pending' while the verdict is
    awaited); reasons carries the state-machine branch tags ('declared-
    scope', 'verdict-fail', 'human-override', 'draft', 'disabled',
    'merge-group', 'awaiting-verdict', ...)."""
    return {
        "ts": time.time() if ts is None else ts,
        "repo": repo,
        "pr": pr,
        "head_sha": head_sha,
        "event": event,
        "state": state,
        "reasons": list(reasons),
        "verdict": verdict,
        "verdict_review_url": verdict_review_url,
        "human_actor": human_actor,
        "resolution": resolution,
    }


def append_log(path: str, record: dict) -> None:
    """Append one JSON line. FAIL-OPEN: any OSError prints a loud warning to
    stderr and returns - logging never blocks the check. Creates the parent
    dir. Path comes from env VEGGIES_REVIEW_LOG or LOG_DEFAULT."""
    try:
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        with open(p, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, sort_keys=True) + "\n")
    except OSError as e:
        print(f"WARNING: pr-review gate decision log append failed ({e}); "
              "the check result stands", file=sys.stderr)


def gh_api(token: str, method: str, path: str,
           body: dict | None = None) -> object:
    """GitHub REST over urllib (Bearer token). GET or POST. Raises on
    HTTP/transport error."""
    req = urllib.request.Request(
        f"https://api.github.com{path}", method=method,
        data=json.dumps(body).encode() if body is not None else None,
        headers={"Authorization": f"Bearer {token}",
                 "Accept": "application/vnd.github+json"})
    if body is not None:
        req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
        raw = resp.read()
    return json.loads(raw) if raw.strip() else {}


def gh_paginated(token: str, path: str) -> list:
    """GET path with per_page=100&page=N until a short page or MAX_PAGES.
    If the MAX_PAGES-th page comes back full, raise RuntimeError - a diff
    too big to scan must never silently pass the scope guard."""
    sep = "&" if "?" in path else "?"
    items: list = []
    for page in range(1, MAX_PAGES + 1):
        batch = gh_api(token, "GET",
                       f"{path}{sep}per_page=100&page={page}")
        items.extend(batch)
        if len(batch) < 100:
            return items
    raise RuntimeError(
        f"pagination overflow: {path} still full after {MAX_PAGES} pages "
        f"({MAX_PAGES * 100} items) - refusing to gate an unscannable diff")


def create_check_run(token: str, repo: str, head_sha: str, status: str,
                     conclusion: str | None, title: str,
                     summary: str) -> dict:
    """POST /repos/{repo}/check-runs with {name: CONTEXT, head_sha, status,
    output: {title, summary}} plus conclusion when status == 'completed'."""
    body: dict = {"name": CONTEXT, "head_sha": head_sha, "status": status,
                  "output": {"title": title, "summary": summary}}
    if status == "completed":
        body["conclusion"] = conclusion
    return gh_api(token, "POST", f"/repos/{repo}/check-runs", body)


def _require_env(names: list[str]) -> bool:
    """True when every named env var is set; else print and False."""
    missing = [k for k in names if not os.environ.get(k)]
    if missing:
        print(f"missing env: {', '.join(missing)}", file=sys.stderr)
        return False
    return True


def _report(token: str, repo: str, head_sha: str, event: str | None,
            log_path: str, pr: int | None, status: str,
            conclusion: str | None, title: str, summary: str,
            reasons: list[str], verdict: tuple[str, str, str] | None = None,
            human_actor: str | None = None,
            resolution: str | None = None) -> int:
    """Create the check run, append the decision log, print one summary
    line. The exit code carries the plumbing (0 = ran), never the verdict -
    the check STATE is the signal."""
    try:
        create_check_run(token, repo, head_sha, status, conclusion,
                         title, summary)
    except (urllib.error.URLError, RuntimeError, TimeoutError,
            json.JSONDecodeError) as e:
        print(f"pr-review gate: check-run create failed: {e}",
              file=sys.stderr)
        return 1
    state = "pending" if status == "in_progress" else conclusion
    append_log(log_path, decision_record(
        repo, pr, head_sha, event, state, reasons,
        verdict=verdict[0] if verdict else None,
        verdict_review_url=verdict[2] if verdict else None,
        human_actor=human_actor, resolution=resolution))
    print(f"{title} [{state}]")
    return 0


def main_gate() -> int:
    """PR events and /gate-override comments: read the PR's files, reviews,
    and issue comments; decide; report."""
    if not _require_env(["REPO", "PR_NUMBER", "GITHUB_TOKEN"]):
        return 2
    repo = os.environ["REPO"]
    try:
        pr = int(os.environ["PR_NUMBER"])
    except ValueError:
        print(f"bad env: PR_NUMBER={os.environ['PR_NUMBER']!r} is not an "
              "integer", file=sys.stderr)
        return 2
    token = os.environ["GITHUB_TOKEN"]
    head_sha = os.environ.get("HEAD_SHA")
    event = os.environ.get("EVENT_NAME")
    log_path = os.environ.get("VEGGIES_REVIEW_LOG", LOG_DEFAULT)
    # Kill switch: report green and leave - no reads, so HEAD_SHA must come
    # from the env.
    if os.environ.get("PR_REVIEW_GATE") == "disabled":
        if not head_sha:
            print("missing env: HEAD_SHA (required when "
                  "PR_REVIEW_GATE=disabled)", file=sys.stderr)
            return 2
        return _report(token, repo, head_sha, event, log_path, pr,
                       "completed", "success", f"{CONTEXT}: disabled",
                       "The gate is disabled by the PR_REVIEW_GATE "
                       "repository variable.", ["disabled"])
    draft_env = os.environ.get("DRAFT", "").lower()
    draft: bool | None = {"true": True, "false": False}.get(draft_env)
    pr_author = os.environ.get("PR_AUTHOR")
    try:
        if head_sha is None or draft is None or pr_author is None:
            pr_data = gh_api(token, "GET", f"/repos/{repo}/pulls/{pr}")
            head_sha = head_sha or pr_data["head"]["sha"]
            if draft is None:
                draft = bool(pr_data.get("draft"))
            pr_author = (pr_author or (pr_data.get("user") or {})
                         .get("login") or "")
        files = gh_paginated(token, f"/repos/{repo}/pulls/{pr}/files")
        reviews = gh_paginated(token, f"/repos/{repo}/pulls/{pr}/reviews")
        comments = gh_paginated(token, f"/repos/{repo}/issues/{pr}/comments")
        scope = scope_hits([f.get("filename", "") for f in files])
        verdict = latest_verdict(reviews, head_sha)
        act = _clearing_act(reviews, comments, head_sha, pr_author)
        human_ts = act[0] if act else None
        human_actor = act[1] if act else None
        # The PR payload does not carry the head commit date - the 'signal
        # time' a clearing act must postdate for scope-red (per the ADR: the
        # human's act must be newer than the thing it clears). Fetched only
        # when a scope hit makes it load-bearing.
        head_commit_ts = 0.0
        if scope:
            commit = gh_api(token, "GET",
                            f"/repos/{repo}/commits/{head_sha}")
            head_commit_ts = _iso_to_epoch(
                commit["commit"]["committer"]["date"])
        status, conclusion, title, summary, reasons, resolution = _evaluate(
            draft, scope, verdict, human_ts, head_commit_ts)
    except (urllib.error.URLError, RuntimeError, TimeoutError,
            json.JSONDecodeError, KeyError) as e:
        print(f"pr-review gate failed: {e}", file=sys.stderr)
        return 1
    return _report(token, repo, head_sha, event, log_path, pr,
                   status, conclusion, title, summary, reasons,
                   verdict=verdict, human_actor=human_actor,
                   resolution=resolution)


def main_merge_group() -> int:
    """merge_group runs: each PR in the group already passed the gate at
    its own head; report green for the group (ADR 0053's always-report
    invariant)."""
    if not _require_env(["REPO", "HEAD_SHA", "GITHUB_TOKEN"]):
        return 2
    return _report(os.environ["GITHUB_TOKEN"], os.environ["REPO"],
                   os.environ["HEAD_SHA"], os.environ.get("EVENT_NAME"),
                   os.environ.get("VEGGIES_REVIEW_LOG", LOG_DEFAULT), None,
                   "completed", "success", f"{CONTEXT}: gated at PR head",
                   "Each PR in the group passed the gate at its own head; "
                   "the group run verifies CI only.", ["merge-group"])


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if not args or args[0] not in ("gate", "merge-group"):
        print("usage: pr_review_gate.py {gate|merge-group}", file=sys.stderr)
        return 2
    if args[0] == "gate":
        return main_gate()
    return main_merge_group()


if __name__ == "__main__":
    sys.exit(main())
