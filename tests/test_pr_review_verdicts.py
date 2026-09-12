"""Tests for scripts/pr_review_verdicts.py - the pull-based harvester that
materializes the GitHub review history (the system of record) into
pr-review-verdicts.jsonl next to the spend log (issue #103, ADR 0055
decision 7). Idempotent by review_id; the gate workflow never writes the
log (the runner container is ephemeral)."""

import importlib.util
import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

_spec = importlib.util.spec_from_file_location(
    "pr_review_verdicts",
    Path(__file__).parent.parent / "scripts/pr_review_verdicts.py")
harv = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(harv)

ROOT = Path(__file__).resolve().parents[1]

HEAD40 = "0123456789abcdef0123456789abcdef01234567"
T0 = "2026-09-12T09:00:00Z"
T1 = "2026-09-12T10:00:00Z"
T2 = "2026-09-12T11:00:00Z"
E1 = datetime(2026, 9, 12, 10, 0, 0, tzinfo=timezone.utc).timestamp()

# The pinned ADR 0055 schema: the 11 original keys plus review_id.
LOG_KEYS = {"ts", "repo", "pr", "head_sha", "event", "state", "reasons",
            "verdict", "verdict_review_url", "human_actor", "resolution",
            "review_id"}

HARV_ENV = ("REPO", "GITHUB_TOKEN", "VEGGIES_REVIEW_LOG", "HARVEST_MAX_PRS")


def _env(monkeypatch, tmp_path, extra=None):
    for k in HARV_ENV:
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("REPO", "o/r")
    monkeypatch.setenv("GITHUB_TOKEN", "tok")
    monkeypatch.setenv("VEGGIES_REVIEW_LOG", str(tmp_path / "log.jsonl"))
    for k, v in (extra or {}).items():
        monkeypatch.setenv(k, v)
    return tmp_path / "log.jsonl"


def _pr(number, login="alice", head=HEAD40):
    return {"number": number, "user": {"login": login},
            "head": {"sha": head}}


def _verdict(verdict, rid, commit_id=HEAD40, association="OWNER",
             submitted_at=T1, state="COMMENTED", login="reviewer-bot"):
    return {"id": rid, "body": f"looks fine\n\npr-review-verdict: {verdict}",
            "commit_id": commit_id, "author_association": association,
            "submitted_at": submitted_at,
            "html_url": f"https://x/review/{rid}", "state": state,
            "user": {"login": login}}


def _approval(commit_id=HEAD40, association="OWNER", login="bob",
              submitted_at=T2):
    return {"id": 9000, "state": "APPROVED", "commit_id": commit_id,
            "author_association": association, "submitted_at": submitted_at,
            "user": {"login": login}, "body": "ship it"}


def _comment(body, association="MEMBER", login="carol", created_at=T2):
    return {"body": body, "author_association": association,
            "created_at": created_at, "user": {"login": login}}


def _fake_api(prs, seen_gets):
    def fake(token, method, path, body=None):
        seen_gets.append(path)
        assert path == ("/repos/o/r/pulls?state=all&sort=updated&"
                        "direction=desc&per_page=100")
        assert method == "GET"
        return prs
    return fake


def _fake_paginated(reviews_by_pr, comments_by_pr, fetched):
    """reviews/comments fakes keyed by PR number; comments_by_pr=None makes
    any issue-comments fetch blow up (proving it never happens)."""
    def fake(token, path):
        for kind, table in (("pulls", reviews_by_pr),
                            ("issues", comments_by_pr)):
            prefix = f"/repos/o/r/{kind}/"
            if path.startswith(prefix):
                number = int(path[len(prefix):].split("/")[0])
                assert path.split("/")[-1].startswith(
                    "reviews" if kind == "pulls" else "comments")
                if table is None:
                    raise AssertionError(f"must not fetch {path}")
                fetched.append((kind, number))
                return table.get(number, [])
        raise AssertionError(f"unexpected path {path}")
    return fake


def _wire(monkeypatch, prs, reviews_by_pr, comments_by_pr):
    gets, fetched = [], []
    monkeypatch.setattr(harv.pr_review_gate, "gh_api",
                        _fake_api(prs, gets))
    monkeypatch.setattr(harv.pr_review_gate, "gh_paginated",
                        _fake_paginated(reviews_by_pr, comments_by_pr,
                                        fetched))
    return gets, fetched


def _read(log):
    return [json.loads(line) for line in log.read_text().splitlines()]


