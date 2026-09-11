"""Tests for scripts/stack_kick.py - the issue/discussion->session kick
(ADR 0033 issues, ADR 0038 discussions)."""

import importlib.util
import io
import json
import re
from pathlib import Path

import pytest
import yaml

_spec = importlib.util.spec_from_file_location(
    "stack_kick", Path(__file__).parent.parent / "scripts/stack_kick.py")
stack_kick = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(stack_kick)


class FakeResponse:
    def __init__(self, payload):
        self.payload = payload

    def read(self):
        return json.dumps(self.payload).encode()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


@pytest.fixture()
def calls(monkeypatch):
    """Capture every urlopen call; sessions get id ses_test."""
    seen = []

    def fake_urlopen(req, timeout=0):
        seen.append(req)
        if req.full_url.endswith("/session?directory=/workspace"):
            return FakeResponse({"id": "ses_test"})
        return FakeResponse({})  # prompt_async: 204, empty body

    monkeypatch.setattr(stack_kick.urllib.request, "urlopen", fake_urlopen)
    return seen


@pytest.fixture(autouse=True)
def _isolated_cwd(tmp_path, monkeypatch):
    """The no-ask gate scans the CWD: keep every test off the real
    checkout (a stray project-tier file must never flip unrelated tests)."""
    monkeypatch.chdir(tmp_path)


def test_build_prompt_contains_issue_and_rules():
    # a deliberately foreign gate proves interpolation, not hardcoding
    p = stack_kick.build_prompt("o/r", "12", "Fix the thing",
                                "Some body", "https://x/12",
                                verify_gate="npm test -- --changed")
    assert "#12" in p and "Fix the thing" in p and "Some body" in p
    assert "agent/issue-12" in p and "Closes #12" in p
    assert "npm test -- --changed" in p
    # issue #48: gitleaks/actionlint are enumerated as image-baked
    assert "gitleaks/actionlint" in p


def test_build_prompt_mandates_a_per_session_worktree():
    """Issue #27 / ADR 0037: kicked sessions share one clone at /workspace,
    so the prompt must move each session into its own git worktree before
    any work - and keep the shared checkout read-only for that session."""
    p = stack_kick.build_prompt("o/r", "12", "Fix the thing", "body", "u")
    assert ("git -C /workspace worktree add --lock --reason "
            "'session issue-12' -B agent/issue-12 "
            "/workspace/.veggies/wt/issue-12 origin/main") in p
    assert ".git/info/exclude" in p  # the shared checkout stays clean
    assert "The shared checkout at /workspace itself is read-only to you" in p
    # opencode file tools resolve relative paths against the session dir
    # (/workspace), not the bash cwd - edits must use absolute paths.
    assert "absolute paths under /workspace/.veggies/wt/issue-12" in p
    # recovery guidance must name the errors git 2.54 actually prints for
    # `worktree add`, and must never bless -f/--force (it overrides the
    # branch-held tripwire - verified 2026-09-11).
    assert "already used by worktree" in p and "already exists" in p
    assert "never pass -f/--force" in p


def test_build_prompt_mandates_the_pipeline():
    """Issue #26 / ADR 0036: a kicked session must run the full pipeline -
    plan first, subagent execution, adversarial review, verified checks -
    not just dive into code."""
    p = stack_kick.build_prompt("o/r", "12", "Fix the thing", "body", "u",
                                verify_gate="npm test -- --changed")
    # 1. plan first, posted back to the issue for human review
    assert "writing-plans" in p
    assert f"gh issue comment 12" in p
    # 2. subagent execution, not a solo main loop
    assert "subagent" in p
    # 3. adversarial review of the diff (the vendored different-model
    # subagent) before the ready gate - under draft-first, pushing
    # starts at the first commit
    assert "adversarial-review" in p
    # 4. verified claims only, via the repo's declared gate
    assert "npm test -- --changed" in p


def test_build_prompt_mandates_draft_first_lifecycle():
    """Issue #53 / ADR 0046: the PR exists from the FIRST commit as a
    draft - `gh pr create --draft` right after the plan comment, pushed
    early and often so the draft's CI is the feedback loop for the checks
    this environment cannot run - and `ready` is the final act of a
    green-and-mergeable gate. The done-guard treats the issue as handled
    from that moment, so late changes (a supervisor refinement, ADR 0036)
    convert back with `gh pr ready --undo` first - never push onto a
    ready PR."""
    p = stack_kick.build_prompt("o/r", "12", "Fix the thing", "body", "u")
    # draft from the first commit, not at the end
    assert "gh pr create --draft" in p
    # the draft's CI on the final head gates ready
    assert "gh pr checks --watch" in p
    # mergeable is computed async - UNKNOWN is transient, never a rebase
    # trigger
    assert "--json mergeable" in p and "UNKNOWN" in p
    # rebase-only repo: ready means green AND mergeable against CURRENT
    # main
    assert "git rebase origin/main" in p and "--force-with-lease" in p
    # the supervisor collision: rework converts back first
    assert "gh pr ready --undo" in p
    # honesty rule carried over from the pre-0046 verify step
    assert "Claim only what you actually ran" in p
    # the adversarial review is anchored to the ready gate, not the first
    # push - under draft-first pushing starts at the first commit
    assert "before the ready gate" in p
    # the post-watch freshness check: main may move while the CI watch
    # runs, and mergeable only means conflict-free, never up-to-date
    assert "merge-base --is-ancestor" in p
def test_build_prompt_reads_the_repos_own_instruction_file():
    """Issue #54: a CLAUDE.md-only repo's conventions must enter kicked
    sessions - the prompt must not name AGENTS.md exclusively (the harness
    auto-loads either file; the prompt text lagged)."""
    p = stack_kick.build_prompt("o/r", "12", "Fix the thing", "body", "u")
    assert "Read AGENTS.md first" not in p
    assert "AGENTS.md" in p and "CLAUDE.md" in p
    d = stack_kick.build_discussion_prompt("o/r", "3", "T", "b", "u", [])
    e = stack_kick.build_elaborate_prompt("o/r", "3", "T", "b", "u", [])
    for prompt in (d, e):
        assert "Read AGENTS.md first" not in prompt
        assert "CLAUDE.md" in prompt


