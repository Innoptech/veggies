"""Tests for scripts/pr_review_gate.py - the deterministic core of the
pr-review-agent required check (issue #103, ADR 0055): declared-scope
hard-fail plus reviewer-verdict state machine, check-run writer, and
fail-closed error reporting. The gate does NOT write the decision log -
the runner container is ephemeral; scripts/pr_review_verdicts.py harvests
it (ADR 0055 decision 7, tested in tests/test_pr_review_verdicts.py)."""

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
# A realistic full sha for the sha-bound override command (/gate-override
# names the exact 40-hex head).
HEAD40 = "0123456789abcdef0123456789abcdef01234567"


def _review(body, commit_id=HEAD, association="OWNER",
            submitted_at="2026-09-12T10:00:00Z", url="https://x/review/1",
            login="reviewer-bot", state="COMMENTED", id=1):
    return {"body": body, "commit_id": commit_id,
            "author_association": association, "submitted_at": submitted_at,
            "html_url": url, "user": {"login": login}, "state": state,
            "id": id}


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


def test_latest_verdict_ignores_dismissed_reviews():
    # a dismissed verdict stops counting - the workflow re-triggers on
    # `dismissed` and recomputes without it. The API uppercases the state;
    # compare case-insensitively anyway.
    dismissed = _review("pr-review-verdict: fail", state="DISMISSED",
                        submitted_at="2026-09-12T11:00:00Z")
    assert gate.latest_verdict([dismissed], HEAD) is None
    lower = _review("pr-review-verdict: fail", state="dismissed")
    assert gate.latest_verdict([lower], HEAD) is None
    # a live pass posted BEFORE the dismissal survives the dismissed fail
    live = _review("pr-review-verdict: pass", url="https://x/review/2")
    v = gate.latest_verdict([dismissed, live], HEAD)
    assert v == ("pass", "2026-09-12T10:00:00Z", "https://x/review/2")


def test_latest_verdict_latest_submission_wins():
    older = _review("pr-review-verdict: fail", url="https://x/review/1",
                    submitted_at="2026-09-12T10:00:00Z", id=10)
    newer = _review("pr-review-verdict: pass", url="https://x/review/2",
                    submitted_at="2026-09-12T11:00:00Z", id=11)
    v = gate.latest_verdict([newer, older], HEAD)
    assert v == ("pass", "2026-09-12T11:00:00Z", "https://x/review/2")
    # order in the API page must not matter
    assert gate.latest_verdict([older, newer], HEAD) == v


def test_latest_verdict_same_timestamp_higher_review_id_wins():
    # same-second submissions happen (retried posts); the review id breaks
    # the tie - higher id = newer.
    first = _review("pr-review-verdict: fail", url="https://x/review/1",
                    id=101)
    second = _review("pr-review-verdict: pass", url="https://x/review/2",
                     id=102)
    v = ("pass", "2026-09-12T10:00:00Z", "https://x/review/2")
    assert gate.latest_verdict([first, second], HEAD) == v
    assert gate.latest_verdict([second, first], HEAD) == v
    # a missing id counts as 0 and loses any tie
    no_id = _review("pr-review-verdict: fail", url="https://x/review/3")
    del no_id["id"]
    assert gate.latest_verdict([no_id, second], HEAD) == v


# --- human_acts: every qualifying human act on THIS head -----------------
#
# Two kinds (ADR 0055 decision 5, as amended):
# - an APPROVED review pinned to head_sha, OWNER/MEMBER, non-author, that
#   does NOT itself carry a verdict marker (a verdict never clears itself);
# - an issue comment starting with `/gate-override <full-head-sha>` from an
#   OWNER/MEMBER - the sha binds the act to this exact head.
# Expected epochs are built with the datetime constructor, an independent
# path from the implementation's fromisoformat.

T1 = "2026-09-12T10:00:00Z"
T2 = "2026-09-12T11:00:00Z"
E1 = datetime(2026, 9, 12, 10, 0, 0, tzinfo=timezone.utc).timestamp()
E2 = datetime(2026, 9, 12, 11, 0, 0, tzinfo=timezone.utc).timestamp()


def _approval(commit_id=HEAD, association="OWNER", login="bob",
              submitted_at=T1, state="APPROVED", body=None):
    return {"state": state, "commit_id": commit_id,
            "author_association": association, "submitted_at": submitted_at,
            "user": {"login": login}, "html_url": "https://x/review/9",
            "body": body}


