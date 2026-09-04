"""Workflow schema v0 core tests (ADR 0017) - pure, no IO."""

import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "deploy" / "orchestrator"))

import core  # noqa: E402

ROSTER = ["build", "plan", "adversarial-review", "explore"]


def wf(**over):
    base = {
        "name": "demo",
        "steps": [
            {"id": "implement", "kind": "agent", "agent": "build",
             "prompt": "do {{ task }}"},
            {"id": "check", "kind": "shell", "run": "pytest -q",
             "needs": ["implement"]},
        ],
    }
    base.update(over)
    return base


def test_valid_minimal_normalizes():
    w = core.validate_workflow(wf(), ROSTER)
    assert w["name"] == "demo"
    assert w["permissions"] == "deny"          # default policy
    assert w["steps"][0]["timeout"] == 1800    # 30m default
    assert w["steps"][0]["kind"] == "agent"
    assert w["order"] == [["implement"], ["check"]]


def test_unknown_top_key_rejected():
    with pytest.raises(core.WorkflowError, match="unknown workflow keys"):
        core.validate_workflow(wf(nope=1), ROSTER)


def test_unknown_step_key_rejected():
    with pytest.raises(core.WorkflowError, match="unknown keys"):
        core.validate_workflow(wf(steps=[{"id": "a", "agent": "build",
                                          "prompt": "x", "nope": 1}]), ROSTER)


def test_agent_step_needs_agent_and_prompt():
    with pytest.raises(core.WorkflowError, match="need 'agent'"):
        core.validate_workflow(wf(steps=[{"id": "a", "prompt": "x"}]), ROSTER)
    with pytest.raises(core.WorkflowError, match="need 'prompt'"):
        core.validate_workflow(wf(steps=[{"id": "a", "agent": "build"}]), ROSTER)


def test_shell_step_needs_run():
    with pytest.raises(core.WorkflowError, match="need 'run'"):
        core.validate_workflow(wf(steps=[{"id": "a", "kind": "shell"}]), ROSTER)


def test_unknown_agent_checked_against_roster():
    with pytest.raises(core.WorkflowError, match="unknown agent 'claude'"):
        core.validate_workflow(
            wf(steps=[{"id": "a", "agent": "claude", "prompt": "x"}]), ROSTER)


def test_roster_none_skips_agent_check():
    w = core.validate_workflow(
        wf(steps=[{"id": "a", "agent": "anything", "prompt": "x"}]), None)
    assert w["steps"][0]["agent"] == "anything"


def test_needs_unknown_and_cycle():
    with pytest.raises(core.WorkflowError, match="needs unknown step"):
        core.validate_workflow(wf(steps=[{"id": "a", "agent": "build",
                                          "prompt": "x", "needs": ["ghost"]}]), ROSTER)
    cyclic = [{"id": "a", "agent": "build", "prompt": "x", "needs": ["b"]},
              {"id": "b", "kind": "shell", "run": "true", "needs": ["a"]}]
    with pytest.raises(core.WorkflowError, match="needs-cycle"):
        core.validate_workflow(wf(steps=cyclic), ROSTER)


def test_duplicate_id():
    with pytest.raises(core.WorkflowError, match="duplicate step id"):
        core.validate_workflow(wf(steps=[
            {"id": "a", "agent": "build", "prompt": "x"},
            {"id": "a", "agent": "build", "prompt": "y"}]), ROSTER)


def test_parallel_requires_combine_and_bounds():
    with pytest.raises(core.WorkflowError, match="parallel requires combine"):
        core.validate_workflow(wf(steps=[{"id": "a", "agent": "build",
                                          "prompt": "x", "parallel": 3}]), ROSTER)
    with pytest.raises(core.WorkflowError, match="combine without parallel"):
        core.validate_workflow(wf(steps=[{"id": "a", "agent": "build",
                                          "prompt": "x", "combine": "best-of"}]), ROSTER)
    with pytest.raises(core.WorkflowError, match="2..8"):
        core.validate_workflow(wf(steps=[{"id": "a", "agent": "build", "prompt": "x",
                                          "parallel": 99, "combine": "shard"}]), ROSTER)
    ok = core.validate_workflow(wf(steps=[{"id": "a", "agent": "build", "prompt": "x",
                                           "parallel": 3, "combine": "best-of"}]), ROSTER)
    assert ok["steps"][0]["parallel"] == 3


def test_shell_cannot_fan_out():
    with pytest.raises(core.WorkflowError, match="cannot fan out"):
        core.validate_workflow(wf(steps=[{"id": "a", "kind": "shell", "run": "t",
                                          "parallel": 2, "combine": "shard"}]), ROSTER)


@pytest.mark.parametrize("raw,seconds", [("30m", 1800), ("90s", 90), ("1h", 3600),
                                         (45, 45), (None, 1800)])
def test_timeout_parse(raw, seconds):
    assert core.parse_timeout(raw) == seconds
    with pytest.raises(core.WorkflowError):
        core.parse_timeout("soon")


def test_plan_order_diamond():
    steps = [{"id": "a", "needs": []}, {"id": "b", "needs": ["a"]},
             {"id": "c", "needs": ["a"]}, {"id": "d", "needs": ["b", "c"]}]
    assert core.plan_order(steps) == [["a"], ["b", "c"], ["d"]]


def test_render_prompt_strict():
    ctx = {"task": "do x", "steps": {"plan": {"output": "the plan", "diff": "@@"}}}
    assert core.render_prompt("Implement: {{ task }} per {{ steps.plan.output }}",
                              ctx) == "Implement: do x per the plan"
    with pytest.raises(core.WorkflowError, match="template error"):
        core.render_prompt("{{ steps.plan.nope }}", ctx)
    with pytest.raises(core.WorkflowError, match="template error"):
        core.render_prompt("{{ typo }}", ctx)


def test_extract_workflow_yaml():
    fenced = "Here you go:\n```yaml\nname: x\nsteps: []\n```\nDone."
    assert core.extract_workflow_yaml(fenced) == "name: x\nsteps: []\n"
    raw = "name: x\nsteps: []"
    assert core.extract_workflow_yaml(raw) == raw
    with pytest.raises(core.WorkflowError, match="no ```yaml block"):
        core.extract_workflow_yaml("sorry, I cannot help with that")


def test_drafter_prompt_contents():
    p = core.build_drafter_prompt(
        "add rate limiting",
        [{"name": "build", "description": "full tools"},
         {"name": "plan", "description": "read-only planning"}])
    assert "add rate limiting" in p
    assert "- build: full tools" in p and "- plan: read-only planning" in p
    assert "```yaml" in p and "ONLY" in p
    # pipeline adaptation variant includes the source pipeline
    p2 = core.build_drafter_prompt("t", [], pipeline="name: ci\nsteps: []")
    assert "Adapt this existing pipeline" in p2 and "name: ci" in p2


def test_full_roundtrip_drafted_yaml():
    reply = "Sure!\n```yaml\n" + yaml.safe_dump(wf()) + "\n```"
    data = yaml.safe_load(core.extract_workflow_yaml(reply))
    w = core.validate_workflow(data, ROSTER)
    assert [s["id"] for s in w["steps"]] == ["implement", "check"]
