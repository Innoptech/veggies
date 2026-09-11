"""Per-call cost metering writer for the litellm proxy (ADR 0022/0047).

The pinned litellm proxy loads this module via the config key
`litellm_settings.callbacks: ["custom_callbacks.proxy_handler_instance"]`
(list form - a bare string would replace litellm.callbacks, evicting the
proxy's built-in budget limiter).
(litellm imports the module from the config file's directory). One JSON
line per completed/failed model call is appended to a size-rotated JSONL
file (default /costs/costs.jsonl, host-mounted into the container).

Invariants:
- Fail-open: metering NEVER raises into the request path. A broken log
  degrades to a stderr line, never a failed model call.
- Single writer: this callback is the ONLY writer of the cost log;
  rotation correctness assumes a single writer process.

Only the async event methods are implemented: the proxy never takes the
sync path, so log_success_event/log_failure_event would be dead code.
"""

import json
import logging
import logging.handlers
import os
import sys
import urllib.parse
from datetime import timezone

try:
    from litellm.integrations.custom_logger import CustomLogger
except ImportError:  # pytest env has no litellm; the proxy image does
    class CustomLogger:  # type: ignore[no-redef]
        """Structural stand-in so the module imports without litellm."""

SCHEMA_VERSION = 1
MAX_BYTES = 10 * 1024 * 1024
BACKUP_COUNT = 5
TAG_TITLE_PREFIX = "session-title:"
TAG_CALLER_PREFIX = "caller:"
TAG_SESSION_PREFIX = "session-id:"


def _from_tags(tags, prefix):
    """First matching tag's remainder; title tags are URI-decoded."""
    for tag in tags or []:
        if isinstance(tag, str) and tag.startswith(prefix):
            remainder = tag[len(prefix):]
            if prefix == TAG_TITLE_PREFIX:
                return urllib.parse.unquote(remainder)
            return remainder
    return None


def _iso_utc(dt):
    """UTC ISO-8601 (milliseconds, +00:00 suffix); naive means UTC."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat(timespec="milliseconds")


def _usage_dict(response_obj):
    """Usage as a plain dict, from a dict or pydantic-ish response_obj."""
    if hasattr(response_obj, "model_dump"):
        response_obj = response_obj.model_dump()
    if isinstance(response_obj, dict):
        usage = response_obj.get("usage")
    else:
        usage = getattr(response_obj, "usage", None)
    if hasattr(usage, "model_dump"):
        usage = usage.model_dump()
    return usage if isinstance(usage, dict) else None


def build_record(kwargs, response_obj, end_time, status, stack, error=None):
    """Build the cost-log record dict for one model call.

    On failure the usage/spend keys are present with value None (explicit
    nulls, so readers see one stable schema) and an `error` key is added;
    success records have no `error` key.
    """
    metadata = kwargs.get("litellm_params", {}).get("metadata", {}) or {}
    tags = metadata.get("tags") or []
    if status == "success":
        usage = _usage_dict(response_obj)
        # Verbatim passthrough: unpriced models report None, priced-at-zero
        # report 0 - both must survive so readers can distinguish them.
        spend = kwargs.get("response_cost")
    else:
        usage, spend = None, None
    record = {
        "v": SCHEMA_VERSION,
        "ts": _iso_utc(end_time),
        "call_id": kwargs.get("litellm_call_id"),
        "stack": stack,
        "status": status,
        "caller": metadata.get("caller")
                  or _from_tags(tags, TAG_CALLER_PREFIX) or None,
        "model": kwargs.get("model"),
        "model_group": metadata.get("model_group") or None,
        "prompt_tokens": usage.get("prompt_tokens") if usage else None,
        "completion_tokens": usage.get("completion_tokens") if usage else None,
        "total_tokens": usage.get("total_tokens") if usage else None,
        "spend": spend,
        "session_id": metadata.get("session_id")
                      or _from_tags(tags, TAG_SESSION_PREFIX) or None,
        "session_title": metadata.get("session_title")
                         or _from_tags(tags, TAG_TITLE_PREFIX) or None,
        "key_alias": metadata.get("user_api_key_alias") or None,
        "tags": tags,
    }
    if status == "failure":
        record["error"] = f"{type(error).__name__}: {error}"[:200]
    return record


def _make_handler(path, max_bytes=MAX_BYTES, backup_count=BACKUP_COUNT):
    """A RotatingFileHandler whose formatter emits the message only."""
    handler = logging.handlers.RotatingFileHandler(
        path, maxBytes=max_bytes, backupCount=backup_count)
    handler.setFormatter(logging.Formatter("%(message)s"))
    return handler


class VeggiesCostLogger(CustomLogger):
    """Appends one JSON line per model call to the cost log. Fail-open."""

    def __init__(self, path=None):
        # Tolerated by the real CustomLogger and the ImportError stand-in;
        # future litellm bumps may make the base stateful.
        super().__init__()
        self._path = path or os.environ.get("VEGGIES_COST_LOG",
                                            "/costs/costs.jsonl")
        self._stack = os.environ.get("VEGGIES_STACK", "")
        try:
            self._handler = _make_handler(self._path)
        except Exception as exc:  # missing dir, EACCES, ... -> fail open
            print(f"COST METERING DISABLED: cannot open {self._path}: "
                  f"{exc}; model calls are unaffected", file=sys.stderr)
            self._handler = None
        else:
            print(f"cost metering: appending to {self._path} (10MiB x 5)",
                  file=sys.stderr)

    def _emit(self, record):
        """Append one record; never raises (one stderr line per failure)."""
        if self._handler is None:
            return
        try:
            self._handler.handle(logging.LogRecord(
                "veggies.cost", logging.INFO, __file__, 0,
                json.dumps(record), (), None))
        except Exception as exc:
            print(f"cost metering: dropping record: {exc}", file=sys.stderr)

    async def async_log_success_event(self, kwargs, response_obj,
                                      start_time, end_time):
        try:
            self._emit(build_record(kwargs, response_obj, end_time,
                                    "success", self._stack))
        except Exception as exc:
            print(f"cost metering: success-event failed: {exc}",
                  file=sys.stderr)

    async def async_log_failure_event(self, kwargs, response_obj,
                                      start_time, end_time):
        try:
            # The proxy's failure path always calls this with
            # response_obj=None; the exception lives at kwargs["exception"]
            # (litellm_logging.py _async_failure_handler_body). response_obj
            # stays as a fallback for any other caller.
            error = kwargs.get("exception") or response_obj
            self._emit(build_record(kwargs, response_obj, end_time,
                                    "failure", self._stack,
                                    error=error))
        except Exception as exc:
            print(f"cost metering: failure-event failed: {exc}",
                  file=sys.stderr)


proxy_handler_instance = VeggiesCostLogger()