def _comment(body, association="MEMBER", login="carol", created_at=T2):
    return {"body": body, "author_association": association,
            "created_at": created_at, "user": {"login": login},
            "html_url": "https://x/comment/1"}


def test_human_acts_head_approval_by_owner_counts():
    assert gate.human_acts([_approval()], [], HEAD, "alice") == [(E1, "bob")]


def test_human_acts_ignores_approval_on_an_old_sha():
    assert gate.human_acts([_approval(commit_id="oldsha")], [],
                           HEAD, "alice") == []


def test_human_acts_ignores_self_approval():
    # GitHub rejects self-approvals; belt and braces (a forged payload must
    # not clear the gate either).
    assert gate.human_acts([_approval(login="alice")], [], HEAD, "alice") == []


def test_human_acts_ignores_contributor_approval():
    assert gate.human_acts([_approval(association="CONTRIBUTOR")], [],
                           HEAD, "alice") == []


def test_human_acts_ignores_non_approval_states():
    for state in ("COMMENTED", "CHANGES_REQUESTED", "DISMISSED"):
        assert gate.human_acts([_approval(state=state)], [],
                               HEAD, "alice") == [], state


def test_human_acts_marker_carrying_approval_is_not_a_clearing_act():
    # a verdict must not clear itself, even when APPROVED-state from an
    # OWNER/MEMBER non-author.
    for body in ("pr-review-verdict: pass",
                 "LGTM.\n\npr-review-verdict: fail\n"):
        assert gate.human_acts([_approval(body=body)], [],
                               HEAD, "alice") == [], body


def test_human_acts_override_comment_naming_this_head_counts():
    c = _comment(f"/gate-override {HEAD40}")
    assert gate.human_acts([], [c], HEAD40, "alice") == [(E2, "carol")]
    # trailing prose after the sha is fine
    c = _comment(f"/gate-override {HEAD40} - I read the diff")
    assert gate.human_acts([], [c], HEAD40, "alice") == [(E2, "carol")]
    # the sha compare is case-insensitive (a pasted sha may be uppercased)
    c = _comment(f"/gate-override {HEAD40.upper()}")
    assert gate.human_acts([], [c], HEAD40, "alice") == [(E2, "carol")]


def test_human_acts_override_comment_must_name_this_head():
    # a comment naming any other sha (or no sha) is not a clearing act -
    # one genuine override must not pre-clear later heads.
    other = "f" * 40
    for body in (f"/gate-override {other}",         # another sha
                 "/gate-override",                  # no sha
                 "/gate-override: I read the diff",  # the old unbound form
                 f"/gate-override {HEAD40[:39]}",   # a short sha
                 f"please /gate-override {HEAD40}",  # leading prose
                 f"lgtm\n/gate-override {HEAD40}"):  # mid-comment mention
        assert gate.human_acts([], [_comment(body)], HEAD40,
                               "alice") == [], body


def test_human_acts_override_comment_requires_a_human_association():
    c = _comment(f"/gate-override {HEAD40}", association="CONTRIBUTOR")
    assert gate.human_acts([], [c], HEAD40, "alice") == []


def test_human_acts_returns_every_qualifying_act():
    acts = gate.human_acts(
        [_approval(commit_id=HEAD40, submitted_at=T1),
         _approval(commit_id=HEAD40, login="dave", submitted_at=T2)],
        [_comment(f"/gate-override {HEAD40}", created_at=T2)],
        HEAD40, "alice")
    assert acts == [(E1, "bob"), (E2, "dave"), (E2, "carol")]


# --- decide: the whole state machine -------------------------------------
#
# decide() evaluates BOTH red lanes on every call; a human act only rescues
# a RED state, never the no-verdict pending one. human_acts is the list of
# (epoch_ts, actor_login) qualifying acts on THIS head (see above).

FAIL_VERDICT = ("fail", T1, "https://x/review/1")
PASS_VERDICT = ("pass", T1, "https://x/review/1")


def _remediation(head=HEAD):
    # the red summaries' last line: the exact paste-able command (the
    # summary is the UI).
    return ("Cleared by a human APPROVED review on the current head, or an "
            f"OWNER/MEMBER comment: /gate-override {head}")


