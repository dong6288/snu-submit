"""file-review/1: read-only checks of selected submission files against host-written requirements.

collect_files() reads each selected file once under the safe-read rules, parses
it (and every entry of a selected ZIP) in the bounded parser worker and re-reads
it to detect changes. inspect_files() then evaluates each requirement of a
file-review/1 profile against the files its patterns match:

- presence and count (patterns, min_count, max_count)
- optional extension (formats), basename globs (name_globs) and page limits
- required literals (text_contains) and non-empty sections (sections)

Each file gets a status from its own facts: pass, limited (present, but its
main content can only be checked by a person or a native program), fail (empty,
wrong format or a Python syntax error) or unknown (unreadable, unsafe, changed).
Requirements combine those with their own checks; the verdict is NEEDS_CHANGES
for any observed violation, NEEDS_CONFIRMATION for anything unreadable or
undecidable, PASS_WITH_LIMITS when only documented content or source limits
remain, else PASS. Files no requirement matches never create a violation.
Nothing is written, executed, uploaded or graded here.
"""

import fnmatch
import os
import re
import unicodedata

from . import parsers
from .core import ValidationError, read_file_bound

PROFILE_VERSION = "file-review/1"
REPORT_FORMAT = "file-review-report/1"
INSPECTION_FORMAT = "file-inspection/1"
MAX_FILES = 100
MAX_TOTAL_BYTES = 200 * 1024 * 1024
NATIVE_EXTENSIONS = {".cdb", ".fdb", ".sln", ".tdb", ".db", ".database", ".fig"}
LIMIT_KINDS = {"unsupported_format", "image", "scanned_page", "embedded_object", "other"}
OS_METADATA_NAMES = {".ds_store", "thumbs.db", "desktop.ini"}
RANK = {"pass": 0, "limited": 1, "unknown": 2, "fail": 3}
VERDICT_LABEL = {"PASS": "PASS", "PASS_WITH_LIMITS": "PASS_WITH_LIMITS",
                 "NEEDS_CHANGES": "수정 필요", "NEEDS_CONFIRMATION": "확인 필요"}
VERDICT_SUMMARY = {
    "PASS": "확인된 요건을 모두 충족했습니다.",
    "PASS_WITH_LIMITS": "확인된 요구 파일은 모두 있는 것으로 보입니다. '직접 확인할 것'은 도구가 내용을 읽지 못한 부분입니다.",
    "NEEDS_CHANGES": "고쳐야 할 항목이 있습니다.",
    "NEEDS_CONFIRMATION": "읽지 못했거나 판단할 수 없는 항목이 있어 직접 확인이 필요합니다.",
}
SCOPE = ("호스트가 확인한 요건과 대응 파일의 점검입니다. 요건·근거·파일 대응은 호스트가 작성하며, 이 결과는 온라인 최신성, "
         "전체 요건 수집, 답의 정답, 제출 접수를 보증하지 않습니다.")
FORMAT_NAMES = {"zip": "ZIP(압축 파일 또는 Word·PowerPoint·Excel·한글 HWPX)", "pdf": "PDF",
                "ole_compound": "구형 Office·한글(HWP) 문서", "png": "PNG 그림", "jpeg": "JPEG 그림"}
# Regions that describe text that WAS read (or a page that draws nothing); they never hide required text.
_TEXT_READ_NOTES = ("formula read", "hidden sheet", "spreadsheet error value", "equation text extracted",
                    "equation script extracted", "blank page")
_NOTE_LABELS = (  # (detail prefix or fragment, Korean label) for secondary notes
    ("blank page", "빈 쪽"),
    ("formula read", "수식 셀(저장된 계산값만 읽음, 다시 계산하지 않음)"),
    ("spreadsheet error value", "오류 값이 든 셀"),
    ("hidden sheet", "숨긴 시트(내용은 읽음)"),
    ("equation", "수식(글자만 읽음, 배치는 확인하지 않음)"),
    ("header", "머리글·바닥글·각주·메모(읽지 않음)"),
    ("footer", "머리글·바닥글·각주·메모(읽지 않음)"),
    ("footnote", "머리글·바닥글·각주·메모(읽지 않음)"),
    ("endnote", "머리글·바닥글·각주·메모(읽지 않음)"),
    ("comment", "머리글·바닥글·각주·메모(읽지 않음)"),
    ("note content", "머리글·바닥글·각주·메모(읽지 않음)"),
    ("graphic frame", "도형·차트(그리지 않음)"),
    ("chart", "차트(읽지 않음)"),
    ("XFA form", "PDF 양식(읽지 않음)"),
    ("embedded files", "PDF 첨부 파일(읽지 않음)"),
    ("annotation", "PDF 주석·양식 필드(읽지 않음)"),
    ("page resources not fully", "쪽 자원 일부(그림·개체 여부 미확인)"),
    ("slideMaster", "슬라이드 마스터·레이아웃(해석하지 않음)"),
    ("slideLayout", "슬라이드 마스터·레이아웃(해석하지 않음)"),
    ("notesMaster", "슬라이드 마스터·레이아웃(해석하지 않음)"),
)
_KIND_LABELS = {"image": "그림(내용은 읽지 않음)", "embedded_object": "포함 개체(내용은 읽지 않음)",
                "unsupported_format": "지원하지 않는 요소(읽지 않음)", "scanned_page": "글자 없이 이미지로 된 쪽",
                "other": "기타 읽지 않은 부분"}
