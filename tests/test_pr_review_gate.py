"""Tests for scripts/pr_review_gate.py - the deterministic core of the
pr-review-agent required check (issue #103, ADR 0055): declared-scope
hard-fail plus reviewer-verdict state machine, check-run writer, and the
fail-open decision log."""

import ast
import importlib.util
import json
import re
from datetime import datetime, timezone
from pathlib import Path

import pytest
import yaml

_spec = importlib.util.spec_from_file_location(
    "pr_review_gate",
    Path(__file__).parent.parent / "scripts/pr_review_gate.py")
gate = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(gate)


# --- scope_hits: the declared-scope guard (ADR 0055) ---------------------

def test_scope_hits_matches_every_declared_root():
    """Every SCOPE_ROOTS entry must actually bite: a file under each
    slash-root, and the file itself for the exact-path entries."""
    paths = [
        "secrets/prod.yml",
        ".github/workflows/ci.yml",
        "terraform/github/repo.tf",
        "agent-config/agents/cto.md",
        "scripts/stack_kick.py",
        "ansible/roles/egress/tasks/main.yml",
        "AGENTS.md",
        "CLAUDE.md",
        "veggies.yml",
        "cli/permission_envelope.py",
    ]
    assert gate.scope_hits(paths) == sorted(paths)


def test_scope_hits_codeowners_matches_at_any_depth():
    # CODEOWNERS is basename-matched: root, .github, or deeper all count.
    assert gate.scope_hits(["CODEOWNERS"]) == ["CODEOWNERS"]
    assert gate.scope_hits([".github/CODEOWNERS"]) == [".github/CODEOWNERS"]
    assert gate.scope_hits(["docs/CODEOWNERS"]) == ["docs/CODEOWNERS"]


def test_scope_hits_ignores_unscoped_paths():
    assert gate.scope_hits([
        "docs/runbook.md",
        "cli/veggies.py",          # only cli/permission_envelope.py is scoped
        "tests/x.py",
        "ansible/roles/other/tasks/main.yml",  # only the egress role is scoped
        "docs/AGENTS.md",          # exact-path roots match at the root only
    ]) == []


def test_scope_hits_dedupes_and_sorts():
    assert gate.scope_hits(["terraform/b.tf", "AGENTS.md",
                            "terraform/a.tf", "AGENTS.md"]) == \
        ["AGENTS.md", "terraform/a.tf", "terraform/b.tf"]


def test_scope_hits_empty():
    assert gate.scope_hits([]) == []


# --- latest_verdict: the reviewer agent's marker, latest head-pinned one --

HEAD = "abc123"


def _review(body, commit_id=HEAD, association="OWNER",
            submitted_at="2026-09-12T10:00:00Z", url="https://x/review/1",
            login="reviewer-bot"):
    return {"body": body, "commit_id": commit_id,
            "author_association": association, "submitted_at": submitted_at,
            "html_url": url, "user": {"login": login}, "state": "COMMENTED"}


def test_latest_verdict_parses_the_marker_line():
    v = gate.latest_verdict([_review("pr-review-verdict: pass")], HEAD)
    assert v == ("pass", "2026-09-12T10:00:00Z", "https://x/review/1")


def test_latest_verdict_marker_survives_surrounding_prose():
    body = "Looks good overall.\n\npr-review-verdict: pass\n\n(automated)"
    v = gate.latest_verdict([_review(body)], HEAD)
    assert v is not None and v[0] == "pass"


def test_latest_verdict_value_is_case_insensitive():
    v = gate.latest_verdict([_review("pr-review-verdict: FAIL")], HEAD)
    assert v is not None and v[0] == "fail"


def test_latest_verdict_requires_the_marker_line():
    for body in ("", "no verdict here", "pr-review-verdict: passed",
                 "the pr-review-verdict: pass is not line-anchored", None):
        assert gate.latest_verdict([_review(body)], HEAD) is None, body


def test_latest_verdict_must_be_pinned_to_head():
    stale = _review("pr-review-verdict: pass", commit_id="oldsha")
    assert gate.latest_verdict([stale], HEAD) is None


def test_latest_verdict_requires_a_trusted_association():
    # the repo is public: an outsider's comment review carries NONE.
    untrusted = _review("pr-review-verdict: pass", association="NONE")
    assert gate.latest_verdict([untrusted], HEAD) is None
    for assoc in ("OWNER", "MEMBER", "COLLABORATOR"):
        v = gate.latest_verdict([_review("pr-review-verdict: pass",
                                         association=assoc)], HEAD)
        assert v is not None, assoc