def test_build_prompt_truncates_and_defaults():
    p = stack_kick.build_prompt("o/r", "1", "t", "x" * 9000, "u")
    # the body is cut at BODY_LIMIT: the prompt's size is bounded by the
    # template plus that budget, never by the raw body (the draft-first
    # lifecycle, ADR 0046, grew the template past the old absolute 9000 -
    # assert the shape, not a number the template legitimately crosses)
    assert "x" * (stack_kick.BODY_LIMIT + 1) not in p
    assert len(p) < len(stack_kick.build_prompt("o/r", "1", "t", "", "u")) \
        + stack_kick.BODY_LIMIT
    assert "(no description)" in stack_kick.build_prompt("o/r", "1", "t", "", "u")


def test_kick_creates_session_then_queues_prompt(calls):
    sid = stack_kick.kick("http://h:1", "pw", "do it", title="#9: t")
    assert sid == "ses_test"
    create, prompt = calls
    assert create.full_url == "http://h:1/session?directory=/workspace"
    assert create.get_method() == "POST"
    # titled session: the web UI list reads like an issue list (ADR 0034)
    assert json.loads(create.data) == {"title": "#9: t"}
    # basic auth header carries opencode:<password>
    import base64
    assert create.headers["Authorization"] == "Basic " + base64.b64encode(
        b"opencode:pw").decode()
    assert prompt.full_url == \
        "http://h:1/session/ses_test/prompt_async?directory=/workspace"
    body = json.loads(prompt.data)
    assert body == {"parts": [{"type": "text", "text": "do it"}]}


def test_kick_without_title_sends_empty_create(calls):
    stack_kick.kick("http://h:1", "pw", "do it")
    create, _ = calls
    assert json.loads(create.data) == {}


def test_build_prompt_comment_section():
    p = stack_kick.build_prompt("o/r", "3", "t", "b", "u",
                                comment="please also check the tests",
                                comment_author="josee")
    assert "Triggered by a comment from @josee" in p
    assert "please also check the tests" in p
    # no comment -> no section
    assert "Triggered by" not in stack_kick.build_prompt("o/r", "3", "t", "b", "u")


def test_main_writes_github_output(monkeypatch, calls, tmp_path, capsys):
    out = tmp_path / "github_output"
    for k, v in {"STACK_URL": "http://h:1", "STACK_PASSWORD": "pw",
                 "ISSUE_NUMBER": "7", "ISSUE_TITLE": "t", "ISSUE_URL": "u",
                 "REPO": "o/r", "GITHUB_OUTPUT": str(out)}.items():
        monkeypatch.setenv(k, v)
    for k in ("GITHUB_TOKEN", "GH_TOKEN"):
        monkeypatch.delenv(k, raising=False)
    assert stack_kick.main() == 0
    assert "session_id=ses_test" in out.read_text()
    assert "SESSION_ID=ses_test" in capsys.readouterr().out


def test_kick_rejects_idless_create(calls, monkeypatch):
    monkeypatch.setattr(stack_kick.urllib.request, "urlopen",
                        lambda req, timeout=0: FakeResponse({"weird": 1}))
    with pytest.raises(RuntimeError, match="no id"):
        stack_kick.kick("http://h:1", "pw", "p")


def test_done_reason_closed_issue(monkeypatch):
    monkeypatch.setattr(stack_kick, "gh_api",
                        lambda tok, path: {"state": "closed"})
    assert "closed" in stack_kick.done_reason("o/r", "5", "t")


def test_done_reason_ready_pr_blocks(monkeypatch):
    def fake(tok, path):
        if "/pulls?" in path:
            # "draft" pinned: without it a missing key would pass by accident
            return [{"html_url": "https://x/pr/9", "state": "open",
                     "draft": False}]
        return {"state": "open"}

    monkeypatch.setattr(stack_kick, "gh_api", fake)
    reason = stack_kick.done_reason("o/r", "5", "t")
    assert "https://x/pr/9" in reason and "ready for review" in reason


def test_done_reason_merged_behind_newer_draft_blocks(monkeypatch):
    """The guard is existential over EVERY attempt (AGENTS.md rule 9), not
    just the newest: a newer open draft (the current session's workbench)
    must not hide an older merged PR from the per_page=100 list."""
    def fake(tok, path):
        if "/pulls?" in path:
            assert "per_page=100" in path  # the whole list is scanned
            return [{"html_url": "https://x/pr/10", "state": "open",
                     "draft": True},
                    {"html_url": "https://x/pr/9", "state": "closed",
                     "merged_at": "2026-09-11T12:00:00Z"}]
        return {"state": "open"}

    monkeypatch.setattr(stack_kick, "gh_api", fake)
    reason = stack_kick.done_reason("o/r", "5", "t")
    assert "https://x/pr/9" in reason and "merged" in reason


def test_done_reason_open_draft_pr_does_not_block(monkeypatch):
    """ADR 0046: an open draft PR is the session's workbench under
    draft-first, not handled work - the done-guard must not block the
    re-kick (the in-flight guard, ADR 0040, owns the double-book
    window)."""
    def fake(tok, path):
        if "/pulls?" in path:
            return [{"html_url": "https://x/pr/9", "state": "open",
                     "draft": True}]
        return {"state": "open"}

    monkeypatch.setattr(stack_kick, "gh_api", fake)
    assert stack_kick.done_reason("o/r", "5", "t") is None


def test_done_reason_merged_pr_blocks(monkeypatch):
    def fake(tok, path):
        if "/pulls?" in path:
            return [{"html_url": "https://x/pr/9", "state": "closed",
                     "merged_at": "2026-09-11T12:00:00Z"}]
        return {"state": "open"}

    monkeypatch.setattr(stack_kick, "gh_api", fake)
    reason = stack_kick.done_reason("o/r", "5", "t")
    assert "https://x/pr/9" in reason and "merged" in reason


def test_done_reason_closed_unmerged_pr_does_not_block(monkeypatch):
    """A closed-unmerged PR is an abandoned attempt - a re-kick
    reconciles the branch (ADR 0046)."""
    def fake(tok, path):
        if "/pulls?" in path:
            return [{"html_url": "https://x/pr/9", "state": "closed",
                     "merged_at": None}]
        return {"state": "open"}

    monkeypatch.setattr(stack_kick, "gh_api", fake)
    assert stack_kick.done_reason("o/r", "5", "t") is None


