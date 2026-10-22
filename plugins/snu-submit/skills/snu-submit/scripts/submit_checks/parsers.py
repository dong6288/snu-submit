"""Bounded byte-oriented parsers. Bootstrap: UTF-8 plaintext only.

parse_bytes() never opens caller paths, writes files of its own, uses the
network or changes global state. It validates input in the calling process,
then runs the raw parser in one spawned worker process with a wall-clock
timeout. The bounded JSON result returns through multiprocessing shared
memory (RawArray). On Windows that memory is an anonymous mapping; on POSIX
the standard library backs it with a file it unlinks immediately, in /dev/shm
when available, otherwise in the temp directory, so parsed text can exist
there in memory-mapped form until the call ends. It returns neutral facts; the
core maps them to report fields. PDF/DOCX/ZIP parsing is not implemented yet,
so those inputs return status "incomplete" with an explicit unsupported reason.

Callers that run this from a script must guard their entry point with
`if __name__ == "__main__":` because the worker uses the "spawn" start method.
"""

import ctypes
import io
import json
import logging
import math
import multiprocessing
import time
import unicodedata
from contextlib import redirect_stderr, redirect_stdout

MAX_INPUT_BYTES = 1024 * 1024
MAX_TEXT_CHARS = 100_000
MAX_COMBINING_RUN = 64
MAX_REASON_CHARS = 512
MAX_FILENAME_CHARS = 1024
DEFAULT_LIMITS = {"max_input_bytes": MAX_INPUT_BYTES, "max_text_chars": MAX_TEXT_CHARS}
TIMEOUT_KEY = "timeout_seconds"
DEFAULT_TIMEOUT_SECONDS = 30
MAX_TIMEOUT_SECONDS = 30
MAX_IPC_BYTES = 4 * 1024 * 1024
MAX_RESULT_ITEMS = 10_000
MAX_CAPTURED_OUTPUT_CHARS = 2048
JOIN_GRACE_SECONDS = 2.0
RESULT_KEYS = frozenset({"format", "status", "reason", "metrics", "pages", "text", "locations", "unread_regions", "archive_entries"})
METRIC_KEYS = frozenset({"size_bytes", "text_chars", "text_chars_no_whitespace", "text_words", "encoding"})
STATUSES = frozenset({"complete", "incomplete", "failed"})
_IN_WORKER = False
TEXT_EXTENSIONS = frozenset({".txt", ".md", ".csv", ".py", ".json"})
SIGNATURES = (
    (b"%PDF-", "pdf"),
    (b"PK\x03\x04", "zip"),
    (b"PK\x05\x06", "zip"),
    (b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1", "ole_compound"),
    (b"\x89PNG\r\n\x1a\n", "png"),
    (b"\xff\xd8\xff", "jpeg"),
)
EXPECTED_SIGNATURE = {".pdf": "pdf", ".docx": "zip", ".zip": "zip", ".hwpx": "zip", ".hwp": "ole_compound"}
UTF8_BOM = b"\xef\xbb\xbf"


def _unread(kind, detail):
    return {"kind": kind, "location": None, "detail": str(detail)[:MAX_REASON_CHARS]}


def _result(fmt, status, reason, size_bytes, unread_kind=None):
    unread = [_unread(unread_kind, reason)] if unread_kind and reason else []
    return {
        "format": fmt,
        "status": status,
        "reason": None if reason is None else str(reason)[:MAX_REASON_CHARS],
        "metrics": {
            "size_bytes": size_bytes,
            "text_chars": None,
            "text_chars_no_whitespace": None,
            "text_words": None,
            "encoding": None,
        },
        "pages": None,
        "text": None,
        "locations": [],
        "unread_regions": unread,
        "archive_entries": [],
    }


def _validate_limits(limits):
    """Return (limits, error). Only known keys with positive int values <= caps."""
    if limits is None:
        return dict(DEFAULT_LIMITS), None
    if type(limits) is not dict:
        return None, "limits must be a dict or None"
    merged = dict(DEFAULT_LIMITS)
    for key, value in limits.items():
        if type(key) is not str or key not in DEFAULT_LIMITS:
            return None, "unknown limit key"
        if type(value) is not int or value <= 0 or value > DEFAULT_LIMITS[key]:
            return None, f"limit {key} must be a positive integer <= {DEFAULT_LIMITS[key]}"
        merged[key] = value
    return merged, None


def _extension(filename):
    base = filename.replace("\\", "/").rsplit("/", 1)[-1]
    dot = base.rfind(".")
    return base[dot:].lower() if dot > 0 else ""


def _detect_signature(data):
    for magic, fmt in SIGNATURES:
        if data.startswith(magic):
            return fmt
    return None


def _combining_run_exceeded(text):
    run = 0
    for ch in text:
        if unicodedata.category(ch) in ("Mn", "Mc", "Me"):
            run += 1
            if run > MAX_COMBINING_RUN:
                return True
        else:
            run = 0
    return False


def _parse_raw(data, filename, limits=None):
    """Raw in-process text parser. Runs only inside the worker; never spawns."""
    if type(data) not in (bytes, bytearray):
        return _result("unknown", "failed", "data must be bytes", None, "other")
    if type(filename) is not str or len(filename) > MAX_FILENAME_CHARS:
        return _result("unknown", "failed", "filename must be a bounded string", len(data), "other")
    limits, error = _validate_limits(limits)
    if error:
        return _result("unknown", "failed", error, len(data), "other")
    data = bytes(data)
    size = len(data)
    if size == 0:
        return _result("empty", "incomplete", "empty input; no content to read", 0, "other")

    ext = _extension(filename)
    detected = _detect_signature(data)
    expected = EXPECTED_SIGNATURE.get(ext)
    if expected is not None and detected != expected:
        reason = f"declared extension {ext} expects {expected} signature; detected {detected or 'none'}"
        return _result(detected or "unknown", "incomplete", reason, size, "unsupported_format")
    if detected is not None:
        reason = f"{detected} content (extension {ext or 'none'}) is not parsed by this bootstrap parser"
        return _result(detected, "incomplete", reason, size, "unsupported_format")
    if ext not in TEXT_EXTENSIONS:
        reason = f"no supported parser for extension {ext or 'none'}; content not read"
        return _result("unknown", "incomplete", reason, size, "unsupported_format")

    if size > limits["max_input_bytes"]:
        reason = f"input {size} bytes exceeds parser budget {limits['max_input_bytes']} bytes; not read"
        return _result("text", "incomplete", reason, size, "budget_limit")
    encoding = "utf-8-sig" if data.startswith(UTF8_BOM) else "utf-8"
    try:
        text = data.decode(encoding, errors="strict")
    except UnicodeDecodeError as exc:
        return _result("unknown", "failed", f"not valid UTF-8 at byte offset {exc.start}", size, "parse_error")
    if "\x00" in text:
        return _result("unknown", "failed", "NUL character found; likely binary data", size, "parse_error")
    if len(text) > limits["max_text_chars"]:
        reason = f"decoded text exceeds {limits['max_text_chars']} characters; not normalized"
        return _result("text", "incomplete", reason, size, "budget_limit")
    if _combining_run_exceeded(text):
        reason = f"combining-mark run longer than {MAX_COMBINING_RUN}; not normalized"
        return _result("text", "incomplete", reason, size, "parse_error")

    norm = unicodedata.normalize("NFC", text).replace("\r\n", "\n").replace("\r", "\n")
    if len(norm) > limits["max_text_chars"]:
        reason = f"normalized text exceeds {limits['max_text_chars']} characters"
        return _result("text", "incomplete", reason, size, "budget_limit")
    if not norm:
        return _result("empty", "incomplete", "decoded text is empty; no content to read", size, "other")

    result = _result("text", "complete", None, size)
    result["metrics"].update({
        # Unicode code points of the normalized text excluding LF (CR already folded).
        "text_chars": len(norm) - norm.count("\n"),
        # Code points that are not Unicode whitespace (str.isspace).
        "text_chars_no_whitespace": sum(1 for ch in norm if not ch.isspace()),
        # Tokens separated by Unicode whitespace (str.split); not a linguistic count.
        "text_words": len(norm.split()),
        "encoding": encoding,
    })
    result["text"] = norm
    # Zero-based code point offsets into `text`, start inclusive, end exclusive.
    result["locations"] = [{"char_start": 0, "char_end": len(norm), "label": "whole_text"}]
    return result


# ---------------------------------------------------------------------------
# Worker process boundary
# ---------------------------------------------------------------------------

class _BoundedSink(io.TextIOBase):
    """Swallows worker stdout/stderr, keeping at most a small prefix."""

    def __init__(self):
        super().__init__()
        self._parts = []
        self._size = 0

    def writable(self):
        return True

    def write(self, s):
        s = str(s)
        room = MAX_CAPTURED_OUTPUT_CHARS - self._size
        if room > 0:
            self._parts.append(s[:room])
            self._size += min(len(s), room)
        return len(s)

    def getvalue(self):
        return "".join(self._parts)


def _encode_result(result, size):
    """Serialize strictly; replace oversize/non-finite results with a failed result."""
    try:
        payload = json.dumps(result, allow_nan=False, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    except (TypeError, ValueError) as exc:
        result = _result("unknown", "failed", f"result not serializable: {type(exc).__name__}", size, "parse_error")
        payload = json.dumps(result, allow_nan=False, separators=(",", ":")).encode("utf-8")
    if len(payload) > MAX_IPC_BYTES:
        result = _result("unknown", "incomplete", "parser result exceeded IPC size cap", size, "budget_limit")
        payload = json.dumps(result, allow_nan=False, separators=(",", ":")).encode("utf-8")
    return payload


def _worker_main(buf, length, data, filename, limits):
    """Spawn target. Runs the raw parser once and publishes one bounded JSON result.

    The payload is copied into the shared byte buffer first and its length is
    published last. The parent reads neither until this process has exited 0.
    """
    global _IN_WORKER
    _IN_WORKER = True
    sink = _BoundedSink()
    logging.disable(logging.CRITICAL)
    try:
        with redirect_stdout(sink), redirect_stderr(sink):
            try:
                result = _parse_raw(data, filename, limits)
            except Exception as exc:  # report, never claim success
                detail = str(exc)[:200]
                result = _result("unknown", "failed", f"parser error {type(exc).__name__}: {detail}", len(data), "parse_error")
            payload = _encode_result(result, len(data))
            if 0 < len(payload) <= min(MAX_IPC_BYTES, len(buf)):
                ctypes.memmove(buf, payload, len(payload))
                length.value = len(payload)
    finally:
        logging.disable(logging.NOTSET)


def _reject_constant(name):
    raise ValueError(f"non-finite constant {name}")


def _parse_finite_float(text):
    value = float(text)
    if not math.isfinite(value):  # e.g. 1e999 overflows to inf
        raise ValueError("non-finite number")
    return value


def _valid_int(value):
    return value is None or (type(value) is int and value >= 0)


def _decode_result(payload, size):
    """Parse the worker message strictly and check the stable result shape."""
    try:
        obj = json.loads(payload.decode("utf-8"), parse_constant=_reject_constant, parse_float=_parse_finite_float)
    except (UnicodeDecodeError, ValueError, RecursionError):
        return None
    if type(obj) is not dict or set(obj) != RESULT_KEYS:
        return None
    if type(obj["status"]) is not str or obj["status"] not in STATUSES:
        return None
    metrics = obj["metrics"]
    if type(metrics) is not dict or set(metrics) != METRIC_KEYS or metrics["size_bytes"] != size:
        return None
    if not all(_valid_int(metrics[k]) for k in ("text_chars", "text_chars_no_whitespace", "text_words")):
        return None
    if metrics["encoding"] is not None and type(metrics["encoding"]) is not str:
        return None
    if not _valid_int(obj["pages"]) or (obj["text"] is not None and type(obj["text"]) is not str):
        return None
    for key in ("format", "reason"):
        if obj[key] is not None and (type(obj[key]) is not str or len(obj[key]) > MAX_REASON_CHARS):
            return None
    for key in ("locations", "unread_regions", "archive_entries"):
        if type(obj[key]) is not list or len(obj[key]) > MAX_RESULT_ITEMS:
            return None
    if obj["status"] == "complete" and (obj["text"] is None or obj["reason"] is not None):
        return None
    return obj


def _split_limits(limits):
    """Return (raw_limits, timeout_seconds, error). The timeout never reaches the worker."""
    if limits is None:
        return dict(DEFAULT_LIMITS), DEFAULT_TIMEOUT_SECONDS, None
    if type(limits) is not dict:
        return None, None, "limits must be a dict or None"
    raw = {}
    timeout = DEFAULT_TIMEOUT_SECONDS
    for key, value in limits.items():
        if type(key) is str and key == TIMEOUT_KEY:
            if type(value) not in (int, float) or value <= 0 or value > MAX_TIMEOUT_SECONDS or not math.isfinite(value):
                return None, None, f"limit {TIMEOUT_KEY} must be a positive finite number <= {MAX_TIMEOUT_SECONDS}"
            timeout = value
        else:
            raw[key] = value
    raw, error = _validate_limits(raw)
    if error:
        return None, None, error
    return raw, timeout, None


def _format_hint(data, filename):
    detected = _detect_signature(bytes(data[:16]))
    if detected:
        return detected
    return "text" if _extension(filename) in TEXT_EXTENSIONS else "unknown"


def _stop_process(proc, wait):
    proc.join(wait)
    if proc.is_alive():
        proc.terminate()
        proc.join(JOIN_GRACE_SECONDS)
    if proc.is_alive():
        proc.kill()
        proc.join(JOIN_GRACE_SECONDS)


def _run_worker(data, filename, raw_limits, timeout, size, hint):
    """Run one spawned worker and read its result only after a clean exit.

    Transport: a zero-filled shared RawArray of MAX_IPC_BYTES bytes plus a
    RawValue length. No pipe, reader thread or Manager. The parent waits with
    join() under one absolute deadline; on timeout it terminates/kills the
    worker and never looks at the possibly half-written buffer.
    """
    ctx = multiprocessing.get_context("spawn")
    buf = ctx.RawArray("B", MAX_IPC_BYTES)
    length = ctx.RawValue("I", 0)
    proc = ctx.Process(target=_worker_main, args=(buf, length, data, filename, raw_limits), daemon=True)
    started = False
    payload = None
    failure = None
    deadline = time.monotonic() + timeout
    try:
        proc.start()
        started = True
        proc.join(max(0.0, deadline - time.monotonic()))
        if proc.is_alive():
            failure = ("incomplete", f"parser timed out after {timeout} seconds", "budget_limit")
        elif proc.exitcode != 0:
            failure = ("failed", f"parser worker exited with code {proc.exitcode}", "parse_error")
        else:
            n = length.value
            if n <= 0 or n > MAX_IPC_BYTES:
                failure = ("failed", "parser worker published no valid result", "parse_error")
            else:
                payload = ctypes.string_at(ctypes.addressof(buf), n)
    finally:
        worker_alive = False
        if started:
            _stop_process(proc, 0.0)
            worker_alive = proc.is_alive()
            if not worker_alive:
                try:
                    proc.close()
                except ValueError:
                    pass
        if not worker_alive:
            # No writer remains: scrub the shared result buffer before release.
            ctypes.memset(ctypes.addressof(buf), 0, MAX_IPC_BYTES)
            length.value = 0
    if failure is not None:
        return _result(hint, failure[0], failure[1], size, failure[2])
    result = _decode_result(payload, size)
    if result is None:
        return _result(hint, "failed", "parser worker returned a malformed result", size, "parse_error")
    return result


def parse_bytes(data, filename, limits=None):
    """Parse in-memory bytes in an isolated worker. `filename` is a type hint only, never opened.

    limits: optional dict with max_input_bytes (int <= 1 MiB), max_text_chars
    (int <= 100000) and timeout_seconds (finite number, 0 < t <= 30).
    """
    if _IN_WORKER:
        return _result("unknown", "failed", "nested parser worker launch refused", None, "other")
    if type(data) not in (bytes, bytearray):
        return _result("unknown", "failed", "data must be bytes", None, "other")
    size = len(data)
    if type(filename) is not str or len(filename) > MAX_FILENAME_CHARS:
        return _result("unknown", "failed", "filename must be a bounded string", size, "other")
    raw_limits, timeout, error = _split_limits(limits)
    if error:
        return _result("unknown", "failed", error, size, "other")
    if size == 0:
        return _result("empty", "incomplete", "empty input; no content to read", 0, "other")
    hint = _format_hint(data, filename)
    if size > raw_limits["max_input_bytes"]:
        reason = f"input {size} bytes exceeds parser budget {raw_limits['max_input_bytes']} bytes; not read"
        return _result(hint, "incomplete", reason, size, "budget_limit")
    data = bytes(data)
    try:
        return _run_worker(data, filename, raw_limits, timeout, size, hint)
    except Exception as exc:
        return _result(hint, "failed", f"parser worker could not run: {type(exc).__name__}", size, "other")
