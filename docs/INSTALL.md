# SNU Submit 설치 (0.1.0-alpha.3 · 알파)

Windows에서 공개 소스 ZIP으로 SNU Submit 플러그인을 Codex에 설치하는 방법입니다. 설치 결과(새 OS 프로필, 실제 eTL 사용 포함)는 이 문서가 보증하지 않으며, 설치한 사람이 따로 확인·기록합니다.

## 준비물

- Codex에 로그인되어 있어야 합니다. `codex` 명령이 `PATH`에 없으면 설치 스크립트가 Codex 데스크톱 앱에 들어 있는 CLI(`%LOCALAPPDATA%\OpenAI\Codex\bin\…\codex.exe` 중 가장 최근 것)를 씁니다. CLI는 `codex plugin add`와 `codex plugin marketplace list`를 지원해야 합니다.
- python.org에서 설치한 Python 3.11 또는 3.12. Microsoft Store의 `python` 별칭(실행 파일 없는 바로가기)은 쓸 수 없습니다.
- 인터넷 연결(pip가 `requirements.lock`에 해시로 고정된 휠 7개를 받을 때만 필요).
- 관리자 권한은 필요 없습니다. Gmail 등 다른 서비스도 필요 없습니다. eTL 점검에는 Codex의 Chrome 연결이 필요합니다.

## 설치 (한 번)

1. ZIP을 **계속 둘 폴더**에 풉니다(예: `%USERPROFILE%\Apps\snu-submit-0.1.0-alpha.3`). 설치 후 이 폴더를 지우거나 옮기지 마세요. Codex가 이 폴더를 로컬 마켓플레이스로 쓰고, 스킬이 이 폴더의 `.venv` Python을 씁니다.
2. 그 폴더에서 PowerShell을 열고 실행합니다.

   ```powershell
   .\Install-SnuSubmit.ps1
   ```

   Python이나 Codex를 직접 지정하려면 `-PythonExe C:\path\to\python.exe`, `-CodexExe C:\path\to\codex.exe`를 붙입니다.

   Windows 기본 설정에서는 "이 시스템에서 스크립트를 실행할 수 없으므로…"(실행 정책)로 막힐 수 있습니다. 시스템 정책을 바꾸지 않고 이번 한 번만 실행하려면 같은 폴더에서 다음을 씁니다(직접 판단해 실행하세요).

   ```powershell
   powershell -NoProfile -ExecutionPolicy Bypass -File .\Install-SnuSubmit.ps1
   ```
3. 성공하면 작업을 저장하고 Codex(ChatGPT) 데스크톱 앱을 **완전히 종료한 뒤 다시 엽니다.** 그다음 **새 로컬 채팅**에서 `$snu-submit`으로 요청합니다. 로컬 마켓플레이스·플러그인을 바꾼 뒤 앱을 다시 시작하고 새 채팅에서 확인하라는 것은 OpenAI 문서의 안내와 같습니다: <https://developers.openai.com/plugins/build/plugins>. 스크립트는 앱을 대신 종료하거나 다시 시작하지 않습니다.

이전 버전(alpha.1/alpha.2)이 **다른 폴더**에서 `snu-submit` 마켓플레이스로 등록되어 있으면 스크립트는 아무것도 바꾸지 않고 멈춥니다. 이전 등록을 직접 정리하거나(예: `codex plugin marketplace remove snu-submit`, 직접 확인 후 실행), 이전과 같은 폴더에 새 버전을 풀어 설치하세요.

### 확인한 것과 확인하지 않은 것

| 관찰 | 확인 방법 |
|---|---|
| 플러그인 등록(설치됨·사용 중) | 설치 스크립트 3단계, 또는 `codex plugin list --marketplace snu-submit --json` |
| 설치된 환경의 로컬 엔진 동작 | 설치 스크립트의 `doctor`, 아래 가상 예시(`sample`) |
| 스킬이 쓸 Python | 스킬 폴더의 `runtime.json`(`python` 값) |
| 데스크톱 앱의 자동 스킬 인식 | 앱을 다시 시작한 뒤 새 채팅에서 `$snu-submit`이 스킬로 인식되는지 직접 확인 |

이전 버전을 한 PC에서 설치했을 때는 등록과 같은 폴더 재설치가 성공했고 설치된 환경의 `doctor`와 `sample`도 동작했지만, 앱을 다시 시작하기 전의 채팅에서는 스킬이 자동으로 나타나지 않았습니다. 앱 재시작 후에도 스킬이 보이지 않으면 자동 인식이 확인되지 않은 것이며, 이때는 채팅에 설치된 스킬 파일의 절대 경로(`<설치 폴더>\plugins\snu-submit\skills\snu-submit\SKILL.md`)를 알려 주고 따르게 할 수 있습니다(대체 방법일 뿐 자동 인식의 증거가 아님).

설치 스크립트가 하는 일(각 명령의 종료 코드를 확인하고, 실패하면 `Installation did not complete.`와 함께 1로 끝납니다):

