"""deploy/github-auth/daemon.py - the in-pod token rotator (ADR 0063)."""
import importlib.util
import json
import sys
from pathlib import Path
from unittest import mock

import pytest

ROOT = Path(__file__).parent.parent


@pytest.fixture()
def daemon(tmp_path, monkeypatch):
    # The daemon imports the minter from /stack-config; point it at scripts/.
    monkeypatch.setenv("STACK_CONFIG_DIR", str(ROOT / "scripts"))
    monkeypatch.setenv("GITHUB_AUTH_DIR", str(tmp_path / "auth"))
    sys.modules.pop("github_app_token", None)
    spec = importlib.util.spec_from_file_location(
        "github_auth_daemon", ROOT / "deploy/github-auth/daemon.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    mod.AUTH_DIR.mkdir(parents=True)
    mod.HEARTBEAT = tmp_path / "heartbeat"
    return mod


def test_identity_gitconfig_is_the_bot_noreply_identity(daemon):
    text = daemon.identity_gitconfig("veggies-harness[bot]", 329273836)
    assert "name = veggies-harness[bot]" in text
    assert "email = 329273836+veggies-harness[bot]@users.noreply.github.com" in text


def test_discover_identity_uses_app_then_bot_user(daemon):
    calls = []

    def get(jwt, path):
        calls.append((jwt, path))
        return {"slug": "veggies-harness"} if path == "/app" else {"id": 329273836}

    assert daemon.discover_identity("JWT", get) == ("veggies-harness[bot]", 329273836)
    # brackets are percent-encoded in the path; both calls ride the App JWT
    assert calls == [("JWT", "/app"), ("JWT", "/users/veggies-harness%5Bbot%5D")]


def test_needs_refresh_window(daemon):
    assert daemon.needs_refresh(None, 1000.0)
    assert daemon.needs_refresh(1000.0 + 1199, 1000.0, before=1200)
    assert not daemon.needs_refresh(1000.0 + 1201, 1000.0, before=1200)


def test_repo_names_takes_the_name_half(daemon):
    assert daemon.repo_names("Innoptech/veggies") == ["veggies"]
    assert daemon.repo_names("") is None


def test_tick_mints_scoped_token_and_writes_files_atomically(daemon):
    mint = mock.Mock(return_value={"token": "ghs_1", "expires_at": "1970-01-01T01:00:00Z"})
    state = {"login": "veggies-harness[bot]", "id": 1}
    state = daemon.tick(state, now=0.0, mint=mint, github_repo="Innoptech/veggies",
                        permissions={"contents": "write"}, log=lambda m: None)
    assert mint.call_args.kwargs == {"repositories": ["veggies"],
                                     "permissions": {"contents": "write"}}
    token = daemon.AUTH_DIR / "token"
    assert token.read_text() == "ghs_1"
    assert (token.stat().st_mode & 0o777) == 0o644  # the agent must read it
    assert not (daemon.AUTH_DIR / "token.tmp").exists()  # os.replace, no torn file
    assert state["expires_at_epoch"] == 3600.0
    status = json.loads((daemon.AUTH_DIR / "status.json").read_text())
    assert status["login"] == "veggies-harness[bot]" and status["repo"] == "Innoptech/veggies"
    assert daemon.HEARTBEAT.exists()


def test_tick_skips_when_fresh(daemon):
    mint = mock.Mock()
    state = daemon.tick({"expires_at_epoch": 10_000.0}, now=0.0, mint=mint,
                        github_repo="", permissions={}, log=lambda m: None)
    mint.assert_not_called()
    assert state == {"expires_at_epoch": 10_000.0}


def test_tick_keeps_old_token_when_mint_fails(daemon):
    (daemon.AUTH_DIR / "token").write_text("ghs_old")
    logs = []
    mint = mock.Mock(side_effect=RuntimeError("GitHub 422 on /app/...: not granted"))
    state = daemon.tick({"expires_at_epoch": 100.0}, now=0.0, mint=mint,
                        github_repo="", permissions={}, log=logs.append)
    assert (daemon.AUTH_DIR / "token").read_text() == "ghs_old"
    assert state == {"expires_at_epoch": 100.0}
    assert logs and "keeping the current token" in logs[0]


def test_installation_wide_when_no_repo(daemon):
    mint = mock.Mock(return_value={"token": "t", "expires_at": "1970-01-01T01:00:00Z"})
    daemon.tick({}, now=0.0, mint=mint, github_repo="", permissions={"contents": "read"},
                log=lambda m: None)
    assert mint.call_args.kwargs["repositories"] is None