def test_done_reason_none_when_open_and_no_pr(monkeypatch):
    def fake(tok, path):
        if "/pulls?" in path:
            return []
        return {"state": "open"}

    monkeypatch.setattr(stack_kick, "gh_api", fake)
    assert stack_kick.done_reason("o/r", "5", "t") is None


def test_main_skips_done_issues_with_exit_3(monkeypatch, calls, tmp_path, capsys):
    out = tmp_path / "github_output"
    for k, v in {"STACK_URL": "http://h:1", "STACK_PASSWORD": "pw",
                 "ISSUE_NUMBER": "7", "ISSUE_TITLE": "t", "ISSUE_URL": "u",
                 "REPO": "o/r", "GITHUB_TOKEN": "gh-tok",
                 "GITHUB_OUTPUT": str(out)}.items():
        monkeypatch.setenv(k, v)
    monkeypatch.setattr(stack_kick, "done_reason",
                        lambda r, n, t: "PR https://x/9 already exists (merged)")
    assert stack_kick.main() == stack_kick.SKIP_DONE
    assert "skip_reason=PR https://x/9" in out.read_text()
    assert calls == []  # a done issue is never re-kicked
    assert "SKIP" in capsys.readouterr().out


# --- In-flight guard (ADR 0040): a busy '#N:' session already holds the
# issue - a stray trigger must skip, not double-book (issue #33, run
# 34603739921: the agent's own plan comment re-kicked it) ---------------

def _route_sessions(monkeypatch, sessions, status):
    def fake_api(url, password, method, path, body=None):
        if path == "/session":
            return sessions
        if path == "/session/status":
            return status
        raise AssertionError(f"unexpected api call {method} {path}")

    monkeypatch.setattr(stack_kick, "api", fake_api)


def test_inflight_reason_busy_titled_session_blocks(monkeypatch):
    _route_sessions(monkeypatch,
                    [{"id": "s1", "title": "#7: fix the thing"},
                     {"id": "s2", "title": "#70: other issue"}],
                    {"s1": {"type": "busy"}, "s2": {"type": "busy"}})
    reason = stack_kick.inflight_reason("http://h:1", "pw", ("#7: ",))
    assert reason and "s1" in reason and "busy" in reason


def test_inflight_reason_idle_absent_or_foreign_proceeds(monkeypatch):
    # finished (idle) sessions never block a deliberate re-kick
    _route_sessions(monkeypatch, [{"id": "s1", "title": "#7: t"}],
                    {"s1": {"type": "idle"}})
    assert stack_kick.inflight_reason("http://h:1", "pw", ("#7: ",)) is None
    # prefix matches the exact issue only (#7 must not match #70)
    _route_sessions(monkeypatch, [{"id": "s2", "title": "#70: t"}],
                    {"s2": {"type": "busy"}})
    assert stack_kick.inflight_reason("http://h:1", "pw", ("#7: ",)) is None
    # odd payloads (degraded endpoints) proceed rather than block
    _route_sessions(monkeypatch, {}, {})
    assert stack_kick.inflight_reason("http://h:1", "pw", ("#7: ",)) is None


def test_inflight_reason_discussion_prefix_shapes(monkeypatch):
    # ADR 0043: the two discussion prefixes match their own title forms...
    prefixes = ("D#7: ", "D#7 elaborate: ")
    for title in ("D#7: some thread", "D#7 elaborate: some thread"):
        _route_sessions(monkeypatch, [{"id": "s1", "title": title}],
                        {"s1": {"type": "busy"}})
        reason = stack_kick.inflight_reason("http://h:1", "pw", prefixes,
                                            subject="discussion")
        assert reason and "s1" in reason and "discussion" in reason, title
    # ...but never a near-miss number (D#70 vs D#7) in either form
    for title in ("D#70: other", "D#70 elaborate: other"):
        _route_sessions(monkeypatch, [{"id": "s2", "title": title}],
                        {"s2": {"type": "busy"}})
        assert stack_kick.inflight_reason(
            "http://h:1", "pw", prefixes, subject="discussion") is None


def test_main_skips_inflight_issue_with_exit_3(monkeypatch, tmp_path, capsys):
    out = tmp_path / "github_output"
    for k, v in {"STACK_URL": "http://h:1", "STACK_PASSWORD": "pw",
                 "ISSUE_NUMBER": "7", "ISSUE_TITLE": "t", "ISSUE_URL": "u",
                 "REPO": "o/r", "GITHUB_OUTPUT": str(out)}.items():
        monkeypatch.setenv(k, v)
    for k in ("GITHUB_TOKEN", "GH_TOKEN"):
        monkeypatch.delenv(k, raising=False)  # done-guard off, guard still on
    _route_sessions(monkeypatch, [{"id": "s9", "title": "#7: t"}],
                    {"s9": {"type": "busy"}})
    kicked = []
    monkeypatch.setattr(stack_kick, "kick",
                        lambda *a, **k: kicked.append(a) or "ses_x")
    assert stack_kick.main() == stack_kick.SKIP_DONE
    assert kicked == []
    assert "skip_reason=session s9" in out.read_text()
    assert "SKIP" in capsys.readouterr().out


def test_main_inflight_check_failure_proceeds(monkeypatch, calls):
    for k, v in {"STACK_URL": "http://h:1/", "STACK_PASSWORD": "pw",
                 "ISSUE_NUMBER": "7", "ISSUE_TITLE": "t", "ISSUE_URL": "u",
                 "REPO": "o/r"}.items():
        monkeypatch.setenv(k, v)
    for k in ("GITHUB_TOKEN", "GH_TOKEN"):
        monkeypatch.delenv(k, raising=False)

    def boom(url, password, method, path, body=None):
        raise TimeoutError("stack down")

    monkeypatch.setattr(stack_kick, "api", boom)
    monkeypatch.setattr(stack_kick, "kick", lambda *a, **k: "ses_x")
    assert stack_kick.main() == 0  # the guard degrades, it never blocks


def test_main_missing_env_is_exit_2(monkeypatch, capsys):
    for k in ("STACK_URL", "STACK_PASSWORD", "ISSUE_NUMBER", "ISSUE_TITLE",
              "ISSUE_URL", "REPO", "GITHUB_TOKEN", "GH_TOKEN"):
        monkeypatch.delenv(k, raising=False)
    assert stack_kick.main() == 2
    assert "missing env" in capsys.readouterr().err


