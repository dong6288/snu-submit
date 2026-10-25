"""Bounded archive structure and opt-in text-section probes (profile ``archive-profile/1``).

inspect_archive(path, profile) reads ONE local ZIP through core.read_file_bound
(no links, stable identity, SHA-256), lists it in memory with the parser's
bounded ZIP helpers (50 MiB file, 1000 entries, 200 MiB actual decompressed
reads, CRC checks, unsafe/duplicate name detection, no extraction) and runs
host-supplied probes. Nothing is executed, extracted or written.

Profile (all keys required, no extra keys):

    {"profile_version": "archive-profile/1",
     "required_entries": [{"path", "reason", "evidence"}],
     "nonempty_entries": [{"path", "reason", "evidence", "encoding": "utf-8" | null}],
     "text_sections":    [{"path", "start_literal", "end_literal", "encoding": "utf-8", "reason", "evidence"}],
     "existing_issues":  [{"status": "fail" | "unknown", "reason", "evidence"}]}

* ``path`` is a forward-slash path INSIDE the archive. A required path matches
  an exact entry, or an implicit directory when some entry lies below it.
* ``nonempty_entries``: with ``encoding: "utf-8"`` the entry is text (strict
  UTF-8, optional leading BOM) and empty or whitespace-only text fails; with
  ``null`` only a 0-byte entry fails (binary bytes are never judged empty).
* ``text_sections``: a marker matches a line whose text, after removing
  leading/trailing whitespace, equals the literal exactly (no regex, no prefix
  match). Exactly one start and one end line are required, end after start,
  with a non-blank body between them. Several matching lines are ambiguous.
* ``reason``/``evidence`` are host-supplied provenance; they are echoed but
  never treated as proof of what an instructor wrote.

Validation: a profile must contain at least one required/nonempty/text-section
probe (existing_issues alone are rejected), so there is no vacuous pass.

Result ``overall``: ``needs_changes`` if any probe or existing issue fails,
``needs_confirmation`` if anything is unknown, the archive was not read
(missing, unreadable, corrupt, over budget) or changed during inspection, any
archive issue exists or any existing issue remains, otherwise
``profile_pass``. profile_pass is NOT a submission-ready verdict.
"""

import os
import re
import time

from . import parsers
from .core import ValidationError, read_file_bound

PROFILE_VERSION = "archive-profile/1"
RESULT_VERSION = "archive-probe-result/1"
SCOPE = "archive structural/text-profile only; online collection, native DB validity, correctness not certified"
EVIDENCE_NOTE = "host-supplied provenance; not independently verified against instructor instructions"
MAX_ARCHIVE_BYTES = parsers.MAX_ARCHIVE_BYTES
TEXT_ENTRY_MAX_BYTES = 8 * 1024 * 1024
TIME_BUDGET_SECONDS = 30
MAX_PROBES_PER_LIST = 200
MAX_REASON_CHARS = 512
MAX_EVIDENCE_CHARS = 1024
MAX_LITERAL_CHARS = 256
EXCERPT_CHARS = 200
_PROFILE_KEYS = {"profile_version", "required_entries", "nonempty_entries", "text_sections", "existing_issues"}
_ITEM_KEYS = {
    "required_entries": {"path", "reason", "evidence"},
    "nonempty_entries": {"path", "reason", "evidence", "encoding"},
    "text_sections": {"path", "start_literal", "end_literal", "encoding", "reason", "evidence"},
    "existing_issues": {"status", "reason", "evidence"},
}
# Same grammar as the schemas' rel_path definition.
_SAFE_PATH_RE = re.compile(r'(?!/)(?![A-Za-z]:)(?!.*//)(?!.*/$)(?!(?:.*/)?\.{1,2}(?:/|$))[^\\:*?"<>|\x00-\x1f]{1,1024}')


def _text(value, limit, what):
    if type(value) is not str or not value.strip() or len(value) > limit:
        raise ValidationError(f"profile {what} must be a non-blank string of at most {limit} characters")
    return value


