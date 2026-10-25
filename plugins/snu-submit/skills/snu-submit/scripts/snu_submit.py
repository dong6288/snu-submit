#!/usr/bin/env python3
"""SNU Submit read-only file review and archive diagnostics (0.1.0-alpha.2).

review inspects selected files with file-review/1 requirements and reports
PASS_WITH_LIMITS when only documented content or source limitations remain.
check/recheck/demo/sample retain archive-profile/1 diagnostics. Neither mode
logs in to eTL, executes student code or grades answers. Outputs go to a NEW
directory. Exit 0 means the command completed; read the verdict. Exit 1 means
refused or failed; exit 2 means a usage error.
"""

import argparse
import hashlib
import html
import importlib
import importlib.metadata
import json
import os
import platform
import re
import secrets
import stat
import sys
import zipfile
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from submit_checks import archive_probes, parsers, file_review
from submit_checks.core import MAX_JSON_BYTES, ValidationError, load_json_strict, read_file_bound  # noqa: E402

TOOL_VERSION = "0.1.0-alpha.2"
REPORT_FORMAT = "archive-cli-report/1"
SUMMARY_FORMAT = "archive-demo-summary/1"
DEMO_VERSION = "archive-demo/1"
SCOPE_NOTICE = ("archive-profile 모드: 압축 파일 구조와 프로필에 적힌 텍스트 표지만 점검합니다. 온라인 eTL 요건 수집, "
                "파일 형식 내부의 정합성, 내용의 정답은 확인하지 않으며 profile_pass는 제출 준비 완료가 아닙니다.")
MAX_INPUT_JSON = 1024 * 1024
MAX_REPORT_JSON = MAX_JSON_BYTES  # the strict core loader's own upper bound (8 MiB)
CASE_ID_RE = re.compile(r"[a-z0-9][a-z0-9-]{0,47}")
SHA_RE = re.compile(r"[0-9a-f]{64}")
PROFILE_KEYS = {"required_entry": "required_entries", "nonempty_entry": "nonempty_entries", "text_section": "text_sections"}


class CliError(Exception):
    """Refusal or failure with a bounded, payload-free message."""


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _sha(data):
    return hashlib.sha256(data).hexdigest()


def _label(text):
    if type(text) is not str or not text.strip() or len(text) > 200:
        raise CliError("label must be a non-blank string of at most 200 characters")
    return text


def _has_link(path):
    """True if the path or any ancestor is a symlink or Windows reparse point (junction)."""
    current = os.path.abspath(path)
    while True:
        info = os.lstat(current)
        if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
            return True
        parent = os.path.dirname(current)
        if parent == current:
            return False
        current = parent


def _new_dir(path, inputs=()):
    """Create a NEW output directory; refuse existing paths, links and overlap with inputs."""
    target = os.path.abspath(path)
    parent = os.path.dirname(target)
    if os.path.lexists(target):
        raise CliError("output already exists; choose a new directory")
    if not os.path.isdir(parent):
        raise CliError("output parent directory does not exist")
    if _has_link(parent):
        raise CliError("output parent path contains a link or junction")
    key = os.path.normcase(target)
    for item in inputs:
        source = os.path.normcase(os.path.abspath(item))
        if source == key or source.startswith(key + os.sep):
            raise CliError("output must not contain or replace an input")
    os.mkdir(target)
    return target


def _write(path, text):
    with open(path, "x", encoding="utf-8", newline="\n") as handle:
        handle.write(text)