def test_main_happy_path(monkeypatch, calls):
    for k, v in {"STACK_URL": "http://h:1/", "STACK_PASSWORD": "pw",
                 "ISSUE_NUMBER": "7", "ISSUE_TITLE": "t", "ISSUE_URL": "u",
                 "REPO": "o/r"}.items():
        monkeypatch.setenv(k, v)
    for k in ("GITHUB_TOKEN", "GH_TOKEN"):
        monkeypatch.delenv(k, raising=False)  # done-guard off -> straight kick
    assert stack_kick.main() == 0
    # trailing slash stripped from STACK_URL
    assert calls[0].full_url.startswith("http://h:1/session")


# --- Discussion mode (ADR 0038): a /opencode discussion comment kicks a
# session that distills the whole thread into issues ---------------------


def _clear_issue_env(monkeypatch):
    for k in ("ISSUE_NUMBER", "ISSUE_TITLE", "ISSUE_URL", "ISSUE_BODY",
              "COMMENT_BODY", "COMMENT_AUTHOR"):
        monkeypatch.delenv(k, raising=False)


def test_build_discussion_prompt_carries_thread_and_mission():
    p = stack_kick.build_discussion_prompt(
        "o/r", "4", "MCP roadmap", "Let us plan MCP support",
        "https://x/d/4", [("alice", "what about mcp?"), ("bob", "later")])
    assert "#4" in p and "MCP roadmap" in p
    assert "Let us plan MCP support" in p
    assert "@alice" in p and "what about mcp?" in p and "@bob" in p
    # the mission: issues with plan / happy path / criteria of success
    assert "gh issue create" in p
    assert "Happy path" in p and "Criteria of success" in p
    # discussion kicks never branch or PR, and must not self-retrigger
    assert "agent/issue-" not in p and "Closes #" not in p
    assert "agent-task" in p  # named as the do-NOT-add label
    # the closing comment on the discussion: addDiscussionComment, never
    # addComment (discussions reject it - run 34602194185, ADR 0039)
    assert "addDiscussionComment" in p and "addComment(" not in p


def test_build_discussion_prompt_defaults_and_empty_thread():
    p = stack_kick.build_discussion_prompt("o/r", "1", "t", "", "u", [])
    assert "(no description)" in p and "(no comments yet)" in p


def test_build_discussion_prompt_truncates_thread():
    big = [("a", "x" * 5000) for _ in range(10)]
    p = stack_kick.build_discussion_prompt("o/r", "1", "t", "b", "u", big)
    assert len(p) < 5000 * 10
    assert "thread truncated" in p


def test_build_discussion_prompt_trigger_comment_section():
    # no-token manual kicks never see the thread; the triggering comment
    # still rides along like on issue kicks (ADR 0034).
    p = stack_kick.build_discussion_prompt(
        "o/r", "3", "t", "b", "u", [], comment="/opencode go",
        comment_author="josee")
    assert "Triggered by a comment from @josee" in p
    assert "/opencode go" in p


def test_fetch_discussion_comments_maps_authors_and_bodies(monkeypatch):
    monkeypatch.setattr(stack_kick, "gh_api", lambda tok, path: [
        {"body": "first", "user": {"login": "alice"}},
        {"body": "second", "user": {"login": "bob"}},
    ])
    assert stack_kick.fetch_discussion_comments("o/r", "3", "tok") == [
        ("alice", "first"), ("bob", "second")]


def test_fetch_discussion_comments_degrades_to_empty(monkeypatch, capsys):
    def boom(tok, path):
        raise RuntimeError("api down")

    monkeypatch.setattr(stack_kick, "gh_api", boom)
    assert stack_kick.fetch_discussion_comments("o/r", "3", "tok") == []
    assert "comment fetch failed" in capsys.readouterr().err


def test_main_discussion_mode_kicks_with_thread(monkeypatch, calls, tmp_path):
    out = tmp_path / "gh_out"
    for k, v in {"STACK_URL": "http://h:1", "STACK_PASSWORD": "pw",
                 "DISCUSSION_NUMBER": "7", "DISCUSSION_TITLE": "dt",
                 "DISCUSSION_URL": "u", "DISCUSSION_BODY": "db",
                 "REPO": "o/r", "GITHUB_TOKEN": "t",
                 "GITHUB_OUTPUT": str(out)}.items():
        monkeypatch.setenv(k, v)
    _clear_issue_env(monkeypatch)
    # the in-flight guard is covered by its own tests; here it would only
    # add two probe calls ahead of the kick
    monkeypatch.setattr(stack_kick, "inflight_guard", lambda *a, **k: None)
    monkeypatch.setattr(stack_kick, "fetch_discussion_comments",
                        lambda r, n, t: [("alice", "hi")])
    # no done-guard for discussions (ADR 0038): every /opencode is deliberate
    def no_guard(*a):
        raise AssertionError("done-guard must not run for discussions")

    monkeypatch.setattr(stack_kick, "done_reason", no_guard)
    assert stack_kick.main() == 0
    create, prompt = calls
    assert json.loads(create.data) == {"title": "D#7: dt"}  # ADR 0034 style
    text = json.loads(prompt.data)["parts"][0]["text"]
    assert "hi" in text and "db" in text
    assert "session_id=ses_test" in out.read_text()


def test_main_discussion_mode_without_token_uses_payload_only(
        monkeypatch, calls, capsys):
    for k, v in {"STACK_URL": "http://h:1", "STACK_PASSWORD": "pw",
                 "DISCUSSION_NUMBER": "7", "DISCUSSION_TITLE": "dt",
                 "DISCUSSION_URL": "u", "REPO": "o/r"}.items():
        monkeypatch.setenv(k, v)
    _clear_issue_env(monkeypatch)
    monkeypatch.setattr(stack_kick, "inflight_guard", lambda *a, **k: None)
    for k in ("GITHUB_TOKEN", "GH_TOKEN", "DISCUSSION_BODY",
              "GITHUB_OUTPUT"):
        monkeypatch.delenv(k, raising=False)

    def no_api(*a):
        raise AssertionError("no token -> the thread is never fetched")

    monkeypatch.setattr(stack_kick, "gh_api", no_api)
    assert stack_kick.main() == 0
    text = json.loads(calls[1].data)["parts"][0]["text"]
    assert "(no description)" in text
    assert "thread" in capsys.readouterr().err