_PROBLEM_LABELS = {"parse_error": "읽기 실패", "encrypted": "암호화되어 읽지 못함", "budget_limit": "검사 한도 초과",
                   "truncated": "글자 수 한도로 일부만 읽음"}


# ---------------------------------------------------------------------------
# Profile
# ---------------------------------------------------------------------------

def _text(value, limit, what, single_line=False):
    if (type(value) is not str or not value.strip() or len(value) > limit or "\x00" in value
            or (single_line and ("\n" in value or "\r" in value))):
        raise ValidationError(f"{what} must be nonempty text of at most {limit} characters")
    return value


def _str_list(value, what, limit=512, max_items=100, allow_empty=True):
    if type(value) is not list or len(value) > max_items or (not allow_empty and not value):
        raise ValidationError(f"{what} must be a list of at most {max_items} strings")
    return [_text(item, limit, what) for item in value]


def validate_profile(profile):
    """Validate a file-review/1 profile and return a normalized copy (optional keys filled in)."""
    allowed = {"profile_version", "requirements", "source_limits", "assignment", "sources"}
    if type(profile) is not dict or not {"profile_version", "requirements"} <= set(profile) <= allowed:
        raise ValidationError("expected file-review/1 profile keys: profile_version, requirements "
                              "and optional source_limits, assignment, sources")
    if profile["profile_version"] != PROFILE_VERSION:
        raise ValidationError("unsupported file-review profile version")
    requirements = profile["requirements"]
    if type(requirements) is not list or not 1 <= len(requirements) <= 200:
        raise ValidationError("requirements must contain 1..200 rows")
    out = {"profile_version": PROFILE_VERSION,
           "assignment": None if profile.get("assignment") is None else _text(profile["assignment"], 300, "assignment"),
           "sources": _str_list(profile.get("sources", []), "sources", 2048),
           "source_limits": _str_list(profile.get("source_limits", []), "source limit", 2048),
           "requirements": []}
    ids = set()
    required = {"id", "description", "evidence", "patterns"}
    optional = {"min_count", "max_count", "text_contains", "formats", "name_globs", "pages", "sections"}
    for row in requirements:
        if type(row) is not dict or not required <= set(row) <= required | optional:
            raise ValidationError("requirement keys: id, description, evidence, patterns and optional "
                                  + ", ".join(sorted(optional)))
        for key in ("id", "description", "evidence"):
            _text(row[key], 2048, "requirement identity, description and source evidence")
        if row["id"] in ids:
            raise ValidationError("duplicate requirement ID")
        ids.add(row["id"])
        item = {"id": row["id"], "description": row["description"], "evidence": row["evidence"],
                "patterns": _str_list(row["patterns"], "patterns", allow_empty=False),
                "min_count": row.get("min_count", 1), "max_count": row.get("max_count"),
                "text_contains": _str_list(row.get("text_contains", []), "text_contains"),
                "formats": [], "name_globs": _str_list(row.get("name_globs", []), "name_globs", 256, 20),
                "pages": None, "sections": []}
        for pattern in item["patterns"]:
            if "\\" in pattern or pattern.startswith("/") or re.search(r"(^|[/!])\.\.(/|!|$)", pattern):
                raise ValidationError("patterns must use relative forward-slash paths")
        if type(item["min_count"]) is not int or not 1 <= item["min_count"] <= 1000:
            raise ValidationError("min_count must be 1..1000")
        if item["max_count"] is not None and (type(item["max_count"]) is not int
                                              or not item["min_count"] <= item["max_count"] <= 1000):
            raise ValidationError("max_count must be null or >= min_count")
        for fmt in _str_list(row.get("formats", []), "formats", 12, 20):
            fmt = fmt.strip().lower().lstrip(".")
            if not re.fullmatch(r"[a-z0-9]{1,10}", fmt):
                raise ValidationError("formats are file extensions such as pdf or docx")
            item["formats"].append(fmt)
        if any("/" in glob or "\\" in glob or "!" in glob for glob in item["name_globs"]):
            raise ValidationError("name_globs match file names only (no folders)")
        pages = row.get("pages")
        if pages is not None:
            if type(pages) is not dict or not set(pages) <= {"min", "max"} or not pages:
                raise ValidationError("pages must be {min, max}")
            low, high = pages.get("min"), pages.get("max")
            for value in (low, high):
                if value is not None and (type(value) is not int or not 1 <= value <= 100000):
                    raise ValidationError("page limits must be positive integers or null")
            if low is None and high is None or (low is not None and high is not None and low > high):
                raise ValidationError("pages needs min and/or max with min <= max")
            item["pages"] = {"min": low, "max": high}
        sections = row.get("sections", [])
        if type(sections) is not list or len(sections) > 50:
            raise ValidationError("sections must be a list of at most 50 items")
        for section in sections:
            if type(section) is not dict or "heading" not in section or not set(section) <= {"heading", "until"}:
                raise ValidationError("each section is {heading, until}")
            until = section.get("until")
            item["sections"].append({"heading": _text(section["heading"], 200, "section heading", True),
                                     "until": None if until is None else _text(until, 200, "section until", True)})
        out["requirements"].append(item)
    return out


