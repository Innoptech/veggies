#!/usr/bin/env python3
"""Print `export TF_VAR_*` shell lines from ansible-vault encrypted files.

Plaintext only ever exists in this process's memory and the eval'd environment
of the calling shell - never on disk. Used by `mask tofu-plan` / `tofu-apply`.

Usage:
    eval "$(python scripts/tfvars_from_vault.py secrets/github.yml ...)"

Missing files are skipped with a warning on stderr (they may not exist yet
in early phases). Every top-level scalar becomes TF_VAR_<key>; nested
structures become JSON.
"""

from __future__ import annotations

import argparse
import json
import shlex
import subprocess
import sys
from pathlib import Path

import yaml

DEFAULT_PASSWORD_FILE = "~/.config/infra/vault-password"


def read_vault(path: Path, password_file: str | None = None) -> str:
    """Decrypt a vault file to a string using the ansible-vault CLI."""
    cmd = ["ansible-vault", "view", str(path)]
    pw = password_file or DEFAULT_PASSWORD_FILE
    if Path(pw).expanduser().is_file():
        cmd += ["--vault-password-file", str(Path(pw).expanduser())]
    result = subprocess.run(cmd, check=True, capture_output=True, text=True)
    return result.stdout


def exports(data: dict, prefix: str = "TF_VAR_") -> list[str]:
    """Build sorted `export KEY=value` lines with safe shell quoting."""
    lines = []
    for key in sorted(data):
        value = data[key]
        if isinstance(value, (dict, list)):
            value = json.dumps(value)
        lines.append(f"export {prefix}{key}={shlex.quote(str(value))}")
    return lines


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("files", nargs="+", help="vault-encrypted YAML files")
    parser.add_argument("--password-file", default=None)
    args = parser.parse_args()

    merged: dict = {}
    for name in args.files:
        path = Path(name)
        if not path.is_file():
            print(f"warning: {name} not found, skipping", file=sys.stderr)
            continue
        data = yaml.safe_load(read_vault(path, args.password_file)) or {}
        if not isinstance(data, dict):
            parser.error(f"{name}: expected a mapping at top level")
        merged.update(data)

    for line in exports(merged):
        print(line)
    return 0


if __name__ == "__main__":
    sys.exit(main())
