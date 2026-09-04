#!/usr/bin/env python3
"""Print a single key from a vault-encrypted YAML file.

Used by CI to extract one credential without dumping the whole file.
Usage: vault_get.py secrets/infra.yml tailscale_oauth_secret
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

import yaml


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("file")
    parser.add_argument("key")
    parser.add_argument("--password-file", default="~/.config/infra/vault-password")
    args = parser.parse_args()

    out = subprocess.run(
        [
            "ansible-vault",
            "view",
            "--vault-password-file",
            str(Path(args.password_file).expanduser()),
            args.file,
        ],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    data = yaml.safe_load(out)
    if args.key not in data:
        print(f"key {args.key!r} not in {args.file}", file=sys.stderr)
        return 2
    print(data[args.key])
    return 0


if __name__ == "__main__":
    sys.exit(main())