def _validate_profile(profile):
    if type(profile) is not dict or set(profile) != _PROFILE_KEYS:
        raise ValidationError("profile must have exactly the archive-profile/1 keys")
    if profile["profile_version"] != PROFILE_VERSION:
        raise ValidationError("unsupported profile_version")
    for key, keys in _ITEM_KEYS.items():
        items = profile[key]
        if type(items) is not list or len(items) > MAX_PROBES_PER_LIST:
            raise ValidationError(f"profile {key} must be a list of at most {MAX_PROBES_PER_LIST} items")
        seen = set()
        for item in items:
            if type(item) is not dict or set(item) != keys:
                raise ValidationError(f"profile {key} items must have exactly the documented keys")
            _text(item["reason"], MAX_REASON_CHARS, "reason")
            _text(item["evidence"], MAX_EVIDENCE_CHARS, "evidence")
            if "path" in item and (type(item["path"]) is not str or not _SAFE_PATH_RE.fullmatch(item["path"])):
                raise ValidationError(f"profile {key} path must be a safe relative archive path")
            if key == "nonempty_entries" and item["encoding"] not in ("utf-8", None):
                raise ValidationError("nonempty encoding must be 'utf-8' or null")
            if key == "text_sections":
                if item["encoding"] != "utf-8":
                    raise ValidationError("text_sections encoding must be 'utf-8'")
                for literal in (item["start_literal"], item["end_literal"]):
                    _text(literal, MAX_LITERAL_CHARS, "marker literal")
                    if literal != literal.strip() or any(ord(ch) < 32 or ord(ch) == 127 for ch in literal):
                        raise ValidationError("marker literals must be single trimmed lines without control characters")
                if item["start_literal"] == item["end_literal"]:
                    raise ValidationError("start and end literals must differ")
            if key == "existing_issues" and item["status"] not in ("fail", "unknown"):
                raise ValidationError("existing issue status must be 'fail' or 'unknown'")
            identity = tuple(sorted((k, str(v)) for k, v in item.items() if k not in ("reason", "evidence")))
            if key != "existing_issues" and identity in seen:
                raise ValidationError(f"profile {key} contains a duplicate probe")
            seen.add(identity)
    if not (profile["required_entries"] or profile["nonempty_entries"] or profile["text_sections"]):
        # existing_issues alone are not probes; an empty profile could only "pass" vacuously.
        raise ValidationError("profile must contain at least one required_entries, nonempty_entries or text_sections probe")


def _probe(kind, index, item, status, code, detail, expected, observed=None):
    return {"probe": kind, "index": index, "path": item.get("path"), "status": status, "reason_code": code,
            "detail": str(detail)[:MAX_REASON_CHARS], "expected": expected, "observed": observed,
            "profile_reason": item["reason"], "profile_evidence": item["evidence"], "evidence_note": EVIDENCE_NOTE}


def _decode(data):
    """Strict UTF-8 (one leading BOM allowed). Returns (text, problem_code)."""
    if data.startswith(b"\xef\xbb\xbf"):
        data = data[3:]
    try:
        text = data.decode("utf-8", errors="strict")
    except UnicodeDecodeError:
        return None, "decode_failed"
    if "\x00" in text:
        return None, "nul_character"
    return text, None


def _metadata_problem(info):
    """File/directory type disagreement between the entry name and its attributes, or None.

    A name without a trailing '/' must not carry Unix S_IFDIR or the DOS
    directory bit (0x10); a name with a trailing '/' must not carry Unix
    regular-file mode. Mirrors the shared parser's metadata checks.
    """
    mode = (info.external_attr >> 16) & 0o170000
    if info.is_dir():
        if mode == 0o100000:
            return ("metadata_mismatch", "directory name with regular-file attributes")
    elif mode == 0o040000 or info.external_attr & 0x10:
        return ("metadata_mismatch", "file name with directory attributes")
    return None


