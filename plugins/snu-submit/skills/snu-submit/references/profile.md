# file-review/1 프로필과 파일 검사

대상: `0.1.0-alpha.3`. 엔진은 로컬 파일만 읽는다. eTL 접속·업로드·코드 실행·채점은 하지 않으며, 요건과 근거는 호스트가 eTL에서 모은 것을 프로필에 적는다.

목차: 1 명령 · 2 프로필 형식 · 3 패턴과 항목 검사 · 4 엔진 판정 · 5 형식별 범위 · 6 예시 · 7 이전 archive-profile 명령

## 1. 명령

```text
python snu_submit.py inspect --file A [--file B ...] [--output 새폴더]
python snu_submit.py review  --file A [--file B ...] --profile profile.json [--output 새폴더] [--include-content] [--previous 이전/report.json]
python snu_submit.py doctor
```

- `inspect`: 요건 없이 파일(ZIP 안 포함)을 읽어 형식·크기·쪽수·앞부분 글자와 제한을 보여 주고 `inspection.json`에 전체 글자와 위치를 남긴다. 과제 식별과 프로필 작성에 쓴다.
- `review`: 프로필로 검사해 `report.json`(기준)과 `report.html`을 쓰고 화면에 요건별 결과를 한국어로 출력한다. `--include-content`는 추출 글자를 보고서에 넣는다(내용 대조에 필요). `--previous`는 파일 해시·요건 결과의 변화를 보여 주며 이전 결과를 재사용하지 않는다.
- 출력은 항상 새 폴더다. 생략하면 현재 폴더의 `.snu-submit/runs/<시각>-<명령>-<번호>/`. 입력 파일을 품는 위치나 링크·정션은 거부한다.
- 종료 코드: 0 = 완료(판정은 출력을 읽음), 1 = 거부·실패(결과 없음), 2 = 사용법 오류.

## 2. 프로필 형식

```json
{
  "profile_version": "file-review/1",
  "assignment": "가상 강좌 「데이터구조」 002 · 과제 2",
  "sources": ["과제 상세 https://myetl.snu.ac.kr/courses/<ID>/assignments/<ID> (10/25 확인)", "공지 10/22 '[정정] 과제 2 제출물'"],
  "requirements": [
    {"id": "report", "description": "보고서 DOCX", "evidence": "과제 안내: \"보고서(report.docx)…\"", "patterns": ["*.zip!report.docx"]}
  ],
  "source_limits": []
}
```

`profile_version`과 `requirements`만 필수다. `assignment`, `sources`(확인한 출처 설명), `source_limits`(읽지 못한 첨부, 오프라인, 미확인 공지 등 실제 수집 한계)는 보고서에 그대로 실린다. `source_limits`가 있으면 결과는 최대 PASS_WITH_LIMITS다. 과제 혼동·지침 충돌·적용 여부 미해결은 한계가 아니라 확인 필요 사유이므로 여기 넣어 덮지 않는다.

요건 키:

| 키 | 기본값 | 뜻 |
|---|---|---|
| `id`, `description`, `evidence` | 필수 | 식별자, 학생에게 보일 설명, 근거 원문 인용과 출처 |
| `patterns` | 필수 | 대응 파일 패턴 목록(3절) |
| `min_count`, `max_count` | 1, null | 대응 파일 개수 범위. 단일 파일 요구나 초안/최종본 중복은 `max_count: 1` |
| `formats` | [] | 허용 확장자(`pdf`, `docx`, `zip` …). 다르면 형식 위반 |
| `name_globs` | [] | 파일 이름 규칙(폴더 없이 이름만, 예: `*_*_HW3.pdf`) |
| `pages` | null | `{"min": n, "max": n}`. PDF·PPTX만 셀 수 있고, DOCX 등은 확인 필요 |
| `text_contains` | [] | 반드시 들어 있어야 하는 문구(공백·대소문자 무시) |
| `sections` | [] | `[{"heading": "2. 실행 결과", "until": "3. 결론"}]` — 제목 아래 내용이 비어 있지 않아야 함 |

요건은 데이터다. 코드·정규식·셸 명령을 넣지 않는다. 과제가 말하지 않은 조건은 만들지 않는다.

## 3. 패턴과 항목 검사

