import fnmatch
import os
import re

from . import parsers
from .core import ValidationError, read_file_bound

PROFILE_VERSION = "file-review/1"
MAX_FILES = 100
MAX_TOTAL_BYTES = 200 * 1024 * 1024
NATIVE_EXTENSIONS = {".cdb", ".fdb", ".sln", ".tdb", ".db", ".database", ".fig"}
LIMIT_KINDS = {"unsupported_format", "image", "scanned_page", "embedded_object", "other"}


def validate_profile(profile):
    if type(profile) is not dict or set(profile) != {"profile_version", "requirements", "source_limits"}:
        raise ValidationError("expected file-review/1 profile keys")
    if profile["profile_version"] != PROFILE_VERSION:
        raise ValidationError("unsupported file-review profile version")
    requirements = profile["requirements"]
    if type(requirements) is not list or not 1 <= len(requirements) <= 200:
        raise ValidationError("requirements must contain 1..200 rows")
    if type(profile["source_limits"]) is not list or len(profile["source_limits"]) > 100:
        raise ValidationError("source_limits must be a bounded list")
    for limit in profile["source_limits"]:
        if type(limit) is not str or not limit.strip() or len(limit) > 2048:
            raise ValidationError("source limit must be nonempty text")
    ids = set()
    for row in requirements:
        if type(row) is not dict or set(row) != {"id", "description", "evidence", "patterns", "min_count", "max_count", "text_contains"}:
            raise ValidationError("invalid requirement keys")
        for key in ("id", "description", "evidence"):
            if type(row[key]) is not str or not row[key].strip() or len(row[key]) > 2048:
                raise ValidationError("requirement identity, description and source evidence are required")
        if row["id"] in ids:
            raise ValidationError("duplicate requirement ID")
        ids.add(row["id"])
        if type(row["min_count"]) is not int or not 1 <= row["min_count"] <= 1000:
            raise ValidationError("min_count must be 1..1000")
        if row["max_count"] is not None and (type(row["max_count"]) is not int or not row["min_count"] <= row["max_count"] <= 1000):
            raise ValidationError("max_count must be null or >= min_count")
        for key in ("patterns", "text_contains"):
            values = row[key]
            if type(values) is not list or len(values) > 100 or (key == "patterns" and not values):
                raise ValidationError("invalid patterns or text_contains list")
            for value in values:
                if type(value) is not str or not value.strip() or len(value) > 512 or "\x00" in value:
                    raise ValidationError("pattern or literal must be bounded nonempty text")
                if key == "patterns" and ("\\" in value or value.startswith("/") or re.search(r"(^|/)\.\.(/|$)", value)):
                    raise ValidationError("patterns must use relative forward-slash paths")


def _classification(result, size, name):
    limits, problems = [], []
    if size == 0:
        return "fail", [], ["파일이 0바이트입니다."]
    entries = result.get("archive_entries", [])
    if any(e["unsafe_name"] or e["encrypted"] or e["nested_archive"] or e["crc_ok"] is not True for e in entries):
        problems.append("ZIP/Office 내부 항목의 경로·암호화·CRC 검사를 완료하지 못했습니다.")
    for region in result["unread_regions"]:
        detail = region.get("detail") or region["kind"]
        if region["kind"] in LIMIT_KINDS:
            limits.append(detail)
        else:
            problems.append(detail)
    if result["status"] == "failed" and not problems:
        problems.append(result["reason"] or "parser failed")
    if any(problem.startswith("Python syntax error") for problem in problems):
        return "fail", limits, problems
    if result["format"] == "text" and result["text"] is not None and not result["text"].strip():
        return "fail", [], ["텍스트가 공백뿐입니다."]
    if result["format"] in {"docx", "pptx", "xlsx"} and not (result["text"] or "").strip():
        if not limits and not problems:
            return "fail", [], ["문서에 읽을 수 있는 본문이 없습니다."]
        if limits == ["document body has no extractable paragraph text"]:
            return "fail", [], ["문서 본문이 비어 있습니다."]
    if parsers._extension(name) in NATIVE_EXTENSIONS:
        limits.append("파일 존재·크기는 확인했습니다. 전용 프로그램에서 내용과 요구 산출물을 직접 확인하세요.")
    if problems:
        return "unknown", limits, problems
    if result["format"] == "zip" and not any(not e["is_dir"] for e in entries):
        return "fail", [], ["압축 파일에 제출할 파일이 없습니다."]
    if result["status"] != "complete" and not limits:
        return "unknown", [], [result["reason"] or "inspection incomplete"]
    return ("limited" if limits else "pass"), limits, []


def _item(name, size, sha, result):
    status, limits, problems = _classification(result, size, name)
    return {"path": name, "bytes": size, "sha256": sha, "format": result["format"],
            "status": status, "limits": limits, "problems": problems,
            "pages": result["pages"], "text": result["text"], "locations": result["locations"]}


