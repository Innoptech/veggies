"""Tests for cli/costs.py (pure spend-log module, ADR 0044) and the
`veggies costs` wiring in cli/veggies.py (handler tests never touch real
ssh/podman/gh - State, host_run, run and shutil.which are stubbed)."""

import importlib.util
import json
import subprocess
import sys
from datetime import date, datetime, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT / "cli"))
import costs  # noqa: E402

_spec = importlib.util.spec_from_file_location("veggies", ROOT / "cli/veggies.py")
veggies = importlib.util.module_from_spec(_spec)
sys.modules["veggies"] = veggies  # dataclass introspection needs this (py3.14)
_spec.loader.exec_module(veggies)


def _ts(y, m, d, hh=0, mm=0, ss=0) -> float:
    return datetime(y, m, d, hh, mm, ss, tzinfo=timezone.utc).timestamp()


def _line(**kw) -> str:
    obj = {"ts": _ts(2026, 9, 5, 12), "model": "deepseek-v4",
           "prompt_tokens": 100, "completion_tokens": 40,
           "spend": 0.002, "session": "", "session_id": "ses_x"}
    obj.update(kw)
    return json.dumps(obj)


def _rec(**kw) -> costs.SpendRecord:
    base = {"ts": _ts(2026, 9, 5, 12), "model": "deepseek-v4",
            "prompt_tokens": 100, "completion_tokens": 40,
            "spend": 0.002, "session": "", "session_id": "ses_x",
            "inferred": False}
    base.update(kw)
    return costs.SpendRecord(**base)


# --- parse_spend_log -----------------------------------------------------------


def test_parse_golden_multi_line():
    text = "\n".join([
        _line(session="#47: add costs", session_id="ses_1", spend=1.20),
        _line(session="D#38: cost visibility?", session_id="ses_2",
              spend=2.00, attr="inferred"),
        _line(session="D#38 elaborate: personas", session_id="ses_3", spend=0.50),
        _line(session="", session_id="ses_4", spend=0.10),
        _line(session="#12: tidy", session_id="ses_5", spend=0.01,
              ts=_ts(2026, 9, 5, 13, 30), litellm_extra={"trace": "abc"}),
    ])
    res = costs.parse_spend_log(text)
    assert res.skipped == 0 and res.unpriced == 0
    assert len(res.records) == 5
    first = res.records[0]
    assert first.session == "#47: add costs" and first.session_id == "ses_1"
    assert first.spend == 1.20 and first.inferred is False
    assert first.prompt_tokens == 100 and first.completion_tokens == 40
    assert res.records[1].inferred is True  # attr: inferred
    assert res.records[2].inferred is False  # default is stamped
    assert res.records[4].ts == _ts(2026, 9, 5, 13, 30)  # unknown keys ignored


def test_parse_blank_and_malformed_lines_are_skipped_and_counted():
    text = "\n".join([
        _line(session_id="ses_1"),
        "",
        "   ",
        "{not json",
        '["a", "list"]',
        _line(session_id="ses_2"),
    ])
    res = costs.parse_spend_log(text)
    assert [r.session_id for r in res.records] == ["ses_1", "ses_2"]
    assert res.skipped == 4


def test_parse_accepts_int_and_float_ts():
    res = costs.parse_spend_log(
        _line(ts=1757000000, session_id="a") + "\n"
        + _line(ts=1757000000.5, session_id="b"))
    assert [r.ts for r in res.records] == [1757000000.0, 1757000000.5]


def test_parse_missing_or_nonnumeric_spend_kept_counted_unpriced():
    missing = json.dumps({"ts": 1, "model": "m", "prompt_tokens": 1,
                          "completion_tokens": 1, "session": "",
                          "session_id": "ses_a"})
    res = costs.parse_spend_log("\n".join([
        missing,
        _line(spend="lots", session_id="ses_b"),
        _line(spend=0.5, session_id="ses_c"),
    ]))
    assert res.skipped == 0
    assert res.unpriced == 2
    assert [r.spend for r in res.records] == [None, None, 0.5]
    # excluded from sums, still counted as calls (ADR 0044 reader policy)
    row = costs.summarize(res.records)[-1]
    assert row.calls == 3 and row.spend == 0.5 and row.unpriced == 2


