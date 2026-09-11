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


def test_build_prompt_truncates_and_defaults():
    p = stack_kick.build_prompt("o/r", "1", "t", "x" * 9000, "u")
    assert len(p) < 9000
    assert "(no description)" in stack_kick.build_prompt("o/r", "1", "t", "", "u")


def test_kick_creates_session_then_queues_prompt(calls):
    sid = stack_kick.kick("http://h:1", "pw", "do it")
    assert sid == "ses_test"
    create, prompt = calls
    assert create.full_url == "http://h:1/session?directory=/workspace"
    assert create.get_method() == "POST"
    # basic auth header carries opencode:<password>
    import base64
    assert create.headers["Authorization"] == "Basic " + base64.b64encode(
        b"opencode:pw").decode()
    assert prompt.full_url == \
        "http://h:1/session/ses_test/prompt_async?directory=/workspace"
    body = json.loads(prompt.data)
    assert body == {"parts": [{"type": "text", "text": "do it"}]}


def test_kick_rejects_idless_create(calls, monkeypatch):
    monkeypatch.setattr(stack_kick.urllib.request, "urlopen",
                        lambda req, timeout=0: FakeResponse({"weird": 1}))
    with pytest.raises(RuntimeError, match="no id"):
        stack_kick.kick("http://h:1", "pw", "p")


def test_main_missing_env_is_exit_2(monkeypatch, capsys):
    for k in ("STACK_URL", "STACK_PASSWORD", "ISSUE_NUMBER", "ISSUE_TITLE",
              "ISSUE_URL", "REPO"):
        monkeypatch.delenv(k, raising=False)
    assert stack_kick.main() == 2
    assert "missing env" in capsys.readouterr().err


def test_main_happy_path(monkeypatch, calls):
    for k, v in {"STACK_URL": "http://h:1/", "STACK_PASSWORD": "pw",
                 "ISSUE_NUMBER": "7", "ISSUE_TITLE": "t", "ISSUE_URL": "u",
                 "REPO": "o/r"}.items():
        monkeypatch.setenv(k, v)
    assert stack_kick.main() == 0
    # trailing slash stripped from STACK_URL
    assert calls[0].full_url.startswith("http://h:1/session")
