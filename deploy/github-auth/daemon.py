#!/usr/bin/env python3
"""github-auth sidecar (ADR 0063): keep a fresh GitHub App installation
token on a shared emptyDir for the harness.

Every pass: beat the heartbeat, and when no token exists or the current one
expires within REFRESH_BEFORE_S, mint a new one scoped to GITHUB_REPO with
the permission set in GITHUB_TOKEN_PERMISSIONS and write it atomically to
<GITHUB_AUTH_DIR>/token (0644 - the agent must read it; the boundary is the
App's permission set plus the repository scope, not secrecy from the
agent). On start, discover the App's bot identity (`GET /app` with the JWT,
then the bot user's id) and write identity.gitconfig for git's
include.path, plus status.json for `veggies status`.

A mint failure logs and keeps the previous file: the old token stays valid
until its own expiry, so a transient GitHub or proxy hiccup never yanks a
credential mid-session. The private key only ever exists in this
container's env.

Stdlib + the minter shipped alongside in /stack-config; IO is injected so
tests/test_github_auth_daemon.py needs no network.
"""

from __future__ import annotations

import json
import os
import sys
import time
import urllib.parse
from pathlib import Path

sys.path.insert(0, os.environ.get("STACK_CONFIG_DIR", "/stack-config"))
import github_app_token  # noqa: E402 - the one minter, shipped alongside

AUTH_DIR = Path(os.environ.get("GITHUB_AUTH_DIR", "/github-auth"))
HEARTBEAT = Path("/tmp/github-auth.heartbeat")
REFRESH_BEFORE_S = int(os.environ.get("REFRESH_BEFORE_S", "1200"))
POLL_S = int(os.environ.get("POLL_S", "60"))


def log(msg: str) -> None:
    print(f"github-auth: {msg}", flush=True)


def write_atomic(path: Path, text: str, mode: int = 0o644) -> None:
    """tmp + os.replace: readers never see a torn file."""
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text)
    os.chmod(tmp, mode)
    os.replace(tmp, path)


def identity_gitconfig(login: str, user_id: int) -> str:
    """git config fragment that authors commits as the App's bot user."""
    return (f"[user]\n\tname = {login}\n"
            f"\temail = {user_id}+{login}@users.noreply.github.com\n")


def discover_identity(app_jwt: str, installation_token: str, get) -> tuple[str, int]:
    """(login, id) of the App's bot user. The App JWT is valid on /app only
    (the slug); the bot user's id comes from /users/<slug>[bot] with the
    installation token - a JWT there is 401 (verified 2026-09-15), and
    installation tokens cannot call /user at all."""
    slug = get(app_jwt, "/app")["slug"]
    login = f"{slug}[bot]"
    user = get(installation_token, "/users/" + urllib.parse.quote(login, safe=""))
    return login, int(user["id"])


def needs_refresh(expires_at_epoch: float | None, now: float,
                  before: int = REFRESH_BEFORE_S) -> bool:
    return expires_at_epoch is None or expires_at_epoch - now < before


def repo_names(github_repo: str) -> list[str] | None:
    """`owner/name` -> ["name"] (the API takes repository NAMES); "" -> None."""
    if not github_repo:
        return None
    return [github_repo.rsplit("/", 1)[-1]]


def ensure_identity(state: dict, discover, log=log) -> dict:
    """Discover and publish the bot identity once; retried every pass until
    it lands (seen live 2026-09-15: a startup-only attempt timed out on the
    pod squid's first-connect stall and commits fell back to git defaults
    for the pod's whole life). Needs a minted token in state."""
    if state.get("login") or not state.get("token"):
        return state
    try:
        login, user_id = discover(state["token"])
    except Exception as exc:  # noqa: BLE001 - cosmetic until it lands; tokens are not
        log(f"identity discovery failed, will retry: {exc}")
        return state
    write_atomic(AUTH_DIR / "identity.gitconfig", identity_gitconfig(login, user_id))
    state.update(login=login, id=user_id)
    write_status(state)  # `veggies status` shows the login once known
    log(f"identity: {login} {user_id}")
    return state


def write_status(state: dict) -> None:
    write_atomic(AUTH_DIR / "status.json", json.dumps({
        "login": state.get("login"), "id": state.get("id"),
        "repo": state.get("repo"), "expires_at": state.get("expires_at"),
        "expires_at_epoch": state.get("expires_at_epoch")}))


def tick(state: dict, now: float, mint, *, github_repo: str,
         permissions: dict, log=log, discover=None) -> dict:
    """One pass: publish the identity if still missing, refresh the token
    when due. `state` carries expires_at_epoch (+ login/id once known);
    `mint()` returns {"token", "expires_at"}."""
    HEARTBEAT.touch()
    if needs_refresh(state.get("expires_at_epoch"), now):
        state = _refresh(state, mint, github_repo=github_repo,
                         permissions=permissions, log=log)
    if discover is not None:
        state = ensure_identity(state, discover, log=log)
    return state


def _refresh(state: dict, mint, *, github_repo: str, permissions: dict, log) -> dict:
    try:
        minted = mint(repositories=repo_names(github_repo), permissions=permissions)
    except Exception as exc:  # noqa: BLE001 - keep the old token, say why
        log(f"mint failed, keeping the current token: {exc}")
        return state
    write_atomic(AUTH_DIR / "token", minted["token"])
    state["token"] = minted["token"]  # in-memory only: identity lookups reuse it
    state["repo"] = github_repo
    state["expires_at"] = minted["expires_at"]
    state["expires_at_epoch"] = github_app_token.expires_at_epoch(minted["expires_at"])
    write_status(state)
    log(f"minted, expires_at={minted['expires_at']} repo={github_repo or '<installation>'}")
    return state


def main() -> int:
    app_id = os.environ["GITHUB_APP_ID"]
    installation_id = os.environ["GITHUB_APP_INSTALLATION_ID"]
    pem = os.environ["GITHUB_APP_PRIVATE_KEY"]
    github_repo = os.environ.get("GITHUB_REPO", "")
    permissions = json.loads(os.environ.get("GITHUB_TOKEN_PERMISSIONS") or "{}")
    AUTH_DIR.mkdir(parents=True, exist_ok=True)
    HEARTBEAT.touch()  # first beat before any IO: wait_healthy is not held

    state: dict = {}

    def mint(**kw):
        return github_app_token.mint(app_id, installation_id, pem, **kw)

    def discover(installation_token):
        return discover_identity(github_app_token.app_jwt(app_id, pem),
                                 installation_token, github_app_token.app_get)

    while True:
        state = tick(state, time.time(), mint, github_repo=github_repo,
                     permissions=permissions, discover=discover)
        time.sleep(POLL_S)


if __name__ == "__main__":
    sys.exit(main())
