"""Workflow schema v0 core: validation, DAG ordering, prompt rendering,
drafter prompt (ADR 0017).

Pure functions only: no IO, no HTTP, no sqlite - pytest-covered from
tests/test_workflows.py. Shipped into the orchestrator container via the
component's config_files (ADR 0023) and imported by server.py there.
"""

from __future__ import annotations

import re

import yaml
from jinja2 import Environment, StrictUndefined
from jinja2.exceptions import UndefinedError

SCHEMA_VERSION = 0
DEFAULT_TIMEOUT_S = 30 * 60
POLICIES = ("allow", "ask", "deny")
COMBINE = ("best-of", "shard")
WORKFLOW_KEYS = {"name", "permissions", "notify_webhook", "steps"}
STEP_KEYS = {"id", "kind", "agent", "model", "why", "prompt", "run",
             "needs", "parallel", "combine", "gate", "timeout", "required"}
ID_RE = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")
TIMEOUT_RE = re.compile(r"^(\d+)(s|m|h)?$")

_JINJA = Environment(undefined=StrictUndefined)


class WorkflowError(ValueError):
    """Load-time validation failure. Workflows run unattended: fail fast."""


def parse_timeout(value: object, *, step: str = "") -> int:
    """'30m' / '90s' / '1h' / int seconds -> seconds."""
    where = f"step {step!r}: " if step else ""
    if value is None:
        return DEFAULT_TIMEOUT_S
    if isinstance(value, int) and value > 0:
        return value
    if isinstance(value, str):
        m = TIMEOUT_RE.match(value.strip())
        if m:
            n = int(m.group(1))
            mult = {"s": 1, "m": 60, "h": 3600, None: 1}[m.group(2)]
            if n * mult > 0:
                return n * mult
    raise WorkflowError(f"{where}bad timeout {value!r} (use 90s/30m/1h or seconds)")


def validate_workflow(data: object, roster: list[str] | None = None) -> dict:
    """Validate + normalize a workflow mapping. roster = live agent names
    from GET /agent (ADR 0017: the dispatch API silently no-ops on unknown
    agents, so we check at load). Returns the normalized workflow."""
    if not isinstance(data, dict):
        raise WorkflowError("workflow must be a mapping")
    unknown = set(data) - WORKFLOW_KEYS
    if unknown:
        raise WorkflowError(f"unknown workflow keys: {', '.join(sorted(unknown))}")
    name = data.get("name", "run")
    if not isinstance(name, str) or not ID_RE.match(name):
        raise WorkflowError(f"bad workflow name {name!r}")
    policy = data.get("permissions", "deny")
    if policy not in POLICIES:
        raise WorkflowError(f"permissions must be one of {POLICIES}")
    hook = data.get("notify_webhook")
    if hook is not None and not (isinstance(hook, str) and hook.startswith("http")):
        raise WorkflowError("notify_webhook must be an http(s) URL")

    raw_steps = data.get("steps")
    if not isinstance(raw_steps, list) or not raw_steps:
        raise WorkflowError("steps must be a non-empty list")

    steps: dict[str, dict] = {}
    for i, raw in enumerate(raw_steps):
        if not isinstance(raw, dict):
            raise WorkflowError(f"step #{i + 1} must be a mapping")
        sid = raw.get("id")
        if not isinstance(sid, str) or not ID_RE.match(sid):
            raise WorkflowError(f"step #{i + 1}: bad or missing id {sid!r}")
        if sid in steps:
            raise WorkflowError(f"duplicate step id {sid!r}")
        bad = set(raw) - STEP_KEYS
        if bad:
            raise WorkflowError(f"step {sid!r}: unknown keys: {', '.join(sorted(bad))}")
        kind = raw.get("kind", "agent")
        if kind not in ("agent", "shell"):
            raise WorkflowError(f"step {sid!r}: kind must be agent|shell")
        step = {
            "id": sid,
            "kind": kind,
            "needs": list(raw.get("needs") or []),
            "timeout": parse_timeout(raw.get("timeout"), step=sid),
            "gate": raw.get("gate"),
            "why": raw.get("why", ""),
            "required": bool(raw.get("required", False)),
        }
        if step["gate"] not in (None, "approval"):
            raise WorkflowError(f"step {sid!r}: gate must be 'approval' if set")
        if kind == "agent":
            agent = raw.get("agent")
            if not agent:
                raise WorkflowError(f"step {sid!r}: agent steps need 'agent'")
            if roster is not None and agent not in roster:
                raise WorkflowError(
                    f"step {sid!r}: unknown agent {agent!r} "
                    f"(roster: {', '.join(sorted(roster))})")
            if not raw.get("prompt"):
                raise WorkflowError(f"step {sid!r}: agent steps need 'prompt'")
            step.update(agent=agent, prompt=raw["prompt"], model=raw.get("model"))
            parallel = raw.get("parallel")
            combine = raw.get("combine")
            if parallel is not None:
                if not (isinstance(parallel, int) and 2 <= parallel <= 8):
                    raise WorkflowError(f"step {sid!r}: parallel must be an int 2..8")
                if combine not in COMBINE:
                    raise WorkflowError(
                        f"step {sid!r}: parallel requires combine: {'|'.join(COMBINE)}")
            elif combine is not None:
                raise WorkflowError(f"step {sid!r}: combine without parallel")
            step.update(parallel=parallel, combine=combine)
        else:
            if not raw.get("run"):
                raise WorkflowError(f"step {sid!r}: shell steps need 'run'")
            if raw.get("parallel") or raw.get("combine"):
                raise WorkflowError(f"step {sid!r}: shell steps cannot fan out")
            step["run"] = raw["run"]
        steps[sid] = step

    for sid, step in steps.items():
        for dep in step["needs"]:
            if dep not in steps:
                raise WorkflowError(f"step {sid!r}: needs unknown step {dep!r}")
    order = plan_order(list(steps.values()))  # raises on cycles
    return {"name": name, "permissions": policy, "notify_webhook": hook,
            "steps": list(steps.values()), "order": order}