패턴:
- 직접 고른 파일은 파일 이름(`report.pdf`), ZIP 안은 `바깥.zip!안/경로`(`*.zip!src/*.py`).
- 대소문자를 무시하고 유니코드 정규화(NFC)를 맞춘다. `*`는 `/`와 `!`까지 건너뛴다.
- 한 요건의 `patterns`는 '또는'이다. 하나라도 맞으면 그 요건의 대응 파일이고, 한 파일이 여러 요건에 대응할 수도 있다.
- `__MACOSX/`, `._*`, `.DS_Store`, `Thumbs.db`는 운영체제 부산물로 보고 대응하지 않는다.

| 패턴 | 맞음 | 맞지 않음 |
|---|---|---|
| `report.pdf` | 직접 고른 `report.pdf` | `hw.zip!report.pdf` |
| `*.zip!report.pdf` | `hw.zip!report.pdf` | `hw.zip!docs/report.pdf` |
| `*.zip!*report.pdf` | `hw.zip!docs/report.pdf` | 직접 고른 `report.pdf` |
| `*report*.pdf` | `report.pdf`, `hw.zip!docs/final_report.pdf` | `report.docx` |
| `*.pdf` | 직접 고른 PDF와 **ZIP 안의 PDF 모두** | — |
| `*.zip!*case05/*.fig` | `w.zip!results/HW1_case05/a.FIG` | `w.zip!results/HW1_case5/a.fig` |
- 과제가 파일 이름을 정했으면 그 이름을, 역할만 정했으면 역할에 맞는 패턴(`*.pdf`, `*.zip!database/*.cdb`)을 쓴다. 이름이 비슷하다는 이유만으로 다른 역할의 파일을 대신 인정하지 않는다.
- 여러 개를 모두 내야 하면(예: 9개 경우의 그림) 하나씩 요건을 만든다. `min_count: 9` 하나로 묶으면 한 경우가 빠져도 다른 파일 수로 채워질 수 있다.

항목 검사(`sections`):
- `heading`은 `inspect` 출력에서 본 제목 줄을 그대로 쓴다. 줄 맨 앞에서 시작하는지 공백을 무시하고 보며, "문제 1"이 "문제 10"에 붙지 않도록 낱말 경계를 지킨다.
- 내용은 제목 다음 줄부터 `until` 제목(없으면 같은 제목의 다음 등장 또는 끝)까지다. 제목이 `:`로 끝나면("답:") 같은 줄의 뒷부분도 내용으로 센다. 그 밖에는 제목 줄에 붙은 배점·문제 문장을 답으로 세지 않는다.
- 목차 줄("2. 실행 결과 ........ 3")은 건너뛴다.
- 결과에는 내용 글자 수와 발췌가 있다. 발췌가 문제 문장뿐이면 답이 없는 것이니 직접 읽고 판단한다.
- 내용이 비었는데 그 범위에 그림·수식 등 읽지 못한 부분이 있으면 위반이 아니라 확인 필요다.

## 4. 엔진 판정

파일마다 먼저 자기 상태를 정한다.

| 파일 상태 | 경우 |
|---|---|
| 위반 | 0바이트, 공백뿐인 텍스트, 빈 문서, 쪽이 없거나 모든 쪽이 빈 PDF, 확장자와 실제 형식이 다름(예: `.pdf`인데 Word), Python 문법 오류 |
| 확인 필요 | 열 수 없음·손상·암호화, 위험한 압축 항목 이름, 7z·RAR 등 열지 못한 압축, 중첩 압축, 검사 한도 초과, 검사 중 변경 |
| 제한부 통과 | 존재하고 비어 있지 않지만 주된 내용을 읽지 못함: 전용 형식(database·FIG 등), HWP·구형 DOC/XLS·그림 파일, 글자 없는 쪽(스캔·손글씨) |
| 통과 | 구조와 본문을 읽음. 그림·머리글·수식 배치 같은 부수 부분은 메모로만 남음 |

요건 상태:
- 개수: 쓸 수 있는(통과·제한부 통과) 대응 파일이 `min_count` 이상이어야 하고, 전체 대응 파일은 `max_count` 이하여야 한다. 개수가 채워졌으면 함께 대응된 빈 파일(예: 빈 `__init__.py`)은 주의로만 남는다.
- 없는 파일은 위반이다. 단 읽지 못한 압축 파일 안에 있을 수 있으면 확인 필요다.
- 형식·파일명·쪽수·문구·항목 검사는 위 규칙대로 위반·확인 필요를 더한다. 필수 문구가 없는데 파일에 읽지 못한 부분이 있으면 확인 필요다.

