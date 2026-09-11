"""veggies costs - spend-log parsing, aggregation and rendering.

Pure module, ZERO IO: every function takes text/records and returns data or
strings, so tests never touch podman, ssh or the filesystem. The record
contract and reader policies (skip+count malformed, keep-but-exclude
unpriced) are ADR 0044; the attribution axis (#N:/D#N: titles) is ADR 0022
decision 3.
"""

from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone

SPEND_LOG_NAME = "spend.jsonl"

# Beyond this window the daily rollup gets unreadable; switch to ISO weeks.
WEEKLY_AFTER_DAYS = 62

BAR_WIDTH = 40


@dataclass
class SpendRecord:
    ts: float
    model: str
    prompt_tokens: int
    completion_tokens: int
    spend: float | None  # None = unpriced: kept, excluded from sums (ADR 0044)
    session: str       # stamped title; "" = unattributed
    session_id: str
    inferred: bool     # attr == "inferred" (reduced-confidence attribution)


@dataclass
class ParseResult:
    records: list[SpendRecord]
    skipped: int    # malformed/blank lines - a money report never shrinks silently
    unpriced: int   # records kept with spend None


@dataclass
class TargetRow:
    kind: str          # "issue" | "discussion" | "unattributed"
    number: int | None
    label: str         # "#47: <title>" | "D#38: <title>" | "(unattributed)"
    calls: int
    spend: float       # priced sum; 0.0 when every call is unpriced
    unpriced: int
    inferred: float    # the inferred-attribution share of spend


def _is_num(x: object) -> bool:
    return isinstance(x, (int, float)) and not isinstance(x, bool)


def _is_int(x: object) -> bool:
    return isinstance(x, int) and not isinstance(x, bool)


def _record(obj: object) -> SpendRecord | None:
    """One JSONL object -> SpendRecord, or None when malformed. Required
    fields with wrong types are malformed (tolerate-by-skip); only `spend`
    degrades in place (None = unpriced), per ADR 0044 decision 3."""
    if not isinstance(obj, dict):
        return None
    raw_ts = obj.get("ts")
    if isinstance(raw_ts, bool) or not isinstance(raw_ts, (int, float)):
        return None
    try:
        ts = float(raw_ts)
    except (OverflowError, ValueError, TypeError):
        return None  # e.g. an int too large to convert to float
    # ts feeds datetime.fromtimestamp: non-finite or beyond datetime.max's
    # epoch (253402300800) would raise there - malformed, not fatal.
    if not math.isfinite(ts) or abs(ts) > 253402300800:
        return None
    if not isinstance(obj.get("model"), str):
        return None
    if not _is_int(obj.get("prompt_tokens")) or \
            not _is_int(obj.get("completion_tokens")):
        return None
    if not isinstance(obj.get("session_id"), str):
        return None
    session = obj.get("session", "")
    if not isinstance(session, str):
        return None
    spend = obj.get("spend")
    # json.loads accepts NaN/Infinity, and a huge int overflows float() -
    # both degrade to unpriced, never a traceback or a poisoned sum.
    priced = None
    if _is_num(spend):
        try:
            priced = float(spend)
        except OverflowError:
            priced = None
    if priced is not None and not math.isfinite(priced):
        priced = None
    return SpendRecord(
        ts=ts, model=obj["model"],
        prompt_tokens=int(obj["prompt_tokens"]),
        completion_tokens=int(obj["completion_tokens"]),
        spend=priced,
        session=session, session_id=obj["session_id"],
        inferred=obj.get("attr") == "inferred")


def parse_spend_log(text: str) -> ParseResult:
    records: list[SpendRecord] = []
    skipped = 0
    for line in text.splitlines():
        line = line.strip()
        if not line:
            skipped += 1
            continue
        try:
            obj = json.loads(line)
        except ValueError:
            # JSONDecodeError is a ValueError subclass; this clause also
            # catches 4300+-digit int literals hiding in ignored extra keys.
            skipped += 1
            continue
        rec = _record(obj)
        if rec is None:
            skipped += 1
        else:
            records.append(rec)
    return ParseResult(records, skipped,
                       sum(1 for r in records if r.spend is None))


# The colon is load-bearing: `#1:` must not match a prefix of `#12: foo`.
_ISSUE = re.compile(r"^#(\d+):")
_DISCUSSION = re.compile(r"^D#(\d+)(?: elaborate)?:")


