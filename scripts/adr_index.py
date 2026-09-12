#!/usr/bin/env python3
"""Regenerate the docs/adr/README.md index table from the ADR files.

Issue #72 / ADR 0053: the table between the <!-- adr-index:start --> and
<!-- adr-index:end --> markers is a generated artifact - never edit it by
hand. The source of truth is the ADR files themselves: the number comes
from the filename (NNNN-<slug>.md), the title from the first H1
(`# NNNN. Title`), and the status/date from the frontmatter. Anything
breaking that contract - a duplicate number, a bad filename, missing or
invalid frontmatter, an unknown status, an H1/filename mismatch - is a
loud error, never a silent skip.

Stdlib-only on purpose: the pre-commit hook is `language: system` and runs
on whatever `python3` the shebang finds, with no network to install
dependencies (ADR 0047's networkless hooks). The frontmatter is therefore
parsed per line and its shape validated, so a frontmatter that ever grows
past flat `key: value` lines fails loudly instead of parsing subtly wrong.

Exit codes: 0 = index already in sync, 1 = README rewritten (stage the
change), 2 = validation errors.
"""

import datetime
import re
import sys
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ADR_DIR = ROOT / "docs/adr"
README = ADR_DIR / "README.md"
TEMPLATE = "0000-madr-template.md"
START = "<!-- adr-index:start -->"
END = "<!-- adr-index:end -->"

FILENAME_RE = re.compile(r"^(\d{4})-.+\.md$")
H1_RE = re.compile(r"^# (\d{4})\. (.+)$")
# fromisoformat is interpreter-lenient (3.14 accepts 20260911 and week
# dates, <=3.10 rejects them); pin the shape so every python3 agrees.
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
STATUS_RE = re.compile(
    r"^(proposed|accepted|rejected|deprecated)( \(.*\))?$"
    r"|^superseded by ADR-\d{4}( \(.*\))?$")
STATUS_HINT = ("proposed | accepted | rejected | deprecated | "
               "superseded by ADR-NNNN; parenthetical allowed")


class AdrIndexError(Exception):
    """One or more ADR files (or the README markers) broke the contract."""


@dataclass
class ADR:
    number: str      # "0043"
    filename: str    # "0043-interim-shared-identity-trigger.md"
    title: str       # H1 text after "NNNN. "
    status: str      # frontmatter status, verbatim
    date: str        # frontmatter date, verbatim


def collect(adr_dir: Path) -> list[ADR]:
    """Parse every ADR under adr_dir; return the list sorted by number.

    Every problem in every file is collected and raised as one
    AdrIndexError, one `<file>: <problem>` line each - only README.md and
    the MADR template (whose `date: YYYY-MM-DD` placeholder would fail
    validation) are skipped, anything else nonconforming is an error.
    """
    errors: list[str] = []
    adrs: list[ADR] = []
    seen: dict[str, str] = {}
    # Discovery is case/extension-tolerant on purpose: glob("*.md") misses
    # 0054-x.MD and 0055-y.markdown on Linux but not macOS. Anything that
    # looks like markdown must instead fail FILENAME_RE loudly, the same
    # way on every platform.
    candidates = sorted(
        path for path in adr_dir.iterdir()
        if path.name.lower().endswith((".md", ".markdown")))
    for path in candidates:
        if path.name in ("README.md", TEMPLATE):
            continue
        match = FILENAME_RE.match(path.name)
        if match is None:
            errors.append(
                f"{path.name}: filename does not match 'NNNN-<slug>.md'")
            continue
        number = match.group(1)
        if number in seen:
            errors.append(f"{path.name}: duplicate ADR number {number} "
                          f"(also {seen[number]})")
            continue
        # Registration is filename-derived, not coupled to parse success: a
        # broken first file still names the duplicate that follows it.
        seen[number] = path.name
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError as exc:
            errors.append(f"{path.name}: not valid UTF-8 ({exc})")
            continue
        except OSError as exc:
            errors.append(f"{path.name}: unreadable: {exc}")
            continue
        adr, problems = _parse_adr(path.name, number, text)
        if problems:
            errors.extend(f"{path.name}: {problem}" for problem in problems)
            continue
        # A pipe would render a four-column row that both the hook and the
        # byte-compare would then ratify (review of PR #97).
        pipes = [field for field in ("title", "status")
                 if "|" in getattr(adr, field)]
        for field in pipes:
            errors.append(f"{path.name}: {field} must not contain '|' "
                          "(it breaks the index table)")
        if not pipes:
            adrs.append(adr)
    if errors:
        raise AdrIndexError("\n".join(errors))
    return sorted(adrs, key=lambda adr: adr.number)


