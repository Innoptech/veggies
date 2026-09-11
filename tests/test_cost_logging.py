"""Cost-metering callback tests (ADR 0022/0051/0052).

The module under test is the litellm proxy custom callback at
agent-config/litellm/custom_callbacks.py; it is not a package, so it is
loaded by file location - same pattern as tests/test_supervise_daemon.py.
The pytest env has no litellm installed; the module's import guard must
still make it importable, and the module-level proxy_handler_instance must
fail open at import time (/stack-state does not exist here).

The record-shape assertions pin the WRITER side of the ADR 0051 contract;
test_emitted_lines_parse_under_the_reader runs real emitted lines through
the landed reader (cli/costs.py) - the conformance pin that matters.
"""

import asyncio
import importlib.util
import json
import logging
import sys
import urllib.parse
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT / "cli"))
import costs  # noqa: E402

_spec = importlib.util.spec_from_file_location(
    "custom_callbacks",
    ROOT / "agent-config" / "litellm" / "custom_callbacks.py")
cc = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(cc)

END = datetime(2026, 9, 11, 12, 34, 56, 789000, tzinfo=timezone.utc)
END_TS = END.timestamp()
START = END - timedelta(seconds=3)

# ADR 0051 required keys; the rest are writer extras readers ignore (0052).
CONTRACT_KEYS = {
    "ts", "model", "prompt_tokens", "completion_tokens", "spend",
    "session", "session_id",
}
EXTRA_KEYS = {
    "call_id", "stack", "status", "caller", "provider_model",
    "key_alias", "tags",
}
SUCCESS_KEYS = CONTRACT_KEYS | EXTRA_KEYS


def make_kwargs(metadata=None, **overrides):
    """A litellm-shaped success/failure kwargs dict."""
    kwargs = {
        "model": "fireworks_ai/accounts/fireworks/models/kimi-k3",
        "response_cost": 0.0123,
        "litellm_call_id": "call-abc-123",
        "litellm_params": {"metadata": metadata if metadata is not None else {}},
    }
    kwargs.update(overrides)
    return kwargs


def make_response(usage=None):
    """A litellm-shaped response_obj: dict-like with a usage dict."""
    return {
        "usage": usage if usage is not None else {
            "prompt_tokens": 11, "completion_tokens": 7, "total_tokens": 18},
    }


def build(kwargs=None, response=None, status="success", stack="mystack",
          start=START, end=END, error=None):
    return cc.build_record(kwargs if kwargs is not None else make_kwargs(),
                           response if response is not None else make_response(),
                           start, end, status, stack, error=error)


def test_success_record_shape():
    metadata = {
        "model_group": "kimi-k3",
        "tags": ["agent", "caller:tag-caller"],
        "session_title": "#46: Meter spend, durably",
        "session_id": "ses_123",
        "caller": "opencode",
    }
    rec = build(make_kwargs(metadata=metadata))
    assert set(rec) == SUCCESS_KEYS
    # Contract keys and their types (ADR 0051).
    assert rec["ts"] == END_TS and isinstance(rec["ts"], float)
    assert rec["model"] == "kimi-k3"  # the router alias, not the provider id
    assert rec["prompt_tokens"] == 11 and isinstance(rec["prompt_tokens"], int)
    assert rec["completion_tokens"] == 7 and isinstance(rec["completion_tokens"], int)
    assert rec["spend"] == 0.0123 and isinstance(rec["spend"], float)
    assert rec["session"] == "#46: Meter spend, durably"
    assert rec["session_id"] == "ses_123"
    # Writer extras (ignored by 0051 readers, kept for forensics).
    assert rec["call_id"] == "call-abc-123"
    assert rec["stack"] == "mystack"
    assert rec["status"] == "success"
    assert rec["caller"] == "opencode"
    assert rec["provider_model"] == "fireworks_ai/accounts/fireworks/models/kimi-k3"
    assert rec["key_alias"] is None
    assert rec["tags"] == ["agent", "caller:tag-caller"]
    # Dropped pre-0051 keys must not come back.
    for gone in ("v", "model_group", "session_title", "total_tokens"):
        assert gone not in rec


def test_model_falls_back_to_provider_id_without_alias():
    rec = build(make_kwargs(metadata={}))
    assert rec["model"] == "fireworks_ai/accounts/fireworks/models/kimi-k3"