def test_decide_draft_short_circuits_everything():
    status, conclusion, title, summary = gate.decide(
        True, ["scripts/x.py"], FAIL_VERDICT, [(E2, "bob")], HEAD)
    assert (status, conclusion) == ("completed", "success")
    assert title == "pr-review-agent: draft"
    assert summary == "The gate evaluates at the ready-for-review transition."


def test_decide_scope_red_uncleared_fails_closed():
    scope = ["scripts/x.py", "AGENTS.md", "docs/CODEOWNERS"]
    status, conclusion, title, summary = gate.decide(
        False, scope, PASS_VERDICT, [], HEAD)
    assert (status, conclusion) == ("completed", "failure")
    assert title == "pr-review-agent: declared-scope diff"
    # every matched path, one per line (listed as a markdown bullet)
    lines = summary.splitlines()
    for path in scope:
        assert any(line.endswith(path) for line in lines), path
    # the matched roots are named (derived from the paths)
    for root in ("scripts/", "AGENTS.md", "CODEOWNERS"):
        assert root in summary
    assert summary.endswith(_remediation())


def test_decide_scope_red_cleared_by_a_human_act_on_this_head():
    status, conclusion, title, summary = gate.decide(
        False, ["scripts/x.py"], None, [(E2, "bob")], HEAD)
    assert (status, conclusion) == ("completed", "success")
    assert title == "pr-review-agent: human override"
    assert "scripts/" in summary  # the matched root is named
    assert "bob" in summary       # and so is the clearing actor


def test_decide_scope_clearing_needs_no_timestamp_comparison():
    # head-binding IS the postdating for scope: the human named/approved
    # this exact head, so even an act OLDER than the head commit clears it
    # (the forgeable GIT_COMMITTER_DATE signal is gone).
    assert gate.decide(False, ["scripts/x.py"], None,
                       [(E1 - 9000, "bob")], HEAD)[1] == "success"


def test_decide_scope_cleared_then_a_later_fail_verdict_is_red():
    # the M2 hole: a human's scope-clearing act must NOT green a reviewer
    # fail posted AFTER it - both red lanes are evaluated on every call.
    status, conclusion, title, summary = gate.decide(
        False, ["scripts/x.py"], ("fail", T2, "https://x/review/2"),
        [(E1, "bob")], HEAD)
    assert (status, conclusion) == ("completed", "failure")
    assert title == "pr-review-agent: reviewer verdict: fail"
    assert "https://x/review/2" in summary


def test_decide_both_lanes_red_names_the_first_and_covers_both():
    status, conclusion, title, summary = gate.decide(
        False, ["scripts/x.py"], FAIL_VERDICT, [], HEAD)
    assert (status, conclusion) == ("completed", "failure")
    assert title == "pr-review-agent: declared-scope diff"
    assert "declared human-review scope" in summary  # the scope reason
    assert "FAIL verdict" in summary                 # the verdict reason
    assert "https://x/review/1" in summary
    assert summary.endswith(_remediation())


def test_decide_verdict_fail_uncleared():
    status, conclusion, title, summary = gate.decide(
        False, [], FAIL_VERDICT, [], HEAD)
    assert (status, conclusion) == ("completed", "failure")
    assert title == "pr-review-agent: reviewer verdict: fail"
    assert "https://x/review/1" in summary
    assert summary.endswith(_remediation())


def test_decide_verdict_fail_cleared_only_when_newer_than_the_verdict():
    # T1/E1 is the verdict's own timestamp; the act must postdate it (>=).
    assert gate.decide(False, [], FAIL_VERDICT, [(E1, "bob")],
                       HEAD)[1] == "success"
    status, conclusion, title, _ = gate.decide(
        False, [], FAIL_VERDICT, [(E1 - 1, "bob")], HEAD)
    assert (status, conclusion) == ("completed", "failure")


def test_decide_verdict_fail_cleared_reports_the_override():
    status, conclusion, title, summary = gate.decide(
        False, [], FAIL_VERDICT, [(E2, "carol")], HEAD)
    assert (status, conclusion) == ("completed", "success")
    assert title == "pr-review-agent: human override"
    assert "fail" in summary  # says the human overrode the fail verdict
    assert "https://x/review/1" in summary  # and links the review
    assert "carol" in summary               # and names the actor


