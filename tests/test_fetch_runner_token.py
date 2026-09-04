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
    env_file.write_text("# comment\n\nGH_RUNNER_ADMIN_TOKEN=tok\nGITHUB_OWNER=me\nBAD LINE\n")
    monkeypatch.setenv("GITHUB_OWNER", "preexisting")
    frt.load_env_file(str(env_file))
    import os

    assert os.environ["GH_RUNNER_ADMIN_TOKEN"] == "tok"
    assert os.environ["GITHUB_OWNER"] == "preexisting"  # never overrides
    assert "BAD LINE" not in os.environ
