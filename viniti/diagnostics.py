"""Opt-in diagnostics for the VINITI backend.

One small logging layer for every contour (batch, pipeline, parsers, server),
configured through the environment only:

* ``VINITI_LOG_LEVEL`` - ``WARNING`` (default), ``INFO``, ``DEBUG``, ``ERROR``
  or any name ``logging`` knows. An unknown value falls back to ``WARNING``.
* ``VINITI_LOG_FORMAT`` - ``text`` (default) or ``json``, one record per line.

The default is ``WARNING`` on purpose: the CLI already prints its own progress
to stdout, and a second INFO stream on stderr would duplicate it. Turn the
level up explicitly when a run needs stage-by-stage detail.

The module is a leaf: it imports nothing but the standard library, so a CLI
that only needs ``viniti.paths`` still works without pdfplumber. It does not
replace ``print``: CLI output stays on stdout byte for byte, diagnostics go to
stderr (or to the stream a caller passes to :func:`configure`). Every record
is flushed by the handler, so events already written survive a later crash.

Safety rules, enforced in :func:`event` and not left to the caller:

* field names that look like secrets (password, token, cookie, api key, csrf,
  session) or like document bodies (pdf, text, raw, payload, bytes) are
  redacted to ``<redacted>`` before they reach a formatter;
* event names and free messages are flattened to one line and truncated, and
  mappings and sequences are summarised with a depth and item limit, so a
  stray object cannot dump a whole PDF page;
* only counts, identifiers, durations, parser names and short messages are
  meant to be logged. Callers must still never pass page text or raw payloads.

Configuration is explicit, never a side effect of import: the ``viniti``
logger carries a ``NullHandler`` until a program entry point (``batch.main``,
``pipeline.main``, ``server.main``, ``parse_document.main``) calls
:func:`configure`. Without that call the library stays silent instead of
writing bare warnings through ``logging.lastResort``, and importing a module
in a test cannot change the logging state of the process.

The handler is a narrow-console safe ``StreamHandler``: on a cp1251 Windows
console an unencodable character is escaped instead of raising, so logging
never prints ``--- Logging error ---`` or breaks the caller.

Context fields (``area``, ``publication_id``, ``job_id``, ``parser``) travel
through a :class:`contextvars.ContextVar`, so a worker thread or a request
handler can add them once with :func:`context` and every nested event carries
them without repeating the arguments.

Run:  python -X utf8 -c "from viniti import diagnostics as d; d.configure(level='DEBUG'); d.get_logger('x').info('hi')"
      $env:VINITI_LOG_FORMAT="json"; $env:VINITI_LOG_LEVEL="DEBUG"
"""
from __future__ import annotations

import json
import logging
import os
import re
import sys
import threading
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

LOGGER_NAME = "viniti"
LEVEL_ENV = "VINITI_LOG_LEVEL"
FORMAT_ENV = "VINITI_LOG_FORMAT"
DEFAULT_LEVEL = "WARNING"
DEFAULT_FORMAT = "text"

#: Longest string kept in one field; the rest is replaced by an ellipsis.
MAX_FIELD_CHARS = 300
#: Longest escaped traceback kept in one record.
MAX_EXCEPTION_CHARS = 1200
#: Mapping/sequence depth and item limits for the field sanitiser.
MAX_DEPTH = 3
MAX_ITEMS = 12

_context: ContextVar[dict[str, Any]] = ContextVar("viniti_diagnostics", default={})
_lock = threading.Lock()
_configured = False

_REDACTED = "<redacted>"

# Control characters cannot appear in a log line: a newline would split one
# record into two, and an ANSI escape would rewrite the terminal. They are
# escaped as ``\n``, ``\t`` or ``\xNN`` instead of being dropped, so a stray
# character stays visible in the log.
_CONTROL = re.compile(r"[\x00-\x1f\x7f-\x9f\u2028\u2029]")
_ESCAPES = {"\n": "\\n", "\r": "\\r", "\t": "\\t"}

