#!/usr/bin/env python3
"""Mint short-lived GitHub App installation tokens (ADR 0063).

The one implementation behind every GitHub credential this repo uses:

- the operator workstation (`cli/veggies.py`: clone/pull header, and the
  OpenTofu github provider's `app_auth` via the vault-fed TF_VAR_github_app_*),
- the VPS runner fetcher (`ansible/roles/github_runner/files/fetch_runner_token.py`
  imports this module from a sibling copy - the role's `files/` entry is a git
  symlink to this file),
- the in-pod `github-auth` sidecar (shipped as a stack-config file).

Flow: sign a 9-minute RS256 JWT with the App's private key, then
`POST /app/installations/{id}/access_tokens` for a token that lives one hour
and can be narrowed to a repository list and a permission subset. Requested
permissions must be a subset of what the App holds - GitHub answers 422 and
names the offender, which `GitHubAppError` surfaces.

Dependencies: PyJWT + cryptography (Fedora `python3-jwt` / `python3-cryptography`;
`PyJWT==2.10.1` in requirements-dev.txt; pip-pinned in the sidecar image).
Everything else is stdlib. `urlopen` is injectable for tests; the module
never logs or embeds a token or the JWT in an error message.

`repositories` takes repository NAMES (not owner/name) - the installation
already fixes the owner.
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone

API_BASE = "https://api.github.com"
API_VERSION = "2022-11-28"
USER_AGENT = "veggies-github-app"
# GitHub caps App JWTs at 10 minutes; leave headroom and absorb clock skew.
JWT_TTL_SECONDS = 9 * 60
CLOCK_SKEW_SECONDS = 60
# Per-request timeout. Measured 2026-09-15: the pod squid's FIRST CONNECT to
# api.github.com takes 35-40s (chained-proxy DNS stall); a 30s timeout made
# every first attempt fail while the second succeeded.
REQUEST_TIMEOUT_S = 90


class GitHubAppError(RuntimeError):
    """A GitHub API call as the App failed. Carries the HTTP status and
    GitHub's own message; never the credential."""

    def __init__(self, status: int, message: str, path: str):
        super().__init__(f"GitHub {status} on {path}: {message}")
        self.status = status
        self.message = message
        self.path = path


def app_jwt(app_id: str | int, private_key_pem: str, now: float | None = None) -> str:
    """Return a signed RS256 JWT identifying the App (`iss` = App ID)."""
    import jwt  # PyJWT - imported here so the HTTP half stays importable without it

    issued = int(time.time() if now is None else now)
    payload = {
        "iat": issued - CLOCK_SKEW_SECONDS,
        "exp": issued + JWT_TTL_SECONDS,
        "iss": str(app_id),
    }
    return jwt.encode(payload, private_key_pem, algorithm="RS256")


def _request(method: str, path: str, bearer: str, body: dict | None,
             urlopen=urllib.request.urlopen, timeout: float = REQUEST_TIMEOUT_S) -> dict:
    data = None if body is None else json.dumps(body).encode()
    req = urllib.request.Request(
        f"{API_BASE}{path}",
        data=data,
        method=method,
        headers={
            "Authorization": f"Bearer {bearer}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": API_VERSION,
            "User-Agent": USER_AGENT,
            **({"Content-Type": "application/json"} if data is not None else {}),
        },
    )
    try:
        with urlopen(req, timeout=timeout) as resp:
            return json.load(resp)
    except urllib.error.HTTPError as exc:
        try:
            message = json.loads(exc.read().decode() or "{}").get("message", "")
        except (ValueError, AttributeError):
            message = ""
        raise GitHubAppError(exc.code, message or exc.reason or "", path) from None


def app_get(bearer: str, path: str, urlopen=urllib.request.urlopen) -> dict:
    """GET with a bearer: the App JWT for `/app*` (e.g. `/app` for the slug),
    an installation token for everything else - a JWT on `/users/...` is
    401 "Bad credentials" (verified 2026-09-15)."""
    return _request("GET", path, bearer, None, urlopen=urlopen)


def installation_token(token_jwt: str, installation_id: str | int, *,
                       repositories: list[str] | None = None,
                       permissions: dict[str, str] | None = None,
                       urlopen=urllib.request.urlopen) -> dict:
    """Exchange the App JWT for an installation token.

    Returns {"token": str, "expires_at": ISO-8601 str}. Omitting
    `repositories`/`permissions` yields the installation's full grant.
    """
    body: dict = {}
    if repositories:
        body["repositories"] = list(repositories)
    if permissions:
        body["permissions"] = dict(permissions)
    payload = _request("POST", f"/app/installations/{installation_id}/access_tokens",
                       token_jwt, body, urlopen=urlopen)
    return {"token": payload["token"], "expires_at": payload["expires_at"]}


def mint(app_id: str | int, installation_id: str | int, private_key_pem: str, *,
         repositories: list[str] | None = None,
         permissions: dict[str, str] | None = None,
         urlopen=urllib.request.urlopen) -> dict:
    """JWT + exchange in one call; the shape every caller wants."""
    return installation_token(app_jwt(app_id, private_key_pem), installation_id,
                              repositories=repositories, permissions=permissions,
                              urlopen=urlopen)


def expires_at_epoch(expires_at: str) -> float:
    """GitHub's `expires_at` ("2026-09-15T12:00:00Z") as a POSIX timestamp."""
    return datetime.fromisoformat(expires_at.replace("Z", "+00:00")).astimezone(timezone.utc).timestamp()
