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

import hashlib
import json
import os
import shlex
import time
import urllib.request
from pathlib import Path

API = "http://127.0.0.1:8000"
# The critic renderer's tokenizer config (sdk chat_template.py caches it as
# plain JSON keyed by md5(name)); pre-seeded from the vendored copy so the
# huggingface.co fetch never happens (ADR 0027).
_TOKENIZER_NAME = "Qwen/Qwen3-4B-Instruct-2507"


def seed_tokenizer_cache() -> None:
    cache_dir = Path.home() / ".cache" / "chat_templates"
    target = cache_dir / (hashlib.md5(_TOKENIZER_NAME.encode()).hexdigest()
                          + "_tokenizer_config.json")
    if target.exists():
        return
    cache_dir.mkdir(parents=True, exist_ok=True)
    target.write_text(
        Path("/stack-config/tokenizer_config.qwen3-4b.json").read_text())
    print(f"canvas bootstrap: tokenizer cache seeded ({_TOKENIZER_NAME})",
          flush=True)


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
    seed_tokenizer_cache()
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

    # ADR 0027: second harness. Verified against the live agent server
    # (2026-09-09): conversations resolve their agent from an AGENT PROFILE
    # (agent_profile_id), which references an LLM PROFILE by name; inline
    # api_keys in conversation requests are dropped by design. So: save the
    # LLM profile (router + master key), then an agent profile pointing at
    # it; the UI lists it as a one-click kind for new conversations.
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
        post(key, "/api/agent-profiles/veggies-openhands", {
            "agent_kind": "openhands",
            "llm_profile_ref": "veggies-litellm",
            # ADR 0027: critic ON via our in-pod shim (/classify adapter,
            # critic_shim.py; judge = deepseek-v4 via the in-pod router).
            # The renderer's tokenizer config is vendored (seeded by this
            # bootstrap); nothing leaves the pod.
            "verification": {
                "critic_enabled": True,
                "critic_server_url": "http://127.0.0.1:4401",
                "critic_model_name": os.environ.get("VEGGIES_JUDGE_MODEL",
                                                    "deepseek-v4"),
                "enable_iterative_refinement": True,
                "critic_threshold": 0.6,
                "max_refinement_iterations": 2,
            },
        })
        print(f"canvas bootstrap: LLM profile veggies-litellm + agent profile "
              f"veggies-openhands ready ({sdk_model} via in-pod router, "
              "critic on)", flush=True)


if __name__ == "__main__":
    main()
