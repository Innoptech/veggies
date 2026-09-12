"""Tests for scripts/gh_comment.py - the verify-before-retry comment
helper (issue #101): GitHub may execute a comment mutation and then fail
the response (run 34706580396), so errors[] is checked against ground
truth before any retry. Idempotency is pinned to the helper's own
per-invocation nonce marker (adversarial review M1-M4): the negative
fixtures prove a foreign thread can never false-green a lost comment."""

import importlib.util
import io
import json
import re
import urllib.error
from pathlib import Path

import pytest

_spec = importlib.util.spec_from_file_location(
    "gh_comment", Path(__file__).parent.parent / "scripts/gh_comment.py")
gh_comment = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(gh_comment)

RUN_URL = "https://github.com/o/r/actions/runs/12345"
NONCE = "c0ffee42"  # deterministic stand-in for secrets.token_hex(4)
FOREIGN_NONCE = "deadbeef"


class FakeResponse:
    def __init__(self, payload):
        self.payload = payload

    def read(self):
        if isinstance(self.payload, bytes):
            return self.payload  # raw-body fixture (response corruption)
        return json.dumps(self.payload).encode()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class _Scripted:
    """Recorded urlopen requests (.requests) plus the queue of per-call
    outcomes (.outcomes): a dict is returned as the GraphQL payload, a
    bytes object is returned as the raw body, an exception instance is
    raised, and a callable is invoked with the request (its result is
    then treated the same way)."""

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
        if callable(outcome):
            outcome = outcome(req)
        if isinstance(outcome, Exception):
            raise outcome
        return FakeResponse(outcome)

    monkeypatch.setattr(gh_comment.urllib.request, "urlopen", fake_urlopen)
    return script


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    """RETRY_DELAY must never slow the suite."""
    monkeypatch.setattr(gh_comment.time, "sleep", lambda seconds: None)


@pytest.fixture(autouse=True)
def _no_ambient_run_id(monkeypatch):
    """The stamp embeds the ambient GITHUB_RUN_ID; keep it deterministic
    (tests that care set it explicitly)."""
    monkeypatch.delenv("GITHUB_RUN_ID", raising=False)


def _errors(message="Something went wrong while executing your query"):
    return {"errors": [{"message": message}]}


def _clean(mutation_field="addComment"):
    return {"data": {mutation_field: {"clientMutationId": "x"}}}


def _comments(*bodies):
    return {"data": {"node": {"comments": {
        "nodes": [{"body": b} for b in bodies]}}}}


def _queries(script):
    return [json.loads(r.data)["query"] for r in script.requests]


def _posted_body(req):
    return json.loads(req.data)["variables"]["body"]


def _marker_of_call(req):
    """The helper's own nonce, parsed out of the RECORDED mutation
    request - tests never hardcode a marker of their own."""
    match = re.search(r"veggies-ghc:\S+", _posted_body(req))
    assert match, f"no veggies-ghc marker in posted body: {_posted_body(req)!r}"
    return match.group(0)


def _echo_our_marker(script, *other_bodies):
    """A verify outcome echoing OUR marker (derived from the recorded
    mutation request) inside a comment body, alongside foreign ones."""
    def respond(_req):
        marker = _marker_of_call(script.requests[0])
        return _comments(*other_bodies, f"on it\n<!-- {marker} -->")
    return respond


def test_post_clean_success(calls):
    body = "plan posted"
    calls.outcomes[:] = [_clean()]
    assert gh_comment.post("t0ken", "issue", "I_abc", body) == "posted"
    assert len(calls.requests) == 1
    req = calls.requests[0]
    assert req.full_url == "https://api.github.com/graphql"
    assert req.headers["Authorization"] == "Bearer t0ken"
    payload = json.loads(req.data)
    # the kind-correct mutation, with the STAMPED body as a variable
    assert payload["query"] == gh_comment.MUTATION["issue"]
    assert "addComment(" in payload["query"]
    assert payload["variables"]["id"] == "I_abc"
    stamped = payload["variables"]["body"]
    assert stamped.startswith(body + "\n\n<!-- veggies-ghc:")
    assert stamped.endswith(" -->")


def test_errors_but_comment_landed(calls, capsys):
    """The #101 case (run 34706580396): the mutation executed but the
    response came back errors[] - the verify query finds the comment by
    its nonce marker, so there is NO retry and the step stays green."""
    calls.outcomes[:] = [
        _errors(),
        _echo_our_marker(calls, "someone else's comment"),
    ]
    assert gh_comment.post("t0ken", "issue", "I_abc", "on it") == \
        "landed-after-errors"
    assert len(calls.requests) == 2  # mutation + verify, NO retry
    assert _queries(calls) == [gh_comment.MUTATION["issue"],
                               gh_comment.VERIFY_QUERY["issue"]]
    # the raw error was echoed, not swallowed
    assert "Something went wrong" in capsys.readouterr().err


