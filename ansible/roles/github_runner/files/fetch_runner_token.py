#!/usr/bin/env python3
"""Fetch a short-lived GitHub Actions runner registration token.

Called by systemd ExecStartPre before every runner (re)start, so no long-lived
runner token is ever stored. Reads configuration from the environment
(written by ansible into api.env, mode 0600, from the vault):

  GH_RUNNER_ADMIN_TOKEN  PAT/Fine-grained token with runner administration
                         rights (repo Administration:write, or org admin).
  GITHUB_OWNER           org or user name.
  GITHUB_RUNNER_SCOPE    "repo" (default) or "org".
  GITHUB_RUNNER_LABELS   comma-separated extra labels.

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

    token = os.environ.get("GH_RUNNER_ADMIN_TOKEN", "")
    owner = os.environ.get("GITHUB_OWNER", "")
    scope = os.environ.get("GITHUB_RUNNER_SCOPE", "repo")
    labels = os.environ.get("GITHUB_RUNNER_LABELS", "self-hosted,linux,x64")
    if not token or not owner:
        print("GH_RUNNER_ADMIN_TOKEN and GITHUB_OWNER are required", file=sys.stderr)
        return 2

    repo = None
    if scope == "repo":
        repo = repo_for_instance(args.instance)

    reg_token, runner_url = fetch_token(token, scope, owner, repo)
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
