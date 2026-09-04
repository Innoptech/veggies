import importlib.util
import subprocess
from pathlib import Path

_SCRIPT = Path(__file__).parent.parent / "scripts/vault_get.py"


def _run(*args):
    return subprocess.run(
        [str(_SCRIPT), *args],
        capture_output=True,
        text=True,
        executable=None,
    )


def test_missing_key_errors(tmp_path):
    plain = tmp_path / "plain.yml"
    plain.write_text("a: 1\n")
    # Not a vault file: ansible-vault errors -> non-zero, message on stderr.
    result = subprocess.run(
        ["./.venv/bin/python", str(_SCRIPT), str(plain), "a"],
        capture_output=True,
        text=True,
        cwd=Path(__file__).parent.parent,
    )
    assert result.returncode != 0
    assert "vault" in result.stderr.lower() or "Error" in result.stderr


def test_module_loads():
    spec = importlib.util.spec_from_file_location("vault_get", _SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert callable(module.main)
