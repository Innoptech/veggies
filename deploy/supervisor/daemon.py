#!/usr/bin/env python3
"""Always-on critic for kicked sessions (ADR 0036) - in-pod sidecar.

Polls the stack's opencode API on pod loopback; every time a KICKED
session (titled `#N: <issue>`, ADR 0034, or `PR#N: <pr>` for review
sessions, ADR 0054) goes idle with an unjudged assistant finish, the
transcript is judged by a DIFFERENT model via the in-pod router and a
refinement is posted when below threshold. This is the ADR 0028 critic
loop, self-driving: `veggies supervise` stays the operator-driven
per-session tool.

Scope rules (deliberate, see the ADR):
- only sessions created AFTER this process starts are judged - judging a
  back-catalog of finished sessions would resurrect completed work (a
  refinement message re-runs the agent);
- PASS/STOP are log-only: ANY message posted to a session re-runs the
  agent, so a visible marker would loop forever;
- judge garbage fails loud in the pod log and the finish is marked
  judged (never a silent pass, never a token-burning retry of the same
  deterministic garbage);
- a session that exhausts its refinements is left for a human.

Zero egress: no proxy env is set; the container talks pod loopback only.
Stdlib-only. The pure critic logic is imported from supervisor.py, which
ships alongside in /stack-config; the tick loop takes its IO as injected
callables so tests/test_supervise_daemon.py needs no network.
"""

from __future__ import annotations

import base64
import json
import os
import re
import sys
import time
import urllib.request
from pathlib import Path

sys.path.insert(0, os.environ.get("STACK_CONFIG_DIR", "/stack-config"))
import supervisor  # noqa: E402 - the pure critic logic, shipped alongside

KICKED_TITLE = re.compile(r"^(?:PR)?#\d+:")  # ADR 0034/0054: kicked sessions are titled

HEARTBEAT = Path("/tmp/supervise.heartbeat")  # the liveness probe reads this


def supervised(title: str, created_ms: object, start_ms: float) -> bool:
    """Which sessions the daemon judges: kicked ones created after it
    started. Undated sessions are skipped (conservative)."""
    return (bool(KICKED_TITLE.match(title))
            and isinstance(created_ms, (int, float))
            and created_ms >= start_ms)


def _judge_finish(ocall, judge_call, sid: str, title: str, st: dict, *,
                  threshold: float, max_iters: int, log) -> None:
    """Judge one idle session's latest unjudged finish; act on the verdict."""
    msgs = ocall("GET", f"/session/{sid}/message?directory=/workspace")
    if not isinstance(msgs, list):
        return
    assistants = [m for m in msgs
                  if (m.get("info") or {}).get("role") == "assistant"]
    if not assistants:
        return
    last_id = assistants[-1]["info"].get("id")
    if last_id in st["judged"]:
        return
    transcript = supervisor.render_transcript(msgs)
    if not transcript.strip():
        st["judged"].add(last_id)
        return
    try:
        verdict = judge_call(transcript, title, sid)
    except ValueError as e:
        # Deterministic garbage (unparseable reply): fail loud and mark
        # judged - never a silent pass, never a token-burning retry of the
        # same input. Transient errors (router restart, timeout, 5xx)
        # propagate instead: the finish stays UNJUDGED and the next pass
        # retries - the gate reappears when the router does (ADR 0036).
        st["judged"].add(last_id)
        log(f"!! judge failed for {title!r}: {e}")
        return
    action = supervisor.decide(st["scores"] + [verdict["score"]],
                               threshold, max_iters)
    log(f"critic: {title} score {verdict['score']:.2f} "
        f"issues={verdict['issues'] or '[]'} -> {action}")
    if action == "refine":
        # Post BEFORE recording state: a failed POST propagates with the
        # finish still unjudged, so the next pass re-judges and re-posts -
        # the one action this loop exists for is never silently dropped.
        # async so one slow session never blocks the pass; an idle session
        # restarts on admit (the endpoint the kick itself uses, ADR 0033).
        ocall("POST", f"/session/{sid}/prompt_async?directory=/workspace",
              {"parts": [{"type": "text",
                          "text": supervisor.refinement_prompt(verdict)}]})
    st["judged"].add(last_id)
    st["scores"].append(verdict["score"])
    if action == "stop":
        st["stopped"] = True
        log(f"STOP: {title} exhausted {max_iters} refinements "
            f"(scores {st['scores']}) - needs a human")
    # "pass" is deliberately log-only (see module docstring).


