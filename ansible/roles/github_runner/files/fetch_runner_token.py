#!/usr/bin/env python3
"""Fetch a short-lived GitHub Actions runner registration token.

Called by systemd ExecStartPre before every runner (re)start, so no long-lived
runner token is ever stored. Two hops (ADR 0063): mint a GitHub App
installation token narrowed to runner administration on the runner's own
repository (or the org's self-hosted runners for scope=org), then exchange
it for the registration token. Reads configuration from the environment
(written by ansible into api.env, mode 0600):

  GITHUB_APP_ID               the App's id.
  GITHUB_APP_INSTALLATION_ID  its installation on the owner.
  GITHUB_APP_PEM_PATH         0600 file holding the App private key (PEM).
  GITHUB_OWNER                org or user name.
  GITHUB_RUNNER_SCOPE         "repo" (default) or "org".
  GITHUB_RUNNER_LABELS        comma-separated extra labels.

Arguments:
  --instance NAME  quadlet instance name; for scope=repo the repo is derived
                   from it ("<repo>-<n>").
  --out PATH       env file to write (RUNNER_TOKEN, RUNNER_URL, RUNNER_NAME).

The token itself is never printed - only written to the env file.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.request
from pathlib import Path

# The minter is installed beside this script (ansible copies both into
# ~/.local/bin); the repo's files/ entry is a symlink to scripts/, so the
# same path trick serves the tests that load this file by location.
sys.path.insert(0, str(Path(__file__).resolve().parent))
import github_app_token  # noqa: E402

API_BASE = "https://api.github.com"
API_VERSION = "2022-11-28"


def fetch_token(token: str, scope: str, owner: str, repo: str | None) -> tuple[str, str]:
    """Return (registration_token, runner_url)."""
    if scope == "org":
        url = f"{API_BASE}/orgs/{owner}/actions/runners/registration-token"
        runner_url = f"https://github.com/{owner}"
    else:
        if not repo:
            raise ValueError("scope=repo requires a repository name")
        url = f"{API_BASE}/repos/{owner}/{repo}/actions/runners/registration-token"
        runner_url = f"https://github.com/{owner}/{repo}"

    req = urllib.request.Request(
        url,
        data=b"{}",
        method="POST",
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": API_VERSION,
            "User-Agent": "infra-runner-bootstrap",
        },
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        payload = json.load(resp)
    return payload["token"], runner_url


def write_env(path: str, values: dict[str, str]) -> None:
    """Write KEY=value lines with mode 0600 (created or truncated)."""
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        for key, value in values.items():
            f.write(f"{key}={value}\n")
    os.chmod(path, 0o600)


def admin_permissions(scope: str) -> dict[str, str]:
    """The narrowest App permission that can mint a registration token."""
    if scope == "org":
        return {"organization_self_hosted_runners": "write"}
    return {"administration": "write"}


def repo_for_instance(instance: str) -> str:
    """scope=repo instance names look like '<repo>-<n>'; derive the repo."""
    if "-" not in instance:
        raise ValueError(f"cannot derive repo from instance name {instance!r}")
    return instance.rsplit("-", 1)[0]


def load_env_file(path: str) -> None:
    """Load KEY=value lines into os.environ without overriding existing vars."""
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            os.environ.setdefault(key.strip(), value.strip())


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--instance", required=True)
    parser.add_argument("--out", required=True)
    parser.add_argument("--env-file", default=None, help="KEY=value file to load first (systemd ExecStartPre has no env)")
    args = parser.parse_args()

    if args.env_file:
        load_env_file(args.env_file)

    required = ("GITHUB_APP_ID", "GITHUB_APP_INSTALLATION_ID", "GITHUB_APP_PEM_PATH", "GITHUB_OWNER")
    missing = [name for name in required if not os.environ.get(name)]
    if missing:
        print(f"missing required env: {', '.join(missing)}", file=sys.stderr)
        return 2
    owner = os.environ["GITHUB_OWNER"]
    scope = os.environ.get("GITHUB_RUNNER_SCOPE", "repo")
    labels = os.environ.get("GITHUB_RUNNER_LABELS", "self-hosted,linux,x64")

    repo = None
    if scope == "repo":
        repo = repo_for_instance(args.instance)

    # Hop 1: a one-hour installation token that can do nothing but runner
    # administration, on this repo only. Hop 2: the registration token.
    app_token = github_app_token.mint(
        os.environ["GITHUB_APP_ID"],
        os.environ["GITHUB_APP_INSTALLATION_ID"],
        Path(os.environ["GITHUB_APP_PEM_PATH"]).read_text(encoding="utf-8"),
        repositories=[repo] if repo else None,
        permissions=admin_permissions(scope),
    )["token"]
    reg_token, runner_url = fetch_token(app_token, scope, owner, repo)
    write_env(
        args.out,
        {
            "RUNNER_TOKEN": reg_token,
            "RUNNER_URL": runner_url,
            "RUNNER_NAME": f"veggies-{args.instance}",
            "RUNNER_LABELS": labels,
        },
    )
    print(f"wrote {args.out} (token not shown)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