# Whole field names that are never printed, plus prefixes/suffixes that mark a
# derived column (``raw_value``, ``source_text``, ``pdf_bytes``).
_SECRET_NAMES = frozenset({
    "password", "passwd", "pwd", "secret", "token", "cookie", "cookies",
    "authorization", "auth", "csrf", "session", "credential", "credentials",
    "api_key", "apikey", "private_key", "private", "key",
})
_BODY_NAMES = frozenset({
    "pdf", "text", "body", "content", "contents", "payload", "raw", "bytes",
    "blob", "page_text", "page_texts", "fragment", "quote", "source_fragment",
})
_BODY_PREFIXES = ("raw_", "pdf_", "text_", "body_", "content_", "payload_", "bytes_")
_BODY_SUFFIXES = ("_text", "_pdf", "_bytes", "_payload", "_body", "_content",
                  "_fragment", "_quote", "_blob")
_SECRET_PREFIXES = ("api_", "secret_", "auth_", "csrf_", "session_")
_SECRET_SUFFIXES = ("_token", "_secret", "_password", "_key", "_cookie", "_hash")

_LEVELS = {
    name: value for name, value in vars(logging).items()
    if name.isupper() and isinstance(value, int) and not name.startswith("_")
}

# Silence by default: a library must not emit through ``lastResort``. The real
# handler is installed by :func:`configure` from an entry point only.
logging.getLogger(LOGGER_NAME).addHandler(logging.NullHandler())
logging.getLogger(LOGGER_NAME).propagate = False


def _field_key(name: Any) -> str:
    """Normalise a field name to ``snake_case`` for the sensitivity check."""
    return re.sub(r"[^a-z0-9]+", "_", str(name).strip().lower()).strip("_")


def _is_sensitive(name: Any) -> bool:
    key = _field_key(name)
    if not key:
        return False
    if key in _SECRET_NAMES or key in _BODY_NAMES:
        return True
    if key.startswith(_BODY_PREFIXES) or key.endswith(_BODY_SUFFIXES):
        return True
    return key.startswith(_SECRET_PREFIXES) or key.endswith(_SECRET_SUFFIXES)


#: Credential shapes that may appear inside a free message or a traceback.
_SECRET_PATTERN = re.compile(
    r"(?i)\b(password|passwd|pwd|token|secret|api[_-]?key|apikey|cookie|authorization)"
    r"(\s*[:=]\s*)(\S+)"
)
_BEARER_PATTERN = re.compile(r"(?i)\b(bearer)\s+(\S+)")
_KEY_PATTERN = re.compile(r"\b(sk-[A-Za-z0-9_\-]{8,})")


def _escape_controls(text: str) -> str:
    """Escape control characters so the value cannot add a log line."""
    def replace(match: re.Match[str]) -> str:
        char = match.group(0)
        return _ESCAPES.get(char) or "\\x%02x" % ord(char)

    return _CONTROL.sub(replace, text)


def _clean_text(value: str) -> str:
    """One line, bounded: a page of PDF text cannot leak through a message."""
    flat = _scrub_secrets(_escape_controls(value).strip())
    if len(flat) > MAX_FIELD_CHARS:
        return flat[:MAX_FIELD_CHARS] + "..."
    return flat


def _scrub_secrets(text: str) -> str:
    """Best-effort redaction of credentials quoted inside free text."""
    text = _SECRET_PATTERN.sub(lambda m: f"{m.group(1)}{m.group(2)}<redacted>", text)
    text = _BEARER_PATTERN.sub(lambda m: f"{m.group(1)} <redacted>", text)
    return _KEY_PATTERN.sub("<redacted>", text)


def safe_value(value: Any, *, depth: int = 0) -> Any:
    """Return a JSON/text friendly copy of ``value`` with no unbounded payloads."""
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        return _clean_text(value)
    if isinstance(value, Path):
        return _clean_text(str(value))
    if depth >= MAX_DEPTH:
        return f"<{type(value).__name__}>"
    if isinstance(value, Mapping):
        result = {}
        for index, (key, item) in enumerate(value.items()):
            if index >= MAX_ITEMS:
                result["..."] = f"{len(value) - MAX_ITEMS} more"
                break
            result[str(key)] = _REDACTED if _is_sensitive(key) else safe_value(item, depth=depth + 1)
        return result
    if isinstance(value, (list, tuple, set, frozenset)):
        items = list(value)
        result = [safe_value(item, depth=depth + 1) for item in items[:MAX_ITEMS]]
        if len(items) > MAX_ITEMS:
            result.append(f"... {len(items) - MAX_ITEMS} more")
        return result
    return _clean_text(repr(value))