def attribute(session: str) -> tuple[str, int | None]:
    """Title -> attribution target (ADR 0022 decision 3; D#N elaborate from
    ADR 0041 reports under the discussion)."""
    m = _ISSUE.match(session)
    if m:
        return "issue", int(m.group(1))
    m = _DISCUSSION.match(session)
    if m:
        return "discussion", int(m.group(1))
    return "unattributed", None


def filter_since(records: list[SpendRecord],
                 since: date | None) -> list[SpendRecord]:
    """Keep records at/after `since` 00:00:00 UTC; None keeps everything."""
    if since is None:
        return list(records)
    floor = datetime(since.year, since.month, since.day,
                     tzinfo=timezone.utc).timestamp()
    return [r for r in records if r.ts >= floor]


def use_weekly(days: int, force: bool = False) -> bool:
    return force or days > WEEKLY_AFTER_DAYS


def summarize(records: list[SpendRecord]) -> list[TargetRow]:
    """One row per attributed target, spend desc; the unattributed row is
    always present and pinned last - overhead stays visible (ADR 0022)."""
    groups: dict[tuple[str, int | None], list[SpendRecord]] = {}
    for r in records:
        groups.setdefault(attribute(r.session), []).append(r)
    rows = []
    for (kind, number), recs in groups.items():
        label = "(unattributed)" if kind == "unattributed" else \
            max(recs, key=lambda r: r.ts).session
        rows.append(TargetRow(
            kind, number, label, len(recs),
            sum(r.spend for r in recs if r.spend is not None),
            sum(1 for r in recs if r.spend is None),
            sum(r.spend for r in recs
                if r.spend is not None and r.inferred)))
    if ("unattributed", None) not in groups:
        rows.append(TargetRow("unattributed", None, "(unattributed)",
                              0, 0.0, 0, 0.0))
    rows.sort(key=lambda r: (r.kind == "unattributed", -r.spend,
                             r.kind, r.number or 0))
    return rows


def _bucket_label(day: date, weekly: bool) -> str:
    if weekly:
        iso = day.isocalendar()
        return f"{iso.year}-W{iso.week:02d}"
    return day.isoformat()


def rollup(records: list[SpendRecord],
           weekly: bool = False) -> list[tuple[str, float]]:
    """(bucket, spend) pairs, chronological, None-spend excluded. Sparse:
    only buckets with priced records appear; render_bars fills the gaps."""
    buckets: dict[str, tuple[date, float]] = {}
    for r in records:
        if r.spend is None:
            continue
        day = datetime.fromtimestamp(r.ts, tz=timezone.utc).date()
        label = _bucket_label(day, weekly)
        first, total = buckets.get(label, (day, 0.0))
        buckets[label] = (min(first, day), total + r.spend)
    return [(label, total) for label, (_, total) in
            sorted(buckets.items(), key=lambda kv: kv[1][0])]


def money(x: float | None) -> str:
    if x is None:
        return "?"
    return f"${x:.4f}" if x < 1 else f"${x:.2f}"


def _dense(buckets: list[tuple[str, float]],
           weekly: bool) -> list[tuple[str, float]]:
    """Zero-fill the gaps between the first and last bucket so quiet days
    render as visible gaps instead of vanishing."""
    if not buckets:
        return []
    amounts = dict(buckets)
    fmt = "%G-W%V-%u" if weekly else "%Y-%m-%d"
    first = datetime.strptime(buckets[0][0] + "-1" if weekly else
                              buckets[0][0], fmt).date()
    last = datetime.strptime(buckets[-1][0] + "-1" if weekly else
                             buckets[-1][0], fmt).date()
    step = timedelta(days=7 if weekly else 1)
    out = []
    day = first
    while day <= last:
        label = _bucket_label(day, weekly)
        out.append((label, amounts.get(label, 0.0)))
        day += step
    return out


def render_bars(buckets: list[tuple[str, float]], weekly: bool = False) -> str:
    mode = "weekly" if weekly else "daily"
    dense = _dense(buckets, weekly)
    if not dense:
        return f"{mode} (no spend):"
    lines = [f"{mode} ({dense[0][0]} -> {dense[-1][0]}):"]
    peak = max(v for _, v in dense)
    for label, value in dense:
        bar = ""
        if value > 0 and peak > 0:
            bar = "█" * max(1, round(BAR_WIDTH * value / peak))
        lines.append(f"{label}  {money(value)}  {bar}".rstrip())
    return "\n".join(lines)