def test_latest_verdict_latest_submission_wins():
    older = _review("pr-review-verdict: fail", url="https://x/review/1",
                    submitted_at="2026-09-12T10:00:00Z")
    newer = _review("pr-review-verdict: pass", url="https://x/review/2",
                    submitted_at="2026-09-12T11:00:00Z")
    v = gate.latest_verdict([newer, older], HEAD)
    assert v == ("pass", "2026-09-12T11:00:00Z", "https://x/review/2")
    # order in the API page must not matter
    assert gate.latest_verdict([older, newer], HEAD) == v


# --- clearing_act_ts: the newest human clearing act ----------------------
#
# Expected epochs are built with the datetime constructor, an independent
# path from the implementation's fromisoformat.

T1 = "2026-09-12T10:00:00Z"
T2 = "2026-09-12T11:00:00Z"
E1 = datetime(2026, 9, 12, 10, 0, 0, tzinfo=timezone.utc).timestamp()
E2 = datetime(2026, 9, 12, 11, 0, 0, tzinfo=timezone.utc).timestamp()


def _approval(commit_id=HEAD, association="OWNER", login="bob",
              submitted_at=T1, state="APPROVED"):
    return {"state": state, "commit_id": commit_id,
            "author_association": association, "submitted_at": submitted_at,
            "user": {"login": login}, "html_url": "https://x/review/9"}


def _comment(body, association="MEMBER", login="carol", created_at=T2):
    return {"body": body, "author_association": association,
            "created_at": created_at, "user": {"login": login},
            "html_url": "https://x/comment/1"}


def test_clearing_act_ts_head_approval_by_owner_counts():
    ts = gate.clearing_act_ts([_approval()], [], HEAD, "alice")
    assert ts == E1


def test_clearing_act_ts_ignores_approval_on_an_old_sha():
    assert gate.clearing_act_ts([_approval(commit_id="oldsha")], [],
                                HEAD, "alice") is None


def test_clearing_act_ts_ignores_self_approval():
    # GitHub rejects self-approvals; belt and braces (a forged payload must
    # not clear the gate either).
    assert gate.clearing_act_ts([_approval(login="alice")], [],
                                HEAD, "alice") is None


def test_clearing_act_ts_ignores_contributor_approval():
    assert gate.clearing_act_ts([_approval(association="CONTRIBUTOR")], [],
                                HEAD, "alice") is None


def test_clearing_act_ts_ignores_non_approval_states():
    for state in ("COMMENTED", "CHANGES_REQUESTED", "DISMISSED"):
        assert gate.clearing_act_ts([_approval(state=state)], [],
                                    HEAD, "alice") is None, state


def test_clearing_act_ts_override_comment_by_member_counts():
    ts = gate.clearing_act_ts([], [_comment("/gate-override: I read the diff")],
                              HEAD, "alice")
    assert ts == E2


def test_clearing_act_ts_comment_must_start_with_the_prefix():
    for body in ("please /gate-override this",  # leading prose
                 "lgtm\n/gate-override"):        # mid-comment mention
        assert gate.clearing_act_ts([], [_comment(body)], HEAD,
                                    "alice") is None, body


def test_clearing_act_ts_override_comment_requires_a_human_association():
    c = _comment("/gate-override", association="CONTRIBUTOR")
    assert gate.clearing_act_ts([], [c], HEAD, "alice") is None


def test_clearing_act_ts_newest_act_wins():
    ts = gate.clearing_act_ts([_approval(submitted_at=T1)],
                              [_comment("/gate-override", created_at=T2)],
                              HEAD, "alice")
    assert ts == E2


def test_clearing_act_reports_the_newest_actor():
    """main() logs human_actor: the login behind the NEWEST clearing act."""
    act = gate._clearing_act([_approval(login="bob", submitted_at=T1)],
                             [_comment("/gate-override", login="carol",
                                       created_at=T2)],
                             HEAD, "alice")
    assert act == (E2, "carol")
    act = gate._clearing_act([_approval(login="bob", submitted_at=T2)],
                             [_comment("/gate-override", login="carol",
                                       created_at=T1)],
                             HEAD, "alice")
    assert act == (E2, "bob")


