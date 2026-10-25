# 출처 해시·인용 검증 API (고급)

대상: `0.1.0-alpha.3`. **기본 흐름(SKILL.md, `inspect`/`review`)에서는 쓰지 않는다.** 출처 원문을 바이트 그대로 저장하고, 요구사항 인용이 정규화 원문의 정확한 구간과 일치하는지까지 엔진이 검증해야 할 때만 쓴다. 함수와 JSON 스키마는 `scripts/submit_checks/core.py`, `report.py`, `assets/schemas/`에 있다. 이 저장소에는 이 API 전체 흐름의 자동 테스트가 없으므로 실험적 기능으로 다룬다. 적용·정정·충돌·날짜 판단은 [etl.md](etl.md) 6절을 따른다.

호출 순서(같은 `ReadBudget`): `verify_sources` → `bind_selection` → `verify_requirements` → `assemble_checks` → `assemble_report` → `report.render_report`. 재검사 비교는 `compare_report_delta`, 최신성은 `evaluate_freshness`. 실제 필드·허용 값은 스키마가 우선한다.

## 1. 실행 폴더

```text
<작업 위치>/.snu-submit/runs/<run_id>/
  sources/raw/          호스트 도구 원문 출력·다운로드 바이트(그대로)
  sources/normalized/   엔진이 만든 정규화 텍스트(NFC, LF, UTF-8)
  source*.json, requirements.json, selection.json, coverage.json, freshness.json
  report.json / report.html
```

작업 위치는 제출 폴더와 겹치지 않아야 하며, 지난 실행 폴더는 고치지 않는다.

## 2. 출처 기록

`source_id`, `kind`(과제 상세·공지·첨부·연결 문서·메일·사용자 제공 문서), 관찰한 `course_id`·`assignment_id`·`section_id`·`audience`(모르면 `"unknown"`), `original_url`·`final_url`, `observed_at`(시간대 포함), `raw_ref`, `collection_status`(`complete`/`partial`/`failed`)와 이유, `locations`, `issued_at`·`modified_at`, `unread_regions`. 해시는 모델이 적지 않고 엔진이 바이트에서 계산한다. 참조 경로는 실행 폴더 안이어야 하고 링크를 허용하지 않는다.

## 3. 요구사항 기록

`requirement_id`, `source_id`, `quote`(정규화 텍스트에서 그대로 복사), `span`(`normalized_text[start:end] == quote`, 코드 포인트 기준), `axis`(`instructor_requirement` / `system_constraint`), `applicability`, `mandatory`, `check`(`kind`·`operator`·`value`·`unit`·대상), `state`(`confirmed`/`ambiguous`/`conflict`), 명시적 정정일 때만 `supersedes`. 인용·구간·해시가 맞지 않거나 읽지 못한 영역에 기댄 요구사항은 근거로 인정하지 않는다. 의미 판단 항목은 특정 파일·위치에 대한 사용자 확인 기록이 있을 때만 `pass`가 될 수 있고, "다 괜찮아" 같은 일괄 승인은 아무것도 바꾸지 못한다.

지원 검사: 파일 존재·개수, 이름(정확/안전한 glob), 확장자·실제 형식, 바이트 크기(MB처럼 10진/2진이 불명확하면 두 해석 모두 같을 때만 판정), PDF 쪽수, ZIP 내부 항목, DOCX 표, 글자·단어 수(단위가 명확할 때만), 특정 내용 포함(사용자 확인 전까지 `unknown`).

## 4. 제출 파일 선택과 대상

선택 목록은 `submission_root`와 `files: [{file_id, relative_path, role}]`, 제외 목록과 이유를 담는다. 초안/최종본처럼 모호하면 선택은 `unknown`이다. 대상은 `submission_set` / `file` / `archive_entry`, 선택자(역할·정확한 이름·안전한 glob·내부 경로)와 `cardinality`·`quantifier`로 지정한다. 없으면 `missing_target`이며 해시를 지어내거나 다른 파일로 대체하지 않는다.

## 5. API의 종합 판정

1. 필수 항목에 `fail` → `수정 필요`(`unknown` 개수 함께 표시)
2. 필수 `unknown`, 수집 범위 미완료, 해결되지 않은 충돌·적용 대상·날짜·파일 선택, 파싱 실패, 예산 초과, 오래된 출처 → `확인 필요`
3. 비어 있지 않은 필수 조건이 모두 평가되고 위반·`unknown`이 없을 때만 `확인된 요구사항 기준 파일 준비됨`

같은 실행 안의 최신성 재수집 기록이 없으면 3에 도달할 수 없다. 가상 예제는 `가상 과제 재현` 표시를 유지한다.

## 6. 재검사

이전 실행의 파일·출처 해시와 현재 바이트를 비교해 바뀐 바이트, 이름 변경, 누락, 추가를 구분한다. 바뀐 대상에 영향받는 항목은 현재 바이트로 다시 평가하며 이전 `pass`를 재사용하지 않는다. 검사 중 파일이 바뀌면 그 결과는 `unknown`이다.

## 7. 보고서

JSON이 기준이고 HTML은 같은 내용을 인라인 CSS만으로 보여 준다(스크립트·외부 요청 없음, 모든 값 이스케이프). 스키마·도구·규칙 버전, 실행 ID, 시각, 출처 해시, 선택 목록과 해시, 수집 범위와 `unknown` 이유, 항목별 근거와 수정 방법, 스냅샷 한계 안내를 담는다.