def test_session_precedence_and_empty_fallback():
    tag = "session-title:%2346%3A%20tag%20title"
    # Body metadata wins over the tag.
    rec = build(make_kwargs(metadata={"session_title": "body title",
                                      "tags": [tag]}))
    assert rec["session"] == "body title"
    # No metadata title: fall back to the decoded tag.
    rec = build(make_kwargs(metadata={"tags": [tag]}))
    assert rec["session"] == "#46: tag title"
    # Neither: "" (unattributed), never null.
    rec = build(make_kwargs(metadata={"tags": ["other"]}))
    assert rec["session"] == ""


def test_title_tag_uri_decoding():
    # The exact vector from the ADR 0052 stamping convention.
    assert cc._from_tags(
        ["session-title:%2346%3A%20Meter%20spend%2C%20durably"],
        cc.TAG_TITLE_PREFIX) == "#46: Meter spend, durably"
    # Commas and unicode survive a round-trip through urllib.parse.quote.
    for title in ("#46: Meter spend, durably", "café — déjà vu ☕, round 2"):
        tag = cc.TAG_TITLE_PREFIX + urllib.parse.quote(title)
        assert cc._from_tags([tag], cc.TAG_TITLE_PREFIX) == title
    # First matching tag wins.
    tags = [cc.TAG_TITLE_PREFIX + "first", cc.TAG_TITLE_PREFIX + "second"]
    assert cc._from_tags(tags, cc.TAG_TITLE_PREFIX) == "first"
    assert cc._from_tags([], cc.TAG_TITLE_PREFIX) is None
    assert cc._from_tags(None, cc.TAG_TITLE_PREFIX) is None


def test_caller_and_session_id_precedence():
    tags = ["caller:tag-caller", "session-id:tag-session"]
    # Metadata wins over tags.
    rec = build(make_kwargs(metadata={"caller": "meta-caller",
                                      "session_id": "meta-session",
                                      "tags": tags}))
    assert rec["caller"] == "meta-caller"
    assert rec["session_id"] == "meta-session"
    # Tags are the fallback when metadata is absent.
    rec = build(make_kwargs(metadata={"tags": tags}))
    assert rec["caller"] == "tag-caller"
    assert rec["session_id"] == "tag-session"
    # Caller/session-id tags are NOT URI-decoded (raw remainder).
    rec = build(make_kwargs(metadata={"tags": ["caller:a%20b"]}))
    assert rec["caller"] == "a%20b"
    # Neither source: caller None, session_id "" (0051 requires a string).
    rec = build(make_kwargs(metadata={}))
    assert rec["caller"] is None
    assert rec["session_id"] == ""


def test_failure_record():
    err = RuntimeError("boom " * 100)  # long message: must be truncated
    rec = build(status="failure", response=err, error=err)
    assert set(rec) == SUCCESS_KEYS | {"error"}
    assert rec["status"] == "failure"
    # Failure lines stay parseable: 0-token unpriced calls (ADR 0051/0052).
    assert rec["prompt_tokens"] == 0 and isinstance(rec["prompt_tokens"], int)
    assert rec["completion_tokens"] == 0
    assert rec["spend"] is None
    assert rec["error"].startswith("RuntimeError: boom boom ")
    assert len(rec["error"]) == 200
    # Success records have no error key at all.
    assert "error" not in build()


def test_failure_event_reads_kwargs_exception(tmp_path):
    # Seam-level: the proxy's failure path always calls the callback with
    # response_obj=None and the exception at kwargs["exception"]
    # (litellm_logging.py _async_failure_handler_body). The logged error
    # must be the real exception, never "NoneType: None".
    inst = cc.VeggiesCostLogger(path=str(tmp_path / "spend.jsonl"))
    kwargs = make_kwargs()
    kwargs["exception"] = ValueError("boom")
    asyncio.run(inst.async_log_failure_event(kwargs, None, None, END))
    line = (tmp_path / "spend.jsonl").read_text().strip()
    rec = json.loads(line)
    assert rec["error"] == "ValueError: boom"
    assert rec["prompt_tokens"] == 0 and rec["spend"] is None


def test_spend_passthrough():
    # response_cost passes through verbatim: a priced-at-zero model's 0
    # stays 0.0 (not null), and an unpriced model's None stays None.
    rec = build(make_kwargs(response_cost=0))
    assert rec["spend"] == 0.0 and isinstance(rec["spend"], float)
    assert rec["spend"] is not None
    rec = build(make_kwargs(response_cost=None))
    assert rec["spend"] is None