# ---------------------------------------------------------------------------
# Per-file facts
# ---------------------------------------------------------------------------

def _nfc(text):
    return unicodedata.normalize("NFC", text)


def _matches(path, pattern):
    return fnmatch.fnmatchcase(_nfc(path).casefold(), _nfc(pattern).casefold())


def _basename(path):
    return path.split("!")[-1].rsplit("/", 1)[-1]


def _is_os_metadata(inner_path):
    parts = inner_path.split("/")
    name = parts[-1]
    return "__MACOSX" in parts or name.startswith("._") or name.casefold() in OS_METADATA_NAMES


def _where(location):
    """Korean location text for a parser location (page, paragraph, table cell, slide, sheet cell)."""
    if not location:
        return None
    parts = []
    if location.get("page") is not None:
        parts.append(f"{location['page']}쪽")
    if location.get("paragraph") is not None:
        parts.append(f"문단 {location['paragraph']}")
    if location.get("table") is not None:
        parts.append(f"표 {location['table']}({location.get('row')}행 {location.get('column')}열)")
    label = str(location.get("label") or "")
    for token in label.split(" "):
        if token.startswith("slide:"):
            parts.append(f"슬라이드 {token[6:]}")
        elif token.startswith("sheet:"):
            parts.append(f"시트 {token[6:]}")
        elif token.startswith("cell:"):
            parts.append(f"셀 {token[5:]}")
    return ", ".join(parts) or None


def _note_label(region):
    detail = region.get("detail") or ""
    for fragment, label in _NOTE_LABELS:
        if fragment in detail:
            return label
    return _KIND_LABELS.get(region["kind"], "기타 읽지 않은 부분")


def _grouped(regions):
    """[(label, count, [locations...])] in first-seen order."""
    groups = {}
    for region in regions:
        label = _note_label(region)
        count, where = groups.get(label, (0, []))
        place = _where(region.get("location"))
        if place and len(where) < 5 and place not in where:
            where.append(place)
        groups[label] = (count + 1, where)
    return [(label, count, where) for label, (count, where) in groups.items()]


def _describe(groups):
    out = []
    for label, count, where in groups:
        text = f"{label} {count}곳" if count > 1 else label
        out.append(text + (f" ({', '.join(where)})" if where else ""))
    return out


def _mismatch_message(name, detail):
    found = re.search(r"(?:detected|entry signature) (\w+)", detail or "")
    actual = FORMAT_NAMES.get(found.group(1), found.group(1)) if found else "다른 형식"
    return f"형식 오류: 확장자는 {parsers._extension(name) or '(없음)'}인데 실제 내용은 {actual}입니다. 올바른 형식으로 다시 저장·내보내기 하세요."


def _no_text_page(region):
    return region["kind"] == "scanned_page" or (region["kind"] == "other"
                                               and (region.get("detail") or "").startswith("no extractable text on page"))


def _assess(fmt, size, name, regions, text, pages, blank_pages):
    """Status of one file from its own facts: (status, limits, notes, problems, empty_only)."""
    if size == 0:
        return "fail", [], [], ["빈 파일입니다(0바이트)."], True
    for region in regions:
        if region["kind"] == "format_mismatch":
            return "fail", [], [], [_mismatch_message(name, region.get("detail"))], False
    syntax = [r for r in regions if r["kind"] == "syntax_error"]
    if syntax:
        detail = syntax[0].get("detail") or ""
        line = re.search(r"line (\d+): (.*)", detail)
        message = f"Python 문법 오류: {line.group(1)}번째 줄 ({line.group(2)})" if line else "Python 문법 오류"
        return "fail", [], [], [message + " — 이대로는 실행되지 않습니다."], False
    problems = []
    for region in regions:
        if region["kind"] not in LIMIT_KINDS:
            label = _PROBLEM_LABELS.get(region["kind"], "확인 필요")
            place = _where(region.get("location"))
            problems.append(f"{label}: {region.get('detail') or region['kind']}" + (f" ({place})" if place else ""))
    if problems:
        return "unknown", [], [], problems[:20], False
    body = (text or "").strip()
    if fmt == "text" and text is not None and not body:
        return "fail", [], [], ["공백만 있는 텍스트 파일입니다."], True
    if fmt == "pdf" and pages == 0:
        return "fail", [], [], ["쪽이 하나도 없는 PDF입니다."], True
    if fmt == "pdf" and pages and blank_pages == pages:
        return "fail", [], [], [f"PDF의 모든 쪽({pages}쪽)이 비어 있습니다(글자·그림·그리기 없음)."], True
    empty_doc = [r for r in regions if (r.get("detail") or "").startswith("document body has no extractable")]
    if fmt in ("docx", "pptx", "xlsx", "hwpx") and not body and len(empty_doc) == len(regions):
        return "fail", [], [], ["문서에 읽을 수 있는 본문이 없습니다(빈 문서)."], True
    ext = parsers._extension(name)
    if ext in parsers.ARCHIVE_EXTENSIONS:
        return "unknown", [], [], [f"이 압축 형식({ext})은 열어 보지 못했습니다. ZIP으로 다시 압축하거나 안의 파일을 직접 확인하세요."], False
    limits = []
    whole = [r for r in regions if r["kind"] == "unsupported_format" and not _located(r)]
    if ext in NATIVE_EXTENSIONS:
        limits.append(f"전용 형식({ext}) — 존재·크기·해시만 확인했습니다. 내용과 요구 산출물은 전용 프로그램에서 직접 확인하세요.")
    elif whole and not body:
        limits.append(f"이 형식({ext or '확장자 없음'})은 내용을 읽지 못했습니다. 존재·크기·해시만 확인했습니다.")
    no_text = [r for r in regions if _no_text_page(r)]
    if no_text:
        numbers = sorted({(r.get("location") or {}).get("page") for r in no_text} - {None})
        pages_text = ", ".join(f"{n}쪽" for n in numbers[:15]) + (" 외" if len(numbers) > 15 else "") or "일부 쪽"
        limits.append(f"글자를 읽을 수 없는 쪽(스캔·손글씨·그림): {pages_text}. 그 쪽의 내용은 직접 확인하세요.")
    if fmt in ("docx", "pptx", "xlsx", "hwpx") and not body and not limits:
        limits.append("본문 글자 없이 그림·개체만 있어 내용을 읽지 못했습니다.")
    secondary = [r for r in regions if r not in no_text and r not in empty_doc and not (limits and r in whole)]
    notes = _describe(_grouped(secondary))
    return ("limited" if limits else "pass"), limits, notes, [], False