def test_main_discussion_skips_inflight_with_exit_3(monkeypatch, calls,
                                                    tmp_path, capsys):
    """ADR 0043: with the shared olgam4 identity allowed to trigger, a busy
    'D#N ' session is the bound on self-kick loops - serial, never
    parallel. The guard runs before the thread fetch (a skipped kick
    renders no prompt)."""
    out = tmp_path / "gh_out"
    for k, v in {"STACK_URL": "http://h:1", "STACK_PASSWORD": "pw",
                 "DISCUSSION_NUMBER": "7", "DISCUSSION_TITLE": "dt",
                 "DISCUSSION_URL": "u", "REPO": "o/r", "GITHUB_TOKEN": "t",
                 "GITHUB_OUTPUT": str(out)}.items():
        monkeypatch.setenv(k, v)
    _clear_issue_env(monkeypatch)
    _route_sessions(monkeypatch, [{"id": "s9", "title": "D#7: dt"}],
                    {"s9": {"type": "busy"}})

    def no_fetch(*a):
        raise AssertionError("a skipped kick never fetches the thread")

    monkeypatch.setattr(stack_kick, "fetch_discussion_comments", no_fetch)
    assert stack_kick.main() == stack_kick.SKIP_DONE
    assert calls == []  # no session created, no prompt queued
    assert "skip_reason=session s9" in out.read_text()
    assert "SKIP" in capsys.readouterr().out


def test_main_discussion_inflight_failure_proceeds(monkeypatch, calls):
    for k, v in {"STACK_URL": "http://h:1", "STACK_PASSWORD": "pw",
                 "DISCUSSION_NUMBER": "7", "DISCUSSION_TITLE": "dt",
                 "DISCUSSION_URL": "u", "REPO": "o/r"}.items():
        monkeypatch.setenv(k, v)
    _clear_issue_env(monkeypatch)
    for k in ("GITHUB_TOKEN", "GH_TOKEN", "DISCUSSION_BODY",
              "GITHUB_OUTPUT"):
        monkeypatch.delenv(k, raising=False)

    def boom(url, password, method, path, body=None):
        raise TimeoutError("stack down")

    monkeypatch.setattr(stack_kick, "api", boom)
    monkeypatch.setattr(stack_kick, "kick", lambda *a, **k: "ses_x")
    assert stack_kick.main() == 0  # the guard degrades, it never blocks


def test_all_prompts_forbid_leading_command_comments():
    """ADR 0043: while olgam4 may trigger, the agent must never emit a
    comment STARTING with /opencode or /elaborate - the one remaining
    self-kick vector. Every prompt template must carry the hygiene line."""
    builders = [
        stack_kick.build_prompt("o/r", "1", "t", "b", "u"),
        stack_kick.build_discussion_prompt("o/r", "1", "t", "b", "u", []),
        stack_kick.build_elaborate_prompt("o/r", "1", "t", "b", "u", []),
    ]
    for p in builders:
        assert "NEVER start a GitHub comment" in p


# --- Elaborate mode (issue #33 / ADR 0041): a /elaborate discussion
# comment kicks a session that posts five persona POV comments ---------

def test_build_elaborate_prompt_carries_thread_personas_and_posting():
    p = stack_kick.build_elaborate_prompt(
        "o/r", "4", "MCP roadmap", "Let us plan MCP support",
        "https://x/d/4", [("alice", "what about mcp?"), ("bob", "later")])
    assert "#4" in p and "MCP roadmap" in p
    assert "Let us plan MCP support" in p and "https://x/d/4" in p
    assert "@alice" in p and "what about mcp?" in p and "@bob" in p
    # all five personas are dispatched by agent-name, with role-titles
    assert len(stack_kick.PERSONAS) == 5
    for name, role in stack_kick.PERSONAS:
        assert name in p and role in p
    # the session dispatches the personas; it does not write the POVs
    assert "task subagent" in p
    # attribution: every comment starts with **<Role> POV**
    assert "**<Role> POV**" in p and "**CTO POV**" in p
    # posting is GraphQL addDiscussionComment on the discussion node id
    # (0038 two-step: REST for the node id, GraphQL for the comment;
    # discussions reject addComment - ADR 0039)
    assert "gh api repos/o/r/discussions/4 --jq .node_id" in p
    assert "addDiscussionComment" in p and "addComment(" not in p
    # degradation posture, same as 0038
    assert "missing token permission" in p


def test_build_elaborate_prompt_never_branches_or_creates_issues():
    """The deliverable is the five persona comments - no branch, no PR,
    no issues, no labels. Issue creation is distill's job (ADR 0038), so
    none of its machinery may leak into the elaborate prompt."""
    p = stack_kick.build_elaborate_prompt("o/r", "4", "t", "b", "u", [])
    assert "agent/issue-" not in p
    assert "gh issue create" not in p
    assert "do not branch" in p


def test_build_elaborate_prompt_trigger_comment_section():
    # same as distill: a token-less manual kick still sees the triggering
    # comment.
    p = stack_kick.build_elaborate_prompt(
        "o/r", "3", "t", "b", "u", [], comment="/elaborate go",
        comment_author="josee")
    assert "Triggered by a comment from @josee" in p
    assert "/elaborate go" in p


def test_persona_roster_files_exist_and_are_subagents():
    """Every persona the elaborate prompt dispatches must exist in the
    stack's agent roster as a subagent - a missing or mis-moded file
    means the kicked session names an agent that cannot be dispatched.
    Personas also stay read-only (ADR 0041): the envelope is
    allow-by-default, so edit/bash/task/webfetch must each be an explicit
    deny - a missing key silently inherits allow."""
    agents = Path(__file__).parent.parent / "agent-config/agents"
    for name, _role in stack_kick.PERSONAS:
        f = agents / f"{name}.md"
        assert f.is_file(), f"missing persona agent file: {f.name}"
        parts = f.read_text().split("---", 2)
        assert len(parts) == 3, f"{f.name}: missing frontmatter"
        front = yaml.safe_load(parts[1])
        assert front.get("mode") == "subagent", f"{f.name}: not a subagent"
        for key in ("edit", "bash", "task", "webfetch"):
            assert front["permission"].get(key) == "deny", (
                f"{f.name}: permission.{key} must be 'deny' "
                f"(read-only persona, ADR 0041), "
                f"got {front['permission'].get(key)!r}")