# --- decide: the whole state machine -------------------------------------

REMEDIATION = ("Cleared by a human APPROVED review on the current head, or "
               "an OWNER/MEMBER comment starting with /gate-override.")
FAIL_VERDICT = ("fail", T1, "https://x/review/1")
PASS_VERDICT = ("pass", T1, "https://x/review/1")


def test_decide_draft_short_circuits_everything():
    status, conclusion, title, summary = gate.decide(
        True, ["scripts/x.py"], FAIL_VERDICT, None, E1)
    assert (status, conclusion) == ("completed", "success")
    assert title == "pr-review-agent: draft"
    assert summary == "The gate evaluates at the ready-for-review transition."


def test_decide_scope_red_uncleared_fails_closed():
    scope = ["scripts/x.py", "AGENTS.md", "docs/CODEOWNERS"]
    status, conclusion, title, summary = gate.decide(
        False, scope, PASS_VERDICT, None, E1)
    assert (status, conclusion) == ("completed", "failure")
    assert title == "pr-review-agent: declared-scope diff"
    # every matched path, one per line (listed as a markdown bullet)
    lines = summary.splitlines()
    for path in scope:
        assert any(line.endswith(path) for line in lines), path
    # the matched roots are named (derived from the paths)
    for root in ("scripts/", "AGENTS.md", "CODEOWNERS"):
        assert root in summary
    assert summary.endswith(REMEDIATION)


def test_decide_scope_red_cleared_by_a_human():
    status, conclusion, title, summary = gate.decide(
        False, ["scripts/x.py"], None, E2, E1)
    assert (status, conclusion) == ("completed", "success")
    assert title == "pr-review-agent: human override"
    assert "scripts/" in summary  # the matched root is named
    assert "human" in summary.lower()


def test_decide_scope_clearing_must_postdate_the_head_commit():
    # the human's act must be newer than the thing it clears: equal clears
    # (>=), older does not.
    scope = ["scripts/x.py"]
    assert gate.decide(False, scope, None, E1, E1)[1] == "success"
    assert gate.decide(False, scope, None, E1 - 1, E1)[1] == "failure"


def test_decide_verdict_fail_uncleared():
    status, conclusion, title, summary = gate.decide(
        False, [], FAIL_VERDICT, None, E1)
    assert (status, conclusion) == ("completed", "failure")
    assert title == "pr-review-agent: reviewer verdict: fail"
    assert "https://x/review/1" in summary
    assert summary.endswith(REMEDIATION)


def test_decide_verdict_fail_cleared_only_when_newer_than_the_verdict():
    # E1 is the verdict's own timestamp; the act must postdate it (>=).
    assert gate.decide(False, [], FAIL_VERDICT, E1, 0.0)[1] == "success"
    status, conclusion, title, summary = gate.decide(
        False, [], FAIL_VERDICT, E1 - 1, 0.0)
    assert (status, conclusion) == ("completed", "failure")


def test_decide_verdict_fail_cleared_reports_the_override():
    status, conclusion, title, summary = gate.decide(
        False, [], FAIL_VERDICT, E2, E1)
    assert (status, conclusion) == ("completed", "success")
    assert title == "pr-review-agent: human override"
    assert "fail" in summary  # says the human overrode the fail verdict
    assert "https://x/review/1" in summary  # and links the review


def test_decide_verdict_pass():
    status, conclusion, title, summary = gate.decide(
        False, [], PASS_VERDICT, None, E1)
    assert (status, conclusion) == ("completed", "success")
    assert title == "pr-review-agent: reviewer verdict: pass"
    assert "https://x/review/1" in summary


def test_decide_no_verdict_stays_pending():
    status, conclusion, title, summary = gate.decide(
        False, [], None, None, E1)
    assert (status, conclusion) == ("in_progress", None)
    assert title == "pr-review-agent: awaiting reviewer verdict"
    assert summary == ("The reviewer posts its verdict as a review carrying "
                       "a pr-review-verdict line (issue #102).")


def test_decide_human_act_does_not_rescue_the_pending_state():
    # a /gate-override or approval clears a RED state, never substitutes
    # for the reviewer agent's verdict.
    status, conclusion, title, _ = gate.decide(False, [], None, E2, E1)
    assert (status, conclusion) == ("in_progress", None)
    assert title == "pr-review-agent: awaiting reviewer verdict"


# --- decision_record + append_log: the ADR 0055 audit trail --------------

