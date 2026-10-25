"""Escaped, script-free local HTML rendering of a validated checks.json report.

render_report(report) validates the report with the packaged schema, then
builds Korean HTML from the packaged assets/report.html template. Every report
value is treated as untrusted text and HTML-escaped; no value is placed in CSS,
scripts or element IDs (IDs are generated from indexes). The template's single
inline style is allowed by its SHA-256 in a default-src 'none' CSP. Nothing is
written to disk and the input is not modified. JSON stays the source of truth.
"""

import base64
import hashlib
import html
import re
from pathlib import Path

from .core import ValidationError, validate_document

TEMPLATE_PATH = Path(__file__).resolve().parents[2] / "assets" / "report.html"
MAX_TEMPLATE_BYTES = 64 * 1024
STATUS_LABEL = {"pass": ("통과", "st-pass"), "fail": ("위반", "st-fail"),
                "unknown": ("확인 필요", "st-unknown"), "not_applicable": ("해당 없음", "st-na")}
VERDICT_CLASS = {"ready": "v-ready", "needs_changes": "v-changes", "needs_confirmation": "v-confirm"}
SECTIONS = [("sec-verdict", "종합 판정"), ("sec-files", "제출 파일"), ("sec-checks", "요구사항별 결과"),
            ("sec-unknown", "확인 필요 사유"), ("sec-coverage", "수집 범위"), ("sec-freshness", "출처 최신성"),
            ("sec-window", "제출 기간·서버 상태"), ("sec-sources", "출처"), ("sec-delta", "이전 실행과 비교")]


def _e(value):
    """Escape any value as text. None/booleans get fixed Korean words."""
    if value is None:
        return "—"
    if value is True:
        return "예"
    if value is False:
        return "아니오"
    return html.escape(str(value), quote=True)


def _code(value):
    return "—" if value is None else f"<code>{_e(value)}</code>"


def _status(status):
    label, css = STATUS_LABEL.get(status, ("알 수 없음", "st-unknown"))
    return f'<span class="st {css}">{_e(label)} ({_e(status)})</span>'


def _table(caption, headers, rows):
    head = "".join(f'<th scope="col">{_e(h)}</th>' for h in headers)
    body = "".join("<tr>" + "".join(f"<td>{cell}</td>" for cell in row) + "</tr>" for row in rows)
    if not body:
        body = f'<tr><td colspan="{len(headers)}">없음</td></tr>'
    return (f'<div class="scroll"><table><caption>{_e(caption)}</caption><thead><tr>{head}</tr></thead>'
            f"<tbody>{body}</tbody></table></div>")


def _kv(caption, pairs):
    rows = "".join(f'<tr><th scope="row">{_e(label)}</th><td>{cell}</td></tr>' for label, cell in pairs)
    return f'<div class="scroll"><table><caption>{_e(caption)}</caption><tbody>{rows}</tbody></table></div>'


def _list(items):
    items = list(items)
    if not items:
        return "없음"
    return '<ul class="plain">' + "".join(f"<li>{item}</li>" for item in items) + "</ul>"


def _location(loc):
    if not loc:
        return "—"
    parts = [f"{label} {loc[key]}" for key, label in (("page", "쪽"), ("paragraph", "문단"), ("table", "표"),
                                                         ("row", "행"), ("column", "열")) if loc.get(key) is not None]
    if loc.get("char_start") is not None or loc.get("char_end") is not None:
        parts.append(f"문자 {loc.get('char_start')}–{loc.get('char_end')}")
    if loc.get("label"):
        parts.append(f"표지 {loc['label']}")
    return _e(", ".join(parts) or "—")


