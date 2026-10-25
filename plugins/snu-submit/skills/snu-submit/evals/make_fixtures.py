#!/usr/bin/env python3
"""Write the input files of every eval case (가상 과제 재현: all courses, people and files are fictional).

    python evals/make_fixtures.py

Each case gets files/<submission files> and files/mock-etl/ (text copies of eTL screens plus
downloads). Existing files/ folders are replaced. Needs pypdf (the skill's own dependency).
"""

import io
import shutil
import sys
import zipfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
FIXED = (2026, 10, 20, 9, 0, 0)
W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
ETL = "https://myetl.snu.ac.kr"
MOCK_README = f"""테스트용 eTL 사본 (가상 과제 재현)

이 폴더는 실제 Chrome 대신 eTL 화면 역할을 한다. 모든 강좌·사람·과제는 가상이다.
- pages/ 아래 각 파일은 Chrome에서 그 주소를 열었을 때 보이는 화면 글자를 옮긴 것이다.
- 주소 → 파일: {ETL}/ → pages/index.txt, {ETL}/courses/51001/assignments → pages/courses/51001/assignments.txt,
  {ETL}/courses/51001/assignments/88003 → pages/courses/51001/assignments/88003.txt
- 각 페이지 끝의 [링크] 목록은 화면에 보이는 링크와 그 주소다. 파일이 없는 주소는 "페이지를 찾을 수 없음"으로 본다.
- 첨부 링크(…/files/<번호>/download)를 따라가면 downloads/의 같은 이름 파일을 받은 것으로 본다.
- 이 사본에는 제출·업로드 기능이 없다. 화면의 버튼 이름은 글자로만 적혀 있다.
"""


def zip_bytes(entries):
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, data in entries:
            info = zipfile.ZipInfo(name, date_time=FIXED)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o100644 << 16
            zf.writestr(info, data)
    return out.getvalue()


def docx(paragraphs):
    body = "".join(f'<w:p><w:r><w:t xml:space="preserve">{p}</w:t></w:r></w:p>' if p else "<w:p/>" for p in paragraphs)
    ns = "http://schemas.openxmlformats.org/"
    return zip_bytes([
        ("[Content_Types].xml", f'<Types xmlns="{ns}package/2006/content-types"><Default Extension="rels" '
                                f'ContentType="application/vnd.openxmlformats-package.relationships+xml"/><Override PartName="/word/document.xml" '
                                'ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/></Types>'),
        ("_rels/.rels", f'<Relationships xmlns="{ns}package/2006/relationships"><Relationship Id="rId1" '
                        f'Type="{ns}officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/></Relationships>'),
        ("word/document.xml", f'<w:document xmlns:w="{W}"><w:body>{body}<w:sectPr/></w:body></w:document>'),
    ])


def pdf(pages):
    """Text-only PDF (Helvetica, ASCII). pages: list of line lists."""
    from pypdf import PdfWriter
    from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject
    writer = PdfWriter()
    for lines in pages:
        page = writer.add_blank_page(width=595, height=842)
        font = DictionaryObject({NameObject("/Type"): NameObject("/Font"), NameObject("/Subtype"): NameObject("/Type1"),
                                 NameObject("/BaseFont"): NameObject("/Helvetica")})
        page[NameObject("/Resources")] = DictionaryObject({NameObject("/Font"): DictionaryObject({NameObject("/F1"): font})})
        escaped = [line.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)") for line in lines]
        stream = DecodedStreamObject()
        stream.set_data(("BT /F1 11 Tf 14 TL 60 780 Td " + " ".join(f"({line}) Tj T*" for line in escaped) + " ET").encode("latin-1"))
        page[NameObject("/Contents")] = writer._add_object(stream)
    writer.add_metadata({"/Producer": "snu-submit eval fixture"})
    out = io.BytesIO()
    writer.write(out)
    return out.getvalue()


