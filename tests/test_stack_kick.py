"""Tests for scripts/stack_kick.py - the issue->session kick (ADR 0033)."""

import importlib.util
import io
import json
from pathlib import Path

import pytest

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


def test_build_prompt_contains_issue_and_rules():
    p = stack_kick.build_prompt("o/r", "12", "Fix the thing",
                                "Some body", "https://x/12")
    assert "#12" in p and "Fix the thing" in p and "Some body" in p
    assert "agent/issue-12" in p and "Closes #12" in p
    assert "mask ci" in p


def test_build_prompt_mandates_the_pipeline():
    """Issue #26 / ADR 0036: a kicked session must run the full pipeline -
    plan first, subagent execution, adversarial review, verified checks -
    not just dive into code."""
    p = stack_kick.build_prompt("o/r", "12", "Fix the thing", "body", "u")
    # 1. plan first, posted back to the issue for human review
    assert "writing-plans" in p
    assert f"gh issue comment 12" in p
    # 2. subagent execution, not a solo main loop
    assert "subagent" in p
    # 3. adversarial review of the diff (the vendored different-model
    # subagent) before pushing
    assert "adversarial-review" in p
    # 4. verified claims only
    assert "mask ci" in p


def test_build_prompt_mandates_a_per_session_worktree():
    """Issue #27 / ADR 0036: kicked sessions share one clone at /workspace,
    so the prompt must move each session into its own git worktree before
    any work - and keep the shared checkout read-only for that session."""
    p = stack_kick.build_prompt("o/r", "12", "Fix the thing", "body", "u")
    assert ("git -C /workspace worktree add -B agent/issue-12 "
            "/workspace/.veggies/wt/issue-12 origin/main") in p
    assert ".git/info/exclude" in p  # the shared checkout stays clean
    assert "the shared checkout at /workspace itself is read-only to you" in p


def test_build_prompt_truncates_and_defaults():
    p = stack_kick.build_prompt("o/r", "1", "t", "x" * 9000, "u")
    assert len(p) < 9000
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


def test_done_reason_existing_agent_pr(monkeypatch):
    def fake(tok, path):
        if "/pulls?" in path:
            return [{"html_url": "https://x/pr/9", "state": "open"}]
        return {"state": "open"}

    monkeypatch.setattr(stack_kick, "gh_api", fake)
    reason = stack_kick.done_reason("o/r", "5", "t")
    assert "https://x/pr/9" in reason and "open" in reason


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