def safe_fields(fields: Mapping[str, Any] | None) -> dict[str, Any]:
    """Sanitise a mapping of structured fields for one log record."""
    result: dict[str, Any] = {}
    for key, value in (fields or {}).items():
        name = str(key)
        result[name] = _REDACTED if _is_sensitive(name) else safe_value(value)
    return result


def current_context() -> dict[str, Any]:
    """Copy of the context fields bound on this thread/task."""
    return dict(_context.get())


@contextmanager
def context(**fields: Any) -> Iterator[dict[str, Any]]:
    """Bind context fields for the duration of the ``with`` block.

    Nested blocks merge, they do not replace: a per-publication context inside
    a per-area context keeps both. The previous mapping is restored on exit,
    including on an exception.
    """
    token = _context.set({**_context.get(), **fields})
    try:
        yield current_context()
    finally:
        _context.reset(token)


def _timestamp(created: float) -> str:
    return datetime.fromtimestamp(created, timezone.utc).isoformat(timespec="milliseconds")


def _exception_text(record: logging.LogRecord) -> str:
    """Traceback on one bounded line, with secrets and controls escaped.

    ``logging.Formatter.formatException`` returns several lines and quotes the
    exception message verbatim, so a message built from document text would
    both break the one-record-per-line contract and leak page content. Here the
    text is flattened, scrubbed of credential shapes, and capped; callers still
    must not raise exceptions whose message is a PDF fragment.
    """
    text = record.exc_text
    if not text and record.exc_info and any(record.exc_info):
        text = logging.Formatter().formatException(record.exc_info)
    if not text:
        return ""
    flat = _scrub_secrets(_escape_controls(text.strip()))
    if len(flat) > MAX_EXCEPTION_CHARS:
        return flat[:MAX_EXCEPTION_CHARS] + "..."
    return flat


def _render(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, (bool, int, float)) or value is None:
        return str(value)
    return json.dumps(value, ensure_ascii=False, default=str)


class SafeStreamHandler(logging.StreamHandler):
    """``StreamHandler`` that survives a narrow console code page.

    A Windows console redirected to a pipe uses the locale encoding (cp1251
    here), so one unencodable character would make ``StreamHandler.emit`` raise
    and print ``--- Logging error ---``. This handler encodes first and replaces
    what the stream cannot represent; a diagnostics failure never propagates
    into the caller.
    """

    def emit(self, record: logging.LogRecord) -> None:
        try:
            message = self.format(record) + self.terminator
            encoding = getattr(self.stream, "encoding", None) or "utf-8"
            try:
                message.encode(encoding)
            except (UnicodeEncodeError, LookupError):
                # Not ``encode(..., "replace")``: replacing with "?" would make
                # the word unreadable. Escaping keeps the characters visible.
                message = message.encode(encoding, "backslashreplace").decode(
                    encoding, "backslashreplace")
            self.stream.write(message)
            self.flush()
        except Exception:  # noqa: BLE001 - logging must never break the caller
            pass


class TextFormatter(logging.Formatter):
    """``2026-11-22T10:00:00.000Z INFO viniti.batch event=job.ok area=solar``."""

    def format(self, record: logging.LogRecord) -> str:
        event = _clean_text(getattr(record, "event", "") or record.getMessage())
        fields = getattr(record, "fields", None) or {}
        parts = [_timestamp(record.created), record.levelname, record.name]
        if event:
            parts.append(f"event={event}")
        if record.threadName and record.threadName != "MainThread":
            parts.append(f"thread={record.threadName}")
        for key, value in fields.items():
            parts.append(f"{key}={_render(value)}")
        line = " ".join(part for part in parts if part)
        traceback = _exception_text(record)
        if traceback:
            # One physical line: the traceback is escaped and bounded above.
            line = line + " exception=" + traceback
        return line