def test_elaborate_prompt_carries_thread_roster_and_posting_rules():
    p = stack_kick.build_elaborate_prompt(
        "o/r", "4", "MCP roadmap", "Let us plan MCP support",
        "https://x/d/4", [("alice", "what about mcp?"), ("bob", "later")])
    assert "#4" in p and "MCP roadmap" in p and "Let us plan MCP support" in p
    assert "@alice" in p and "what about mcp?" in p
    for agent, role in stack_kick.PERSONAS:
        assert agent in p and role in p  # the session must know the roster
    assert "task" in p.lower()  # one task subagent per persona
    assert "addDiscussionComment" in p  # GraphQL posting instructions
    assert "**Domain expert POV**" in p  # attribution format, verbatim
    # elaborate kicks never branch/PR and never create issues
    assert "agent/issue-" not in p and "gh issue create" not in p


def test_elaborate_prompt_defaults_and_trigger_comment():
    p = stack_kick.build_elaborate_prompt("o/r", "1", "t", "", "u", [],
                                            comment="/elaborate",
                                            comment_author="josee")
    assert "(no description)" in p and "(no comments yet)" in p
    assert "Triggered by a comment from @josee" in p


def test_persona_roster_matches_agent_files():
    agents = Path(__file__).parent.parent / "agent-config/agents"
    on_disk = {p.stem for p in agents.glob("*.md")}
    rostered = {name for name, _ in stack_kick.PERSONAS}
    assert rostered <= on_disk  # every rostered persona exists as a file


def test_main_elaborate_command_routes_and_titles(monkeypatch, calls, tmp_path):
    out = tmp_path / "gh_out"
    for k, v in {"STACK_URL": "http://h:1", "STACK_PASSWORD": "pw",
                   "DISCUSSION_NUMBER": "7", "DISCUSSION_TITLE": "dt",
                   "DISCUSSION_URL": "u", "DISCUSSION_BODY": "db",
                   "DISCUSSION_COMMAND": "elaborate",
                   "REPO": "o/r", "GITHUB_TOKEN": "t",
                   "GITHUB_OUTPUT": str(out)}.items():
        monkeypatch.setenv(k, v)
    _clear_issue_env(monkeypatch)
    monkeypatch.setattr(stack_kick, "inflight_guard", lambda *a, **k: None)
    monkeypatch.setattr(stack_kick, "fetch_discussion_comments",
                        lambda r, n, t: [("alice", "hi")])
    assert stack_kick.main() == 0
    create, prompt = calls
    assert json.loads(create.data) == {"title": "D#7 elaborate: dt"}
    text = json.loads(prompt.data)["parts"][0]["text"]
    assert "addDiscussionComment" in text and "hi" in text


@pytest.mark.parametrize("command", ["distill", "Elaborate", "elaborate2"])
def test_main_discussion_non_elaborate_command_stays_distill(
        command, monkeypatch, calls, tmp_path):
    """Routing is exact-match on "elaborate" (ADR 0041): any other
    DISCUSSION_COMMAND value - including near-misses - keeps the distill
    path (ADR 0038), never the persona roster."""
    out = tmp_path / "gh_out"
    for k, v in {"STACK_URL": "http://h:1", "STACK_PASSWORD": "pw",
                 "DISCUSSION_NUMBER": "7", "DISCUSSION_TITLE": "dt",
                 "DISCUSSION_URL": "u", "DISCUSSION_BODY": "db",
                 "DISCUSSION_COMMAND": command,
                 "REPO": "o/r", "GITHUB_TOKEN": "t",
                 "GITHUB_OUTPUT": str(out)}.items():
        monkeypatch.setenv(k, v)
    _clear_issue_env(monkeypatch)
    monkeypatch.setattr(stack_kick, "inflight_guard", lambda *a, **k: None)
    monkeypatch.setattr(stack_kick, "fetch_discussion_comments",
                        lambda r, n, t: [("alice", "hi")])
    assert stack_kick.main() == 0
    create, prompt = calls
    # distill: ADR 0034-style title, issue-creation mission, no roster
    assert json.loads(create.data) == {"title": "D#7: dt"}
    text = json.loads(prompt.data)["parts"][0]["text"]
    assert "gh issue create" in text
    for name, _role in stack_kick.PERSONAS:
        assert name not in text, f"distill prompt leaked persona {name}"

# --- Multi-role plan review (issue #34 / ADR 0042): the issue kick's
# plan phase fans the draft plan out to the shared persona roster ------


def test_build_prompt_mandates_multi_role_plan_review():
    """Issue #34 / ADR 0042: the plan phase fans out to one task subagent
    per persona - the shared /elaborate roster (ADR 0041), one definition,
    two consumers - and the posted plan shows each role's input (or its
    explicit no-objection) BEFORE implementation starts."""
    p = stack_kick.build_prompt("o/r", "12", "Fix the thing", "body", "u")
    assert stack_kick.PERSONAS  # a gutted roster fails, never vacuously passes
    for name, role in stack_kick.PERSONAS:
        assert name in p and role in p
    assert "task` subagent per persona" in p  # the fan-out
    assert "## Role review" in p  # per-role section in the posted plan
    assert "no-objection" in p
    # role review refines the draft plan; it never implements
    assert "BEFORE writing code" in p


# --- Repo-declared verify gate (ADR 0045): the verify step interpolates
# the gate the repo declares via the veggies-verify-gate marker in its
# agent-instruction file - never a hardcoded command --------------------


def test_prompt_hardcodes_no_repo_gate():
    assert "mask ci" not in stack_kick.PROMPT_TEMPLATE
    assert "mask ci" not in (stack_kick.__doc__ or "")
    assert "{verify_step}" in stack_kick.PROMPT_TEMPLATE


def test_build_prompt_verify_gate_fallback():
    p = stack_kick.build_prompt("o/r", "12", "t", "b", "u", verify_gate=None)
    assert "veggies-verify-gate" in p  # names the missing marker
    assert "Claim only what you actually ran." in p
    p = stack_kick.build_prompt("o/r", "12", "t", "b", "u",
                                verify_gate="npm test -- --changed")
    assert "`npm test -- --changed`" in p  # the gate, in backticks
    assert "Claim only what you actually ran." in p
    # the vendored template stays repo-neutral: scoping is conditional on
    # what the declaration declares, never this repo's policy asserted
    assert "follow the scoping the declaration declares" in p
    assert "the security hooks it names" not in p