def native(seed, size):
    """Deterministic non-empty bytes standing in for a proprietary file."""
    data, value = bytearray(), seed
    while len(data) < size:
        value = (value * 1103515245 + 12345) & 0x7FFFFFFF
        data.append(value & 0xFF)
    return b"NATIVE\x00\x01" + bytes(data)


def page(lines, links=()):
    text = "\n".join(lines).strip() + "\n"
    if links:
        text += "\n[링크]\n" + "\n".join(f"- {label} → {url}" for label, url in links) + "\n"
    return text


def dashboard(courses):
    lines = ["대시보드", "대시보드 옵션"]
    links = []
    for cid, name in courses:
        lines += [f"{name}의 과목 카드 색상 영역", name, "2026년 2학기", f"공지 - {name}", f"과제 - {name}", f"파일 - {name}"]
        links += [(f"공지 - {name}", f"/courses/{cid}/announcements"), (f"과제 - {name}", f"/courses/{cid}/assignments"),
                  (f"파일 - {name}", f"/courses/{cid}/files")]
    lines += ["할 일", "계정 · 대시보드 · 강좌 · 캘린더 · 받은 편지함 · 도움말"]
    return page(lines, links)


def assignment_list(cid, name, rows):
    lines = ["과제", "과제 검색", "정렬", "과제", "SIS에서 가져옴", "전체 성적의 40%"]
    links = []
    for aid, title, due, points in rows:
        lines += ["과제", title, f"마감 {due}", due, f"-/{points}점"]
        links.append((title, f"{ETL}/courses/{cid}/assignments/{aid}"))
    return page([f"과제: {name}"] + lines, links)


def announcement_list(cid, name, rows):
    lines = [f"공지: {name}", "공지", "공지 사항 필터", "전체보기", "공지 사항 목록"]
    links = []
    for tid, author, title, scope, preview, posted in rows:
        lines += [author[0], title, scope, preview, "", "게시일시:", "", posted, ""]
        links.append((title, f"{ETL}/courses/{cid}/discussion_topics/{tid}"))
    return page(lines, links)


def notice_detail(author, title, scope, body, posted, attachments=()):
    lines = [author, title, scope, "읽지 않은 댓글이 없습니다.", "", body, "", f"게시일시: {posted}",
             "이 공지사항에는 댓글을 작성할 수 없습니다."] + [name for name, _ in attachments]
    return page(lines, list(attachments))


def write_mock(root, pages, downloads):
    mock = root / "mock-etl"
    (mock / "pages").mkdir(parents=True)
    (mock / "downloads").mkdir()
    (mock / "README.txt").write_text(MOCK_README, encoding="utf-8")
    for rel, text in pages.items():
        target = mock / "pages" / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
    for name, data in downloads.items():
        (mock / "downloads" / name).write_bytes(data)


# ---------------------------------------------------------------------------
# Cases
# ---------------------------------------------------------------------------

ALGO = (51001, "2026-2 알고리즘 실습 (002)")
SOLID = (51207, "2026-2 고체물리 (001)")
WRITE = (51990, "2026-2 글쓰기의 기초 (015)")