def test_decide_cleared_scope_title_wins_over_a_pass_verdict():
    # a human-cleared scope diff plus a pass verdict: the override is the
    # more significant fact for the check title.
    status, conclusion, title, _ = gate.decide(
        False, ["scripts/x.py"], PASS_VERDICT, [(E2, "bob")], HEAD)
    assert (status, conclusion) == ("completed", "success")
    assert title == "pr-review-agent: human override"


def test_decide_verdict_pass():
    status, conclusion, title, summary = gate.decide(
        False, [], PASS_VERDICT, [], HEAD)
    assert (status, conclusion) == ("completed", "success")
    assert title == "pr-review-agent: reviewer verdict: pass"
    assert "https://x/review/1" in summary


def test_decide_no_verdict_stays_pending():
    status, conclusion, title, summary = gate.decide(
        False, [], None, [], HEAD)
    assert (status, conclusion) == ("in_progress", None)
    assert title == "pr-review-agent: awaiting reviewer verdict"
    assert summary == ("The reviewer posts its verdict as a review carrying "
                       "a pr-review-verdict line (issue #102).")


def test_decide_human_act_does_not_rescue_the_pending_state():
    # a /gate-override or approval clears a RED state, never substitutes
    # for the reviewer agent's verdict.
    status, conclusion, title, _ = gate.decide(
        False, [], None, [(E2, "bob")], HEAD)
    assert (status, conclusion) == ("in_progress", None)
    assert title == "pr-review-agent: awaiting reviewer verdict"


def test_decide_override_posted_early_clears_the_same_head_later():
    # accepted and now explicit: the override binds a HEAD, not a moment -
    # posted while the PR was still a draft, it clears the post-ready red
    # at that same head (the comment named this exact sha).
    acts = gate.human_acts(
        [], [_comment(f"/gate-override {HEAD40}", created_at=T1)],
        HEAD40, "alice")
    status, conclusion, title, _ = gate.decide(
        False, ["scripts/x.py"], None, acts, HEAD40)
    assert (status, conclusion) == ("completed", "success")
    assert title == "pr-review-agent: human override"


# --- decision_record + append_log: the ADR 0055 audit-trail schema -------
#
# The gate itself never appends (the runner container is ephemeral); the
# harvester (scripts/pr_review_verdicts.py) is the writer. The schema is
# pinned here because decision_record/append_log live in the gate module.

LOG_KEYS = {"ts", "repo", "pr", "head_sha", "event", "state", "reasons",
            "verdict", "verdict_review_url", "human_actor", "resolution",
            "review_id"}


def test_decision_record_has_the_exact_schema_keys():
    rec = gate.decision_record("o/r", 12, HEAD, "harvest", "verdict",
                               ["verdict-pass"], verdict="pass",
                               verdict_review_url="https://x/review/1",
                               human_actor=None, resolution=None,
                               review_id=101, ts=E1)
    assert set(rec) == LOG_KEYS
    assert rec == {"ts": E1, "repo": "o/r", "pr": 12, "head_sha": HEAD,
                   "event": "harvest", "state": "verdict",
                   "reasons": ["verdict-pass"], "verdict": "pass",
                   "verdict_review_url": "https://x/review/1",
                   "human_actor": None, "resolution": None,
                   "review_id": 101}


def test_decision_record_defaults():
    rec = gate.decision_record("o/r", None, HEAD, "merge_group", "success",
                               ["merge-group"], ts=E1)
    assert set(rec) == LOG_KEYS
    assert rec["verdict"] is None and rec["verdict_review_url"] is None
    assert rec["human_actor"] is None and rec["resolution"] is None
    assert rec["review_id"] is None


def test_append_log_writes_one_parseable_json_line(tmp_path):
    log = tmp_path / "nested" / "dir" / "verdicts.jsonl"  # parent created
    rec = gate.decision_record("o/r", 12, HEAD, "harvest", "verdict",
                               ["verdict-fail"], verdict="fail",
                               review_id=101, ts=E1)
    gate.append_log(str(log), rec)
    lines = log.read_text().splitlines()
    assert len(lines) == 1
    parsed = json.loads(lines[0])
    assert set(parsed) == LOG_KEYS and parsed["state"] == "verdict"
    gate.append_log(str(log), rec)  # appends, never truncates
    assert len(log.read_text().splitlines()) == 2