def test_errors_then_retry_succeeds(calls):
    calls.outcomes[:] = [_errors(), _comments(), _clean()]
    assert gh_comment.post("t0ken", "issue", "I_abc", "on it") == \
        "posted-on-retry"
    assert len(calls.requests) == 3
    assert _queries(calls) == [gh_comment.MUTATION["issue"],
                               gh_comment.VERIFY_QUERY["issue"],
                               gh_comment.MUTATION["issue"]]
    # one stamp per logical comment: the retry posts the SAME stamped body
    assert _posted_body(calls.requests[2]) == _posted_body(calls.requests[0])


def test_double_failure_exits_1(calls, capsys):
    calls.outcomes[:] = [_errors("boom-1"), _comments(),
                         _errors("boom-2"), _comments()]
    with pytest.raises(SystemExit) as exc:
        gh_comment.post("t0ken", "issue", "I_abc", "on it")
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
        _echo_our_marker(calls),
    ]
    assert gh_comment.post("t0ken", "issue", "I_abc", "on it") == \
        "landed-after-errors"
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
    # M3: the issue-kind verify also covers PR subjects (addComment's
    # whole domain; ADR 0054's deferred PR path inherits this helper)
    assert "... on PullRequest" in gh_comment.VERIFY_QUERY["issue"]
    assert "... on Discussion" in gh_comment.VERIFY_QUERY["discussion"]
    # the window literal has one source of truth (twice in the issue
    # query: Issue fragment + PullRequest fragment)
    assert gh_comment.VERIFY_QUERY["issue"].count(
        f"last: {gh_comment.VERIFY_WINDOW}") == 2
    assert f"last: {gh_comment.VERIFY_WINDOW}" in \
        gh_comment.VERIFY_QUERY["discussion"]
    # the hand-written GraphQL strings stay brace-balanced
    for query in (*gh_comment.MUTATION.values(),
                  *gh_comment.VERIFY_QUERY.values()):
        assert query.count("{") == query.count("}")
    # runtime: kind="discussion" actually selects the discussion pair
    calls.outcomes[:] = [_clean("addDiscussionComment")]
    assert gh_comment.post("t0ken", "discussion", "D_abc", "pov") == "posted"
    payload = json.loads(calls.requests[0].data)
    assert payload["query"] == gh_comment.MUTATION["discussion"]
    assert payload["variables"]["id"] == "D_abc"
    assert payload["variables"]["body"].startswith(
        "pov\n\n<!-- veggies-ghc:")


def test_verify_query_error_treated_as_absent(calls):
    """A verify that itself fails (transport) counts as not-landed: the
    retry then fires - duplicate-over-silent is the deliberate
    precedence."""
    calls.outcomes[:] = [
        _errors(),
        urllib.error.URLError("verify unreachable"),
        _clean(),
    ]
    assert gh_comment.post("t0ken", "issue", "I_abc", "on it") == \
        "posted-on-retry"
    assert len(calls.requests) == 3


# --- Negative fixtures (M4): the idempotency property must never regress
# invisibly - a foreign thread is NOT our comment ----------------------


def test_foreign_thread_does_not_false_green(calls, monkeypatch):
    """M4 negative fixture: a thread full of OTHER comments - a different
    run URL, a foreign nonce - with OURS absent must never read as
    landed: the retry fires."""
    monkeypatch.setattr(gh_comment.secrets, "token_hex", lambda n: NONCE)
    calls.outcomes[:] = [
        _errors(),
        _comments("watching https://github.com/o/r/actions/runs/99999",
                  f"note <!-- veggies-ghc:99999:{FOREIGN_NONCE} -->"),
        _clean(),
    ]
    assert gh_comment.post("t0ken", "issue", "I_abc", "plan") == \
        "posted-on-retry"
    assert len(calls.requests) == 3


def test_quoted_current_run_url_does_not_false_green(calls, monkeypatch):
    """M1 regression: an earlier comment quoting the CURRENT run's URL -
    plus a foreign nonce minted for the SAME run id - is NOT our comment.
    The per-run URL marker this helper used to match would false-green
    here; only the full per-invocation nonce counts."""
    monkeypatch.setenv("GITHUB_RUN_ID", "12345")
    monkeypatch.setattr(gh_comment.secrets, "token_hex", lambda n: NONCE)
    calls.outcomes[:] = [
        _errors(),
        _comments(f"watching {RUN_URL}",
                  f"note <!-- veggies-ghc:12345:{FOREIGN_NONCE} -->"),
        _clean(),
    ]
    assert gh_comment.post("t0ken", "issue", "I_abc", "plan") == \
        "posted-on-retry"
    assert len(calls.requests) == 3
    # our stamped body carries the ambient run id inside OUR nonce
    assert f"veggies-ghc:12345:{NONCE}" in _posted_body(calls.requests[0])


