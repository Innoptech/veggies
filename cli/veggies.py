#!/usr/bin/env python3
"""veggies - repo-scoped, persistent agent stacks.

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
import shlex
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

# The stack definition (components, renderers, spec) lives in veggies_stack;
# names are re-exported here so tests and the shim keep one
# import surface.
sys.path.insert(0, str(Path(__file__).resolve().parent))
from veggies_stack import (  # noqa: E402
    Generated,
    REMOTE_PROXY,
    REMOTE_STATE_ROOT,
    REMOTE_USER,
    Component,
    StackSpec,
    allocate_port,
    build_context,
    container_names,
    load_repo_config,
    parse_repo_config,
    render_secret_docs,
    render_yaml,
    required_secret_values,
    stack_components,
    sanitize_name,
    secret_names,
    state_dir,
)
from capabilities import VAULT_GITHUB, VAULT_MODEL  # noqa: E402
import costs  # noqa: E402

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
            "components": spec.components,
            "selections": spec.selections,
            "mcps": list(spec.mcps),
            "github": spec.github,
            "model": spec.model,
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


# --- Vault access (isolated; used by `up` and `secrets`) ---------------------


_SECRET_STRINGS: set[str] = set()  # every vault value, for error redaction


def redact(text: str) -> str:
    """Scrub known secrets from text we may print (exceptions carry argv)."""
    for secret in _SECRET_STRINGS:
        if secret:
            text = text.replace(secret, "***")
    return text


def vault_key(key: str, vault_file: str = VAULT_MODEL) -> str:
    script = Path(__file__).parent.parent / "scripts/vault_get.py"
    try:
        value = subprocess.run(
            [sys.executable, str(script), vault_file, key,
             "--password-file", VAULT_PASSWORD_FILE],
            check=True,
            capture_output=True,
            text=True,
            cwd=Path(__file__).parent.parent,
        ).stdout.strip()
    except subprocess.CalledProcessError as exc:
        # The actionable message is on vault_get's stderr (missing/wrong
        # password file, missing key, ...); str(exc) alone is just
        # "returned non-zero exit status 1".
        detail = " | ".join((exc.stderr or "").strip().splitlines())
        raise ValueError(redact(
            f"vault lookup failed for {key!r} in {vault_file}: "
            + (detail or f"exit {exc.returncode}"))) from None
    if value:
        _SECRET_STRINGS.add(value)
    return value


def resolve_secret_values(
    spec: StackSpec, components: list[Component] | None = None
) -> dict[str, str]:
    """Flat key -> resolved value map for every declared secret: Generated
    values are random, VaultKey values are read from the key's own vault
    file (VaultKey.vault)."""
    values = {}
    for key, source in required_secret_values(spec, components).items():
        values[key] = (secrets_mod.token_hex(source.nbytes)
                       if isinstance(source, Generated)
                       else vault_key(source.key, source.vault))
    return values


# --- Runtime (podman, images, health) -------------------------------------------


def run(cmd: list[str], *, input_text: str | None = None, check: bool = True,
        capture: bool = False, cwd: str | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(
        cmd, input=input_text, text=True, check=check,
        capture_output=capture, cwd=cwd,
    )


def github_https_url(repo_url: str) -> str:
    """Pure: git@github.com: -> https://github.com/. SSH from the VPS is dead
    by design (squid CONNECT allowlist is 443-only, no keys for the stacks
    user); the HTTPS form rides the substrate proxy and, in github-enabled
    pods, the GH_TOKEN credential helper."""
    if repo_url.startswith("git@github.com:"):
        return "https://github.com/" + repo_url[len("git@github.com:"):]
    return repo_url


def remote_repo_needs_token(host: str, repo_url: str) -> bool:
    """Probe the repo anonymously (through the substrate proxy) to decide
    whether the vault token must ride along. Public repos must NOT get the
    token - an org-blocked or limited-scope token fails even public clones
    (verified 2026-09-08)."""
    return host_run(host, ["git", "-c", f"http.proxy={REMOTE_PROXY}",
                           "ls-remote", repo_url, "HEAD"],
                    check=False).returncode != 0


def remote_git_prefix(host: str, repo_url: str) -> list[str]:
    """git argv prefix for network ops on the VPS: the substrate proxy (the
    stacks user is direct-egress-denied) plus, for private github.com repos,
    the vault token via extraHeader. The -c pairs lead the subcommand on
    purpose: a TRAILING -c on clone is git-clone's own --config and would
    PERSIST the header into the new repo's .git/config (pod-readable at
    /workspace; verified 2026-09-10, git 2.54), while a leading -c is
    command-scoped and never written out. The token still rides the VPS
    process list briefly - see docs/threat-model.md."""
    cmd = ["git", "-c", f"http.proxy={REMOTE_PROXY}"]
    if repo_url.startswith("https://github.com/") and \
            remote_repo_needs_token(host, repo_url):
        token = vault_key("github_token", VAULT_GITHUB)
        cmd += ["-c", f"http.extraHeader=Authorization: Bearer {token}"]
    return cmd


def remote_clone_cmd(host: str, repo_url: str, clone_dir: str) -> list[str]:
    """git-clone command for the VPS (auth/proxy: remote_git_prefix)."""
    repo_url = github_https_url(repo_url)
    return remote_git_prefix(host, repo_url) + ["clone", repo_url, clone_dir]


def clone_pull_argv(clone_dir: str, *, proxy: str | None = None,
                    token: str | None = None) -> list[str]:
    """Pure: fast-forward the shared clone checkout. --ff-only is a tripwire,
    not a limitation: sessions are forbidden to touch /workspace (ADR 0037),
    so a non-ff pull means something dirtied the shared checkout - fail loud,
    never reset --hard over the evidence."""
    cmd = ["git", "-C", clone_dir]
    if proxy:
        cmd += ["-c", f"http.proxy={proxy}"]
    if token:
        cmd += ["-c", f"http.extraHeader=Authorization: Bearer {token}"]
    return cmd + ["pull", "--ff-only"]


_REMOTE_UID: dict[str, str] = {}


def _remote_uid(host: str) -> str:
    if host not in _REMOTE_UID:
        _REMOTE_UID[host] = run(
            ["ssh", host, "sudo", "-n", "-u", REMOTE_USER, "id", "-u"],
            capture=True).stdout.strip()
    return _REMOTE_UID[host]


def remote_sh(host: str, args: list[str]) -> str:
    """The one-string remote command: ssh re-joins argv with spaces for the
    remote login shell, so everything is shlex.quoted - quoted payloads
    (sh -c 'a && b') survive exactly (verified 2026-09-08: unquoted, the &&
    chain ran as fedora and died on /home/stacks traversal). env sets
    HOME/XDG so nologin service users work (no `sudo -i`: it re-parses
    through the login shell, and stacks' shell is nologin - verified
    2026-09-10 when `logs` broke with "account is currently not
    available"). cd / first: podman chdirs to $cwd."""
    remote = " ".join(shlex.quote(a) for a in
                      ["sudo", "-n", "-u", REMOTE_USER,
                       "env", f"HOME=/home/{REMOTE_USER}",
                       f"XDG_RUNTIME_DIR=/run/user/{_remote_uid(host)}",
                       *args])
    return "cd / && " + remote


def host_run(host: str | None, args: list[str], **kwargs) -> subprocess.CompletedProcess:
    """Run a command on the stack's host. Remote = ssh + passwordless sudo
    to the stacks user; stdin (kube YAML, secrets) pipes through."""
    if host is None:
        return run(args, **kwargs)
    return run(["ssh", host, remote_sh(host, args)], **kwargs)


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


def ensure_images(host: str | None, infra_repo: Path, spec: StackSpec,
                  verbose: bool = False) -> None:
    """Images are component-owned: build/pull exactly the selected
    components' images. Built images use layer-cache (no-op when unchanged);
    pull-only images are pulled once. Remote: Containerfiles are shipped into
    the remote state dir and built there. verbose streams the full build
    output (`veggies prepare`); `up` stays quiet (-q)."""
    quiet = [] if verbose else ["-q"]
    for c in stack_components(spec):
        b = c.build
        if b is None:
            continue
        def hp(*a, **k):  # remote podman needs the substrate proxy (stacks is egress-denied)
            if host is None:
                return host_podman(None, *a, **k)
            return host_run(host, ["env", f"HTTPS_PROXY={REMOTE_PROXY}",
                                   f"HTTP_PROXY={REMOTE_PROXY}",
                                   "podman", *a], **k)
        if b.containerfile is None:
            if hp("image", "exists", b.image,
                  check=False, capture=True).returncode != 0:
                if verbose:
                    print(f"==> pull {b.image}")
                hp("pull", *quiet, b.image)
            elif verbose:
                print(f"==> {b.image} present")
            continue
        cf = (infra_repo / b.containerfile).read_text()
        base = b.image.split("/")[-1].split(":")[0]
        images_dir = str(state_dir() / "images") if host is None else f"{REMOTE_STATE_ROOT}/images"
        cf_path = f"{images_dir}/{base}.Containerfile"
        host_write(host, cf_path, cf)
        build_args = []
        if host is not None:
            # RUN steps get their own netns: 127.0.0.1 would be the build
            # container itself. --network=host makes the proxy's loopback
            # reachable; packets still carry the (denied) stacks uid, so the
            # proxy remains the only path.
            build_args += ["--network=host"]
            for v in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy"):
                build_args += ["--build-arg", f"{v}={REMOTE_PROXY}"]
            build_args += ["--build-arg", "NO_PROXY=127.0.0.1,localhost"]
        if verbose:
            print(f"==> build {b.image} ({b.containerfile})")
        hp("build", *quiet, "-t", b.image, "-f", cf_path, *build_args, images_dir)


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


# Session worktrees (ADR 0037): kicked sessions each work in their own git
# worktree under <repo>/.veggies/wt/ so parallel sessions never share a
# checkout. The dir is hidden via the clone's .git/info/exclude (local to
# this clone; the committed .gitignore layer exists only in OUR repo).
# Root-anchored: an unanchored pattern would swallow nested .veggies/ dirs.
WORKTREE_EXCLUDE = "/.veggies/"


def info_exclude_add(text: str, line: str) -> str:
    """Add `line` to git info/exclude content, idempotently. Matching is
    whole-line: an existing '.veggies/' pattern must not satisfy '/.veggies/'."""
    if line in text.splitlines():
        return text
    if text and not text.endswith("\n"):
        text += "\n"
    return text + line + "\n"


def ensure_worktree_exclude(host: str | None, repo_path: str) -> None:
    """Exclude the session-worktree dir in the stack repo's info/exclude
    (ADR 0037) so parallel sessions never appear in each other's - or the
    operator's - git status. Resolves the git COMMON dir: a mounted repo
    that is itself a linked worktree must write the main repo's
    info/exclude - git ignores <admin>/info/exclude in a worktree's own
    admin dir (verified 2026-09-11, git 2.54). Best-effort: not-a-git-repo,
    a missing git binary, or an unwritable file only warns; `up` must not
    fail over an ignore line."""
    if host is not None:
        q = shlex.quote(WORKTREE_EXCLUDE)
        sh = ('gd=$(git -C "$1" rev-parse --path-format=absolute '
              '--git-common-dir 2>/dev/null) || exit 0; '
              'mkdir -p "$gd/info" && '
              f'(grep -qxF {q} "$gd/info/exclude" 2>/dev/null || '
              f'echo {q} >> "$gd/info/exclude")')
        r = host_run(host, ["sh", "-c", sh, "sh", repo_path],
                     check=False, capture=True)
        if r.returncode != 0:
            print(f"warning: could not exclude {WORKTREE_EXCLUDE} in "
                  f"{repo_path}: {r.stderr.strip()}", file=sys.stderr)
        return
    try:
        r = run(["git", "-C", repo_path, "rev-parse", "--path-format=absolute",
                 "--git-common-dir"], check=False, capture=True)
    except OSError as exc:  # git itself missing: mount mode never needed it
        print(f"warning: could not exclude {WORKTREE_EXCLUDE} in "
              f"{repo_path}: {exc}", file=sys.stderr)
        return
    if r.returncode != 0:
        return  # not a git repo: nothing to exclude in
    try:
        info = Path(r.stdout.strip()) / "info"
        info.mkdir(parents=True, exist_ok=True)
        exclude = info / "exclude"
        exclude.write_text(info_exclude_add(
            exclude.read_text() if exclude.exists() else "",
            WORKTREE_EXCLUDE))
    except OSError as exc:
        print(f"warning: could not exclude {WORKTREE_EXCLUDE} in "
              f"{repo_path}: {exc}", file=sys.stderr)


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
    return f"""# Generated by veggies. Removed by `veggies down`.
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


WATCHDOG_SERVICE = """# Generated by veggies. Restarts crashed stack containers.
# Under a systemd unit, podman delegates container restart policy to systemd
# (event log: died -> cleanup, no restart), so a killed stack container would
# otherwise stay dead until the next boot. Verified 2026-09-04.
[Unit]
Description=veggies watchdog: restart exited stack containers

[Service]
Type=oneshot
ExecStart=/bin/sh -c "podman ps -aq --filter label=app=veggies --filter status=exited | xargs -r podman container restart"
"""

WATCHDOG_TIMER = """# Generated by veggies.
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


def stack_name_from(repo_arg: str) -> str:
    """Stack name from the --repo arg. URLs sanitize as-is; local paths
    resolve first - bare `veggies up` passes '.', and the name must come
    from the cwd's basename, not the literal string."""
    if "://" in repo_arg or repo_arg.startswith("git@"):
        return sanitize_name(repo_arg)
    return sanitize_name(str(Path(repo_arg).expanduser().resolve()))


def stack_url(record: dict) -> str:
    """Attach URL: loopback locally, the tailnet name for remote stacks."""
    host = record["host"] or "127.0.0.1"
    return f"http://{host}:{record['port']}"


def discover_repo_config(host: str | None, repo_path: str) -> tuple[dict, list[str]]:
    """veggies.yml from the target repo: local path, or the
    remote clone over ssh. Missing file = empty config, never an error."""
    if host is None:
        return load_repo_config(Path(repo_path))
    r = host_run(host, ["cat", f"{repo_path}/veggies.yml"], check=False, capture=True)
    if r.returncode != 0:
        return {}, []
    return parse_repo_config(r.stdout)


def cmd_render(args: argparse.Namespace) -> int:
    infra_repo = Path(__file__).parent.parent.resolve()
    name = args.name or stack_name_from(args.repo)
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
        selections=cfg.get("selections"),
        mcps=tuple(cfg.get("mcps") or ()),
        github=cfg.get("github", False),
    )
    sys.stdout.write(render_yaml(spec, infra_repo))
    return 0