def test_parse_missing_session_id_is_malformed():
    line = json.dumps({"ts": 1, "model": "m", "prompt_tokens": 1,
                       "completion_tokens": 1, "spend": 0.1, "session": ""})
    res = costs.parse_spend_log(line)
    assert res.records == [] and res.skipped == 1


@pytest.mark.parametrize("bad", [
    {"ts": "2026-09-05"},
    {"model": 42},
    {"prompt_tokens": 1.5},
    {"completion_tokens": "7"},
    {"session": 5},
    {"session_id": 99},
])
def test_parse_wrong_typed_required_fields_are_malformed(bad):
    res = costs.parse_spend_log(_line(**bad))
    assert res.records == [] and res.skipped == 1


# --- attribute -------------------------------------------------------------------


@pytest.mark.parametrize("title,expected", [
    ("#1: add costs", ("issue", 1)),
    ("#12: foo", ("issue", 12)),       # the colon anchors: not issue 1
    ("#1x: nope", ("unattributed", None)),
    ("#1", ("unattributed", None)),
    ("#", ("unattributed", None)),
    ("D#3: chat", ("discussion", 3)),
    ("D#3 elaborate: x", ("discussion", 3)),
    ("D#3 elaborates: x", ("unattributed", None)),
    ("", ("unattributed", None)),
    ("(untitled)", ("unattributed", None)),
])
def test_attribute(title, expected):
    assert costs.attribute(title) == expected


# --- filter_since ------------------------------------------------------------------


def test_filter_since_inclusive_at_midnight_utc():
    before = _rec(ts=_ts(2026, 9, 1, 23, 59, 59), session_id="a")
    at = _rec(ts=_ts(2026, 9, 2), session_id="b")
    after = _rec(ts=_ts(2026, 9, 2, 0, 0, 1), session_id="c")
    recs = [before, at, after]
    assert costs.filter_since(recs, None) == recs
    assert costs.filter_since(recs, date(2026, 9, 2)) == [at, after]


# --- rollup / use_weekly ------------------------------------------------------------


def test_rollup_daily_buckets_respect_utc_day_boundary():
    recs = [_rec(ts=_ts(2026, 9, 1, 23, 59, 59), spend=1.0),
            _rec(ts=_ts(2026, 9, 2, 0, 0, 1), spend=2.0),
            _rec(ts=_ts(2026, 9, 2, 12), spend=4.0)]
    assert costs.rollup(recs, weekly=False) == [
        ("2026-09-01", 1.0), ("2026-09-02", 6.0)]


def test_rollup_weekly_iso_buckets():
    recs = [_rec(ts=_ts(2026, 9, 1), spend=1.0),
            _rec(ts=_ts(2026, 9, 3), spend=2.0),   # same ISO week as Sep 1
            _rec(ts=_ts(2026, 9, 8), spend=4.0)]   # next ISO week
    w1 = date(2026, 9, 1).isocalendar()
    w2 = date(2026, 9, 8).isocalendar()
    assert costs.rollup(recs, weekly=True) == [
        (f"{w1.year}-W{w1.week:02d}", 3.0),
        (f"{w2.year}-W{w2.week:02d}", 4.0)]


def test_rollup_excludes_unpriced_records():
    recs = [_rec(ts=_ts(2026, 9, 1), spend=None),
            _rec(ts=_ts(2026, 9, 2), spend=1.0)]
    assert costs.rollup(recs, weekly=False) == [("2026-09-02", 1.0)]


def test_use_weekly_threshold_and_force():
    assert costs.use_weekly(62) is False
    assert costs.use_weekly(63) is True
    assert costs.use_weekly(3, force=True) is True


# --- money ---------------------------------------------------------------------------


def test_money():
    assert costs.money(None) == "?"
    assert costs.money(0.0042) == "$0.0042"
    assert costs.money(0.00852) == "$0.0085"
    assert costs.money(0) == "$0.0000"
    assert costs.money(1.0) == "$1.00"
    assert costs.money(4.2) == "$4.20"


# --- summarize -------------------------------------------------------------------------


