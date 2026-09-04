#!/usr/bin/env python3
"""veggies - repo-scoped, persistent agent stacks (ADR 0013).

One stack = one pod (opencode + litellm + squid) that can see exactly one
repository. Stacks run under rootless podman via `podman kube play`, locally
or on a remote host over ssh. Only the opencode port is ever published;
litellm and squid are pod-internal.

Stdlib + PyYAML only. All functions that render/derive are pure and tested
in tests/test_veggies.py; anything that touches podman, ssh, or the vault is
isolated in cmd_* handlers.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import re
import secrets as secrets_mod
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import yaml

# The stack definition (components, renderers, spec) lives in veggies_stack
# (ADR 0016); names are re-exported here so tests and the shim keep one
# import surface.
sys.path.insert(0, str(Path(__file__).resolve().parent))
from veggies_stack import (  # noqa: E402
    Generated,
    IMAGE_LITELLM,
    IMAGE_OPENCODE,
    IMAGE_SQUID,
    REMOTE_STATE_ROOT,
    REMOTE_USER,
    SQUID_ALLOWLIST_BASE,
    SQUID_MODEL_ENDPOINTS,
    StackSpec,
    VaultKey,
    allocate_port,
    container_names,
    load_repo_config,
    parse_repo_config,
    render_allowlist,
    render_opencode_json,
    build_context,
    resolve_components,
    render_squid_conf,
    render_yaml,
    render_secret_docs,
    required_secret_values,
    sanitize_name,
    secret_names,
    state_dir,
)

VAULT_MODEL = "secrets/model.yml"
VAULT_GITHUB = "secrets/github.yml"
VAULT_PASSWORD_FILE = "~/.config/infra/vault-password"

class State:
    """~/.local/state/veggies/state.json (0600, atomic writes)."""

    def __init__(self, root: Path | None = None):
        self.root = root or state_dir()
        self.file = self.root / "state.json"

    def load(self) -> dict:
        if not self.file.exists():
            return {"stacks": {}}
        return json.loads(self.file.read_text())

    def save(self, data: dict) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=self.root, prefix=".state-", suffix=".tmp")
        with os.fdopen(fd, "w") as f:
            json.dump(data, f, indent=2, sort_keys=True)
            f.write("\n")
        os.chmod(tmp, 0o600)
        os.replace(tmp, self.file)

    def add(self, spec: StackSpec, password: str = "") -> None:
        data = self.load()
        data["stacks"][spec.name] = {
            "repo": spec.repo,
            "mode": spec.mode,
            "port": spec.port,
            "host": spec.host,
            "created": spec.created,
            # opencode serve basic-auth password. Kept here (0600) rather than
            # re-derived from podman secrets - same protection level locally.
            "password": password,
        }
        self.save(data)

    def remove(self, name: str) -> bool:
        data = self.load()
        if data["stacks"].pop(name, None) is None:
            return False
        self.save(data)
        return True

    def get(self, name: str) -> dict | None:
        return self.load()["stacks"].get(name)

    def used_ports(self) -> set[int]:
        return {s["port"] for s in self.load()["stacks"].values()}


# --- Vault access (isolated; phase 11 uses this from `up`) ---------------------


def vault_key(key: str, vault_file: str = VAULT_MODEL) -> str:
    script = Path(__file__).parent.parent / "scripts/vault_get.py"
    return subprocess.run(
        [sys.executable, str(script), vault_file, key,
         "--password-file", VAULT_PASSWORD_FILE],
        check=True,
        capture_output=True,
        text=True,
        cwd=Path(__file__).parent.parent,
    ).stdout.strip()


# --- Runtime (podman, images, health) -------------------------------------------


def run(cmd: list[str], *, input_text: str | None = None, check: bool = True,
        capture: bool = False) -> subprocess.CompletedProcess:
    return subprocess.run(
        cmd, input=input_text, text=True, check=check,
        capture_output=capture,
    )


def host_run(host: str | None, args: list[str], **kwargs) -> subprocess.CompletedProcess:
    """Run a command on the stack's host. Remote = ssh + passwordless sudo
    into the stacks user (ADR 0014); stdin (kube YAML, secrets) pipes through."""
    if host is None:
        return run(args, **kwargs)
    return run(["ssh", host, "sudo", "-n", "-iu", REMOTE_USER, *args], **kwargs)


def host_podman(host: str | None, *args: str, **kwargs) -> subprocess.CompletedProcess:
    return host_run(host, ["podman", *args], **kwargs)


def host_systemctl(host: str | None, *args: str, **kwargs) -> subprocess.CompletedProcess:
    if host is None:
        return run(["systemctl", "--user", *args], **kwargs)
    return host_run(host, ["systemctl", "--user", *args], **kwargs)


def host_write(host: str | None, path: str, content: str, mode: int = 0o600) -> None:
    if host is None:
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content)
        os.chmod(p, mode)
        return
    import shlex
    q = shlex.quote(path)
    host_run(host, ["sh", "-c",
                    f"umask 077 && mkdir -p $(dirname {q}) && cat > {q}"],
             input_text=content)


def quadlet_dir(host: str | None) -> str:
    if host is None:
        return str(Path(os.environ.get(
            "VEGGIES_QUADLET_DIR", "~/.config/containers/systemd")).expanduser())
    return f"/home/{REMOTE_USER}/.config/containers/systemd"


def host_exists(host: str | None, path: str, kind: str = "f") -> bool:
    result = host_run(host, ["test", f"-{kind}", path], check=False,
                      capture=True)
    return result.returncode == 0


def ensure_images(host: str | None, infra_repo: Path) -> None:
    """Squid and the opencode derivative are built (layer-cache makes this a
    no-op when unchanged); litellm is pulled once. Remote: the Containerfiles
    are shipped into the remote state dir and built there (ADR 0014)."""
    squid_cf = (infra_repo / "ansible/roles/egress/files/squid.Containerfile").read_text()
    opencode_cf = (infra_repo / "deploy/images/opencode.Containerfile").read_text()
    if host is None:
        (infra_repo / "deploy/images").mkdir(parents=True, exist_ok=True)
        host_build_dir = None
    images_dir = f"{REMOTE_STATE_ROOT}/images" if host else None
    for image, containerfile in ((IMAGE_SQUID, squid_cf), (IMAGE_OPENCODE, opencode_cf)):
        if host is None:
            cf_path = str(state_dir() / "images" / f"{image.split('/')[-1].split(':')[0]}.Containerfile")
            host_write(None, cf_path, containerfile)
            host_podman(None, "build", "-q", "-t", image, "-f", cf_path,
                        str(state_dir() / "images"))
        else:
            cf_path = f"{images_dir}/{image.split('/')[-1].split(':')[0]}.Containerfile"
            host_write(host, cf_path, containerfile)
            host_podman(host, "build", "-q", "-t", image, "-f", cf_path, images_dir)
    if host_podman(host, "image", "exists", IMAGE_LITELLM,
                   check=False, capture=True).returncode != 0:
        host_podman(host, "pull", "-q", IMAGE_LITELLM)


def wait_healthy(spec: StackSpec, timeout: int = 240) -> None:
    """kube play maps livenessProbe to a podman healthcheck; wait on those."""
    deadline = time.monotonic() + timeout
    pending = set(container_names(spec))
    while time.monotonic() < deadline:
        for name in sorted(pending):
            status = host_podman(
                spec.host,
                "inspect", "--format",
                "{{.State.Status}}:{{if .State.Health}}{{.State.Health.Status}}{{end}}",
                name, capture=True,
            ).stdout.strip()
            if status.startswith("exited") or status.startswith("dead"):
                logs = host_podman(spec.host, "logs", "--tail", "20", name,
                                   capture=True, check=False)
                raise RuntimeError(
                    f"container {name} died ({status}):\n{logs.stdout}{logs.stderr}"
                )
            if status.endswith("healthy"):
                pending.discard(name)
        if not pending:
            return
        time.sleep(3)
    raise RuntimeError(f"timed out waiting for healthy: {sorted(pending)}")


def label_for_containers(host: str | None, path: str) -> None:
    """Shared SELinux label for hostPath sources. podman kube play does NOT
    reliably relabel hostPath volumes (observed: user_tmp_t/gconf_home_t left
    as-is; once even private :Z MCS categories that then blocked other pods).
    chcon -R is the explicit equivalent of a shared :z. The `-l s0` matters:
    a previous kube play left *private* MCS categories (cX,cY) on these files,
    which then blocked every other pod. Harmless for the owning user
    (unconfined_t can still read/write)."""
    host_run(host, ["chcon", "-R", "-t", "container_file_t", "-l", "s0", path])


def write_stack_config(spec: StackSpec, infra_repo: Path) -> None:
    ctx = build_context(spec, infra_repo)
    files: dict[str, str] = {}
    for c in ctx.components:
        files |= c.config_files(ctx)
    for filename, content in files.items():
        host_write(spec.host, f"{spec.config_dir()}/{filename}", content)
    label_for_containers(spec.host, f"{spec.state_root()}/{spec.name}")


def safe_rmtree(host: str | None, root: str, path: str) -> None:
    """Refuse to delete anything outside the veggies state dir (a mounted repo
    must never be touched by `down --purge`)."""
    if not (path == root or path.startswith(root.rstrip("/") + "/")):
        raise ValueError(f"refusing to remove {path} - outside {root}")
    if host is None:
        shutil.rmtree(path, ignore_errors=True)
    else:
        host_run(host, ["rm", "-rf", path])


def quadlet_path(spec: StackSpec) -> str:
    return f"{quadlet_dir(spec.host)}/{spec.pod}.kube"


def render_quadlet(spec: StackSpec, pod_yaml: str) -> str:
    """Boot/crash persistence: systemd plays the pod-only YAML (secrets live
    in the podman store, created once at up time and referenced by name)."""
    return f"""# Generated by veggies (ADR 0013). Removed by `veggies down`.