def warn_if_root(host: str | None) -> None:
    """Stacks assume a rootless user (systemd --user, linger, rootless
    podman); root mostly works but is off-label - say so, don't block."""
    if host is None and hasattr(os, "geteuid") and os.geteuid() == 0:
        print("!! running as root: stacks assume a rootless user "
              "(systemd --user, linger) - untested, use a normal account",
              file=sys.stderr)


def warm_api(host: str | None, port: int, password: str,
             timeout: int = 180) -> bool:
    """Absorb opencode's cold bootstrap: the first authenticated API call
    installs plugins through the proxy (~1 min) and looks like a hang to
    whoever triggers it (verified 2026-09-10, `veggies status` right after
    up). Probe until 200 so the stall happens inside `up`, not later."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if probe_api(host, port, password,
                     "/config?directory=/workspace") is not None:
            return True
        time.sleep(3)
    return False


def cmd_prepare(args: argparse.Namespace) -> int:
    """Pre-build/pull a stack's images on the target host - the slow part of
    a first `up`, streamed live (no -q). Touches no pods, secrets or state;
    safe to re-run (layer cache). Image selection comes from the LOCAL
    --repo checkout's veggies.yml (a fresh host has no clone to read yet)."""
    infra_repo = Path(__file__).parent.parent.resolve()
    host = args.host
    name = args.name or stack_name_from(args.repo)
    local = Path(args.repo).expanduser()
    if local.is_dir():
        cfg, warnings = load_repo_config(local)
    else:
        cfg, warnings = {}, [f"{args.repo}: not a local checkout - "
                             "preparing the default component set (per-repo "
                             "mcps are only visible in a local --repo)"]
    for w in warnings:
        print(f"warning: {w}", file=sys.stderr)
    spec = StackSpec(name=name, repo=str(local if local.is_dir() else args.repo),
                     mode="mount", port=0, host=host,
                     mcps=tuple(cfg.get("mcps") or ()))
    t0 = time.monotonic()
    ensure_images(host, infra_repo, spec, verbose=True)
    print(f"\nimages ready on {host or 'this machine'} "
          f"in {time.monotonic() - t0:.0f}s")
    nxt = f"veggies up --repo {args.repo} --name {name}"
    if host:  # remote stacks are clone-mode (cmd_up enforces)
        nxt = (f"veggies up --host {host} --clone "
               f"--repo $(git remote get-url origin) --name {name} -y")
    print(f"next: {nxt}")
    return 0


