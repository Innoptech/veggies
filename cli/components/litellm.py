"""model-router capability: LiteLLM gateway (ADR 0011/0023).

Sole owner of the image pin since ADR 0016. Exposes an OpenAI-compatible
endpoint pod-internally; holds the real provider keys so nothing else in
the pod ever sees them."""

from __future__ import annotations

from capabilities import (
    HARDENED,
    Component,
    Generated,
    PodContext,
    SecretSpec,
    ServiceRef,
    StackSpec,
    VaultKey,
    secret_env,
)

IMAGE_LITELLM = "ghcr.io/berriai/litellm:v1.99.1"
_LITELLM_PORT = 4000  # pod-internal only, never published


def _service_ref(spec: StackSpec) -> ServiceRef:
    return ServiceRef(
        capability="model-router",
        base_url=f"http://127.0.0.1:{_LITELLM_PORT}/v1",
        secret=f"{spec.pod}-litellm",
    )


def _render(ctx: PodContext) -> dict:
    spec = ctx.spec
    return {
        "name": "litellm",
        "image": IMAGE_LITELLM,
        "args": ["--config", "/agent-config/config.yaml", "--port", str(_LITELLM_PORT), "--host", "0.0.0.0"],
        "env": [
            secret_env("LITELLM_MASTER_KEY", spec.secret_litellm, "master_key"),
            secret_env("LITELLM_SALT_KEY", spec.secret_litellm, "salt_key"),
            secret_env("FIREWORKS_API_KEY", spec.secret_litellm, "fireworks_api_key"),
            # litellm calls providers through the egress proxy too: on the
            # VPS the per-UID nftables rules drop anything else (ADR 0006).
        ] + [{"name": k, "value": v} for k, v in ctx.service("egress").env.items()],
        "volumeMounts": [
            {"name": "agent-config", "mountPath": "/agent-config", "readOnly": True},
            {"name": "tmp", "mountPath": "/tmp"},
        ],
        "resources": {"limits": {"memory": "768Mi"}},
        "securityContext": HARDENED,
        # exec probes only: the image ships python3 but no nc (verified
        # 2026-09-04).
        "livenessProbe": {
            "exec": {"command": ["sh", "-c",
                                 f"python3 -c \"import socket; socket.create_connection(('127.0.0.1', {_LITELLM_PORT}), 3)\" || exit 1"]},
            "initialDelaySeconds": 15,
            "periodSeconds": 30,
        },
    }


def _volumes(ctx: PodContext) -> list[dict]:
    # Local: live-mount the vendored config (edit + `veggies up` to apply).
    # Remote: a rendered copy sits in the stack's config dir on that host.
    spec = ctx.spec
    litellm_cfg = (
        spec.config_dir() if spec.is_remote
        else str(ctx.infra_repo / "agent-config" / "litellm")
    )
    return [
        {"name": "agent-config", "hostPath": {"path": litellm_cfg, "type": "Directory"}},
    ]


def _secrets(spec: StackSpec) -> list[SecretSpec]:
    return [SecretSpec("litellm", {
        "master_key": Generated(32),
        "salt_key": Generated(32),
        "fireworks_api_key": VaultKey("fireworks_api_key"),
    })]


def _config_files(ctx: PodContext) -> dict[str, str]:
    if not ctx.spec.is_remote:
        return {}  # local: agent-config/litellm is live-mounted instead
    # No infra checkout on the VPS: ship the litellm config as a copy.
    return {"config.yaml": (ctx.infra_repo / "agent-config/litellm/config.yaml").read_text()}


COMPONENT = Component(
    name="litellm",
    provides="model-router",
    requires=("egress",),
    render=_render,
    volumes=_volumes,
    service_ref=_service_ref,
    secrets=_secrets,
    config_files=_config_files,
)