def _located(region):
    """True when the region points inside the file (page, paragraph, table) rather than at the whole file."""
    return bool({"page", "paragraph", "table"} & set(region.get("location") or {}))


def _item(path, size, sha, fmt, result, regions, text, locations, container=None):
    status, limits, notes, problems, empty = _assess(fmt, size, path, regions, text, result.get("pages"),
                                                     (result.get("metrics") or {}).get("blank_pages"))
    return {"path": path, "bytes": size, "sha256": sha, "format": fmt, "status": status, "limits": limits,
            "notes": notes, "problems": problems, "empty": empty, "pages": result.get("pages"), "text": text,
            "locations": locations, "unread_regions": regions, "container": container,
            "os_metadata": bool(container) and _is_os_metadata(path.split("!", 1)[1]), "matched_by": []}


def _archive_children(name, parsed):
    """One item per file entry of a parsed ZIP, with entry-local text, rebased locations and regions."""
    items, text = [], parsed["text"] or ""
    for entry in parsed["archive_entries"]:
        if entry["is_dir"]:
            continue
        tag = f"entry:{entry['index']}"
        regions = [r for r in parsed["unread_regions"] if str((r.get("location") or {}).get("label", "")).split(" ")[0] == tag]
        locations = [loc for loc in parsed["locations"] if str(loc.get("label", "")).split(" ")[0] == tag]
        content = entry.get("content") or {}
        sub_text, rebased = None, []
        if locations:
            start = locations[0]["char_start"]
            sub_text = text[start:locations[-1]["char_end"]]
            for loc in locations:
                moved = dict(loc, char_start=loc["char_start"] - start, char_end=loc["char_end"] - start)
                moved["label"] = str(loc.get("label", ""))[len(tag):].strip() or None
                rebased.append(moved)
        elif content.get("status") == "complete" and content.get("format") in ("text", "pdf", "docx", "pptx", "xlsx", "hwpx"):
            sub_text = ""
        path = name + "!" + (entry["entry_path"] or entry["name"])
        fmt = content.get("format") or "unknown"
        result = {"pages": content.get("pages"), "metrics": {"blank_pages": None}}
        if entry["unsafe_name"] or entry["encrypted"] or entry["crc_ok"] is not True or not entry["sha256"]:
            reasons = (["위험한 이름(경로 이탈·예약어 등): " + "; ".join(entry.get("unsafe_reasons") or [])] if entry["unsafe_name"] else []) + \
                      (["암호화된 항목"] if entry["encrypted"] else []) + \
                      (["CRC·읽기 검사를 완료하지 못함"] if entry["crc_ok"] is not True or not entry["sha256"] else [])
            items.append({"path": path, "bytes": entry["bytes_read"] or entry["declared_size"], "sha256": entry["sha256"],
                          "format": fmt, "status": "unknown", "limits": [], "notes": [],
                          "problems": ["압축 항목을 안전하게 읽지 못했습니다: " + ", ".join(reasons)], "empty": False,
                          "pages": None, "text": None, "locations": [], "unread_regions": regions, "container": name,
                          "os_metadata": False, "matched_by": []})
            continue
        if entry["nested_archive"]:
            items.append({"path": path, "bytes": entry["bytes_read"], "sha256": entry["sha256"], "format": "zip",
                          "status": "unknown", "limits": [], "notes": [],
                          "problems": ["압축 파일 안의 압축 파일은 열어 보지 않았습니다. 필요하면 바깥으로 꺼내 따로 점검하세요."],
                          "empty": False, "pages": None, "text": None, "locations": [], "unread_regions": regions,
                          "container": name, "os_metadata": _is_os_metadata(path.split("!", 1)[1]), "matched_by": []})
            continue
        if entry["bytes_read"] == 0:
            fmt = "empty"
        if entry["bytes_read"] and fmt == "unknown" and not regions:
            regions = [{"kind": "unsupported_format", "location": None, "detail": "no internal parser; content not read"}]
        result["metrics"]["blank_pages"] = content.get("blank_pages")
        items.append(_item(path, entry["bytes_read"], entry["sha256"], fmt, result, regions, sub_text, rebased, name))
    return items