최종 판정(위에서부터):
1. 어떤 요건이든 위반 → `NEEDS_CHANGES`(수정 필요)
2. 요건의 확인 필요, 선택 파일의 읽기 실패, 압축 파일 구조 문제, 요건과 대응하지 않는 선택 파일의 문제 → `NEEDS_CONFIRMATION`(확인 필요)
3. 제한부 통과 요건이나 `source_limits`가 있음 → `PASS_WITH_LIMITS`
4. 그 밖 → `PASS`

요건과 대응하지 않은 ZIP 안 파일은 판정을 바꾸지 않고 "요건과 대응하지 않은 파일"로 보고된다. 엔진 PASS는 프로필의 요건 기준이다. 최종 PASS는 SKILL.md 7절의 출처·식별 조건도 갖춰야 한다.

## 5. 형식별 범위

| 형식 | 직접 확인 | 남는 한계 |
|---|---|---|
| ZIP | 안전한 경로, CRC, 해시, 빈 항목, 안의 PDF·Office·HWPX·텍스트·Python | 중첩 압축·암호화·손상·한도 초과는 확인 필요 |
| PDF | 쪽수, 쪽별 글자와 위치, 빈 쪽, 스캔 쪽 | 그림·수식의 시각적 내용, 암호 PDF |
| DOCX | 문단·표 글자와 위치, 수식 글자 | 실제 쪽 배치, 그림, 머리글·각주 |
| PPTX | 슬라이드 수와 글자 | 그림·차트·노트 |
| XLSX | 시트·셀·수식·저장된 계산값 | 다시 계산하지 않음, 차트 |
| HWPX | 문단·표·글상자 글자, 수식 스크립트 | 쪽 배치, 그림, 머리글·각주 |
| 텍스트·Python | UTF-8 원문, Python 문법 | 실행·정답 검증 없음 |
| database·FIG·HWP·DOC·XLS·그림 등 | 존재, 크기, 해시, ZIP 안 CRC | 내용은 전용 프로그램에서 직접 확인 |

제품 예산(과제 제한 아님): 파일 하나 50 MiB, 선택 파일 합계 200 MiB, ZIP 1,000항목·해제 읽기 200 MiB, 글자 100,000자, PDF 300쪽, 파서 30초.

## 6. 예시 (가상 과제 재현)

주간 보고서를 PDF 1쪽으로:

```json
{"id": "weekly-pdf", "description": "요약문 PDF 1쪽", "patterns": ["*"], "max_count": 1, "formats": ["pdf"], "pages": {"max": 1},
 "evidence": "공지(9/3): \"주간 보고서는 1쪽 분량의 PDF로 제출\"", "sections": [{"heading": "Summary Report (Week 3)", "until": null}]}
```

전용 프로그램 결과를 ZIP으로(각 경우를 따로 요건화):

```json
{"id": "case03", "description": "경우 (3) 결과 그림", "patterns": ["*.zip!*case03/*.fig"], "evidence": "문제지: \"9개 경우의 결과 그림을 모두 제출\""},
{"id": "compound-db", "description": "최종 compound database", "patterns": ["*.zip!*.cdb"], "evidence": "과제 안내: \"compound와 solution을 모두 포함한 database\""}
```
→ 모두 있으면 PASS_WITH_LIMITS("전용 형식 — 내용은 전용 프로그램에서 확인"), 하나라도 없으면 그 경우를 짚어 수정 필요.

코드와 보고서 ZIP, 정정 공지로 추가된 파일:

```json
{"id": "code", "description": "sort.py", "patterns": ["*.zip!sort.py"], "evidence": "과제 안내 …"},
{"id": "run-log", "description": "실행 로그", "patterns": ["*.zip!run_log.txt"], "evidence": "10/22 공지: \"[정정] … run_log.txt를 포함\""},
{"id": "report", "description": "보고서 세 절", "patterns": ["*.zip!report.docx"], "evidence": "과제 안내 …",
 "sections": [{"heading": "1. 구현 방법", "until": "2. 실행 결과"}, {"heading": "2. 실행 결과", "until": "3. 결론"}, {"heading": "3. 결론", "until": null}]}
```

## 7. 이전 archive-profile 명령

`check`, `recheck`, `demo`, `sample`은 이전 버전의 ZIP 진단(archive-profile/1: 필수 항목, 비어 있지 않은 항목, 줄 표지로 감싼 텍스트 구간)과 복사본 결함 주입 시연을 그대로 제공한다. 새 점검에는 `review`를 쓴다. `sample`은 계정·네트워크 없이 가상 ZIP으로 설치를 확인하는 데 쓸 수 있다(`profile_pass`는 프로필 항목만 통과했다는 뜻이며 제출 준비 판정이 아니다).