def cmd_up(args: argparse.Namespace) -> int:
    infra_repo = Path(__file__).parent.parent.resolve()
    state = State()
    host = args.host
    warn_if_root(host)

    name = args.name or stack_name_from(args.repo)
    if host and not args.clone:
        raise ValueError(
            "remote stacks are clone-mode: pass --clone with a git URL "
            "(a local bind-mount is meaningless on another host)"
        )
    if args.clone:
        if host:
            clone_dir = f"{REMOTE_STATE_ROOT}/clones/{name}"
            if not host_exists(host, clone_dir, kind="d"):
                host_run(host, remote_clone_cmd(host, args.repo, clone_dir))
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
    if existing and (existing["host"] or None) != host:
        # Names are the state primary key: a same-named stack on another
        # host would silently reuse the port and then overwrite the record
        # (verified 2026-09-10: re-upping "veggie" on the VPS orphaned the
        # local stack's record).
        raise ValueError(
            f"stack {name!r} already exists on host "
            f"{existing['host'] or 'local'} - pick another --name or "
            "`veggies down` it first")
    port = existing["port"] if existing else allocate_port(state.used_ports())
    spec = StackSpec(name=name, repo=repo_path, mode=mode, port=port, host=host,
                     model=args.model or cfg.get("model"),
                     components=cfg.get("components"),
                     selections=cfg.get("selections"),
                     mcps=tuple(cfg.get("mcps") or ()),
                     github=args.github or cfg.get("github", False),
                     created=existing["created"] if existing else
                     datetime.now(timezone.utc).isoformat(timespec="seconds"))
    if spec.model:
        print(f"model:   litellm/{spec.model} (veggies.yml)")
    if spec.github:
        print("github:  GH_TOKEN + gh push/PR access enabled (ADR 0030)")

    url = f"http://{host or '127.0.0.1'}:{port}"
    if sys.stdin.isatty() and not args.yes:
        print(f"stack:   {spec.pod} on {host or 'this machine'}")
        print(f"repo:    {spec.repo} ({mode})")
        print(f"attach:  {url}")
        if input("Bring it up? [Y/n] ").strip().lower() not in ("", "y", "yes"):
            print("aborted")
            return 1

    # Resolve secrets first: a missing/wrong vault password must fail in a
    # second, not after a full image build (fresh-machine onboarding).
    values = resolve_secret_values(spec)

    print("==> images")
    ensure_images(host, infra_repo, spec)

    print("==> stack config")
    write_stack_config(spec, infra_repo)
    # Everything a container bind-mounts must carry container_file_t - the
    # workspace included, remote too (verified 2026-09-09: the VPS clone
    # was never labeled and opencode got EACCES even reading /workspace;
    # the agent then "debugged the environment" instead of working).
    if host is None:
        label_for_containers(None, str(infra_repo / "agent-config" / "litellm"))
    label_for_containers(host, repo_path)
    ensure_worktree_exclude(host, repo_path)

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
    # kube play's hostPath relabeling is unreliable (label_for_containers has
    # the history): a replaced pod can leave its *private* MCS categories on
    # the repo, and the new pod then reads EACCES (verified 2026-09-10 on a
    # local re-up). Re-assert the shared label now that the pod exists -
    # chcon applies live to the running pod's mounts, no restart needed.
    label_for_containers(host, repo_path)
    state.add(spec, password=values["password"])
    print("==> warming api (first authenticated call cold-boots opencode)")
    if not warm_api(host, port, values["password"]):
        print("!! api still cold - first attach/status may take a minute")

    print(f"\nstack up: {url}  (user: opencode, password: {values['password']})")
    if host:
        print("remote attach: ssh -L {0}:127.0.0.1:{0} <host> then open "               "http://127.0.0.1:{0} in a browser (web UI) or `opencode attach` "               "(tailnet deferred - ADR 0024)".format(port))
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
                     port=record["port"], host=record["host"],
                     github=record.get("github", False))
    host = spec.host
    if host_exists(host, quadlet_path(spec)):
        host_run(host, ["rm", "-f", quadlet_path(spec)])
        host_systemctl(host, "daemon-reload", check=False)
    host_podman(host, "pod", "stop", "-t", "5", spec.pod, check=False, capture=True)
    host_podman(host, "pod", "rm", "-f", spec.pod, check=False, capture=True)
    if args.purge:
        host_podman(host, "volume", "rm", "-f", spec.volume_opencode,
                    check=False, capture=True)
        host_podman(host, "secret", "rm", *secret_names(spec),
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


def busy_titles(sessions: object, status: object) -> list[str]:
    """Pure: titles of the busy sessions, given the /session and
    /session/status payloads. Anything off-shape means 'cannot tell',
    which is the guard's degrade-to-proceed case, not an error."""
    if not isinstance(sessions, list) or not isinstance(status, dict):
        return []
    return [str(s.get("title") or s.get("id", "?"))
            for s in sessions if isinstance(s, dict)
            and (status.get(s.get("id", "")) or {}).get("type") == "busy"]


def busy_sessions(record: dict) -> list[str]:
    """Titles of sessions currently busy on the stack. Any API failure
    degrades to [] (proceed), same posture as the kick's in-flight guard -
    and a DOWN stack must stay syncable anyway."""
    password = record.get("password", "")
    if not password:
        return []
    q = "?directory=/workspace"
    sessions = probe_api(record["host"], record["port"], password,
                         f"/session{q}")
    status = probe_api(record["host"], record["port"], password,
                       f"/session/status{q}")
    return busy_titles(sessions, status)


def cmd_sync(args: argparse.Namespace) -> int:
    """Sync a clone-mode stack with its repo, then re-up (ADR 0014): pull
    the clone (workspace + the veggies.yml `up` reads), re-ship the stack
    config from THIS checkout, rebuild changed images, recreate the pod.
    The one-command 'feature merged -> live on the stack' path. Kicked
    sessions never wait for this: their bootstrap fetches and branches off
    origin/main per kick (ADR 0037)."""
    record = State().get(args.name)
    if record is None:
        raise ValueError(f"unknown stack {args.name!r} (veggies ls)")
    if record["mode"] != "clone":
        raise ValueError(
            f"stack {args.name!r} is mount-mode: the workspace IS your live "
            "checkout - a plain `veggies up` (re-ships config and images) is "
            "all there is to sync")
    host, clone_dir = record["host"], record["repo"]
    busy = busy_sessions(record)
    if busy and not args.force:
        raise ValueError(
            f"stack {args.name!r} has busy sessions: {', '.join(busy)} - "
            "sync recreates the pod and would kill them; wait for them, "
            "or --force")
    origin = host_run(host, ["git", "-C", clone_dir, "remote", "get-url",
                             "origin"], capture=True).stdout.strip()
    print(f"==> pull {clone_dir} ({host or 'this machine'})")
    if host:
        url = github_https_url(origin)
        token = None
        if url.startswith("https://github.com/") and \
                remote_repo_needs_token(host, url):
            token = vault_key("github_token", VAULT_GITHUB)
        pull = clone_pull_argv(clone_dir, proxy=REMOTE_PROXY, token=token)
    else:
        pull = clone_pull_argv(clone_dir)
    host_run(host, pull)
    # agent-config/images ship from THIS checkout at re-up - say so when it
    # is not the pushed main the operator may think they are syncing.
    infra_repo = Path(__file__).parent.parent.resolve()
    heads = run(["git", "-C", str(infra_repo), "rev-parse", "HEAD",
                 "@{upstream}"], check=False, capture=True)
    if heads.returncode == 0 and len(heads.stdout.split()) == 2 and \
            len(set(heads.stdout.split())) != 1:
        print("warning: this checkout is not at its upstream - the re-up "
              "ships agent-config/images from THIS tree, not origin/main",
              file=sys.stderr)
    up_args = argparse.Namespace(
        repo=origin, name=args.name, host=host, model=None, clone=True,
        # The recorded opt-in rides along so a --github CLI-upped stack
        # cannot silently lose its credentials; veggies.yml can only ever
        # ADD it here. Dropping github stays a down + up.
        github=record.get("github", False),
        no_attach=True, no_install=False, yes=True)
    return cmd_up(up_args)


def cmd_supervise(args: argparse.Namespace) -> int:
    """Watch an opencode session; judge each finish with a different model
    and inject a refinement message when below threshold (ADR 0028).
    Operator-invoked: runs while this command runs."""
    import supervisor
    record = State().get(args.name)
    if record is None:
        raise ValueError(f"unknown stack {args.name!r} (veggies ls)")
    host, port = record["host"], record["port"]
    password = record.get("password", "")
    if not password:
        raise ValueError(f"stack {args.name!r} has no password on record")
    pod = f"veggies-{args.name}"
    sid = args.session
    q = "?directory=/workspace"
    print(f"supervising {args.name}/{sid} (judge: {args.judge_model}, "
          f"threshold {args.threshold}, max {args.max_iters} refinements)")
    judged: set[str] = set()
    reported_perms: set[str] = set()
    scores: list[float] = []
    deadline = time.time() + args.timeout
    while time.time() < deadline:
        status = api_call(host, port, password, "GET", f"/session/status{q}")
        if status is None:
            print("api unreachable; retrying...")
            time.sleep(args.interval)
            continue
        entry = status.get(sid) if isinstance(status, dict) else None
        if entry and entry.get("type") != "idle":
            # A busy session may in fact be PARKED on a permission prompt
            # (verified 2026-09-09: a headless session sat 25 min on an
            # external_directory prompt). Surface pendings so the operator
            # knows to attach and answer; we never auto-reply.
            pending = api_call(host, port, password, "GET",
                               f"/permission{q}")
            if isinstance(pending, list):
                for perm in pending:
                    if perm.get("sessionID") == sid:
                        key = perm.get("id")
                        if key not in reported_perms:
                            reported_perms.add(key)
                            print(f"!! pending permission {key}: "
                                  f"{perm.get('permission')} "
                                  f"{perm.get('patterns')} - answer it in "
                                  "the web UI / TUI; supervision waits")
            time.sleep(args.interval)
            continue
        msgs = api_call(host, port, password, "GET",
                        f"/session/{sid}/message{q}")
        if not isinstance(msgs, list):
            raise ValueError(f"no such session {sid!r} on stack {args.name!r}")
        assistants = [m for m in msgs
                      if (m.get("info") or {}).get("role") == "assistant"]
        if not assistants:
            time.sleep(args.interval)
            continue
        last_id = assistants[-1]["info"]["id"]
        if last_id in judged:
            time.sleep(args.interval)
            continue
        transcript = supervisor.render_transcript(msgs)
        if not transcript.strip():
            judged.add(last_id)
            continue
        # Judge inside the litellm container: the master key stays in-pod.
        r = host_podman(host, "exec", "-i", f"{pod}-litellm",
                        "python3", "-",
                        input_text=supervisor.judge_exec_script(
                            args.judge_model, transcript),
                        capture=True, check=False)
        if r.returncode != 0:
            raise ValueError(f"judge exec failed: "
                             f"{redact(r.stderr.strip()[-300:])}")
        verdict = supervisor.parse_judgment(r.stdout.strip())
        scores.append(verdict["score"])
        judged.add(last_id)
        print(f"critic: score {verdict['score']:.2f} "
              f"issues={verdict['issues'] or '[]'}")
        action = supervisor.decide(scores, args.threshold, args.max_iters)
        if action == "pass":
            print(f"PASS (score {scores[-1]:.2f} >= {args.threshold})")
            return 0
        if action == "stop":
            print(f"STOP: {args.max_iters} refinements used, "
                  f"last score {scores[-1]:.2f} - needs a human")
            return 1
        prompt = supervisor.refinement_prompt(verdict)
        resp = api_call(host, port, password, "POST",
                        f"/session/{sid}/message{q}",
                        {"parts": [{"type": "text", "text": prompt}]},
                        timeout=max(args.timeout, 900))
        if resp is None:
            raise ValueError("failed to post refinement message")
        print("refinement posted; the agent is iterating...")
    print(f"timeout after {args.timeout}s (scores so far: {scores})")
    return 1


def cmd_attach(args: argparse.Namespace) -> int:
    record = State().get(args.name)
    if record is None:
        raise ValueError(f"unknown stack {args.name!r} (veggies ls)")
    url = stack_url(record)
    if record["host"]:
        # The bare hostname only resolves once a tailnet exists (ADR 0024);
        # until then attach rides a tunnel. Distinct local port: a local
        # stack on the same number silently wins the bind (verified
        # 2026-09-10).
        print(f"remote stack: tunnel first, e.g. "
              f"`ssh -N -L 5{record['port']}:127.0.0.1:{record['port']} "
              f"{record['host']}` then attach http://127.0.0.1:5{record['port']}",
              file=sys.stderr)
    harness = harness_of(spec_from_record(args.name, record))
    if harness is None or harness.attach is None:
        print(f"this stack's harness is not attachable via the CLI; "
              f"url: {url} (password: {record['password']})")
        return 1
    argv = harness.attach(url, record["password"])
    if not shutil.which(argv[0]):
        print(f"{argv[0]} CLI not found; attach manually: {url} "
              f"(password: {record['password']})")
        return 1
    os.execvp(argv[0], argv)
    return 0  # unreachable


# --- web UI / session listing (ADR 0034) ---------------------------------------


def ui_dir_segment(directory: str = "/workspace") -> str:
    """The web UI scopes everything by directory: the route carries it as
    base64url without padding (read from the 1.18.27 bundle: btoa, +/ ->
    -_, = stripped). The bare root URL lands on an empty 'no project'
    state (verified 2026-09-11) - links must include this segment."""
    return base64.urlsafe_b64encode(directory.encode()).decode().rstrip("=")


def port_free(port: int) -> bool:
    """True if nothing listens on 127.0.0.1:port locally."""
    import socket
    with socket.socket() as s:
        return s.connect_ex(("127.0.0.1", port)) != 0


def pick_ui_port(stack_port: int, is_free=None) -> int:
    """Local side of a UI tunnel: stack_port + 1000 (4098 -> 5098), else the
    next free port. A LOCAL stack already on the same number silently wins
    an ssh -L bind and every request then hits the wrong stack (verified
    2026-09-10), so 'free' must mean really free."""
    is_free = is_free or port_free  # late-bind: tests monkeypatch port_free
    candidates = [stack_port + 1000, *range(5200, 5300)]
    for p in candidates:
        if 1024 < p <= 65535 and is_free(p):
            return p
    raise ValueError("no free local port for the tunnel")


def _tunnel_file(name: str) -> Path:
    return state_dir() / "tunnels" / f"{name}.json"


def _tunnel_alive(path: Path) -> dict | None:
    try:
        info = json.loads(path.read_text())
        os.kill(int(info["pid"]), 0)
        return info
    except (OSError, ValueError, KeyError, json.JSONDecodeError):
        return None


def _ui_stop(name: str) -> int:
    path = _tunnel_file(name)
    info = _tunnel_alive(path)
    if info:
        os.kill(int(info["pid"]), 15)
        print(f"tunnel to {name} stopped (was :{info['port']})")
    else:
        print("no live tunnel on record")
    path.unlink(missing_ok=True)
    return 0


IDLE_ROWS_DEFAULT = 10  # one screen of the most-recently-touched idle


def live_first(sessions: list, status: object) -> list:
    """Pure watch-path ordering (ADR 0044): live sessions first, then idle,
    newest-updated first within each group (missing timestamps sort last;
    the sort is stable). 'Live' = present in the /session/status map with a
    type other than 'idle' - the endpoint lists only non-idle sessions
    (ADR 0017), and this matches the supervisor daemon's predicate
    (deploy/supervisor/daemon.py) so the two never disagree about the same
    session. An unknown/garbage status payload - top-level or any single
    entry - degrades to plain newest-first: 'cannot tell' must not
    reorder anything."""
    def ts(s):
        t = s.get("time") or {}
        v = t.get("updated") or t.get("created") or 0
        return v if isinstance(v, (int, float)) else 0

    def live(s):
        if not isinstance(status, dict):
            return False
        entry = status.get(str(s.get("id", "")))
        return isinstance(entry, dict) and entry.get("type", "idle") != "idle"

    ordered = sorted(sessions, key=ts, reverse=True)
    return [s for s in ordered if live(s)] + \
        [s for s in ordered if not live(s)]


def session_links(sessions: list, status: dict, base_url: str,
                  limit: int = 5) -> list[str]:
    """Pure: 'state title url' lines, live first (ADR 0044), then
    newest-updated idle. The web UI's dir route alone opens a composer,
    not a session list (verified 2026-09-11), so ui prints deep links to
    the actual session views."""
    lines = []
    for s in live_first(sessions, status)[:limit]:
        sid = s.get("id", "")
        st = (status.get(sid) or {}).get("type", "idle") \
            if isinstance(status, dict) else "idle"
        title = str(s.get("title") or "(untitled)")[:44]
        lines.append(f"  {st:<6} {title:<46} "
                     f"{base_url}/{ui_dir_segment()}/session/{sid}")
    return lines


def cmd_ui(args: argparse.Namespace) -> int:
    """Easy web UI access: print URL+password; for remote stacks, hold a
    background ssh -L tunnel (pidfile in the state dir) so the terminal
    stays free."""
    record = State().get(args.name)
    if record is None:
        raise ValueError(f"unknown stack {args.name!r} (veggies ls)")
    if args.stop:
        return _ui_stop(args.name)
    password = record.get("password", "")
    if not record["host"]:
        url = f"http://127.0.0.1:{record['port']}"
        pid = None
    else:
        path = _tunnel_file(args.name)
        info = _tunnel_alive(path)
        if info:
            local, pid = info["port"], int(info["pid"])
        else:
            local = args.port or pick_ui_port(record["port"])
            proc = subprocess.Popen(
                ["ssh", "-N", "-L",
                 f"{local}:127.0.0.1:{record['port']}", record["host"]],
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL, start_new_session=True)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps({"pid": proc.pid, "port": local}))
            pid = proc.pid
            # Wait until the tunnel actually serves (auth-checked) or die.
            deadline = time.time() + 15
            while time.time() < deadline:
                if probe_api(None, local, password,
                             "/config?directory=/workspace") is not None:
                    break
                if proc.poll() is not None:
                    path.unlink(missing_ok=True)
                    raise ValueError(
                        f"ssh tunnel to {record['host']} died - "
                        f"check `ssh {record['host']}`")
                time.sleep(0.5)
        url = f"http://127.0.0.1:{local}"
    print(f"web UI: {url}/{ui_dir_segment()}  "
          f"(user: opencode, password: {password})")
    # Home is the all-sessions view, but its project list is browser-local
    # (no server registration API in opencode 1.18.27, verified): add
    # /workspace once per browser. Deep links below bypass that entirely.
    print(f"home:   {url}/  (per-browser: Add project -> /workspace once)")
    if pid:
        print(f"tunnel pid {pid}; close with: veggies ui {args.name} --stop")
    # Deep links to live sessions: after tunneling, the API is loopback-
    # local on either host flavor, so this works for local and remote alike.
    probe_port = local if record["host"] else record["port"]
    sessions = api_call(None, probe_port, password, "GET",
                        "/session?directory=/workspace")
    if isinstance(sessions, list) and sessions:
        status = api_call(None, probe_port, password, "GET",
                          "/session/status?directory=/workspace")
        print("sessions (live first):")
        links = session_links(sessions,
                              status if isinstance(status, dict) else {},
                              url)
        for line in links:
            print(line)
        if len(sessions) > len(links):
            print(f"  ... and {len(sessions) - len(links)} more - "
                  f"`veggies sessions {args.name}` lists them, live first")
    if args.open_browser and shutil.which("xdg-open"):
        subprocess.Popen(["xdg-open", url], stdin=subprocess.DEVNULL,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return 0


def format_sessions(sessions: list, status: dict | None,
                    issue: int | None = None, show_all: bool = False,
                    idle_limit: int = IDLE_ROWS_DEFAULT) -> str:
    """Pure table, live first (ADR 0044): one row per session; kicked
    sessions carry '#N:' titles (ADR 0034), so --issue filters on the
    title prefix. Idle rows are capped at idle_limit (default
    IDLE_ROWS_DEFAULT; --all lifts the cap, --issue is never capped) -
    live rows are never hidden: the runbook's stale-worktree ownership
    check stands on seeing every live session. The cap only engages when
    the status map actually answered: an unreachable /session/status
    (status=None) degrades to the full newest-first table - 'cannot
    tell' must never hide a session."""
    rows = []
    for s in live_first(sessions, status):
        sid = str(s.get("id", ""))
        title = str(s.get("title") or "(untitled)")
        if issue is not None and f"#{issue}:" not in title:
            continue
        st = (status.get(sid) or {}).get("type", "idle") \
            if isinstance(status, dict) else "idle"
        ts = (s.get("time") or {}).get("updated") or \
            (s.get("time") or {}).get("created")
        when = ""
        if isinstance(ts, (int, float)):
            when = datetime.fromtimestamp(ts / 1000, tz=timezone.utc) \
                .strftime("%m-%d %H:%M")
        rows.append((sid, st, when, title))
    if not rows:
        return "no sessions" + (f" for issue #{issue}" if issue else "")
    hidden = 0
    if not show_all and issue is None and isinstance(status, dict):
        kept = []
        idle_seen = 0
        for row in rows:
            if row[1] != "idle":
                kept.append(row)  # live rows are never hidden
            elif idle_seen < idle_limit:
                kept.append(row)
                idle_seen += 1
            else:
                hidden += 1
        rows = kept
    out = [f"{'SESSION':<26} {'STATE':<6} {'UPDATED':<12} TITLE"]
    for sid, st, when, title in rows:
        out.append(f"{sid:<26} {st:<6} {when:<12} {title[:60]}")
    if hidden:
        out.append(f"... and {hidden} more idle "
                   f"session{'s' if hidden != 1 else ''} (use --all)")
    return "\n".join(out)


def cmd_sessions(args: argparse.Namespace) -> int:
    record = State().get(args.name)
    if record is None:
        raise ValueError(f"unknown stack {args.name!r} (veggies ls)")
    password = record.get("password", "")
    q = "?directory=/workspace"
    sessions = api_call(record["host"], record["port"], password, "GET",
                        f"/session{q}")
    if not isinstance(sessions, list):
        raise ValueError(f"stack {args.name!r} API unreachable "
                         f"(veggies status {args.name})")
    status = api_call(record["host"], record["port"], password, "GET",
                      f"/session/status{q}")
    print(format_sessions(sessions, status, args.issue, show_all=args.all))
    return 0


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
        os.execvp("ssh", ["ssh", record["host"],
                          remote_sh(record["host"], cmd)])
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


# --- veggies costs (ADR 0022 decision 4; record contract ADR 0051) --------------
# All parsing/aggregation/rendering is pure in costs.py; only the log read
# (filesystem or one ssh round-trip) and the --pr gh lookup live here.

# Lists then cats every spend.jsonl* segment; basenames ride the first line
# ("segments:a b") so the summary header can name what it read. awk '1'
# normalizes a missing trailing newline - a franken-line across the segment
# seam would count as malformed and shrink the report noisily.
_COSTS_REMOTE_READ = "\n".join([
    'found=""',
    'for f in "$1"*; do',
    '  [ -f "$f" ] || continue',
    '  found="$found ${f##*/}"',
    'done',
    '[ -n "$found" ] || exit 3',  # no spend.jsonl* -> missing log
    "printf 'segments:%s\\n' \"${found# }\"",
    'for f in "$1"*; do',
    '  [ -f "$f" ] || continue',
    # propagate a mid-read failure: it must never look like "missing"
    "  awk '1' \"$f\" || exit 4",
    'done',
])


def _read_spend_log(host: str | None,
                    log_base: str) -> tuple[str, list[str]] | None:
    """(text, segment basenames) over all spend.jsonl* segments, or None
    when none exist. Rotated segments are plain text (ADR 0051 decision 1)."""
    if host is None:
        d = Path(log_base).parent
        paths = []
        if d.is_dir():
            # is_file: a spend.jsonl* DIRECTORY must not raise past main()
            paths = sorted(p for p in d.glob(costs.SPEND_LOG_NAME + "*")
                           if p.is_file())
        if not paths:
            return None
        texts = []
        for p in paths:
            try:
                # errors="replace": a forbidden compressed/rotten segment
                # surfaces as counted malformed lines, never silent shrink.
                texts.append(p.read_text(errors="replace").rstrip("\n"))
            except OSError as exc:
                raise ValueError(f"cannot read spend segment {p}: {exc}") \
                    from None
        # Drop empty segments so a non-final one can't fabricate a phantom
        # blank (skipped) line at the seam - remote cat behaves the same.
        return "\n".join(t for t in texts if t), [p.name for p in paths]
    r = host_run(host, ["sh", "-c", _COSTS_REMOTE_READ, "sh", log_base],
                 check=False, capture=True)
    if r.returncode == 3:
        return None  # the script's own "no spend.jsonl* segments" sentinel
    if r.returncode != 0:
        # Any other failure (rotation race, dropped read, ssh error) is an
        # error with detail - it must never masquerade as "no spend log".
        detail = r.stderr.strip() or f"exit {r.returncode}"
        raise ValueError(f"failed to read spend log on {host}: {detail}")
    lines = r.stdout.splitlines()
    if not lines or not lines[0].startswith("segments:"):
        raise ValueError(f"unexpected output reading spend log on {host}")
    return "\n".join(lines[1:]), lines[0][len("segments:"):].split()


def _owner_repo(repo_url: str) -> str:
    """owner/repo for `gh -R`: strip the scheme/host (or git@ host:) and any
    .git suffix. Degenerate recorded URLs are a clean error, not IndexError."""
    path = re.sub(r"\.git$", "", repo_url.rstrip("/"))
    if path.startswith("git@"):
        path = path.split(":", 1)[1] if ":" in path else ""
    elif "://" in path:
        rest = path.split("://", 1)[1]
        path = rest.split("/", 1)[1] if "/" in rest else ""
    parts = [p for p in path.split("/") if p]
    if len(parts) < 2:
        raise ValueError(f"cannot derive owner/repo from recorded repo "
                         f"{repo_url!r} - pass --issue N directly")
    return "/".join(parts[-2:])


def _resolve_pr_issue(record: dict, pr: int) -> int:
    """PR N -> issue M via the agent/issue-M branch convention (ADR 0035),
    using the operator's gh. The only place gh is ever called."""
    if shutil.which("gh") is None:
        raise ValueError("--pr needs the gh CLI, which was not found - "
                         "pass --issue N directly")
    repo = record["repo"]
    argv = ["gh", "pr", "view", str(pr), "--json", "headRefName"]
    if "://" in repo or repo.startswith("git@"):
        r = run([*argv, "-R", _owner_repo(repo)], check=False, capture=True)
    else:  # mount mode: the local checkout tells gh which repo
        r = run(argv, check=False, capture=True, cwd=repo)
    if r.returncode != 0:
        raise ValueError(f"gh pr view {pr} failed: {r.stderr.strip()} - "
                         "pass --issue N directly")
    data = json.loads(r.stdout)
    if not isinstance(data, dict):
        raise ValueError(f"gh pr view {pr} returned unexpected JSON "
                         f"{data!r} - pass --issue N directly")
    branch = data.get("headRefName")
    if not isinstance(branch, str):
        raise ValueError(f"gh pr view {pr} returned unexpected headRefName "
                         f"{branch!r} - pass --issue N directly")
    m = re.match(r"^agent/issue-(\d+)$", branch)
    if not m:
        raise ValueError(f"PR #{pr} is on branch {branch!r}, not "
                         "agent/issue-N - pass --issue N directly")
    return int(m.group(1))


def cmd_costs(args: argparse.Namespace) -> int:
    record = State().get(args.name)
    if record is None:
        raise ValueError(f"unknown stack {args.name!r} (veggies ls)")
    if args.since is not None:
        try:
            since = datetime.strptime(args.since, "%Y-%m-%d").date()
        except ValueError:
            raise ValueError(f"--since must be a date like 2026-09-01 "
                             f"(got {args.since!r})") from None
    else:
        since = None
    spec = StackSpec(name=args.name, repo=record["repo"], host=record["host"])
    log_base = f"{spec.state_root()}/{args.name}/{costs.SPEND_LOG_NAME}"
    got = _read_spend_log(record["host"], log_base)
    if got is None:
        print(f"no spend log for stack {args.name!r} yet - metering lands "
              f"with #46 (ADR 0022/0051); {log_base}")
        return 0
    text, segments = got
    parsed = costs.parse_spend_log(text)
    if not parsed.records and parsed.skipped == 0:
        print(f"spend log exists but has no records yet ({log_base})")
        return 0
    records = sorted(parsed.records, key=lambda r: r.ts)  # ADR 0051: by ts
    today = datetime.now(timezone.utc).date()
    if since is None:
        since = (datetime.fromtimestamp(records[0].ts, tz=timezone.utc).date()
                 if records else today)
    # the suffix marks a real floor; an empty parse has no earliest record
    earliest = args.since is None and bool(records)
    window = costs.filter_since(records, since)
    if not window and args.since is not None:
        print(f"no spend records since {since.isoformat()} ({log_base})")
        return 0
    weekly = costs.use_weekly((today - since).days, args.weekly)
    if args.issue is not None or args.pr is not None or \
            args.session is not None:
        if args.pr is not None:
            number = _resolve_pr_issue(record, args.pr)
            target, subject = ("issue", number), f"Issue #{number}"
        elif args.issue is not None:
            target = ("issue", args.issue)
            subject = f"Issue #{args.issue}"
        else:
            target = None
            subject = f"Sessions matching {args.session!r}"
        if target is not None:
            sel = [r for r in window if costs.attribute(r.session) == target]
        else:
            needle = args.session.lower()
            sel = [r for r in window if needle in r.session.lower()]
        print(costs.render_detail(
            sel, subject=subject,
            unpriced=sum(1 for r in sel if r.spend is None),
            weekly=weekly))
        return 0
    print(costs.render_summary(
        costs.summarize(window), since=since, today=today, earliest=earliest,
        segments=segments, skipped=parsed.skipped,
        unpriced=sum(1 for r in window if r.spend is None),
        bars=costs.rollup(window, weekly), weekly=weekly))
    return 0


API_TIMEOUT = 90  # cold bootstrap (~20s: plugin cache warm-up) must fit


def api_call(host: str | None, port: int, password: str, method: str,
             path: str, body: dict | None = None,
             timeout: int = API_TIMEOUT) -> object | None:
    """Any-method opencode API call with the stack's basic auth (GET sibling
    of probe_api). Body goes over stdin to curl remotely - never argv.
    Returns parsed JSON, or None on any failure."""
    import urllib.request
    url = f"http://127.0.0.1:{port}{path}"
    data = json.dumps(body) if body is not None else None
    if host:
        argv = ["curl", "-s", "-m", str(timeout), "-u",
                f"opencode:{password}", "-X", method, url]
        if data is not None:
            argv += ["-H", "Content-Type: application/json",
                     "--data-binary", "@-"]
        r = run(["ssh", host, " ".join(shlex.quote(a) for a in argv)],
                input_text=data, check=False, capture=True)
        if r.returncode != 0 or not r.stdout.strip():
            return None
        try:
            return json.loads(r.stdout)
        except json.JSONDecodeError:
            return None
    req = urllib.request.Request(url, method=method,
                                 data=data.encode() if data else None)
    req.add_header("Authorization", "Basic " + _basic_auth(password))
    if data is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
            return json.loads(raw) if raw.strip() else {}
    except Exception:  # connection refused, timeout, 401/5xx, bad json
        return None


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


def spec_from_record(name: str, record: dict) -> StackSpec:
    """The stack as it was brought up (state persists the wiring choices)."""
    selections = dict(record.get("selections") or {})
    if selections.pop("control-plane", None) is not None:
        print(f"warning: stack {name!r} selected the retired canvas control "
              "plane (ADR 0028); ignoring it - delete and re-up to drop the "
              "container", file=sys.stderr)
    return StackSpec(name=name, repo=record["repo"], host=record["host"],
                     components=record.get("components"),
                     selections=selections or None,
                     mcps=tuple(record.get("mcps") or ()),
                     github=record.get("github", False))


def harness_of(spec: StackSpec) -> Component | None:
    return next((c for c in stack_components(spec)
                 if c.provides == "harness"), None)


def format_status(name: str, record: dict, containers: list[tuple[str, str, str]],
                  api_results: list[str] | None) -> str:
    """Pure formatter for `veggies status` (tested without IO)."""
    lines = [f"stack: {name} ({record['mode']}, port {record['port']}, "
             f"host {record['host'] or 'local'})",
             f"repo:  {record['repo']}"]
    lines.append("containers:")
    for cname, status, health in containers:
        lines.append(f"  {cname:<28} {status:<10} {health}")
    if api_results is None:
        lines.append("api:     unreachable (down, or cold bootstrap in progress - retry)")
    else:
        lines.append(f"api:     ok  ({', '.join(api_results)})")
    return "\n".join(lines)


def cmd_status(args: argparse.Namespace) -> int:
    record = State().get(args.name)
    if record is None:
        raise ValueError(f"unknown stack: {args.name} (see `veggies ls`)")
    spec = spec_from_record(args.name, record)
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

    api_results: list[str] | None = None
    password = record.get("password", "")
    components = stack_components(spec)
    if password and any(c[1] == "running" for c in containers):
        results = []
        for comp in components:
            for probe in comp.probes(spec):
                if probe.kind == "http":  # http probes are served by the harness
                    j = probe_api(record["host"], record["port"], password,
                                  probe.http_path)
                    if j is None:
                        results = None  # one failed probe = api unreachable
                        break
                else:  # exec probes run inside the probe-owning container
                    r = host_podman(record["host"], "exec", f"{spec.pod}-{comp.name}",
                                    *probe.exec_argv, capture=True, check=False)
                    out = r.stdout.strip()
                    try:
                        j = json.loads(out)
                    except (json.JSONDecodeError, ValueError):
                        j = out  # plain-text line is fine for extract
                    if r.returncode != 0:
                        j = None
                if j is None:
                    results.append(f"{probe.label} unreachable")
                else:
                    results.append(f"{probe.label} {probe.extract(j)}")
            if results is None:
                break
        api_results = results
    print(format_status(args.name, record, containers, api_results))
    return 0




LEGACY_STATE_DIR = Path("~/.local/state/garden")


def legacy_hint() -> None:
    """One-time rename notice. Pure detection, no auto-migration:
    podman secrets/volumes cannot be renamed, only recreated."""
    if LEGACY_STATE_DIR.expanduser().exists() and not state_dir().exists():
        print(
            "veggies: found legacy 'garden' state (project renamed to veggies).\n"
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

    p_status = sub.add_parser("status", help="stack health + agent/session view")
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
    p_up.add_argument("--github", action="store_true",
                      help="deliver the vault's github_token as GH_TOKEN + git "
                      "credential config (push/PR; ADR 0030)")
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

    p_sync = sub.add_parser("sync", help="pull a clone-mode stack's repo, "
                            "then re-up (workspace + config + images go live)")
    p_sync.add_argument("name")
    p_sync.add_argument("--force", action="store_true",
                        help="sync even while sessions are busy (recreating "
                        "the pod kills them)")
    p_sync.set_defaults(func=cmd_sync)

    p_attach = sub.add_parser("attach", help="attach the opencode TUI to a stack")
    p_attach.add_argument("name")
    p_attach.set_defaults(func=cmd_attach)

    p_ui = sub.add_parser("ui", help="print the web UI URL; tunnel remote stacks")
    p_ui.add_argument("name")
    p_ui.add_argument("--port", type=int, default=None,
                      help="local tunnel port (default: stack port + 1000)")
    p_ui.add_argument("--stop", action="store_true", help="close the tunnel")
    p_ui.add_argument("--open", action="store_true", dest="open_browser",
                      help="xdg-open the URL")
    p_ui.set_defaults(func=cmd_ui)

    p_sessions = sub.add_parser("sessions",
                                help="list sessions on a stack (live first)")
    p_sessions.add_argument("name")
    p_sessions.add_argument("--issue", type=int, default=None,
                            help="only sessions titled '#N: ...'")
    p_sessions.add_argument("--all", action="store_true", dest="all",
                            help="show every session (default: live first, "
                                 "idle capped at 10)")
    p_sessions.set_defaults(func=cmd_sessions)

    p_logs = sub.add_parser("logs", help="pod logs (or one container)")
    p_logs.add_argument("name")
    p_logs.add_argument("container", nargs="?",
                        choices=["opencode", "litellm", "squid",
                                 "supervisor"])
    p_logs.add_argument("-f", "--follow", action="store_true")
    p_logs.set_defaults(func=cmd_logs)

    p_costs = sub.add_parser(
        "costs", help="per-call spend rollup from the stack's spend.jsonl "
        "(ADR 0022/0051)")
    p_costs.add_argument("name")
    p_costs.add_argument("--since", default=None, metavar="YYYY-MM-DD",
                         help="window start (default: earliest retained record)")
    p_costs.add_argument("--weekly", action="store_true",
                         help="ISO-week buckets (auto beyond 62 days)")
    p_costs_detail = p_costs.add_mutually_exclusive_group()
    p_costs_detail.add_argument("--issue", type=int, default=None,
                                help="detail for sessions titled '#N: ...'")
    p_costs_detail.add_argument("--pr", type=int, default=None,
                                help="detail for the issue behind PR N "
                                "(resolves agent/issue-N via gh)")
    p_costs_detail.add_argument("--session", default=None, metavar="SUBSTR",
                                help="detail for titles containing SUBSTR "
                                "(case-insensitive)")
    p_costs.set_defaults(func=cmd_costs)

    p_prepare = sub.add_parser(
        "prepare", help="pre-build/pull a stack's images on a host, with "
        "build logs (the slow part of a first up)")
    p_prepare.add_argument("--repo", default=os.environ.get("VEGGIES_REPO", "."),
                           help="LOCAL checkout; its veggies.yml selects images")
    p_prepare.add_argument("--host", default=os.environ.get("VEGGIES_HOST"))
    p_prepare.add_argument("--name", default=os.environ.get("VEGGIES_NAME"))
    p_prepare.set_defaults(func=cmd_prepare)

    p_sup = sub.add_parser(
        "supervise",
        help="watch a session; judge finishes, inject refinements (ADR 0028)")
    p_sup.add_argument("name")
    p_sup.add_argument("--session", required=True, help="session id to watch")
    p_sup.add_argument("--threshold", type=float, default=0.6)
    p_sup.add_argument("--max", type=int, default=2, dest="max_iters",
                       help="max refinement injections before giving up")
    p_sup.add_argument("--judge-model", default="deepseek-v4",
                       help="litellm alias; must differ from the author model")
    p_sup.add_argument("--interval", type=int, default=10,
                       help="poll seconds")
    p_sup.add_argument("--timeout", type=int, default=3600)
    p_sup.set_defaults(func=cmd_supervise)

    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except (ValueError, FileNotFoundError, subprocess.CalledProcessError) as exc:
        print(f"veggies: error: {redact(str(exc))}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