# The log schema, key for key - the workflow task and the contract tests
# rely on exactly this set.
LOG_KEYS = {"ts", "repo", "pr", "head_sha", "event", "state", "reasons",
            "verdict", "verdict_review_url", "human_actor", "resolution"}


def test_decision_record_has_the_exact_schema_keys():
    rec = gate.decision_record("o/r", 12, HEAD, "pull_request", "success",
                               ["verdict-pass"], verdict="pass",
                               verdict_review_url="https://x/review/1",
                               human_actor=None, resolution=None, ts=E1)
    assert set(rec) == LOG_KEYS
    assert rec == {"ts": E1, "repo": "o/r", "pr": 12, "head_sha": HEAD,
                   "event": "pull_request", "state": "success",
                   "reasons": ["verdict-pass"], "verdict": "pass",
                   "verdict_review_url": "https://x/review/1",
                   "human_actor": None, "resolution": None}


def test_decision_record_defaults():
    rec = gate.decision_record("o/r", None, HEAD, "merge_group", "success",
                               ["merge-group"], ts=E1)
    assert set(rec) == LOG_KEYS
    assert rec["verdict"] is None and rec["verdict_review_url"] is None
    assert rec["human_actor"] is None and rec["resolution"] is None


def test_append_log_writes_one_parseable_json_line(tmp_path):
    log = tmp_path / "nested" / "dir" / "verdicts.jsonl"  # parent created
    rec = gate.decision_record("o/r", 12, HEAD, "pull_request", "failure",
                               ["declared-scope"], ts=E1)
    gate.append_log(str(log), rec)
    lines = log.read_text().splitlines()
    assert len(lines) == 1
    parsed = json.loads(lines[0])
    assert set(parsed) == LOG_KEYS and parsed["state"] == "failure"
    gate.append_log(str(log), rec)  # appends, never truncates
    assert len(log.read_text().splitlines()) == 2


def test_append_log_fails_open_on_an_unwritable_path(tmp_path, capsys):
    blocker = tmp_path / "afile"
    blocker.write_text("not a dir")
    rec = gate.decision_record("o/r", 12, HEAD, "pull_request", "success",
                               ["verdict-pass"], ts=E1)
    # no raise - logging never blocks the check
    gate.append_log(str(blocker / "x.jsonl"), rec)
    assert capsys.readouterr().err  # but loudly, on stderr


# --- I/O layer: gh_api / gh_paginated / create_check_run -----------------

class FakeResponse:
    def __init__(self, payload):
        self.payload = payload

    def read(self):
        return json.dumps(self.payload).encode()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def test_gh_api_get_shape(monkeypatch):
    seen = []
    monkeypatch.setattr(gate.urllib.request, "urlopen",
                        lambda req, timeout=0: seen.append(req)
                        or FakeResponse({"ok": True}))
    assert gate.gh_api("tok", "GET", "/repos/o/r/pulls/12") == {"ok": True}
    req = seen[0]
    assert req.full_url == "https://api.github.com/repos/o/r/pulls/12"
    assert req.headers["Authorization"] == "Bearer tok"
    assert req.data is None  # a GET carries no body


def test_gh_api_post_serializes_the_body(monkeypatch):
    seen = []
    monkeypatch.setattr(gate.urllib.request, "urlopen",
                        lambda req, timeout=0: seen.append(req)
                        or FakeResponse({"id": 1}))
    gate.gh_api("tok", "POST", "/repos/o/r/check-runs", {"name": "x"})
    req = seen[0]
    assert req.get_method() == "POST"
    assert json.loads(req.data) == {"name": "x"}
    assert req.headers["Content-type"] == "application/json"


def test_gh_api_raises_on_http_error(monkeypatch):
    def boom(req, timeout=0):
        raise gate.urllib.error.HTTPError(
            req.full_url, 403, "Forbidden", {}, None)

    monkeypatch.setattr(gate.urllib.request, "urlopen", boom)
    with pytest.raises(gate.urllib.error.HTTPError):
        gate.gh_api("tok", "GET", "/repos/o/r/pulls/12")


