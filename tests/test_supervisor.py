"""Supervisor (ADR 0028): pure judge/decide logic. The two IO seams
(opencode API, podman-exec judge call) live in cli/veggies.py and are
covered by live selftest verification, not unit tests."""

import base64
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "cli"))

import supervisor  # noqa: E402


def test_labels_match_upstream_taxonomy():
    assert len(supervisor.LABELS) == 27
    assert supervisor.LABELS[0] == "success"
    assert supervisor.LABELS[-1] == "report_errors"


def test_render_transcript_roles_and_tools():
    msgs = [
        {"info": {"role": "user", "id": "u1"},
         "parts": [{"type": "text", "text": "write hello.py"}]},
        {"info": {"role": "assistant", "id": "a1"},
         "parts": [
             {"type": "tool", "tool": "write",
              "state": {"input": {"path": "hello.py"}, "output": "ok"}},
             {"type": "text", "text": "done"}]},
    ]
    t = supervisor.render_transcript(msgs)
    assert "[user] write hello.py" in t
    assert "[assistant tool write]" in t and 'hello.py' in t
    assert "[assistant] done" in t


def test_parse_judgment_variants():
    assert supervisor.parse_judgment('{"score": 0.9, "issues": []}')["score"] == 0.9
    fenced = '```json\n{"score": 0.4, "issues": ["insufficient_testing"]}\n```'
    assert supervisor.parse_judgment(fenced)["issues"] == ["insufficient_testing"]
    thinking = '<think>hmm</think>{"score": 0.3, "issues": []}'
    assert supervisor.parse_judgment(thinking)["score"] == 0.3
    with pytest.raises(ValueError):
        supervisor.parse_judgment("not json at all")
    with pytest.raises(ValueError):
        supervisor.parse_judgment('{"score": 42}')
    # unknown labels are filtered, not fatal
    out = supervisor.parse_judgment('{"score": 0.5, "issues": ["bogus", "stuck_in_loop"]}')
    assert out["issues"] == ["stuck_in_loop"]


def test_decide_policy():
    assert supervisor.decide([0.9], 0.6, 2) == "pass"
    assert supervisor.decide([0.5], 0.6, 2) == "refine"
    assert supervisor.decide([0.5, 0.4], 0.6, 2) == "refine"
    assert supervisor.decide([0.5, 0.4, 0.3], 0.6, 2) == "stop"
    assert supervisor.decide([0.5, 0.4, 0.7], 0.6, 2) == "pass"
    assert supervisor.decide([], 0.6, 2) == "refine"  # no score yet


def test_refinement_prompt_carries_verdict():
    p = supervisor.refinement_prompt({"score": 0.5,
                                      "issues": ["insufficient_testing"]})
    assert "0.50" in p and "insufficient_testing" in p


def test_judge_exec_script_embeds_payload_and_survives_roundtrip():
    script = supervisor.judge_exec_script("deepseek-v4", "[user] hi\n[assistant] done")
    assert "python3" not in script  # the script IS the payload carrier
    assert "__PAYLOAD_B64__" not in script
    b64 = [l for l in script.splitlines() if "b64decode" in l][0]
    embedded = b64.split('"')[1]
    payload = json.loads(base64.b64decode(embedded))
    assert payload["model"] == "deepseek-v4"
    assert payload["messages"][0]["role"] == "system"
    assert "[user] hi" in payload["messages"][1]["content"]
    # the transcript never touches argv; the master key is env-read
    assert "LITELLM_MASTER_KEY" in script and "os.environ" in script


def _embedded_payload(script):
    b64 = [l for l in script.splitlines() if "b64decode" in l][0]
    return json.loads(base64.b64decode(b64.split('"')[1]))


def test_judge_exec_script_stamps_session_metadata():
    """ADR 0047: the judge call's cost record attributes to the judged
    session via request-body metadata."""
    script = supervisor.judge_exec_script(
        "deepseek-v4", "[user] hi", session_title="#7: do the thing",
        session_id="ses_1")
    payload = _embedded_payload(script)
    assert payload["metadata"] == {"caller": "veggies-supervise",
                                   "session_title": "#7: do the thing",
                                   "session_id": "ses_1"}
    # the exec'd script forwards it into the POST body
    assert 'req_body["metadata"]' in script
    # empty title/id are omitted; the caller is always stamped
    payload = _embedded_payload(supervisor.judge_exec_script("m", "t"))
    assert payload["metadata"] == {"caller": "veggies-supervise"}