def _container_item(name, size, sha, parsed):
    """Top-level ZIP: integrity only (entries are separate items)."""
    entries = parsed["archive_entries"]
    files = [e for e in entries if not e["is_dir"]]
    tags = {f"entry:{e['index']}" for e in entries}
    loose = [r for r in parsed["unread_regions"]
             if str((r.get("location") or {}).get("label", "")).split(" ")[0] not in tags]
    dir_problems = [r for r in parsed["unread_regions"] if any(
        str((r.get("location") or {}).get("label", "")).split(" ")[0] == f"entry:{e['index']}" for e in entries if e["is_dir"])]
    item = {"path": name, "bytes": size, "sha256": sha, "format": "zip", "status": "pass", "limits": [], "notes": [],
            "problems": [], "empty": False, "pages": None, "text": parsed["text"], "locations": parsed["locations"],
            "unread_regions": parsed["unread_regions"], "container": None, "os_metadata": False, "matched_by": []}
    if size == 0:
        item.update(status="fail", empty=True, problems=["빈 파일입니다(0바이트)."])
    elif parsed["status"] == "failed" or (not entries and parsed["status"] != "complete"):
        item.update(status="unknown", problems=["압축 파일을 열지 못했습니다: " + str(parsed["reason"] or "읽기 실패")])
    elif not files:
        item.update(status="fail", empty=True, problems=["압축 파일 안에 제출할 파일이 없습니다."])
    else:
        unsafe = [e for e in files if e["unsafe_name"] or e["encrypted"] or e["crc_ok"] is not True or not e["sha256"]]
        structural = [r for r in loose if (r.get("detail") or "") != "archive has no file entries"] + dir_problems
        if unsafe or structural:
            detail = [f"{e['name']}" for e in unsafe[:5]] + [str(r.get("detail")) for r in structural[:5]]
            item.update(status="unknown", problems=["압축 파일 구조를 모두 확인하지 못했습니다(위험한 이름·암호화·손상·한도): "
                                                    + "; ".join(detail)])
        metadata = [e for e in files if e["entry_path"] and _is_os_metadata(e["entry_path"])]
        if metadata:
            item["notes"].append(f"운영체제 메타데이터 파일 {len(metadata)}개(__MACOSX, .DS_Store 등)는 요건 대응에서 제외했습니다.")
    return item


def collect_files(paths):
    """Read and parse the selected files. Returns (items, failures); never raises for a single bad file."""
    if type(paths) is not list or not 1 <= len(paths) <= MAX_FILES:
        raise ValidationError("select 1..100 files")
    names, items, failures, total = set(), [], [], 0
    for path in paths:
        if type(path) is not str or not path:
            raise ValidationError("file paths must be nonempty strings")
        root, name = os.path.split(os.path.abspath(path))
        if _nfc(name).casefold() in names:
            raise ValidationError("selected filenames collide; review separately or use an archive")
        names.add(_nfc(name).casefold())
        try:
            before = read_file_bound(root, name, min(parsers.MAX_ARCHIVE_BYTES, MAX_TOTAL_BYTES - total))
            total += before["size_bytes"]
            parsed = parsers.parse_bytes(before["data"], name)
            after = read_file_bound(root, name, parsers.MAX_ARCHIVE_BYTES)
            if (before["sha256"] != after["sha256"] or before["file_identity"] != after["file_identity"] or
                    before["root_identity"] != after["root_identity"] or before["stat_after"] != after["stat_before"]):
                failures.append({"path": name, "reason": "검사 중에 파일이 바뀌었습니다. 저장을 마친 뒤 다시 점검하세요."})
                continue
        except ValidationError as exc:
            failures.append({"path": name, "reason": "파일을 읽지 못했습니다: " + str(exc)[:500]})
            continue
        mismatch = any(r["kind"] == "format_mismatch" for r in parsed["unread_regions"])
        if parsed["format"] == "zip" and not mismatch and parsers._extension(name) not in parsers.OOXML_LIKE_EXTENSIONS:
            items.append(_container_item(name, before["size_bytes"], before["sha256"], parsed))
            items.extend(_archive_children(name, parsed))
        else:
            items.append(_item(name, before["size_bytes"], before["sha256"], parsed["format"], parsed,
                               parsed["unread_regions"], parsed["text"], parsed["locations"]))
    return items, failures


# ---------------------------------------------------------------------------
# Requirement checks
# ---------------------------------------------------------------------------

_SPACE = re.compile(r"\s+")


def _squash(text):
    return _SPACE.sub(" ", _nfc(text)).strip().casefold()