def _template():
    try:
        with open(TEMPLATE_PATH, "rb") as handle:
            raw = handle.read(MAX_TEMPLATE_BYTES + 1)
    except OSError as exc:
        raise ValidationError(f"report template unreadable: {type(exc).__name__}") from None
    if len(raw) > MAX_TEMPLATE_BYTES:
        raise ValidationError("report template too large")
    text = raw.decode("utf-8")
    styles = re.findall(r"<style>(.*?)</style>", text, re.S)
    if len(styles) != 1 or "{{" in styles[0] or text.count("{{BODY}}") != 1 or text.count("{{STYLE_HASH}}") != 1:
        raise ValidationError("report template is malformed")
    digest = base64.b64encode(hashlib.sha256(styles[0].encode("utf-8")).digest()).decode("ascii")
    head, tail = text.split("{{BODY}}")
    return head.replace("{{STYLE_HASH}}", digest), tail


def _header(report):
    out = ["<header>", "<h1>SNU Submit 제출 전 점검 보고서</h1>"]
    if report["mode"] == "synthetic":
        out.append(f'<p class="synthetic" role="note">{_e(report["synthetic_label"])} — 가상으로 만든 과제를 재현한 '
                   "결과입니다. 실제 eTL 과제의 준비 상태나 실제 서비스 결과가 아닙니다.</p>")
    out.append(f'<p class="small">SNU Submit {_e(report["tool_version"])} · offline-preview(알파) · 보고서 스키마 '
               f'{_e(report["report_schema_version"])} · 규칙 {_e(report["rules_version"])} · 모드 {_e(report["mode"])}</p>')
    out.append(f'<p class="notice" role="note">{_e(report["snapshot_notice"])}</p>')
    a = report["assignment"]
    out.append(_kv("실행 정보", [("실행 ID", _code(report["run_id"])), ("보고서 생성", _e(report["generated_at"])),
                                ("검사 시작", _e(report["inspection_started_at"])),
                                ("검사 종료", _e(report["inspection_finished_at"])),
                                ("강의 ID", _code(a["course_id"])), ("과제 ID", _code(a["assignment_id"])),
                                ("분반", _e(a["section_id"])), ("대상", _e(a["audience"])),
                                ("과제 제목", _e(a.get("title"))), ("과제 주소(링크 아님)", _code(a.get("url")))]))
    out.append('<nav aria-label="보고서 목차"><ul>' + "".join(
        f'<li><a href="#{sid}">{_e(title)}</a></li>' for sid, title in SECTIONS) + "</ul></nav></header>")
    return "".join(out)


def _verdict(report):
    v = report["verdict"]
    note = "파일 준비 상태만 나타내며, 제출 접수·마감 준수·답안 품질을 뜻하지 않습니다."
    if v["overall"] == "ready" and report["mode"] == "synthetic":
        note = "가상 과제 재현 결과입니다. 실제 과제나 실제 서비스의 준비 완료가 아닙니다. " + note
    counts = _table("필수 조건 집계", ["전체", "통과", "위반", "확인 필요", "해당 없음", "출처 최신성"],
                    [[_e(v["mandatory_total"]), _e(v["mandatory_pass"]), _e(v["mandatory_fail"]),
                      _e(v["mandatory_unknown"]), _e(v["mandatory_not_applicable"]), _e(v["freshness"])]])
    return (f'<section id="sec-verdict"><h2>종합 판정</h2><div class="verdict {VERDICT_CLASS[v["overall"]]}">'
            f'<p class="headline">{_e(v["label"])} <span class="small">({_e(v["overall"])})</span></p>'
            f'<p>{_e(v["summary"])}</p><p class="small">{_e(note)}</p></div>{counts}</section>')