def _write_json(path, value):
    _write(path, json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n")


def _load(path, max_bytes=MAX_INPUT_JSON):
    try:
        return load_json_strict(path, max_bytes=max_bytes)
    except ValidationError as exc:
        raise CliError(f"JSON input rejected: {exc}") from None


def _check_id(probe, profile):
    item = profile[PROFILE_KEYS[probe["probe"]]][probe["index"]]
    ident = {k: v for k, v in item.items() if k not in ("reason", "evidence")}
    return probe["probe"] + ":" + _sha(json.dumps(ident, sort_keys=True, ensure_ascii=False).encode("utf-8"))[:16]


def run_check(archive, profile, label, include_excerpts=False):
    """Fresh inspection (never reuses an earlier result) wrapped in the CLI report format."""
    try:
        result = archive_probes.inspect_archive(archive, profile)
    except ValidationError as exc:
        raise CliError(f"profile rejected: {exc}") from None
    for probe in result["probes"]:
        probe["check_id"] = _check_id(probe, profile)
        if not include_excerpts and isinstance(probe.get("observed"), dict) and "excerpt" in probe["observed"]:
            probe["observed"]["excerpt"] = None  # answer text is not copied into reports by default
    return {"tool": "snu-submit", "tool_version": TOOL_VERSION, "report_format": REPORT_FORMAT,
            "mode": "archive-profile", "scope_notice": SCOPE_NOTICE, "label": label,
            "run_id": f"run-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}-{secrets.token_hex(4)}",
            "generated_at": _now(), "profile_sha256": _sha(json.dumps(profile, sort_keys=True, ensure_ascii=False).encode()),
            "result": result, "delta": None}


def compare(previous, current):
    """Delta between two CLI reports from their recorded check IDs, statuses and hashes."""
    if (type(previous) is not dict or previous.get("report_format") != REPORT_FORMAT
            or type(previous.get("result")) is not dict or type(previous["result"].get("probes")) is not list):
        raise CliError("previous report is not an archive-cli-report/1 document")
    old = {p.get("check_id"): p for p in previous["result"]["probes"] if type(p) is dict}
    if None in old or len(old) != len(previous["result"]["probes"]):
        raise CliError("previous report has missing or duplicate check IDs")
    new = {p["check_id"]: p for p in current["result"]["probes"]}
    old_entries = {e.get("path"): e.get("sha256") for e in previous["result"].get("entries", []) if type(e) is dict and e.get("path")}
    new_entries = {e["path"]: e["sha256"] for e in current["result"]["entries"] if e["path"]}
    real = lambda value: type(value) is str and SHA_RE.fullmatch(value) is not None  # noqa: E731
    old_sha, new_sha = (previous["result"].get("archive") or {}).get("sha256"), current["result"]["archive"]["sha256"]
    return {"previous_run_id": previous.get("run_id"),
            "archive_changed": bool(real(old_sha) and real(new_sha) and old_sha != new_sha),
            "entries_added": sorted(set(new_entries) - set(old_entries)),
            "entries_removed": sorted(set(old_entries) - set(new_entries)),
            "entries_changed": sorted(p for p in set(old_entries) & set(new_entries)
                                      if real(old_entries[p]) and real(new_entries[p]) and old_entries[p] != new_entries[p]),
            "checks_added": sorted(set(new) - set(old)), "checks_removed": sorted(set(old) - set(new)),
            "status_changes": [{"check_id": cid, "previous_status": old[cid].get("status"),
                                "current_status": new[cid]["status"]}
                               for cid in sorted(set(old) & set(new)) if old[cid].get("status") != new[cid]["status"]],
            "note": "all statuses were recomputed from current bytes; previous results are not reused"}


# --- self-contained HTML (escaped text, no scripts, styles, images or network) ---

PAGE = ('<!DOCTYPE html>\n<html lang="ko"><head><meta charset="utf-8">'
        '<meta http-equiv="Content-Security-Policy" content="default-src \'none\'">'
        '<meta name="viewport" content="width=device-width, initial-scale=1"><title>{title}</title></head>'
        '<body><main>{body}</main></body></html>\n')


def _e(value):
    return "—" if value is None else html.escape(str(value), quote=True)


def _table(caption, headers, rows):
    head = "".join(f'<th scope="col">{_e(h)}</th>' for h in headers)
    body = "".join("<tr>" + "".join(f"<td>{cell}</td>" for cell in row) + "</tr>" for row in rows)
    body = body or f'<tr><td colspan="{len(headers)}">없음</td></tr>'
    return f'<table border="1"><caption>{_e(caption)}</caption><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table>'


def _where(probe):
    obs, exp = probe.get("observed") or {}, probe.get("expected") or {}
    if "start_line" in obs:
        return _e(f'줄 {obs["start_line"]}–{obs["end_line"]}')
    if "start_literal" in exp:
        return _e(f'기대 표지: {exp["start_literal"]} … {exp["end_literal"]}')
    return _e(exp.get("path"))


def report_html(report):
    r = report["result"]
    a = r["archive"]
    parts = [f'<h1>SNU Submit archive-profile 점검: {_e(report["label"])}</h1>',
             f'<p><strong>{_e(report["scope_notice"])}</strong></p>',
             f'<p>판정: <strong>{_e(r["overall"])}</strong> · 실행 {_e(report["run_id"])} · {_e(report["generated_at"])}</p>',
             _table("압축 파일", ["이름", "상태", "SHA-256", "바이트", "검사 후 변경 없음"],
                    [[_e(a["file_name"]), _e(a["status"]), _e(a["sha256"]), _e(a["size_bytes"]),
                      _e(a["stable_after_inspection"])]]),
             _table("프로필 점검", ["검사 ID", "종류", "경로", "결과", "코드", "설명", "위치"],
                    [[_e(p["check_id"]), _e(p["probe"]), _e(p["path"]), _e(p["status"]), _e(p["reason_code"]),
                      _e(p["detail"]), _where(p)] for p in r["probes"]]),
             _table("압축 파일 문제", ["코드", "항목", "설명"],
                    [[_e(i["code"]), _e(i["entry_index"]), _e(i["detail"])] for i in r["issues"]]),
             _table("기존 미해결 사항(프로필 제공, 그대로 유지)", ["상태", "이유", "근거"],
                    [[_e(i["status"]), _e(i["reason"]), _e(i["evidence"])] for i in r["existing_issues"]])]
    if report["delta"]:
        d = report["delta"]
        parts.append(_table("이전 보고서와 비교", ["항목", "값"],
                            [[_e(k), _e(json.dumps(d[k], ensure_ascii=False))] for k in d]))
    parts.append("<p>JSON 보고서가 기준입니다. 근거(evidence)는 호스트가 적은 출처 설명이며 교수자 지시로 검증된 것이 아닙니다.</p>")
    return PAGE.format(title=_e("archive-profile 점검"), body="".join(parts))


def index_html(summary):
    rows = []
    for case in summary["cases"]:
        mutations = "; ".join(f'{m["op"]} {m["path"]}' + (f' 줄 {m["removed_lines"][0]}–{m["removed_lines"][1]}'
                                                          if m.get("removed_lines") else "") for m in case["mutations"])
        rows.append([f'<a href="cases/{_e(case["id"])}/report.html">{_e(case["id"])}</a>', _e(case["label"]),
                     _e(case["overall"]), _e(json.dumps(case["counts"])), _e(", ".join(case["missing_entries"]) or "없음"),
                     _e("; ".join(f'{s["path"]} {s["reason_code"]}' for s in case["section_findings"]) or "없음"),
                     _e(case["existing_issues"]), _e(mutations or "변경 없음(원본과 같은 바이트)")])
    s = summary["source"]
    body = (f'<h1>복사본 결함 주입 시연: {_e(summary["label"])}</h1><p><strong>{_e(summary["notice"])}</strong></p>'
            f'<p>{_e(SCOPE_NOTICE)}</p><p>원본 SHA-256 {_e(s["sha256_before"])} → {_e(s["sha256_after"])} '
            f'(변경 없음: {_e(s["unchanged"])})</p>'
            + _table("사례", ["사례", "설명", "판정", "개수", "없는 항목", "구간 결과", "기존 미해결", "주입한 변경"], rows))
    return PAGE.format(title=_e("결함 주입 시연"), body=body)


# --- copy-only fault injection (archive-demo/1) ---

MUTATION_KEYS = {"remove_entry": {"op", "path"}, "empty_entry": {"op", "path"},
                 "remove_text_section": {"op", "path", "start_literal", "end_literal"}}


def validate_scenarios(doc):
    if type(doc) is not dict or set(doc) != {"version", "expected_source_sha256", "label", "cases"}:
        raise CliError("scenarios must have exactly version, expected_source_sha256, label, cases")
    if doc["version"] != DEMO_VERSION or type(doc["expected_source_sha256"]) is not str \
            or not SHA_RE.fullmatch(doc["expected_source_sha256"]):
        raise CliError("unsupported scenario version or invalid expected_source_sha256")
    _label(doc["label"])
    if type(doc["cases"]) is not list or not 1 <= len(doc["cases"]) <= 20:
        raise CliError("cases must be a list of 1 to 20 items")
    seen = set()
    for case in doc["cases"]:
        if type(case) is not dict or set(case) != {"id", "label", "mutations"}:
            raise CliError("each case must have exactly id, label, mutations")
        if type(case["id"]) is not str or not CASE_ID_RE.fullmatch(case["id"]) or case["id"] in seen:
            raise CliError("case id must be a unique lowercase slug [a-z0-9-], at most 48 characters")
        seen.add(case["id"])
        _label(case["label"])
        if type(case["mutations"]) is not list or len(case["mutations"]) > 50:
            raise CliError("mutations must be a list of at most 50 items")
        for m in case["mutations"]:
            if type(m) is not dict or m.get("op") not in MUTATION_KEYS or set(m) != MUTATION_KEYS[m["op"]]:
                raise CliError("unsupported mutation or wrong keys")
            if type(m["path"]) is not str or not archive_probes._SAFE_PATH_RE.fullmatch(m["path"]):
                raise CliError("mutation path must be a safe relative archive path")
            if m["op"] == "remove_text_section":
                for literal in (m["start_literal"], m["end_literal"]):
                    if (type(literal) is not str or not literal.strip() or literal != literal.strip()
                            or len(literal) > archive_probes.MAX_LITERAL_CHARS
                            or any(ord(ch) < 32 or ord(ch) == 127 for ch in literal)):
                        raise CliError("section literals must be trimmed single-line strings")
                if m["start_literal"] == m["end_literal"]:
                    raise CliError("section start and end literals must differ")


def read_source(path):
    root, name = os.path.split(os.path.abspath(path))
    try:
        return read_file_bound(root, name, archive_probes.MAX_ARCHIVE_BYTES)
    except ValidationError as exc:
        raise CliError(f"source archive unreadable: {exc}") from None


def load_entries(data):
    """All entries of a safe, unencrypted, CRC-valid ZIP, read in memory under the parser budgets."""
    limits = dict(parsers.DEFAULT_LIMITS)
    zf, infos, problems, error = parsers._zip_open(data, len(data), limits, "zip")
    if error is not None:
        raise CliError("source archive refused: " + str(error["reason"]))
    state = {"budget": limits["max_uncompressed_bytes"], "used": 0, "deadline": float("inf")}
    entries = []
    for index, info in enumerate(infos):
        record, safe, raw = parsers._zip_new_record(index, info, problems)
        if not safe or record["encrypted"]:
            raise CliError("source archive has unsafe, duplicate or encrypted entries")
        if archive_probes._metadata_problem(info) is not None:
            raise CliError("source archive has an entry whose file/directory attributes contradict its name")
        if record["is_dir"]:
            if parsers._zip_check_directory(zf, info, record, state) is not None:
                raise CliError("source archive has an invalid directory entry")
            entries.append({"name": raw, "info": info, "data": b"", "is_dir": True})
            continue
        read = parsers._zip_read_entry(zf, info, state, limits["max_uncompressed_bytes"])
        if read["problem"] is not None or read["collected"] is None:
            raise CliError("source archive entry unreadable, corrupt or over budget")
        entries.append({"name": raw, "info": info, "data": read["collected"], "is_dir": False})
    return entries


def remove_section(data, start_literal, end_literal):
    """Remove the marker lines and body, using the probes' exact trimmed-line matching."""
    bom = b"\xef\xbb\xbf" if data.startswith(b"\xef\xbb\xbf") else b""
    try:
        text = data[len(bom):].decode("utf-8")
    except UnicodeDecodeError:
        raise CliError("section target is not strict UTF-8 text") from None
    pieces = re.split(r"(\r\n|\r|\n)", text)
    lines, ends = pieces[0::2], pieces[1::2] + [""]
    starts = [i for i, line in enumerate(lines) if line.strip() == start_literal]
    stops = [i for i, line in enumerate(lines) if line.strip() == end_literal]
    if len(starts) != 1 or len(stops) != 1 or stops[0] <= starts[0]:
        raise CliError("section markers must each appear exactly once with the end after the start")
    s, e = starts[0], stops[0]
    removed = "".join(lines[i] + ends[i] for i in range(s, e + 1))
    kept = "".join(lines[i] + ends[i] for i in range(len(lines)) if not s <= i <= e)
    return bom + kept.encode("utf-8"), [s + 1, e + 1], _sha(removed.encode("utf-8"))


def apply_mutation(entries, m):
    targets = [e for e in entries if not e["is_dir"] and e["name"] == m["path"]]
    if len(targets) != 1:
        raise CliError(f"{m['op']} target is not exactly one file entry in the source")
    entry = targets[0]
    ledger = {"op": m["op"], "path": m["path"], "original_entry_sha256": _sha(entry["data"]),
              "original_bytes": len(entry["data"]), "current_entry_sha256": None, "current_bytes": None,
              "removed_lines": None, "removed_segment_sha256": None}
    if m["op"] == "remove_entry":
        entries.remove(entry)
        return ledger
    if m["op"] == "empty_entry":
        entry["data"] = b""
    else:
        entry["data"], ledger["removed_lines"], ledger["removed_segment_sha256"] = remove_section(
            entry["data"], m["start_literal"], m["end_literal"])
    ledger.update(current_entry_sha256=_sha(entry["data"]), current_bytes=len(entry["data"]))
    return ledger


def write_zip(path, entries):
    with open(path, "xb") as handle, zipfile.ZipFile(handle, "w") as zf:
        for e in entries:
            info = zipfile.ZipInfo(e["name"], date_time=e["info"].date_time)
            info.external_attr = e["info"].external_attr
            info.compress_type = zipfile.ZIP_STORED if e["is_dir"] else zipfile.ZIP_DEFLATED
            zf.writestr(info, e["data"])  # zipfile computes a fresh CRC-32


def case_summary(case, report, ledger, archive_rel):
    r = report["result"]
    counts = {s: sum(1 for p in r["probes"] if p["status"] == s) for s in ("pass", "fail", "unknown")}
    return {"id": case["id"], "label": case["label"], "archive": archive_rel, "archive_sha256": r["archive"]["sha256"],
            "report_json": f"cases/{case['id']}/report.json", "report_html": f"cases/{case['id']}/report.html",
            "overall": r["overall"], "counts": counts,
            "missing_entries": sorted({p["path"] for p in r["probes"] if p["reason_code"] == "missing_target"}),
            "section_findings": [{"path": p["path"], "status": p["status"], "reason_code": p["reason_code"],
                                  "start_line": (p["observed"] or {}).get("start_line"),
                                  "end_line": (p["observed"] or {}).get("end_line")}
                                 for p in r["probes"] if p["probe"] == "text_section" and p["status"] != "pass"],
            "existing_issues": len(r["existing_issues"]), "mutations": ledger}


def run_demo(archive, profile_path, scenarios_path, output, include_excerpts=False, notice=None):
    profile = _load(profile_path)
    scenarios = _load(scenarios_path)
    validate_scenarios(scenarios)
    try:
        archive_probes._validate_profile(profile)
    except ValidationError as exc:
        raise CliError(f"profile rejected: {exc}") from None
    before = read_source(archive)
    if before["sha256"] != scenarios["expected_source_sha256"]:
        raise CliError("source archive SHA-256 does not match expected_source_sha256; nothing generated")
    entries = load_entries(before["data"])
    planned = []  # every mutation is applied in memory first, so a bad marker refuses before any output
    for case in scenarios["cases"]:
        working = [dict(e) for e in entries]
        planned.append((case, working, [apply_mutation(working, m) for m in case["mutations"]]))
    out = _new_dir(output, [archive, profile_path, scenarios_path])
    os.mkdir(os.path.join(out, "archives"))
    os.mkdir(os.path.join(out, "cases"))
    cases = []
    for case, working, ledger in planned:
        target = os.path.join(out, "archives", case["id"] + ".zip")
        if ledger:
            write_zip(target, working)
        else:
            with open(target, "xb") as handle:
                handle.write(before["data"])  # byte-for-byte control/restored copy
        report = run_check(target, profile, f'{scenarios["label"]} / {case["label"]}', include_excerpts)
        case_dir = os.path.join(out, "cases", case["id"])
        os.mkdir(case_dir)
        _write_json(os.path.join(case_dir, "report.json"), report)
        _write(os.path.join(case_dir, "report.html"), report_html(report))
        cases.append(case_summary(case, report, ledger, f"archives/{case['id']}.zip"))
    try:  # the source is re-verified BEFORE any summary or notice is published
        after_sha = read_source(archive)["sha256"]
    except CliError:
        after_sha = None
    unchanged = after_sha == before["sha256"]
    if unchanged:
        text = notice or "actual-derived-copy: 원본의 복사본에만 변경을 주입했습니다. 원본은 바뀌지 않았고 실제 제출·업로드는 없습니다."
    else:
        text = ("INCOMPLETE: 시연 중 원본 ZIP이 바뀌었거나 다시 읽을 수 없었습니다. 아래 사례 결과는 유효하지 않습니다. "
                "실제 제출·업로드는 없습니다.")
    summary = {"format": SUMMARY_FORMAT, "label": scenarios["label"], "complete": unchanged, "notice": text,
               "source": {"file_name": before["relative_path"], "sha256_before": before["sha256"],
                          "sha256_after": after_sha, "unchanged": unchanged},
               "profile_sha256": _sha(json.dumps(profile, sort_keys=True, ensure_ascii=False).encode()), "cases": cases}
    _write_json(os.path.join(out, "summary.json"), summary)
    _write(os.path.join(out, "index.html"), index_html(summary))
    if not unchanged:
        raise CliError("source archive changed during the demonstration; results are not valid")
    return out, summary


# --- synthetic sample, doctor, commands ---

SAMPLE_REPORT = ("Fictional lab report (synthetic)\n\nBEGIN_SECTION SAMPLE\nObserved values were recorded for run 1.\n"
                 "END_SECTION SAMPLE\n\nBEGIN_SECTION NOTES\nNo deviations noted.\nEND_SECTION NOTES\n")


def sample_profile():
    note = "synthetic sample rule (가상 과제 재현)"
    return {"profile_version": "archive-profile/1",
            "required_entries": [{"path": p, "reason": note, "evidence": "generated by snu_submit.py sample"}
                                 for p in ("report.txt", "data/values.csv", "results/run1.txt")],
            "nonempty_entries": [{"path": "report.txt", "reason": note, "evidence": "generated", "encoding": "utf-8"},
                                 {"path": "data/values.csv", "reason": note, "evidence": "generated", "encoding": None}],
            "text_sections": [{"path": "report.txt", "start_literal": "BEGIN_SECTION SAMPLE", "end_literal": "END_SECTION SAMPLE",
                               "encoding": "utf-8", "reason": note, "evidence": "generated"}],
            "existing_issues": []}


def cmd_sample(args):
    out = _new_dir(args.output)
    inputs = os.path.join(out, "inputs")
    os.mkdir(inputs)
    archive = os.path.join(inputs, "sample.zip")
    write_zip(archive, [{"name": n, "info": zipfile.ZipInfo(n), "data": d, "is_dir": False} for n, d in (
        ("report.txt", SAMPLE_REPORT.encode()), ("data/values.csv", b"run,value\n1,42\n"),
        ("results/run1.txt", b"synthetic run output\n"))])
    _write_json(os.path.join(inputs, "profile.json"), sample_profile())
    with open(archive, "rb") as handle:
        digest = _sha(handle.read())
    scenarios = {"version": DEMO_VERSION, "expected_source_sha256": digest, "label": "synthetic sample (가상 과제 재현)",
                 "cases": [{"id": "control", "label": "unchanged copy", "mutations": []},
                           {"id": "missing-file", "label": "required file removed",
                            "mutations": [{"op": "remove_entry", "path": "data/values.csv"}]},
                           {"id": "empty-file", "label": "required file emptied",
                            "mutations": [{"op": "empty_entry", "path": "data/values.csv"}]},
                           {"id": "removed-section", "label": "marked section removed, file kept",
                            "mutations": [{"op": "remove_text_section", "path": "report.txt",
                                           "start_literal": "BEGIN_SECTION SAMPLE", "end_literal": "END_SECTION SAMPLE"}]},
                           {"id": "restored", "label": "original bytes restored", "mutations": []}]}
    _write_json(os.path.join(inputs, "scenarios.json"), scenarios)
    demo_dir, summary = run_demo(archive, os.path.join(inputs, "profile.json"), os.path.join(inputs, "scenarios.json"),
                                 os.path.join(out, "demo"), notice="synthetic: 코드로 만든 가상 과제 재현 자료입니다. 실제 과제·제출이 아닙니다.")
    print(f"sample written: {out}")
    _print_summary(demo_dir, summary)
    return 0


def _print_summary(directory, summary):
    print(f"index: {os.path.join(directory, 'index.html')}")
    for case in summary["cases"]:
        print(f"  {case['id']}: {case['overall']} (pass {case['counts']['pass']}, fail {case['counts']['fail']}, "
              f"unknown {case['counts']['unknown']})")


def cmd_doctor(_args):
    checks = [{"name": "python", "ok": sys.version_info >= (3, 11), "detail": f"{platform.python_version()} {sys.executable}"}]
    for dist in ("pypdf", "jsonschema"):
        try:
            checks.append({"name": dist, "ok": True, "detail": importlib.metadata.version(dist)})
        except importlib.metadata.PackageNotFoundError:
            checks.append({"name": dist, "ok": False, "detail": "not installed"})
    for module in ("submit_checks.core", "submit_checks.parsers", "submit_checks.archive_probes", "submit_checks.file_review", "submit_checks.report"):
        try:
            importlib.import_module(module)
            checks.append({"name": module, "ok": True, "detail": "imported"})
        except Exception as exc:  # report, never claim success
            checks.append({"name": module, "ok": False, "detail": type(exc).__name__})
    for schema in ("source.json", "requirements.json", "checks.json"):
        try:
            from submit_checks.core import _packaged_validator
            _packaged_validator(schema)
            checks.append({"name": "schema " + schema, "ok": True, "detail": "valid"})
        except Exception as exc:
            checks.append({"name": "schema " + schema, "ok": False, "detail": type(exc).__name__})
    template = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "assets", "report.html")
    checks.append({"name": "assets/report.html", "ok": os.path.isfile(template), "detail": "present" if os.path.isfile(template) else "missing"})
    ok = all(c["ok"] for c in checks)
    print(json.dumps({"tool": "snu-submit", "tool_version": TOOL_VERSION, "ok": ok, "checks": checks,
                      "note": "eTL login is NOT checked here; the host agent must confirm it in the user's Chrome. "
                              "This command makes no network request."}, ensure_ascii=True, indent=2))
    return 0 if ok else 1


