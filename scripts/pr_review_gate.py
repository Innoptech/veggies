#!/usr/bin/env python3
"""The pr-review-agent gate (issue #103, ADR 0055): the single writer of the
`pr-review-agent` check run.

Reads a PR's changed files, reviews, and issue comments; computes a
deterministic gate decision (declared-scope hard-fail, then the reviewer
agent's `pr-review-verdict:` marker); and creates the check run via the
Checks API. The merge stays human: the gate only ever hard-fails a
declared-scope diff or a reviewer FAIL that no OWNER/MEMBER has cleared.
Fail CLOSED means a red check, never an absent one: any evaluation error
after the head sha is known still reports a `failure` check run.

This script does NOT write the decision log: the self-hosted runner
container is ephemeral and mounts only its `_work` dir, so a workflow-side
append would vanish silently. The GitHub review history is the system of
record; scripts/pr_review_verdicts.py harvests it into
pr-review-verdicts.jsonl (ADR 0055 decision 7). decision_record/append_log
stay here because the harvester imports them.

Stdlib-only (like scripts/stack_kick.py). Env-driven; exits 2 with a message
on missing env, 1 on API/evaluation failure (with the red check reported
whenever the head sha is known), 0 otherwise - the check STATE carries the
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
    PR_REVIEW_GATE     'disabled' -> skip all reads, report success, exit 0
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
# The override command is HEAD-BOUND: `/gate-override <full-head-sha>`. The
# sha (case-insensitive) must equal the head being gated, so one genuine
# override can never pre-clear a later head. Trailing prose is fine.
OVERRIDE_RE = re.compile(r"^/gate-override\s+([0-9a-fA-F]{40})(?:\s|$)")
LOG_DEFAULT = "/home/stacks/.local/state/veggies/veggie/pr-review-verdicts.jsonl"
TIMEOUT = 60
MAX_PAGES = 30  # files/reviews/comments pagination cap (100/page)


def remediation(head_sha: str) -> str:
    """The remediation line ending every red summary. The summary is the
    UI, so it carries the exact paste-able override command for THIS
    head."""
    return ("Cleared by a human APPROVED review on the current head, or an "
            f"OWNER/MEMBER comment: /gate-override {head_sha}")


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
    review['author_association'] in TRUSTED_VERDICT_ASSOCIATIONS, are not
    DISMISSED (a dismissed verdict stops counting), and have a body
    matching VERDICT_RE (group 1 lowercased). Latest = max by
    (submitted_at, id): ISO 8601 strings compare correctly, and the review
    id breaks same-second ties (higher id = newer; missing id = 0)."""
    candidates = []
    for review in reviews:
        if (review.get("state") or "").upper() == "DISMISSED":
            continue
        if review.get("commit_id") != head_sha:
            continue
        if review.get("author_association") not in TRUSTED_VERDICT_ASSOCIATIONS:
            continue
        m = VERDICT_RE.search(review.get("body") or "")
        if m:
            candidates.append((m.group(1).lower(),
                               review.get("submitted_at") or "",
                               review.get("html_url") or "",
                               review.get("id") or 0))
    if not candidates:
        return None
    verdict, submitted_at, url, _ = max(candidates,
                                        key=lambda c: (c[1], c[3]))
    return verdict, submitted_at, url


def _iso_to_epoch(iso: str) -> float:
    """ISO 8601 ('...Z') -> epoch seconds."""
    return datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp()


def _override_acts(comments: list[dict],
                   head_sha: str) -> list[tuple[float, str]]:
    """(epoch_ts, actor_login) per /gate-override comment naming THIS head
    sha, from an OWNER/MEMBER. A comment naming any other sha (or no sha)
    is not a clearing act."""
    acts = []
    for comment in comments:
        m = OVERRIDE_RE.match(comment.get("body") or "")
        if not m or m.group(1).lower() != head_sha.lower():
            continue
        if comment.get("author_association") not in HUMAN_ASSOCIATIONS:
            continue
        login = (comment.get("user") or {}).get("login") or ""
        acts.append((_iso_to_epoch(comment["created_at"]), login))
    return acts