def test_ts_is_epoch_float_with_fallbacks(monkeypatch):
    # Aware end_time converts to the right epoch.
    aware = datetime(2026, 9, 11, 14, 34, 56, 789000,
                     tzinfo=timezone(timedelta(hours=2)))
    assert build(end=aware)["ts"] == END_TS
    # Naive end_time is treated as UTC.
    naive = datetime(2026, 9, 11, 12, 34, 56, 789000)
    assert build(end=naive)["ts"] == END_TS
    # end_time None: start_time takes over; both None: the clock.
    assert build(end=None)["ts"] == START.timestamp()
    monkeypatch.setattr(cc.time, "time", lambda: 42.5)
    assert build(start=None, end=None)["ts"] == 42.5


def test_rotation_caps_segments(tmp_path):
    path = tmp_path / "spend.jsonl"
    handler = cc._make_handler(str(path), max_bytes=200, backup_count=2)
    for i in range(50):  # ~3.5KB total >> 200B cap: forces many rollovers
        line = json.dumps({"i": i, "pad": "x" * 50})
        handler.emit(logging.LogRecord("veggies.cost", logging.INFO,
                                       __file__, 0, line, (), None))
    handler.close()
    files = sorted(p.name for p in tmp_path.iterdir())
    assert "spend.jsonl" in files
    assert "spend.jsonl.1" in files
    assert "spend.jsonl.2" in files
    assert len(files) <= 3  # 1 active + backup_count rolled segments


def test_fail_open_init_and_events_do_not_raise(capsys):
    capsys.readouterr()  # drain the module-import stderr line
    inst = cc.VeggiesCostLogger(path="/nonexistent-dir-xyz/spend.jsonl")
    err = capsys.readouterr().err
    assert "COST METERING DISABLED" in err
    assert "/nonexistent-dir-xyz" in err
    assert inst._handler is None
    # Neither event may raise into the request path, even with no handler.
    asyncio.run(inst.async_log_success_event(
        make_kwargs(), make_response(), START, END))
    asyncio.run(inst.async_log_failure_event(
        make_kwargs(), RuntimeError("boom"), START, END))


def test_emit_after_init_failure_never_raises(capsys):
    inst = cc.VeggiesCostLogger(path="/nonexistent-dir-xyz/spend.jsonl")
    capsys.readouterr()
    inst._emit(build())  # a working record on a broken logger must not raise


def test_emitted_lines_parse_under_the_reader(tmp_path):
    """Conformance pin (ADR 0051/0052): real handler output, parsed by the
    landed reader - every line must parse, every field must land."""
    inst = cc.VeggiesCostLogger(path=str(tmp_path / "spend.jsonl"))
    stamped = {"model_group": "kimi-k3", "session_title": "#46: Meter spend",
               "session_id": "ses_123", "caller": "opencode"}
    # A priced, stamped success.
    asyncio.run(inst.async_log_success_event(
        make_kwargs(metadata=stamped), make_response(), START, END))
    # An unpriced success (response_cost None).
    asyncio.run(inst.async_log_success_event(
        make_kwargs(metadata=stamped, response_cost=None),
        make_response(), START, END))
    # A failure (exception via kwargs, as the proxy's failure path does).
    failed = make_kwargs(metadata=stamped)
    failed["exception"] = ValueError("boom")
    asyncio.run(inst.async_log_failure_event(failed, None, START, END))
    # An unattributed success: no title, no session id.
    asyncio.run(inst.async_log_success_event(
        make_kwargs(), make_response(), START, END))

    parsed = costs.parse_spend_log((tmp_path / "spend.jsonl").read_text())
    assert parsed.skipped == 0
    assert len(parsed.records) == 4
    by_session = {}
    for r in parsed.records:
        by_session.setdefault((r.session, r.session_id), []).append(r)
    stamped_recs = by_session[("#46: Meter spend", "ses_123")]
    assert len(stamped_recs) == 3  # priced + unpriced + failure
    assert stamped_recs[0].model == "kimi-k3"  # the router alias
    assert stamped_recs[0].ts == END_TS
    assert stamped_recs[0].spend == 0.0123
    assert stamped_recs[1].spend is None  # unpriced success
    # The failure surfaces as a 0-token unpriced call.
    failure = stamped_recs[2]
    assert failure.spend is None
    assert failure.prompt_tokens == 0 and failure.completion_tokens == 0
    # The unattributed record lands in the "" bucket with an empty id.
    assert ("", "") in by_session
    assert parsed.unpriced == 2  # unpriced success + failure