# --- records ---------------------------------------------------------------

def test_harvest_records_the_12_key_schema(monkeypatch, tmp_path):
    log = _env(monkeypatch, tmp_path)
    _wire(monkeypatch, [_pr(12)], {12: [_verdict("pass", 5001)]}, {12: []})
    assert harv.main() == 0
    (rec,) = _read(log)
    assert set(rec) == LOG_KEYS
    assert rec == {"ts": E1, "repo": "o/r", "pr": 12, "head_sha": HEAD40,
                   "event": "harvest", "state": "verdict",
                   "reasons": ["verdict-pass"], "verdict": "pass",
                   "verdict_review_url": "https://x/review/5001",
                   "human_actor": None, "resolution": None,
                   "review_id": 5001}


def test_harvest_records_every_verdict_review_not_just_the_latest(
        monkeypatch, tmp_path):
    log = _env(monkeypatch, tmp_path)
    reviews = [_verdict("fail", 5001), _verdict("pass", 5002,
                                                submitted_at=T2)]
    _wire(monkeypatch, [_pr(12)], {12: reviews}, {12: []})
    assert harv.main() == 0
    recs = _read(log)
    assert [r["review_id"] for r in recs] == [5001, 5002]
    assert [r["verdict"] for r in recs] == ["fail", "pass"]
    assert recs[0]["reasons"] == ["verdict-fail"]


def test_harvest_excludes_dismissed_verdicts(monkeypatch, tmp_path):
    log = _env(monkeypatch, tmp_path)
    reviews = [_verdict("fail", 5001, state="DISMISSED"),
               _verdict("fail", 5002, state="dismissed"),
               _verdict("pass", 5003)]
    _wire(monkeypatch, [_pr(12)], {12: reviews}, {12: []})
    assert harv.main() == 0
    assert [r["review_id"] for r in _read(log)] == [5003]


def test_harvest_excludes_untrusted_associations(monkeypatch, tmp_path):
    log = _env(monkeypatch, tmp_path)
    reviews = [_verdict("pass", 5001, association="NONE"),
               _verdict("pass", 5002, association="CONTRIBUTOR"),
               _verdict("pass", 5003, association="COLLABORATOR")]
    _wire(monkeypatch, [_pr(12)], {12: reviews}, {12: []})
    assert harv.main() == 0
    assert [r["review_id"] for r in _read(log)] == [5003]


def test_harvest_resolution_only_when_a_human_act_postdates_the_verdict(
        monkeypatch, tmp_path):
    log = _env(monkeypatch, tmp_path)
    prs = [_pr(12), _pr(13), _pr(14), _pr(15)]
    reviews = {
        # approval on the verdict's head, posted AFTER the verdict
        12: [_verdict("fail", 5001), _approval(submitted_at=T2)],
        # approval posted BEFORE the verdict - not a resolution
        13: [_verdict("fail", 5002, submitted_at=T2),
             _approval(submitted_at=T1)],
        # sha-bound /gate-override comment after the verdict
        14: [_verdict("fail", 5003)],
        # an override naming a DIFFERENT sha resolves nothing
        15: [_verdict("fail", 5004)],
    }
    comments = {
        14: [_comment(f"/gate-override {HEAD40}", created_at=T2)],
        15: [_comment(f"/gate-override {'f' * 40}", created_at=T2)],
    }
    _wire(monkeypatch, prs, reviews, comments)
    assert harv.main() == 0
    by_id = {r["review_id"]: r for r in _read(log)}
    assert by_id[5001]["resolution"] == "human-override"
    assert by_id[5002]["resolution"] is None
    assert by_id[5003]["resolution"] == "human-override"
    assert by_id[5004]["resolution"] is None
    # harvest records never carry a human_actor (the act's who stays in
    # the GitHub history; the record keeps the resolution only)
    assert all(r["human_actor"] is None for r in by_id.values())


def test_harvest_fetches_comments_only_when_a_verdict_exists(
        monkeypatch, tmp_path):
    _env(monkeypatch, tmp_path)
    # no verdict review on PR 12 -> the comments fetch must not happen
    _wire(monkeypatch, [_pr(12)], {12: [_approval()]}, None)
    assert harv.main() == 0