def test_summarize_sorts_by_spend_desc_with_unattributed_last():
    recs = [
        _rec(session="#47: add costs", session_id="s1", spend=1.20),
        _rec(session="#12: tidy", session_id="s2", spend=5.00),
        _rec(session="D#38: costs?", session_id="s3", spend=2.00),
        _rec(session="", session_id="s4", spend=0.10),
    ]
    rows = costs.summarize(recs)
    assert [(r.kind, r.number) for r in rows] == [
        ("issue", 12), ("discussion", 38), ("issue", 47),
        ("unattributed", None)]
    assert rows[2].label == "#47: add costs"
    assert rows[3].label == "(unattributed)"
    assert [r.calls for r in rows] == [1, 1, 1, 1]


def test_summarize_unattributed_row_always_present_even_empty():
    rows = costs.summarize([_rec(session="#1: x", session_id="s1")])
    assert rows[-1].kind == "unattributed" and rows[-1].calls == 0
    rows = costs.summarize([])
    assert [r.kind for r in rows] == ["unattributed"]


def test_summarize_label_is_latest_title_and_tracks_inferred_share():
    recs = [
        _rec(ts=_ts(2026, 9, 1), session="#47: old", session_id="s1", spend=1.0),
        _rec(ts=_ts(2026, 9, 2), session="#47: new", session_id="s2", spend=2.0,
             inferred=True),
    ]
    (row,) = [r for r in costs.summarize(recs) if r.kind == "issue"]
    assert row.label == "#47: new"
    assert row.spend == 3.0 and row.inferred == 2.0 and row.calls == 2


# --- render_summary ----------------------------------------------------------------------


def _summary_rows():
    return costs.summarize([
        _rec(session="#47: add costs", session_id="s1", spend=1.20),
        _rec(session="#12: tidy", session_id="s2", spend=5.00),
        _rec(session="D#38: costs?", session_id="s3", spend=2.00),
        _rec(session="", session_id="s4", spend=0.10),
    ])


def test_render_summary_header_window_and_earliest_suffix():
    out = costs.render_summary(
        _summary_rows(), since=date(2026, 9, 1), today=date(2026, 9, 11),
        earliest=True, segments=["spend.jsonl"], skipped=0, unpriced=0,
        bars=[], weekly=False)
    assert out.splitlines()[0] == ("spend since 2026-09-01 (10 days), $8.30 "
                                   "total, 4 calls, earliest retained record")


def test_render_summary_explicit_since_and_segments():
    out = costs.render_summary(
        _summary_rows(), since=date(2026, 9, 1), today=date(2026, 9, 11),
        earliest=False, segments=["spend.jsonl", "spend.jsonl.1"],
        skipped=0, unpriced=0, bars=[], weekly=False)
    header = out.splitlines()[0]
    assert "earliest retained record" not in header
    assert header.endswith("2 segments (spend.jsonl, spend.jsonl.1)")
    # one segment is silent about it
    out = costs.render_summary(
        _summary_rows(), since=date(2026, 9, 1), today=date(2026, 9, 11),
        earliest=False, segments=["spend.jsonl"], skipped=0, unpriced=0,
        bars=[], weekly=False)
    assert "segment" not in out.splitlines()[0]


def test_render_summary_skipped_unpriced_lines_only_when_nonzero():
    base = dict(since=date(2026, 9, 1), today=date(2026, 9, 11), earliest=True,
                segments=["spend.jsonl"], bars=[], weekly=False)
    out = costs.render_summary(_summary_rows(), skipped=0, unpriced=0, **base)
    assert "skipped:" not in out and "unpriced:" not in out
    out = costs.render_summary(_summary_rows(), skipped=3, unpriced=2, **base)
    assert "skipped: 3 malformed lines" in out
    assert ("unpriced: 2 record(s) missing spend "
            "(rendered ?, excluded from totals)") in out


