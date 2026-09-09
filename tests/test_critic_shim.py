"""Critic shim: pure-helper coverage (ADR 0027). The server shell is IO;
what matters is exact alignment with APIBasedCritic's flat-label contract."""

import importlib.util
import json
from pathlib import Path

import pytest

_spec = importlib.util.spec_from_file_location(
    "critic_shim", Path(__file__).parent.parent / "deploy/canvas/critic_shim.py")
shim = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(shim)


def test_labels_match_upstream_taxonomy():
    assert len(shim.LABELS) == 27
    assert shim.LABELS[0] == "success"
    assert shim.LABELS[1:4] == ("sentiment_positive", "sentiment_neutral",
                                "sentiment_negative")
    assert "risky_actions_or_permission" in shim.LABELS
    assert shim.LABELS[-1] == "other_user_issue"


def test_judge_prompt_lists_issue_labels_and_caps_trace():
    msgs = shim.build_judge_messages("x" * 30000)
    assert msgs[0]["role"] == "system" and "insufficient_testing" in msgs[0]["content"]
    assert len(msgs[1]["content"]) == 24000  # tail-capped


def test_parse_judgment_strict_json():
    assert shim.parse_judgment('{"score": 0.9, "issues": ["loop_behavior"]}') == {
        "score": 0.9, "sentiment": "neutral", "issues": ["loop_behavior"]}
    fenced = '```json\n{"score": 0.4, "sentiment": "negative", "issues": []}\n```'
    assert shim.parse_judgment(fenced)["score"] == 0.4
    thinking = '<think>reasoning about the trace...</think>{"score": 0.3, "issues": []}'
    assert shim.parse_judgment(thinking)["score"] == 0.3
    with pytest.raises(ValueError):
        shim.parse_judgment("no json here")
    with pytest.raises(ValueError):
        shim.parse_judgment('{"issues": []}')  # no score
    # unknown labels are dropped, score clamped
    j = shim.parse_judgment('{"score": 4, "issues": ["nonsense", "scope_creep"]}')
    assert j["score"] == 1.0 and j["issues"] == ["scope_creep"]


def test_vector_alignment():
    vec = shim.vector_from_judgment({"score": 0.7, "sentiment": "positive",
                                     "issues": ["insufficient_testing"]})
    assert len(vec) == 27
    assert vec[0] == 0.7
    assert vec[shim.LABELS.index("sentiment_positive")] == 0.9
    assert vec[shim.LABELS.index("sentiment_negative")] == 0.05
    assert vec[shim.LABELS.index("insufficient_testing")] == 0.8
    assert vec[shim.LABELS.index("infrastructure_external_issue")] == 0.0
    assert vec[shim.LABELS.index("correction")] == 0.0


def test_classify_shape_against_stubbed_router(monkeypatch):
    class R:
        def read(self):
            return json.dumps({"choices": [{"message": {"content":
                '{"score": 0.5, "issues": ["incomplete_implementation"]}'}}]}).encode()
        def __enter__(self):
            return self
        def __exit__(self, *a):
            return False
    monkeypatch.setattr(shim.urllib.request, "urlopen",
                        lambda req, timeout=None: R())
    out = shim.classify("trace", "http://127.0.0.1:4000/v1", "k", "deepseek-v4")
    item = out["data"][0]
    assert item["num_classes"] == 27 and len(item["probs"]) == 27
    assert item["probs"][0] == 0.5
    assert item["probs"][shim.LABELS.index("incomplete_implementation")] == 0.8
