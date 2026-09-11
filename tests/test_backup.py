"""Drift guard: the spend log's host directory stays inside the backup
role's backup_paths (ADR 0051/0052/0053, issue #80).

spend.jsonl* is durable paid history. On remote stacks it lives at
REMOTE_STATE_ROOT/<stack>/; the backup role's backup_paths decides what
restic covers once the ADR 0024 gate un-gates (procedure: ADR 0053). If
the two sides drift apart, the spend log silently leaves the backup set.
"""

import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT / "cli"))
import capabilities  # noqa: E402

STATE_DIR = "/home/stacks/.local/state/veggies"


def _backup_paths() -> list:
    defaults = yaml.safe_load(
        (ROOT / "ansible/roles/backup/defaults/main.yml").read_text()
    )
    return defaults["backup_paths"]


def test_state_dir_inside_backup_paths():
    assert STATE_DIR in _backup_paths()


def test_remote_state_root_matches_backup_path():
    # The CLI and the role must name the same directory.
    assert capabilities.REMOTE_STATE_ROOT == STATE_DIR
    assert capabilities.REMOTE_STATE_ROOT in _backup_paths()