def case_normal(root):
    cid, name = ALGO
    report = pdf([
        ["Algorithm Lab HW3: Sorting Algorithm Comparison", "Student ID: 2026-12345", "Name: Kim Seoul", "",
         "1. Method", "We implemented insertion sort, merge sort and quick sort in Python 3.12.",
         "Each algorithm sorted random integer lists of size 1,000 to 100,000, five runs each.",
         "Timing used time.perf_counter on the same laptop with no other load."],
        ["2. Results", "n = 1,000: insertion 0.041 s, merge 0.002 s, quick 0.001 s",
         "n = 10,000: insertion 4.1 s, merge 0.025 s, quick 0.016 s",
         "n = 100,000: insertion not run (over 6 min), merge 0.31 s, quick 0.20 s",
         "Quick sort was fastest on random input; insertion sort grew quadratically."],
        ["3. Conclusion", "For random inputs above a few thousand elements merge or quick sort should be used.",
         "Insertion sort is only competitive for very small or nearly sorted lists.",
         "Quick sort's worst case did not appear because pivots were chosen at random."],
    ])
    (root / "2026-12345_KimSeoul_HW3.pdf").write_bytes(report)
    notices = [
        (77104, "조교 이하늘", "[001분반 전용] 과제 3 제출 형식", "1 섹션\n2026-2 알고리즘 실습 (001)",
         "001분반은 과제 3을 HWP 파일로 제출합니다. 002분반은 해당 없습니다.", "2026년 10월 21일 오후 3:10"),
        (77103, "조교 이하늘", "과제 3 추가 안내", "모든 섹션",
         "과제 3 보고서 첫 쪽에 학번(Student ID)과 이름을 꼭 적어 주세요. 그 밖의 조건은 과제 안내와 같습니다.",
         "2026년 10월 20일 오전 10:05"),
        (77102, "조교 이하늘", "실습실 변경 안내", "모든 섹션", "10월 셋째 주 실습은 302동 311호에서 진행합니다.",
         "2026년 10월 12일 오후 1:00"),
        (77101, "교수 박정렬", "강의 계획 및 과제 공통 규칙", "모든 섹션",
         "과제는 eTL 과제 메뉴로만 받습니다. 늦은 제출은 받지 않습니다.", "2026년 9월 1일 오전 9:00")]
    pages = {f"courses/{cid}/discussion_topics/{tid}.txt": notice_detail(a, t, s, b, p) for tid, a, t, s, b, p in notices}
    pages.update({
        "index.txt": dashboard([ALGO, SOLID, WRITE]),
        f"courses/{cid}/assignments.txt": assignment_list(cid, name, [
            (88001, "과제 1: 재귀와 반복", "9월 26일 오후 11:59", 10), (88002, "과제 2: 이진 탐색", "10월 10일 오후 11:59", 10),
            (88003, "과제 3: 정렬 알고리즘 비교 보고서", "10월 30일 오후 11:59", 20)]),
        f"courses/{cid}/assignments/88003.txt": page([
            "과제 3: 정렬 알고리즘 비교 보고서", "과제 제출",
            "마감 10월 30일 기준 오후 11:59 점수 20 제출물 유형 파일 업로드 파일 형식 pdf",
            "가능 2026년 10월 20일 오전 12:00 - 2026년 10월 30일 오후 11:59 (KST)",
            "Submit ONE PDF report comparing insertion sort, merge sort and quick sort.",
            "The report must contain the sections '1. Method', '2. Results' and '3. Conclusion' and must not exceed 4 pages.",
            "File name: StudentID_Name_HW3.pdf (e.g. 2026-00000_HongGildong_HW3.pdf).",
            "", "제출물", "제출 가능 — 아직 제출하지 않았습니다."]),
        f"courses/{cid}/announcements.txt": announcement_list(cid, name, notices),
    })
    write_mock(root, pages, {})