# --- M2: an unparseable 200 (truncated / HTML / non-dict JSON) is the
# same response-corruption family as #101 - verify-then-retry, never a
# blind exit -----------------------------------------------------------


def test_garbage_200_mutation_takes_the_verify_path(calls, capsys):
    calls.outcomes[:] = [
        b"<html><body>Bad Gateway</body></html>",  # not JSON at all
        _comments(),   # verify: absent
        _clean(),      # retry clean
    ]
    assert gh_comment.post("t0ken", "issue", "I_abc", "plan") == \
        "posted-on-retry"
    assert len(calls.requests) == 3
    assert "unparseable" in capsys.readouterr().err


def test_valid_json_non_dict_200_takes_the_verify_path(calls):
    calls.outcomes[:] = [["not", "a", "dict"], _comments(), _clean()]
    assert gh_comment.post("t0ken", "issue", "I_abc", "plan") == \
        "posted-on-retry"
    assert len(calls.requests) == 3


def test_garbage_200_verify_is_treated_as_absent(calls):
    """Verify side: a garbage VERIFY response counts as not-landed
    (duplicate-over-silent) - the retry still fires."""
    calls.outcomes[:] = [
        _errors(),
        b"\xff\xfe\x00 not json",  # the verify call itself is corrupt
        _clean(),
    ]
    assert gh_comment.post("t0ken", "issue", "I_abc", "plan") == \
        "posted-on-retry"
    assert len(calls.requests) == 3


# --- M3: addComment accepts PR node ids, so the issue-kind verify must
# see PR subjects too ---------------------------------------------------


def test_issue_kind_verify_matches_a_pr_subject(calls):
    """The issue-kind verify query carries a PullRequest inline fragment
    (ADR 0054's deferred PR path inherits this helper). Exactly one
    fragment matches any subject, so the PR-shaped payload parses down
    the same node.comments path and landed() sees our marker."""
    calls.outcomes[:] = [
        _errors(),
        _echo_our_marker(calls),  # node.comments filled by the PR fragment
    ]
    assert gh_comment.post("t0ken", "issue", "PR_abc", "plan") == \
        "landed-after-errors"
    assert len(calls.requests) == 2
    assert "... on PullRequest" in _queries(calls)[1]


# --- m1: success requires the kind's mutation FIELD, not just a data
# dict ------------------------------------------------------------------


@pytest.mark.parametrize("payload", [{"data": {}},
                                     {"data": {"addComment": None}}])
def test_data_without_mutation_field_is_not_posted(calls, payload):
    """A 200 whose data lacks the kind's mutation field (absent or null)
    did NOT post a comment - fall through to verify, then retry."""
    calls.outcomes[:] = [payload, _comments(), _clean()]
    assert gh_comment.post("t0ken", "issue", "I_abc", "plan") == \
        "posted-on-retry"
    assert len(calls.requests) == 3


def test_null_client_mutation_id_still_posted(calls):
    """Flip side: clientMutationId itself may be null on success - the
    FIELD present and non-None is what counts as posted."""
    calls.outcomes[:] = [{"data": {"addComment": {"clientMutationId": None}}}]
    assert gh_comment.post("t0ken", "issue", "I_abc", "plan") == "posted"
    assert len(calls.requests) == 1


# --- stamp(): the per-invocation nonce marker ---------------------------


def test_stamp_embeds_the_run_id_in_an_html_comment(monkeypatch):
    monkeypatch.setenv("GITHUB_RUN_ID", "777")
    monkeypatch.setattr(gh_comment.secrets, "token_hex", lambda n: NONCE)
    stamped, marker = gh_comment.stamp("hello")
    assert marker == f"veggies-ghc:777:{NONCE}"
    assert stamped == f"hello\n\n<!-- veggies-ghc:777:{NONCE} -->"


def test_stamp_defaults_to_local_without_a_run_id(monkeypatch):
    monkeypatch.delenv("GITHUB_RUN_ID", raising=False)
    monkeypatch.setattr(gh_comment.secrets, "token_hex", lambda n: NONCE)
    stamped, marker = gh_comment.stamp("hello")
    assert marker == f"veggies-ghc:local:{NONCE}"
    assert stamped.endswith(f"<!-- veggies-ghc:local:{NONCE} -->")


# --- main(): env + stdin -----------------------------------------------


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
    body = "hello"
    _set_env(monkeypatch)
    monkeypatch.setattr(gh_comment.sys, "stdin", io.StringIO(body))
    calls.outcomes[:] = [_clean()]
    gh_comment.main()
    assert "posted" in capsys.readouterr().out
    payload = json.loads(calls.requests[0].data)
    assert payload["query"] == gh_comment.MUTATION["issue"]
    assert payload["variables"]["id"] == "I_abc"
    assert payload["variables"]["body"].startswith(
        body + "\n\n<!-- veggies-ghc:")


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
