"""github-auth capability: rotating GitHub App installation tokens for the
pod (ADR 0063).

Implied by `github: true` (no veggies.yml key): the one place in a stack
that holds the App credentials. The sidecar mints an installation token
scoped to the stack's own repository with a fixed write set, refreshes it
twenty minutes before expiry, and publishes it as a FILE on a shared
emptyDir the harness mounts read-only. The harness's git credential helper
and `gh` wrapper read that file per invocation, so rotation is invisible to
the agent and no static `GH_TOKEN` env exists anywhere. The App private key
never enters the agent container.

Egress: api.github.com through the pod's squid (already allowlisted). The
daemon and the minter (scripts/github_app_token.py, the repo's one
implementation) ship as stack-config files - the image is python + PyJWT.
"""

from __future__ import annotations

import json

from capabilities import (
    GITHUB_AUTH_DIR,
    HARDENED,
    VAULT_GITHUB,
    BuildSpec,
    Component,
    PodContext,
    SecretSpec,
    ServiceRef,
    StackSpec,
    StatusProbe,
    VaultKey,
    secret_env,
)

# Derived image (python + pinned PyJWT/cryptography); tag = the PyJWT pin.
IMAGE_GITHUB_AUTH = "localhost/veggies-github-auth:2.10.1"
TOKEN_FILE = f"{GITHUB_AUTH_DIR}/token"
IDENTITY_FILE = f"{GITHUB_AUTH_DIR}/identity.gitconfig"
STATUS_FILE = f"{GITHUB_AUTH_DIR}/status.json"
HEARTBEAT = "/tmp/github-auth.heartbeat"

# What a session may do as the App, on its own repository only. Must stay a
# subset of the App's granted permissions (GitHub 422s otherwise; ADR 0063
# lists the grant). Reads cover the kick guards and `gh pr checks`.
POD_TOKEN_PERMISSIONS = {
    "contents": "write",
    "pull_requests": "write",
    "issues": "write",
    "discussions": "write",
    "workflows": "write",
    "actions": "read",
    "checks": "read",
    "statuses": "read",
    "metadata": "read",
}


def _service_ref(spec: StackSpec) -> ServiceRef:
    return ServiceRef(
        capability="github-auth",
        base_url=f"file://{GITHUB_AUTH_DIR}",
        env={"GH_TOKEN_FILE": TOKEN_FILE, "GITHUB_AUTH_DIR": GITHUB_AUTH_DIR},
    )


def _render(ctx: PodContext) -> dict:
    spec = ctx.spec
    egress = ctx.service("egress")
    return {
        "name": "github-auth",
        "image": IMAGE_GITHUB_AUTH,
        # python:<ver>-alpine sets CMD (not ENTRYPOINT) to python3, so args
        # are the whole argv (supervisor/toolbox precedent).
        "args": ["python", "/stack-config/github-auth-daemon.py"],
        "env": [
            secret_env("GITHUB_APP_ID", spec.secret_github, "app_id"),
            secret_env("GITHUB_APP_INSTALLATION_ID", spec.secret_github, "installation_id"),
            secret_env("GITHUB_APP_PRIVATE_KEY", spec.secret_github, "private_key"),
            # owner/name; empty = installation-wide token (no repo known)
            {"name": "GITHUB_REPO", "value": spec.github_repo or ""},
            {"name": "GITHUB_TOKEN_PERMISSIONS",
             "value": json.dumps(POD_TOKEN_PERMISSIONS, sort_keys=True)},
            {"name": "GITHUB_AUTH_DIR", "value": GITHUB_AUTH_DIR},
            {"name": "PYTHONDONTWRITEBYTECODE", "value": "1"},
            {"name": "PYTHONUNBUFFERED", "value": "1"},
        ] + [{"name": k, "value": v} for k, v in egress.env.items()],
        "volumeMounts": [
            {"name": "stack-config", "mountPath": "/stack-config", "readOnly": True},
            {"name": "github-auth", "mountPath": GITHUB_AUTH_DIR},
            # own /tmp: the heartbeat is this component's liveness signal
            # and must not sit on a volume the agent can write.
            {"name": "github-auth-tmp", "mountPath": "/tmp"},
        ],
        "resources": {"limits": {"memory": "64Mi"}},
        "securityContext": HARDENED,
        # The daemon beats at the top of every pass (60s); a wedged pass goes
        # unhealthy in 15 min while the last token file stays valid until
        # its own expiry.
        "livenessProbe": {
            "exec": {"command": [
                "python", "-c",
                "import os,sys,time; sys.exit(0 if time.time()"
                f"-os.path.getmtime('{HEARTBEAT}') < 900 else 1)"]},
            "initialDelaySeconds": 15,
            "periodSeconds": 30,
        },
    }


def _volumes(ctx: PodContext) -> list[dict]:
    # stack-config is squid's declaration (first declaration wins).
    return [{"name": "github-auth", "emptyDir": {}},
            {"name": "github-auth-tmp", "emptyDir": {}}]


def _secrets(spec: StackSpec) -> list[SecretSpec]:
    return [SecretSpec("github", {
        "app_id": VaultKey("github_app_id", VAULT_GITHUB),
        "installation_id": VaultKey("github_app_installation_id", VAULT_GITHUB),
        "private_key": VaultKey("github_app_private_key", VAULT_GITHUB),
    })]


def _config_files(ctx: PodContext) -> dict[str, str]:
    repo = ctx.infra_repo
    return {
        # The pytest-covered minter ships verbatim; the daemon imports it
        # from /stack-config (supervisor precedent).
        "github_app_token.py": (repo / "scripts/github_app_token.py").read_text(),
        "github-auth-daemon.py": (repo / "deploy/github-auth/daemon.py").read_text(),
        # Installed by the harness wrapper at /root/.local/bin/gh.
        "gh-wrapper.sh": (repo / "deploy/github-auth/gh-wrapper.sh").read_text(),
    }


def _probes(spec: StackSpec) -> list[StatusProbe]:
    return [
        StatusProbe(
            "github",
            exec_argv=("python", "-c",
                       "import json,time; s=json.load(open("
                       f"'{STATUS_FILE}')); s['expires_in_s']=int(s.get('expires_at_epoch',0)-time.time()); "
                       "print(json.dumps(s))"),
            extract=lambda j: (f"{j.get('login', '?')} token {j.get('expires_in_s', 0) // 60}m left"
                               if isinstance(j, dict) else "?"),
        ),
    ]


COMPONENT = Component(
    name="github-auth",
    provides="github-auth",
    requires=("egress",),
    render=_render,
    volumes=_volumes,
    service_ref=_service_ref,
    secrets=_secrets,
    config_files=_config_files,
    probes=_probes,
    build=BuildSpec(IMAGE_GITHUB_AUTH, "deploy/images/github-auth.Containerfile"),
)