def render_summary(rows: list[TargetRow], *, since: date, today: date,
                   earliest: bool, segments: list[str], skipped: int,
                   unpriced: int, bars: list[tuple[str, float]],
                   weekly: bool) -> str:
    total = sum(r.spend for r in rows)
    calls = sum(r.calls for r in rows)
    days = max(0, (today - since).days)  # a future --since never shows -N
    header = (f"spend since {since.isoformat()} ({days} days)"
              f", {money(total)} total, {calls} calls")
    if earliest:  # default window: the start IS the oldest record we have
        header += ", earliest retained record"
    if len(segments) > 1:
        header += f", {len(segments)} segments ({', '.join(segments)})"
    lines = [header]
    if skipped:
        # skipped spans the whole log; unpriced is window-scoped - say so
        lines.append(f"skipped: {skipped} malformed lines (whole log)")
    if unpriced:
        lines.append(f"unpriced: {unpriced} record(s) missing spend "
                     "(rendered ?, excluded from totals)")
    lines += ["", f"{'TARGET':<44} {'CALLS':>5}  SPEND"]
    for r in rows:
        spend = money(r.spend)
        if r.inferred > 0:
            spend += f" (inferred: {money(r.inferred)})"
        lines.append(f"{r.label[:44]:<44} {r.calls:>5}  {spend}")
    nouns = []
    issues = sum(1 for r in rows if r.kind == "issue")
    discussions = sum(1 for r in rows if r.kind == "discussion")
    if issues:
        nouns.append(f"{issues} issues")
    if discussions:
        nouns.append(f"{discussions} discussions")
    nouns.append(f"{calls} calls")
    lines.append(f"total ({', '.join(nouns)})  {money(total)}")
    lines += ["", render_bars(bars, weekly)]
    return "\n".join(lines)


def _priced(recs: list[SpendRecord]) -> float | None:
    """Sum of priced spends, or None when every call is unpriced."""
    values = [r.spend for r in recs if r.spend is not None]
    return sum(values) if values else None


def render_detail(records: list[SpendRecord], *, subject: str,
                  unpriced: int, weekly: bool = False) -> str:
    """Per-(session_id, title) cascade + per-model + daily bars for one
    filter. session_id is part of the group key: re-kicks mint new sessions
    under the same `#N:` title (ADR 0035), so title alone cannot separate
    them (ADR 0044)."""
    groups: dict[tuple[str, str], list[SpendRecord]] = {}
    for r in records:
        groups.setdefault((r.session_id, r.session), []).append(r)
    ordered = sorted(groups.items(), key=lambda kv: min(r.ts for r in kv[1]))
    total = sum(r.spend for r in records if r.spend is not None)
    lines = [f"{subject}: {money(total)} across {len(ordered)} sessions "
             f"({len(records)} calls)"]
    if unpriced:
        lines.append(f"unpriced: {unpriced} record(s) missing spend "
                     "(rendered ?, excluded from totals)")
    lines += ["", f"{'SESSION_ID':<24} {'CALLS':>5} {'TOKENS_IN':>9} "
                  f"{'TOKENS_OUT':>10} {'SPEND':>10}  TITLE"]
    for (sid, title), recs in ordered:
        lines.append(f"{sid[:24]:<24} {len(recs):>5} "
                     f"{sum(r.prompt_tokens for r in recs):>9} "
                     f"{sum(r.completion_tokens for r in recs):>10} "
                     f"{money(_priced(recs)):>10}  {title or '(untitled)'}")
    titles: dict[str, list[list[SpendRecord]]] = {}
    for (_, title), recs in ordered:
        titles.setdefault(title, []).append(recs)
    for title, grouped in titles.items():
        if len(grouped) > 1:
            all_recs = [r for recs in grouped for r in recs]
            lines.append(f"subtotal {title or '(untitled)'!r}: "
                         f"{money(_priced(all_recs))} across "
                         f"{len(grouped)} sessions ({len(all_recs)} calls)")
    lines += ["", f"{'MODEL':<24} {'CALLS':>5} {'TOKENS_IN':>9} "
                  f"{'TOKENS_OUT':>10} {'SPEND':>10}"]
    models: dict[str, list[SpendRecord]] = {}
    for r in records:
        models.setdefault(r.model, []).append(r)
    for model, recs in sorted(models.items(),
                              key=lambda kv: (_priced(kv[1]) is None,
                                              -(_priced(kv[1]) or 0),
                                              kv[0])):
        lines.append(f"{model[:24]:<24} {len(recs):>5} "
                     f"{sum(r.prompt_tokens for r in recs):>9} "
                     f"{sum(r.completion_tokens for r in recs):>10} "
                     f"{money(_priced(recs)):>10}")
    lines += ["", render_bars(rollup(records, weekly=weekly), weekly=weekly)]
    return "\n".join(lines)