def test_gh_paginated_collects_pages_until_a_short_one(monkeypatch):
    pages = []

    def fake(token, method, path, body=None):
        m = re.search(r"[?&]page=(\d+)", path)  # not the 'page' in per_page
        pages.append(int(m.group(1)))
        return list(range(100)) if pages[-1] == 1 else [1, 2]

    monkeypatch.setattr(gate, "gh_api", fake)
    items = gate.gh_paginated("tok", "/repos/o/r/pulls/12/files")
    assert items == list(range(100)) + [1, 2]
    assert pages == [1, 2]  # the short page ends the walk


def test_gh_paginated_joins_an_existing_query_string(monkeypatch):
    seen = []
    monkeypatch.setattr(gate, "gh_api",
                        lambda t, m, path, body=None: seen.append(path) or [])
    gate.gh_paginated("tok", "/repos/o/r/pulls?state=all")
    assert "&per_page=100&page=1" in seen[0]


def test_gh_paginated_overflow_raises_instead_of_passing(monkeypatch):
    """A diff too big to scan must never silently pass the scope guard: a
    full cap page raises."""
    monkeypatch.setattr(gate, "MAX_PAGES", 2)
    monkeypatch.setattr(gate, "gh_api",
                        lambda t, m, path, body=None: list(range(100)))
    with pytest.raises(RuntimeError, match="overflow"):
        gate.gh_paginated("tok", "/repos/o/r/pulls/12/files")


def test_create_check_run_completed_payload(monkeypatch):
    seen = []
    monkeypatch.setattr(gate, "gh_api",
                        lambda t, m, path, body=None: seen.append((m, path, body))
                        or {"id": 7})
    out = gate.create_check_run("tok", "o/r", HEAD, "completed", "success",
                                "t", "s")
    assert out == {"id": 7}
    method, path, body = seen[0]
    assert (method, path) == ("POST", "/repos/o/r/check-runs")
    assert body == {"name": "pr-review-agent", "head_sha": HEAD,
                    "status": "completed", "conclusion": "success",
                    "output": {"title": "t", "summary": "s"}}


def test_create_check_run_in_progress_omits_conclusion(monkeypatch):
    seen = []
    monkeypatch.setattr(gate, "gh_api",
                        lambda t, m, path, body=None: seen.append(body) or {})
    gate.create_check_run("tok", "o/r", HEAD, "in_progress", None, "t", "s")
    body = seen[0]
    assert body["name"] == "pr-review-agent"
    assert body["status"] == "in_progress"
    assert "conclusion" not in body


# --- main(): env posture, mode routing, the gate flow end to end ---------

BASE_ENV = {"REPO": "o/r", "PR_NUMBER": "12", "GITHUB_TOKEN": "tok",
            "HEAD_SHA": HEAD, "DRAFT": "false", "PR_AUTHOR": "alice",
            "EVENT_NAME": "pull_request"}
OPTIONAL_ENV = ("PR_REVIEW_GATE", "VEGGIES_REVIEW_LOG")


def _env(monkeypatch, values, tmp_path):
    for k in OPTIONAL_ENV:
        monkeypatch.delenv(k, raising=False)
    for k, v in values.items():
        monkeypatch.setenv(k, v)
    monkeypatch.setenv("VEGGIES_REVIEW_LOG", str(tmp_path / "log.jsonl"))
    return tmp_path / "log.jsonl"


def _no_reads(monkeypatch):
    def boom(*a, **k):
        raise AssertionError("this mode must not read the API")

    monkeypatch.setattr(gate, "gh_api", boom)
    monkeypatch.setattr(gate, "gh_paginated", boom)


def _capture_checks(monkeypatch):
    created = []
    monkeypatch.setattr(gate, "create_check_run",
                        lambda *a: created.append(a) or {"id": 1})
    return created


def _read_log(log):
    return [json.loads(l) for l in log.read_text().splitlines()]


def test_main_usage_exit_2(capsys):
    assert gate.main([]) == 2
    assert "usage" in capsys.readouterr().err
    assert gate.main(["frobnicate"]) == 2
    assert "usage" in capsys.readouterr().err


def test_main_gate_missing_env_is_exit_2(monkeypatch, capsys):
    for k in ("REPO", "PR_NUMBER", "GITHUB_TOKEN"):
        monkeypatch.delenv(k, raising=False)
    assert gate.main(["gate"]) == 2
    assert "missing env" in capsys.readouterr().err