def tick(ocall, judge_call, state: dict, *, start_ms: float,
         threshold: float, max_iters: int, log=print,
         announced: set | None = None) -> None:
    """One supervision pass over the stack's sessions. ocall(method, path,
    body=None) is the opencode API; judge_call(transcript, session_title,
    session_id) returns a verdict - both injected so the loop is
    unit-testable. One bad session
    never stalls the pass. `announced` (a caller-owned set) makes skipped
    pre-start sessions visible exactly once - the gate being off must be
    distinguishable from the gate passing."""
    sessions = ocall("GET", "/session?directory=/workspace")
    if not isinstance(sessions, list):
        return
    status = ocall("GET", "/session/status?directory=/workspace")
    if not isinstance(status, dict):
        status = {}
    for s in sessions:
        sid = s.get("id")
        if not sid:
            continue
        title = str(s.get("title") or "")
        if not supervised(title, (s.get("time") or {}).get("created"),
                          start_ms):
            if (announced is not None and KICKED_TITLE.match(title)
                    and sid not in announced):
                announced.add(sid)
                log(f"skipping {title} ({sid}): predates supervisor start")
            continue
        st = state.setdefault(sid, {"judged": set(), "scores": [],
                                    "stopped": False})
        if st["stopped"]:
            continue
        if (status.get(sid) or {}).get("type", "idle") != "idle":
            continue
        try:
            _judge_finish(ocall, judge_call, sid, title, st,
                          threshold=threshold, max_iters=max_iters, log=log)
        except Exception as e:
            log(f"!! session {sid} ({title or 'untitled'}): {e}")


# --- the thin IO seam (live-verified on deploy, not unit-tested) -----------------


def api(base: str, password: str, method: str, path: str,
        body: dict | None = None) -> object:
    """One opencode API call on pod loopback (basic auth)."""
    req = urllib.request.Request(
        f"{base}{path}", method=method,
        data=json.dumps(body).encode() if body is not None else None)
    req.add_header("Authorization", "Basic " + base64.b64encode(
        f"opencode:{password}".encode()).decode())
    if body is not None:
        req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req, timeout=90) as resp:
        raw = resp.read()
    return json.loads(raw) if raw.strip() else {}


def judge(router_url: str, master_key: str, model: str, transcript: str,
          session_title: str = "", session_id: str = "") -> dict:
    """Judge a transcript via the in-pod router. The master key arrives via
    env (podman secret) and never leaves the pod - the ADR 0028 invariant
    the podman-exec path was built for, kept by staying in-pod. The
    metadata stamps the judge call's cost record with the session it
    judges (ADR 0052); title/id keys are omitted when empty."""
    metadata = {"caller": "supervisor-daemon"}
    if session_title:
        metadata["session_title"] = session_title
    if session_id:
        metadata["session_id"] = session_id
    body = json.dumps({
        "model": model,
        "messages": supervisor.build_judge_messages(transcript),
        "metadata": metadata,
        "temperature": 0,
        # reasoning judges (deepseek-v4) burn tokens on <think> first
        "max_tokens": 2400,
    }).encode()
    req = urllib.request.Request(
        f"{router_url}/chat/completions", data=body,
        headers={"Content-Type": "application/json",
                 "Authorization": f"Bearer {master_key}"})
    with urllib.request.urlopen(req, timeout=180) as r:
        content = json.loads(r.read())["choices"][0]["message"]["content"]
    return supervisor.parse_judgment(content)


def main() -> int:
    base = os.environ.get("OPENCODE_URL", "http://127.0.0.1:4096")
    router = os.environ.get("ROUTER_URL", "http://127.0.0.1:4000/v1")
    password = os.environ["OPENCODE_SERVER_PASSWORD"]
    master_key = os.environ["LITELLM_MASTER_KEY"]
    judge_model = os.environ.get("SUPERVISE_JUDGE_MODEL", "deepseek-v4")
    interval = float(os.environ.get("SUPERVISE_INTERVAL", "15"))
    threshold = float(os.environ.get("SUPERVISE_THRESHOLD", "0.6"))
    max_iters = int(os.environ.get("SUPERVISE_MAX_ITERS", "2"))
    start_ms = time.time() * 1000  # opencode session times are ms epoch
    state: dict = {}  # sid -> judged/scores/stopped; pod restart re-dates
    announced: set = set()  # pre-start sessions already skip-logged
    print(f"supervisor up: judging kicked sessions on {base} "
          f"(judge {judge_model}, threshold {threshold}, "
          f"max {max_iters} refinements)", flush=True)
    while True:
        # First thing every pass: the liveness probe reads this file; a
        # wedged pass (e.g. a hung judge call) goes unhealthy eventually.
        HEARTBEAT.write_text(f"{time.time():.0f}\n")
        try:
            tick(lambda m, p, b=None: api(base, password, m, p, b),
                 lambda t, title, sid: judge(router, master_key,
                                             judge_model, t, title, sid),
                 state, start_ms=start_ms, threshold=threshold,
                 max_iters=max_iters, announced=announced)
        except Exception as e:
            print(f"!! tick failed: {e}", flush=True)
        time.sleep(interval)


if __name__ == "__main__":
    sys.exit(main())
