"""Bounded read-only text, PDF, ZIP and Office parsers.

parse_bytes() never opens caller paths, writes files of its own, uses the
network or changes global state. It validates input in the calling process,
then runs the raw parser in one spawned worker process with a wall-clock
timeout. The bounded JSON result returns through multiprocessing shared
memory (RawArray). On Windows that memory is an anonymous mapping; on POSIX
the standard library backs it with a file it unlinks immediately, in /dev/shm
when available, otherwise in the temp directory, so parsed text can exist
there in memory-mapped form until the call ends. It returns neutral facts; the
core maps them to report fields. ZIP archives and Office packages use bounded
in-memory reads with CRC and hashes. DOCX body paragraphs/tables, PPTX slide
text and XLSX cells/formulas are read with DTD-free UTF-8 XML, including inside
ZIP submissions. Python is parsed without execution. DOCX pagination and
spreadsheet recalculation are not performed. Native formats remain incomplete.

Callers that run this from a script must guard their entry point with
`if __name__ == "__main__":` because the worker uses the "spawn" start method.
"""

import ast
import ctypes
import hashlib
import io
import json
import logging
import math
import multiprocessing
import re
import time
import unicodedata
import xml.etree.ElementTree as ET
import zipfile
from contextlib import redirect_stderr, redirect_stdout

MAX_INPUT_BYTES = 1024 * 1024
MAX_TEXT_CHARS = 100_000
MAX_PDF_BYTES = 50 * 1024 * 1024
MAX_PDF_PAGES = 300
MAX_ARCHIVE_BYTES = 50 * 1024 * 1024
MAX_ARCHIVE_ENTRIES = 1000
MAX_UNCOMPRESSED_BYTES = 200 * 1024 * 1024
PDF_MAX_FORM_DEPTH = 3
MAX_COMBINING_RUN = 64
MAX_REASON_CHARS = 512
MAX_FILENAME_CHARS = 1024
# Defaults are also the maximum values a caller may request.
DEFAULT_LIMITS = {
    "max_input_bytes": MAX_INPUT_BYTES,  # non-PDF (text) input
    "max_text_chars": MAX_TEXT_CHARS,
    "max_pdf_bytes": MAX_PDF_BYTES,
    "max_pdf_pages": MAX_PDF_PAGES,
    "max_archive_bytes": MAX_ARCHIVE_BYTES,
    "max_archive_entries": MAX_ARCHIVE_ENTRIES,
    "max_uncompressed_bytes": MAX_UNCOMPRESSED_BYTES,
}
ZIP_CHUNK_BYTES = 64 * 1024
ZIP_MAX_NAME_CHARS = 300
ZIP_MAX_UNREAD = 1000
OFFICE_EXTENSIONS = frozenset({".docx", ".pptx", ".xlsx"})
OOXML_LIKE_EXTENSIONS = OFFICE_EXTENSIONS | {".hwpx"}
ARCHIVE_EXTENSIONS = frozenset({".zip", ".7z", ".rar", ".gz", ".tgz", ".bz2", ".xz", ".tar", ".jar", ".zst"})
ARCHIVE_MAGIC = (b"PK\x03\x04", b"PK\x05\x06", b"PK\x07\x08", b"7z\xbc\xaf\x27\x1c", b"Rar!\x1a\x07", b"\x1f\x8b", b"BZh", b"\xfd7zXZ\x00", b"\x28\xb5\x2f\xfd")
WINDOWS_RESERVED_NAMES = frozenset({"con", "prn", "aux", "nul"} | {f"com{i}" for i in range(1, 10)} | {f"lpt{i}" for i in range(1, 10)})
# Worker-only pypdf decode caps, applied via pypdf.apply_configuration().
PDF_DECODE_CONFIG = {
    "zlib_maximum_output_length": 16 * 1024 * 1024,
    "array_based_stream_maximum_output_length": 16 * 1024 * 1024,
    "lzw_maximum_output_length": 16 * 1024 * 1024,
    "run_length_maximum_output_length": 16 * 1024 * 1024,
    "brotli_maximum_output_length": 16 * 1024 * 1024,
    "jbig2_maximum_output_length": 8 * 1024 * 1024,
    "xform_maximum_invocations_per_extraction": 500,
}
PDF_SOFT_DEADLINE_FRACTION = 0.75
PDF_MAX_XOBJECTS_PER_PAGE = 200
PDF_MAX_ANNOTS_PER_PAGE = 200
PDF_EMBEDDED_ANNOTS = frozenset({"/FileAttachment", "/Widget", "/RichMedia", "/Screen", "/Movie", "/Sound", "/3D"})
INLINE_IMAGE_RE = re.compile(rb"(?:^|\s)BI\s")
TIMEOUT_KEY = "timeout_seconds"
DEFAULT_TIMEOUT_SECONDS = 30
MAX_TIMEOUT_SECONDS = 30
MAX_IPC_BYTES = 4 * 1024 * 1024
MAX_RESULT_ITEMS = 10_000
MAX_CAPTURED_OUTPUT_CHARS = 2048
JOIN_GRACE_SECONDS = 2.0
RESULT_KEYS = frozenset({"format", "status", "reason", "metrics", "pages", "text", "locations", "unread_regions", "archive_entries"})
COUNT_METRIC_KEYS = ("text_chars", "text_chars_no_whitespace", "text_words", "paragraphs", "tables", "images", "entries", "uncompressed_bytes_read")
METRIC_KEYS = frozenset({"size_bytes", "encoding", *COUNT_METRIC_KEYS})
DOCX_MAX_PART_BYTES = 16 * 1024 * 1024
DOCX_MAX_COLLECT_BYTES = 48 * 1024 * 1024
DOCX_MAX_XML_ELEMENTS = 1_000_000
DOCX_MAX_XML_DEPTH = 256
DOCX_MAX_BLOCK_DEPTH = 32
DOCX_MAIN_TYPE = "application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"
NS_W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
NS_CT = "{http://schemas.openxmlformats.org/package/2006/content-types}"
NS_REL = "{http://schemas.openxmlformats.org/package/2006/relationships}"
REL_OFFICE_DOCUMENT = "http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument"
REL_UNREAD_KINDS = {"/image": "image", "/oleObject": "embedded_object", "/package": "embedded_object",
                    "/subDocument": "embedded_object", "/aFChunk": "embedded_object", "/chart": "embedded_object",
                    "/header": "other", "/footer": "other", "/footnotes": "other", "/endnotes": "other", "/comments": "other"}
XML_ENCODING_RE = re.compile(rb"^<\?xml[^>]*?encoding\s*=\s*[\"']([A-Za-z0-9._-]{1,40})[\"']")
XML_DECL_RE = re.compile(rb"<!(?!--|\[CDATA\[)")
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
EXPECTED_SIGNATURE = {".pdf": "pdf", ".docx": "zip", ".pptx": "zip", ".xlsx": "zip", ".zip": "zip", ".hwpx": "zip", ".hwp": "ole_compound", ".doc": "ole_compound", ".xls": "ole_compound"}
UTF8_BOM = b"\xef\xbb\xbf"


def _unread(kind, detail):
    return {"kind": kind, "location": None, "detail": str(detail)[:MAX_REASON_CHARS]}