def test_append_log_fails_open_on_an_unwritable_path(tmp_path, capsys):
    blocker = tmp_path / "afile"
    blocker.write_text("not a dir")
    rec = gate.decision_record("o/r", 12, HEAD, "harvest", "verdict",
                               ["verdict-pass"], review_id=101, ts=E1)
    # no raise - logging never blocks the caller
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
GATE_ENV = ("REPO", "PR_NUMBER", "GITHUB_TOKEN", "HEAD_SHA", "DRAFT",
            "PR_AUTHOR", "EVENT_NAME", "PR_REVIEW_GATE",
            "VEGGIES_REVIEW_LOG")


def _env(monkeypatch, values, tmp_path):
    """A hermetic gate env (nothing leaks in from the real environment).
    Returns the decision-log path - every main_* test asserts the gate
    never writes it (the harvester owns the log)."""
    for k in GATE_ENV:
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


def _no_single_fetch(monkeypatch):
    def boom(*a, **k):
        raise AssertionError("HEAD_SHA/DRAFT/PR_AUTHOR are all in env - "
                             "no single-resource fetch may happen")

    monkeypatch.setattr(gate, "gh_api", boom)


def _capture_checks(monkeypatch):
    created = []
    monkeypatch.setattr(gate, "create_check_run",
                        lambda *a: created.append(a) or {"id": 1})
    return created


def test_main_usage_exit_2(capsys):
    assert gate.main([]) == 2
    assert "usage" in capsys.readouterr().err
    assert gate.main(["frobnicate"]) == 2
    assert "usage" in capsys.readouterr().err


def test_main_gate_missing_env_is_exit_2(monkeypatch, capsys):
    for k in GATE_ENV:
        monkeypatch.delenv(k, raising=False)
    assert gate.main(["gate"]) == 2
    assert "missing env" in capsys.readouterr().err


def test_main_gate_happy_path_pass_verdict(monkeypatch, tmp_path):
    log = _env(monkeypatch, BASE_ENV, tmp_path)
    created = _capture_checks(monkeypatch)
    _no_single_fetch(monkeypatch)

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
    assert not log.exists()  # the gate NEVER writes the decision log


def test_main_gate_scope_red_needs_no_head_commit_fetch(monkeypatch,
                                                        tmp_path):
    # the committer-date postdating is gone (it was forgeable via
    # GIT_COMMITTER_DATE) - scope-red never fetches /commits/{sha}.
    log = _env(monkeypatch, BASE_ENV, tmp_path)
    created = _capture_checks(monkeypatch)
    _no_single_fetch(monkeypatch)

    def fake_paginated(token, path):
        if "/pulls/12/files" in path:
            return [{"filename": "scripts/x.py"}]
        if "/pulls/12/reviews" in path or "/issues/12/comments" in path:
            return []
        raise AssertionError(f"unexpected path {path}")

    monkeypatch.setattr(gate, "gh_paginated", fake_paginated)
    # a legit failure verdict is exit 0 - the check STATE carries the signal
    assert gate.main(["gate"]) == 0
    (_, _, _, status, conclusion, title, _), = created
    assert (status, conclusion) == ("completed", "failure")
    assert title == "pr-review-agent: declared-scope diff"
    assert not log.exists()


def test_main_gate_scope_scan_sees_a_rename_out_of_scope(monkeypatch,
                                                         tmp_path):
    # scripts/x.py -> docs/x.py: the OLD name was in scope; the files API
    # carries it as previous_filename and the scan must see both names.
    _env(monkeypatch, BASE_ENV, tmp_path)
    created = _capture_checks(monkeypatch)
    _no_single_fetch(monkeypatch)

    def fake_paginated(token, path):
        if "/pulls/12/files" in path:
            return [{"filename": "docs/x.py",
                     "previous_filename": "scripts/x.py"}]
        if "/pulls/12/reviews" in path:
            return [_review("pr-review-verdict: pass")]
        if "/issues/12/comments" in path:
            return []
        raise AssertionError(f"unexpected path {path}")

    monkeypatch.setattr(gate, "gh_paginated", fake_paginated)
    assert gate.main(["gate"]) == 0
    (_, _, _, status, conclusion, title, summary), = created
    assert (status, conclusion) == ("completed", "failure")
    assert title == "pr-review-agent: declared-scope diff"
    assert "scripts/x.py" in summary


