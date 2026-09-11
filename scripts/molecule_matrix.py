#!/usr/bin/env python3
"""Print the molecule scenario matrix for a CI event as a JSON list.

Usage: molecule_matrix.py --event-name pull_request --files-file files.txt
("--files-file -" reads one changed path per line from stdin). Any event
other than pull_request prints the full matrix; a pull_request event prints
only the scenarios the changed files can affect.

The dependency map covers converge-time role application ONLY (parsed from
the roles: lists in scenario converge.yml files). It structurally cannot see
other couplings - e.g. the egress/github_runner scenarios' pre_tasks
hand-replicate the base role's user/linger contract - so a base change must
be reasoned about by a human, not by this map. The nightly full-matrix run
is the designated backstop for everything the map cannot see.

Fail-open rule: anything unrecognized maps to the FULL matrix, never to
fewer scenarios.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Iterable

import yaml

SHARED_INPUTS = (
    "ansible/molecule/",               # shared fedora44-systemd Containerfile
    "ansible/requirements.yml",        # collections, symlinked into every scenario as collections.yml
    "requirements-dev.txt",            # molecule + plugin toolchain pins
    ".github/workflows/infra-ci.yml",  # the harness itself - workflow changes prove the full matrix
    "ansible.cfg",                     # repo-root ansible config (conservative)
)

_SHARED_PREFIXES = tuple(p for p in SHARED_INPUTS if p.endswith("/"))


def role_dirs(root: Path) -> list[str]:
    """Sorted names of the directories directly under ansible/roles/."""
    return sorted(
        p.name for p in (root / "ansible/roles").iterdir() if p.is_dir()
    )


def scenario_deps(root: Path) -> dict[str, set[str]]:
    """Role names each scenario's converge.yml applies, plus the role itself."""
    deps: dict[str, set[str]] = {}
    for role in role_dirs(root):
        seen = {role}
        for converge in sorted(
            (root / "ansible/roles" / role / "molecule").glob("*/converge.yml")
        ):
            for play in yaml.safe_load(converge.read_text()) or []:
                for entry in play.get("roles") or []:
                    if isinstance(entry, str):
                        seen.add(entry)
                    elif isinstance(entry, dict):
                        name = entry.get("role") or entry.get("name")
                        if name:
                            seen.add(name)
        deps[role] = seen
    return deps


def select_roles(
    changed_files: Iterable[str], roles: list[str], deps: dict[str, set[str]]
) -> list[str]:
    """Scenario names a changed-file list can affect; fail open to all of them."""
    affected: set[str] = set()
    for path in changed_files:
        if path.startswith('"'):  # git C-quoted unusual filename
            return sorted(roles)
        if path in SHARED_INPUTS or path.startswith(_SHARED_PREFIXES):
            return sorted(roles)
        if path.startswith("ansible/roles/"):
            parts = path.split("/")
            if len(parts) < 3:
                return sorted(roles)
            role = parts[2]
            if role not in roles:  # renames/deletions stay safe
                return sorted(roles)
            affected.update(s for s in roles if role in deps.get(s, {s}))
        elif path == "ansible" or path.startswith("ansible/"):
            # Boring-conservative: only paths molecule provably ignores are
            # excluded; anything else under ansible/ gets the full matrix.
            return sorted(roles)
        # Anything else (cli/, docs/, terraform/, ...) affects no scenario.
    return sorted(affected)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--event-name", required=True)
    parser.add_argument(
        "--files-file",
        help="one changed path per line; - reads stdin "
        "(required for pull_request events)",
    )
    args = parser.parse_args()

    root = Path(__file__).resolve().parents[1]
    roles = role_dirs(root)
    if args.event_name != "pull_request":
        # push-to-main, the nightly schedule, and any future event such as
        # merge_group always get the full matrix; the files are ignored.
        print(json.dumps(roles))
        return 0
    if not args.files_file:
        parser.error("--files-file is required for pull_request events")
    if args.files_file == "-":
        lines = sys.stdin.read().splitlines()
    else:
        lines = Path(args.files_file).read_text().splitlines()
    selected = select_roles(lines, roles, scenario_deps(root))
    print(json.dumps(sorted(selected)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
