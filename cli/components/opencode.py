"""harness capability: opencode serve.

The user-facing component: publishes the only host port, owns the workspace
and home volumes, and describes how `veggies status`/`veggies attach` talk
to it. Status endpoints verified against opencode 1.18.27 (2026-09-04)."""

from __future__ import annotations

import json
from pathlib import Path

from capabilities import (
    HARDENED,
    VAULT_GITHUB,
    Component,
    Generated,
    PodContext,
    SecretSpec,
    BuildSpec,
    ServiceRef,
    StackSpec,
    StatusProbe,
    VaultKey,
    secret_env,
)

# Derived image (official + git, deploy/images/opencode.Containerfile); the
# official one has no git (verified 2026-09-04). Base pinned by tag+digest.
# Instruction-file discovery (AGENTS.md/CLAUDE.md auto-loaded from the mounted
# repo, first match walking up) verified in 1.18.27 session/instruction.ts -
# re-verify that list when bumping this image (ADR 0044).
IMAGE_OPENCODE = "localhost/veggies-opencode:1.18.27"
_OPENCODE_CONTAINER_PORT = 4096


def _service_ref(spec: StackSpec) -> ServiceRef:
    return ServiceRef(
        capability="harness",
        base_url=f"http://127.0.0.1:{_OPENCODE_CONTAINER_PORT}",
        secret=spec.secret_opencode,
        secret_key="password",
    )


def render_opencode_json(infra_repo: Path, router_base_url: str,
                         model: str | None = None,
                         mcp_entries: dict[str, dict] | None = None) -> str:
    """Stack variant of agent-config/opencode.json: router address injected
    by the caller and the master key via env (secretKeyRef)
    instead of an auth.json file. mcp_entries (ADR 0018) come from the
    selected components' mcp_entry() hooks."""
    src = json.loads((infra_repo / "agent-config/opencode.json").read_text())
    provider = src["provider"]["litellm"]
    provider["options"]["baseURL"] = router_base_url
    provider["options"]["apiKey"] = "{env:LITELLM_MASTER_KEY}"
    if model:
        src["model"] = f"litellm/{model}"
    if mcp_entries:
        src["mcp"] = dict(sorted(mcp_entries.items()))
    return json.dumps(src, indent=2) + "\n"


def _render(ctx: PodContext) -> dict:
    spec = ctx.spec
    publish_ip = "0.0.0.0" if spec.host else "127.0.0.1"
    router = ctx.service("model-router")
    egress = ctx.service("egress")
    git_setup = ""
    gh_env: list[dict] = []
    if spec.github:
        # ADR 0030 opt-in: GH_TOKEN (podman secret env) + a credential helper that
        # expands it at use time - never persisted into .git/config. insteadOf lets
        # SSH-style remotes work; identity so commits attribute to the bot.
        git_setup = (
            "git config --global credential.helper "
            "'!f() { echo \"username=x-access-token\"; echo \"password=$GH_TOKEN\"; }; f' && "
            "git config --global \"url.https://github.com/.insteadOf\" \"git@github.com:\" && "
            "git config --global user.name \"veggies-agent\" && "
            "git config --global user.email \"veggies-agent@users.noreply.github.com\" && "
        )
        gh_env = [secret_env("GH_TOKEN", spec.secret_github, "token")]
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
            # ansible-core >=2.21 hard-fails at startup when the configured
            # vault password file is missing (ansible.cfg points at
            # ~/.config/infra/vault-password); a dummy satisfies the check -
            # same trick as infra-ci. Real decryption stays impossible
            # in-pod: the vault password is never shipped here.
            "mkdir -p /root/.config/infra; "
            "[ -f /root/.config/infra/vault-password ] || "
            "printf 'ci-dummy-not-a-real-secret\\n' > /root/.config/infra/vault-password; "
            + git_setup +
            f"exec opencode serve --hostname 0.0.0.0 --port {_OPENCODE_CONTAINER_PORT}"
        ],
        "workingDir": "/workspace",
        "env": [
            secret_env("OPENCODE_SERVER_PASSWORD", spec.secret_opencode, "password"),
            secret_env("LITELLM_MASTER_KEY", router.secret, "master_key"),
            # Superpowers phones home a version ping (opt-out per upstream
            # README); the egress proxy blocks it anyway - belt and braces.
            {"name": "SUPERPOWERS_DISABLE_TELEMETRY", "value": "1"},
        ] + gh_env + [{"name": k, "value": v} for k, v in egress.env.items()],
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
    # github-enabled stacks take the serve password from the vault: the same
    # value is published as the repo's VEGGIES_STACK_PASSWORD Actions secret
    # (ADR 0033), so the GHA trigger never goes stale on re-up. Other stacks
    # keep a per-stack random password.
    password = (VaultKey("veggies_stack_password", VAULT_GITHUB) if spec.github
                else Generated(12))
    out = [SecretSpec("opencode", {"password": password})]
    if spec.github:
        out.append(SecretSpec("github", {"token": VaultKey("github_token", VAULT_GITHUB)}))
    return out


def _config_files(ctx: PodContext) -> dict[str, str]:
    infra_repo = ctx.infra_repo
    mcp_entries = {c.name: e for c in ctx.components
                   if (e := c.mcp_entry(ctx)) is not None}
    files = {"opencode.json": render_opencode_json(
        infra_repo,
        router_base_url=ctx.service("model-router").base_url,
        model=ctx.spec.model,
        mcp_entries=mcp_entries)}
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
        StatusProbe("model", http_path=f"/config{d}",
                    extract=lambda j: str(j.get("model", "?")) if isinstance(j, dict) else "?"),
        StatusProbe("agents", http_path=f"/agent{d}",
                    extract=lambda j: str(len(j)) if isinstance(j, list) else "?"),
        StatusProbe("sessions", http_path=f"/session{d}",
                    extract=lambda j: str(len(j)) if isinstance(j, list) else "?"),
        StatusProbe("activity", http_path=f"/session/status{d}",
                    extract=lambda j: "busy" if j else "idle"),
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
    service_ref=_service_ref,
    secrets=_secrets,
    config_files=_config_files,
    probes=_probes,
    attach=_attach,
    build=BuildSpec(IMAGE_OPENCODE, "deploy/images/opencode.Containerfile"),
)