def _archive_items(name, parsed):
    items = []
    for index, entry in enumerate(parsed["archive_entries"]):
        if entry["is_dir"]:
            continue
        label = f"entry:{index}"
        regions = [r for r in parsed["unread_regions"] if (r.get("location") or {}).get("label", "").split(" ")[0] == label]
        locations = [loc for loc in parsed["locations"] if loc.get("label", "").split(" ")[0] == label]
        content = entry.get("content") or {}
        text = "\n".join((parsed["text"] or "")[loc["char_start"]:loc["char_end"]] for loc in locations)
        sub = {"format": content.get("format", "unknown"), "status": content.get("status", "incomplete"),
               "reason": content.get("reason"), "text": text or None, "pages": content.get("pages"),
               "locations": locations, "unread_regions": regions}
        item = _item(name + "!" + (entry["entry_path"] or entry["name"]), entry["bytes_read"], entry["sha256"], sub)
        if entry["unsafe_name"] or entry["encrypted"] or entry["nested_archive"] or entry["crc_ok"] is not True or not entry["sha256"]:
            item["status"] = "unknown"
            item["problems"].append("압축 항목의 안전한 경로·CRC·내용 검사를 완료하지 못했습니다.")
        items.append(item)
    return items


def inspect_files(paths, profile):
    validate_profile(profile)
    if type(paths) is not list or not 1 <= len(paths) <= MAX_FILES:
        raise ValidationError("select 1..100 files")
    names, items, failures, total = set(), [], [], 0
    for path in paths:
        if type(path) is not str or not path:
            raise ValidationError("file paths must be nonempty strings")
        root, name = os.path.split(os.path.abspath(path))
        if name.casefold() in names:
            raise ValidationError("selected filenames collide; review separately or use an archive")
        names.add(name.casefold())
        try:
            before = read_file_bound(root, name, min(parsers.MAX_ARCHIVE_BYTES, MAX_TOTAL_BYTES - total))
            total += before["size_bytes"]
            parsed = parsers.parse_bytes(before["data"], name)
            after = read_file_bound(root, name, parsers.MAX_ARCHIVE_BYTES)
            if (before["sha256"] != after["sha256"] or before["file_identity"] != after["file_identity"] or
                    before["root_identity"] != after["root_identity"] or before["stat_after"] != after["stat_before"]):
                failures.append({"path": name, "reason": "file changed during inspection"})
                continue
        except ValidationError as exc:
            failures.append({"path": name, "reason": str(exc)[:512]})
            continue
        item = _item(name, before["size_bytes"], before["sha256"], parsed)
        items.append(item)
        if parsed["format"] == "zip":
            children = _archive_items(name, parsed)
            items.extend(children)
            if any(child["status"] == "unknown" for child in children):
                item["status"] = "unknown"
                item["problems"].append("하나 이상의 압축 항목을 검사하지 못했습니다.")
    checks = []
    for row in profile["requirements"]:
        matched = [item for item in items if any(fnmatch.fnmatchcase(item["path"].casefold(), pattern.casefold()) for pattern in row["patterns"])]
        count = len(matched)
        status, missing_literals = "pass", []
        if count < row["min_count"] or (row["max_count"] is not None and count > row["max_count"]):
            status = "unknown" if failures or any(item["status"] == "unknown" for item in items) else "fail"
        elif any(item["status"] == "fail" for item in matched):
            status = "fail"
        elif any(item["status"] == "unknown" for item in matched):
            status = "unknown"
        elif any(item["status"] == "limited" for item in matched):
            status = "limited"
        for literal in row["text_contains"]:
            if not any(literal in (item["text"] or "") for item in matched):
                missing_literals.append(literal)
        if missing_literals and status == "pass":
            status = "fail"
        checks.append({**row, "status": status, "matched_paths": [item["path"] for item in matched],
                       "observed_count": count, "missing_literals": missing_literals})
    states = [check["status"] for check in checks] + [item["status"] for item in items]
    if "fail" in states:
        verdict = "NEEDS_CHANGES"
    elif failures or "unknown" in states:
        verdict = "NEEDS_CONFIRMATION"
    elif "limited" in states or profile["source_limits"]:
        verdict = "PASS_WITH_LIMITS"
    else:
        verdict = "PASS"
    return {"report_format": "file-review-report/1", "verdict": verdict,
            "scope": "확인된 요건과 대응 파일의 점검입니다. 출처 설명·파일 대응은 호스트가 제공하며, 온라인 최신성·전체 요건 수집·정답·제출 접수를 보증하지 않습니다.",
            "summary": ("확인된 요구 파일은 모두 있는 것으로 보입니다. 아래 제한 항목의 내용은 직접 확인하세요."
                        if verdict == "PASS_WITH_LIMITS" else "요구사항별 결과와 파일별 검사 범위를 확인하세요."),
            "source_limits": list(profile["source_limits"]), "files": items, "checks": checks, "failures": failures}