def plan_order(steps: list[dict]) -> list[list[str]]:
    """Kahn's algorithm tiers: steps in the same tier have all dependencies
    in earlier tiers and may run concurrently (executor still serializes
    tiers; within-tier concurrency is the orchestrator's knob)."""
    by_id = {s["id"]: s for s in steps}
    remaining = {sid: set(s["needs"]) for sid, s in by_id.items()}
    tiers: list[list[str]] = []
    done: set[str] = set()
    while remaining:
        ready = sorted(sid for sid, deps in remaining.items() if deps <= done)
        if not ready:
            cycle = ", ".join(sorted(remaining))
            raise WorkflowError(f"needs-cycle among: {cycle}")
        tiers.append(ready)
        done.update(ready)
        for sid in ready:
            del remaining[sid]
    return tiers


def render_prompt(template: str, context: dict) -> str:
    """Jinja2 strict-undefined: a typo'd placeholder fails at load/render
    time, never silently mid-run (ADR 0017)."""
    try:
        return _JINJA.from_string(template).render(**context)
    except UndefinedError as e:
        raise WorkflowError(f"template error: {e}") from e


def to_submission(wf: dict) -> dict:
    """The inverse of validate_workflow's normalization: strip derived
    (`order`) and default/None values so the doc round-trips through
    validation (draft -> confirm -> run)."""
    out: dict = {"name": wf["name"], "steps": []}
    if wf.get("permissions", "deny") != "deny":
        out["permissions"] = wf["permissions"]
    if wf.get("notify_webhook"):
        out["notify_webhook"] = wf["notify_webhook"]
    for s in wf["steps"]:
        st: dict = {"id": s["id"], "kind": s["kind"]}
        if s["kind"] == "agent":
            st["agent"] = s["agent"]
            if s.get("model"):
                st["model"] = s["model"]
            st["prompt"] = s["prompt"]
            if s.get("parallel"):
                st["parallel"] = s["parallel"]
                st["combine"] = s["combine"]
        else:
            st["run"] = s["run"]
        if s.get("why"):
            st["why"] = s["why"]
        if s.get("needs"):
            st["needs"] = s["needs"]
        if s.get("gate"):
            st["gate"] = s["gate"]
        if s.get("required"):
            st["required"] = True
        if s.get("timeout", DEFAULT_TIMEOUT_S) != DEFAULT_TIMEOUT_S:
            st["timeout"] = s["timeout"]
        out["steps"].append(st)
    return out


# --- Drafter -----------------------------------------------------------------

SCHEMA_HINT = """\
Workflow YAML schema v0:
  name: <slug>
  permissions: allow|ask|deny        # default deny
  steps:
    - id: <slug>
      kind: agent                    # or: shell
      agent: <roster name>           # agent steps only
      model: <alias>                 # optional
      why: "<one line: why this step exists for THIS task>"   # required of you
      prompt: "..."                  # agent steps; may use {{ task }},
                                     # {{ steps.<id>.output }}, {{ steps.<id>.diff }}
      run: "<shell command>"         # shell steps only (checks: tests, lint)
      needs: [<step ids>]            # default []
      parallel: <2-8>                # optional fan-out; requires combine
      combine: best-of|shard         # best-of: N attempts, later step picks;
                                     # shard: split into disjoint subtasks
      gate: approval                 # optional human gate
Rules: agent steps use ONLY roster agents; include a shell check step that
runs the project's tests after any code-writing step; keep the pipeline as
SMALL as the task allows (a trivial task is one step); end with a review
step (roster permitting) for anything non-trivial.
"""


def build_drafter_prompt(task: str, roster: list[dict], pipeline: str | None = None) -> str:
    """The prompt sent to the drafter agent (ADR 0017: generation subsumes
    planner selection). roster: live GET /agent entries (name+description)."""
    lines = ["You are the veggies workflow drafter. Produce ONE workflow in a "
             "single ```yaml fenced block and nothing else.",
             "", "Roster (the only agents you may name):"]
    for a in roster:
        lines.append(f"  - {a.get('name')}: {a.get('description', '')}")
    lines += ["", SCHEMA_HINT]
    if pipeline:
        lines += ["Adapt this existing pipeline to the task (keep its required "
                  "steps and approval gates):", "```yaml", pipeline, "```", ""]
    lines += ["The task:", "---", task, "---",
              "Remember: output ONLY the ```yaml block."]
    return "\n".join(lines)


def extract_workflow_yaml(reply: str) -> str:
    """Pull the workflow YAML out of a drafter reply: the fenced block, or
    the whole reply if it parses as a mapping with steps."""
    m = re.search(r"```(?:yaml|yml)\s*\n(.*?)```", reply, re.S)
    if m:
        return m.group(1)
    try:
        data = yaml.safe_load(reply)
    except yaml.YAMLError as e:
        raise WorkflowError(f"drafter reply is not YAML and has no fenced block: {e}")
    if isinstance(data, dict) and "steps" in data:
        return reply
    raise WorkflowError("drafter reply contains no ```yaml block and is not a workflow")
