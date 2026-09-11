"""No-ask enforcement over the merged project+global permission config
(ADR 0044, amends 0031's agent-config-only scope).

opencode merges config tiers global < project (project wins), so a mounted
repo's own opencode.json[c] / agent frontmatter can reintroduce `ask` - the
value ADR 0031 bans because it parks unattended sessions forever. These
pure, stdlib-only scanners are the shared enforcement: pytest covers them
over fixtures and this repo (tests/test_veggies.py::test_no_ask_anywhere),
and scripts/stack_kick.py refuses to kick a repo whose checked-out project
tier carries `ask`. Stdlib-only because the kick script runs on a bare
GitHub runner (no PyYAML).
"""

from __future__ import annotations

import json
import re
from pathlib import Path

# opencode 1.18.27 project tier (config.ts + config/paths.ts, verified
# 2026-09-11): opencode.json[c] at the root and under .opencode/, walked up
# from the workspace to the worktree root - a kicked session's workspace IS
# the repo root, so repo-root paths suffice. Agents/modes load from
# {agent,agents,mode,modes}/**/*.md under each config dir; .claude/ and
# .agents/ are the compat paths ADR 0019 verified live (over-scanned
# deliberately: vacuous when absent, a permission carrier when not).
PROJECT_CONFIG_NAMES = ("opencode.json", "opencode.jsonc")
PROJECT_CONFIG_DIRS = (".opencode",)
PROJECT_AGENT_BASES = (".opencode", ".claude", ".agents")
_AGENT_SUBDIRS = ("agent", "agents", "mode", "modes")


def project_tier_files(repo: Path) -> list[Path]:
    """Every project-tier opencode config/agent file present in `repo`."""
    repo = Path(repo)
    out: list[Path] = []
    for name in PROJECT_CONFIG_NAMES:
        p = repo / name
        if p.is_file():
            out.append(p)
    for d in PROJECT_CONFIG_DIRS:
        for name in PROJECT_CONFIG_NAMES:
            p = repo / d / name
            if p.is_file():
                out.append(p)
    for base in PROJECT_AGENT_BASES:
        for sub in _AGENT_SUBDIRS:
            root = repo / base / sub
            if root.is_dir():
                out.extend(sorted(root.rglob("*.md")))
    return out


def _ask_paths(node, prefix):
    """Dotted paths under `node` whose scalar value is exactly "ask"."""
    if isinstance(node, dict):
        for k, v in node.items():
            yield from _ask_paths(v, f"{prefix}.{k}")
    elif isinstance(node, list):
        for i, v in enumerate(node):
            yield from _ask_paths(v, f"{prefix}[{i}]")
    elif node == "ask":
        yield prefix


def ask_violations_in_config(cfg: dict, origin: str) -> list[str]:
    """`ask` wherever it is a permission decision in a parsed opencode
    config: the top-level `permission` tree AND each inline agent's/mode's
    own `permission` block (both merge over the global envelope)."""
    out = [f"{origin}: {p}"
           for p in _ask_paths(cfg.get("permission") or {}, "permission")]
    for section in ("agent", "mode"):
        subs = cfg.get(section)
        if not isinstance(subs, dict):
            continue  # wrong-shaped section carries no permission tree
        for name, sub in subs.items():
            if isinstance(sub, dict):
                out += [f"{origin}: {p}" for p in _ask_paths(
                    sub.get("permission") or {},
                    f"{section}.{name}.permission")]
    return out


# `ask` in value position: after a key colon / flow opener, optionally
# quoted, closed by end-of-line, a flow closer, or an inline comment.
_ASK_VALUE = re.compile(r"(?:^|[:\[{,])\s*[\"']?ask[\"']?\s*(?:$|[,}\]]|\s+#)")


def ask_violations_in_markdown(text: str, origin: str) -> list[str]:
    """`ask` in an agent/mode .md file's frontmatter `permission:` block.
    Text scan, not a YAML parse (stdlib-only, see module docstring): the
    regex matches `ask` only in value position and full-line comments are
    skipped, so prose and pattern keys are ignored. Asymmetry is intended:
    a false positive costs a loud refusal a human reviews; a false negative
    re-opens the ADR 0031 park."""
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return []
    try:
        end = lines[1:].index("---") + 1
    except ValueError:
        return []  # no closing fence: no frontmatter, nothing to scan
    out = []
    in_perm = False
    for lineno, line in enumerate(lines[1:end], 1):
        top = re.match(r"^([^:\s][^:]*):\s*(.*)$", line)
        if top:  # top-level frontmatter key opens/closes the block
            # A quoted key ("permission":) is valid YAML and opens the
            # block just the same - strip quotes or we fail open.
            in_perm = top.group(1).strip().strip("\"'") == "permission"
            rest = top.group(2)  # flow-style value on the key line itself
            if in_perm and rest and _ASK_VALUE.search(rest):
                out.append(f"{origin}: frontmatter permission line "
                           f"{lineno}: {line.strip()}")
            continue
        if not in_perm or line.strip().startswith("#"):
            continue
        if _ASK_VALUE.search(line):
            out.append(f"{origin}: frontmatter permission line "
                       f"{lineno}: {line.strip()}")
    return out


def _strip_jsonc_comments(text: str) -> str:
    """Remove // and /* */ comments outside string literals (opencode
    parses config as JSONC). Anything else exotic (trailing commas) still
    fails json.loads and fails closed in scan_project_tier."""
    out, i, n = [], 0, len(text)
    in_str = False
    while i < n:
        c = text[i]
        if in_str:
            out.append(c)
            if c == "\\" and i + 1 < n:
                out.append(text[i + 1])
                i += 2
                continue
            if c == '"':
                in_str = False
            i += 1
        elif c == '"':
            in_str = True
            out.append(c)
            i += 1
        elif c == "/" and text[i + 1:i + 2] == "/":
            i += 2
            while i < n and text[i] != "\n":
                i += 1
        elif c == "/" and text[i + 1:i + 2] == "*":
            i += 2
            while text[i:i + 2] not in ("*/", ""):
                i += 1
            i += 2
        else:
            out.append(c)
            i += 1
    return "".join(out)


def scan_project_tier(repo: Path) -> list[str]:
    """Every `ask` violation a mounted repo's project tier would merge over
    the global envelope. Fail-closed on content: an unparseable or
    unreadable file cannot be verified ask-free, so it is reported as a
    violation naming the file - per file, so one bad file never discards
    violations already collected from the others. (The kick gate's broad
    except stays as belt-and-braces for anything unexpected.)"""
    out: list[str] = []
    for f in project_tier_files(repo):
        origin = str(f)
        try:
            text = f.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as e:
            # Content we cannot read cannot be verified ask-free - fail
            # closed per file, so one bad file never discards violations
            # already collected from the others.
            out.append(f"{origin}: unreadable ({e}) - cannot verify ask-free")
            continue
        if f.suffix == ".md":
            out += ask_violations_in_markdown(text, origin)
            continue
        try:
            cfg = json.loads(_strip_jsonc_comments(text))
        except json.JSONDecodeError as e:
            out.append(f"{origin}: unparseable ({e.msg}) - "
                       "cannot verify ask-free")
            continue
        if isinstance(cfg, dict):
            out += ask_violations_in_config(cfg, origin)
    return out
