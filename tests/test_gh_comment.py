"""Tests for scripts/gh_comment.py - the verify-before-retry comment
helper (issue #101): GitHub may execute a comment mutation and then fail
the response (run 34706580396), so errors[] is checked against ground
truth before any retry."""

import importlib.util
import io
import json
import urllib.error
from pathlib import Path

import pytest

_spec = importlib.util.spec_from_file_location(
    "gh_comment", Path(__file__).parent.parent / "scripts/gh_comment.py")
gh_comment = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(gh_comment)

RUN_URL = "https://github.com/o/r/actions/runs/12345"


class FakeResponse:
    def __init__(self, payload):
        self.payload = payload

    def read(self):
        return json.dumps(self.payload).encode()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class _Scripted:
    """Recorded urlopen requests (.requests) plus the queue of per-call
    outcomes (.outcomes): a dict is returned as the GraphQL payload, an
    exception instance is raised."""

    def __init__(self):
        self.requests = []
        self.outcomes = []


@pytest.fixture()
def calls(monkeypatch):
    """Scripted urlopen: every request is recorded; each call consumes
    one entry of .outcomes."""
    script = _Scripted()

    def fake_urlopen(req, timeout=0):
        script.requests.append(req)
        outcome = script.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return FakeResponse(outcome)

    monkeypatch.setattr(gh_comment.urllib.request, "urlopen", fake_urlopen)
    return script


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    """RETRY_DELAY must never slow the suite."""
    monkeypatch.setattr(gh_comment.time, "sleep", lambda seconds: None)


def _errors(message="Something went wrong while executing your query"):
    return {"errors": [{"message": message}]}


def _clean(mutation_field="addComment"):
    return {"data": {mutation_field: {"clientMutationId": "x"}}}


def _comments(*bodies):
    return {"data": {"node": {"comments": {
        "nodes": [{"body": b} for b in bodies]}}}}


def _queries(script):
    return [json.loads(r.data)["query"] for r in script.requests]


def test_post_clean_success(calls):
    body = f"plan posted\n{RUN_URL}"
    calls.outcomes[:] = [_clean()]
    assert gh_comment.post("t0ken", "issue", "I_abc", body) == "posted"
    assert len(calls.requests) == 1
    req = calls.requests[0]
    assert req.full_url == "https://api.github.com/graphql"
    assert req.headers["Authorization"] == "Bearer t0ken"
    payload = json.loads(req.data)
    # the kind-correct mutation, with the body as a variable
    assert payload["query"] == gh_comment.MUTATION["issue"]
    assert "addComment(" in payload["query"]
    assert payload["variables"] == {"id": "I_abc", "body": body}


def test_errors_but_comment_landed(calls, capsys):
    """The #101 case (run 34706580396): the mutation executed but the
    response came back errors[] - the verify query finds the comment by
    its run-URL marker, so there is NO retry and the step stays green."""
    body = f"on it\n{RUN_URL}\n"
    calls.outcomes[:] = [
        _errors(),
        _comments("someone else's comment",
                  f"on it\n{RUN_URL}"),  # GitHub trimmed the newline
    ]
    assert gh_comment.post("t0ken", "issue", "I_abc", body) == \
        "landed-after-errors"
    assert len(calls.requests) == 2  # mutation + verify, NO retry
    assert _queries(calls) == [gh_comment.MUTATION["issue"],
                               gh_comment.VERIFY_QUERY["issue"]]
    # the raw error was echoed, not swallowed
    assert "Something went wrong" in capsys.readouterr().err


def test_errors_then_retry_succeeds(calls):
    calls.outcomes[:] = [_errors(), _comments(), _clean()]
    assert gh_comment.post("t0ken", "issue", "I_abc",
                           f"on it\n{RUN_URL}") == "posted-on-retry"
    assert len(calls.requests) == 3
    assert _queries(calls) == [gh_comment.MUTATION["issue"],
                               gh_comment.VERIFY_QUERY["issue"],
                               gh_comment.MUTATION["issue"]]


def test_double_failure_exits_1(calls, capsys):
    calls.outcomes[:] = [_errors("boom-1"), _comments(),
                         _errors("boom-2"), _comments()]
    with pytest.raises(SystemExit) as exc:
        gh_comment.post("t0ken", "issue", "I_abc", f"on it\n{RUN_URL}")
    assert exc.value.code == 1
    assert len(calls.requests) == 4  # mutation, verify, retry, verify
    err = capsys.readouterr().err
    assert "boom-1" in err and "boom-2" in err


def test_transport_error_path(calls):
    """An HTTPError on the mutation is the same situation as errors[]:
    the write may have landed - verify before retrying."""
    calls.outcomes[:] = [
        urllib.error.HTTPError("https://api.github.com/graphql", 502,
                               "bad gateway", None, None),
        _comments(f"on it\n{RUN_URL}"),
    ]
    assert gh_comment.post("t0ken", "issue", "I_abc",
                           f"on it\n{RUN_URL}") == "landed-after-errors"
    assert len(calls.requests) == 2