def _parse_adr(name: str, number: str,
               text: str) -> tuple[ADR | None, list[str]]:
    """Validate one ADR file; problems come back, never an exception."""
    lines = text.splitlines()
    if not lines or lines[0] != "---":
        return None, ["missing frontmatter fences"]
    try:
        close = lines.index("---", 1)
    except ValueError:
        return None, ["missing frontmatter fences"]
    problems: list[str] = []
    values: dict[str, str] = {}
    for key in ("status", "date"):
        for line in lines[1:close]:
            if line.startswith(f"{key}:"):
                values[key] = line[len(key) + 1:].strip()
                break
        else:
            problems.append(f"missing '{key}:' in frontmatter")
    if "date" in values:
        try:
            if DATE_RE.match(values["date"]) is None:
                raise ValueError(values["date"])
            datetime.date.fromisoformat(values["date"])
        except ValueError:
            problems.append(
                f"invalid date '{values['date']}' (not ISO YYYY-MM-DD)")
    if "status" in values and STATUS_RE.match(values["status"]) is None:
        problems.append(
            f"unknown status '{values['status']}' ({STATUS_HINT})")
    if problems:
        return None, problems
    for line in lines[close + 1:]:
        if line.startswith("# "):
            h1 = H1_RE.match(line)
            if h1 is None:
                return None, [f"first H1 lacks the 'NNNN. ' prefix: '{line}'"]
            if h1.group(1) != number:
                return None, [f"H1 number {h1.group(1)} does not match "
                              f"filename number {number}"]
            return ADR(number=number, filename=name,
                       title=h1.group(2).strip(),
                       status=values["status"], date=values["date"]), []
    return None, ["no H1 ('# NNNN. Title') after the frontmatter"]


def render_table(adrs: list[ADR]) -> str:
    """The index table, every line LF-terminated including the last."""
    lines = ["| ADR | Title | Status |", "|-----|-------|--------|"]
    for adr in adrs:
        lines.append(f"| [{adr.number}]({adr.filename}) "
                     f"| {adr.title} | {adr.status} |")
    return "".join(f"{line}\n" for line in lines)


def _region_bounds(text: str) -> tuple[list[str], int, int]:
    """Split text into lines and locate the one START/END marker pair.

    Each marker must appear exactly once as a whole line: zero of either is
    the missing-marker error; more than one of either (e.g. a marker quoted
    inside a fenced code block above the real pair) is a hard error, because
    splicing from a quoted START to the real END would silently delete the
    prose between them - and afterwards hook and pytest would agree on the
    mutilated file (review of PR #97).
    """
    lines = text.splitlines(keepends=True)

    def find_all(marker: str) -> list[int]:
        return [i for i, line in enumerate(lines)
                if line.rstrip("\r\n") == marker]

    starts = find_all(START)
    ends = find_all(END)
    if not starts or not ends:
        raise AdrIndexError("docs/adr/README.md: missing adr-index markers")
    if len(starts) > 1 or len(ends) > 1:
        raise AdrIndexError(
            "docs/adr/README.md: expected exactly one adr-index:start and "
            f"one adr-index:end marker (found {len(starts)} start, "
            f"{len(ends)} end)")
    start, end = starts[0], ends[0]
    if end < start:
        raise AdrIndexError(
            "docs/adr/README.md: adr-index:end before adr-index:start")
    return lines, start, end


def extract_region(readme_text: str) -> str:
    """The text strictly between the START and END marker lines."""
    lines, start, end = _region_bounds(readme_text)
    return "".join(lines[start + 1:end])


def rewrite_readme(readme: Path, table: str) -> bool:
    """Splice table into the marked region; return True iff the file changed.

    The drift check goes through extract_region, so this guard and the
    pytest byte-compare can never disagree. Everything outside the
    markers is byte-preserved; an in-sync region is not rewritten. IO
    failures on the README join the curated contract (main() exit 2),
    never a traceback.
    """
    try:
        text = readme.read_text(encoding="utf-8", newline="")
    except OSError as exc:
        raise AdrIndexError(
            f"docs/adr/README.md: unreadable: {exc}") from exc
    if extract_region(text) == table:
        return False
    lines, start, end = _region_bounds(text)
    spliced = "".join(lines[:start + 1]) + table + "".join(lines[end:])
    try:
        readme.write_text(spliced, encoding="utf-8", newline="")
    except OSError as exc:
        raise AdrIndexError(
            f"docs/adr/README.md: unreadable: {exc}") from exc
    return True


def main() -> int:
    try:
        table = render_table(collect(ADR_DIR))
        changed = rewrite_readme(README, table)
    except AdrIndexError as exc:
        print(exc, file=sys.stderr)
        return 2
    if changed:
        print("docs/adr/README.md: index regenerated - stage the change",
              file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