def _files(report):
    sel = report["selection"]
    out = ['<section id="sec-files"><h2>제출 파일</h2>',
           _kv("선택 목록", [("확정 여부", _e(sel["resolved"])), ("목록 해시", _code(sel["manifest_sha256"])),
                            ("미확정 사유", _e(sel["unresolved_reason"]))]),
           _table("선택된 파일", ["파일 ID", "경로", "역할", "SHA-256"],
                  [[_code(f["file_id"]), _code(f["path"]), _e(f["role"]), _code(f["sha256"])] for f in sel["files"]]),
           _table("검사한 파일", ["#", "파일 ID", "경로", "크기(바이트)", "형식", "파서", "쪽", "글자", "단어", "검사 중 변경"],
                  [[_e(i + 1), _code(r["file_id"]), _code(r["path"]), _e(r["bytes"]), _e(r["detected_format"]),
                    f'{_e(r["parser"]["name"])} / {_e(r["parser"]["status"])}', _e(r["parser"]["metrics"]["pages"]),
                    _e(r["parser"]["metrics"]["text_chars"]), _e(r["parser"]["metrics"]["text_words"]),
                    _e(r["changed_during_inspection"])] for i, r in enumerate(report["files"])])]
    for i, r in enumerate(report["files"]):
        p = r["parser"]
        out.append(f'<h3>파일 {i + 1} 세부: <code>{_e(r["path"])}</code></h3>')
        out.append(_kv("파일 상태", [("SHA-256", _code(r["sha256"])), ("존재", _e(r["exists"])),
                                     ("검사 전", _e(r["before"])), ("검사 후", _e(r["after"])),
                                     ("파서 한도 도달", _e(", ".join(p["limits_hit"]) or "없음")),
                                     ("파서 오류", _list(f'{_code(x["code"])} {_e(x["detail"])}' for x in p["errors"]))]))
        out.append(_table("읽지 못한 영역", ["종류", "위치", "설명"],
                          [[_e(u["kind"]), _location(u["location"]), _e(u["detail"])] for u in p["unread_regions"]]))
        if r["archive_entries"]:
            out.append(_table("압축 파일 항목", ["이름", "내부 경로", "읽은 바이트", "CRC", "암호화", "중첩", "위험한 이름", "SHA-256"],
                              [[_code(x["name"]), _code(x["entry_path"]), _e(x["bytes_read"]), _e(x["crc_ok"]),
                                _e(x["encrypted"]), _e(x["nested_archive"]), _e(x["unsafe_name"]), _code(x["sha256"])]
                               for x in r["archive_entries"]]))
    out.append("</section>")
    return "".join(out)


def _target(target):
    kind = target["type"]
    if kind == "resolved_files":
        return _list(f'{_code(f["file_id"])} {_code(f["path"])} SHA {_code(f["sha256"])}'
                     + (f' 내부 {_code(f["entry_path"])} SHA {_code(f["entry_sha256"])}' if f["entry_path"] else "")
                     for f in target["files"])
    if kind == "submission_set":
        return f'제출 집합 {_e(target["count"])}개, 목록 해시 {_code(target["manifest_sha256"])}, 파일 ' + _e(
            ", ".join(target["file_ids"]))
    if kind == "missing_target":
        container = target["container"]
        return (f'없음 — 기대 역할 {_e(target["expected_role"])}, 이름 {_code(target["expected_name"])}, '
                f'경로 {_code(target["expected_path"])}'
                + (f', 압축 파일 {_code(container["file_id"])}' if container else ""))
    return f'확정 안 됨 ({_code(target["reason_code"])}): {_e(target["reason"])}'