def test_render_summary_table_order_total_row_and_inferred_share():
    rows = costs.summarize([
        _rec(session="#47: add costs", session_id="s1", spend=1.20,
             inferred=True),
        _rec(session="#47: add costs", session_id="s2", spend=0.30,
             inferred=True),
        _rec(session="#12: tidy", session_id="s3", spend=5.00),
        _rec(session="", session_id="s4", spend=0.10),
    ])
    out = costs.render_summary(rows, since=date(2026, 9, 1),
                               today=date(2026, 9, 2), earliest=True,
                               segments=["spend.jsonl"], skipped=0, unpriced=0,
                               bars=[], weekly=False)
    lines = out.splitlines()
    target_idx = next(i for i, ln in enumerate(lines) if ln.startswith("TARGET"))
    assert lines[target_idx + 1].startswith("#12: tidy")
    assert lines[target_idx + 2].startswith("#47: add costs")
    assert "$1.50 (inferred: $1.50)" in lines[target_idx + 2]
    assert lines[target_idx + 3].startswith("(unattributed)")
    total_line = next(ln for ln in lines if ln.startswith("total ("))
    assert total_line == "total (2 issues, 4 calls)  $6.60"


# --- render_bars ---------------------------------------------------------------------------


def test_render_bars_scales_max_to_40_chars():
    out = costs.render_bars([("2026-09-01", 1.0), ("2026-09-02", 2.0)],
                            weekly=False)
    lines = out.splitlines()
    assert lines[0] == "daily (2026-09-01 -> 2026-09-02):"
    assert "█" * 40 in lines[2]
    assert "█" * 41 not in out
    assert lines[1].count("█") == 20


def test_render_bars_shows_zero_gaps_only_between_first_and_last():
    out = costs.render_bars([("2026-09-01", 1.0), ("2026-09-03", 1.0)],
                            weekly=False)
    gap = next(ln for ln in out.splitlines() if ln.startswith("2026-09-02"))
    assert "█" not in gap and "$0.0000" in gap
    assert "2026-08-31" not in out and "2026-09-04" not in out


def test_render_bars_weekly_header_and_empty():
    out = costs.render_bars([("2026-W36", 1.0), ("2026-W38", 2.0)], weekly=True)
    assert out.splitlines()[0] == "weekly (2026-W36 -> 2026-W38):"
    assert any(ln.startswith("2026-W37") and "█" not in ln
               for ln in out.splitlines())
    assert costs.render_bars([], weekly=False) == "daily (no spend):"


# --- render_detail ---------------------------------------------------------------------------


def _detail_recs():
    return [
        _rec(ts=_ts(2026, 9, 1, 10), session="#47: add costs",
             session_id="ses_aaa", spend=1.00, model="deepseek-v4",
             prompt_tokens=1000, completion_tokens=200),
        _rec(ts=_ts(2026, 9, 2, 11), session="#47: add costs",
             session_id="ses_bbb", spend=2.00, model="kimi-k3",
             prompt_tokens=2000, completion_tokens=300),
        _rec(ts=_ts(2026, 9, 2, 12), session="#47: add costs",
             session_id="ses_bbb", spend=0.17, model="deepseek-v4",
             prompt_tokens=50, completion_tokens=10),
    ]


def test_render_detail_answer_line_and_session_grouping():
    out = costs.render_detail(_detail_recs(), subject="Issue #47", unpriced=0)
    lines = out.splitlines()
    assert lines[0] == "Issue #47: $3.17 across 2 sessions (3 calls)"
    # (session_id, title) grouping: ses_bbb's two calls collapse to one row
    body = [ln for ln in lines if ln.startswith("ses_")]
    assert len(body) == 2
    assert body[0].startswith("ses_aaa")  # chronological by first call
    cols = body[1].split()
    assert cols[0] == "ses_bbb" and cols[1] == "2"  # 2 calls in that group
    assert cols[2] == "2050" and cols[3] == "310"  # tokens summed


def test_render_detail_subtotal_only_for_shared_titles():
    out = costs.render_detail(_detail_recs(), subject="Issue #47", unpriced=0)
    subs = [ln for ln in out.splitlines() if ln.startswith("subtotal")]
    assert len(subs) == 1
    assert "'#47: add costs'" in subs[0] and "$3.17" in subs[0]
    assert "2 sessions" in subs[0] and "(3 calls)" in subs[0]
    unique = costs.render_detail(
        [_rec(session="#1: a", session_id="s1"),
         _rec(session="#2: b", session_id="s2")],
        subject="x", unpriced=0)
    assert "subtotal" not in unique