def test_main_gate_scope_scan_sees_a_rename_into_scope(monkeypatch,
                                                       tmp_path):
    # docs/x.py -> scripts/x.py: the new name alone hits.
    _env(monkeypatch, BASE_ENV, tmp_path)
    created = _capture_checks(monkeypatch)
    _no_single_fetch(monkeypatch)

    def fake_paginated(token, path):
        if "/pulls/12/files" in path:
            return [{"filename": "scripts/x.py",
                     "previous_filename": "docs/x.py"}]
        if "/pulls/12/reviews" in path:
            return [_review("pr-review-verdict: pass")]
        if "/issues/12/comments" in path:
            return []
        raise AssertionError(f"unexpected path {path}")

    monkeypatch.setattr(gate, "gh_paginated", fake_paginated)
    assert gate.main(["gate"]) == 0
    (_, _, _, status, conclusion, title, _), = created
    assert (status, conclusion) == ("completed", "failure")
    assert title == "pr-review-agent: declared-scope diff"


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
    assert not log.exists()


def test_main_gate_api_failure_reports_a_red_check_and_exits_1(
        monkeypatch, tmp_path, capsys):
    # fail CLOSED means a red check, not an absent check: the required
    # context must always report.
    _env(monkeypatch, BASE_ENV, tmp_path)
    created = _capture_checks(monkeypatch)

    def boom(token, path):
        raise gate.urllib.error.URLError("api down")

    monkeypatch.setattr(gate, "gh_paginated", boom)
    _no_single_fetch(monkeypatch)
    assert gate.main(["gate"]) == 1
    (_, _, head, status, conclusion, title, summary), = created
    assert head == HEAD and (status, conclusion) == ("completed", "failure")
    assert title == "pr-review-agent: gate error"
    assert "URLError" in summary      # the exception CLASS is named...
    assert "api down" not in summary  # ...never the message (untrusted text)
    assert f"/gate-override {HEAD}" in summary  # the escape is spelled out
    assert "api down" in capsys.readouterr().err  # full error on stderr


def test_main_gate_failure_before_the_head_sha_is_known_reports_nothing(
        monkeypatch, tmp_path, capsys):
    # no head sha yet -> nothing to report a check against -> exit 1,
    # stderr only (issue_comment events resolve the head from the PR).
    env = {"REPO": "o/r", "PR_NUMBER": "12", "GITHUB_TOKEN": "tok",
           "EVENT_NAME": "issue_comment"}
    _env(monkeypatch, env, tmp_path)
    created = _capture_checks(monkeypatch)

    def boom(*a, **k):
        raise gate.urllib.error.URLError("api down")

    monkeypatch.setattr(gate, "gh_api", boom)
    monkeypatch.setattr(gate, "gh_paginated", boom)
    assert gate.main(["gate"]) == 1
    assert created == []
    assert "api down" in capsys.readouterr().err


def _overflow_fakes(monkeypatch, reviews, comments):
    """The files read overflows (>3000-file diff); reviews/comments were
    fetched FIRST so a sha-bound override is knowable."""
    def fake_paginated(token, path):
        if "/pulls/12/reviews" in path:
            return reviews
        if "/issues/12/comments" in path:
            return comments
        if "/pulls/12/files" in path:
            raise RuntimeError(f"pagination overflow: {path}")
        raise AssertionError(f"unexpected path {path}")

    monkeypatch.setattr(gate, "gh_paginated", fake_paginated)
    _no_single_fetch(monkeypatch)


def test_main_gate_overflow_with_a_sha_bound_override_clears(
        monkeypatch, tmp_path):
    _env(monkeypatch, {**BASE_ENV, "HEAD_SHA": HEAD40}, tmp_path)
    created = _capture_checks(monkeypatch)
    _overflow_fakes(monkeypatch, [], [_comment(f"/gate-override {HEAD40}")])
    assert gate.main(["gate"]) == 0
    (_, _, head, status, conclusion, title, summary), = created
    assert head == HEAD40
    assert (status, conclusion) == ("completed", "success")
    assert title == "pr-review-agent: human override"
    assert ">3000 files" in summary  # the diff was too large to scan
    assert "carol" in summary        # the human took responsibility


