"""One-shot self-config for the canvas container (ADR 0025).

Waits for the agent server, finds its generated session API key (a
loopback-only random token passed as a process arg), and points the
backend at the stack's harness via a custom ACP command. Idempotent: the
PATCH restates the same settings on every boot. Runs backgrounded from
the container wrapper; failures only mean ACP stays unconfigured (the UI
shows the onboarding form instead).

Env: VEGGIES_ACP_TARGET (harness container name), VEGGIES_MODEL (ACP-side
model id), VEGGIES_SDK_MODEL + VEGGIES_ROUTER_BASE + LITELLM_MASTER_KEY
(the second-harness LLM profile, ADR 0027).
"""

import json
import os
import shlex
import time
import urllib.request
from pathlib import Path

API = "http://127.0.0.1:8000"


def session_key() -> str | None:
    for cmdline in Path("/proc").glob("[0-9]*/cmdline"):
        try:
            args = cmdline.read_bytes().decode(errors="replace").split("\0")
        except OSError:
            continue
        if "--session-api-key" in args:
            return args[args.index("--session-api-key") + 1]
    return None


def patch(key: str, body: dict) -> None:
    req = urllib.request.Request(
        f"{API}/api/settings", method="PATCH",
        data=json.dumps(body).encode(),
        headers={"X-Session-API-Key": key, "Content-Type": "application/json"})
    urllib.request.urlopen(req, timeout=15)


def post(key: str, path: str, body: dict) -> None:
    req = urllib.request.Request(
        f"{API}{path}", method="POST",
        data=json.dumps(body).encode(),
        headers={"X-Session-API-Key": key, "Content-Type": "application/json"})
    urllib.request.urlopen(req, timeout=15)


def main() -> None:
    key = None
    for _ in range(90):  # ~3 min of startup grace
        try:
            urllib.request.urlopen(f"{API}/health", timeout=2)
            key = session_key()
            if key:
                break
        except Exception:
            pass
        time.sleep(2)
    if not key:
        raise SystemExit("canvas bootstrap: agent server never came up")
    target = os.environ["VEGGIES_ACP_TARGET"]
    diff = {
        "agent_kind": "acp",
        "acp_server": "custom",
        "acp_command": shlex.split(
            f"podman exec -i -w /workspace {target} opencode acp"),
    }
    model = os.environ.get("VEGGIES_MODEL")
    if model:
        diff["acp_model"] = model
    patch(key, {"agent_settings_diff": diff})
    print(f"canvas bootstrap: ACP configured -> {target}", flush=True)

    # ADR 0027: second harness. Save + activate an LLM profile pointing at
    # the in-pod router (verified: conversations resolve their LLM from the
    # ACTIVE PROFILE, not from agent_settings.llm; inline api_key in a
    # conversation request is dropped). Profiles API: POST /api/profiles/
    # {name} then /activate.
    sdk_model = os.environ.get("VEGGIES_SDK_MODEL")
    master_key = os.environ.get("LITELLM_MASTER_KEY")
    if sdk_model and master_key:
        post(key, "/api/profiles/veggies-litellm", {
            "llm": {
                "model": sdk_model,
                "base_url": os.environ["VEGGIES_ROUTER_BASE"],
                "api_key": master_key,
                "usage_id": "veggies-litellm",
            },
            "include_secrets": True,
        })
        post(key, "/api/profiles/veggies-litellm/activate", {})
        print(f"canvas bootstrap: LLM profile veggies-litellm active "
              f"({sdk_model} via in-pod router)", flush=True)


if __name__ == "__main__":
    main()