def _checks(report):
    checks = report["checks"]
    out = ['<section id="sec-checks"><h2>요구사항별 결과</h2>',
           _table("요약", ["#", "요구사항 ID", "결과", "필수", "종류", "축"],
                  [[f'<a href="#check-{i}">{i + 1}</a>', _code(c["requirement_id"]), _status(c["status"]),
                    _e(c["mandatory"]), _e(c["kind"]), _e(c["axis"])] for i, c in enumerate(checks)])]
    for i, c in enumerate(checks):
        ev, ob = c["evidence"], c["observed"]
        out.append(f'<h3 id="check-{i}">{i + 1}. <code>{_e(c["requirement_id"])}</code> — {_status(c["status"])}</h3>')
        out.append(f"<blockquote>{_e(ev['quote'])}</blockquote>")
        out.append(_kv("근거와 판정", [
            ("출처", _code(ev["source_id"])), ("인용 위치(코드 포인트)", _e(f'{ev["span"]["start"]}–{ev["span"]["end"]}')),
            ("원문 위치", _location(ev["location"])), ("근거 검증", _e(ev["verified"])),
            ("결과", _status(c["status"])), ("방법", _e(c["method"])), ("필수", _e(c["mandatory"])),
            ("종류 / 축", f'{_e(c["kind"])} / {_e(c["axis"])}'), ("대상", _target(c["target"])),
            ("기대 조건", "—" if ob is None else f'{_e(ob["operator"])} {_e(ob["expected"])} {_e(ob["unit"])}'),
            ("판단 이유", _e(c["reason"])), ("확인 필요 코드", _e(", ".join(c["unknown_codes"]) or "없음")),
            ("수정 방법", _e(c["remediation"])), ("요구사항 해시", _code(c["requirement_sha256"]))]))
        if ob is not None:
            out.append(_table("관찰값", ["파일 ID", "내부 경로", "값", "결과"],
                              [[_code(v["file_id"]), _code(v["entry_path"]), _e(v["value"]), _status(v["status"])]
                               for v in ob["values"]]))
        if c["content_candidates"]:
            out.append(_table("내용 후보(확인 전에는 통과가 아님)", ["파일 ID", "위치", "발췌"],
                              [[_code(x["file_id"]), _location(x["location"]), f"<blockquote>{_e(x['excerpt'])}</blockquote>"]
                               for x in c["content_candidates"]]))
    out.append("</section>")
    return "".join(out)


def _unknowns(report):
    return ('<section id="sec-unknown"><h2>확인 필요 사유</h2>'
            + _table("모든 확인 필요 사유", ["코드", "요구사항", "출처", "파일", "설명"],
                     [[_code(u["code"]), _code(u["requirement_id"]), _code(u["source_id"]), _code(u["file_id"]),
                       _e(u["detail"])] for u in report["unknown_reasons"]]) + "</section>")


def _coverage(report):
    cov = report["coverage"]
    mail = cov["mail"]
    return ('<section id="sec-coverage"><h2>수집 범위</h2><p class="small">호스트 수집 기록과 로컬 바이트 검증에 근거하며, '
            "독립적인 네트워크 확인이 아닙니다.</p>"
            + _kv("수집 범위 요약", [
                ("상태", _e(cov["state"])), ("학기 시작", _e(cov["semester_start"])),
                ("학기 시작 근거", _code(cov["semester_start_source_id"])), ("확인 기간 끝", _e(cov["through_at"])),
                ("공지 목록 페이지/건수", _e(f'{cov["notice_pages_read"]} / {cov["notice_records_read"]}')),
                ("공지 한도 도달", _e(cov["notice_cap_reached"])), ("공지 더 있음", _e(cov["notice_has_more"])),
                ("본문 잘림", _e(cov["body_truncated"])), ("고정 공지", _e(cov["pinned_notices"])),
                ("공통 공지", _e(cov["common_notices"])), ("참고 자료", _e(cov["referenced_materials"])),
                ("메일", _e(f'{mail["status"]} (페이지 {mail["pages_read"]}, {mail["records_read"]}건, 더 있음 '
                            f'{mail["has_more"]}, 한도 {mail["cap_reached"]})')),
                ("읽지 못한 필수 출처", _e(", ".join(cov["unread_required_sources"]) or "없음"))])
            + _table("수집 실패", ["코드", "출처", "설명"],
                     [[_code(f["code"]), _code(f["source_id"]), _e(f["detail"])] for f in cov["failures"]])
            + "</section>")


def _freshness(report):
    fr = report["freshness"]
    return ('<section id="sec-freshness"><h2>출처 최신성</h2>'
            + _kv("최종 재수집", [("상태", _e(fr["state"])), ("재수집 시각", _e(fr["refreshed_at"])), ("설명", _e(fr["reason"]))])
            + _table("출처별 재수집", ["출처", "이전 원문 해시", "현재 원문 해시", "이전 정규화 해시", "현재 정규화 해시", "관찰 시각", "상태"],
                     [[_code(x["source_id"]), _code(x["previous_artifact_sha256"]), _code(x["current_artifact_sha256"]),
                       _code(x["previous_normalized_text_sha256"]), _code(x["current_normalized_text_sha256"]),
                       _e(x["observed_at"]), _e(x["status"])] for x in fr["items"]]) + "</section>")


