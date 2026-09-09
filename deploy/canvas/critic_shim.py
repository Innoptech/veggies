"""Critic shim (ADR 0027): the OpenHands APIBasedCritic speaks a bespoke
POST /classify ({model, input: rendered-trace-text}) -> {data: [{probs,
num_classes}]}. Neither litellm nor any model provider serves that route,
so this pod-local stdlib shim adapts it to our router as LLM-as-judge.

Runs inside the canvas container on 127.0.0.1:4401 (never published).
Auth: Bearer must equal the pod's LITELLM_MASTER_KEY (env). The judge
model defaults to deepseek-v4 (never the author's model - the
adversarial-review habit, automated).

Pure helpers (LABELS, build_judge_messages, parse_judgment,
vector_from_judgment) are pytest-covered in tests/test_critic_shim.py.
"""

from __future__ import annotations

import json
import os
import re
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PORT = 4401

# Mirrors APIBasedCritic.all_labels ORDER EXACTLY (sdk/critic/impl/api/
# client.py, has_success_label=True): success, then sentiment, then agent
# issues, then infra, then user-followup. The flat probs vector aligns by
# index; drift here shifts every probability - the test pins the order.
LABELS = (
    "success",
    "sentiment_positive", "sentiment_neutral", "sentiment_negative",
    "misunderstood_intention", "did_not_follow_instruction",
    "insufficient_analysis", "insufficient_clarification",
    "improper_tool_use_or_setup", "loop_behavior", "insufficient_testing",
    "insufficient_debugging", "incomplete_implementation",
    "file_management_errors", "scope_creep", "risky_actions_or_permission",
    "other_agent_issue",
    "infrastructure_external_issue", "infrastructure_agent_caused_issue",
    "clarification_or_restatement", "correction", "direction_change",
    "vcs_update_requests", "progress_or_scope_concern",
    "frustration_or_complaint", "removal_or_reversion_request",
    "other_user_issue",
)
_ISSUES = LABELS[LABELS.index("misunderstood_intention"):
                 LABELS.index("infrastructure_external_issue")]
_SENTIMENT = ("sentiment_positive", "sentiment_neutral", "sentiment_negative")

_RUBRIC = """\
You are a rigorous, adversarial judge of an AI coding agent's trace. The
task and the agent's actions follow. Judge the FINISHED work, not effort.

Rules:
- If the work was not actually verified (no test/check run when one was
  possible), that is insufficient_testing - and the score must drop.
- Partial or assumed completion is incomplete_implementation.
- Score 1.0 only for complete, verified, on-scope work.

Reply with ONLY a JSON object (no prose, no fences):
{"score": <float 0..1>, "sentiment": "positive"|"neutral"|"negative",
 "issues": [<zero or more of these labels: %s>]}
"""


def build_judge_messages(trace_text: str) -> list[dict]:
    """The judge conversation sent to our router (pure)."""
    return [
        {"role": "system", "content": _RUBRIC % ", ".join(_ISSUES)},
        {"role": "user", "content": trace_text[-24000:]},
    ]


def parse_judgment(reply: str) -> dict:
    """Strict JSON out of a judge reply; tolerates code fences and
    reasoning-model <think> blocks, nothing else. Garbage raises
    ValueError (the critic call then 502s loud)."""
    text = re.sub(r"<think>.*?</think>", "", reply, flags=re.S)
    text = re.sub(r"```(?:json)?\s*|\s*```", "", text).strip()
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        raise ValueError("judge reply contains no JSON object")
    data = json.loads(m.group(0))
    score = data.get("score")
    if not isinstance(score, (int, float)):
        raise ValueError("judge reply lacks a numeric score")
    issues = [i for i in (data.get("issues") or []) if i in _ISSUES]
    return {"score": min(1.0, max(0.0, float(score))),
            "sentiment": data.get("sentiment", "neutral"),
            "issues": issues}


def vector_from_judgment(judgment: dict) -> list[float]:
    """Judgment -> flat prob vector aligned to LABELS (pure). score is the
    'success' probability; listed issues get 0.8, sentiment is one-hot;
    infra/user-followup labels stay 0 (no signal in a headless run)."""
    vec = [0.0] * len(LABELS)
    vec[LABELS.index("success")] = judgment["score"]
    sentiment = judgment.get("sentiment", "neutral")
    for s in _SENTIMENT:
        vec[LABELS.index(s)] = 0.9 if s == f"sentiment_{sentiment}" else 0.05
    for issue in judgment.get("issues", []):
        vec[LABELS.index(issue)] = 0.8
    return vec


def classify(trace_text: str, router_base: str, key: str, judge_model: str) -> dict:
    """One judge call over the in-pod router (IO; the only impure step)."""
    body = json.dumps({
        "model": judge_model,
        "messages": build_judge_messages(trace_text),
        "temperature": 0,
        # reasoning judges (deepseek-v4) burn tokens on <think> first;
        # 400 truncated them before the JSON (verified 2026-09-09).
        "max_tokens": 2400,
    }).encode()
    req = urllib.request.Request(
        f"{router_base}/chat/completions", data=body,
        headers={"Content-Type": "application/json",
                 "Authorization": f"Bearer {key}"})
    with urllib.request.urlopen(req, timeout=180) as r:
        reply = json.loads(r.read())["choices"][0]["message"]["content"]
    vec = vector_from_judgment(parse_judgment(reply))
    return {"data": [{"probs": vec, "num_classes": len(LABELS)}]}


def main() -> None:
    router_base = os.environ["VEGGIES_ROUTER_BASE"]
    key = os.environ["LITELLM_MASTER_KEY"]
    judge_model = os.environ.get("VEGGIES_JUDGE_MODEL", "deepseek-v4")

    class Handler(BaseHTTPRequestHandler):
        def _send(self, code: int, obj: dict) -> None:
            body = json.dumps(obj).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a) -> None:  # quiet
            pass

        def do_GET(self) -> None:
            if self.path == "/healthz":
                return self._send(200, {"ok": True})
            self._send(404, {"error": "unknown path"})

        def do_POST(self) -> None:
            if self.path != "/classify":
                return self._send(404, {"error": "unknown path"})
            auth = self.headers.get("Authorization", "")
            if auth != f"Bearer {key}":
                return self._send(401, {"error": "bad key"})
            try:
                n = int(self.headers.get("Content-Length", 0))
                payload = json.loads(self.rfile.read(n) or b"{}")
                out = classify(str(payload.get("input", "")),
                               router_base, key, judge_model)
                self._send(200, out)
            except (ValueError, json.JSONDecodeError) as e:
                self._send(502, {"error": f"judge failed: {e}"})
            except Exception as e:  # router down etc: loud, never a fake score
                self._send(502, {"error": f"{type(e).__name__}: {e}"})

    print(f"critic-shim listening on 127.0.0.1:{PORT} (judge: {judge_model})",
          flush=True)
    ThreadingHTTPServer(("127.0.0.1", PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()
