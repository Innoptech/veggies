"""egress capability: squid forward proxy.

Owns the proxy contract other components consume via ctx.service("egress"):
base_url + env dict (both case variants; busybox tools only honor the
lowercase forms, verified 2026-09-04)."""

from __future__ import annotations

from capabilities import HARDENED, BuildSpec, Component, PodContext, ServiceRef, StackSpec

IMAGE_SQUID = "localhost/squid:latest"  # ansible/roles/egress/files/squid.Containerfile
_SQUID_PORT = 3128  # pod-internal only, never published

# Mirrors ansible/roles/egress/defaults/main.yml (egress_allowlist_base) -
# drift test enforces equality. In-pod traffic is loopback or the pasta
# gateway only.
SQUID_ACL_SRC = ["127.0.0.0/8", "169.254.0.0/16"]
SQUID_ALLOWLIST_BASE = [
    "github.com",
    "api.github.com",
    ".githubusercontent.com",
    "codeload.github.com",
    "registry.npmjs.org",
    "pypi.org",
    "files.pythonhosted.org",
    "archive.ubuntu.com",
    "security.ubuntu.com",
    "dl-cdn.alpinelinux.org",  # alpine package index (opencode image builds)
    "production.cloudfront.docker.com",  # docker hub blob CDN (seen live 2026-09-08)
    "ghcr.io",
    "registry-1.docker.io",
    "auth.docker.io",
    "production.cloudflare.docker.com",
]
SQUID_MODEL_ENDPOINTS = ["api.fireworks.ai"]  # group_vars egress_model_endpoints

# Squid starts as root and setuids to the proxy user (verified 2026-09-04:
# "initgroups: unable to set groups" crash with drop-ALL).
HARDENED_SQUID = {
    **HARDENED,
    "capabilities": {"drop": ["ALL"], "add": ["SETUID", "SETGID"]},
}


def render_allowlist(extra: list[str] | None = None) -> str:
    """Base + model endpoints + per-component egress domains (ADR 0018:
    selected components declare what they need; squid merges)."""
    extra = sorted(set(extra or []))
    return "\n".join(SQUID_ALLOWLIST_BASE + SQUID_MODEL_ENDPOINTS + extra) + "\n"


def render_squid_conf(chained: bool = False) -> str:
    src = " ".join(SQUID_ACL_SRC)
    chain = ""
    if chained:
        # Remote stacks live on a substrate host whose nftables denies direct
        # egress for the stacks user (verified 2026-09-08: everything 503s).
        # Parent through the substrate's host-published proxy via the pasta
        # gateway; the host's own allowlist is the boundary upstream.
        chain = f"""
# Chained egress: parent is the substrate proxy on the host (the stacks user
# may not egress directly - ansible egress role's nftables).
cache_peer host.containers.internal parent {_SQUID_PORT} 0 no-query default
never_direct allow all
"""
    return f"""# Rendered by veggies - mirrors ansible/roles/egress/templates/squid.conf.j2.
http_port {_SQUID_PORT}
{chain}
acl allowed_src src {src}
acl allowed_sites dstdomain "/stack-config/allowlist.txt"
acl SSL_ports port 443
acl CONNECT method CONNECT
# Pod-loopback destinations (e.g. the router at 4000): some clients'
# proxy libs ignore no_proxy (aiohttp; verified 2026-09-09) and would
# CONNECT through us. Allow pod-local targets; the internet boundary
# below is unchanged.
acl pod_local dst 127.0.0.1

http_access deny CONNECT !SSL_ports
http_access allow pod_local
http_access deny !allowed_src
http_access allow allowed_sites
http_access deny all

# No caching: `cache deny all` suffices; Ubuntu's squid lacks the null store
# module, and the PID file must not persist across in-pod restarts (emptyDir
# is pod-scoped) or squid crash-loops on "already running". Both verified
# 2026-09-04.
cache deny all
pid_filename none

access_log stdio:/proc/self/fd/1
logfile_rotate 0
"""


def _service_ref(spec: StackSpec) -> ServiceRef:
    proxy = f"http://127.0.0.1:{_SQUID_PORT}"
    return ServiceRef(
        capability="egress",
        base_url=proxy,
        env={
            "HTTP_PROXY": proxy,
            "HTTPS_PROXY": proxy,
            "http_proxy": proxy,
            "https_proxy": proxy,
            "NO_PROXY": "127.0.0.1,localhost",
            "no_proxy": "127.0.0.1,localhost",
        },
    )


def _render(ctx: PodContext) -> dict:
    return {
        "name": "squid",
        "image": IMAGE_SQUID,
        "args": ["-f", "/stack-config/squid.conf"],
        "volumeMounts": [
            {"name": "stack-config", "mountPath": "/stack-config", "readOnly": True},
            {"name": "run", "mountPath": "/run"},
            {"name": "tmp", "mountPath": "/tmp"},
        ],
        "resources": {"limits": {"memory": "128Mi"}},
        "securityContext": HARDENED_SQUID,
        # kube play maps tcpSocket probes to `nc` inside the container, which
        # these minimal images lack (verified 2026-09-04) - exec probes with
        # tools each image actually ships: bash /dev/tcp here.
        "livenessProbe": {
            "exec": {"command": ["bash", "-c", f"exec 3<>/dev/tcp/127.0.0.1/{_SQUID_PORT} || exit 1"]},
            "initialDelaySeconds": 5,
            "periodSeconds": 30,
        },
    }


def _volumes(ctx: PodContext) -> list[dict]:
    return [
        {"name": "stack-config", "hostPath": {"path": ctx.spec.config_dir(), "type": "Directory"}},
        {"name": "run", "emptyDir": {}},
    ]


def _config_files(ctx: PodContext) -> dict[str, str]:
    # spec.host set = running on the substrate host = chain to its proxy.
    extra = sorted({d for c in ctx.components for d in c.egress_domains(ctx)})
    return {"squid.conf": render_squid_conf(chained=ctx.spec.host is not None),
            "allowlist.txt": render_allowlist(extra)}


COMPONENT = Component(
    name="squid",
    provides="egress",
    requires=(),
    render=_render,
    volumes=_volumes,
    service_ref=_service_ref,
    config_files=_config_files,
    build=BuildSpec(IMAGE_SQUID, "ansible/roles/egress/files/squid.Containerfile"),
)