def test_main_gate_happy_path_pass_verdict(monkeypatch, tmp_path):
    log = _env(monkeypatch, BASE_ENV, tmp_path)
    created = _capture_checks(monkeypatch)

    def no_single_fetch(*a, **k):
        raise AssertionError("HEAD_SHA/DRAFT/PR_AUTHOR are all in env, and "
                             "an unscoped diff never needs the commit fetch")

    monkeypatch.setattr(gate, "gh_api", no_single_fetch)

    def fake_paginated(token, path):
        if "/pulls/12/files" in path:
            return [{"filename": "docs/runbook.md"}]
        if "/pulls/12/reviews" in path:
            return [_review("pr-review-verdict: pass")]
        if "/issues/12/comments" in path:
            return []
        raise AssertionError(f"unexpected path {path}")

    monkeypatch.setattr(gate, "gh_paginated", fake_paginated)
    assert gate.main(["gate"]) == 0
    (token, repo, head, status, conclusion, title, summary), = created
    assert (token, repo, head) == ("tok", "o/r", HEAD)
    assert (status, conclusion) == ("completed", "success")
    assert title == "pr-review-agent: reviewer verdict: pass"
    (rec,) = _read_log(log)
    assert set(rec) == LOG_KEYS
    assert rec["state"] == "success" and rec["reasons"] == ["verdict-pass"]
    assert rec["verdict"] == "pass"
    assert rec["verdict_review_url"] == "https://x/review/1"
    assert rec["pr"] == 12 and rec["head_sha"] == HEAD
    assert rec["event"] == "pull_request" and rec["resolution"] is None


def test_main_gate_scope_red_fetches_the_head_commit_and_fails(
        monkeypatch, tmp_path):
    log = _env(monkeypatch, BASE_ENV, tmp_path)
    created = _capture_checks(monkeypatch)
    fetched = []

    def fake_api(token, method, path, body=None):
        fetched.append(path)
        if path == f"/repos/o/r/commits/{HEAD}":
            return {"commit": {"committer": {"date": T1}}}
        raise AssertionError(f"unexpected path {path}")

    monkeypatch.setattr(gate, "gh_api", fake_api)

    def fake_paginated(token, path):
        if "/pulls/12/files" in path:
            return [{"filename": "scripts/x.py"}]
        if "/pulls/12/reviews" in path or "/issues/12/comments" in path:
            return []
        raise AssertionError(f"unexpected path {path}")

    monkeypatch.setattr(gate, "gh_paginated", fake_paginated)
    # a legit failure verdict is exit 0 - the check STATE carries the signal
    assert gate.main(["gate"]) == 0
    assert f"/repos/o/r/commits/{HEAD}" in fetched  # the scope signal time
    (_, _, _, status, conclusion, title, _), = created
    assert (status, conclusion) == ("completed", "failure")
    assert title == "pr-review-agent: declared-scope diff"
    (rec,) = _read_log(log)
    assert rec["state"] == "failure" and rec["reasons"] == ["declared-scope"]
    assert rec["verdict"] is None and rec["human_actor"] is None


def test_main_gate_disabled_short_circuits(monkeypatch, tmp_path):
    log = _env(monkeypatch, {**BASE_ENV, "PR_REVIEW_GATE": "disabled"},
               tmp_path)
    created = _capture_checks(monkeypatch)
    _no_reads(monkeypatch)
    assert gate.main(["gate"]) == 0
    (_, _, head, status, conclusion, title, summary), = created
    assert head == HEAD and (status, conclusion) == ("completed", "success")
    assert title == "pr-review-agent: disabled"
    assert summary == ("The gate is disabled by the PR_REVIEW_GATE "
                       "repository variable.")
    (rec,) = _read_log(log)
    assert rec["state"] == "success" and rec["reasons"] == ["disabled"]


def test_main_gate_api_failure_is_exit_1(monkeypatch, tmp_path, capsys):
    _env(monkeypatch, BASE_ENV, tmp_path)
    _capture_checks(monkeypatch)

    def boom(token, path):
        raise gate.urllib.error.URLError("api down")

    monkeypatch.setattr(gate, "gh_paginated", boom)
    assert gate.main(["gate"]) == 1
    assert "api down" in capsys.readouterr().err