def _contains(text, literal):
    hay, needle = _squash(text or ""), _squash(literal)
    return needle in hay or needle.replace(" ", "") in hay.replace(" ", "")


def _after_heading(line, heading):
    """Remainder of `line` after `heading` when the line starts with it at a word boundary, else None."""
    norm, head = _squash(line), _squash(heading)
    for text, prefix in ((norm, head), (norm.replace(" ", ""), head.replace(" ", ""))):
        if prefix and text.startswith(prefix):
            rest = text[len(prefix):]
            if not (prefix[-1].isalnum() and rest[:1].isalnum()):
                return rest
    return None


def _hides_text(region):
    return not any(fragment in (region.get("detail") or "") for fragment in _TEXT_READ_NOTES)


def _location_at(item, pos):
    for loc in item["locations"]:
        if loc.get("char_start", -1) <= pos <= loc.get("char_end", -1):
            return loc
    return None


def _unread_between(item, start, end):
    """True if an unread region that could hold text lies in the char range [start, end) of the item text."""
    regions = [r for r in item["unread_regions"] if _hides_text(r)]
    if not regions:
        return False
    first, last = _location_at(item, start), (_location_at(item, end) if end < len(item["text"] or "") else None)
    for region in regions:
        where = region.get("location") or {}
        for key, inclusive in (("paragraph", False), ("page", True)):
            if key in where and first and key in first:
                high = last[key] if last and key in last else float("inf")
                if first[key] <= where[key] < high or (inclusive and where[key] == high):
                    return True
                break
        else:
            return True  # no comparable location: cannot rule it out
    return False


def _is_toc(rest, inline):
    """A table-of-contents entry: leader dots before a page number, or (for ordinary headings) a bare page number."""
    tail = rest.strip()
    if re.fullmatch(r"[.·…‥_\-–—\s]*\d{0,4}", tail) and re.search(r"[.·…‥_]{2,}", tail):
        return True
    return not inline and re.fullmatch(r"\d{1,4}", tail) is not None


def _section_hits(item, heading, until):
    """Every occurrence of `heading` with the text up to `until` (or the next same heading, or the end).

    Body text starts on the line after the heading. Only an answer marker ending in ':' (e.g. "답:")
    also counts the rest of its own line, so question text or points printed beside a heading are not
    mistaken for an answer. Table-of-contents leaders ("..... 3") never count.
    """
    text = item["text"] or ""
    lines, pos = [], 0
    for line in text.split("\n"):
        lines.append((pos, line))
        pos += len(line) + 1
    inline = _squash(heading).endswith((":", "："))
    hits = []
    for index, (start, line) in enumerate(lines):
        rest = _after_heading(line, heading)
        if rest is None or _is_toc(rest, inline):
            continue  # not this heading, or a table-of-contents entry ("2. 실행 결과 ..... 3")
        body, end = [rest] if inline else [], len(text)
        for next_start, next_line in lines[index + 1:]:
            if _after_heading(next_line, heading) is not None or (until and _after_heading(next_line, until) is not None):
                end = next_start
                break
            body.append(next_line)
        joined = " ".join(part.strip() for part in body if part.strip())
        hits.append({"start": start, "end": end, "chars": sum(1 for ch in joined if not ch.isspace()),
                     "excerpt": joined[:160], "unread": _unread_between(item, start, end),
                     "where": _where(_location_at(item, start))})
    return hits


def _finding(status, message, path=None, where=None, paths=None):
    return {"status": status, "path": path, "where": where, "message": message, "paths": paths or ([path] if path else [])}


def _grouped_findings(status, pairs):
    """One finding per distinct message; files sharing a message are listed together."""
    groups = {}
    for path, message in pairs:
        groups.setdefault(message, []).append(path)
    return [_finding(status, message, paths[0]) if len(paths) == 1 else
            _finding(status, f"{message} (해당 파일 {len(paths)}개)", None, None, paths) for message, paths in groups.items()]


def _could_hide(pattern, items, failures):
    """True when an unreadable file or archive might contain a match for the pattern."""
    if any(_matches(f["path"], pattern) for f in failures):
        return True
    if "!" not in pattern:
        return False
    outer = pattern.rsplit("!", 1)[0]
    for item in items:
        if item["status"] == "unknown" and (_matches(item["path"], outer)
                                            or (item["container"] and _matches(item["container"], outer))):
            return True
    return any(_matches(f["path"], outer) for f in failures)


