"""Cost-metering callback tests (ADR 0022/0044).

The module under test is the litellm proxy custom callback at
agent-config/litellm/custom_callbacks.py; it is not a package, so it is
loaded by file location - same pattern as tests/test_supervise_daemon.py.
The pytest env has no litellm installed; the module's import guard must
still make it importable, and the module-level proxy_handler_instance must
fail open at import time (/costs does not exist here).
"""

import asyncio
import importlib.util
import json
import logging
import urllib.parse
from datetime import datetime, timedelta, timezone
from pathlib import Path

_spec = importlib.util.spec_from_file_location(
    "custom_callbacks",
    Path(__file__).parent.parent / "agent-config" / "litellm" / "custom_callbacks.py")
cc = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(cc)

END = datetime(2026, 9, 11, 12, 34, 56, 789000, tzinfo=timezone.utc)
END_ISO = "2026-09-11T12:34:56.789+00:00"

SUCCESS_KEYS = {
    "v", "ts", "call_id", "stack", "status", "caller", "model",
    "model_group", "prompt_tokens", "completion_tokens", "total_tokens",
    "spend", "session_id", "session_title", "key_alias", "tags",
}


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


def test_success_record_shape():
    metadata = {
        "model_group": "kimi-k3",
        "tags": ["agent", "caller:tag-caller"],
        "session_title": "#46: Meter spend, durably",
        "session_id": "ses_123",
        "caller": "opencode",
    }
    rec = cc.build_record(make_kwargs(metadata=metadata), make_response(),
                          END, "success", "mystack")
    assert set(rec) == SUCCESS_KEYS
    assert rec["v"] == 1
    assert rec["ts"] == END_ISO
    assert rec["call_id"] == "call-abc-123"
    assert rec["stack"] == "mystack"
    assert rec["status"] == "success"
    assert rec["caller"] == "opencode"
    assert rec["model"] == "fireworks_ai/accounts/fireworks/models/kimi-k3"
    assert rec["model_group"] == "kimi-k3"
    assert rec["prompt_tokens"] == 11
    assert rec["completion_tokens"] == 7
    assert rec["total_tokens"] == 18
    assert rec["spend"] == 0.0123
    assert rec["session_id"] == "ses_123"
    assert rec["session_title"] == "#46: Meter spend, durably"
    assert rec["key_alias"] is None
    assert rec["tags"] == ["agent", "caller:tag-caller"]


def test_session_title_precedence():
    tag = "session-title:%2346%3A%20tag%20title"
    # Body metadata wins over the tag.
    rec = cc.build_record(
        make_kwargs(metadata={"session_title": "body title", "tags": [tag]}),
        make_response(), END, "success", "")
    assert rec["session_title"] == "body title"
    # No metadata title: fall back to the decoded tag.
    rec = cc.build_record(make_kwargs(metadata={"tags": [tag]}),
                          make_response(), END, "success", "")
    assert rec["session_title"] == "#46: tag title"
    # Neither: None.
    rec = cc.build_record(make_kwargs(metadata={"tags": ["other"]}),
                          make_response(), END, "success", "")
    assert rec["session_title"] is None


def test_title_tag_uri_decoding():
    # The exact vector from the ADR 0044 stamping convention.
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
    rec = cc.build_record(
        make_kwargs(metadata={"caller": "meta-caller",
                              "session_id": "meta-session", "tags": tags}),
        make_response(), END, "success", "")
    assert rec["caller"] == "meta-caller"
    assert rec["session_id"] == "meta-session"
    # Tags are the fallback when metadata is absent.
    rec = cc.build_record(make_kwargs(metadata={"tags": tags}),
                          make_response(), END, "success", "")
    assert rec["caller"] == "tag-caller"
    assert rec["session_id"] == "tag-session"
    # Caller/session-id tags are NOT URI-decoded (raw remainder).
    rec = cc.build_record(make_kwargs(metadata={"tags": ["caller:a%20b"]}),
                          make_response(), END, "success", "")
    assert rec["caller"] == "a%20b"
    # Neither source: None.
    rec = cc.build_record(make_kwargs(metadata={}), make_response(),
                          END, "success", "")
    assert rec["caller"] is None
    assert rec["session_id"] is None


def test_failure_record():
    err = RuntimeError("boom " * 100)  # long message: must be truncated
    rec = cc.build_record(make_kwargs(), err, END, "failure", "mystack",
                          error=err)
    assert set(rec) == SUCCESS_KEYS | {"error"}
    assert rec["status"] == "failure"
    assert rec["prompt_tokens"] is None
    assert rec["completion_tokens"] is None
    assert rec["total_tokens"] is None
    assert rec["spend"] is None
    assert rec["error"].startswith("RuntimeError: boom boom ")
    assert len(rec["error"]) == 200
    # Success records have no error key at all.
    rec_ok = cc.build_record(make_kwargs(), make_response(),
                             END, "success", "mystack")
    assert "error" not in rec_ok


def test_zero_spend_recorded_as_zero():
    # Unpriced models report response_cost=0; that must not read as missing.
    rec = cc.build_record(make_kwargs(response_cost=0), make_response(),
                          END, "success", "")
    assert rec["spend"] == 0
    assert rec["spend"] is not None


def test_end_time_timezone_handling():
    # Naive end_time is treated as UTC.
    naive = datetime(2026, 9, 11, 12, 34, 56, 789000)
    rec = cc.build_record(make_kwargs(), make_response(), naive, "success", "")
    assert rec["ts"] == END_ISO
    # Aware end_time is converted to UTC.
    aware = datetime(2026, 9, 11, 14, 34, 56, 789000,
                     tzinfo=timezone(timedelta(hours=2)))
    rec = cc.build_record(make_kwargs(), make_response(), aware, "success", "")
    assert rec["ts"] == END_ISO
    assert rec["ts"].endswith("+00:00")


def test_rotation_caps_segments(tmp_path):
    path = tmp_path / "costs.jsonl"
    handler = cc._make_handler(str(path), max_bytes=200, backup_count=2)
    for i in range(50):  # ~3.5KB total >> 200B cap: forces many rollovers
        line = json.dumps({"i": i, "pad": "x" * 50})
        handler.emit(logging.LogRecord("veggies.cost", logging.INFO,
                                       __file__, 0, line, (), None))
    handler.close()
    files = sorted(p.name for p in tmp_path.iterdir())
    assert "costs.jsonl" in files
    assert "costs.jsonl.1" in files
    assert "costs.jsonl.2" in files
    assert len(files) <= 3  # 1 active + backup_count rolled segments


def test_fail_open_init_and_events_do_not_raise(capsys):
    capsys.readouterr()  # drain the module-import stderr line
    inst = cc.VeggiesCostLogger(path="/nonexistent-dir-xyz/costs.jsonl")
    err = capsys.readouterr().err
    assert "COST METERING DISABLED" in err
    assert "/nonexistent-dir-xyz" in err
    assert inst._handler is None
    # Neither event may raise into the request path, even with no handler.
    asyncio.run(inst.async_log_success_event(
        make_kwargs(), make_response(), None, END))
    asyncio.run(inst.async_log_failure_event(
        make_kwargs(), RuntimeError("boom"), None, END))


def test_emit_after_init_failure_never_raises(capsys):
    inst = cc.VeggiesCostLogger(path="/nonexistent-dir-xyz/costs.jsonl")
    capsys.readouterr()
    rec = cc.build_record(make_kwargs(), make_response(), END, "success", "s")
    inst._emit(rec)  # a working record on a broken logger must not raise
