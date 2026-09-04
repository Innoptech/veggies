"""harness capability: opencode serve (ADR 0013/0023).

The user-facing component: publishes the only host port, owns the workspace
and home volumes, and describes how `veggies status`/`veggies attach` talk
to it. Status endpoints verified against opencode 1.18.27 (2026-09-04)."""

from __future__ import annotations

import json
from pathlib import Path

from capabilities import (
    HARDENED,
    Component,
    Generated,
    PodContext,
    SecretSpec,
    BuildSpec,
    StackSpec,
    StatusProbe,
    secret_env,
)

# Derived image (official + git, deploy/images/opencode.Containerfile); the
# official one has no git (verified 2026-09-04). Base pinned by tag+digest.
IMAGE_OPENCODE = "localhost/veggies-opencode:1.18.27"
_OPENCODE_CONTAINER_PORT = 4096


def render_opencode_json(infra_repo: Path, router_base_url: str,
                         model: str | None = None) -> str:
    """Stack variant of agent-config/opencode.json: router address injected
    by the caller (ADR 0023) and the master key via env (secretKeyRef)
    instead of an auth.json file."""
    src = json.loads((infra_repo / "agent-config/opencode.json").read_text())
    provider = src["provider"]["litellm"]
    provider["options"]["baseURL"] = router_base_url
    provider["options"]["apiKey"] = "{env:LITELLM_MASTER_KEY}"
    if model:
        src["model"] = f"litellm/{model}"
    return json.dumps(src, indent=2) + "\n"


def _render(ctx: PodContext) -> dict:
    spec = ctx.spec
    publish_ip = "0.0.0.0" if spec.host else "127.0.0.1"
    router = ctx.service("model-router")
    egress = ctx.service("egress")
    return {
        "name": "opencode",
        "image": IMAGE_OPENCODE,
        # opencode writes instance state (.gitignore etc.) into
        # ~/.config/opencode at bootstrap (EROFS 500s on every API call if
        # read-only, verified 2026-09-04) - so stack-config mounts at
        # /stack-config and the wrapper copies opencode.json into the
        # writable home volume before exec'ing the server.
        "command": ["sh", "-c"],
        "args": [
            "mkdir -p /root/.config/opencode && "
            "cp /stack-config/opencode.json /root/.config/opencode/ && "
            # Vendored agents/skills (if shipped) copy alongside; global
            # config dirs are where opencode discovers them.
            "cp -r /stack-config/agents /root/.config/opencode/ 2>/dev/null; "
            "cp -r /stack-config/skills /root/.config/opencode/ 2>/dev/null; "
            f"exec opencode serve --hostname 0.0.0.0 --port {_OPENCODE_CONTAINER_PORT}"
        ],
        "workingDir": "/workspace",
        "env": [
            secret_env("OPENCODE_SERVER_PASSWORD", spec.secret_opencode, "password"),
            secret_env("LITELLM_MASTER_KEY", router.secret, "master_key"),
        ] + [{"name": k, "value": v} for k, v in egress.env.items()],
        "ports": [
            {
                "containerPort": _OPENCODE_CONTAINER_PORT,
                "hostPort": spec.port,
                "hostIP": publish_ip,
            }
        ],
        "volumeMounts": [
            {"name": "repo", "mountPath": "/workspace"},
            # Directory mounts only - subPath file mounts bypass SELinux
            # relabeling and read as EACCES (verified 2026-09-04).
            {"name": "stack-config", "mountPath": "/stack-config", "readOnly": True},
            {"name": "opencode-home", "mountPath": "/root"},
            {"name": "tmp", "mountPath": "/tmp"},
        ],
        "resources": {"limits": {"memory": "512Mi"}},
        "securityContext": HARDENED,
        # exec probe with the tool this image ships: busybox nc.
        # 127.0.0.1, not "localhost": busybox nc tries only the first
        # resolved address (::1) and the server binds IPv4-only.
        "livenessProbe": {
            "exec": {"command": ["sh", "-c", f"nc -z 127.0.0.1 {_OPENCODE_CONTAINER_PORT} || exit 1"]},
            "initialDelaySeconds": 10,
            "periodSeconds": 30,
        },
    }


def _volumes(ctx: PodContext) -> list[dict]:
    repo_path = ctx.spec.repo  # clone mode: CLI clones first, repo is the clone path
    return [
        {"name": "repo", "hostPath": {"path": repo_path, "type": "Directory"}},
        {
            "name": "opencode-home",
            "persistentVolumeClaim": {"claimName": ctx.spec.volume_opencode},
        },
        {"name": "tmp", "emptyDir": {}},
    ]


def _secrets(spec: StackSpec) -> list[SecretSpec]:
    return [SecretSpec("opencode", {"password": Generated(12)})]


def _config_files(ctx: PodContext) -> dict[str, str]:
    infra_repo = ctx.infra_repo
    files = {"opencode.json": render_opencode_json(
        infra_repo,
        router_base_url=ctx.service("model-router").base_url,
        model=ctx.spec.model)}
    # Vendored agents + skills ship as per-stack copies (edit + `veggies up`
    # to apply; the wrapper copies them into opencode's global config dir).
    for sub in ("agents", "skills"):
        src = infra_repo / "agent-config" / sub
        if src.is_dir():
            for f in sorted(src.rglob("*")):
                if f.is_file():
                    files[f"{sub}/{f.relative_to(src)}"] = f.read_text()
    return files


def _probes(spec: StackSpec) -> list[StatusProbe]:
    d = "?directory=/workspace"
    return [
        StatusProbe("model", f"/config{d}",
                    lambda j: str(j.get("model", "?")) if isinstance(j, dict) else "?"),
        StatusProbe("agents", f"/agent{d}",
                    lambda j: str(len(j)) if isinstance(j, list) else "?"),
        StatusProbe("sessions", f"/session{d}",
                    lambda j: str(len(j)) if isinstance(j, list) else "?"),
        StatusProbe("activity", f"/session/status{d}",
                    lambda j: "busy" if j else "idle"),
    ]


def _attach(url: str, password: str) -> list[str]:
    return ["opencode", "attach", url,
            "--username", "opencode", "--password", password]


COMPONENT = Component(
    name="opencode",
    provides="harness",
    requires=("model-router", "egress"),
    render=_render,
    volumes=_volumes,
    secrets=_secrets,
    config_files=_config_files,
    probes=_probes,
    attach=_attach,
    build=BuildSpec(IMAGE_OPENCODE, "deploy/images/opencode.Containerfile"),
)
