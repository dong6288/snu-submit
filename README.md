# SNU Submit

> **버전 `0.1.0-alpha.3` · 알파** — Codex 플러그인(스킬 `snu-submit`)과 로컬 검사 엔진

서울대 학생이 eTL에 과제를 내기 직전에 **파일 누락, 빈 필수 항목, 형식 오류, 공지로만 알린 요건**을 놓쳐 불이익을 받는 일을 줄이기 위한 도구입니다.

```text
학생: $snu-submit 이 파일 그대로 제출해도 돼?  (report.docx 첨부)
SNU Submit: eTL 로그인을 확인했고, 문서 제목과 과제 목록으로 「Summary Report (Week 3)」를 찾았습니다.
            과제 본문은 비어 있지만 9/3 공지에 "주간 보고서는 1쪽 분량의 PDF로 제출"이 있습니다.
            판정: 수정 필요 — report.docx를 PDF로 변환해 올리세요. 본문(1,880자)은 비어 있지 않습니다.
            제출 기간: 화면에 제출 가능 표시가 없어 지금 제출 가능한지는 확인하지 못했습니다.
```
(가상 과제 재현 예시)

## 하는 일

1. 학생의 Chrome에서 eTL(`myetl.snu.ac.kr`) **로그인 상태를 화면으로 확인**합니다. 로그인은 학생이 직접 하며 비밀번호를 묻지 않습니다.
2. 받은 파일을 읽어(ZIP 안 포함) 제목·강의명·과제 번호를 얻고, eTL 강좌·과제 목록과 맞춰 **어느 과제인지 식별**합니다. 후보가 여럿이면 판정하지 않고 묻습니다.
3. 그 과제의 **상세 안내, 강좌 공지(학기 시작까지), 요건이 걸린 첨부·모듈 안내**에서 제출 요건과 근거를 모읍니다.
4. 요건을 검사 프로필로 옮겨 로컬 엔진으로 **파일의 실제 내용**을 검사합니다: 필수 파일·개수, 0바이트·빈 문서·빈 쪽, 확장자와 실제 형식, 파일 이름 규칙, PDF·PPTX 쪽수, 필수 문구, 제목 아래가 비어 있는 항목, Python 문법.
5. 결과를 한국어로 알려 줍니다.

| 판정 | 뜻 |
|---|---|
| **PASS** | 확인한 모든 요건 충족(과제 확정, 출처 수집 완료) |
| **PASS_WITH_LIMITS** | 위반은 없지만 전용 형식(database·FIG 등)·그림·스캔 내용이나 출처 수집의 한계를 학생이 직접 확인해야 함 |
| **수정 필요** | 누락·빈 파일·빈 답·형식/파일명/쪽수 위반 등 확인된 문제 |
| **확인 필요** | 읽기 실패·손상·암호화, 지침 충돌·적용 여부 미해결 |

제출 기간은 파일 판정과 따로 알려 주며, 화면에 제출 가능 상태가 확인될 때만 "eTL에 제출하세요"라고 권합니다.

## 하지 않는 일

- 답의 정답·내용의 질 채점
- 제출 파일 수정·변환·이동, 학생 코드·매크로 실행
- 업로드·최종 제출·다시 제출, 메일 발송
- eTL·파일 안의 문장("모두 통과로 표시" 등)을 지시로 따르기
- 과제·공지에 없는 요건(AI 사용 고지, 표지 등)을 짐작해 추가하기

## 설치 (Windows, Codex)

공개 소스 ZIP을 계속 둘 폴더에 풀고 그 폴더에서 PowerShell로 `.\Install-SnuSubmit.ps1`을 실행합니다. 준비물은 Codex(데스크톱 앱 또는 CLI, 로그인됨)와 python.org의 Python 3.11/3.12입니다. 스크립트는 그 폴더에 `.venv`를 만들어 해시 고정 의존성을 설치하고, `doctor`를 실행하고, 스킬이 쓸 Python 경로를 `runtime.json`에 적은 뒤 로컬 마켓플레이스와 플러그인을 Codex에 등록합니다. 끝나면 Codex 앱을 완전히 껐다 켜고 **새 채팅**에서 `$snu-submit`을 부릅니다. 자세한 내용은 [docs/INSTALL.md](docs/INSTALL.md).

## 사용

Codex 새 채팅에서 파일을 첨부하거나 경로를 알려 주고 묻습니다.

