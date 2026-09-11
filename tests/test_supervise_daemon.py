"""Supervisor daemon (ADR 0036): the always-on in-pod critic loop.

The tick logic is tested with injected IO callables; the thin urllib
wrappers (api/judge/main) are live-verified on deploy, same seam policy as
cli/veggies.py. The pure judging/decision logic itself is covered in
tests/test_supervisor.py.
"""

import importlib.util
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "cli"))
# Bind the worktree's supervisor.py BEFORE daemon.py runs its own
# `import supervisor`: daemon.py prepends /stack-config to sys.path, and
# on a host with a deployed stack that directory holds an older copy
# which would otherwise shadow the one under test here.
import supervisor  # noqa: E402,F401

_spec = importlib.util.spec_from_file_location(
    "supervise_daemon",
    Path(__file__).parent.parent / "deploy" / "supervisor" / "daemon.py")
daemon = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(daemon)

NOW = 1_000_000.0  # daemon start, ms epoch (opencode session times are ms)


class FakeApi:
    """opencode API double: serves scripted GETs, records POSTs."""

    def __init__(self, sessions=(), status=None, messages=None):
        self._sessions = list(sessions)
        self._status = dict(status or {})
        self._messages = dict(messages or {})
        self.posts = []  # (path, body)

    def __call__(self, method, path, body=None):
        if method == "POST":
            self.posts.append((path, body))
            return {}
        if path.startswith("/session/status"):
            return self._status
        if "/message" in path:
            return self._messages.get(path.split("/")[2], [])
        return self._sessions


def kicked(sid, title="#7: do the thing", created=NOW + 1000,
           busy=False, msg_id="a1", text="done"):
    """A kicked-looking session + its status + a one-finish transcript."""
    session = {"id": sid, "title": title, "time": {"created": created}}
    status = {sid: {"type": "busy" if busy else "idle"}}
    messages = {sid: [
        {"info": {"role": "user", "id": "u1"},
         "parts": [{"type": "text", "text": "work the issue"}]},
        {"info": {"role": "assistant", "id": msg_id},
         "parts": [{"type": "text", "text": text}]},
    ]}
    return session, status, messages


def judge_script(*verdicts):
    """A judge callable yielding verdicts in order. calls = transcripts;
    stamped = the (session_title, session_id) each call arrived with."""
    calls = []

    def judge(transcript, session_title="", session_id=""):
        calls.append(transcript)
        judge.stamped.append((session_title, session_id))
        v = verdicts[len(calls) - 1]
        if isinstance(v, Exception):
            raise v
        return v

    judge.calls = calls
    judge.stamped = []
    return judge


def tick(api, judge, state=None, **kw):
    state = state if state is not None else {}
    logs = []
    daemon.tick(api, judge, state, start_ms=kw.pop("start_ms", NOW),
                threshold=kw.pop("threshold", 0.6),
                max_iters=kw.pop("max_iters", 2), log=logs.append)
    return state, logs


# --- which sessions get judged -------------------------------------------------


def test_only_kicked_titles_are_judged():
    s1, st1, m1 = kicked("s1", title="#7: real issue")
    s2, st2, m2 = kicked("s2", title="operator chat")
    api = FakeApi([s1, s2], {**st1, **st2}, {**m1, **m2})
    judge = judge_script({"score": 0.9, "issues": []})
    tick(api, judge)
    assert len(judge.calls) == 1
    assert "[user] work the issue" in judge.calls[0]


def test_judge_call_is_stamped_with_session_title_and_id():
    """ADR 0047: the judge call carries the judged session's title/id so
    the cost log attributes judge spend to the kicked session."""
    s, st, m = kicked("s1", title="#7: do the thing")
    api = FakeApi([s], st, m)
    judge = judge_script({"score": 0.9, "issues": []})
    tick(api, judge)
    assert judge.stamped == [("#7: do the thing", "s1")]


def test_sessions_older_than_daemon_start_are_never_judged():
    # A pod recreate must not resurrect old finished work: a refinement
    # re-runs the agent (ADR 0036).
    s, st, m = kicked("s1", created=NOW - 60_000)
    api = FakeApi([s], st, m)
    judge = judge_script({"score": 0.1, "issues": []})
    tick(api, judge)
    assert judge.calls == []
    assert api.posts == []


def test_busy_sessions_are_skipped():
    s, st, m = kicked("s1", busy=True)
    api = FakeApi([s], st, m)
    judge = judge_script({"score": 0.1, "issues": []})
    tick(api, judge)
    assert judge.calls == []


def test_sessions_without_assistant_finish_are_skipped():
    s, st, m = kicked("s1")
    m["s1"] = [{"info": {"role": "user", "id": "u1"},
                "parts": [{"type": "text", "text": "hi"}]}]
    api = FakeApi([s], st, m)
    judge = judge_script({"score": 0.1, "issues": []})
    tick(api, judge)
    assert judge.calls == []


def test_supervised_predicate():
    assert daemon.supervised("#12: x", NOW, NOW) is True
    assert daemon.supervised("#12: x", NOW - 1, NOW) is False
    assert daemon.supervised("chat #12", NOW, NOW) is False
    assert daemon.supervised("#x", NOW, NOW) is False
    assert daemon.supervised("#12: x", None, NOW) is False


# --- the loop policy -----------------------------------------------------------


def test_pass_posts_nothing_and_judges_once():
    """A PASS must never post into the session: any posted message re-runs
    the agent, so a visible marker would loop forever (ADR 0036)."""
    s, st, m = kicked("s1")
    api = FakeApi([s], st, m)
    judge = judge_script({"score": 0.9, "issues": []})
    state, logs = tick(api, judge)
    assert api.posts == []
    assert any("pass" in l for l in logs)
    # a second tick over the same finish does not re-judge
    tick(api, judge, state)
    assert len(judge.calls) == 1