def case_missing(root):
    cid, name = 52003, "2026-2 프로그래밍 응용 (003)"
    report = docx(["과제 2 보고서: 그래프 탐색", "학번 2026-12345 김서울", "1. 구현 방법",
                   "큐를 이용한 너비 우선 탐색(BFS)으로 최단 이동 횟수를 구했다. 방문 표시는 집합으로 관리했다.",
                   "2. 실행 결과", "3. 결론", "BFS는 간선 가중치가 같을 때 최단 경로를 보장하며 시간 복잡도는 O(V+E)였다."])
    bfs = ("from collections import deque\n\n\ndef bfs(graph, start):\n    seen, queue, order = {start}, deque([start]), []\n"
           "    while queue:\n        node = queue.popleft()\n        order.append(node)\n        for nxt in graph[node]:\n"
           "            if nxt not in seen:\n                seen.add(nxt)\n                queue.append(nxt)\n    return order\n")
    png = b"\x89PNG\r\n\x1a\n" + b"\x00\x00\x00\rIHDR" + bytes(17)
    (root / "hw2.zip").write_bytes(zip_bytes([("bfs.py", bfs.encode()), ("report.docx", report), ("result.png", png)]))
    notices = [
        (78203, "조교 최그래프", "[정정] 과제 2 제출물", "모든 섹션",
         "과제 2 제출물을 정정합니다. 실행 결과 화면 캡처(result.png) 대신 프로그램 실행 로그 파일(run_log.txt)을 zip에 포함해 주세요. "
         "result.png는 넣지 않아도 됩니다. 나머지는 과제 안내와 같습니다.", "2026년 10월 23일 오후 5:40"),
        (78202, "조교 최그래프", "과제 1 채점 완료", "모든 섹션", "과제 1 점수를 확인하세요.", "2026년 10월 15일 오전 11:00"),
        (78201, "교수 정자료", "수업 운영 안내", "모든 섹션", "과제는 eTL로 제출합니다. 질문은 게시판에 남겨 주세요.",
         "2026년 9월 1일 오전 9:30")]
    pages = {f"courses/{cid}/discussion_topics/{tid}.txt": notice_detail(a, t, s, b, p) for tid, a, t, s, b, p in notices}
    pages.update({
        "index.txt": dashboard([(cid, name), SOLID, WRITE]),
        f"courses/{cid}/assignments.txt": assignment_list(cid, name, [
            (89001, "과제 1: 스택과 큐", "10월 3일 오후 11:59", 10), (89002, "과제 2: 그래프 탐색 구현", "10월 31일 오후 11:59", 15)]),
        f"courses/{cid}/assignments/89002.txt": page([
            "과제 2: 그래프 탐색 구현", "과제 제출", "마감 10월 31일 기준 오후 11:59 점수 15 제출물 유형 파일 업로드 파일 형식 zip",
            "가능 2026년 10월 17일 오전 12:00 - 2026년 10월 31일 오후 11:59 (KST)",
            "bfs.py와 보고서(report.docx), 실행 결과 화면 캡처(result.png)를 hw2.zip 하나로 묶어 제출하세요.",
            "보고서에는 '1. 구현 방법', '2. 실행 결과', '3. 결론' 세 절을 모두 쓰세요.", "", "제출물", "제출 가능 — 아직 제출하지 않았습니다."]),
        f"courses/{cid}/announcements.txt": announcement_list(cid, name, notices),
    })
    write_mock(root, pages, {})


def case_unreadable(root):
    cid, name = 53001, "2026-2 공업수학 2 (001)"
    good = pdf([["Homework 5", "Problem 1. Solve y'' + y = 0.", "Answer: y = A cos x + B sin x"]])
    broken = good[:9] + b"\n1 0 obj\n<< /Type /Catalog /Pages 2 0 R >>\nendobj\n" + bytes(range(256)) * 3
    (root / "HW5_solution.pdf").write_bytes(broken)
    pages = {
        "index.txt": dashboard([(cid, name), WRITE]),
        f"courses/{cid}/assignments.txt": assignment_list(cid, name, [
            (90004, "Homework 4", "10월 15일 오후 11:59", 10), (90005, "Homework 5", "10월 29일 오후 11:59", 10)]),
        f"courses/{cid}/assignments/90005.txt": page([
            "Homework 5", "과제 제출", "마감 10월 29일 기준 오후 11:59 점수 10 제출물 유형 파일 업로드",
            "가능 2026년 10월 22일 오전 12:00 - 2026년 10월 29일 오후 11:59 (KST)",
            "Upload your handwritten or typed solutions to Problems 1-4 as a single PDF file.", "", "제출물", "제출 가능"]),
        f"courses/{cid}/announcements.txt": announcement_list(cid, name, [
            (79101, "교수 한미분", "Homework policy", "모든 섹션", "All homework must be uploaded as one PDF file.",
             "2026년 9월 2일 오전 10:00")]),
        f"courses/{cid}/discussion_topics/79101.txt": notice_detail(
            "교수 한미분", "Homework policy", "모든 섹션", "All homework must be uploaded as one PDF file.", "2026년 9월 2일 오전 10:00"),
    }
    write_mock(root, pages, {})