def human_acts(reviews: list[dict], comments: list[dict],
               head_sha: str,
               pr_author: str) -> list[tuple[float, str]]:
    """Every qualifying human clearing act on THIS head, as
    (epoch_seconds, actor_login) pairs. Two kinds:
    - an APPROVED review (review['state'] == 'APPROVED') pinned to
      head_sha, association in HUMAN_ASSOCIATIONS, reviewer login !=
      pr_author (GitHub rejects self-approvals; belt and braces), and NOT
      carrying a verdict marker - a verdict never clears itself;
    - a sha-bound /gate-override comment (see _override_acts)."""
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
        if VERDICT_RE.search(review.get("body") or ""):
            continue
        acts.append((_iso_to_epoch(review["submitted_at"]), login))
    acts.extend(_override_acts(comments, head_sha))
    return acts


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


def decide(draft: bool, scope: list[str],
           verdict: tuple[str, str, str] | None,
           human_acts: list[tuple[float, str]],
           head_sha: str) -> tuple[str, str | None, str, str]:
    """The whole state machine. Returns (status, conclusion, title, summary)
    for the check run: status is 'completed' or 'in_progress'; conclusion is
    'success'/'failure'/None. title is one short line; summary is the
    check-run output body (markdown, may be multi-line).

    BOTH red lanes are evaluated on every call: scope (any act on this
    head clears - head-binding IS the postdating) and verdict-fail (only
    an act at or after the verdict's submitted_at clears). A human act
    alone never rescues the no-verdict pending state."""
    if draft:
        return ("completed", "success", f"{CONTEXT}: draft",
                "The gate evaluates at the ready-for-review transition.")
    reds = []
    if scope and not human_acts:
        reds.append("declared-scope")
    if verdict and verdict[0] == "fail":
        verdict_ts = _iso_to_epoch(verdict[1])
        if not any(ts >= verdict_ts for ts, _ in human_acts):
            reds.append("verdict-fail")
    if reds:
        title = (f"{CONTEXT}: declared-scope diff"
                 if reds[0] == "declared-scope"
                 else f"{CONTEXT}: reviewer verdict: fail")
        parts = []
        if "declared-scope" in reds:
            roots = ", ".join(f"`{r}`" for r in matched_roots(scope))
            listing = "\n".join(f"- {p}" for p in scope)
            parts.append(
                "A reviewer agent may not clear this PR: it touches the "
                "declared human-review scope (ADR 0055).\n\n"
                f"Changed paths in declared scope:\n{listing}\n\n"
                f"Matched scope roots: {roots}")
        if "verdict-fail" in reds:
            parts.append("The reviewer agent returned a FAIL verdict: "
                         f"{verdict[2]}")
        return ("completed", "failure", title,
                "\n\n".join(parts) + "\n\n" + remediation(head_sha))
    cleared = []
    if scope:
        roots = ", ".join(f"`{r}`" for r in matched_roots(scope))
        cleared.append(f"the declared-scope diff (roots: {roots})")
    if verdict and verdict[0] == "fail":
        cleared.append(f"the reviewer fail verdict ({verdict[2]})")
    if cleared and human_acts:
        actors = ", ".join(sorted({actor for _, actor in human_acts}))
        return ("completed", "success", f"{CONTEXT}: human override",
                "A human cleared " + " and ".join(cleared) +
                f". Clearing act(s) on this head by: {actors}.")
    if verdict and verdict[0] == "pass":
        return ("completed", "success", f"{CONTEXT}: reviewer verdict: pass",
                f"The reviewer agent passed this PR: {verdict[2]}")
    return ("in_progress", None, f"{CONTEXT}: awaiting reviewer verdict",
            "The reviewer posts its verdict as a review carrying a "
            "pr-review-verdict line (issue #102).")


def decision_record(repo: str, pr: int | None, head_sha: str,
                    event: str | None, state: str, reasons: list[str],
                    verdict: str | None = None,
                    verdict_review_url: str | None = None,
                    human_actor: str | None = None,
                    resolution: str | None = None,
                    review_id: int | None = None,
                    ts: float | None = None) -> dict:
    """One ADR 0055 decision-log record (12 keys, pinned by pytest).
    Written only by the harvester (scripts/pr_review_verdicts.py): state is
    'verdict', event 'harvest', reasons ['verdict-pass'|'verdict-fail'],
    review_id the GitHub review id the record was harvested from (the
    idempotency key), ts the review's submitted_at epoch."""
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
        "review_id": review_id,
    }


def append_log(path: str, record: dict) -> None:
    """Append one JSON line. FAIL-OPEN: any OSError prints a loud warning to
    stderr and returns - logging never blocks the caller. Creates the
    parent dir. Path comes from env VEGGIES_REVIEW_LOG or LOG_DEFAULT."""
    try:
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        with open(p, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, sort_keys=True) + "\n")
    except OSError as e:
        print(f"WARNING: pr-review verdict log append failed ({e}); "
              "the GitHub review history remains the system of record",
              file=sys.stderr)


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


