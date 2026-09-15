import importlib.util
import json
from pathlib import Path
from unittest import mock

import pytest

# ansible-core already owns the `ansible` python package, so the role path
# cannot be imported as a module tree - load the script by file location.
_SCRIPT = Path(__file__).parent.parent / "ansible/roles/github_runner/files/fetch_runner_token.py"
_spec = importlib.util.spec_from_file_location("fetch_runner_token", _SCRIPT)
frt = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(frt)


def _mock_response(payload):
    m = mock.MagicMock()
    m.read.return_value = json.dumps(payload).encode()
    m.__enter__.return_value = m
    return m


def test_fetch_token_repo_scope_url():
    with mock.patch.object(frt.urllib.request, "urlopen", return_value=_mock_response({"token": "T"})) as u:
        token, runner_url = frt.fetch_token("PAT", "repo", "myorg", "veggie")
    req = u.call_args[0][0]
    assert req.full_url == "https://api.github.com/repos/myorg/veggie/actions/runners/registration-token"
    assert req.method == "POST"
    assert req.headers["Authorization"] == "Bearer PAT"
    assert token == "T"
    assert runner_url == "https://github.com/myorg/veggie"


def test_fetch_token_org_scope_url():
    with mock.patch.object(frt.urllib.request, "urlopen", return_value=_mock_response({"token": "T"})) as u:
        _, runner_url = frt.fetch_token("PAT", "org", "myorg", None)
    req = u.call_args[0][0]
    assert req.full_url == "https://api.github.com/orgs/myorg/actions/runners/registration-token"
    assert runner_url == "https://github.com/myorg"


def test_fetch_token_repo_scope_requires_repo():
    with pytest.raises(ValueError, match="requires a repository"):
        frt.fetch_token("PAT", "repo", "myorg", None)


def test_repo_for_instance():
    assert frt.repo_for_instance("veggie-1") == "veggie"
    assert frt.repo_for_instance("data-pipelines-2") == "data-pipelines"
    with pytest.raises(ValueError):
        frt.repo_for_instance("nodash")


def test_write_env_permissions(tmp_path):
    out = tmp_path / "1.env"
    frt.write_env(str(out), {"RUNNER_TOKEN": "T", "RUNNER_NAME": "veggies-1"})
    assert (out.stat().st_mode & 0o777) == 0o600
    assert out.read_text() == "RUNNER_TOKEN=T\nRUNNER_NAME=veggies-1\n"


def test_load_env_file(tmp_path, monkeypatch):
    env_file = tmp_path / "api.env"
    env_file.write_text("# comment\n\nGITHUB_APP_ID=123\nGITHUB_OWNER=me\nBAD LINE\n")
    monkeypatch.setenv("GITHUB_OWNER", "preexisting")
    frt.load_env_file(str(env_file))
    import os

    assert os.environ["GITHUB_APP_ID"] == "123"
    assert os.environ["GITHUB_OWNER"] == "preexisting"  # never overrides
    assert "BAD LINE" not in os.environ


def test_admin_permissions_are_the_narrowest_per_scope():
    assert frt.admin_permissions("repo") == {"administration": "write"}
    assert frt.admin_permissions("org") == {"organization_self_hosted_runners": "write"}


def test_main_mints_a_narrowed_app_token_then_registers(tmp_path, monkeypatch):
    # ADR 0063: hop 1 = installation token scoped to this repo + runner
    # administration only (never the App's full grant); hop 2 = the
    # registration token with that bearer. The PEM comes from a file, never
    # from the KEY=value env file.
    pem = tmp_path / "app.pem"
    pem.write_text("-----BEGIN PRIVATE KEY-----\nX\n-----END PRIVATE KEY-----\n")
    env_file = tmp_path / "api.env"
    env_file.write_text(
        f"GITHUB_APP_ID=4945586\nGITHUB_APP_INSTALLATION_ID=77\nGITHUB_APP_PEM_PATH={pem}\n"
        "GITHUB_OWNER=Innoptech\nGITHUB_RUNNER_SCOPE=repo\nGITHUB_RUNNER_LABELS=a,b\n"
    )
    for name in ("GITHUB_APP_ID", "GITHUB_APP_INSTALLATION_ID", "GITHUB_APP_PEM_PATH",
                 "GITHUB_OWNER", "GITHUB_RUNNER_SCOPE", "GITHUB_RUNNER_LABELS"):
        monkeypatch.delenv(name, raising=False)
    out = tmp_path / "veggies-1.env"
    mint = mock.Mock(return_value={"token": "ghs_app", "expires_at": "2026-01-01T00:00:00Z"})
    monkeypatch.setattr(frt.github_app_token, "mint", mint)
    monkeypatch.setattr("sys.argv", ["fetch_runner_token", "--instance", "veggies-1",
                                     "--out", str(out), "--env-file", str(env_file)])
    with mock.patch.object(frt.urllib.request, "urlopen", return_value=_mock_response({"token": "REG"})) as u:
        assert frt.main() == 0
    assert mint.call_args.args == ("4945586", "77", pem.read_text())
    assert mint.call_args.kwargs == {"repositories": ["veggies"], "permissions": {"administration": "write"}}
    req = u.call_args[0][0]
    assert req.headers["Authorization"] == "Bearer ghs_app"
    assert req.full_url == "https://api.github.com/repos/Innoptech/veggies/actions/runners/registration-token"
    assert "RUNNER_TOKEN=REG\n" in out.read_text() and "RUNNER_LABELS=a,b\n" in out.read_text()


def test_main_names_missing_env(tmp_path, monkeypatch, capsys):
    for name in ("GITHUB_APP_ID", "GITHUB_APP_INSTALLATION_ID", "GITHUB_APP_PEM_PATH", "GITHUB_OWNER"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr("sys.argv", ["fetch_runner_token", "--instance", "x-1", "--out", str(tmp_path / "o")])
    assert frt.main() == 2
    assert "GITHUB_APP_PEM_PATH" in capsys.readouterr().err