def test_main_merge_group_mode(monkeypatch, tmp_path):
    env = {"REPO": "o/r", "HEAD_SHA": HEAD, "GITHUB_TOKEN": "tok",
           "EVENT_NAME": "merge_group"}
    log = _env(monkeypatch, env, tmp_path)
    for k in ("PR_NUMBER", "DRAFT", "PR_AUTHOR"):
        monkeypatch.delenv(k, raising=False)
    created = _capture_checks(monkeypatch)
    _no_reads(monkeypatch)
    assert gate.main(["merge-group"]) == 0
    (_, _, head, status, conclusion, title, summary), = created
    assert head == HEAD and (status, conclusion) == ("completed", "success")
    assert title == "pr-review-agent: gated at PR head"
    assert summary == ("Each PR in the group passed the gate at its own "
                       "head; the group run verifies CI only.")
    (rec,) = _read_log(log)
    assert rec["state"] == "success" and rec["reasons"] == ["merge-group"]
    assert rec["pr"] is None and rec["event"] == "merge_group"


def test_main_merge_group_requires_head_sha(monkeypatch, tmp_path, capsys):
    _env(monkeypatch, {"REPO": "o/r", "GITHUB_TOKEN": "tok"}, tmp_path)
    monkeypatch.delenv("HEAD_SHA", raising=False)
    assert gate.main(["merge-group"]) == 2
    assert "missing env" in capsys.readouterr().err


# --- workflow contract: .github/workflows/pr-review-gate.yml -------------
#
# The workflow is the ONLY writer of the pr-review-agent check; these tests
# pin its safety invariants (ADR 0055). YAML 1.1 parses bare `on:` as True,
# hence the .get("on", .get(True)) pattern (same as tests/test_infra_ci.py).

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW_PATH = ROOT / ".github/workflows/pr-review-gate.yml"
WORKFLOW = yaml.safe_load(WORKFLOW_PATH.read_text())
GATE_JOB = WORKFLOW["jobs"]["gate"]
BASE_REF = ("${{ github.event.pull_request.base.sha || "
            "github.event.repository.default_branch }}")


def test_workflow_triggers_are_exactly_the_four_required():
    triggers = WORKFLOW.get("on", WORKFLOW.get(True))  # bare `on:` -> True
    assert set(triggers) == {"pull_request_target", "pull_request_review",
                             "issue_comment", "merge_group"}
    assert triggers["pull_request_target"]["types"] == \
        ["opened", "synchronize", "reopened", "ready_for_review"]
    assert triggers["pull_request_review"]["types"] == \
        ["submitted", "dismissed", "edited"]
    assert triggers["issue_comment"]["types"] == ["created"]
    assert triggers["merge_group"] is None  # no types filter


def test_the_only_job_is_gate_and_nothing_is_named_like_the_context():
    # Actions auto-creates a check run per job; a job named like the
    # required context would be satisfied by job completion regardless of
    # the gate's verdict.
    assert set(WORKFLOW["jobs"]) == {"gate"}
    for key, job in WORKFLOW["jobs"].items():
        assert key != gate.CONTEXT
        assert job.get("name") != gate.CONTEXT
    raw = WORKFLOW_PATH.read_text()
    assert not re.search(rf"^\s*name:\s*{re.escape(gate.CONTEXT)}\s*$",
                         raw, re.MULTILINE), \
        "no `name:` anywhere may equal the check context"


def test_workflow_permissions_are_minimal_and_check_writing():
    perms = WORKFLOW["permissions"]
    assert perms["checks"] == "write"
    # the check-runs API only: the legacy statuses API must never appear
    # (a second writer surface for the same context)
    assert "statuses" not in perms
    assert set(perms) == {"checks", "contents", "pull-requests", "issues"}


def test_every_checkout_pins_the_base_sha_never_the_head():
    checkouts = [s for job in WORKFLOW["jobs"].values()
                 for s in job.get("steps", [])
                 if s.get("uses", "").startswith("actions/checkout@")]
    assert checkouts, "the workflow must check out the repo"
    for step in checkouts:
        ref = step.get("with", {}).get("ref", "")
        assert ref == BASE_REF
        assert "head" not in ref  # never a PR-influenced ref


def test_run_step_dispatches_merge_group_iff_event_is_merge_group():
    run_steps = [s for s in GATE_JOB["steps"] if "run" in s]
    (run_step,) = run_steps  # exactly one run step
    script = run_step["run"]
    assert "set -euo pipefail" in script
    assert '"$EVENT_NAME" = "merge_group"' in script
    assert "scripts/pr_review_gate.py merge-group" in script
    assert "scripts/pr_review_gate.py gate" in script
    env = run_step["env"]
    for key in ("REPO", "GITHUB_TOKEN", "EVENT_NAME", "PR_NUMBER",
                "HEAD_SHA", "DRAFT", "PR_AUTHOR", "PR_REVIEW_GATE"):
        assert key in env, f"missing env {key}"


