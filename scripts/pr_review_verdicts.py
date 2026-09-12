#!/usr/bin/env python3
"""Harvest pr-review verdict reviews into the decision log (issue #103,
ADR 0055 decision 7).

The GitHub review history is the system of record. This pull-based
harvester materializes it into pr-review-verdicts.jsonl next to the spend
log (0051's neighbor), so decision 9's disagreement evidence exists even
though GitHub stays authoritative - nothing can be lost, and a fresh log
simply backfills. The gate workflow does NOT write the log: the
self-hosted runner container is ephemeral and mounts only its `_work` dir
(verified against the gh-runner quadlet), so a workflow-side append would
vanish silently at job end.

One record per non-DISMISSED, trusted-association review carrying a
`pr-review-verdict:` marker (the full history, not just the latest per
head). Idempotent by review_id: existing log lines are read first
(missing file tolerated; malformed lines skipped and counted, the 0051
reader policy) and already-harvested reviews are never re-appended.

Stdlib-only. The gh helpers and decision_record come from the sibling
pr_review_gate, imported by explicit path (never sys.path search - the
stack_kick.py permission_envelope pattern, but mandatory: the harvester
is nothing without the gate module).

Env:
    REPO               owner/name (required)
    GITHUB_TOKEN       token with pull-requests/issues read (required)
    VEGGIES_REVIEW_LOG log path (default pr_review_gate.LOG_DEFAULT)
    HARVEST_MAX_PRS    most recently updated PRs to scan (default 50;
                       1..100 - the PR list is a single 100-item page)

Exit: 2 missing/invalid env, 1 API failure, 0 otherwise (log writes fail
open - a loud stderr warning, exit 0).
"""

from __future__ import annotations

import importlib.util
import json
import os
import sys
import urllib.error
from pathlib import Path

_gate_spec = importlib.util.spec_from_file_location(
    "pr_review_gate",
    Path(__file__).resolve().parent / "pr_review_gate.py")
pr_review_gate = importlib.util.module_from_spec(_gate_spec)
_gate_spec.loader.exec_module(pr_review_gate)

MAX_PRS_CAP = 100  # the PR list is fetched as one 100-item page


def _read_seen_ids(path: str) -> set[int]:
    """review_ids already logged. A missing file tolerates to empty;
    malformed/blank lines (and records without a usable review_id) are
    skipped and counted - the 0051 reader policy: never shrink silently.
    Any other read OSError is fail-open like the append: a loud stderr
    warning and an empty set (logging never blocks the harvest)."""
    seen: set[int] = set()
    skipped = 0
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    skipped += 1
                    continue
                rid = obj.get("review_id") if isinstance(obj, dict) else None
                if isinstance(rid, int) and not isinstance(rid, bool):
                    seen.add(rid)
                else:
                    skipped += 1
    except FileNotFoundError:
        pass
    except OSError as e:
        print(f"WARNING: pr-review-verdicts: cannot read {path} ({e}); "
              "treating the log as empty", file=sys.stderr)
    if skipped:
        print(f"pr-review-verdicts: skipped {skipped} malformed line(s) "
              f"in {path}", file=sys.stderr)
    return seen


def _is_verdict_review(review: dict) -> bool:
    """A harvestable verdict: non-DISMISSED, trusted association, and a
    marker line. NOT head-pinned - the log keeps the full history."""
    if (review.get("state") or "").upper() == "DISMISSED":
        return False
    if (review.get("author_association")
            not in pr_review_gate.TRUSTED_VERDICT_ASSOCIATIONS):
        return False
    return bool(pr_review_gate.VERDICT_RE.search(review.get("body") or ""))


def harvest(repo: str, token: str, log_path: str,
            max_prs: int) -> tuple[int, int]:
    """Scan the max_prs most recently updated PRs; append one record per
    new verdict review. Returns (scanned_prs, appended_records)."""
    prs = pr_review_gate.gh_api(
        token, "GET", f"/repos/{repo}/pulls?state=all&sort=updated&"
                      "direction=desc&per_page=100")[:max_prs]
    seen = _read_seen_ids(log_path)
    new_records = []
    for pr in prs:
        number = pr["number"]
        pr_author = (pr.get("user") or {}).get("login") or ""
        reviews = pr_review_gate.gh_paginated(
            token, f"/repos/{repo}/pulls/{number}/reviews")
        verdicts = [r for r in reviews if _is_verdict_review(r)]
        if not verdicts:
            continue
        # comments are fetched only once a verdict review exists
        comments = pr_review_gate.gh_paginated(
            token, f"/repos/{repo}/issues/{number}/comments")
        for review in verdicts:
            rid = review.get("id")
            if rid in seen:
                continue
            verdict = pr_review_gate.VERDICT_RE.search(
                review["body"]).group(1).lower()
            head_sha = review.get("commit_id")
            verdict_ts = pr_review_gate._iso_to_epoch(review["submitted_at"])
            acts = pr_review_gate.human_acts(reviews, comments, head_sha,
                                             pr_author)
            resolution = ("human-override"
                          if any(ts >= verdict_ts for ts, _ in acts)
                          else None)
            new_records.append(pr_review_gate.decision_record(
                repo, number, head_sha, "harvest", "verdict",
                [f"verdict-{verdict}"], verdict=verdict,
                verdict_review_url=review.get("html_url"), human_actor=None,
                resolution=resolution, review_id=rid, ts=verdict_ts))
    for rec in new_records:  # append_log fails open (loud stderr, no raise)
        pr_review_gate.append_log(log_path, rec)
    return len(prs), len(new_records)


def main() -> int:
    if not pr_review_gate._require_env(["REPO", "GITHUB_TOKEN"]):
        return 2
    raw_max = os.environ.get("HARVEST_MAX_PRS", "50")
    try:
        max_prs = int(raw_max)
    except ValueError:
        print(f"bad env: HARVEST_MAX_PRS={raw_max!r} is not an integer",
              file=sys.stderr)
        return 2
    if not 1 <= max_prs <= MAX_PRS_CAP:
        print(f"bad env: HARVEST_MAX_PRS={max_prs} outside "
              f"1..{MAX_PRS_CAP} (the PR list is a single "
              f"{MAX_PRS_CAP}-item page)", file=sys.stderr)
        return 2
    log_path = os.environ.get("VEGGIES_REVIEW_LOG",
                              pr_review_gate.LOG_DEFAULT)
    try:
        scanned, appended = harvest(os.environ["REPO"],
                                    os.environ["GITHUB_TOKEN"],
                                    log_path, max_prs)
    except (urllib.error.URLError, RuntimeError, TimeoutError,
            json.JSONDecodeError, KeyError) as e:
        print(f"pr-review-verdicts harvest failed: {e}", file=sys.stderr)
        return 1
    print(f"pr-review-verdicts: scanned {scanned} PR(s), appended "
          f"{appended} record(s) -> {log_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
