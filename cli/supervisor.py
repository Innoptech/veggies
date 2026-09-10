"""Critic supervision for opencode sessions (ADR 0028).

Owns the loop canvas's SDK critic used to run for us: when a session goes
idle after an assistant finish, judge the transcript with a DIFFERENT
model than the author (default deepseek-v4 judging kimi-k3) and, below
threshold, post a refinement message so the agent iterates. Everything
here is pure except the two IO seams in cli/veggies.py (opencode API over
its published port; judge call exec'd inside the litellm container so the
master key never leaves the pod). Judge prompt/rubric ported from the
canvas critic shim; the /classify protocol and the tokenizer vendoring
were SDK-specific and are gone.
"""

from __future__ import annotations

import json
import re

# Same 27-label taxonomy the upstream critic used (openhands-sdk
# critic/impl/api/renderer.py) - kept as the judge's rubric.
LABELS = [
    "success",
    "positive_sentiment", "negative_sentiment", "neutral_sentiment",
    # agent issues
    "stuck_in_loop", "repeated_tool_failure", "tool_syntax_error",
    "tool_runtime_error", "hallucinated", "incomplete_implementation",
    "insufficient_testing", "code_quality_concern", "insufficient_exploration",
    "planning_issue", "misunderstood_requirements", "scope_expansion",
    "improper_stopping",
    # infra issues
    "rate_limit_error", "context_window_error",
    # user follow-ups
    "ask_clarification", "provide_guidance", "correct_misunderstanding",
    "add_requirements", "express_satisfaction", "express_dissatisfaction",
    "report_stuck_loop", "report_errors",
]

JUDGE_SYSTEM = (
    "You are a strict code-review judge. You are given a transcript of an "
    "AI coding-agent session (user request, agent actions, tool results). "
    "Judge ONLY the most recent assistant work against the user's request.\n"
    "Reply with EXACTLY one JSON object, no prose, no markdown fences:\n"
    '{"score": <float 0..1>, "sentiment": "positive"|"neutral"|"negative", '
    '"issues": [<zero or more labels>]}\n'
    "score = probability the work fully satisfies the request with good "
    "engineering (ran/verified what it claims, complete, no loop or "
    "hallucination). issues must come from this label set:\n"
    + ", ".join(LABELS)
)

def render_transcript(messages: list[dict]) -> str:
    """opencode session messages (API shape verified 2026-09-09: each has
    info.role + parts[]) -> plain text for the judge. Tool calls are
    summarized, not dumped whole (context discipline)."""
    lines = []
    for msg in messages:
        role = (msg.get("info") or {}).get("role", "?")
        for part in msg.get("parts", []):
            kind = part.get("type")
            if kind == "text":
                text = (part.get("text") or "").strip()
                if text:
                    lines.append(f"[{role}] {text}")
            elif kind == "tool":
                tool = part.get("tool", "?")
                state = part.get("state") or {}
                inp = json.dumps(state.get("input", {}))[:200]
                out = str(state.get("output", ""))[:200]
                lines.append(f"[{role} tool {tool}] in={inp} out={out}")
    return "\n".join(lines)


def build_judge_messages(transcript: str) -> list[dict]:
    return [
        {"role": "system", "content": JUDGE_SYSTEM},
        {"role": "user", "content": f"Session transcript:\n\n{transcript}"},
    ]


def parse_judgment(reply: str) -> dict:
    """Strict JSON out of a judge reply; tolerates code fences and
    reasoning-model <think> blocks, nothing else. Garbage raises
    ValueError (supervision fails loud, never silently passes)."""
    text = re.sub(r"<think>.*?</think>", "", reply, flags=re.S)
    text = re.sub(r"```(?:json)?\s*|\s*```", "", text).strip()
    match = re.search(r"\{.*\}", text, re.S)
    if not match:
        raise ValueError("judge reply contains no JSON object")
    data = json.loads(match.group(0))
    score = data.get("score")
    if not isinstance(score, (int, float)) or not 0 <= score <= 1:
        raise ValueError(f"judge score missing or out of range: {score!r}")
    issues = [i for i in data.get("issues", []) if i in LABELS]
    return {"score": float(score), "issues": issues,
            "sentiment": data.get("sentiment", "neutral")}


def decide(scores: list[float], threshold: float, max_iters: int) -> str:
    """The loop policy. scores = judgments so far (one per finish)."""
    if scores and scores[-1] >= threshold:
        return "pass"
    if len(scores) > max_iters:
        return "stop"
    return "refine"


def refinement_prompt(judgment: dict) -> str:
    """The message posted back into the session (visible in the web UI)."""
    issues = ", ".join(judgment["issues"]) or "unspecified"
    return (f"[critic] score {judgment['score']:.2f} below threshold; "
            f"issues: {issues}. Address them and finish again - run and "
            "verify what you claim, and keep the change minimal.")


# Run inside the litellm container (`podman exec -i <pod>-litellm python3
# -`): the request {"model", "messages"} arrives base64-embedded (stdin
# carries this script itself). Posts to the in-pod router, prints the
# judge's reply content. The master key is read from the container's own
# env - it never crosses a command line.
JUDGE_EXEC_SCRIPT = """
import base64, json, os, urllib.request
req_body = json.loads(base64.b64decode("__PAYLOAD_B64__"))
body = json.dumps({
    "model": req_body["model"],
    "messages": req_body["messages"],
    "temperature": 0,
    # reasoning judges (deepseek-v4) burn tokens on <think> first; 400
    # truncated them before the JSON (verified 2026-09-09).
    "max_tokens": 2400,
}).encode()
req = urllib.request.Request(
    "http://127.0.0.1:4000/v1/chat/completions", data=body,
    headers={"Content-Type": "application/json",
             "Authorization": "Bearer " + os.environ["LITELLM_MASTER_KEY"]})
with urllib.request.urlopen(req, timeout=180) as r:
    print(json.loads(r.read())["choices"][0]["message"]["content"])
"""


def judge_exec_script(model: str, transcript: str) -> str:
    """The script for `podman exec -i <pod>-litellm python3 -`."""
    payload = json.dumps({"model": model,
                          "messages": build_judge_messages(transcript)})
    import base64
    return JUDGE_EXEC_SCRIPT.replace(
        "__PAYLOAD_B64__", base64.b64encode(payload.encode()).decode())