def test_harvest_scans_at_most_harvest_max_prs(monkeypatch, tmp_path, capsys):
    _env(monkeypatch, tmp_path, {"HARVEST_MAX_PRS": "2"})
    prs = [_pr(12), _pr(13), _pr(14)]
    _, fetched = _wire(monkeypatch, prs, {12: [_verdict("pass", 5001)]},
                       {12: []})
    assert harv.main() == 0
    # the list is sliced to the cap: PR 14 is never even read
    assert [n for kind, n in fetched] == [12, 12, 13]
    assert "scanned 2" in capsys.readouterr().out


# --- idempotency and the existing log --------------------------------------

def test_harvest_dedupes_by_review_id_across_runs(monkeypatch, tmp_path,
                                                  capsys):
    log = _env(monkeypatch, tmp_path)
    _wire(monkeypatch, [_pr(12)], {12: [_verdict("pass", 5001)]}, {12: []})
    assert harv.main() == 0
    assert harv.main() == 0  # second run: nothing new
    assert len(_read(log)) == 1
    assert "appended 0" in capsys.readouterr().out


def test_harvest_tolerates_a_missing_log(monkeypatch, tmp_path):
    log = tmp_path / "nested" / "dir" / "log.jsonl"  # nothing exists yet
    _env(monkeypatch, tmp_path)
    monkeypatch.setenv("VEGGIES_REVIEW_LOG", str(log))
    _wire(monkeypatch, [_pr(12)], {12: [_verdict("pass", 5001)]}, {12: []})
    assert harv.main() == 0
    assert len(_read(log)) == 1


def test_harvest_skips_malformed_lines_and_still_dedupes(monkeypatch,
                                                         tmp_path, capsys):
    log = _env(monkeypatch, tmp_path)
    log.write_text(
        "not json at all\n"
        '{"review_id": 5001, "state": "verdict"}\n'
        '{"broken": \n'
        '{"no_review_id": true}\n')
    _wire(monkeypatch, [_pr(12)],
          {12: [_verdict("pass", 5001), _verdict("fail", 5002)]},
          {12: []})
    assert harv.main() == 0
    lines = log.read_text().splitlines()
    assert len(lines) == 5  # 4 seeded + exactly 1 appended (5002)
    new = json.loads(lines[-1])
    assert set(new) == LOG_KEYS and new["review_id"] == 5002
    assert "skipped" in capsys.readouterr().err  # counted, never silent


def test_harvest_log_write_failure_is_loud_but_exit_0(monkeypatch, tmp_path,
                                                      capsys):
    blocker = tmp_path / "afile"
    blocker.write_text("not a dir")
    _env(monkeypatch, tmp_path)
    monkeypatch.setenv("VEGGIES_REVIEW_LOG", str(blocker / "log.jsonl"))
    _wire(monkeypatch, [_pr(12)], {12: [_verdict("pass", 5001)]}, {12: []})
    assert harv.main() == 0  # fail-open: GitHub stays the system of record
    assert capsys.readouterr().err


# --- env posture and API failure -------------------------------------------

def test_missing_env_is_exit_2(monkeypatch, tmp_path, capsys):
    for k in HARV_ENV:
        monkeypatch.delenv(k, raising=False)
    assert harv.main() == 2
    assert "missing env" in capsys.readouterr().err


def test_harvest_max_prs_validated(monkeypatch, tmp_path, capsys):
    for bad in ("101", "0", "-3", "fifty"):
        _env(monkeypatch, tmp_path, {"HARVEST_MAX_PRS": bad})
        assert harv.main() == 2, bad
        assert "HARVEST_MAX_PRS" in capsys.readouterr().err


def test_api_failure_is_exit_1(monkeypatch, tmp_path, capsys):
    _env(monkeypatch, tmp_path)

    def boom(token, method, path, body=None):
        raise harv.pr_review_gate.urllib.error.URLError("api down")

    monkeypatch.setattr(harv.pr_review_gate, "gh_api", boom)
    assert harv.main() == 1
    assert "api down" in capsys.readouterr().err


def test_runs_standalone_and_exits_2_without_env():
    # the sibling import of pr_review_gate must work when the script runs
    # as `python3 scripts/pr_review_verdicts.py` (its own dir on sys.path
    # is NOT relied on - the import is by explicit path).
    env = {k: v for k, v in os.environ.items() if k not in HARV_ENV}
    proc = subprocess.run(
        [sys.executable, str(ROOT / "scripts/pr_review_verdicts.py")],
        capture_output=True, text=True, env=env, cwd=ROOT)
    assert proc.returncode == 2
    assert "missing env" in proc.stderr