def test_mutation_and_verify_selection_per_kind(calls):
    # dict level: each kind references its own GraphQL field and type
    assert gh_comment.MUTATION["issue"] != gh_comment.MUTATION["discussion"]
    assert "addComment(" in gh_comment.MUTATION["issue"]
    assert "addDiscussionComment(" not in gh_comment.MUTATION["issue"]
    assert "addDiscussionComment(" in gh_comment.MUTATION["discussion"]
    assert gh_comment.VERIFY_QUERY["issue"] != \
        gh_comment.VERIFY_QUERY["discussion"]
    assert "... on Issue" in gh_comment.VERIFY_QUERY["issue"]
    assert "... on Discussion" in gh_comment.VERIFY_QUERY["discussion"]
    # the window literal has one source of truth
    assert f"last: {gh_comment.VERIFY_WINDOW}" in \
        gh_comment.VERIFY_QUERY["issue"]
    # runtime: kind="discussion" actually selects the discussion pair
    body = f"pov\n{RUN_URL}"
    calls.outcomes[:] = [_clean("addDiscussionComment")]
    assert gh_comment.post("t0ken", "discussion", "D_abc", body) == "posted"
    payload = json.loads(calls.requests[0].data)
    assert payload["query"] == gh_comment.MUTATION["discussion"]
    assert payload["variables"] == {"id": "D_abc", "body": body}


def test_verify_query_error_treated_as_absent(calls):
    """A verify that itself fails (transport) counts as not-landed: the
    retry then fires - duplicate-over-silent is the deliberate
    precedence."""
    calls.outcomes[:] = [
        _errors(),
        urllib.error.URLError("verify unreachable"),
        _clean(),
    ]
    assert gh_comment.post("t0ken", "issue", "I_abc",
                           f"on it\n{RUN_URL}") == "posted-on-retry"
    assert len(calls.requests) == 3


def test_marker_fallback_rstrip_match(calls):
    """No run URL in the body -> the idempotency check degrades to
    rstrip() exact equality (GitHub may trim/add a trailing newline)."""
    body = "a plain comment with no run URL"
    calls.outcomes[:] = [
        _errors(),
        _comments("unrelated", body + "\n"),
    ]
    assert gh_comment.post("t0ken", "issue", "I_abc", body) == \
        "landed-after-errors"
    assert len(calls.requests) == 2


def _set_env(monkeypatch, **overrides):
    env = {"GH_TOKEN": "t0ken", "SUBJECT_ID": "I_abc",
           "SUBJECT_KIND": "issue"}
    env.update(overrides)
    for key, value in env.items():
        if value is None:
            monkeypatch.delenv(key, raising=False)
        else:
            monkeypatch.setenv(key, value)


def test_main_reads_env_and_stdin(monkeypatch, calls, capsys):
    body = f"hello\n{RUN_URL}"
    _set_env(monkeypatch)
    monkeypatch.setattr(gh_comment.sys, "stdin", io.StringIO(body))
    calls.outcomes[:] = [_clean()]
    gh_comment.main()
    assert "posted" in capsys.readouterr().out
    payload = json.loads(calls.requests[0].data)
    assert payload["query"] == gh_comment.MUTATION["issue"]
    assert payload["variables"] == {"id": "I_abc", "body": body}


def test_main_missing_env_exits_2(monkeypatch, calls, capsys):
    for key in ("GH_TOKEN", "SUBJECT_ID", "SUBJECT_KIND"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setattr(gh_comment.sys, "stdin", io.StringIO("body"))
    with pytest.raises(SystemExit) as exc:
        gh_comment.main()
    assert exc.value.code == 2
    assert "usage" in capsys.readouterr().err.lower()
    assert calls.requests == []  # a usage error never calls the API


def test_main_bad_kind_exits_2(monkeypatch, calls, capsys):
    _set_env(monkeypatch, SUBJECT_KIND="pull_request")
    monkeypatch.setattr(gh_comment.sys, "stdin", io.StringIO("body"))
    with pytest.raises(SystemExit) as exc:
        gh_comment.main()
    assert exc.value.code == 2
    assert "usage" in capsys.readouterr().err.lower()
    assert calls.requests == []


def test_main_empty_body_exits_2(monkeypatch, calls, capsys):
    _set_env(monkeypatch)
    monkeypatch.setattr(gh_comment.sys, "stdin", io.StringIO("  \n\t "))
    with pytest.raises(SystemExit) as exc:
        gh_comment.main()
    assert exc.value.code == 2
    assert calls.requests == []