def case_ambiguous(root):
    mat, mech = (54002, "2026-2 재료공학실험 1 (002)"), (54101, "2026-2 기계공학실험 (001)")
    report = docx(["인장 시험 결과 보고서 (Tensile Test Report)", "1. 실험 목적", "시편의 항복 강도와 인장 강도를 구한다.",
                   "2. 실험 방법", "만능 시험기로 상온에서 변형률 속도 0.001/s로 당겼다.", "3. 결과", "항복 강도 250 MPa, 인장 강도 410 MPa.",
                   "4. 고찰", "네킹 이후 공칭 응력이 감소했다."])
    (root / "lab_report_3.docx").write_bytes(report)
    pages = {
        "index.txt": dashboard([mat, mech, WRITE]),
        f"courses/{mat[0]}/assignments.txt": assignment_list(mat[0], mat[1], [
            (91003, "실험 보고서 3: 인장 시험", "10월 30일 오후 11:59", 10)]),
        f"courses/{mat[0]}/assignments/91003.txt": page([
            "실험 보고서 3: 인장 시험", "과제 제출", "마감 10월 30일 기준 오후 11:59 점수 10 제출물 유형 파일 업로드",
            "가능 2026년 10월 23일 오전 12:00 - 2026년 10월 30일 오후 11:59 (KST)",
            "인장 시험 보고서를 PDF로 제출하세요. 목적·방법·결과·고찰을 포함합니다.", "", "제출물", "제출 가능"]),
        f"courses/{mech[0]}/assignments.txt": assignment_list(mech[0], mech[1], [
            (92003, "Lab Report 3: Tensile Test", "10월 31일 오후 11:59", 10)]),
        f"courses/{mech[0]}/assignments/92003.txt": page([
            "Lab Report 3: Tensile Test", "과제 제출", "마감 10월 31일 기준 오후 11:59 점수 10 제출물 유형 파일 업로드",
            "가능 2026년 10월 24일 오전 12:00 - 2026년 10월 31일 오후 11:59 (KST)",
            "Submit your tensile test report (DOCX or PDF) with objective, method, results and discussion.", "", "제출물", "제출 가능"]),
        f"courses/{mat[0]}/announcements.txt": announcement_list(mat[0], mat[1], []),
        f"courses/{mech[0]}/announcements.txt": announcement_list(mech[0], mech[1], []),
    }
    write_mock(root, pages, {})