class _Archive:
    """In-memory listing of one archive with per-entry facts and bounded collected text."""

    def __init__(self, data, text_paths):
        limits = dict(parsers.DEFAULT_LIMITS)
        self.issues, self.entries, self.by_path = [], [], {}
        self.unsafe_present = False
        self.error = None
        zf, infos, problems, error = parsers._zip_open(data, len(data), limits, "zip")
        if error is not None:
            self.error = error["reason"] or "archive structure unreadable"
            self.used = 0
            return
        state = {"budget": limits["max_uncompressed_bytes"], "used": 0,
                 "deadline": time.monotonic() + TIME_BUDGET_SECONDS}
        for index, info in enumerate(infos):
            record, safe, raw_name = parsers._zip_new_record(index, info, problems)
            problem, collected, too_large = None, None, False
            if not safe:
                self.unsafe_present = True
                problem = ("unsafe_names", "; ".join(record["unsafe_reasons"]))
            metadata = _metadata_problem(info)
            if metadata is not None:
                problem = problem or metadata  # entry type is ambiguous: never read or pass it
            elif record["is_dir"]:
                problem = parsers._zip_check_directory(zf, info, record, state) or problem
            elif record["encrypted"]:
                problem = problem or ("encrypted", "encrypted entry; content not read")
            elif state["used"] > state["budget"] or time.monotonic() > state["deadline"]:
                problem = problem or ("budget_limit", "read budget exhausted before this entry")
            else:
                cap = TEXT_ENTRY_MAX_BYTES if safe and record["entry_path"] in text_paths else 0
                read = parsers._zip_read_entry(zf, info, state, cap)
                record["bytes_read"], record["crc_ok"], record["sha256"] = read["bytes_read"], read["crc_ok"], read["sha256"]
                if read["problem"] is not None:
                    problem = problem or read["problem"]
                collected, too_large = read["collected"], read["too_large"]
            entry = {"index": index, "path": record["entry_path"], "name": raw_name[:300] or "(empty name)",
                     "is_dir": record["is_dir"], "declared_size": record["declared_size"],
                     "bytes_read": record["bytes_read"], "sha256": record["sha256"], "crc_ok": record["crc_ok"],
                     "encrypted": record["encrypted"], "unsafe_reasons": record["unsafe_reasons"],
                     "problem": None if problem is None else {"code": problem[0], "detail": str(problem[1])[:MAX_REASON_CHARS]}}
            self.entries.append(entry)
            if problem is not None:
                self.issues.append({"code": problem[0], "entry_index": index, "detail": entry["problem"]["detail"]})
            if safe:
                self.by_path[record["entry_path"]] = (entry, collected, too_large)
        self.used = state["used"]

    def find(self, path):
        """Return ('exact', entry, collected, too_large) / ('implicit_directory', ...) / (None, ...)."""
        if path in self.by_path:
            return ("exact",) + self.by_path[path]
        if any(p.startswith(path + "/") for p in self.by_path):
            return "implicit_directory", None, None, False
        return None, None, None, False


def _absent(kind, index, item, archive, expected):
    if archive.unsafe_present:
        return _probe(kind, index, item, "unknown", "unsafe_names",
                      "unsafe or duplicate entry names prevent proving absence", expected)
    return _probe(kind, index, item, "fail", "missing_target", "entry not found in the archive", expected)


def _entry_observed(entry, how):
    return {"matched_as": how, "entry_sha256": entry["sha256"], "bytes": entry["bytes_read"], "is_dir": entry["is_dir"]}


def _required(index, item, archive):
    expected = {"path": item["path"]}
    how, entry, _, _ = archive.find(item["path"])
    if how is None:
        return _absent("required_entry", index, item, archive, expected)
    if how == "implicit_directory":
        return _probe("required_entry", index, item, "pass", "present", "entries exist below this directory path",
                      expected, {"matched_as": how, "entry_sha256": None, "bytes": None, "is_dir": True})
    if entry["problem"] is not None:
        return _probe("required_entry", index, item, "unknown", entry["problem"]["code"], entry["problem"]["detail"],
                      expected, _entry_observed(entry, how))
    return _probe("required_entry", index, item, "pass", "present", "entry present and fully read",
                  expected, _entry_observed(entry, how))