class JsonFormatter(logging.Formatter):
    """One JSON object per line, safe to pipe into a log collector."""

    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": _timestamp(record.created),
            "level": record.levelname,
            "logger": record.name,
            "event": _clean_text(getattr(record, "event", "") or ""),
            "message": _clean_text(record.getMessage()),
            "thread": record.threadName,
            "fields": getattr(record, "fields", None) or {},
        }
        if record.exc_info or record.exc_text:
            payload["exception"] = _exception_text(record)
        return json.dumps(payload, ensure_ascii=False, default=str)


def resolve_level(raw: str | int | None) -> int:
    """Environment value to a ``logging`` level; unknown names fall back to WARNING."""
    if isinstance(raw, int):
        return raw
    name = (raw or DEFAULT_LEVEL).strip().upper()
    return _LEVELS.get(name, logging.WARNING)


def resolve_format(raw: str | None) -> str:
    name = (raw or DEFAULT_FORMAT).strip().lower()
    return name if name in {"text", "json"} else DEFAULT_FORMAT


def configure(
    *,
    level: str | int | None = None,
    fmt: str | None = None,
    stream: Any = None,
    force: bool = False,
) -> logging.Logger:
    """Install one stderr handler on the ``viniti`` logger; idempotent.

    Explicit arguments win over the environment, the environment wins over the
    defaults. ``force=True`` rebuilds the handler, which tests use to capture
    output; normal callers never need it. There is no log file: records go to
    the stream given here (``sys.stderr`` by default) and every record is
    flushed, so output already written survives a later crash. The handler
    degrades to ``backslashreplace`` on a narrow console code page instead of
    raising ``UnicodeEncodeError``.
    """
    global _configured
    with _lock:
        logger = logging.getLogger(LOGGER_NAME)
        if _configured and not force:
            return logger
        level_value = resolve_level(level if level is not None else os.environ.get(LEVEL_ENV))
        format_value = resolve_format(fmt if fmt is not None else os.environ.get(FORMAT_ENV))
        handler = SafeStreamHandler(stream if stream is not None else sys.stderr)
        handler.setFormatter(JsonFormatter() if format_value == "json" else TextFormatter())
        logger.handlers[:] = [handler]
        logger.setLevel(level_value)
        logger.propagate = False
        _configured = True
        return logger


def reset() -> None:
    """Drop the installed handler and configuration (tests only)."""
    global _configured
    with _lock:
        logger = logging.getLogger(LOGGER_NAME)
        logger.handlers[:] = [logging.NullHandler()]
        logger.setLevel(logging.NOTSET)
        logger.propagate = False
        _configured = False
    _context.set({})


def get_logger(module: str = "") -> logging.Logger:
    """Module logger under ``viniti``.

    This never configures the layer: until an entry point calls
    :func:`configure` the ``viniti`` logger only holds a ``NullHandler``, so a
    library import cannot print or change process-wide logging state.
    """
    return logging.getLogger(f"{LOGGER_NAME}.{module}" if module else LOGGER_NAME)


def _merged(fields: Mapping[str, Any] | None) -> dict[str, Any]:
    merged = dict(_context.get())
    merged.update(fields or {})
    return safe_fields(merged)


def event(logger: logging.Logger, name: str, level: int = logging.INFO, **fields: Any) -> None:
    """Emit a structured event with the active context and sanitised fields."""
    if not logger.isEnabledFor(level):
        return
    label = _clean_text(name)
    logger.log(level, label, extra={"event": label, "fields": _merged(fields)})


def warning(logger: logging.Logger, name: str, **fields: Any) -> None:
    event(logger, name, level=logging.WARNING, **fields)


def error(logger: logging.Logger, name: str, **fields: Any) -> None:
    event(logger, name, level=logging.ERROR, **fields)


def exception(logger: logging.Logger, name: str, **fields: Any) -> None:
    """Log an active exception with its traceback; call from an ``except`` block."""
    label = _clean_text(name)
    logger.exception(label, extra={"event": label, "fields": _merged(fields)})


def debug(logger: logging.Logger, name: str, **fields: Any) -> None:
    event(logger, name, level=logging.DEBUG, **fields)