def case_native(root):
    cid, name = 55001, "2026-2 상평형 계산 실습 (001)"
    problem = pdf([["HW2: Phase equilibria of the fictional A-B system",
                    "1. Build a compound database (A(s), B(s), AB2(s)) and a liquid solution database.",
                    "2. Calculate the phase diagram, the activity of A in the liquid and the enthalpy of mixing.",
                    "Submit the final compound and solution databases and all three calculated figures."]])
    (root / "HW2_2026-12345.zip").write_bytes(zip_bytes([
        ("database/MYBASE.CDB", native(1, 6000)), ("database/MYSOLN.SLN", native(2, 4000)),
        ("figures/phase.fig", native(3, 9000)), ("figures/activity.fig", native(4, 7000)), ("figures/dH.fig", native(5, 5000))]))
    pages = {
        "index.txt": dashboard([(cid, name), SOLID]),
        f"courses/{cid}/assignments.txt": assignment_list(cid, name, [(93002, "HW2: 상평형 계산", "10월 30일 오후 11:59", 50)]),
        f"courses/{cid}/assignments/93002.txt": page([
            "HW2: 상평형 계산", "과제 제출", "마감 10월 30일 기준 오후 11:59 점수 50 제출물 유형 파일 업로드 파일 형식 zip",
            "가능 2026년 10월 16일 오전 12:00 - 2026년 10월 30일 오후 11:59 (KST)",
            "최종 database(compound와 solution 모두)와 계산 결과 그림(.fig)을 zip 하나로 모아 제출하세요.", "", "제출물", "제출 가능"]),
        f"courses/{cid}/announcements.txt": announcement_list(cid, name, [
            (80102, "교수 오평형", "HW2 문제지", "모든 섹션", "HW2 문제지를 첨부합니다.", "2026년 10월 16일 오전 9:00")]),
        f"courses/{cid}/discussion_topics/80102.txt": page(
            ["교수 오평형", "HW2 문제지", "모든 섹션", "HW2 문제지를 첨부합니다.", "HW2_problem.pdf"],
            [("HW2_problem.pdf", f"/courses/{cid}/files/700201/download")]),
    }
    write_mock(root, pages, {"HW2_problem.pdf": problem})


def blank_pdf(pages):
    from pypdf import PdfWriter
    writer = PdfWriter()
    for _ in range(pages):
        writer.add_blank_page(width=595, height=842)
    writer.add_metadata({"/Producer": "snu-submit eval fixture"})
    out = io.BytesIO()
    writer.write(out)
    return out.getvalue()


def case_blank(root):
    cid, name = WRITE
    (root / "writing6_essay.pdf").write_bytes(blank_pdf(2))
    pages = {
        "index.txt": dashboard([WRITE, ALGO]),
        f"courses/{cid}/assignments.txt": assignment_list(cid, name, [
            (94005, "주간 글쓰기 5: 요약문", "10월 24일 오후 11:59", 5), (94006, "주간 글쓰기 6: 비평문", "10월 31일 오후 11:59", 5)]),
        f"courses/{cid}/assignments/94006.txt": page([
            "주간 글쓰기 6: 비평문", "과제 제출", "마감 10월 31일 기준 오후 11:59 점수 5 제출물 유형 파일 업로드 파일 형식 pdf",
            "가능 2026년 10월 25일 오전 12:00 - 2026년 10월 31일 오후 11:59 (KST)",
            "영화나 책 한 편에 대한 비평문(1,500자 내외)을 PDF 1개로 제출하세요. 2쪽을 넘기지 마세요.", "", "제출물", "제출 가능"]),
        f"courses/{cid}/announcements.txt": announcement_list(cid, name, [
            (81101, "강사 문장원", "주간 글쓰기 제출 안내", "1 섹션\n2026-2 글쓰기의 기초 (015)",
             "주간 글쓰기는 매주 금요일 자정까지 eTL에 PDF로 올립니다.", "2026년 9월 2일 오후 2:00")]),
        f"courses/{cid}/discussion_topics/81101.txt": notice_detail(
            "강사 문장원", "주간 글쓰기 제출 안내", "1 섹션\n2026-2 글쓰기의 기초 (015)",
            "주간 글쓰기는 매주 금요일 자정까지 eTL에 PDF로 올립니다.", "2026년 9월 2일 오후 2:00"),
    }
    write_mock(root, pages, {})


CASES = {"normal-submission-pass": case_normal, "missing-and-empty-items": case_missing, "blank-pdf-needs-changes": case_blank,
         "unreadable-file-needs-confirmation": case_unreadable, "ambiguous-assignment-asks": case_ambiguous,
         "native-format-limited-pass": case_native}

if __name__ == "__main__":
    for case, build in CASES.items():
        files = HERE / case / "files"
        if files.exists():
            shutil.rmtree(files)
        files.mkdir(parents=True)
        build(files)
        print(case, sorted(p.relative_to(files).as_posix() for p in files.rglob("*") if p.is_file()))
    sys.exit(0)