def _resolve_file(kind, index, item, archive, expected):
    """Common resolution for content probes. Returns (probe_or_None, entry, collected, too_large)."""
    how, entry, collected, too_large = archive.find(item["path"])
    if how is None:
        return _absent(kind, index, item, archive, expected), None, None, False
    if how == "implicit_directory" or entry["is_dir"]:
        return _probe(kind, index, item, "unknown", "directory_entry", "content probes apply to file entries",
                      expected), None, None, False
    if entry["problem"] is not None:
        return _probe(kind, index, item, "unknown", entry["problem"]["code"], entry["problem"]["detail"], expected,
                      _entry_observed(entry, how)), None, None, False
    return None, entry, collected, too_large


def _nonempty(index, item, archive):
    expected = {"path": item["path"], "encoding": item["encoding"]}
    probe, entry, collected, too_large = _resolve_file("nonempty_entry", index, item, archive, expected)
    if probe is not None:
        return probe
    observed = _entry_observed(entry, "exact")
    if entry["bytes_read"] == 0:
        return _probe("nonempty_entry", index, item, "fail", "zero_bytes", "entry has 0 bytes", expected, observed)
    if item["encoding"] is None:
        return _probe("nonempty_entry", index, item, "pass", "binary_nonzero_size",
                      "entry has bytes; binary content is not judged semantically", expected, observed)
    if too_large or collected is None:
        return _probe("nonempty_entry", index, item, "unknown", "budget_limit",
                      "text entry larger than the text budget", expected, observed)
    text, problem = _decode(collected)
    if problem:
        return _probe("nonempty_entry", index, item, "unknown", problem, "entry is not strict UTF-8 text", expected, observed)
    if not text.strip():
        return _probe("nonempty_entry", index, item, "fail", "whitespace_only", "text entry contains only whitespace",
                      expected, observed)
    return _probe("nonempty_entry", index, item, "pass", "nonempty", "text entry has non-whitespace characters",
                  expected, observed)


def _section(index, item, archive):
    expected = {"path": item["path"], "start_literal": item["start_literal"], "end_literal": item["end_literal"],
                "encoding": item["encoding"], "match": "whole line after trimming surrounding whitespace"}
    probe, entry, collected, too_large = _resolve_file("text_section", index, item, archive, expected)
    if probe is not None:
        return probe
    observed = _entry_observed(entry, "exact")
    if too_large or collected is None:
        return _probe("text_section", index, item, "unknown", "budget_limit", "text entry larger than the text budget",
                      expected, observed)
    text, problem = _decode(collected)
    if problem:
        return _probe("text_section", index, item, "unknown", problem, "entry is not strict UTF-8 text", expected, observed)
    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    starts = [i for i, line in enumerate(lines) if line.strip() == item["start_literal"]]
    ends = [i for i, line in enumerate(lines) if line.strip() == item["end_literal"]]
    observed = dict(observed, start_lines=[i + 1 for i in starts[:10]], end_lines=[i + 1 for i in ends[:10]])
    if len(starts) > 1:
        return _probe("text_section", index, item, "unknown", "ambiguous_start", "start marker appears more than once",
                      expected, observed)
    if len(ends) > 1:
        return _probe("text_section", index, item, "unknown", "ambiguous_end", "end marker appears more than once",
                      expected, observed)
    if not starts:
        return _probe("text_section", index, item, "fail", "missing_start", "start marker line not found", expected, observed)
    if not ends:
        return _probe("text_section", index, item, "fail", "missing_end", "end marker line not found", expected, observed)
    start, end = starts[0], ends[0]
    if end <= start:
        return _probe("text_section", index, item, "fail", "end_before_start", "end marker precedes start marker",
                      expected, observed)
    offsets = [0]
    for line in lines:
        offsets.append(offsets[-1] + len(line) + 1)
    body = "\n".join(lines[start + 1:end])
    observed.update({"start_line": start + 1, "end_line": end + 1, "body_char_start": offsets[start + 1],
                     "body_char_end": offsets[start + 1] + len(body), "excerpt": body.strip()[:EXCERPT_CHARS]})
    if not body.strip():
        return _probe("text_section", index, item, "fail", "empty_section", "section has no non-blank body",
                      expected, observed)
    return _probe("text_section", index, item, "pass", "section_found",
                  "markers found once with a non-blank body (presence only, not semantic completeness)",
                  expected, observed)


