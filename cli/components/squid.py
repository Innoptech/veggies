"""egress capability: squid forward proxy (ADR 0023).

Owns the proxy contract other components consume via ctx.service("egress"):
base_url + env dict (both case variants; busybox tools only honor the
lowercase forms, verified 2026-09-04)."""

from __future__ import annotations

from capabilities import HARDENED, Component, PodContext, ServiceRef, StackSpec

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


def render_allowlist() -> str:
    return "\n".join(SQUID_ALLOWLIST_BASE + SQUID_MODEL_ENDPOINTS) + "\n"


def render_squid_conf() -> str:
    src = " ".join(SQUID_ACL_SRC)
    return f"""# Rendered by veggies (ADR 0013) - mirrors ansible/roles/egress/templates/squid.conf.j2.
http_port {_SQUID_PORT}

acl allowed_src src {src}
acl allowed_sites dstdomain "/stack-config/allowlist.txt"
acl SSL_ports port 443
acl CONNECT method CONNECT

http_access deny CONNECT !SSL_ports
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
    return {"squid.conf": render_squid_conf(), "allowlist.txt": render_allowlist()}


COMPONENT = Component(
    name="squid",
    provides="egress",
    requires=(),
    render=_render,
    volumes=_volumes,
    service_ref=_service_ref,
    config_files=_config_files,
)
