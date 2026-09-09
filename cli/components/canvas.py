"""control-plane capability: OpenHands Agent Canvas (ADR 0025).

Browser supervision for the stack: multi-conversation UI, automations
(cron + git-synced; webhooks stay off under ADR 0024), diff review. It
drives the harness over ACP by spawning `opencode acp` INSIDE the harness
container via the host's rootless podman socket (the same-user boundary is
what makes that acceptable: the socket lets Canvas manage exactly the
containers the stack user already owns). ACP settings self-configure at
boot via deploy/canvas/bootstrap.py (shipped in /stack-config).

Two deliberate deviations from HARDENED, both required and verified in the
ADR 0025 spike: seLinuxOptions spc_t (the podman socket connect is denied
under container_t, even relabeled) and no readOnlyRootFilesystem (the
canvas app writes runtime state outside its mounted dirs). Publishes
127.0.0.1 only - never 0.0.0.0 (ADR 0024 posture). Opt in via veggies.yml
`canvas: builtin`."""

from __future__ import annotations

from capabilities import (
    BuildSpec,
    Component,
    PodContext,
    StackSpec,
    StatusProbe,
)

# Derived image (upstream + podman-remote, deploy/images/canvas.Containerfile).
# Base pinned by tag+digest in the Containerfile; bump together.
IMAGE_CANVAS = "localhost/veggies-canvas:1.16.0"
_CANVAS_CONTAINER_PORT = 8000  # canvas ingress (UI + API)
# Host port is the harness port + 1000: deterministic 1:1, collision-free
# (harness ports are capped at OPENCODE_PORT_BASE + 100).
HOST_PORT_OFFSET = 1000


def _render(ctx: PodContext) -> dict:
    spec = ctx.spec
    if not spec.runtime_dir:
        raise ValueError("canvas needs spec.runtime_dir (set by `veggies up`)")
    harness = ctx.providers.get("harness")
    if harness is None:
        raise ValueError("canvas requires a harness component in the stack")
    return {
        "name": "canvas",
        "image": IMAGE_CANVAS,
        # bootstrap patches ACP settings once the agent server is up, then
        # the stock entrypoint runs. Container root maps to the stack user
        # on the host (default rootless userns) - that identity is what the
        # podman socket checks.
        "command": ["sh", "-c",
                    "python3 /stack-config/canvas-bootstrap.py & "
                    "exec tini -- /opt/agent-canvas/entrypoint.sh"],
        "env": [
            {"name": "HOME", "value": "/home/openhands"},
            {"name": "CONTAINER_HOST", "value": "unix:///run/podman/podman.sock"},
            {"name": "VEGGIES_ACP_TARGET", "value": f"{spec.pod}-{harness.name}"},
            {"name": "VEGGIES_MODEL", "value": f"litellm/{spec.model or 'kimi-k3'}"},
            # The canvas services' own fetches ride the in-pod egress proxy.
            {"name": "HTTPS_PROXY", "value": "http://127.0.0.1:3128"},
            {"name": "HTTP_PROXY", "value": "http://127.0.0.1:3128"},
            {"name": "NO_PROXY", "value": "localhost,127.0.0.1"},
        ],
        "ports": [
            {
                "containerPort": _CANVAS_CONTAINER_PORT,
                "hostPort": spec.port + HOST_PORT_OFFSET,
                "hostIP": "127.0.0.1",  # loopback even on remote hosts (ADR 0024)
            }
        ],
        "volumeMounts": [
            {"name": "repo", "mountPath": "/workspace"},
            {"name": "stack-config", "mountPath": "/stack-config", "readOnly": True},
            {"name": "canvas-state", "mountPath": "/home/openhands/.openhands"},
            # The podman DIR (not the socket file): a directory hostPath mount
            # sidesteps kube hostPath type:Socket support questions; connect
            # needs rw on the socket, so no readOnly here.
            {"name": "podman-runtime", "mountPath": "/run/podman"},
            {"name": "tmp", "mountPath": "/tmp"},
        ],
        "resources": {"limits": {"memory": "1Gi"}},
        # runAsUser 0 is NOT a privilege grab: under the default rootless
        # userns, container root maps to the stack user on the host - the
        # exact identity the podman socket + stack-config files check.
        # (Running as the image's openhands uid maps to a subuid and loses
        # both; verified live 2026-09-08.) spc_t: the socket connect is
        # denied under container_t even relabeled (ADR 0025 spike).
        "securityContext": {"runAsUser": 0, "runAsGroup": 0,
                            "seLinuxOptions": {"type": "spc_t"}},
        # python3 ships in the image (used for the probe and bootstrap).
        "livenessProbe": {
            "exec": {"command": [
                "python3", "-c",
                f"import socket; socket.create_connection(('127.0.0.1', {_CANVAS_CONTAINER_PORT}), 3)"]},
            "initialDelaySeconds": 20,
            "periodSeconds": 30,
        },
    }


def _volumes(ctx: PodContext) -> list[dict]:
    spec = ctx.spec
    return [
        # Name shared with the harness's mount: merged by name, same path.
        {"name": "repo", "hostPath": {"path": spec.repo, "type": "Directory"}},
        {"name": "canvas-state",
         "hostPath": {"path": f"{spec.state_root()}/{spec.name}/canvas-state",
                      "type": "Directory"}},
        {"name": "podman-runtime",
         "hostPath": {"path": f"{spec.runtime_dir}/podman", "type": "Directory"}},
        {"name": "tmp", "emptyDir": {}},
    ]


def _config_files(ctx: PodContext) -> dict[str, str]:
    src = ctx.infra_repo / "deploy" / "canvas" / "bootstrap.py"
    return {"canvas-bootstrap.py": src.read_text()}


def _probes(spec: StackSpec) -> list[StatusProbe]:
    return [StatusProbe(
        label="canvas",
        exec_argv=("python3", "-c",
                   "import urllib.request; "
                   "urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=5); "
                   "print('{\"ui\": \"up\"}')"),
        extract=lambda j: j.get("ui", "?") if isinstance(j, dict) else str(j))]


COMPONENT = Component(
    name="canvas",
    provides="control-plane",
    requires=("harness",),
    render=_render,
    volumes=_volumes,
    config_files=_config_files,
    probes=_probes,
    build=BuildSpec(IMAGE_CANVAS, "deploy/images/canvas.Containerfile"),
)