def cmd_check(args, previous=None):
    profile = _load(args.profile)
    prior = _load(previous, MAX_REPORT_JSON) if previous else None
    report = run_check(args.archive, profile, _label(args.label), args.include_excerpts)
    if prior is not None:
        report["delta"] = compare(prior, report)
    inputs = [args.archive, args.profile] + ([previous] if previous else [])
    out = _new_dir(args.output, inputs)
    _write_json(os.path.join(out, "report.json"), report)
    _write(os.path.join(out, "report.html"), report_html(report))
    print(f"report: {os.path.join(out, 'report.json')}\nhtml: {os.path.join(out, 'report.html')}")
    print(f"verdict: {report['result']['overall']} (archive-profile only; not a full submission-ready verdict)")
    return 0


def cmd_demo(args):
    out, summary = run_demo(args.archive, args.profile, args.scenarios, args.output, args.include_excerpts)
    _print_summary(out, summary)
    return 0


def cmd_review(args):
    profile = _load(args.profile)
    report = file_review.inspect_files(args.file, profile)
    report["generated_at"] = _now()
    report["label"] = _label(args.label)
    if not args.include_content:
        for item in report["files"]:
            item.pop("text", None)
    out = _new_dir(args.output, [*args.file, args.profile])
    _write_json(os.path.join(out, "report.json"), report)
    esc = lambda value: html.escape(str(value), quote=True)
    rows = "".join("<tr>" + "".join("<td>" + esc(value) + "</td>" for value in (
        item["path"], item["status"], item["bytes"], item["format"],
        "; ".join(item["problems"] + item["limits"]))) + "</tr>" for item in report["files"])
    checks = "".join("<li><strong>" + esc(check["description"]) + "</strong>: " + esc(check["status"]) +
                     "<p>근거: " + esc(check["evidence"]) + "</p><p>대응 파일: " +
                     esc(", ".join(check["matched_paths"]) or "없음") + "</p><p>찾지 못한 필수 텍스트: " +
                     esc(", ".join(check["missing_literals"]) or "없음") + "</p></li>" for check in report["checks"])
    content = "".join("<details><summary>" + esc(item["path"]) + " 내용</summary><pre>" +
                      esc(item.get("text") or "추출한 텍스트 없음") + "</pre></details>" for item in report["files"]) if args.include_content else ""
    page = ('<!doctype html><html lang="ko"><meta charset="utf-8"><meta name="viewport" content="width=device-width">'
            '<title>SNU Submit 파일 점검</title><style>body{font:16px/1.6 system-ui;max-width:1200px;margin:40px auto;padding:0 20px}'
            'table{border-collapse:collapse;width:100%}td,th{border:1px solid #bbb;padding:8px;text-align:left;overflow-wrap:anywhere}'
            'pre{white-space:pre-wrap;overflow-wrap:anywhere}h1{font-size:28px}</style>'
            '<h1>' + esc(report["verdict"]) + '</h1><p>' + esc(report["summary"]) + '</p><p>' + esc(report["scope"]) +
            '</p><p>점검 시각: ' + esc(report["generated_at"]) + '</p><h2>출처 확인 제한</h2><ul>' +
            ''.join('<li>' + esc(limit) + '</li>' for limit in report["source_limits"]) +
            '</ul><h2>요구사항</h2><ul>' + checks + '</ul><h2>파일 검사</h2><table><tr><th>파일</th><th>결과</th><th>바이트</th>'
            '<th>형식</th><th>직접 확인할 내용·오류</th></tr>' + rows + '</table><h2>읽기 실패</h2><ul>' +
            ''.join('<li>' + esc(f["path"] + ': ' + f["reason"]) + '</li>' for f in report["failures"]) + '</ul>' + content + '</html>')
    _write(os.path.join(out, "report.html"), page)
    print(f"verdict: {report['verdict']}\nreport: {os.path.join(out, 'report.json')}\nhtml: {os.path.join(out, 'report.html')}")
    return 0