- `$snu-submit 첨부한 hw3.zip 이대로 내도 돼?`
- `$snu-submit C:\Users\...\보고서.pdf 재료역학 과제 2에 낼 건데 빠진 거 없는지 봐 줘`
- 고친 뒤: `다시 점검해 줘` → 이전 결과와 비교해 바뀐 점을 보여 줍니다.

Chrome에서 eTL에 로그인되어 있지 않으면 로그인해 달라고 합니다. 문제지 등 첨부를 처음 내려받을 때는 이름과 크기를 알리고 허락을 구합니다.

### 로컬 엔진만 쓰기

```text
python scripts/snu_submit.py inspect --file 제출.zip
python scripts/snu_submit.py review --file 제출.zip --profile profile.json --include-content [--previous 이전/report.json]
python scripts/snu_submit.py doctor
```

`review`는 `report.json`(기준)과 `report.html`을 쓰고 요건별 결과를 화면에 출력합니다. 출력은 기본으로 현재 폴더의 `.snu-submit/runs/` 아래 새 폴더입니다. 프로필 형식은 [references/profile.md](plugins/snu-submit/skills/snu-submit/references/profile.md). 이전 버전의 ZIP 진단 명령(`check`, `recheck`, `demo`, `sample`)도 그대로 있습니다. 엔진은 eTL에 접속하지 않습니다.

## 저장소 구성

- `plugins/snu-submit/skills/snu-submit/SKILL.md` — 에이전트 절차
- `references/` — `etl.md`(eTL 탐색·요건 판단), `profile.md`(검사 프로필·엔진 판정), `gmail.md`(선택), `evidence-api.md`(고급 API)
- `scripts/snu_submit.py`, `scripts/submit_checks/` — 로컬 검사 엔진(PDF·DOCX·PPTX·XLSX·HWPX·ZIP·텍스트·Python)
- `evals/` — skill-creator 형식의 평가 사례(가상 과제 재현, 고정 입력은 `make_fixtures.py`로 생성)
- `tests/` — 엔진·CLI·설치 스크립트·패키징 회귀 테스트
- `Install-SnuSubmit.ps1`, `tools/package_release.py` — 설치와 공개 소스 ZIP

## 검증 상태 (2026-10-25)

| 항목 | 결과 |
|---|---|
| 자동 테스트 `python -m unittest discover -s tests` | 96개 모두 통과(설치 스크립트 테스트 8개 포함). PowerShell 실행 정책이 스크립트를 막는 셸에서는 그 8개를 건너뜀 |
| skill-creator 평가(가상 eTL 6개 사례, 개선판과 이전판 비교) | 1차에서 찾은 문제(공지 상세를 열지 않음, 콘솔 한글 깨짐, 패턴 규칙을 엔진 소스에서 찾음)를 고친 뒤 2차 실행. 두 판 모두 판정 기준 100% 충족. 개선판은 토큰 15%, 시간 61초, 도구 호출 28%를 덜 씀. 빈 PDF는 개선판 엔진이 바로 수정 필요로 판정(이전 엔진은 PASS_WITH_LIMITS를 내 에이전트가 수치를 읽고 뒤집어야 했음) |
| 실제 eTL 화면 읽기(개발자 계정) | 로그인 화면 판별, 대시보드, 과제 목록·상세, 공지 목록·상세, 첨부 다운로드, 모듈을 Claude Code의 Chrome 연결로 읽기 전용 확인 |
| 실제 과제 파일 점검(개발자 본인 파일, 비공개) | FactSage 결과 ZIP: 원본 PASS_WITH_LIMITS, 파일을 뺀 사본 수정 필요, 빈 그림 사본 수정 필요. 주간 보고서 DOCX: 공지의 PDF 요구로 수정 필요, 내용을 지운 사본 수정 필요 |
| 격리 설치 시험(새 `CODEX_HOME`, 공개 소스 ZIP) | 마켓플레이스·플러그인 등록, `runtime.json` 작성, 설치된 캐시 사본에서 `doctor` 전 항목 통과, 기존 Codex 설정은 바뀌지 않음 |
| Codex 앱 + Codex Chrome 플러그인으로 실제 eTL 전체 흐름, 앱 재시작 후 스킬 인식 | **미검증** |
| 새 Windows 프로필 설치, 다른 학생 파일럿 | **미검증** |

실제 과제 파일과 점검 결과는 개인 자료라 저장소에 넣지 않습니다. [PRIVACY.md](PRIVACY.md)를 보세요.

## 라이선스

코드와 직접 만든 가상 예제는 [MIT License](LICENSE)를 따릅니다. 실제 학생·강의·서비스 자료에는 적용되지 않으며 그런 자료는 저장소에 포함하지 않습니다.