def test_render_detail_model_breakdown_priced_per_call():
    out = costs.render_detail(_detail_recs(), subject="Issue #47", unpriced=0)
    section = out.split("\nMODEL", 1)[1]
    rows = [ln for ln in section.splitlines()
            if ln.startswith(("deepseek", "kimi"))]
    assert rows[0].startswith("kimi-k3")  # spend desc: 2.00 > 1.17
    assert rows[1].startswith("deepseek-v4")
    assert "$2.00" in rows[0] and "$1.17" in rows[1]


def test_render_detail_total_is_raw_sum_not_sum_of_rounded_rows():
    recs = [_rec(session="#1: a", session_id="s1", spend=0.00426),
            _rec(session="#1: a", session_id="s2", spend=0.00426)]
    out = costs.render_detail(recs, subject="Issue #1", unpriced=0)
    assert out.splitlines()[0] == "Issue #1: $0.0085 across 2 sessions (2 calls)"
    # each row displays $0.0043; summing displayed values would give $0.0086
    assert "$0.0086" not in out


def test_render_detail_unpriced_shown_as_question_mark_and_counted():
    recs = [_rec(session="#1: a", session_id="s1", spend=None),
            _rec(session="#1: a", session_id="s2", spend=0.5)]
    out = costs.render_detail(recs, subject="Issue #1", unpriced=1)
    assert "unpriced: 1 record(s) missing spend" in out.splitlines()[1]
    ses_row = next(ln for ln in out.splitlines() if ln.startswith("s1"))
    assert "?" in ses_row and "$" not in ses_row
    assert out.splitlines()[0] == "Issue #1: $0.5000 across 2 sessions (2 calls)"


def test_render_detail_includes_daily_bars():
    out = costs.render_detail(_detail_recs(), subject="Issue #47", unpriced=0)
    assert "daily (2026-09-01 -> 2026-09-02):" in out


def test_empty_inputs_render():
    assert costs.summarize([])[0].kind == "unattributed"
    assert costs.rollup([], weekly=False) == []
    out = costs.render_detail([], subject="Issue #9", unpriced=0)
    assert out.splitlines()[0] == "Issue #9: $0.0000 across 0 sessions (0 calls)"
    assert "daily (no spend):" in out


# --- cmd_costs wiring (cli/veggies.py) -------------------------------------------


@pytest.fixture()
def local_stack(tmp_path, monkeypatch):
    """A local stack on record; its stack dir is <state_dir>/demo."""
    monkeypatch.setenv("VEGGIES_STATE_DIR", str(tmp_path))
    veggies.State().add(veggies.StackSpec(name="demo", repo="/tmp/demo",
                                          port=4096))
    return tmp_path / "demo"


def _write_segments(stack_dir: Path, segments: dict[str, str]) -> None:
    stack_dir.mkdir(parents=True, exist_ok=True)
    for fname, text in segments.items():
        (stack_dir / fname).write_text(text)


