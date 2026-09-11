"""supervision capability: always-on critic for kicked sessions (ADR 0036).

An opt-in sidecar (`supervision: supervisor` in veggies.yml) running the
ADR 0028 critic loop inside the pod: poll the harness API on pod loopback,
judge every finish of a KICKED session (`#N:` titles, ADR 0034) with a
different model via the in-pod router, post a refinement when below
threshold. The payload ships as stack-config files (the tested pure logic
from cli/supervisor.py + the daemon from deploy/supervisor/daemon.py), so
the image is nothing but python.

Zero egress by construction: no proxy env is set and the container never
leaves pod loopback. It reuses the harness serve password and the router
master key from their existing podman secrets - declaring none of its own;
the master key never leaves the pod (the ADR 0028 invariant, kept).
"""

from __future__ import annotations

from capabilities import (
    HARDENED,
    BuildSpec,
    Component,
    PodContext,
    StackSpec,
    StatusProbe,
    secret_env,
)

# Pull-only stdlib-python base (same image family as the toolbox sidecar's
# Containerfile base); tag-only pin matches the litellm pull precedent.
IMAGE_SUPERVISOR = "docker.io/library/python:3.13-alpine"


def _render(ctx: PodContext) -> dict:
    harness = ctx.service("harness")
    router = ctx.service("model-router")
    return {
        "name": "supervisor",
        "image": IMAGE_SUPERVISOR,
        # python:<ver>-alpine sets CMD (not ENTRYPOINT) to python3, so args
        # are the whole argv and `python` resolves on PATH - same pattern
        # as the toolbox sidecar.
        "args": ["python", "/stack-config/supervise-daemon.py"],
        "env": [
            secret_env("OPENCODE_SERVER_PASSWORD",
                       harness.secret, harness.secret_key),
            # "master_key" is the router's declared secret key (its
            # ServiceRef does not name it; same consumption pattern as
            # the opencode component).
            secret_env("LITELLM_MASTER_KEY", router.secret, "master_key"),
            {"name": "OPENCODE_URL", "value": harness.base_url},
            {"name": "ROUTER_URL", "value": router.base_url},
            # /stack-config mounts readOnly: never attempt bytecode cache.
            {"name": "PYTHONDONTWRITEBYTECODE", "value": "1"},
            # PASS/STOP are log-only by design, so the pod log is the only
            # operator surface - python block-buffers stdout on a pipe and
            # would hide every verdict behind ~8KB of silence.
            {"name": "PYTHONUNBUFFERED", "value": "1"},
        ],
        "volumeMounts": [
            {"name": "stack-config", "mountPath": "/stack-config", "readOnly": True},
            # Dedicated, agent-unwritable: the heartbeat is the liveness
            # signal for the component that watches the agent - it must
            # not sit on the shared tmp emptyDir the supervisee can write.
            {"name": "supervisor-tmp", "mountPath": "/tmp"},
        ],
        # Message JSON is held whole per judgment; 256Mi keeps a long
        # session's transcript comfortably inside the limit.
        "resources": {"limits": {"memory": "256Mi"}},
        "securityContext": HARDENED,
        # The daemon rewrites the heartbeat at the top of every pass; a
        # wedged pass (e.g. a hung judge call) goes unhealthy within
        # 15 min. The window is generous because a slow-but-working pass
        # (90s messages GET + 180s judge + 90s POST, per session,
        # sequentially) must not read as dead. The first beat lands before
        # any IO, so `veggies up`'s wait_healthy is not held by the judge.
        "livenessProbe": {
            "exec": {"command": [
                "python", "-c",
                "import os,sys,time; sys.exit(0 if time.time()"
                "-os.path.getmtime('/tmp/supervise.heartbeat') < 900 else 1)"]},
            "initialDelaySeconds": 15,
            "periodSeconds": 30,
        },
    }


def _volumes(ctx: PodContext) -> list[dict]:
    # stack-config is squid's declaration (first declaration wins); the
    # supervisor's /tmp is its own (see the mount comment).
    return [{"name": "supervisor-tmp", "emptyDir": {}}]


def _config_files(ctx: PodContext) -> dict[str, str]:
    return {
        # The pytest-covered pure critic logic ships verbatim; the daemon
        # imports it from /stack-config.
        "supervisor.py": (ctx.infra_repo / "cli/supervisor.py").read_text(),
        "supervise-daemon.py": (
            ctx.infra_repo / "deploy" / "supervisor" / "daemon.py").read_text(),
    }


def _probes(spec: StackSpec) -> list[StatusProbe]:
    return [
        StatusProbe(
            "critic",
            exec_argv=("python", "-c",
                       "import json,os,time; print(json.dumps({'ago_s': "
                       "int(time.time()-os.path.getmtime("
                       "'/tmp/supervise.heartbeat'))}))"),
            extract=lambda j: (f"heartbeat {j['ago_s']}s ago"
                               if isinstance(j, dict) and "ago_s" in j
                               else "?"),
        ),
    ]


COMPONENT = Component(
    name="supervisor",
    provides="supervision",
    requires=("harness", "model-router"),
    render=_render,
    volumes=_volumes,
    config_files=_config_files,
    probes=_probes,
    build=BuildSpec(IMAGE_SUPERVISOR),  # pulled, not built
)