def test_main_gate_overflow_without_an_override_fails_closed(
        monkeypatch, tmp_path, capsys):
    _env(monkeypatch, {**BASE_ENV, "HEAD_SHA": HEAD40}, tmp_path)
    created = _capture_checks(monkeypatch)
    _overflow_fakes(monkeypatch, [], [])
    assert gate.main(["gate"]) == 1
    (_, _, _, status, conclusion, title, summary), = created
    assert (status, conclusion) == ("completed", "failure")
    assert title == "pr-review-agent: gate error"
    assert "RuntimeError" in summary  # the overflow's exception class
    assert "overflow" in capsys.readouterr().err


def test_main_gate_overflow_is_not_cleared_by_an_approval_alone(
        monkeypatch, tmp_path):
    # only the explicit sha-bound override comment rescues an unscannable
    # diff - typing THIS head's sha is the deliberate act.
    _env(monkeypatch, {**BASE_ENV, "HEAD_SHA": HEAD40}, tmp_path)
    created = _capture_checks(monkeypatch)
    _overflow_fakes(monkeypatch, [_approval(commit_id=HEAD40)], [])
    assert gate.main(["gate"]) == 1
    (_, _, _, status, conclusion, title, _), = created
    assert (status, conclusion) == ("completed", "failure")
    assert title == "pr-review-agent: gate error"


def test_main_merge_group_mode(monkeypatch, tmp_path):
    env = {"REPO": "o/r", "HEAD_SHA": HEAD, "GITHUB_TOKEN": "tok",
           "EVENT_NAME": "merge_group"}
    log = _env(monkeypatch, env, tmp_path)
    created = _capture_checks(monkeypatch)
    _no_reads(monkeypatch)
    assert gate.main(["merge-group"]) == 0
    (_, _, head, status, conclusion, title, summary), = created
    assert head == HEAD and (status, conclusion) == ("completed", "success")
    assert title == "pr-review-agent: gated at PR head"
    assert summary == ("Each PR in the group passed the gate at its own "
                       "head; the group run verifies CI only.")
    assert not log.exists()


def test_main_merge_group_requires_head_sha(monkeypatch, tmp_path, capsys):
    _env(monkeypatch, {"REPO": "o/r", "GITHUB_TOKEN": "tok"}, tmp_path)
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
DEFAULT_REF = "${{ github.event.repository.default_branch }}"


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


def test_every_checkout_pins_the_default_branch_never_a_pr_ref():
    # The gate script must NEVER run PR-influenced code. base.sha is the
    # tip of whatever branch a PR targets - attacker-controlled for PRs to
    # unprotected branches (the ruleset covers main only) - and head refs
    # are the PR author's own tree. The default branch is the only trusted
    # ref, for every event.
    checkouts = [s for job in WORKFLOW["jobs"].values()
                 for s in job.get("steps", [])
                 if s.get("uses", "").startswith("actions/checkout@")]
    assert checkouts, "the workflow must check out the repo"
    for step in checkouts:
        ref = str(step.get("with", {}).get("ref", ""))
        assert ref == DEFAULT_REF
        for banned in ("pull_request.base.sha", "pull_request.head.sha",
                       "merge_group.head_sha"):
            assert banned not in ref
    # the job if/ref carry the safety - never a trigger filter
    assert "branches:" not in WORKFLOW_PATH.read_text()


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


def test_job_if_narrows_issue_comment_and_pull_request_review_events():
    cond = GATE_JOB["if"]
    # other events always run; only issue_comment / pull_request_review are
    # narrowed (their payloads carry untrusted authors)
    assert "github.event_name != 'issue_comment'" in cond
    assert "github.event_name != 'pull_request_review'" in cond
    # issue_comment: only a PR comment starting with /gate-override from an
    # OWNER/MEMBER reaches the script (which re-verifies everything itself)
    assert "github.event.issue.pull_request" in cond
    assert "startsWith(github.event.comment.body, '/gate-override')" in cond
    assert '["OWNER","MEMBER"]' in cond
    assert "github.event.comment.author_association" in cond
    # pull_request_review: untrusted reviews can't force a self-hosted
    # runner slot (they never change the decision anyway)
    assert "github.event.review.author_association" in cond
    assert '["OWNER","MEMBER","COLLABORATOR"]' in cond


def test_runs_on_the_self_hosted_veggies_pool():
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