def _evaluate(row, items, failures):
    candidates = [i for i in items if not i["os_metadata"]]
    matched = [i for i in candidates if any(_matches(i["path"], p) for p in row["patterns"])]
    for item in matched:
        item["matched_by"].append(row["id"])
    findings, status = [], "pass"

    def worse(new):
        nonlocal status
        if RANK[new] > RANK[status]:
            status = new

    good = [i for i in matched if i["status"] in ("pass", "limited")]
    empties = [i for i in matched if i["status"] == "fail" and i["empty"]]
    defects = [i for i in matched if i["status"] == "fail" and not i["empty"]]
    unknowns = [i for i in matched if i["status"] == "unknown"]
    if row["max_count"] is not None and len(matched) > row["max_count"]:
        worse("fail")
        findings.append(_finding("fail", f"대응 파일이 {len(matched)}개로 허용 개수({row['max_count']}개)를 넘습니다: "
                                 + ", ".join(i["path"] for i in matched[:10]) + ". 초안·중복 파일인지 확인하고 하나만 남기세요."))
    for item in defects:
        worse("fail")
        findings.extend(_finding("fail", problem, item["path"]) for problem in item["problems"])
    if len(good) >= row["min_count"]:
        for item in empties:
            findings.append(_finding("warning", "함께 대응된 빈 파일입니다(필수 개수는 다른 파일로 충족): " + item["problems"][0], item["path"]))
    else:
        for item in empties:
            worse("fail")
            findings.extend(_finding("fail", problem, item["path"]) for problem in item["problems"])
        if not empties and not defects:
            if unknowns or any(_could_hide(p, items, failures) for p in row["patterns"]):
                worse("unknown")
                if not unknowns:
                    findings.append(_finding("unknown", "대응 파일을 찾지 못했지만, 읽지 못한 파일·압축 항목 안에 있을 수 있어 없다고 확정하지 않았습니다."))
            else:
                worse("fail")
                exact = [_basename(p) for p in row["patterns"] if not re.search(r"[*?\[]", _basename(p))]
                hint = [i["path"] for i in candidates if any(_matches(_basename(i["path"]), name) for name in exact)]
                findings.append(_finding("fail", f"필수 파일이 없습니다(찾은 개수 {len(good)}개, 필요 {row['min_count']}개; 찾는 이름: "
                                         + ", ".join(row["patterns"]) + ")."
                                         + (f" 참고: 같은 이름의 파일이 다른 위치에 있습니다 — {', '.join(hint[:3])}." if hint else "")))
    for item in unknowns:
        worse("unknown")
        findings.extend(_finding("unknown", problem, item["path"]) for problem in item["problems"])
    limited = [(item["path"], limit) for item in good if item["status"] == "limited" for limit in item["limits"]]
    if limited:
        worse("limited")
        findings.extend(_grouped_findings("limited", limited))
    for item in matched:
        if row["formats"] and parsers._extension(_basename(item["path"])).lstrip(".") not in row["formats"]:
            worse("fail")
            findings.append(_finding("fail", f"형식 오류: 요구 형식은 {', '.join(row['formats'])}인데 이 파일은 "
                                     f"{parsers._extension(_basename(item['path'])) or '확장자 없음'}입니다.", item["path"]))
        if row["name_globs"] and not any(_matches(_basename(item["path"]), glob) for glob in row["name_globs"]):
            worse("fail")
            findings.append(_finding("fail", "파일명 규칙 위반: 요구 형식 " + " 또는 ".join(row["name_globs"])
                                     + f", 현재 이름 {_basename(item['path'])}", item["path"]))
    if row["pages"]:
        low, high = row["pages"]["min"], row["pages"]["max"]
        for item in good:
            pages = item["pages"] if item["format"] in ("pdf", "pptx") else None
            if pages is None:
                worse("unknown")
                findings.append(_finding("unknown", f"쪽수를 확인할 수 없는 형식({item['format']})입니다. PDF로 내보낸 쪽수를 직접 확인하세요.", item["path"]))
            elif (low is not None and pages < low) or (high is not None and pages > high):
                worse("fail")
                findings.append(_finding("fail", f"쪽수 위반: {pages}쪽(허용 {low or 1}–{high or '제한 없음'}쪽).", item["path"]))
    missing = []
    if good:
        for literal in row["text_contains"]:
            if not any(_contains(item["text"], literal) for item in good):
                missing.append(literal)
                hidden = [i["path"] for i in good if any(_hides_text(r) for r in i["unread_regions"])]
                if hidden:
                    worse("unknown")
                    findings.append(_finding("unknown", f"필수 문구 '{literal}'를 읽은 본문에서 찾지 못했습니다. "
                                             "그림·스캔 등 읽지 못한 부분 안에 있다면 직접 확인하고, 아니라면 추가하세요.", hidden[0]))
                else:
                    worse("fail")
                    findings.append(_finding("fail", f"필수 문구 '{literal}'가 없습니다.", good[0]["path"]))
    results = []
    for section in row["sections"] if good else []:
        hits = [(item, hit) for item in good for hit in _section_hits(item, section["heading"], section["until"])]
        label = section["heading"]
        best = next(((i, h) for i, h in hits if h["chars"]), None)
        if best:
            item, hit = best
            results.append({"heading": label, "until": section["until"], "status": "pass", "path": item["path"],
                            "where": hit["where"], "body_chars": hit["chars"], "excerpt": hit["excerpt"]})
            continue
        if hits:
            item, hit = hits[-1]
            state = "unknown" if hit["unread"] else "fail"
            message = (f"'{label}' 아래에 글자는 없고 그림·수식 등 읽지 못한 부분만 있습니다. 내용이 들어 있는지 직접 확인하세요."
                       if hit["unread"] else f"'{label}' 아래 내용이 비어 있습니다. 답·본문을 작성하세요.")
            where = hit["where"]
        else:
            hidden = [i for i in good if any(_hides_text(r) for r in i["unread_regions"])]
            item, where = (hidden or good)[0], None
            state = "unknown" if hidden else "fail"
            message = (f"제목 '{label}'를 읽은 본문에서 찾지 못했습니다(읽지 못한 부분 안에 있을 수 있음)."
                       if hidden else f"제목 '{label}'가 없습니다. 해당 항목을 작성했는지 확인하세요.")
        worse(state)
        findings.append(_finding(state, message, item["path"], where))
        results.append({"heading": label, "until": section["until"], "status": state, "path": item["path"],
                        "where": where, "body_chars": 0, "excerpt": None})
    return {**row, "status": status, "matched_paths": [i["path"] for i in matched], "observed_count": len(matched),
            "missing_literals": missing, "sections": results, "findings": findings}