[Unit]
Description=veggies stack {spec.name} (repo-scoped agent pod)

[Kube]
Yaml={pod_yaml}

[Install]
WantedBy=default.target
"""


def linger_enabled() -> bool:
    out = subprocess.run(
        ["loginctl", "show-user", os.environ.get("USER", ""), "-p", "Linger",
         "--value"],
        capture_output=True, text=True, check=False,
    )
    return out.stdout.strip() == "yes"


WATCHDOG_SERVICE = """# Generated by veggies (ADR 0013). Restarts crashed stack containers.
# Under a systemd unit, podman delegates container restart policy to systemd
# (event log: died -> cleanup, no restart), so a killed stack container would
# otherwise stay dead until the next boot. Verified 2026-09-04.
[Unit]
Description=veggies watchdog: restart exited stack containers

[Service]
Type=oneshot
ExecStart=/bin/sh -c "podman ps -aq --filter label=app=veggies --filter status=exited | xargs -r podman container restart"
"""

WATCHDOG_TIMER = """# Generated by veggies (ADR 0013).
[Unit]
Description=veggies watchdog timer

[Timer]
OnBootSec=30s
OnUnitActiveSec=30s

[Install]
WantedBy=timers.target
"""


def ensure_watchdog(host: str | None) -> None:
    """Shared per-user watchdog (idempotent)."""
    if host is None:
        unit_dir = str(Path("~/.config/systemd/user").expanduser())
    else:
        unit_dir = f"/home/{REMOTE_USER}/.config/systemd/user"
    for name, text in (("veggies-watchdog.service", WATCHDOG_SERVICE),
                       ("veggies-watchdog.timer", WATCHDOG_TIMER)):
        host_write(host, f"{unit_dir}/{name}", text, mode=0o644)
    host_systemctl(host, "daemon-reload")
    host_systemctl(host, "enable", "--now", "-q", "veggies-watchdog.timer")


# --- Commands ------------------------------------------------------------------


def stack_url(record: dict) -> str:
    """Attach URL: loopback locally, the tailnet name for remote stacks."""
    host = record["host"] or "127.0.0.1"
    return f"http://{host}:{record['port']}"


def discover_repo_config(host: str | None, repo_path: str) -> tuple[dict, list[str]]:
    """veggies.yml from the target repo (ADR 0016): local path, or the
    remote clone over ssh. Missing file = empty config, never an error."""
    if host is None:
        return load_repo_config(Path(repo_path))
    r = host_run(host, ["cat", f"{repo_path}/veggies.yml"], check=False, capture=True)
    if r.returncode != 0:
        return {}, []
    return parse_repo_config(r.stdout)


def cmd_render(args: argparse.Namespace) -> int:
    infra_repo = Path(__file__).parent.parent.resolve()
    name = args.name or sanitize_name(args.repo)
    if args.clone and args.host:
        repo = f"{REMOTE_STATE_ROOT}/clones/{name}"  # matches cmd_up
    elif args.clone:
        repo = str(state_dir() / "clones" / name)
    else:
        repo = str(Path(args.repo).expanduser().resolve())
    cfg, warnings = discover_repo_config(args.host, repo)
    for w in warnings:
        print(f"warning: {w}", file=sys.stderr)
    spec = StackSpec(
        name=name,
        repo=repo,
        mode="clone" if args.clone else "mount",
        port=args.port or allocate_port(State().used_ports()),
        host=args.host,
        model=args.model or cfg.get("model"),
        components=cfg.get("components"),
    )
    sys.stdout.write(render_yaml(spec, infra_repo))
    return 0


def cmd_up(args: argparse.Namespace) -> int:
    infra_repo = Path(__file__).parent.parent.resolve()
    state = State()
    host = args.host

    name = args.name or sanitize_name(args.repo)
    if host and not args.clone:
        raise ValueError(
            "remote stacks are clone-mode: pass --clone with a git URL "
            "(a local bind-mount is meaningless on another host)"
        )
    if args.clone:
        if host:
            clone_dir = f"{REMOTE_STATE_ROOT}/clones/{name}"
            if not host_exists(host, clone_dir, kind="d"):
                clone_cmd = ["git", "clone"]
                if args.repo.startswith("https://github.com/"):
                    # Token rides the process list on the VPS briefly - see
                    # the threat model (ADR 0014). ssh URLs need no token.
                    token = vault_key("github_token", VAULT_GITHUB)
                    clone_cmd += ["-c",
                                  f"http.extraHeader=Authorization: Bearer {token}"]
                clone_cmd += [args.repo, clone_dir]
                host_run(host, clone_cmd)
        else:
            clone_path = state.root / "clones" / name
            if not clone_path.exists():
                clone_path.parent.mkdir(parents=True, exist_ok=True)
                run(["git", "clone", args.repo, str(clone_path)])
            clone_dir = str(clone_path)
        repo_path = clone_dir
        mode = "clone"
    else:
        repo_path = str(Path(args.repo).expanduser().resolve())
        if not Path(repo_path).is_dir():
            raise ValueError(f"repo path does not exist: {repo_path}")
        mode = "mount"

    cfg, warnings = discover_repo_config(host, repo_path)
    for w in warnings:
        print(f"warning: {w}", file=sys.stderr)
    existing = state.get(name)
    port = existing["port"] if existing else allocate_port(state.used_ports())
    spec = StackSpec(name=name, repo=repo_path, mode=mode, port=port, host=host,
                     model=args.model or cfg.get("model"),
                     components=cfg.get("components"),
                     created=existing["created"] if existing else
                     datetime.now(timezone.utc).isoformat(timespec="seconds"))
    if spec.model:
        print(f"model:   litellm/{spec.model} (veggies.yml)")

    url = f"http://{host or '127.0.0.1'}:{port}"
    if sys.stdin.isatty() and not args.yes:
        print(f"stack:   {spec.pod} on {host or 'this machine'}")
        print(f"repo:    {spec.repo} ({mode})")
        print(f"attach:  {url}")
        if input("Bring it up? [Y/n] ").strip().lower() not in ("", "y", "yes"):
            print("aborted")
            return 1

    print("==> images")
    ensure_images(host, infra_repo)

    print("==> stack config")
    write_stack_config(spec, infra_repo)
    if host is None:
        # Everything a container bind-mounts must carry container_file_t.
        label_for_containers(None, str(infra_repo / "agent-config" / "litellm"))
        label_for_containers(None, repo_path)

    values = {}
    for key, source in required_secret_values(spec).items():
        values[key] = (secrets_mod.token_hex(source.nbytes)
                       if isinstance(source, Generated)
                       else vault_key(source.key))

    # Idempotent refresh: drop the old pod and secrets before replaying.
    host_podman(host, "pod", "rm", "-f", spec.pod, check=False, capture=True)
    host_podman(host, "secret", "rm", *secret_names(spec),
                check=False, capture=True)

    print("==> secrets (stdin only, then they live in the podman store)")
    host_podman(host, "kube", "play", "-",
                input_text=yaml.safe_dump_all(render_secret_docs(spec, values),
                                              sort_keys=True))

    # Pod-only YAML on disk for the quadlet; never contains secrets.
    host_write(host, spec.pod_yaml_path(), render_yaml(spec, infra_repo))

    if args.no_install:
        print("==> kube play (no boot persistence)")
        host_podman(host, "kube", "play", "--replace", spec.pod_yaml_path())
    else:
        host_write(host, quadlet_path(spec),
                   render_quadlet(spec, spec.pod_yaml_path()), mode=0o644)
        host_systemctl(host, "daemon-reload")
        print(f"==> systemd start ({spec.pod}.service)")
        host_systemctl(host, "restart", f"{spec.pod}.service")
        ensure_watchdog(host)
        if host is None and not linger_enabled():
            print("!! linger is off: stacks start at login, not at boot.")
            print(f"   enable once with: sudo loginctl enable-linger {os.environ.get('USER')}")

    print("==> waiting for healthy")
    wait_healthy(spec)
    state.add(spec, password=values["password"])

    print(f"\nstack up: {url}  (user: opencode, password: {values['password']})")
    if host:
        print("remote attach needs your tailnet up (ADR 0003/0014)")
    if not args.no_attach and sys.stdin.isatty() and shutil.which("opencode"):
        os.execvp("opencode", ["opencode", "attach", url,
                               "--username", "opencode",
                               "--password", values["password"]])
    return 0


def cmd_down(args: argparse.Namespace) -> int:
    state = State()
    record = state.get(args.name)
    if record is None:
        raise ValueError(f"unknown stack {args.name!r} (veggies ls)")
    spec = StackSpec(name=args.name, repo=record["repo"], mode=record["mode"],
                     port=record["port"], host=record["host"])
    host = spec.host
    if host_exists(host, quadlet_path(spec)):
        host_run(host, ["rm", "-f", quadlet_path(spec)])
        host_systemctl(host, "daemon-reload", check=False)
    host_podman(host, "pod", "stop", "-t", "5", spec.pod, check=False, capture=True)
    host_podman(host, "pod", "rm", "-f", spec.pod, check=False, capture=True)
    if args.purge:
        host_podman(host, "volume", "rm", "-f", spec.volume_opencode,
                    check=False, capture=True)
        host_podman(host, "secret", "rm", spec.secret_litellm, spec.secret_opencode,
                    check=False, capture=True)
        safe_rmtree(host, spec.state_root(), f"{spec.state_root()}/{args.name}")
        if record["mode"] == "clone":
            safe_rmtree(host, spec.state_root(),
                        f"{spec.state_root()}/clones/{args.name}")
        state.remove(args.name)
        print(f"purged {args.name}")
    else:
        print(f"stopped {args.name} (volumes, secrets and state kept; "
              f"`veggies down {args.name} --purge` deletes everything)")
    return 0


def cmd_attach(args: argparse.Namespace) -> int:
    record = State().get(args.name)
    if record is None:
        raise ValueError(f"unknown stack {args.name!r} (veggies ls)")
    url = stack_url(record)
    if not shutil.which("opencode"):
        print(f"opencode CLI not found; attach manually: {url} "
              f"(user: opencode, password: {record['password']})")
        return 1
    os.execvp("opencode", ["opencode", "attach", url,
                           "--username", "opencode",
                           "--password", record["password"]])
    return 0  # unreachable


def cmd_logs(args: argparse.Namespace) -> int:
    record = State().get(args.name)
    if record is None:
        raise ValueError(f"unknown stack {args.name!r} (veggies ls)")
    if args.container:
        cmd = ["podman", "logs"] + (["-f"] if args.follow else []) + \
              [f"veggies-{args.name}-{args.container}"]
    else:
        cmd = ["podman", "pod", "logs"] + (["-f"] if args.follow else []) + \
              [f"veggies-{args.name}"]
    if record["host"]:
        os.execvp("ssh", ["ssh", record["host"], "sudo", "-n", "-iu",
                          REMOTE_USER, *cmd])
    os.execvp("podman", cmd)
    return 0  # unreachable


def cmd_ls(args: argparse.Namespace) -> int:
    stacks = State().load()["stacks"]
    if not stacks:
        print("no stacks - run `veggies up` inside a repo")
        return 0
    live: dict[str, str] = {}
    hosts = {None} | {s["host"] for s in stacks.values() if s["host"]}
    for host in hosts:
        result = host_podman(host, "pod", "ps", "--format", "json",
                             "--filter", "name=^veggies-",
                             capture=True, check=False)
        if result.returncode == 0 and result.stdout.strip():
            for pod in json.loads(result.stdout):
                live[pod["Name"]] = pod["Status"]
    print(f"{'NAME':<20} {'STATUS':<12} {'PERSIST':<8} {'HOST':<7} {'PORT':<6} REPO")
    for name, s in sorted(stacks.items()):
        status = live.get(f"veggies-{name}", "down")
        spec = StackSpec(name=name, repo=s["repo"], host=s["host"])
        persist = "quadlet" if host_exists(spec.host, quadlet_path(spec)) else "-"
        print(f"{name:<20} {status:<12} {persist:<8} {s['host'] or 'local':<7} "
              f"{s['port']:<6} {s['repo']} ({s['mode']})")
    return 0


API_TIMEOUT = 90  # cold bootstrap (~20s: plugin cache warm-up) must fit


def probe_api(host: str | None, port: int, password: str, path: str) -> object | None:
    """GET an opencode API path with the stack's basic auth. Returns parsed
    JSON, or None on any failure (stack down, bootstrap in progress, etc.).
    Remote: curl on the target host over ssh (the port binds 127.0.0.1 there).
    Endpoints verified against opencode 1.18.27 (2026-09-04)."""
    url = f"http://127.0.0.1:{port}{path}"
    if host:
        r = run(["ssh", host, "curl", "-s", "-m", str(API_TIMEOUT),
                 "-u", f"opencode:{password}", url], check=False, capture=True)
        if r.returncode != 0 or not r.stdout.strip():
            return None
        try:
            return json.loads(r.stdout)
        except json.JSONDecodeError:
            return None
    import urllib.request
    req = urllib.request.Request(url)
    req.add_header("Authorization", "Basic " + _basic_auth(password))
    try:
        with urllib.request.urlopen(req, timeout=API_TIMEOUT) as resp:
            return json.loads(resp.read())
    except Exception:  # connection refused, timeout, 401/5xx, bad json
        return None


def _basic_auth(password: str) -> str:
    import base64 as b64mod
    return b64mod.b64encode(f"opencode:{password}".encode()).decode()


def format_status(name: str, record: dict, containers: list[tuple[str, str, str]],
                  api: dict[str, object] | None) -> str:
    """Pure formatter for `veggies status` (tested without IO)."""
    lines = [f"stack: {name} ({record['mode']}, port {record['port']}, "
             f"host {record['host'] or 'local'})",
             f"repo:  {record['repo']}"]
    lines.append("containers:")
    for cname, status, health in containers:
        lines.append(f"  {cname:<28} {status:<10} {health}")
    if api is None:
        lines.append("api:     unreachable (down, or cold bootstrap in progress - retry)")
    else:
        sessions = api.get("sessions")
        agents = api.get("agents")
        busy = api.get("busy")
        lines.append(f"api:     ok  (model {api.get('model')}, "
                     f"{len(agents) if isinstance(agents, list) else '?'} agents, "
                     f"{len(sessions) if isinstance(sessions, list) else '?'} sessions"
                     f"{', BUSY' if busy else ''})")
    return "\n".join(lines)


def cmd_status(args: argparse.Namespace) -> int:
    record = State().get(args.name)
    if record is None:
        raise ValueError(f"unknown stack: {args.name} (see `veggies ls`)")
    spec = StackSpec(name=args.name, repo=record["repo"], host=record["host"])
    names = container_names(spec)
    containers: list[tuple[str, str, str]] = []
    r = host_podman(record["host"], "inspect", "--format",
                    "{{.Name}} {{.State.Status}} "
                    "{{if .State.Health}}{{.State.Health.Status}}{{else}}-{{end}}",
                    *names, capture=True, check=False)
    if r.returncode == 0:
        for line in r.stdout.splitlines():
            parts = line.split()
            if len(parts) == 3:
                containers.append((parts[0], parts[1], parts[2]))
    if not containers:
        containers = [(n, "down", "-") for n in names]

    api: dict[str, object] | None = None
    password = record.get("password", "")
    if password and any(c[1] == "running" for c in containers):
        cfg = probe_api(record["host"], record["port"], password,
                        "/config?directory=/workspace")
        if cfg is not None:
            sessions = probe_api(record["host"], record["port"], password,
                                 "/session?directory=/workspace")
            agents = probe_api(record["host"], record["port"], password,
                               "/agent?directory=/workspace")
            sess_status = probe_api(record["host"], record["port"], password,
                                    "/session/status?directory=/workspace")
            busy = bool(sess_status) if isinstance(sess_status, dict) else False
            api = {"model": cfg.get("model", "?") if isinstance(cfg, dict) else "?",
                   "sessions": sessions, "agents": agents, "busy": busy}
    print(format_status(args.name, record, containers, api))
    return 0


LEGACY_STATE_DIR = Path("~/.local/state/garden")


def legacy_hint() -> None:
    """One-time rename notice (ADR 0015). Pure detection, no auto-migration:
    podman secrets/volumes cannot be renamed, only recreated."""
    if LEGACY_STATE_DIR.expanduser().exists() and not state_dir().exists():
        print(
            "veggies: found legacy 'garden' state (project renamed, ADR 0015).\n"
            "  migrate: purge old stacks with the old CLI, then remove the rest:\n"
            "    garden down <name> --purge   # per stack, while it still exists\n"
            "    rm -rf ~/.local/state/garden ~/.local/bin/garden \\\n"
            "      ~/.config/containers/systemd/garden-*.kube \\\n"
            "      ~/.config/systemd/user/garden-watchdog.*\n"
            "  then re-run this command.",
            file=sys.stderr,
        )


def main(argv: list[str] | None = None) -> int:
    legacy_hint()
    parser = argparse.ArgumentParser(prog="veggies", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    p_render = sub.add_parser("render", help="print the kube YAML for a stack")
    p_render.add_argument("--repo", default=os.environ.get("VEGGIES_REPO", "."))
    p_render.add_argument("--name", default=os.environ.get("VEGGIES_NAME"))
    p_render.add_argument("--port", type=int, default=None)
    p_render.add_argument("--host", default=os.environ.get("VEGGIES_HOST"))
    p_render.add_argument("--model", default=None,
                      help="litellm model alias (overrides veggies.yml)")
    p_render.add_argument("--clone", action="store_true",
                          help="repo is a URL; clone instead of mounting")
    p_render.set_defaults(func=cmd_render)

    p_ls = sub.add_parser("ls", help="list stacks")
    p_ls.set_defaults(func=cmd_ls)

    p_status = sub.add_parser("status", help="stack health + agent/session view (ADR 0016)")
    p_status.add_argument("name")
    p_status.set_defaults(func=cmd_status)

    p_up = sub.add_parser("up", help="bring a stack up (default repo: cwd)")
    p_up.add_argument("--repo", default=os.environ.get("VEGGIES_REPO", "."))
    p_up.add_argument("--name", default=os.environ.get("VEGGIES_NAME"))
    p_up.add_argument("--host", default=os.environ.get("VEGGIES_HOST"))
    p_up.add_argument("--model", default=None,
                      help="litellm model alias (overrides veggies.yml)")
    p_up.add_argument("--clone", action="store_true",
                      default=os.environ.get("VEGGIES_CLONE") == "1",
                      help="repo is a URL; clone into the state dir")
    p_up.add_argument("--no-attach", action="store_true",
                      default=os.environ.get("VEGGIES_NO_ATTACH") == "1")
    p_up.add_argument("--no-install", action="store_true",
                      default=os.environ.get("VEGGIES_NO_INSTALL") == "1",
                      help="no systemd quadlet: dies at reboot")
    p_up.add_argument("-y", "--yes", action="store_true",
                      default=os.environ.get("VEGGIES_YES") == "1")
    p_up.set_defaults(func=cmd_up)

    p_down = sub.add_parser("down", help="stop a stack (keeps volumes/state)")
    p_down.add_argument("name")
    p_down.add_argument("--purge", action="store_true",
                        help="also delete volumes, secrets, config and state")
    p_down.set_defaults(func=cmd_down)

    p_attach = sub.add_parser("attach", help="attach the opencode TUI to a stack")
    p_attach.add_argument("name")
    p_attach.set_defaults(func=cmd_attach)

    p_logs = sub.add_parser("logs", help="pod logs (or one container)")
    p_logs.add_argument("name")
    p_logs.add_argument("container", nargs="?",
                        choices=["opencode", "litellm", "squid"])
    p_logs.add_argument("-f", "--follow", action="store_true")
    p_logs.set_defaults(func=cmd_logs)

    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except (ValueError, FileNotFoundError, subprocess.CalledProcessError) as exc:
        print(f"veggies: error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
