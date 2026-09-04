import importlib.util
import os
import time
from pathlib import Path

_SCRIPT = Path(__file__).parent.parent / "ansible/roles/backup/files/cleanup.py"
_spec = importlib.util.spec_from_file_location("cleanup", _SCRIPT)
cleanup = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(cleanup)


def test_stale_dirs_selects_only_old(tmp_path):
    fresh = tmp_path / "new-1" / "_work"
    stale = tmp_path / "old-1" / "_work"
    other = tmp_path / "old-1" / "keepme"
    for d in (fresh, stale, other):
        d.mkdir(parents=True)
    now = time.time()
    os.utime(stale, (now - 3 * 86400, now - 3 * 86400))
    os.utime(other, (now - 3 * 86400, now - 3 * 86400))
    os.utime(fresh, (now, now))
    result = cleanup.stale_dirs(str(tmp_path), days=2, now=now)
    assert result == [str(stale)]


def test_stale_dirs_missing_root(tmp_path):
    assert cleanup.stale_dirs(str(tmp_path / "nope"), days=2) == []


def test_dir_size_mb(tmp_path):
    f = tmp_path / "blob"
    f.write_bytes(b"x" * 1024 * 1024 * 3)
    assert cleanup.dir_size_mb(str(tmp_path)) == 3


def test_disk_percent_sane():
    assert 0 < cleanup.disk_percent("/") < 100


def test_user_exists():
    assert cleanup.user_exists("root")
    assert not cleanup.user_exists("definitely-not-a-user-xyz")