def test_below_threshold_posts_async_refinement():
    s, st, m = kicked("s1")
    api = FakeApi([s], st, m)
    judge = judge_script({"score": 0.4, "issues": ["insufficient_testing"]})
    tick(api, judge)
    (path, body), = api.posts
    assert path == "/session/s1/prompt_async?directory=/workspace"
    text = body["parts"][0]["text"]
    assert "[critic]" in text and "insufficient_testing" in text


def test_new_finish_after_refinement_is_judged_again():
    s, st, m = kicked("s1")
    api = FakeApi([s], st, m)
    judge = judge_script({"score": 0.4, "issues": []}, {"score": 0.9, "issues": []})
    state, _ = tick(api, judge)
    # the agent iterated: a NEW assistant finish (new id) is a new judgment
    m["s1"].append({"info": {"role": "assistant", "id": "a2"},
                    "parts": [{"type": "text", "text": "fixed"}]})
    tick(api, judge, state)
    assert len(judge.calls) == 2
    assert len(api.posts) == 1  # the second finish passed


def test_max_refinements_then_stop():
    s, st, m = kicked("s1")
    api = FakeApi([s], st, m)
    low = {"score": 0.3, "issues": ["stuck_in_loop"]}
    judge = judge_script(low, low, low, low)
    state, logs = tick(api, judge)
    for i in (2, 3):  # two more finishes, still below threshold
        m["s1"].append({"info": {"role": "assistant", "id": f"a{i}"},
                        "parts": [{"type": "text", "text": "again"}]})
        _, logs = tick(api, judge, state)
    assert len(api.posts) == 2  # max_iters=2 refinements, then STOP
    assert any("STOP" in l for l in logs)
    # stopped sessions are left alone even when they finish again
    m["s1"].append({"info": {"role": "assistant", "id": "a4"},
                    "parts": [{"type": "text", "text": "again"}]})
    tick(api, judge, state)
    assert len(judge.calls) == 3 and len(api.posts) == 2


def test_judge_garbage_marks_judged_without_posting():
    """Fail loud in the log, never silently pass, and never burn tokens
    re-judging the same deterministic garbage (ADR 0028/0036)."""
    s, st, m = kicked("s1")
    api = FakeApi([s], st, m)
    judge = judge_script(ValueError("judge reply contains no JSON object"))
    state, logs = tick(api, judge)
    assert api.posts == []
    assert any("judge failed" in l for l in logs)
    tick(api, judge, state)  # same finish: not retried
    assert len(judge.calls) == 1


def test_failed_refinement_post_is_retried_next_pass():
    """The refinement is the one action this loop exists for: a failed POST
    must leave the finish unjudged (and the score unrecorded) so the next
    pass re-judges and re-posts - never a silently dropped gate."""
    s, st, m = kicked("s1")
    api = FakeApi([s], st, m)
    real = api.__call__
    attempts = {"n": 0}

    def flaky(method, path, body=None):
        if method == "POST":
            attempts["n"] += 1
            if attempts["n"] == 1:
                raise ConnectionError("opencode busy bootstrapping")
        return real(method, path, body)

    judge = judge_script({"score": 0.4, "issues": []}, {"score": 0.4, "issues": []})
    state, _ = tick(flaky, judge)
    assert api.posts == []
    assert state["s1"]["scores"] == []  # nothing half-recorded
    state, _ = tick(flaky, judge, state)
    assert len(api.posts) == 1  # retried, and it landed
    assert state["s1"]["scores"] == [0.4]  # exactly once


def test_transient_judge_errors_leave_the_finish_unjudged():
    """Router restarts/timeouts/5xx are not deterministic garbage: the
    finish stays unjudged and is retried next pass (the gate reappears
    when the router does)."""
    s, st, m = kicked("s1")
    api = FakeApi([s], st, m)
    judge = judge_script(ConnectionError("litellm restarting"),
                         {"score": 0.9, "issues": []})
    state, _ = tick(api, judge)
    assert api.posts == []
    tick(api, judge, state)  # same finish, router back: judged now
    assert len(judge.calls) == 2


def test_predated_sessions_are_skipped_loudly_once():
    """The accepted ADR 0036 limitation (no back-catalog judging) must be
    observable: one log line per skipped session, not silence, not spam."""
    s, st, m = kicked("s1", created=NOW - 60_000)
    api = FakeApi([s], st, m)
    judge = judge_script({"score": 0.1, "issues": []})
    logs, announced = [], set()
    for _ in range(2):
        daemon.tick(api, judge, {}, start_ms=NOW, threshold=0.6,
                    max_iters=2, log=logs.append, announced=announced)
    skips = [l for l in logs if "predates supervisor start" in l]
    assert len(skips) == 1 and "s1" in skips[0]
    assert judge.calls == []


def test_one_bad_session_does_not_stall_the_pass():
    s1, st1, m1 = kicked("s1", title="#1: first", msg_id="a1")
    s2, st2, m2 = kicked("s2", title="#2: second", msg_id="a1")
    # s1's message fetch explodes; s2 must still be judged
    api = FakeApi([s1, s2], {**st1, **st2}, {**m1, **m2})
    real_call = api.__call__

    def flaky(method, path, body=None):
        if "/session/s1/message" in path:
            raise ConnectionError("api hiccup")
        return real_call(method, path, body)

    judge = judge_script({"score": 0.9, "issues": []})
    _, logs = tick(flaky, judge)
    assert len(judge.calls) == 1
    assert any("s1" in l for l in logs)
