"""One-shot self-config for the canvas container (ADR 0025).

Waits for the agent server, finds its generated session API key (a
loopback-only random token passed as a process arg), and points the
backend at the stack's harness via a custom ACP command. Idempotent: the
PATCH restates the same settings on every boot. Runs backgrounded from
the container wrapper; failures only mean ACP stays unconfigured (the UI
shows the onboarding form instead).

Env: VEGGIES_ACP_TARGET (harness container name), VEGGIES_MODEL.
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
    req = urllib.request.Request(
        f"{API}/api/settings", method="PATCH",
        data=json.dumps({"agent_settings_diff": diff}).encode(),
        headers={"X-Session-API-Key": key, "Content-Type": "application/json"})
    urllib.request.urlopen(req, timeout=15)
    print(f"canvas bootstrap: ACP configured -> {target}", flush=True)


if __name__ == "__main__":
    main()