def _window(report):
    w, r = report["submission_window"], report["server_receipt"]
    return ('<section id="sec-window"><h2>제출 기간·서버 상태</h2><p class="notice" role="note">이 정보는 파일 준비 판정과 '
            "별개입니다. 파일이 준비되어도 제출이 접수되었다는 뜻이 아니며, 같은 파일 이름이 같은 내용이라는 뜻도 아닙니다.</p>"
            + _kv("제출 기간", [("상태", _e(w["state"])), ("마감(절대 시각)", _e(w["deadline_at"])),
                               ("마감 원문", _e(w["deadline_text"])), ("시간대", _e(w["timezone"])),
                               ("근거 출처", _code(w["source_id"])), ("설명", _e(w["reason"]))])
            + _kv("서버 제출 상태(관찰)", [("상태", _e(r["status"])), ("관찰 시각", _e(r["observed_at"])),
                                       ("근거 출처", _code(r["source_id"])),
                                       ("화면에 보인 파일 이름", _e(", ".join(r["observed_filenames"]) or "없음")),
                                       ("설명", _e(r["reason"]))]) + "</section>")


def _sources(report):
    return ('<section id="sec-sources"><h2>출처</h2>'
            + _table("출처 해시 요약", ["출처 ID", "종류", "수집 상태", "관찰 시각", "원문 파일", "원문 해시", "정규화 해시", "해시 검증"],
                     [[_code(s["source_id"]), _e(s["kind"]), _e(s["collection_status"]), _e(s["observed_at"]),
                       _code(s["artifact_ref"]), _code(s["artifact_sha256"]), _code(s["normalized_text_sha256"]),
                       _e(s["hash_verification"])] for s in report["sources"]]) + "</section>")


def _delta(report):
    d = report["delta"]
    lists = [(label, _e(", ".join(d[key]) or "없음")) for key, label in (
        ("files_added", "추가된 파일"), ("files_removed", "빠진 파일"), ("files_changed", "내용이 바뀐 파일"),
        ("sources_added", "추가된 출처"), ("sources_removed", "빠진 출처"), ("sources_changed", "바뀐 출처"),
        ("requirements_added", "추가된 요구사항"), ("requirements_removed", "빠진 요구사항"),
        ("requirements_changed", "바뀐 요구사항"))]
    return ('<section id="sec-delta"><h2>이전 실행과 비교</h2>'
            + _kv("변경 요약", [("이전 실행 ID", _code(d["previous_run_id"]))] + lists)
            + _table("이름이 바뀐 파일", ["파일 ID", "이전 경로", "현재 경로", "같은 바이트"],
                     [[_code(x["file_id"]), _code(x["previous_path"]), _code(x["current_path"]), _e(x["same_bytes"])]
                      for x in d["files_renamed"]])
            + _table("결과가 바뀐 요구사항", ["검사 ID", "요구사항", "이전", "현재"],
                     [[_code(x["check_id"]), _code(x["requirement_id"]),
                       "—" if x["previous_status"] is None else _status(x["previous_status"]), _status(x["current_status"])]
                      for x in d["check_status_changes"]]) + "</section>")


def render_report(report):
    """Validate the report and return escaped, script-free HTML (string only)."""
    validate_document(report, "checks.json")
    head, tail = _template()
    body = "".join([_header(report), _verdict(report), _files(report), _checks(report), _unknowns(report),
                    _coverage(report), _freshness(report), _window(report), _sources(report), _delta(report),
                    '<footer>이 HTML은 JSON 보고서에서 생성되었으며 JSON이 기준입니다. 스크립트와 외부 요청이 없습니다.</footer>'])
    return head + body + tail