def build_parser():
    parser = argparse.ArgumentParser(prog="snu_submit.py", description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("doctor", help="check Python, dependencies, imports and packaged resources (no network, no login check)")
    p = sub.add_parser("review", help="inspect selected files and ZIP contents against a file-review/1 profile")
    p.add_argument("--file", action="append", required=True, help="selected local file; repeat for multiple files")
    p.add_argument("--profile", required=True, help="file-review/1 JSON with source evidence and required file patterns")
    p.add_argument("--output", required=True, help="NEW directory for report.json and report.html")
    p.add_argument("--label", default="파일 제출 요건 점검")
    p.add_argument("--include-content", action="store_true", help="include extracted text for the host's content review")
    for name, text in (("check", "inspect one ZIP with an archive-profile/1 JSON profile"),
                       ("recheck", "like check, plus a delta against a previous report.json (never reuses its results)")):
        p = sub.add_parser(name, help=text)
        p.add_argument("--archive", required=True, help="local ZIP to inspect (read only)")
        p.add_argument("--profile", required=True, help="archive-profile/1 JSON file")
        p.add_argument("--output", required=True, help="NEW directory for report.json and report.html")
        p.add_argument("--label", default="archive-profile check", help="visible label (default: %(default)s)")
        p.add_argument("--include-excerpts", action="store_true", help="keep section body excerpts in reports")
        if name == "recheck":
            p.add_argument("--previous", required=True, help="earlier report.json from check/recheck")
    p = sub.add_parser("demo", help="copy-only fault injection: run the profile on mutated copies (source never modified)")
    p.add_argument("--archive", required=True, help="source ZIP (read only; SHA-256 must match the scenarios file)")
    p.add_argument("--profile", required=True, help="archive-profile/1 JSON file")
    p.add_argument("--scenarios", required=True, help="archive-demo/1 JSON file")
    p.add_argument("--output", required=True, help="NEW directory for copies, case reports, index.html, summary.json")
    p.add_argument("--include-excerpts", action="store_true", help="keep section body excerpts in reports")
    p = sub.add_parser("sample", help="write a wholly synthetic archive/profile/scenarios and run the demo on it")
    p.add_argument("--output", required=True, help="NEW directory")
    return parser


def _safe_console():
    """Never fail on console encoding (e.g. Windows cp949): keep the console's codec, escape what it cannot show.

    Only stdout/stderr are affected; report files are always written as UTF-8.
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="backslashreplace")
        except (AttributeError, ValueError, OSError):
            pass


def main(argv=None):
    _safe_console()
    args = build_parser().parse_args(argv)
    try:
        if args.command == "doctor":
            return cmd_doctor(args)
        if args.command == "review":
            return cmd_review(args)
        if args.command == "check":
            return cmd_check(args)
        if args.command == "recheck":
            return cmd_check(args, previous=args.previous)
        if args.command == "demo":
            return cmd_demo(args)
        return cmd_sample(args)
    except CliError as exc:
        print(f"error: {str(exc)[:500]}", file=sys.stderr)
    except (OSError, ValidationError) as exc:
        print(f"error: {type(exc).__name__}: {str(exc)[:300]}", file=sys.stderr)
    except Exception as exc:  # never report success after an unexpected failure
        print(f"error: unexpected {type(exc).__name__}", file=sys.stderr)
    print("no result was produced; any partial output directory is incomplete", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