def _report(token: str, repo: str, head_sha: str, status: str,
            conclusion: str | None, title: str, summary: str) -> int:
    """Create the check run and print one summary line. The exit code
    carries the plumbing (0 = ran), never the verdict - the check STATE is
    the signal."""
    try:
        create_check_run(token, repo, head_sha, status, conclusion,
                         title, summary)
    except (urllib.error.URLError, RuntimeError, TimeoutError,
            json.JSONDecodeError) as e:
        print(f"pr-review gate: check-run create failed: {e}",
              file=sys.stderr)
        return 1
    state = "pending" if status == "in_progress" else conclusion
    print(f"{title} [{state}]")
    return 0


def _report_gate_error(token: str, repo: str, head_sha: str,
                       error: Exception) -> int:
    """Fail CLOSED: the required context must report a NAMED red check,
    never go absent. The summary names the exception CLASS only - the
    message can carry untrusted API text."""
    summary = (f"The gate could not evaluate this head "
               f"({type(error).__name__}). Re-run the workflow, or clear "
               f"with a human APPROVED review / /gate-override {head_sha}.")
    try:
        create_check_run(token, repo, head_sha, "completed", "failure",
                         f"{CONTEXT}: gate error", summary)
    except Exception as e2:
        print(f"pr-review gate failed ({error}) and reporting the "
              f"gate-error check also failed: {e2}", file=sys.stderr)
        return 1
    print(f"pr-review gate failed: {error} (reported a failing "
          f"{CONTEXT} check)", file=sys.stderr)
    return 1


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
    # Kill switch: report green and leave - no reads, so HEAD_SHA must come
    # from the env.
    if os.environ.get("PR_REVIEW_GATE") == "disabled":
        if not head_sha:
            print("missing env: HEAD_SHA (required when "
                  "PR_REVIEW_GATE=disabled)", file=sys.stderr)
            return 2
        return _report(token, repo, head_sha, "completed", "success",
                       f"{CONTEXT}: disabled",
                       "The gate is disabled by the PR_REVIEW_GATE "
                       "repository variable.")
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
        # Reviews and comments BEFORE files: on a >3000-file diff the files
        # read overflows, and a sha-bound override comment is the escape -
        # it must be knowable before the scan is attempted.
        reviews = gh_paginated(token, f"/repos/{repo}/pulls/{pr}/reviews")
        comments = gh_paginated(token, f"/repos/{repo}/issues/{pr}/comments")
        try:
            files = gh_paginated(token, f"/repos/{repo}/pulls/{pr}/files")
        except RuntimeError:
            overrides = _override_acts(comments, head_sha)
            if overrides:
                actors = ", ".join(sorted({a for _, a in overrides}))
                return _report(token, repo, head_sha, "completed",
                               "success", f"{CONTEXT}: human override",
                               "The diff is too large to scan (>3000 "
                               "files); a human took explicit "
                               "responsibility for this exact head. "
                               f"Override by: {actors}.")
            raise
        # A rename counts on BOTH names: out of scope (previous_filename)
        # and into scope (filename) both hit the sentinel.
        paths = []
        for f in files:
            paths.append(f.get("filename", ""))
            if f.get("previous_filename"):
                paths.append(f["previous_filename"])
        scope = scope_hits(paths)
        verdict = latest_verdict(reviews, head_sha)
        acts = human_acts(reviews, comments, head_sha, pr_author)
        status, conclusion, title, summary = decide(
            draft, scope, verdict, acts, head_sha)
    except Exception as e:
        if not head_sha:
            # nothing to report a check against
            print(f"pr-review gate failed before the head sha was known: "
                  f"{e}", file=sys.stderr)
            return 1
        return _report_gate_error(token, repo, head_sha, e)
    return _report(token, repo, head_sha, status, conclusion, title,
                   summary)


def main_merge_group() -> int:
    """merge_group runs: each PR in the group already passed the gate at
    its own head; report green for the group (ADR 0053's always-report
    invariant)."""
    if not _require_env(["REPO", "HEAD_SHA", "GITHUB_TOKEN"]):
        return 2
    return _report(os.environ["GITHUB_TOKEN"], os.environ["REPO"],
                   os.environ["HEAD_SHA"],
                   "completed", "success", f"{CONTEXT}: gated at PR head",
                   "Each PR in the group passed the gate at its own head; "
                   "the group run verifies CI only.")


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