def test_main_costs_unknown_stack(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("VEGGIES_STATE_DIR", str(tmp_path))
    rc = veggies.main(["costs", "nope"])
    assert rc == 1
    assert "unknown stack 'nope' (veggies ls)" in capsys.readouterr().err


def test_main_costs_missing_log_is_a_message_not_an_error(local_stack, capsys):
    rc = veggies.main(["costs", "demo"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "no spend log for stack 'demo' yet" in out
    assert "spend.jsonl" in out  # the expected path is named


def test_main_costs_empty_log(local_stack, capsys):
    _write_segments(local_stack, {"spend.jsonl": ""})
    rc = veggies.main(["costs", "demo"])
    out = capsys.readouterr().out
    assert rc == 0 and "no records yet" in out


def test_main_costs_local_two_segments_concatenated(local_stack, capsys):
    _write_segments(local_stack, {
        "spend.jsonl": _line(session="#47: x", session_id="s1", spend=1.0),
        "spend.jsonl.1": _line(session="#12: y", session_id="s2",
                               spend=2.0) + "\n",
    })
    rc = veggies.main(["costs", "demo"])
    out = capsys.readouterr().out
    assert rc == 0
    header = out.splitlines()[0]
    assert "2 segments (spend.jsonl, spend.jsonl.1)" in header
    assert "$3.00 total, 2 calls" in header
    assert "earliest retained record" in header
    assert "skipped:" not in out  # the segment seam is not a blank line


def test_main_costs_explicit_since_drops_suffix(local_stack, capsys):
    _write_segments(local_stack, {"spend.jsonl": _line(session_id="s1")})
    rc = veggies.main(["costs", "demo", "--since", "2026-09-01"])
    header = capsys.readouterr().out.splitlines()[0]
    assert rc == 0
    assert header.startswith("spend since 2026-09-01 ")
    assert "earliest retained record" not in header


def test_main_costs_since_bogus_is_a_clean_error(local_stack, capsys):
    _write_segments(local_stack, {"spend.jsonl": _line(session_id="s1")})
    rc = veggies.main(["costs", "demo", "--since", "bogus"])
    err = capsys.readouterr().err
    assert rc == 1
    assert "veggies: error:" in err and "--since" in err
    assert "2026-09-01" in err  # an example to copy


def _remote_stack(tmp_path, monkeypatch):
    monkeypatch.setenv("VEGGIES_STATE_DIR", str(tmp_path))
    veggies.State().add(veggies.StackSpec(
        name="rem", repo="https://github.com/org/rem.git", mode="clone",
        host="vps", port=4097))


def test_main_costs_remote_read_routes_through_host_run(tmp_path, monkeypatch,
                                                        capsys):
    _remote_stack(tmp_path, monkeypatch)
    calls = []

    def fake_host_run(host, args, **kw):
        calls.append((host, args, kw))
        payload = ("segments:spend.jsonl spend.jsonl.1\n"
                   + _line(session="#3: z", session_id="s1", spend=0.25) + "\n")
        return subprocess.CompletedProcess(args, 0, stdout=payload, stderr="")

    monkeypatch.setattr(veggies, "host_run", fake_host_run)
    rc = veggies.main(["costs", "rem"])
    out = capsys.readouterr().out
    assert rc == 0
    header = out.splitlines()[0]
    assert "$0.2500 total, 1 calls" in header
    assert "2 segments (spend.jsonl, spend.jsonl.1)" in header
    host, args, kw = calls[0]
    assert host == "vps"
    assert args[:2] == ["sh", "-c"]
    assert args[3:] == ["sh",
                        "/home/stacks/.local/state/veggies/rem/spend.jsonl"]
    assert kw == {"check": False, "capture": True}


def test_main_costs_remote_missing_log(tmp_path, monkeypatch, capsys):
    _remote_stack(tmp_path, monkeypatch)
    monkeypatch.setattr(veggies, "host_run", lambda *a, **k:
                        subprocess.CompletedProcess(a, 3, stdout="", stderr=""))
    rc = veggies.main(["costs", "rem"])
    assert rc == 0 and "no spend log" in capsys.readouterr().out


def test_main_costs_remote_read_failure_is_clean_error(tmp_path, monkeypatch,
                                                       capsys):
    _remote_stack(tmp_path, monkeypatch)
    monkeypatch.setattr(veggies, "host_run", lambda *a, **k:
                        subprocess.CompletedProcess(
                            a, 1, stdout="partial",
                            stderr="cat: /x/spend.jsonl.2: Permission denied"))
    rc = veggies.main(["costs", "rem"])
    err = capsys.readouterr().err
    assert rc == 1
    assert "veggies: error:" in err and "Permission denied" in err
    assert "Traceback" not in err


def test_main_costs_issue_detail_filters(local_stack, capsys):
    _write_segments(local_stack, {"spend.jsonl": "\n".join([
        _line(session="#47: add costs", session_id="s1", spend=1.0),
        _line(session="#12: tidy", session_id="s2", spend=2.0)])})
    rc = veggies.main(["costs", "demo", "--issue", "47"])
    out = capsys.readouterr().out
    assert rc == 0
    assert out.splitlines()[0].startswith("Issue #47: $1.00 across 1 sessions")
    assert "#12" not in out


def test_main_costs_session_substring_case_insensitive(local_stack, capsys):
    _write_segments(local_stack, {"spend.jsonl": "\n".join([
        _line(session="#47: Add Costs", session_id="s1", spend=1.0),
        _line(session="#12: tidy", session_id="s2", spend=2.0)])})
    rc = veggies.main(["costs", "demo", "--session", "costs"])
    out = capsys.readouterr().out
    assert rc == 0
    assert out.splitlines()[0].startswith("Sessions matching 'costs': $1.00")
    assert "#12" not in out


def test_main_costs_weekly_flag(local_stack, capsys):
    _write_segments(local_stack,
                    {"spend.jsonl": _line(session_id="s1", spend=1.0)})
    rc = veggies.main(["costs", "demo", "--weekly"])
    out = capsys.readouterr().out
    assert rc == 0 and "weekly (" in out


# --pr: operator-side gh resolves agent/issue-N (ADR 0035) -----------------------


def _pr_stack(tmp_path, monkeypatch, repo="/tmp/demo"):
    monkeypatch.setenv("VEGGIES_STATE_DIR", str(tmp_path))
    veggies.State().add(veggies.StackSpec(name="demo", repo=repo, port=4096))
    _write_segments(tmp_path / "demo", {"spend.jsonl": "\n".join([
        _line(session="#47: add costs", session_id="s1", spend=1.0),
        _line(session="#12: tidy", session_id="s2", spend=2.0)])})


def _stub_gh(monkeypatch, branch):
    seen = {}

    def fake_run(cmd, **kw):
        seen["cmd"], seen["kw"] = cmd, kw
        return subprocess.CompletedProcess(
            cmd, 0, stdout=json.dumps({"headRefName": branch}), stderr="")

    monkeypatch.setattr(veggies, "run", fake_run)
    monkeypatch.setattr(veggies.shutil, "which",
                        lambda c: "/usr/bin/gh" if c == "gh" else None)
    return seen


def test_main_costs_pr_resolves_agent_branch_url_repo(tmp_path, monkeypatch,
                                                      capsys):
    _pr_stack(tmp_path, monkeypatch, repo="https://github.com/org/demo.git")
    seen = _stub_gh(monkeypatch, "agent/issue-47")
    rc = veggies.main(["costs", "demo", "--pr", "61"])
    out = capsys.readouterr().out
    assert rc == 0
    assert out.splitlines()[0].startswith("Issue #47: $1.00")
    assert seen["cmd"] == ["gh", "pr", "view", "61", "--json", "headRefName",
                           "-R", "org/demo"]


def test_main_costs_pr_mount_mode_uses_repo_cwd(tmp_path, monkeypatch, capsys):
    _pr_stack(tmp_path, monkeypatch, repo="/tmp/demo")
    seen = _stub_gh(monkeypatch, "agent/issue-47")
    rc = veggies.main(["costs", "demo", "--pr", "61"])
    assert rc == 0
    assert seen["cmd"] == ["gh", "pr", "view", "61", "--json", "headRefName"]
    assert seen["kw"].get("cwd") == "/tmp/demo"


def test_main_costs_pr_non_agent_branch_names_it(tmp_path, monkeypatch, capsys):
    _pr_stack(tmp_path, monkeypatch)
    _stub_gh(monkeypatch, "feature/foo")
    rc = veggies.main(["costs", "demo", "--pr", "61"])
    err = capsys.readouterr().err
    assert rc == 1
    assert "feature/foo" in err and "--issue" in err


def test_main_costs_pr_gh_failure_is_named(tmp_path, monkeypatch, capsys):
    _pr_stack(tmp_path, monkeypatch)

    def fake_run(cmd, **kw):
        return subprocess.CompletedProcess(cmd, 4, stdout="",
                                           stderr="no pull requests found")

    monkeypatch.setattr(veggies, "run", fake_run)
    monkeypatch.setattr(veggies.shutil, "which", lambda c: "/usr/bin/gh")
    rc = veggies.main(["costs", "demo", "--pr", "99"])
    err = capsys.readouterr().err
    assert rc == 1 and "no pull requests found" in err and "--issue" in err


def test_main_costs_pr_without_gh_cli(tmp_path, monkeypatch, capsys):
    _pr_stack(tmp_path, monkeypatch)
    monkeypatch.setattr(veggies.shutil, "which", lambda c: None)
    rc = veggies.main(["costs", "demo", "--pr", "61"])
    err = capsys.readouterr().err
    assert rc == 1 and "gh" in err and "--issue" in err
