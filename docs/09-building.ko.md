# 09 — 빌드와 테스트

읽기: [English](./09-building.md) · **한국어**

이 저장소가 만들어내는 산출물별 빌드 방법, 빌드가 실제로 성공했는지 확인하는
방법, 그리고 여기서 실제로 디버깅 시간을 잡아먹었던 빌드 관련 함정들을 정리한다.
adk-cc를 *실행*만 하려면 README의 Quick start로 충분하다. 이 문서는 코드를
고치는 사람을 위한 것이다.

## 무엇이 빌드되는가

| 산출물 | 명령 | 출력 |
|---|---|---|
| Python 환경 | `uv sync` | `.venv/` |
| 웹 UI 번들 | `npm --prefix web run build` | `web/dist/` |
| 데스크톱 UI 번들 | `npm --prefix web run build:desktop` | `web/dist-desktop/` |
| 데스크톱 앱 | `cargo build --manifest-path src-tauri/Cargo.toml` | Tauri 바이너리 + 사이드카 |
| Python 휠 | `uv build` | `dist/*.whl` |

두 UI 번들은 **같은 소스를 두 번 빌드한 것**이고, 차이는 `VITE_ADK_CC_DESKTOP=1`
뿐이다. 공용 UI 코드를 고치고 한쪽만 다시 빌드하는 것이 낡은 번들을 테스트하게
되는 가장 흔한 경로다.

어느 번들을 서빙하느냐가 사용자가 보는 **셸**을 결정하며, 실제 사용자가 여기서
막혔다: 백엔드의 `ADK_CC_DESKTOP=1`은 런타임 스위치(데스크톱 라우트, 로컬
아티팩트, 프로젝트 레지스트리)인 반면 셸은 빌드 시점에 번들에 박히므로,
데스크톱 모드 백엔드가 웹 앱을 서빙하곤 했다 — 프로젝트 레일도 파일 트리도 없이,
이유를 알려주는 것도 없이. 이제 `ADK_CC_UI_DIST`는 데스크톱 모드이고 해당 빌드가
있으면 `web/dist-desktop`을 기본값으로 쓰고, 없으면 `build:desktop`을 알려주는
경고를 남긴다. [08-desktop-app.ko.md](./08-desktop-app.ko.md) 참고.

## 사전 요구사항

- **uv** — 인터프리터와 패키지를 모두 공급한다. 에이전트의 분석 런타임도 실행
  시점에 `uv`를 호출하므로(아래 참고), 빌드하는 machine뿐 아니라 adk-cc를
  *실행하는* machine의 PATH에도 있어야 한다.
- UI 번들용 **Node 20+**.
- 데스크톱 앱을 빌드할 때만 **Rust + Tauri 사전 요구사항**.

## 빌드 시점 설정은 저장소 루트의 `.env`에 있다

`web/vite.config.ts`가 `envDir`를 저장소 루트로 지정하므로, `VITE_*` 변수는
`web/.env`가 아니라 **`<repo>/.env`**에서 읽힌다.

이 사실을 강조하는 이유는, 틀렸을 때 조용하고 또 사람을 오도하기 때문이다. 이
작업 중 `web/`을 grep한 결과는 HTML 미리보기의 스크립트가 꺼져 있다고 말했지만
실행 중인 앱에서는 켜져 있었다. 플래그가 루트 `.env`에 있었기 때문이다. `VITE_*`
플래그에 의존하는 사안은 빌드된 번들이나 실행 중인 DOM을 봐야만 결론이 난다.

```bash
# 런타임이 아니라 빌드에 영향을 준다 — 값을 바꾸면 다시 빌드해야 한다
VITE_ADK_CC_HTML_PREVIEW_ALLOW_SCRIPTS=1   # 미리보기에서 인터랙티브 차트 렌더링
```

## 빌드 검증

**`tsc --noEmit`이 아니라 빌드 명령을 쓴다.** `npm run build:desktop`은
`tsc -b`(project references)를 돌리고, `npx tsc --noEmit -p tsconfig.json`은
다른 설정을 써서 컴파일되지 않는 코드를 두 번이나 "깨끗하다"고 보고했다. 두 번
모두 그 뒤의 브라우저 테스트가 *이전* 번들을 대상으로 돌면서, 빌드된 적 없는
코드에 대해 초록불을 냈다.

```bash
npm --prefix web run build:desktop 2>&1 | grep -i "error TS"   # 출력 없음 = 정상
```

브라우저에서 확인할 UI 변경에도 똑같이 적용된다. 먼저 빌드하고, `error TS`가
없음을 확인한 뒤 테스트한다. 빌드에 실패한 번들에 대해서는 UI 테스트 통과가
아무것도 증명하지 못한다.

## 패키징: 스킬은 package data로 들어간다

`[tool.setuptools.packages.find]`는 `*.py`만 수집한다. 내장 스킬은 Markdown이라
`pyproject.toml`에 명시적으로 선언한다:

```toml
[tool.setuptools.package-data]
adk_cc = ["skills/**/*"]
```

이게 없으면 저장소 체크아웃에는 스킬이 전부 있는데 설치된 휠에는 하나도 없다 —
개발 중에는 보이지 않고 모든 사용자에게는 깨져 있는 상태다.
`tests/test_builtin_skills.py`가 실제 휠을 빌드해서 SKILL.md가 들어 있는지
확인한다.

## 분석 런타임은 실행 시점에 프로비저닝된다

