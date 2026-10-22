"""Bounded byte-oriented parsers. Bootstrap: UTF-8 plaintext only.

parse_bytes() never opens paths, writes files, uses the network or changes
global state. It returns neutral facts; the core maps them to report fields.
PDF/DOCX/ZIP parsing and process isolation are not implemented yet, so those
inputs return status "incomplete" with an explicit unsupported reason.
"""

import unicodedata

MAX_INPUT_BYTES = 1024 * 1024
MAX_TEXT_CHARS = 100_000
MAX_COMBINING_RUN = 64
MAX_REASON_CHARS = 512
MAX_FILENAME_CHARS = 1024
DEFAULT_LIMITS = {"max_input_bytes": MAX_INPUT_BYTES, "max_text_chars": MAX_TEXT_CHARS}
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


def parse_bytes(data, filename, limits=None):
    """Parse in-memory bytes. `filename` is a display/type hint only, never opened."""
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