| 단계 | 내용 |
|---|---|
| 0 (확인만) | 패키지 파일 확인, 이 폴더·상위 폴더와 기존 `.venv` **전체 트리**에 링크/정션/재분석 지점이 없는지 확인(따라 들어가지 않음), 기존 `.venv`가 실제 가상환경인지 확인, Codex CLI 확인(없으면 데스크톱 앱 CLI), `codex plugin marketplace list --json`이 `marketplaces` 배열을 가진 JSON 객체인지와 같은 이름의 다른 폴더 마켓플레이스가 없는지 확인 |
| 1 | (없을 때만) `<python> -m venv <폴더>\.venv` 후 새 트리도 같은 방식으로 확인, `<.venv python> -m pip install --no-cache-dir --require-hashes --only-binary=:all: -r ...\requirements.lock`, `<.venv python> ...\scripts\snu_submit.py doctor`, `…\skills\snu-submit\runtime.json`에 `.venv` Python 경로 기록 |
| 2 | (이 폴더를 가리키는 마켓플레이스가 없을 때만) `codex plugin marketplace add <폴더> --json` |
| 3 | `codex plugin add snu-submit@snu-submit --json`, 그다음 `codex plugin list --marketplace snu-submit --json`의 `installed`에 `name`과 `marketplaceName`이 `snu-submit`이고 `installed`·`enabled`가 `true`인 항목이 있어야 성공 |

- 0단계에서 걸리면 아무것도 만들거나 설치하지 않고 멈춥니다.
- 다시 실행하면 이 폴더의 호환되는 `.venv`와 이 폴더를 가리키는 마켓플레이스를 그대로 씁니다.
- 실행 정책과 인증 파일은 바꾸지 않고 아무것도 지우지 않습니다. 바뀌는 Codex 설정은 `snu-submit` 마켓플레이스·플러그인 등록뿐입니다. 파일은 이 패키지 폴더 안(`.venv`, `runtime.json`, Python의 `__pycache__`)에만 쓰고, pip는 `--no-cache-dir`로 실행합니다. 실행 정책 때문에 스크립트가 막히면 정책을 바꾸지 말고 위 표의 명령을 같은 순서로 직접 실행하세요(`runtime.json`은 `{"python": "C:/Apps/snu-submit-0.1.0-alpha.3/.venv/Scripts/python.exe"}`처럼 실제 경로로 직접 만들 수 있습니다).

## 설치 확인용 로컬 예시 (가상 과제 재현)

```powershell
& "<설치 폴더>\.venv\Scripts\python.exe" "<설치 폴더>\plugins\snu-submit\skills\snu-submit\scripts\snu_submit.py" sample --output "$env:TEMP\snu-submit-sample-1"
```

`<새 폴더>\demo\index.html`에서 사례별 결과를 봅니다. 원본 복사 사례는 `profile_pass`, 필수 파일을 지우거나 비운 사례와 표시 구간을 지운 사례는 `needs_changes`로 나와야 합니다. 이것은 가상 ZIP의 프로필 항목 결과일 뿐 실제 과제의 제출 준비 판정이 아닙니다.

## 실제 사용 시 주의

- 에이전트는 먼저 학생의 Chrome에서 eTL 로그인을 화면으로 확인합니다. 설치가 끝났다고 로그인이 확인된 것은 아닙니다.
- 기본 파일 점검은 `snu_submit.py inspect`와 `review`입니다. 프로필 형식은 [profile.md](../plugins/snu-submit/skills/snu-submit/references/profile.md).
- 업로드·최종 제출·메일 발송은 하지 않습니다. 제출은 학생이 직접 합니다.
- 실제 과제 파일로 만든 출력(`.snu-submit/runs/`)과 내려받은 첨부는 개인 자료입니다. 공유하거나 저장소에 넣지 마세요.

## 공개 소스 ZIP 만들기 (배포하는 사람용)

```powershell
python tools\package_release.py --output <새 경로>\snu-submit-0.1.0-alpha.3.zip
```

- 출력은 새 `.zip` 경로여야 하며(덮어쓰지 않음), 포장 대상 폴더 안에 둘 수 없습니다.
- 같은 소스에서는 바이트까지 같은 ZIP이 나옵니다(파일 이름순, 고정 시각 1980-01-01, 고정 권한, 무압축).
- 모든 항목은 `snu-submit-0.1.0-alpha.3/` 아래에 있고, 마지막 항목 `SHA256SUMS`에 나머지 항목의 SHA-256이 들어 있습니다.
- `tools/package_release.py`의 `ALLOWLIST`에 적힌 **정확한 파일 경로**만 넣습니다(폴더·확장자 단위로 넣지 않음). 문서, 설치 스크립트, 매니페스트, 스킬(`SKILL.md`, `agents/openai.yaml`, `pyproject.toml`, `requirements.lock`, `references/*.md` 4개, `assets/`, `scripts/`), 테스트가 들어갑니다. `runtime.json`, 평가 사례(`evals/`), 실행 기록·보고서·캐시·자격 증명은 넣지 않습니다. 목록의 경로가 없거나 일반 파일이 아니거나 링크·정션이면 포장을 거부합니다.