`uv`는 에이전트의 데이터 분석 환경도 공급한다(각 워크스페이스 안의
`.adk-cc/analysis-env/`, 최초 사용 시 생성, 약 20~60초). 빌드 단계의 일부가
아니며 프로젝트별이므로, 새로 클론하거나 새 프로젝트를 만들면 한 번씩 비용을
치른다. 진행 중에는 UI에 칩이 표시되고, 상태는 프로비저닝을 유발하지 않고
읽을 수 있다:

```bash
curl 'localhost:8000/desktop/analysis-env?project_id=<id>&session_id=<sid>'
```

`ADK_CC_ANALYSIS_ENV=off`로 맨 `python3` 폴백을 쓸 수 있지만 권장하지 않는다
(기본 macOS에서는 데이터 패키지가 하나도 없는 Python 3.9다). 직접 관리하는
인터프리터 경로를 지정할 수도 있다.

## 테스트

모든 테스트 파일은 독립 실행 스크립트다 — pytest 러너도, 수집 단계도 없다:

```bash
.venv/bin/python tests/test_builtin_skills.py       # 파일 하나
```

유닛/통합 전체 훑기(약 3분, 121개 파일):

```bash
for f in tests/test_*.py; do
  .venv/bin/python "$f" >/tmp/out 2>&1 || echo "FAIL $f: $(tail -1 /tmp/out)"
done
```

### 테스트의 세 부류

- `tests/test_*.py` — 유닛 + 통합. 모델도 네트워크도 쓰지 않는다. 통과해야 한다.
- `tests/e2e_*.py` — 실제 서버, 대개 실제 브라우저(Playwright). 대부분 모델 없이
  돌고, 전제 조건이 없으면(`web/dist-desktop` 없음, Playwright 미설치) 깔끔하게
  스킵한다.
- `ADK_CC_LIVE=1 tests/e2e_*.py` — 실제 모델 턴을 쓰는 일부. opt-in이다.
  간격이 필요한지는 **엔드포인트에 달려 있다**: 이 테스트들이 고정해 쓰는
  ChatGPT 구독 경로(`chatgpt-codex/gpt-5.4-mini`)는 사실상 제한이 없어 반복
  실행해도 되고, API 키 엔드포인트는 제한이 있으므로 임의의 sleep 대신
  `ADK_CC_MODEL_MAX_RPM`을 쓴다.

  UI 단언이 에이전트의 그때그때 행동에 의존한다면 여러 번 돌려라. run-view
  테스트는 코드가 동일한데도 통과 → 실패 → 통과했다. 모델이 아티팩트 4개를
  한 이벤트로 낼 때도, 네 이벤트로 나눠 낼 때도 있는데 후자만 그룹핑 경로를
  실제로 지나갔기 때문이다. 한 번의 초록불이었다면 버그가 그대로 나갔다.

### 여기서 매번 문제를 일으킨 환경 변수 함정

저장소 `.env`는 실제 배포 설정이며 `ADK_CC_SANDBOX_BACKEND=daytona`도 들어 있다.
이를 무시하지 않는 테스트는 그 값을 물려받아 헷갈리는 방식으로 실패하고
(`daytona: backend used before ensure_workspace()`), 반대로
`ADK_CC_SKIP_DOTENV=1`로 무시하는 테스트는 실제 모델 설정까지 잃어서 라이브 턴이
전부 인증/연결 오류로 죽는다.

그래서:

```bash
# 모델 없는 테스트: 배포 설정을 무시한다
ADK_CC_SKIP_DOTENV=1 ADK_CC_SANDBOX_BACKEND=noop .venv/bin/python tests/test_read_file_limits.py

# 라이브 테스트: 실제 설정은 유지하고 스텁만 걷어낸다
env -u ADK_CC_API_KEY -u ADK_CC_SKIP_DOTENV ADK_CC_LIVE=1 \
  .venv/bin/python tests/e2e_markdown_table_ui.py
```

서버를 `ADK_CC_API_KEY=stub`으로 띄우는 라이브 테스트는 모든 턴이
`Connection error`로 죽는다 — 제품이 아니라 하네스의 문제다.

### 환경 변수를 추가할 때

`agents/adk_cc/config/schema.py`가 유일한 출처다. 거기에 선언되지 않은 환경
변수를 읽으면 `tests/test_config_schema.py`가 실패하고, 커밋된 `.env.example`은
스키마에서 생성된다:

```bash
.venv/bin/python -m adk_cc.config gen-env --out .env.example
```

추가하기 전에 같은 의미의 변수가 이미 있는지 확인한다. 테스트는 미등록 변수는
잡아내지만 동의어는 잡아내지 못하고, 하나의 개념에 이름이 둘이면 영원히 동기화
부담을 지게 된다.

## 이미 실패로 알려진 테스트

일부 실패는 현재 작업 이전부터 있었고, 동작이 깨진 것이 아니라 환경에 의존한다
(`test_admin_panel`, `test_grant_flow`, `test_session_title_plugin`,
`test_daytona_backend`, `test_sandbox_service_backend`,
`test_workspace_extra_roots`, `test_working_dirs_persist`). 내 변경이 무언가를
깨뜨렸는지 판단할 때는 빨간불을 곧바로 내 탓으로 보지 말고 기준 커밋에서 같은
테스트를 돌려 비교한다:

```bash
git worktree add -q --detach /tmp/baseline <작업-이전-커밋>
(cd /tmp/baseline && /path/to/.venv/bin/python tests/test_x.py)
```