def test_declared_verify_gate_from_agents_md(tmp_path):
    (tmp_path / "AGENTS.md").write_text(
        "prose\n<!-- veggies-verify-gate: make test -->\nmore\n")
    assert stack_kick.declared_verify_gate(tmp_path) == "make test"


def test_declared_verify_gate_sloppy_whitespace(tmp_path):
    (tmp_path / "AGENTS.md").write_text(
        "<!--   veggies-verify-gate:   make test   -->")
    assert stack_kick.declared_verify_gate(tmp_path) == "make test"


def test_declared_verify_gate_empty_command_is_none(tmp_path):
    (tmp_path / "AGENTS.md").write_text("<!-- veggies-verify-gate: -->")
    assert stack_kick.declared_verify_gate(tmp_path) is None


def test_declared_verify_gate_no_marker_is_none(tmp_path):
    (tmp_path / "AGENTS.md").write_text("no marker here\n")
    assert stack_kick.declared_verify_gate(tmp_path) is None


def test_declared_verify_gate_falls_back_to_claude_md(tmp_path):
    (tmp_path / "CLAUDE.md").write_text("<!-- veggies-verify-gate: tox -q -->")
    assert stack_kick.declared_verify_gate(tmp_path) == "tox -q"


def test_declared_verify_gate_agents_md_wins(tmp_path):
    (tmp_path / "AGENTS.md").write_text("<!-- veggies-verify-gate: a -->")
    (tmp_path / "CLAUDE.md").write_text("<!-- veggies-verify-gate: b -->")
    assert stack_kick.declared_verify_gate(tmp_path) == "a"


def test_declared_verify_gate_ignores_other_files(tmp_path):
    (tmp_path / "README.md").write_text("<!-- veggies-verify-gate: x -->")
    assert stack_kick.declared_verify_gate(tmp_path) is None


def test_declared_verify_gate_inline_marker_does_not_parse(tmp_path):
    # the marker must be alone on its line: a documented example quoted
    # inside prose (e.g. in backticks) is NOT a declaration
    (tmp_path / "AGENTS.md").write_text(
        "Declare it as `<!-- veggies-verify-gate: make test -->` in prose.\n")
    assert stack_kick.declared_verify_gate(tmp_path) is None


def test_declared_verify_gate_non_utf8_file_is_skipped(tmp_path):
    # an undecodable file degrades like an unreadable one - it never
    # blocks the kick (ADR 0045)
    (tmp_path / "AGENTS.md").write_bytes(b"\xff\xfe invalid \x00")
    assert stack_kick.declared_verify_gate(tmp_path) is None
    (tmp_path / "CLAUDE.md").write_text("<!-- veggies-verify-gate: tox -->")
    assert stack_kick.declared_verify_gate(tmp_path) == "tox"


def test_this_repo_declares_its_kick_gate():
    """LOAD-BEARING (ADR 0045): pins the day-one byte-for-byte criterion -
    the declared command is identical to the previously hardcoded one -
    AND the prose<->marker lockstep, so a future rule-3 edit that drops
    or rewords either half fails loudly here instead of silently
    degrading every kick to the fallback."""
    gate = stack_kick.declared_verify_gate()  # no arg: the script-root anchor
    assert gate == "mask ci"
    agents = (Path(stack_kick.__file__).resolve().parents[1]
              / "AGENTS.md").read_text(encoding="utf-8")
    prose = re.sub(r"<!--.*?-->", "", agents, flags=re.DOTALL)
    assert gate in prose


def test_main_interpolates_the_declared_gate(monkeypatch, tmp_path, capsys):
    out = tmp_path / "gh_out"
    for k, v in {"STACK_URL": "http://h:1", "STACK_PASSWORD": "pw",
                 "ISSUE_NUMBER": "7", "ISSUE_TITLE": "t", "ISSUE_URL": "u",
                 "REPO": "o/r", "GITHUB_OUTPUT": str(out)}.items():
        monkeypatch.setenv(k, v)
    for k in ("GITHUB_TOKEN", "GH_TOKEN"):
        monkeypatch.delenv(k, raising=False)  # done-guard off
    # the in-flight guard is covered by its own tests
    monkeypatch.setattr(stack_kick, "inflight_guard", lambda *a, **k: None)
    monkeypatch.setattr(stack_kick, "declared_verify_gate",
                        lambda *a, **k: "make check")
    kicked = []
    monkeypatch.setattr(stack_kick, "kick",
                        lambda *a, **k: kicked.append(a[2]) or "ses_x")
    assert stack_kick.main() == 0
    assert "make check" in kicked[0]  # the prompt carries the gate
    assert "VERIFY_GATE=make check" in capsys.readouterr().out
    assert "verify_gate=make check" in out.read_text()


def test_main_echoes_the_no_gate_sentinel(monkeypatch, tmp_path, capsys):
    out = tmp_path / "gh_out"
    for k, v in {"STACK_URL": "http://h:1", "STACK_PASSWORD": "pw",
                 "ISSUE_NUMBER": "7", "ISSUE_TITLE": "t", "ISSUE_URL": "u",
                 "REPO": "o/r", "GITHUB_OUTPUT": str(out)}.items():
        monkeypatch.setenv(k, v)
    for k in ("GITHUB_TOKEN", "GH_TOKEN"):
        monkeypatch.delenv(k, raising=False)  # done-guard off
    monkeypatch.setattr(stack_kick, "inflight_guard", lambda *a, **k: None)
    monkeypatch.setattr(stack_kick, "declared_verify_gate",
                        lambda *a, **k: None)
    kicked = []
    monkeypatch.setattr(stack_kick, "kick",
                        lambda *a, **k: kicked.append(a[2]) or "ses_x")
    assert stack_kick.main() == 0
    assert "veggies-verify-gate" in kicked[0]  # the missing-marker fallback
    assert ("VERIFY_GATE=(none declared - agent-instruction prose governs)"
            in capsys.readouterr().out)
    assert ("verify_gate=(none declared - agent-instruction prose governs)"
            in out.read_text())