def test_concurrency_serializes_per_subject_without_cancelling():
    conc = GATE_JOB["concurrency"]
    group = conc["group"]
    assert group.startswith("pr-review-gate-")
    for field in ("github.event.pull_request.number",
                  "github.event.issue.number",
                  "github.event.merge_group.head_sha", "github.run_id"):
        assert field in group
    # every run recomputes live head-scoped state; a cancelled run could
    # drop a dismissed/edited review and strand a stale check
    assert conc["cancel-in-progress"] is False


def test_issue_comment_if_requires_pr_override_prefix_and_trust():
    cond = GATE_JOB["if"]
    # all other events always run; only issue_comment is narrowed
    assert "github.event_name != 'issue_comment'" in cond
    assert "github.event.issue.pull_request" in cond
    assert "startsWith(github.event.comment.body, '/gate-override')" in cond
    assert '["OWNER","MEMBER"]' in cond
    assert "github.event.comment.author_association" in cond


def test_runs_on_the_self_hosted_veggies_pool():
    # the self-hosted runner is what lets the decision log reach the stack
    # state dir (the script fails open if it cannot)
    assert GATE_JOB["runs-on"] == ["self-hosted", "linux", "x64", "veggies"]


def test_context_literal_matches_branch_protection():
    # drift guard: the literal the workflow and branch protection rely on
    assert gate.CONTEXT == "pr-review-agent"


# --- terraform contract: the pr_review_gate_repos opt-in (ADR 0055) --------
#
# The ruleset in terraform/github/repos.tf is what turns the pr-review-agent
# context into a REQUIRED check per repo; these tests pin the contract between
# the terraform, the script and the workflow above.

REPOS_TF = (ROOT / "terraform/github/repos.tf").read_text()


def _variable_block(text, name):
    m = re.search(rf'^variable "{name}" {{\n(.*?)(?=^variable |\Z)',
                  text, re.MULTILINE | re.DOTALL)
    assert m, f'variable "{name}" not declared'
    return m.group(1)


def test_repos_tf_context_literal_is_the_local_and_matches_the_script():
    # exactly one "pr-review-agent" string literal in repos.tf: the local.
    assert len(re.findall(r'"pr-review-agent"', REPOS_TF)) == 1
    assert 'pr_review_gate_context = "pr-review-agent"' in REPOS_TF
    assert f'"{gate.CONTEXT}"' == '"pr-review-agent"'


def test_repos_tf_pins_the_github_actions_app_for_the_gate_context():
    # only a GitHub Actions App token (the gate workflow's GITHUB_TOKEN) may
    # satisfy the required context - a forged commit status from a classic
    # PAT must not. 15368 = github-actions (gh api apps/github-actions).
    assert re.search(r"integration_id = required_check\.value == "
                     r"local\.pr_review_gate_context \? 15368 : null",
                     REPOS_TF)


def test_pr_review_gate_repos_declared_at_both_tiers_with_empty_default():
    for rel in ("terraform/variables.tf", "terraform/github/variables.tf"):
        block = _variable_block((ROOT / rel).read_text(),
                                "pr_review_gate_repos")
        assert re.search(r"default\s*=\s*\[\]", block), rel


def test_github_tier_validates_pr_review_gate_repos_subset_of_repos():
    block = _variable_block(
        (ROOT / "terraform/github/variables.tf").read_text(),
        "pr_review_gate_repos")
    assert "contains(var.repos, r)" in block


def test_root_module_passes_pr_review_gate_repos_through():
    main = (ROOT / "terraform/main.tf").read_text()
    assert re.search(r"pr_review_gate_repos\s*=\s*var\.pr_review_gate_repos",
                     main)


def test_tfvars_example_default_required_checks_stay_gate_free():
    # Parse the first required_checks = [ match exactly the way
    # tests/test_infra_ci.py does; the commented pr_review_gate_repos hint
    # must not alter it, and the default required set stays gate-free.
    tfvars = (ROOT / "terraform/terraform.tfvars.example").read_text()
    required = ast.literal_eval(
        re.search(r"required_checks\s*=\s*(\[[^\]]*\])", tfvars).group(1))
    assert gate.CONTEXT not in required
