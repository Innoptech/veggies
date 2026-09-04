"""orchestrator capability: adaptive pipelines over the harness API
(ADR 0017/0023).

Drives the harness via ctx.service("harness") (base_url + secret +
secret_key); code ships via config_files (restart = update, no rebuild).
Runs shell steps itself against the same mounted /workspace (real exit
codes). Not in the default selection: opt in via veggies.yml
`orchestrator: builtin`."""

from __future__ import annotations

from capabilities import (
    HARDENED,
    BuildSpec,
    Component,
    PodContext,
    ServiceRef,
    StackSpec,
    StatusProbe,
    secret_env,
)

IMAGE_ORCHESTRATOR = "localhost/veggies-orchestrator:0.1.0"
_PORT = 4400  # pod-internal only, never published

_SERVER_FILES = ("orchestrator-server.py", "orchestrator-core.py",
                 "orchestrator-client.py")


def _render(ctx: PodContext) -> dict:
    spec = ctx.spec
    harness = ctx.service("harness")
    if not harness.secret or not harness.secret_key:
        raise ValueError("orchestrator requires a harness with credential "
                         "(secret + secret_key) in its ServiceRef")
    return {
        "name": "orchestrator",
        "image": IMAGE_ORCHESTRATOR,
        # wrapper: ensure state dir (mount parent exists; subdir ours)
        "command": ["sh", "-c",
                    "mkdir -p /stack-state/orchestrator && "
                    "exec python3 /stack-config/orchestrator-server.py"],
        "env": [
            {"name": "HARNESS_URL", "value": harness.base_url},
            secret_env("HARNESS_PASSWORD", harness.secret, harness.secret_key),
        ],
        "volumeMounts": [
            {"name": "stack-config", "mountPath": "/stack-config", "readOnly": True},
            {"name": "stack-state", "mountPath": "/stack-state"},
            {"name": "repo", "mountPath": "/workspace"},
            {"name": "tmp", "mountPath": "/tmp"},
        ],
        "resources": {"limits": {"memory": "256Mi"}},
        "securityContext": HARDENED,
        # python3 exists in the image; no nc needed
        "livenessProbe": {
            "exec": {"command": [
                "python3", "-c",
                f"import socket; socket.create_connection(('127.0.0.1', {_PORT}), 3)"]},
            "initialDelaySeconds": 10,
            "periodSeconds": 30,
        },
    }


def _volumes(ctx: PodContext) -> list[dict]:
    spec = ctx.spec
    return [
        # The stack's whole state dir (contains config/): the orchestrator's
        # sqlite lives in its orchestrator/ subdir (mkdir'd by the wrapper).
        {"name": "stack-state", "hostPath": {"path": f"{spec.state_root()}/{spec.name}",
                                             "type": "Directory"}},
        # Shell steps run against the same workspace the harness edits.
        {"name": "repo", "hostPath": {"path": spec.repo, "type": "Directory"}},
        {"name": "tmp", "emptyDir": {}},
    ]


def _config_files(ctx: PodContext) -> dict[str, str]:
    src = ctx.infra_repo / "deploy" / "orchestrator"
    names = {"orchestrator-server.py": "server.py",
             "orchestrator-core.py": "core.py",
             "orchestrator-client.py": "client.py"}
    return {ship: (src / local).read_text() for ship, local in names.items()}


def _probes(spec: StackSpec) -> list[StatusProbe]:
    return [StatusProbe(
        label="orchestrator",
        exec_argv=("python3", "/stack-config/orchestrator-client.py",
                   "status", "--one-line"),
        extract=lambda j: j if isinstance(j, str) else str(j))]


COMPONENT = Component(
    name="orchestrator",
    provides="orchestrator",
    requires=("harness",),
    render=_render,
    volumes=_volumes,
    config_files=_config_files,
    probes=_probes,
    build=BuildSpec(IMAGE_ORCHESTRATOR, "deploy/images/orchestrator.Containerfile"),
)