def _public_item(item, include_text):
    out = {key: item[key] for key in ("path", "bytes", "sha256", "format", "status", "limits", "notes", "problems",
                                      "pages", "container", "os_metadata", "matched_by")}
    out["unread_regions"] = item["unread_regions"][:200]
    out["locations"] = item["locations"][:2000] if include_text else []
    if include_text:
        out["text"] = item["text"]
    return out


def inspect_files(paths, profile, include_text=True):
    """Evaluate a file-review/1 profile against the selected files and return the report dict."""
    profile = validate_profile(profile)
    items, failures = collect_files(paths)
    checks = [_evaluate(row, items, failures) for row in profile["requirements"]]
    blocking = [_finding("unknown", f["reason"], f["path"]) for f in failures]
    for item in items:
        if item["container"] is None and not item["matched_by"] and item["status"] in ("fail", "unknown"):
            matched_inside = any(i["matched_by"] for i in items if i["container"] == item["path"])
            if item["status"] == "unknown" or not matched_inside:
                blocking.append(_finding("unknown", "요건과 대응하지 않은 선택 파일에 문제가 있습니다. 제출 대상인지 확인하세요: "
                                         + "; ".join(item["problems"]), item["path"]))
    states = [check["status"] for check in checks]
    if "fail" in states:
        verdict = "NEEDS_CHANGES"
    elif "unknown" in states or blocking:
        verdict = "NEEDS_CONFIRMATION"
    elif "limited" in states or profile["source_limits"]:
        verdict = "PASS_WITH_LIMITS"
    else:
        verdict = "PASS"
    groups = {}
    for item in items:
        if item["matched_by"] and item["status"] == "limited":
            for limit in item["limits"]:
                groups.setdefault(limit, []).append(item["path"].split("!", 1)[-1])
    manual = [f"{limit} — {paths[0]}" if len(paths) == 1 else
              f"{limit} — {len(paths)}개: {', '.join(paths[:4])}" + (f" 외 {len(paths) - 4}개" if len(paths) > 4 else "")
              for limit, paths in groups.items()]
    extra = [{"path": i["path"], "status": i["status"], "message": "; ".join(i["problems"] + i["limits"]) or None}
             for i in items if not i["matched_by"] and not i["os_metadata"] and i["container"] is not None]
    extra += [{"path": i["path"], "status": i["status"], "message": "; ".join(i["problems"] + i["limits"]) or None}
              for i in items if not i["matched_by"] and i["container"] is None and i["format"] != "zip"]
    return {"report_format": REPORT_FORMAT, "verdict": verdict, "verdict_label": VERDICT_LABEL[verdict],
            "summary": VERDICT_SUMMARY[verdict], "scope": SCOPE, "assignment": profile["assignment"],
            "sources": profile["sources"], "source_limits": profile["source_limits"], "checks": checks,
            "blocking": blocking, "manual_checks": manual, "extra_files": extra,
            "files": [_public_item(i, include_text) for i in items], "failures": failures}


def inspect_only(paths):
    """Parse the selected files without requirements (assignment identification, profile drafting)."""
    items, failures = collect_files(paths)
    return {"report_format": INSPECTION_FORMAT, "files": [_public_item(i, True) for i in items], "failures": failures}


def compare_reports(previous, current):
    """Delta between two file-review reports by path, SHA-256 and requirement status (nothing reused)."""
    if type(previous) is not dict or previous.get("report_format") != REPORT_FORMAT:
        raise ValidationError("previous report is not a file-review-report/1 document")
    old = {f.get("path"): f.get("sha256") for f in previous.get("files", []) if type(f) is dict}
    new = {f["path"]: f["sha256"] for f in current["files"]}
    old_checks = {c.get("id"): c.get("status") for c in previous.get("checks", []) if type(c) is dict}
    return {"previous_verdict": previous.get("verdict"), "current_verdict": current["verdict"],
            "files_added": sorted(set(new) - set(old)), "files_removed": sorted(set(old) - set(new)),
            "files_changed": sorted(p for p in set(old) & set(new) if old[p] != new[p]),
            "requirement_changes": [{"id": c["id"], "previous": old_checks.get(c["id"]), "current": c["status"]}
                                    for c in current["checks"] if old_checks.get(c["id"]) != c["status"]],
            "note": "모든 결과는 현재 파일 바이트로 다시 계산했으며 이전 결과를 재사용하지 않았습니다."}
