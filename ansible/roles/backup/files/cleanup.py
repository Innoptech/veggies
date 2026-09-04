#!/usr/bin/env python3
"""cleanup.py - daily hygiene for veggies (infra repo, backup role).

- prune each service user's container images/containers older than 7 days
- delete runner work dirs (*/ _work) older than --stale-days
- clear pip/npm caches over --cache-mb
- log (and optionally webhook POST) when disk use exceeds --disk-percent

Everything is logged to stdout -> journald. --dry-run prints without acting.
"""

from __future__ import annotations

import argparse
import json
import os
import pwd
import shutil
import subprocess
import sys
import time
import urllib.request

PRUNE_UNTIL_HOURS = 7 * 24


def disk_percent(path: str = "/") -> int:
    usage = shutil.disk_usage(path)
    return round(100 * usage.used / usage.total)


def dir_size_mb(path: str) -> int:
    total = 0
    for root, _dirs, files in os.walk(path):
        for name in files:
            try:
                total += os.path.getsize(os.path.join(root, name))
            except OSError:
                continue
    return total // (1024 * 1024)


def stale_dirs(root: str, days: int, now: float | None = None) -> list[str]:
    """Return <root>/<instance>/_work directories older than `days`."""
    now = now or time.time()
    cutoff = now - days * 86400
    stale = []
    try:
        entries = os.listdir(root)
    except OSError:
        return stale
    for entry in entries:
        work = os.path.join(root, entry, "_work")
        try:
            mtime = os.path.getmtime(work)
        except OSError:
            continue
        if mtime < cutoff:
            stale.append(work)
    return sorted(stale)


def user_exists(name: str) -> bool:
    try:
        pwd.getpwnam(name)
        return True
    except KeyError:
        return False


def run(cmd: list[str], dry_run: bool) -> None:
    print("+ " + " ".join(cmd))
    if not dry_run:
        subprocess.run(cmd, check=False)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--work-root", default="/srv/gh-runner")
    parser.add_argument("--stale-days", type=int, default=2)
    parser.add_argument("--cache-mb", type=int, default=500)
    parser.add_argument("--disk-percent", type=int, default=80)
    parser.add_argument("--users", nargs="*", default=[])
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    for user in args.users:
        if not user_exists(user):
            print(f"user {user} does not exist, skipping prune")
            continue
        run(
            ["su", "-s", "/bin/bash", user, "-c",
             f"podman system prune -af --filter until={PRUNE_UNTIL_HOURS}h"],
            args.dry_run,
        )

    for stale in stale_dirs(args.work_root, args.stale_days):
        print(f"stale work dir: {stale}")
        if not args.dry_run:
            shutil.rmtree(stale, ignore_errors=True)

    for user in args.users:
        if not user_exists(user):
            continue
        home = pwd.getpwnam(user).pw_dir
        for cache in (os.path.join(home, ".cache", "pip"), os.path.join(home, ".npm")):
            if os.path.isdir(cache) and dir_size_mb(cache) > args.cache_mb:
                print(f"cache over {args.cache_mb} MB: {cache}")
                if not args.dry_run:
                    shutil.rmtree(cache, ignore_errors=True)

    percent = disk_percent("/")
    if percent >= args.disk_percent:
        msg = f"disk usage at {percent}% (threshold {args.disk_percent}%)"
        print(f"ALERT: {msg}")
        webhook = os.environ.get("CLEANUP_WEBHOOK_URL", "")
        if webhook and not args.dry_run:
            req = urllib.request.Request(
                webhook,
                data=json.dumps({"text": f"veggies: {msg}"}).encode(),
                headers={"Content-Type": "application/json"},
            )
            try:
                urllib.request.urlopen(req, timeout=10)
            except OSError as exc:
                print(f"webhook POST failed: {exc}")
    else:
        print(f"disk usage {percent}% (ok)")

    return 0


if __name__ == "__main__":
    sys.exit(main())