def test_main_gate_round_trips_through_github_output(monkeypatch, tmp_path,
                                                     capsys):
    # the real gate carries spaces and `=` - the output file must parse
    # back to the exact command
    out = tmp_path / "gh_out"
    for k, v in {"STACK_URL": "http://h:1", "STACK_PASSWORD": "pw",
                 "ISSUE_NUMBER": "7", "ISSUE_TITLE": "t", "ISSUE_URL": "u",
                 "REPO": "o/r", "GITHUB_OUTPUT": str(out)}.items():
        monkeypatch.setenv(k, v)
    for k in ("GITHUB_TOKEN", "GH_TOKEN"):
        monkeypatch.delenv(k, raising=False)  # done-guard off
    monkeypatch.setattr(stack_kick, "inflight_guard", lambda *a, **k: None)
    monkeypatch.setattr(stack_kick, "declared_verify_gate",
                        lambda *a, **k: "mask ci")
    monkeypatch.setattr(stack_kick, "kick", lambda *a, **k: "ses_x")
    assert stack_kick.main() == 0
    kv = dict(l.split("=", 1) for l in out.read_text().splitlines())
    assert kv["verify_gate"] == "mask ci"


def test_main_echoes_gate_even_when_kick_fails(monkeypatch, tmp_path, capsys):
    # ADR 0045: the gate is echoed per kick, success or failure - a typo'd
    # marker on a broken stack is still a visible event
    out = tmp_path / "gh_out"
    for k, v in {"STACK_URL": "http://h:1", "STACK_PASSWORD": "pw",
                 "ISSUE_NUMBER": "7", "ISSUE_TITLE": "t", "ISSUE_URL": "u",
                 "REPO": "o/r", "GITHUB_OUTPUT": str(out)}.items():
        monkeypatch.setenv(k, v)
    for k in ("GITHUB_TOKEN", "GH_TOKEN"):
        monkeypatch.delenv(k, raising=False)  # done-guard off
    monkeypatch.setattr(stack_kick, "inflight_guard", lambda *a, **k: None)
    monkeypatch.setattr(stack_kick, "declared_verify_gate",
                        lambda *a, **k: "make check")

    def boom(*a, **k):
        raise RuntimeError("stack down")

    monkeypatch.setattr(stack_kick, "kick", boom)
    assert stack_kick.main() == 1
    assert "VERIFY_GATE=make check" in capsys.readouterr().out
    assert "verify_gate=make check" in out.read_text()
# --- No-ask gate (issue #54 / ADR 0044): refuse to kick a repo whose
# project-tier opencode config reintroduces `ask` (the ADR 0031 park) ---

def test_permission_gate_refuses_a_project_tier_ask(tmp_path, monkeypatch):
    """Issue #54 / ADR 0044: a project-tier `ask` in the checked-out repo
    reintroduces the park ADR 0031 bans - refuse the kick, name the file."""
    (tmp_path / ".opencode").mkdir()
    (tmp_path / ".opencode/opencode.json").write_text(
        json.dumps({"permission": {"edit": "ask"}}))
    monkeypatch.chdir(tmp_path)
    reason = stack_kick.permission_gate_reason()
    assert reason is not None
    assert "permission.edit" in reason and ".opencode" in reason


def test_permission_gate_passes_a_clean_tier(tmp_path, monkeypatch):
    (tmp_path / "opencode.json").write_text(
        json.dumps({"permission": {"edit": "deny"}}))
    monkeypatch.chdir(tmp_path)
    assert stack_kick.permission_gate_reason() is None
    assert stack_kick.permission_gate() is None


def test_permission_gate_degrades_on_scanner_error(monkeypatch):
    # Degrade-to-proceed, like the done-guard: a scanner hiccup must never
    # block a deliberate kick. (Broad except: OSError, UnicodeDecodeError,
    # anything - only a verified `ask` blocks.)
    def boom(_):
        raise OSError("disk went away")
    monkeypatch.setattr(stack_kick.permission_envelope,
                        "scan_project_tier", boom)
    assert stack_kick.permission_gate_reason() is None


def test_permission_gate_blocks_the_kick(tmp_path, monkeypatch, calls):
    """End to end through main(): a violation exits 3 and creates no
    session (the workflow's rc==3 skip-comment step reports it)."""
    (tmp_path / "opencode.json").write_text(
        json.dumps({"permission": {"edit": "ask"}}))
    monkeypatch.chdir(tmp_path)
    for k, v in {"STACK_URL": "http://h:1", "STACK_PASSWORD": "pw",
                 "REPO": "o/r", "ISSUE_NUMBER": "5", "ISSUE_TITLE": "t",
                 "ISSUE_URL": "u"}.items():
        monkeypatch.setenv(k, v)
    for k in ("GITHUB_TOKEN", "GH_TOKEN"):
        monkeypatch.delenv(k, raising=False)  # done-guard off; gate still on
    # the gate runs after the in-flight guard; stubbing it keeps the fake
    # urlopen quiet so `calls` proves no session was created
    monkeypatch.setattr(stack_kick, "inflight_guard", lambda *a, **k: None)
    assert stack_kick.main() == 3
    assert calls == []  # no session was created


def test_permission_gate_blocks_a_discussion_kick(tmp_path, monkeypatch, calls):
    """The gate covers discussion mode too (main_discussion)."""
    (tmp_path / "opencode.json").write_text(
        json.dumps({"permission": {"edit": "ask"}}))
    for k, v in {"STACK_URL": "http://h:1", "STACK_PASSWORD": "pw",
                 "REPO": "o/r", "DISCUSSION_NUMBER": "7",
                 "DISCUSSION_TITLE": "t", "DISCUSSION_URL": "u"}.items():
        monkeypatch.setenv(k, v)
    monkeypatch.setattr(stack_kick, "inflight_guard", lambda *a, **k: None)
    assert stack_kick.main() == 3
    assert calls == []


def test_skip_reason_output_is_newline_safe(tmp_path, monkeypatch):
    # A newline in a filename/JSON key must never corrupt the key=value
    # stream the workflow reads skip_reason back from.
    out = tmp_path / "gh_out"
    monkeypatch.setenv("GITHUB_OUTPUT", str(out))
    assert stack_kick.skip("a\nb") == stack_kick.SKIP_DONE
    assert out.read_text().splitlines() == ["skip_reason=a // b"]