def _result(fmt, status, reason, size_bytes, unread_kind=None):
    unread = [_unread(unread_kind, reason)] if unread_kind and reason else []
    return {
        "format": fmt,
        "status": status,
        "reason": None if reason is None else str(reason)[:MAX_REASON_CHARS],
        # Every count is null unless actually measured for this format.
        "metrics": {"size_bytes": size_bytes, "encoding": None, **{key: None for key in COUNT_METRIC_KEYS}},
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


def _text_metrics(text):
    return {
        # Unicode code points of the normalized text excluding LF (CR already folded).
        "text_chars": len(text) - text.count("\n"),
        # Code points that are not Unicode whitespace (str.isspace).
        "text_chars_no_whitespace": sum(1 for ch in text if not ch.isspace()),
        # Tokens separated by Unicode whitespace (str.split); not a linguistic count.
        "text_words": len(text.split()),
    }


def _parse_raw(data, filename, limits=None):
    """Raw in-process dispatcher (text, PDF, ZIP, DOCX). Runs only inside the worker; never spawns."""
    if type(data) not in (bytes, bytearray):
        return _result("unknown", "failed", "data must be bytes", None, "other")
    if type(filename) is not str or len(filename) > MAX_FILENAME_CHARS:
        return _result("unknown", "failed", "filename must be a bounded string", len(data), "other")
    limits, timeout, error = _split_limits(limits)
    if error:
        return _result("unknown", "failed", error, len(data), "other")
    soft_deadline = time.monotonic() + timeout * PDF_SOFT_DEADLINE_FRACTION
    data = bytes(data)
    size = len(data)
    if size == 0:
        return _result("empty", "incomplete", "empty input; no content to read", 0, "other")

    ext = _extension(filename)
    detected = _detect_signature(data)
    expected = EXPECTED_SIGNATURE.get(ext)
    if expected is not None and detected != expected:
        reason = f"declared extension {ext} expects {expected} signature; detected {detected or 'none'}"
        return _result(detected or "unknown", "failed", reason, size, "parse_error")
    if detected == "pdf":
        return _parse_pdf(data, size, limits, soft_deadline)
    if detected == "zip" and ext == ".docx":
        return _parse_docx(data, size, limits, soft_deadline)
    if detected == "zip" and ext in {".pptx", ".xlsx"}:
        return _parse_office(data, size, limits, soft_deadline, ext[1:])
    if detected == "zip" and ext not in OOXML_LIKE_EXTENSIONS:
        return _parse_zip(data, size, limits, soft_deadline)
    if detected is not None:
        reason = f"{detected} content (extension {ext or 'none'}) is not parsed by this bootstrap parser"
        return _result(detected, "incomplete", reason, size, "unsupported_format")
    if ext not in TEXT_EXTENSIONS:
        reason = f"no supported parser for extension {ext or 'none'}; content not read"
        return _result("unknown", "incomplete", reason, size, "unsupported_format")
    return _parse_code_or_text(data, size, limits, filename)


def _parse_code_or_text(data, size, limits, filename):
    result = _parse_text(data, size, limits)
    if _extension(filename) == ".py" and result["status"] == "complete":
        try:
            ast.parse(result["text"], filename=filename)
        except SyntaxError as exc:
            result["status"] = "failed"
            result["reason"] = f"Python syntax error at line {exc.lineno}: {exc.msg}"
            result["unread_regions"].append(_unread("parse_error", result["reason"]))
    return result


def _parse_text(data, size, limits):
    """Strict UTF-8 text helper; also used for text entries inside a ZIP."""
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
    result["metrics"].update(_text_metrics(norm))
    result["metrics"]["encoding"] = encoding
    result["text"] = norm
    # Zero-based code point offsets into `text`, start inclusive, end exclusive.
    result["locations"] = [{"char_start": 0, "char_end": len(norm), "label": "whole_text"}]
    return result


# ---------------------------------------------------------------------------
# PDF (worker only; pypdf is imported lazily inside the worker)
# ---------------------------------------------------------------------------

def _page_region(kind, page, detail):
    return {"kind": kind, "location": {"page": page}, "detail": str(detail)[:MAX_REASON_CHARS]}


def _is_limit_error(exc):
    return type(exc).__name__ == "LimitReachedError"


def _resolve(obj):
    return obj.get_object() if hasattr(obj, "get_object") else obj


def _pdf_page_objects(page):
    """Count image/form XObjects (nested forms to PDF_MAX_FORM_DEPTH) without decoding image data.

    Returns (images, forms, uncertain). uncertain=True when the walk hit the
    depth or count cap, an error, or a resource/XObject shape it does not
    recognize; the caller then marks the page as not fully inspected.
    """
    images = forms = seen = 0
    uncertain = False
    visited = set()
    stack = [(page.get("/Resources"), 0)]
    try:
        while stack:
            resources, depth = stack.pop()
            resources = _resolve(resources)
            if resources is None:
                continue
            if not hasattr(resources, "get"):
                uncertain = True
                continue
            if id(resources) in visited:  # cycle-safe: each resource dict once
                continue
            visited.add(id(resources))
            xobjects = _resolve(resources.get("/XObject"))
            if xobjects is None:
                continue
            if not hasattr(xobjects, "values"):
                uncertain = True
                continue
            for ref in xobjects.values():
                seen += 1
                if seen > PDF_MAX_XOBJECTS_PER_PAGE:
                    return images, forms, True
                obj = _resolve(ref)
                subtype = obj.get("/Subtype") if hasattr(obj, "get") else None
                if subtype == "/Image":
                    images += 1
                elif subtype == "/Form":
                    forms += 1
                    if depth + 1 > PDF_MAX_FORM_DEPTH:
                        uncertain = True  # deeper forms are not inspected
                    else:
                        stack.append((obj.get("/Resources"), depth + 1))
                else:
                    uncertain = True
    except Exception:
        return images, forms, True
    return images, forms, uncertain


def _pdf_page_flags(page):
    """Return (inline_image, embedded_annots, uncertain) for one page."""
    inline = False
    embedded = 0
    uncertain = False
    try:
        contents = page.get_contents()
        if contents is not None and INLINE_IMAGE_RE.search(contents.get_data()):
            inline = True
    except Exception:
        uncertain = True
    try:
        annots = _resolve(page.get("/Annots"))
        for ref in list(annots or [])[:PDF_MAX_ANNOTS_PER_PAGE]:
            annot = _resolve(ref)
            if hasattr(annot, "get") and annot.get("/Subtype") in PDF_EMBEDDED_ANNOTS:
                embedded += 1
        if annots is not None and len(annots) > PDF_MAX_ANNOTS_PER_PAGE:
            uncertain = True
    except Exception:
        uncertain = True
    return inline, embedded, uncertain


def _pdf_document_regions(reader):
    regions = []
    try:
        root = _resolve(reader.trailer["/Root"])
        acroform = _resolve(root.get("/AcroForm"))
        if hasattr(acroform, "get") and acroform.get("/XFA") is not None:
            regions.append({"kind": "embedded_object", "location": None, "detail": "XFA form content not read"})
        names = _resolve(root.get("/Names"))
        if hasattr(names, "get") and names.get("/EmbeddedFiles") is not None:
            regions.append({"kind": "embedded_object", "location": None, "detail": "embedded files not read"})
    except Exception as exc:
        regions.append({"kind": "parse_error", "location": None, "detail": f"document catalog not fully inspected: {type(exc).__name__}"})
    return regions


def _parse_pdf(data, size, limits, soft_deadline):
    if size > limits["max_pdf_bytes"]:
        reason = f"PDF {size} bytes exceeds parser budget {limits['max_pdf_bytes']} bytes; not read"
        return _result("pdf", "incomplete", reason, size, "budget_limit")
    if not data.startswith(b"%PDF-"):
        return _result("unknown", "failed", "missing %PDF- signature", size, "parse_error")
    try:
        import pypdf
        config = pypdf.apply_configuration(**PDF_DECODE_CONFIG)
    except Exception as exc:  # fail closed: never parse without decode caps
        reason = f"pypdf or its decode-cap configuration is unavailable ({type(exc).__name__}); PDF content not read"
        return _result("pdf", "incomplete", reason, size, "unsupported_format")
    if not (hasattr(config, "__enter__") and hasattr(config, "__exit__")):
        reason = "pypdf.apply_configuration did not return a context manager; PDF content not read"
        return _result("pdf", "incomplete", reason, size, "unsupported_format")
    try:
        with config:
            return _parse_pdf_configured(pypdf, data, size, limits, soft_deadline)
    except Exception as exc:
        kind = "budget_limit" if _is_limit_error(exc) else "parse_error"
        status = "incomplete" if kind == "budget_limit" else "failed"
        return _result("pdf", status, f"PDF could not be parsed: {type(exc).__name__}", size, kind)


def _parse_pdf_configured(pypdf, data, size, limits, soft_deadline):
    reader = pypdf.PdfReader(io.BytesIO(data), strict=False)
    if reader.is_encrypted:
        return _result("pdf", "incomplete", "encrypted PDF; no password attempted, content not read", size, "encrypted")
    page_count = len(reader.pages)
    if type(page_count) is not int or page_count < 0:
        return _result("pdf", "failed", "PDF page tree is invalid", size, "parse_error")
    if page_count == 0:
        return _result("pdf", "incomplete", "PDF has no pages", size, "other")
    if page_count > limits["max_pdf_pages"]:
        reason = f"PDF has {page_count} pages, over parser budget {limits['max_pdf_pages']}; text not read"
        result = _result("pdf", "incomplete", reason, size, "budget_limit")
        result["pages"] = page_count
        return result

    max_chars = limits["max_text_chars"]
    parts, locations, unread = [], [], _pdf_document_regions(reader)
    total = 0
    for index in range(page_count):
        number = index + 1
        if time.monotonic() > soft_deadline:
            unread.append(_page_region("budget_limit", number, f"time budget reached; pages {number}-{page_count} not read"))
            break
        page = reader.pages[index]
        try:
            raw = page.extract_text()
        except Exception as exc:
            kind = "budget_limit" if _is_limit_error(exc) else "parse_error"
            unread.append(_page_region(kind, number, f"text extraction failed: {type(exc).__name__}"))
            continue
        raw = raw if type(raw) is str else ""
        if len(raw) > max_chars:
            unread.append(_page_region("truncated", number, f"text budget {max_chars} characters reached; pages {number}-{page_count} not read"))
            break
        if _combining_run_exceeded(raw):
            unread.append(_page_region("parse_error", number, f"combining-mark run longer than {MAX_COMBINING_RUN}; page text not normalized"))
            continue
        norm = unicodedata.normalize("NFC", raw).replace("\r\n", "\n").replace("\r", "\n")
        has_text = bool(norm.strip())
        if has_text:
            separator = 1 if parts else 0
            if total + separator + len(norm) > max_chars:
                unread.append(_page_region("truncated", number, f"text budget {max_chars} characters reached; pages {number}-{page_count} not read"))
                break
            if separator:
                parts.append("\n")
                total += 1
            locations.append({"page": number, "char_start": total, "char_end": total + len(norm), "label": "page_text"})
            parts.append(norm)
            total += len(norm)
        images, forms, walk_uncertain = _pdf_page_objects(page)
        inline, embedded, flags_uncertain = _pdf_page_flags(page)
        if not has_text:
            kind = "scanned_page" if images or inline else "other"
            unread.append(_page_region(kind, number, "no extractable text on page (blank, scanned, vector or image-only)"))
        elif images or inline:
            unread.append(_page_region("image", number, f"{images} image object(s){' and inline image data' if inline else ''}; image content not read"))
        if embedded:
            unread.append(_page_region("embedded_object", number, f"{embedded} embedded/form annotation(s) not read"))
        if walk_uncertain or flags_uncertain:
            unread.append(_page_region("other", number, "page resources not fully inspected for images/embedded content"))
        if len(unread) > MAX_RESULT_ITEMS - 2:
            unread = unread[: MAX_RESULT_ITEMS - 2]
            unread.append({"kind": "truncated", "location": None, "detail": "unread region list truncated"})
            break

    text = "".join(parts)
    complete = not unread
    reason = None if complete else f"{len(unread)} unread or uninspected region(s); see unread_regions"
    result = _result("pdf", "complete" if complete else "incomplete", reason, size)
    result["pages"] = page_count
    result["text"] = text
    result["locations"] = locations
    result["unread_regions"] = unread
    if complete:
        # Counts are reported only when every page's text was read.
        result["metrics"].update(_text_metrics(text))
    return result


# ---------------------------------------------------------------------------
# ZIP (in memory only: never extracted, executed or recursed into)
# ---------------------------------------------------------------------------

def _zip_declared_entries(data):
    """Entry count from the last end-of-central-directory record, or None."""
    pos = data.rfind(b"PK\x05\x06", max(0, len(data) - 65557))
    if pos < 0 or pos + 22 > len(data):
        return None
    return int.from_bytes(data[pos + 10:pos + 12], "little")


def _zip_name_problems(info):
    """Problems with the raw (pre-normalization) entry name and type. Empty list = safe."""
    name = info.orig_filename
    if type(name) is not str or not name:
        return ["empty name"]
    problems = []
    if len(name) > ZIP_MAX_NAME_CHARS:
        problems.append("name too long")
    if "\x00" in name:
        problems.append("NUL in name")
    if any(ord(ch) < 32 or ord(ch) == 127 for ch in name):
        problems.append("control character in name")
    if "\\" in name:
        problems.append("backslash in name")
    if ":" in name:
        problems.append("colon or drive in name")
    if name.startswith("/"):
        problems.append("absolute or UNC path")
    if not (info.flag_bits & 0x800) and any(ord(ch) > 127 for ch in name):
        problems.append("name encoding not declared UTF-8")
    body = name[:-1] if name.endswith("/") else name
    for segment in body.split("/"):
        if segment in ("", ".", ".."):
            problems.append("empty, dot or dot-dot path segment")
            break
        if segment[-1] in ". ":
            problems.append("segment ends with dot or space")
            break
        if segment.split(".")[0].casefold() in WINDOWS_RESERVED_NAMES:
            problems.append("reserved device name")
            break
    mode = (info.external_attr >> 16) & 0o170000
    if mode and mode not in (0o100000, 0o040000):
        problems.append("symlink or special file type")
    return problems


def _zip_read_entry(zf, info, state, collect_cap):
    """Stream one entry in bounded chunks under the shared byte budget and deadline.

    Returns a dict with bytes_read, crc_ok, sha256, collected (bytes or None),
    magic, too_large and problem ((kind, detail) or None). Hash and CRC are
    reported only when the entry was read to EOF and zipfile's CRC check passed.
    """
    digest = hashlib.sha256()
    out = {"bytes_read": 0, "crc_ok": None, "sha256": None, "collected": None, "magic": b"", "too_large": False, "problem": None}
    collected = bytearray() if collect_cap else None
    try:
        with zf.open(info) as handle:
            while True:
                if time.monotonic() > state["deadline"]:
                    out["problem"] = ("budget_limit", "time budget reached during entry read")
                    return out
                remaining = state["budget"] - state["used"]
                chunk = handle.read(min(ZIP_CHUNK_BYTES, remaining + 1))  # +1 probes EOF at equality
                if not chunk:
                    break
                state["used"] += len(chunk)
                out["bytes_read"] += len(chunk)
                if len(out["magic"]) < 8:
                    out["magic"] += chunk[: 8 - len(out["magic"])]
                if state["used"] > state["budget"]:
                    out["problem"] = ("budget_limit", "total uncompressed read budget reached")
                    return out
                digest.update(chunk)
                if collected is not None:
                    if len(collected) + len(chunk) > collect_cap:
                        collected = None
                        out["too_large"] = True
                    else:
                        collected += chunk
    except zipfile.BadZipFile as exc:
        if "CRC" in str(exc):
            out["crc_ok"] = False
        out["problem"] = ("parse_error", f"entry corrupt: {str(exc)[:120]}")
        return out
    except Exception as exc:
        out["problem"] = ("parse_error", f"entry unreadable: {type(exc).__name__}")
        return out
    out["crc_ok"] = True
    out["sha256"] = digest.hexdigest()
    out["collected"] = bytes(collected) if collected is not None else None
    return out


def _zip_entry_region(kind, index, detail):
    return {"kind": kind, "location": {"label": f"entry:{index}"}, "detail": str(detail)[:MAX_REASON_CHARS]}


def _zip_open(data, size, limits, fmt):
    """Open an in-memory ZIP and pre-check entries. Returns (zf, infos, problems, error_result)."""
    if size > limits["max_archive_bytes"]:
        reason = f"archive {size} bytes exceeds parser budget {limits['max_archive_bytes']} bytes; not read"
        return None, None, None, _result(fmt, "incomplete", reason, size, "budget_limit")
    max_entries = limits["max_archive_entries"]
    declared = _zip_declared_entries(data)
    if declared is not None and declared != 0xFFFF and declared > max_entries:
        reason = f"archive declares {declared} entries, over parser budget {max_entries}; not read"
        return None, None, None, _result(fmt, "incomplete", reason, size, "budget_limit")
    try:
        zf = zipfile.ZipFile(io.BytesIO(data))
        infos = zf.infolist()
    except Exception as exc:
        return None, None, None, _result(fmt, "failed", f"ZIP structure unreadable: {type(exc).__name__}", size, "parse_error")
    if len(infos) > max_entries:
        reason = f"archive has {len(infos)} entries, over parser budget {max_entries}; not read"
        return None, None, None, _result(fmt, "incomplete", reason, size, "budget_limit")
    if not infos and fmt != "zip":
        return None, None, None, _result(fmt, "incomplete", "archive has no entries", size, "other")
    # Names, types, duplicates and case/Unicode collisions (no content read).
    problems = [_zip_name_problems(info) for info in infos]
    keys = {}
    for index, info in enumerate(infos):
        name = info.orig_filename if type(info.orig_filename) is str else ""
        key = unicodedata.normalize("NFC", name.rstrip("/")).casefold()
        keys.setdefault(key, []).append(index)
    for members in keys.values():
        if len(members) > 1:
            for index in members:
                problems[index].append("duplicate or case/Unicode-colliding name")
    return zf, infos, problems, None


def _zip_new_record(index, info, problems):
    """Return (record, safe, raw_name) with the stable per-entry metadata."""
    name_problems = problems[index][:8]
    is_dir = info.is_dir()
    raw_name = info.orig_filename if type(info.orig_filename) is str else ""
    safe = not name_problems
    record = {
        "index": index,
        "name": raw_name[:ZIP_MAX_NAME_CHARS],
        "entry_path": (raw_name[:-1] if is_dir else raw_name) if safe else None,
        "is_dir": is_dir,
        "compressed_size": info.compress_size,
        "declared_size": info.file_size,
        "bytes_read": 0,
        "crc_ok": None,
        "encrypted": bool(info.flag_bits & 0x1),
        "nested_archive": False,
        "unsafe_name": not safe,
        "unsafe_reasons": name_problems,
        "sha256": None,
        "content": None,
    }
    return record, safe, raw_name


def _zip_check_directory(zf, info, record, state):
    """Directory entries must really be empty; read them like files. Returns a problem or None."""
    mode = (info.external_attr >> 16) & 0o170000
    if mode == 0o100000 or (info.external_attr & 0x10 and not info.is_dir()):
        return ("parse_error", "directory/file type inconsistent with entry name")
    if record["encrypted"]:
        return ("encrypted", "encrypted directory entry; content not read")
    if state["used"] > state["budget"] or time.monotonic() > state["deadline"]:
        return ("budget_limit", "read budget exhausted before this entry")
    read = _zip_read_entry(zf, info, state, 0)
    record["bytes_read"] = read["bytes_read"]
    record["crc_ok"] = read["crc_ok"]
    record["sha256"] = read["sha256"]
    if read["problem"] is not None:
        return read["problem"]
    if read["bytes_read"] or info.file_size:
        return ("parse_error", "directory entry carries data; content not inspected")
    return None


def _parse_zip(data, size, limits, soft_deadline):
    zf, infos, problems, error = _zip_open(data, size, limits, "zip")
    if error is not None:
        return error
    if not infos:
        # zipfile read a structurally valid central directory with zero members: the entry
        # list is completely known (and empty). Not inferred from the signature alone.
        result = _result("zip", "complete", None, size)
        result["text"] = ""
        result["metrics"]["entries"] = 0
        result["metrics"]["uncompressed_bytes_read"] = 0
        return result
    state = {"budget": limits["max_uncompressed_bytes"], "used": 0, "deadline": soft_deadline}
    max_chars = limits["max_text_chars"]
    pages_left = limits["max_pdf_pages"]
    entries, parts, locations, unread = [], [], [], []
    total_chars = 0
    file_entries = 0

    def add_unread(region):
        if len(unread) < ZIP_MAX_UNREAD:
            unread.append(region)
        elif len(unread) == ZIP_MAX_UNREAD:
            unread.append({"kind": "truncated", "location": None, "detail": "unread region list truncated"})

    for index, info in enumerate(infos):
        record, safe, raw_name = _zip_new_record(index, info, problems)
        ext = _extension(raw_name) if safe else ""
        entries.append(record)
        if not safe:
            add_unread(_zip_entry_region("other", index, "unsafe entry name or type: " + "; ".join(record["unsafe_reasons"])))
        if record["is_dir"]:
            problem = _zip_check_directory(zf, info, record, state)
            if problem is not None:
                add_unread(_zip_entry_region(problem[0], index, problem[1]))
            continue
        file_entries += 1
        if (info.external_attr >> 16) & 0o170000 == 0o040000 or info.external_attr & 0x10:
            add_unread(_zip_entry_region("parse_error", index, "file entry marked as directory in attributes"))
        if record["encrypted"]:
            add_unread(_zip_entry_region("encrypted", index, "encrypted entry; content not read"))
            continue
        if state["used"] > state["budget"] or time.monotonic() > state["deadline"]:
            add_unread(_zip_entry_region("budget_limit", index, "read budget exhausted before this entry"))
            continue

        is_pdf_name = ext == ".pdf"
        collect_cap = 0
        if safe and is_pdf_name:
            collect_cap = limits["max_pdf_bytes"]
        elif safe and ext in OFFICE_EXTENSIONS:
            collect_cap = limits["max_archive_bytes"]
        elif safe and ext in TEXT_EXTENSIONS:
            collect_cap = limits["max_input_bytes"]
        read = _zip_read_entry(zf, info, state, collect_cap)
        record["bytes_read"] = read["bytes_read"]
        record["crc_ok"] = read["crc_ok"]
        record["sha256"] = read["sha256"]
        magic = read["magic"]
        record["nested_archive"] = (ext not in OFFICE_EXTENSIONS and
                                    (ext in ARCHIVE_EXTENSIONS or any(magic.startswith(m) for m in ARCHIVE_MAGIC)))
        if read["problem"] is not None:
            add_unread(_zip_entry_region(read["problem"][0], index, read["problem"][1]))
            continue
        if record["nested_archive"]:
            add_unread(_zip_entry_region("unsupported_format", index, "nested archive not inspected"))
            continue
        if not safe:
            continue  # content of unsafe names is never bound to a path
        if read["too_large"]:
            add_unread(_zip_entry_region("budget_limit", index, "entry larger than its per-format parser budget; content not inspected"))
            continue
        content = read["collected"]
        if content is None:
            add_unread(_zip_entry_region("unsupported_format", index, f"no internal parser for extension {ext or 'none'}; content not inspected"))
            continue

        separator = 1 if parts else 0
        available = max_chars - total_chars - separator
        if available <= 0:
            add_unread(_zip_entry_region("truncated", index, "shared text budget exhausted; content not inspected"))
            continue
        if is_pdf_name and pages_left <= 0:
            add_unread(_zip_entry_region("budget_limit", index, "shared PDF page budget exhausted; content not inspected"))
            continue
        sub_limits = dict(limits)
        sub_limits["max_text_chars"] = available
        sub_limits["max_pdf_pages"] = max(1, pages_left)
        signature = _detect_signature(content[:16])
        if is_pdf_name and signature == "pdf":
            sub = _parse_pdf(content, len(content), sub_limits, soft_deadline)
        elif ext in OFFICE_EXTENSIONS and signature == "zip":
            remaining = state["budget"] - state["used"]
            if remaining <= 0:
                add_unread(_zip_entry_region("budget_limit", index, "shared uncompressed budget exhausted"))
                continue
            sub_limits["max_uncompressed_bytes"] = remaining
            sub = (_parse_docx(content, len(content), sub_limits, soft_deadline) if ext == ".docx" else
                   _parse_office(content, len(content), sub_limits, soft_deadline, ext[1:]))
            state["used"] += sub["metrics"]["uncompressed_bytes_read"] or 0
        elif not is_pdf_name and signature is None:
            sub = _parse_code_or_text(content, len(content), sub_limits, raw_name)
        else:
            add_unread(_zip_entry_region("parse_error", index, f"entry signature {signature or 'none'} does not match extension {ext}"))
            continue
        if sub["format"] == "pdf" and type(sub["pages"]) is int and sub["pages"] <= pages_left:
            pages_left -= sub["pages"]
        record["content"] = {
            "format": sub["format"],
            "status": sub["status"],
            "reason": sub["reason"],
            "pages": sub["pages"],
            "text_chars": sub["metrics"]["text_chars"],
            "text_chars_no_whitespace": sub["metrics"]["text_chars_no_whitespace"],
            "text_words": sub["metrics"]["text_words"],
        }
        text = sub["text"] or ""
        if text:
            if separator:
                parts.append("\n")
                total_chars += 1
            for loc in sub["locations"][: MAX_RESULT_ITEMS - len(locations)]:
                shifted = dict(loc)
                shifted["char_start"] = loc["char_start"] + total_chars
                shifted["char_end"] = loc["char_end"] + total_chars
                shifted["label"] = f"entry:{index} " + str(loc.get("label") or "")
                locations.append(shifted)
            parts.append(text)
            total_chars += len(text)
        for region in sub["unread_regions"]:
            bound = dict(region)
            bound["location"] = dict(region["location"] or {})
            bound["location"]["label"] = f"entry:{index}"
            add_unread(bound)
        if sub["status"] != "complete" and not sub["unread_regions"]:
            add_unread(_zip_entry_region("other", index, f"entry content {sub['status']}: {sub['reason']}"))

    if file_entries == 0:
        add_unread({"kind": "other", "location": None, "detail": "archive has no file entries"})
    complete = not unread
    reason = None if complete else f"{len(unread)} unread, unsafe or uninspected region(s); see unread_regions and archive_entries"
    result = _result("zip", "complete" if complete else "incomplete", reason, size)
    result["metrics"]["entries"] = len(infos)
    result["metrics"]["uncompressed_bytes_read"] = state["used"]
    result["text"] = "".join(parts)
    result["locations"] = locations
    result["unread_regions"] = unread
    result["archive_entries"] = entries
    return result


# ---------------------------------------------------------------------------
# DOCX (WordprocessingML through the same bounded in-memory ZIP reader)
# ---------------------------------------------------------------------------

def _safe_xml_root(raw, deadline):
    """Parse UTF-8 XML with no DTD/entity declarations under element/depth/time caps.

    Returns (root, problem). External entities are never resolved: any
    declaration is refused before the parser sees the bytes.
    """
    if raw.startswith(b"\xef\xbb\xbf"):
        raw = raw[3:]
    if raw.startswith((b"\xff\xfe", b"\xfe\xff")) or b"\x00" in raw[:4]:
        return None, "XML part is not UTF-8"
    match = XML_ENCODING_RE.match(raw)
    if match and match.group(1).lower() not in (b"utf-8", b"utf8"):
        return None, "XML encoding declaration is not UTF-8"
    if XML_DECL_RE.search(raw):
        return None, "XML part contains a DTD or markup declaration; refused"
    try:
        raw.decode("utf-8")
    except UnicodeDecodeError:
        return None, "XML part is not valid UTF-8"
    parser = ET.XMLPullParser(events=("start", "end"))
    counters = {"root": None, "depth": 0, "elements": 0}

    def drain():
        for event, elem in parser.read_events():
            if event == "start":
                if counters["root"] is None:
                    counters["root"] = elem
                counters["elements"] += 1
                counters["depth"] += 1
                if counters["elements"] > DOCX_MAX_XML_ELEMENTS or counters["depth"] > DOCX_MAX_XML_DEPTH:
                    return "XML element or depth budget exceeded"
            else:
                counters["depth"] -= 1
        return None

    step = 1024 * 1024
    try:
        for offset in range(0, len(raw), step):
            if time.monotonic() > deadline:
                return None, "time budget reached while parsing XML"
            parser.feed(raw[offset:offset + step])
            problem = drain()
            if problem:
                return None, problem
        parser.close()
        problem = drain()
        if problem:
            return None, problem
    except ET.ParseError as exc:
        return None, f"XML malformed: {str(exc)[:120]}"
    if counters["root"] is None:
        return None, "XML part has no root element"
    return counters["root"], None


def _ooxml_resolve(base_dir, target):
    """Map an internal relationship target to a package entry name, or None if unsafe/unsupported."""
    if type(target) is not str or not target or len(target) > 1024:
        return None
    if any(ch in target for ch in "\\:%?#") or any(ord(ch) < 32 for ch in target):
        return None
    path = target[1:] if target.startswith("/") else base_dir + target
    parts = []
    for segment in path.split("/"):
        if not segment:
            return None
        if segment == "..":
            if not parts:
                return None
            parts.pop()
        elif segment != ".":
            parts.append(segment)
    return "/".join(parts) or None


def _ooxml_content_type(types_root, part):
    for child in types_root:
        if child.tag == NS_CT + "Override" and (child.get("PartName") or "").lstrip("/").casefold() == part.casefold():
            return child.get("ContentType")
    ext = part.rsplit(".", 1)[-1].casefold() if "." in part.rsplit("/", 1)[-1] else ""
    for child in types_root:
        if child.tag == NS_CT + "Default" and (child.get("Extension") or "").casefold() == ext:
            return child.get("ContentType")
    return None


def _ooxml_relationships(root):
    return [((c.get("Type") or ""), (c.get("Target") or ""), (c.get("TargetMode") or "Internal"))
            for c in root if c.tag == NS_REL + "Relationship"]


def _ooxml_relationship_problems(name, root, present):
    if root.tag != NS_REL + "Relationships":
        return ["invalid relationships root"]
    if name == "_rels/.rels":
        base = ""
    elif name.startswith("_rels/"):
        base = ""
        if name[6:-5] not in present:
            return ["relationship source part missing"]
    elif "/_rels/" in name:
        folder, filename = name.rsplit("/_rels/", 1)
        base = folder + "/"
        if base + filename[:-5] not in present:
            return ["relationship source part missing"]
    else:
        return ["invalid relationship part location"]
    problems, ids = [], set()
    for rel in root:
        rid = rel.get("Id")
        if rel.tag != NS_REL + "Relationship" or not rid or rid in ids or not rel.get("Type"):
            problems.append("invalid or duplicate relationship")
        ids.add(rid)
        mode = rel.get("TargetMode", "Internal")
        if mode == "Internal":
            target = _ooxml_resolve(base, rel.get("Target"))
            if target is None or target not in present:
                problems.append("internal relationship target missing or unsafe")
        elif mode != "External" or not rel.get("Target"):
            problems.append("invalid relationship target mode or empty target")
        if len(problems) >= ZIP_MAX_UNREAD:
            break
    return problems


DOCX_SKIP_TAGS = frozenset(NS_W + t for t in ("del", "delText", "instrText", "moveFrom", "rPr", "pPr", "fldChar"))
DOCX_UNREAD_TAGS = frozenset({NS_W + "drawing", NS_W + "pict", NS_W + "object", NS_W + "altChunk",
                              "{http://schemas.openxmlformats.org/markup-compatibility/2006}AlternateContent"})
DOCX_IGNORED_BLOCK_TAGS = frozenset(NS_W + t for t in (
    "sectPr", "bookmarkStart", "bookmarkEnd", "proofErr", "permStart", "permEnd", "commentRangeStart",
    "commentRangeEnd", "moveFromRangeStart", "moveFromRangeEnd", "moveToRangeStart", "moveToRangeEnd",
    "del", "moveFrom", "tblPr", "tblGrid", "trPr", "tcPr", "tblPrEx", "sdtPr", "sdtEndPr"))
DOCX_CONTAINER_TAGS = frozenset(NS_W + t for t in ("customXml", "sdtContent", "ins", "moveTo", "smartTag"))
NS_M = "{http://schemas.openxmlformats.org/officeDocument/2006/math}"
# Inline handling is an explicit allowlist; anything else inside a paragraph is reported unread.
DOCX_INLINE_CONTAINER_TAGS = frozenset(NS_W + t for t in (
    "r", "hyperlink", "smartTag", "customXml", "sdt", "sdtContent", "ins", "moveTo", "fldSimple", "dir", "bdo"))
DOCX_INLINE_IGNORED_TAGS = DOCX_SKIP_TAGS | frozenset(NS_W + t for t in (
    "sdtPr", "sdtEndPr", "smartTagPr", "customXmlPr", "bookmarkStart", "bookmarkEnd", "proofErr", "permStart", "permEnd",
    "commentRangeStart", "commentRangeEnd", "moveFromRangeStart", "moveFromRangeEnd",
    "moveToRangeStart", "moveToRangeEnd", "lastRenderedPageBreak"))
DOCX_INLINE_CHARS = {NS_W + "tab": "\t", NS_W + "ptab": "\t", NS_W + "br": "\n", NS_W + "cr": "\n",
                     NS_W + "noBreakHyphen": "‑", NS_W + "softHyphen": "­"}
DOCX_INLINE_UNREAD = {
    NS_W + "sym": ("other", "symbol character (font-specific w:sym) not read"),
    NS_W + "footnoteReference": ("other", "footnote reference; note content not inspected"),
    NS_W + "endnoteReference": ("other", "endnote reference; note content not inspected"),
    NS_W + "commentReference": ("other", "comment reference; comment content not inspected"),
    NS_W + "ruby": ("other", "ruby (phonetic guide) text not read"),
    NS_W + "contentPart": ("embedded_object", "content part not read"),
    NS_M + "oMath": ("other", "equation (OMML) not read"),
    NS_M + "oMathPara": ("other", "equation (OMML) not read"),
}


def _local(tag):
    return tag.rsplit("}", 1)[-1] if type(tag) is str else "?"


def _docx_unread_kind(elem):
    """Classify a drawing/picture/object subtree without decoding any binary data."""
    kind = "embedded_object" if elem.tag in (NS_W + "object", NS_W + "altChunk") else "other"
    for count, node in enumerate(elem.iter()):
        if count > 2000:
            break
        name = _local(node.tag)
        if name in ("blip", "imagedata"):
            return "image"
    return kind


def _docx_inline(elem, out, found, depth):
    """Collect run text of one paragraph through an explicit allowlist of containers.

    Only DOCX_INLINE_CONTAINER_TAGS are recursed into. Anything not handled
    explicitly (OMML and other foreign namespaces, w:sym, note/comment
    references, unknown tags) is reported in `found` instead of being dropped;
    text already collected is kept.
    """
    if depth > DOCX_MAX_XML_DEPTH:
        found.append(("other", "inline structure too deep; not read"))
        return
    for child in elem:
        tag = child.tag
        if tag == NS_W + "t":
            out.append(child.text or "")
        elif tag in DOCX_INLINE_CHARS:
            out.append(DOCX_INLINE_CHARS[tag])
        elif tag in DOCX_INLINE_IGNORED_TAGS:
            continue
        elif tag in DOCX_INLINE_CONTAINER_TAGS:
            _docx_inline(child, out, found, depth + 1)
        elif tag in DOCX_UNREAD_TAGS:
            kind = _docx_unread_kind(child)
            found.append((kind, f"{_local(tag)} content (image, shape, textbox or object) not read"))
        elif tag in DOCX_INLINE_UNREAD:
            found.append(DOCX_INLINE_UNREAD[tag])
        elif tag in (NS_W + "p", NS_W + "tbl"):
            found.append(("other", "nested block inside paragraph not read"))
        else:
            found.append(("unsupported_format", f"unsupported inline element {_local(tag)[:64]} not read"))


def _docx_region(ctx, kind, detail, number=None, cell=None):
    location = None
    if number is not None:
        location = {"paragraph": number}
        if cell:
            location.update({"table": cell[0], "row": cell[1], "column": cell[2]})
    if len(ctx["unread"]) < ZIP_MAX_UNREAD:
        ctx["unread"].append({"kind": kind, "location": location, "detail": str(detail)[:MAX_REASON_CHARS]})
    elif len(ctx["unread"]) == ZIP_MAX_UNREAD:
        ctx["unread"].append({"kind": "truncated", "location": None, "detail": "unread region list truncated"})


def _docx_paragraph(p, ctx, cell):
    ctx["paragraphs"] += 1
    number = ctx["paragraphs"]
    if number % 200 == 0 and time.monotonic() > ctx["deadline"]:
        _docx_region(ctx, "budget_limit", "time budget reached; remaining body not read", number, cell)
        ctx["stopped"] = True
        return
    out, found = [], []
    _docx_inline(p, out, found, 0)
    ctx["images"] += sum(1 for kind, _ in found if kind == "image")
    for kind, detail in dict.fromkeys(found):  # one located region per distinct fact
        _docx_region(ctx, kind, detail, number, cell)
    raw = "".join(out)
    if not raw:
        return
    if len(raw) > ctx["max_chars"] or _combining_run_exceeded(raw):
        _docx_region(ctx, "truncated" if len(raw) > ctx["max_chars"] else "parse_error",
                     "paragraph over text budget or has an overlong combining-mark run; not normalized", number, cell)
        ctx["stopped"] = len(raw) > ctx["max_chars"]
        return
    norm = unicodedata.normalize("NFC", raw).replace("\r\n", "\n").replace("\r", "\n")
    separator = 1 if ctx["parts"] else 0
    if ctx["total"] + separator + len(norm) > ctx["max_chars"] or len(ctx["locations"]) >= MAX_RESULT_ITEMS:
        _docx_region(ctx, "truncated", "text or location budget reached; remaining body not read", number, cell)
        ctx["stopped"] = True
        return
    if separator:
        ctx["parts"].append("\n")
        ctx["total"] += 1
    location = {"paragraph": number, "char_start": ctx["total"], "char_end": ctx["total"] + len(norm), "label": "paragraph"}
    if cell:
        location.update({"table": cell[0], "row": cell[1], "column": cell[2], "label": "table_cell"})
    ctx["locations"].append(location)
    ctx["parts"].append(norm)
    ctx["total"] += len(norm)


def _docx_table(tbl, ctx, depth):
    ctx["tables"] += 1
    table_no = ctx["tables"]
    row_no = 0
    for row in tbl:
        if ctx["stopped"]:
            return
        if row.tag in DOCX_IGNORED_BLOCK_TAGS:
            continue
        if row.tag != NS_W + "tr":
            _docx_region(ctx, "other", f"table child {_local(row.tag)} not read")
            continue
        row_no += 1
        col_no = 0  # cell ordinal in the row; gridSpan/vMerge are not resolved
        for cell in row:
            if cell.tag in DOCX_IGNORED_BLOCK_TAGS:
                continue
            if cell.tag != NS_W + "tc":
                _docx_region(ctx, "other", f"table row child {_local(cell.tag)} not read")
                continue
            col_no += 1
            _docx_blocks(cell, ctx, depth + 1, (table_no, row_no, col_no))


def _docx_blocks(container, ctx, depth, cell):
    if depth > DOCX_MAX_BLOCK_DEPTH:
        _docx_region(ctx, "other", "block nesting too deep; not read")
        return
    for child in container:
        if ctx["stopped"]:
            return
        tag = child.tag
        if tag == NS_W + "p":
            _docx_paragraph(child, ctx, cell)
        elif tag == NS_W + "tbl":
            _docx_table(child, ctx, depth)
        elif tag == NS_W + "sdt" or tag in DOCX_CONTAINER_TAGS:
            _docx_blocks(child, ctx, depth + 1, cell)
        elif tag in DOCX_IGNORED_BLOCK_TAGS or tag == NS_W + "tcPr":
            continue
        elif tag in DOCX_UNREAD_TAGS:
            kind = _docx_unread_kind(child)
            if kind == "image":
                ctx["images"] += 1
            _docx_region(ctx, kind, f"{_local(tag)} block not read")
        else:
            key = _local(tag)
            if key not in ctx["unsupported"]:
                ctx["unsupported"].add(key)
                _docx_region(ctx, "unsupported_format", f"unsupported block element {key[:64]} not read")


def _parse_docx(data, size, limits, soft_deadline):
    """DOCX body paragraphs/tables. Pagination and layout are never measured (pages stays null)."""
    zf, infos, problems, error = _zip_open(data, size, limits, "docx")
    if error is not None:
        return error
    state = {"budget": limits["max_uncompressed_bytes"], "used": 0, "deadline": soft_deadline}
    ctx = {"parts": [], "locations": [], "unread": [], "total": 0, "max_chars": limits["max_text_chars"],
           "paragraphs": 0, "tables": 0, "images": 0, "deadline": soft_deadline, "stopped": False, "unsupported": set()}
    entries, xml_parts, present = [], {}, set()
    collected = 0

    # Read every entry under the shared budget (CRC/hash); keep XML parts in memory, bounded.
    for index, info in enumerate(infos):
        record, safe, raw_name = _zip_new_record(index, info, problems)
        entries.append(record)
        if not safe:
            _docx_region(ctx, "parse_error", f"entry {index}: unsafe name or type: " + "; ".join(record["unsafe_reasons"]))
            continue
        if record["is_dir"]:
            problem = _zip_check_directory(zf, info, record, state)
            if problem is not None:
                _docx_region(ctx, problem[0], f"entry {index}: {problem[1]}")
            continue
        present.add(raw_name)
        if (info.external_attr >> 16) & 0o170000 == 0o040000 or info.external_attr & 0x10:
            _docx_region(ctx, "parse_error", f"entry {index}: file entry marked as directory in attributes")
        if record["encrypted"]:
            _docx_region(ctx, "encrypted", f"entry {index}: encrypted entry; content not read")
            continue
        if state["used"] > state["budget"] or time.monotonic() > state["deadline"]:
            _docx_region(ctx, "budget_limit", f"entry {index}: read budget exhausted before this entry")
            continue
        wants_xml = raw_name.endswith((".xml", ".rels"))
        cap = min(DOCX_MAX_PART_BYTES, DOCX_MAX_COLLECT_BYTES - collected) if wants_xml else 0
        read = _zip_read_entry(zf, info, state, max(cap, 0))
        record["bytes_read"], record["crc_ok"], record["sha256"] = read["bytes_read"], read["crc_ok"], read["sha256"]
        if read["problem"] is not None:
            _docx_region(ctx, read["problem"][0], f"entry {index}: {read['problem'][1]}")
            continue
        if read["collected"] is not None:
            xml_parts[raw_name] = read["collected"]
            collected += len(read["collected"])
        elif wants_xml:
            _docx_region(ctx, "budget_limit", f"entry {index}: XML part over its parser budget; not inspected")

    for name, raw in xml_parts.items():
        root, problem = _safe_xml_root(raw, soft_deadline)
        if problem:
            _docx_region(ctx, "parse_error", f"{name}: {problem}")
        elif name.endswith(".rels"):
            for problem in _ooxml_relationship_problems(name, root, present):
                _docx_region(ctx, "parse_error", f"{name}: {problem}")

    def finish(status, reason, kind=None):
        if reason is not None and kind is not None:
            _docx_region(ctx, kind, reason)
        result = _result("docx", status, reason, size)
        metrics = result["metrics"]
        metrics.update({"paragraphs": ctx["paragraphs"], "tables": ctx["tables"], "images": ctx["images"],
                        "entries": len(infos), "uncompressed_bytes_read": state["used"]})
        if status == "complete":
            metrics.update(_text_metrics("".join(ctx["parts"])))  # counts only when the whole body was read
        result["text"] = "".join(ctx["parts"])
        result["locations"] = ctx["locations"]
        result["unread_regions"] = ctx["unread"]
        result["archive_entries"] = entries
        return result

    def required(name):
        """Return (root, problem_status, reason) for a required XML part."""
        raw = xml_parts.get(name)
        if raw is None:
            status = "incomplete" if name in present else "failed"
            return None, status, f"required package part {name} missing or unread"
        root, problem = _safe_xml_root(raw, soft_deadline)
        return root, ("incomplete" if problem else None), (f"{name}: {problem}" if problem else None)

    types_root, status, reason = required("[Content_Types].xml")
    if types_root is None:
        return finish(status, reason, "parse_error")
    rels_root, status, reason = required("_rels/.rels")
    if rels_root is None:
        return finish(status, reason, "parse_error")
    if types_root.tag != NS_CT + "Types" or rels_root.tag != NS_REL + "Relationships":
        return finish("failed", "not an OOXML package (content types or relationships root mismatch)", "parse_error")
    mains = [target for rtype, target, mode in _ooxml_relationships(rels_root)
             if rtype == REL_OFFICE_DOCUMENT and mode == "Internal"]
    if len(mains) != 1:
        return finish("failed" if not mains else "incomplete",
                      "transitional officeDocument relationship missing or ambiguous", "parse_error")
    main = _ooxml_resolve("", mains[0])
    if main is None:
        return finish("incomplete", "main document target unsafe or unsupported", "parse_error")
    content_type = _ooxml_content_type(types_root, main)
    if content_type != DOCX_MAIN_TYPE:
        return finish("failed", f"main part content type is not WordprocessingML document: {str(content_type)[:120]}", "parse_error")
    doc_root, status, reason = required(main)
    if doc_root is None:
        return finish(status, reason, "parse_error")
    body = doc_root.find(NS_W + "body") if doc_root.tag == NS_W + "document" else None
    if body is None:
        return finish("failed", "main part has no w:document/w:body", "parse_error")

    # Document-part relationships: images, objects, headers/footers/notes are not inspected.
    base_dir = main.rsplit("/", 1)[0] + "/" if "/" in main else ""
    rels_name = base_dir + "_rels/" + main.rsplit("/", 1)[-1] + ".rels"
    if rels_name in present:
        part_rels, problem = (None, "unread") if rels_name not in xml_parts else _safe_xml_root(xml_parts[rels_name], soft_deadline)
        if part_rels is None:
            _docx_region(ctx, "parse_error", f"{rels_name}: {problem}; related parts unknown")
        else:
            counts = {}
            for rtype, _target, mode in _ooxml_relationships(part_rels):
                suffix = "/" + rtype.rsplit("/", 1)[-1]
                if suffix in REL_UNREAD_KINDS or (mode != "Internal" and suffix != "/hyperlink"):
                    key = (REL_UNREAD_KINDS.get(suffix, "other"), suffix[1:], mode != "Internal")
                    counts[key] = counts.get(key, 0) + 1
            for (kind, name, external), count in sorted(counts.items()):
                where = "external, not fetched" if external else "not inspected"
                _docx_region(ctx, kind, f"{count} {name[:40]} relationship(s) {where}")

    _docx_blocks(body, ctx, 0, None)
    if ctx["paragraphs"] == 0 or not ctx["parts"]:
        _docx_region(ctx, "other", "document body has no extractable paragraph text")
    complete = not ctx["unread"]
    reason = None if complete else f"{len(ctx['unread'])} unread or uninspected region(s); see unread_regions"
    return finish("complete" if complete else "incomplete", reason)


# ---------------------------------------------------------------------------
# Worker process boundary
# ---------------------------------------------------------------------------

def _parse_office(data, size, limits, deadline, fmt):
    zf, infos, problems, error = _zip_open(data, size, limits, fmt)
    if error is not None:
        return error
    result = _result(fmt, "complete", None, size)
    state = {"budget": limits["max_uncompressed_bytes"], "used": 0, "deadline": deadline}
    xml_parts, used, parts, chars = {}, 0, [], 0
    present = set()
    unread = result["unread_regions"]

    def issue(kind, detail, label=None):
        if len(unread) < ZIP_MAX_UNREAD:
            unread.append({"kind": kind, "location": {"label": label} if label else None,
                           "detail": str(detail)[:MAX_REASON_CHARS]})

    def emit(text, label):
        nonlocal chars
        if not text:
            return
        text = unicodedata.normalize("NFC", text).replace("\r\n", "\n").replace("\r", "\n")
        if chars + len(text) + 1 > limits["max_text_chars"] or len(result["locations"]) >= MAX_RESULT_ITEMS:
            issue("budget_limit", "shared text or location budget exhausted", label)
            return
        start = chars
        parts.append(text + "\n")
        chars += len(text) + 1
        result["locations"].append({"char_start": start, "char_end": chars - 1, "label": label[:256]})

    for index, info in enumerate(infos):
        record, safe, name = _zip_new_record(index, info, problems)
        result["archive_entries"].append(record)
        if not safe:
            issue("parse_error", f"unsafe package entry {index}")
            continue
        if record["is_dir"]:
            problem = _zip_check_directory(zf, info, record, state)
            if problem:
                issue(problem[0], problem[1])
            continue
        if (info.external_attr >> 16) & 0o170000 == 0o040000 or info.external_attr & 0x10:
            issue("parse_error", f"package entry {index} has directory attributes")
            continue
        present.add(name)
        if record["encrypted"]:
            issue("encrypted", f"encrypted package entry {index}")
            continue
        wants_xml = name.endswith((".xml", ".rels"))
        cap = max(0, min(DOCX_MAX_PART_BYTES, DOCX_MAX_COLLECT_BYTES - used)) if wants_xml else 0
        read = _zip_read_entry(zf, info, state, cap)
        record.update(bytes_read=read["bytes_read"], crc_ok=read["crc_ok"], sha256=read["sha256"])
        if read["problem"]:
            issue(read["problem"][0], read["problem"][1], name)
        elif wants_xml:
            if read["collected"] is None:
                issue("budget_limit", "XML part exceeds collection budget", name)
            else:
                xml_parts[name] = read["collected"]
                used += len(read["collected"])
        elif "/media/" in name:
            issue("image", "embedded image pixels not inspected", name)
        else:
            issue("embedded_object", "non-XML package content not interpreted", name)
    zf.close()
    result["metrics"].update(entries=len(infos), uncompressed_bytes_read=state["used"])
    roots = {}

    def xml(name, required=True):
        if name in roots:
            return roots[name]
        raw = xml_parts.get(name)
        if raw is None:
            if required:
                issue("parse_error", "required XML part missing or unread", name)
            return None
        root, problem = _safe_xml_root(raw, deadline)
        if problem:
            issue("parse_error", problem, name)
        roots[name] = root
        return root

    def relationships(part):
        base = part.rsplit("/", 1)[0] + "/" if "/" in part else ""
        name = base + "_rels/" + part.rsplit("/", 1)[-1] + ".rels" if part else "_rels/.rels"
        root = xml(name, required=not part)
        found = {}
        if root is not None:
            if root.tag != NS_REL + "Relationships":
                issue("parse_error", "invalid relationships root", name)
                return found
            for rel in root:
                rid = rel.get("Id")
                if not rid or rid in found:
                    issue("parse_error", "missing or duplicate relationship ID", name)
                    continue
                typ = rel.get("Type", "").rsplit("/", 1)[-1]
                mode = rel.get("TargetMode", "Internal")
                target = _ooxml_resolve(base, rel.get("Target")) if mode == "Internal" else None
                found[rid] = (typ, target)
                if mode != "Internal" and typ != "hyperlink":
                    issue("embedded_object", "external content not fetched", name)
        return found

    for part_name in xml_parts:
        part_root = xml(part_name)
        if part_root is not None and part_name.endswith(".rels"):
            for problem in _ooxml_relationship_problems(part_name, part_root, present):
                issue("parse_error", problem, part_name)
    types = xml("[Content_Types].xml")
    mains = [target for typ, target in relationships("").values() if typ == "officeDocument"]
    main = mains[0] if len(mains) == 1 else None
    expected = "application/vnd.openxmlformats-officedocument." + (
        "presentationml.presentation" if fmt == "pptx" else "spreadsheetml.sheet") + ".main+xml"
    if types is None or types.tag != NS_CT + "Types" or not main or _ooxml_content_type(types, main) != expected:
        issue("parse_error", "package main part or content type does not match " + fmt)
    else:
        root = xml(main)
        rels = relationships(main)
        for related_type, _ in rels.values():
            if related_type in {"slideMaster", "notesMaster", "externalLink"}:
                issue("embedded_object", related_type + " content not interpreted", main)
        ns = "{http://schemas.openxmlformats.org/" + ("presentationml" if fmt == "pptx" else "spreadsheetml") + "/2006/main}"
        rns = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"
        expected_root = ns + ("presentation" if fmt == "pptx" else "workbook")
        if root is not None and root.tag != expected_root:
            issue("parse_error", "invalid main document root", main)
            root = None
        children = [] if root is None else root.findall(".//" + ns + ("sldId" if fmt == "pptx" else "sheet"))
        shared = []
        if fmt == "xlsx":
            for typ, target in rels.values():
                if typ == "sharedStrings" and target:
                    sr = xml(target)
                    if sr is not None:
                        shared = ["".join(t.text or "" for t in si.iter(ns + "t")) for si in sr.findall(ns + "si")]
        for number, child in enumerate(children, 1):
            if time.monotonic() > deadline:
                issue("budget_limit", "time budget reached")
                break
            typ, target = rels.get(child.get(rns + "id"), (None, None))
            wanted = "slide" if fmt == "pptx" else "worksheet"
            if typ != wanted or not target:
                issue("parse_error", "missing or unsafe " + wanted + " relationship")
                continue
            doc = xml(target)
            if doc is None:
                continue
            expected_tag = ns + ("sld" if fmt == "pptx" else "worksheet")
            if doc.tag != expected_tag:
                issue("parse_error", "invalid " + wanted + " root", target)
                continue
            related = relationships(target)
            for related_type, _ in related.values():
                if related_type not in {"hyperlink", "image"}:
                    issue("embedded_object", related_type + " content not interpreted", target)
            if fmt == "pptx":
                ans = "{http://schemas.openxmlformats.org/drawingml/2006/main}"
                for paragraph, node in enumerate(doc.iter(ans + "p"), 1):
                    emit("".join(t.text or "" for t in node.iter(ans + "t")), f"slide:{number} paragraph:{paragraph}")
                if doc.find(".//" + ns + "graphicFrame") is not None:
                    issue("embedded_object", "graphic frame layout or chart not rendered", f"slide:{number}")
            else:
                sheet = child.get("name", str(number))[:100]
                for cell in doc.iter(ns + "c"):
                    label = f"sheet:{sheet} cell:{cell.get('r', '?')}"
                    value = cell.findtext(ns + "v", "")
                    cell_type = cell.get("t")
                    if cell_type == "inlineStr":
                        value = "".join(t.text or "" for t in cell.iter(ns + "t"))
                    elif cell_type == "s":
                        if value.isdigit() and int(value) < len(shared):
                            value = shared[int(value)]
                        else:
                            issue("parse_error", "shared-string index missing or invalid", label)
                            value = ""
                    formula = cell.find(ns + "f")
                    if formula is not None:
                        value = f"formula: {formula.text or ''}; cached value: {value}"
                        issue("other", "formula read; cached result not recalculated", label)
                    if cell_type == "e":
                        issue("other", "spreadsheet error value: " + value, label)
                    emit(value, label)
                if child.get("state", "visible") != "visible":
                    issue("other", "hidden sheet included in text extraction", f"sheet:{sheet}")
        if fmt == "pptx":
            result["pages"] = len(children) if not any(r["kind"] == "parse_error" for r in unread) else None
    result["text"] = "".join(parts)
    if unread:
        result["status"] = "failed" if any(r["kind"] == "parse_error" for r in unread) else "incomplete"
        result["reason"] = f"{len(unread)} unread or unverified region(s)"
    else:
        result["metrics"].update(_text_metrics(result["text"]))
    return result


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
    if not all(_valid_int(metrics[k]) for k in COUNT_METRIC_KEYS):
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

    limits: optional dict with max_input_bytes (non-PDF input, int <= 1 MiB),
    max_text_chars (int <= 100000), max_pdf_bytes (int <= 50 MiB),
    max_pdf_pages (int <= 300), max_archive_bytes (int <= 50 MiB),
    max_archive_entries (int <= 1000), max_uncompressed_bytes (int <= 200 MiB)
    and timeout_seconds (finite number, 0 < t <= 30).
    These are parser budgets, never assignment limits.
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
    budget_key = {"pdf": "max_pdf_bytes", "zip": "max_archive_bytes"}.get(hint, "max_input_bytes")
    if size > raw_limits[budget_key]:
        reason = f"input {size} bytes exceeds parser budget {raw_limits[budget_key]} bytes; not read"
        return _result(hint, "incomplete", reason, size, "budget_limit")
    data = bytes(data)
    worker_limits = dict(raw_limits)
    worker_limits[TIMEOUT_KEY] = timeout  # lets the worker stop early with partial PDF facts
    try:
        return _run_worker(data, filename, worker_limits, timeout, size, hint)
    except Exception as exc:
        return _result(hint, "failed", f"parser worker could not run: {type(exc).__name__}", size, "other")