def _read(path):
    if type(path) is not str or not path:
        raise ValidationError("path must be a non-empty string")
    root, name = os.path.split(os.path.abspath(path))
    return read_file_bound(root, name, MAX_ARCHIVE_BYTES)


def inspect_archive(path, profile):
    """Run archive-profile/1 probes against one local ZIP and return JSON-compatible facts."""
    _validate_profile(profile)
    text_paths = {i["path"] for i in profile["nonempty_entries"] if i["encoding"] == "utf-8"}
    text_paths |= {i["path"] for i in profile["text_sections"]}
    result = {"api_version": RESULT_VERSION, "profile_version": PROFILE_VERSION, "scope": SCOPE,
              "overall": None, "archive": None, "entries": [], "issues": [], "probes": [],
              "existing_issues": [dict(issue, label="existing_issue") for issue in profile["existing_issues"]]}
    archive, first = None, None
    try:
        first = _read(path)
    except ValidationError as exc:
        result["archive"] = {"file_name": os.path.basename(str(path))[:255], "status": "unreadable",
                             "reason": str(exc), "sha256": None, "size_bytes": None,
                             "stable_after_inspection": None, "entries_total": None, "uncompressed_bytes_read": None}
    if first is not None:
        archive = _Archive(first["data"], text_paths)
        result["archive"] = {"file_name": first["relative_path"], "status": "read" if archive.error is None else
                             "structure_unreadable", "reason": archive.error, "sha256": first["sha256"],
                             "size_bytes": first["size_bytes"], "stable_after_inspection": None,
                             "entries_total": len(archive.entries), "uncompressed_bytes_read": archive.used}
        result["entries"], result["issues"] = archive.entries, archive.issues
        first = dict(first, data=None)

    for kind, key, run in (("required_entry", "required_entries", _required),
                           ("nonempty_entry", "nonempty_entries", _nonempty),
                           ("text_section", "text_sections", _section)):
        for index, item in enumerate(profile[key]):
            if archive is None or archive.error is not None:
                expected = {"path": item["path"]}
                result["probes"].append(_probe(kind, index, item, "unknown", "archive_unreadable",
                                               "archive could not be read or listed", expected))
            else:
                result["probes"].append(run(index, item, archive))

    if first is not None:  # the input must be unchanged after inspection
        try:
            second = _read(path)
            stable = (second["sha256"] == first["sha256"] and second["file_identity"] == first["file_identity"]
                      and second["stat_before"] == first["stat_after"])
        except ValidationError:
            stable = False
        result["archive"]["stable_after_inspection"] = stable
        if not stable:
            result["issues"].append({"code": "archive_changed", "entry_index": None,
                                     "detail": "archive changed or became unreadable during inspection"})
            for probe in result["probes"]:
                if probe["status"] == "pass":
                    probe.update(status="unknown", reason_code="archive_changed")

    # Archive-level failures always take part in the aggregate, even without a per-probe row.
    archive_state = result["archive"]
    if archive_state["status"] != "read":
        result["issues"].append({"code": "archive_" + archive_state["status"], "entry_index": None,
                                 "detail": str(archive_state["reason"] or "archive not read")[:MAX_REASON_CHARS]})
    archive_ok = archive_state["status"] == "read" and archive_state["stable_after_inspection"] is True
    statuses = [p["status"] for p in result["probes"]] + [i["status"] for i in result["existing_issues"]]
    if "fail" in statuses:
        result["overall"] = "needs_changes"
    elif not archive_ok or not result["probes"] or "unknown" in statuses or result["issues"] or result["existing_issues"]:
        result["overall"] = "needs_confirmation"
    else:
        result["overall"] = "profile_pass"
    return result
